"""국면별 투자 추천 엔진 (Phase 4).

현재 국면 → 유리 섹터(모멘텀 랭킹) → 그 섹터의 S&P500 종목을 펀더멘털+시총으로 선별.
- 종목 유니버스 = 현재 국면 favored 섹터 중 '양(+) 모멘텀 상위' 섹터(패널과 일관).
- 종목 점수 = trading_america 펀더멘털 3팩터(value/momentum/dividend) + 국면별 시가총액 틸트.
- TA 미가용 시 모멘텀/저변동성 + 시총 복합으로 폴백.
"""

from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select

from .config import load_universe
from .db import SessionLocal
from .models import MarketPrice, Recommendation, SP500Constituent
from .regime import current_regime

_TRADING_6M = 126
_TRADING_3M = 63
_MAX_PER_SECTOR = 5      # 종목 섹터 분산
_CANDIDATE_CAP = 45      # 펀더멘털 스코어링 전 모멘텀 프리필터 상한(속도)


def _etf_momentum_from_db(session, symbols):
    out = {}
    for sym in symbols:
        rows = session.execute(
            select(MarketPrice.close).where(MarketPrice.symbol == sym).order_by(MarketPrice.obs_date)
        ).all()
        closes = [r[0] for r in rows]
        if len(closes) > _TRADING_6M:
            out[sym] = round((closes[-1] / closes[-1 - _TRADING_6M] - 1) * 100, 1)
    return out


def _stock_factors(symbols):
    """개별종목 6M 모멘텀(%) + 변동성(연율%). yfinance 배치."""
    import yfinance as yf
    df = yf.download(symbols, period="8mo", progress=False, auto_adjust=True, threads=True)
    if df is None or df.empty:
        return pd.DataFrame()
    close = df["Close"] if "Close" in df else df
    rows = []
    for sym in close.columns:
        s = close[sym].dropna()
        if len(s) < _TRADING_3M:
            continue
        mom6 = (s.iloc[-1] / s.iloc[-1 - min(_TRADING_6M, len(s) - 1)] - 1) * 100
        vol = s.pct_change().std() * np.sqrt(252) * 100
        rows.append({"symbol": sym, "mom6m": round(float(mom6), 1), "vol": round(float(vol), 1)})
    return pd.DataFrame(rows)


def _market_caps(symbols):
    """yfinance fast_info 로 시가총액(USD). 폴백 경로용."""
    import yfinance as yf
    out = {}
    try:
        tks = yf.Tickers(" ".join(symbols))
        for s in symbols:
            try:
                mc = tks.tickers[s].fast_info.get("market_cap") or tks.tickers[s].fast_info.get("marketCap")
                if mc:
                    out[s] = float(mc)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _zmap(d):
    """{key: value} → {key: z-score(clip±2)}. 표본<3 이면 0."""
    vals = {k: v for k, v in d.items() if v is not None and not pd.isna(v)}
    if len(vals) < 3:
        return {k: 0.0 for k in d}
    arr = np.array(list(vals.values()), dtype=float)
    mu, sd = arr.mean(), arr.std() or 1.0
    return {k: float(np.clip((v - mu) / sd, -2, 2)) if k in vals else 0.0 for k, v in d.items()}


def _fmt_cap(v):
    if not v:
        return "-"
    if v >= 1e12:
        return f"{v/1e12:.2f}T"
    return f"{v/1e9:.0f}B"


def _diversify(rows, top_n):
    """섹터당 _MAX_PER_SECTOR 제한하며 상위 top_n 선택(이미 점수 내림차순)."""
    out, cnt = [], {}
    for r in rows:
        g = r.get("gics", "")
        if cnt.get(g, 0) >= _MAX_PER_SECTOR:
            continue
        out.append(r); cnt[g] = cnt.get(g, 0) + 1
        if len(out) >= top_n:
            break
    return out


def recommend(top_sectors: int = 8, top_stocks: int = 15,
              use_ta: bool = True, as_of: str | None = None) -> dict:
    reg = current_regime()
    if "error" in reg:
        return reg
    regime = reg["regime"]
    uni = load_universe()
    pb = uni["regime_playbook"].get(regime, {})
    gics_to_etf = uni["gics_to_etf"]
    etf_to_gics = {v: k for k, v in gics_to_etf.items()}
    metric = pb.get("stock_metric", "momentum")
    size_tilt = uni.get("regime_size_tilt", {}).get(regime, 0.3)

    with SessionLocal() as session:
        index_recs = [{"symbol": s, "name": uni["index_etfs"].get(s, "")} for s in pb.get("index_pref", [])]

        # 섹터 ETF 모멘텀 랭킹
        sec_syms = pb.get("sector_etfs", [])
        sec_mom = _etf_momentum_from_db(session, sec_syms)
        sectors = sorted(
            [{"symbol": s, "name": uni["sector_etfs"].get(s, ""), "mom6m": sec_mom.get(s)} for s in sec_syms],
            key=lambda x: (x["mom6m"] is not None, x["mom6m"] or -999), reverse=True,
        )[:top_sectors]

        # ★ 섹터 일관성: 종목은 '양(+)모멘텀 상위 섹터'에서만 추출(없으면 상위 3개)
        strong = [s for s in sectors if (s["mom6m"] or -999) > 0]
        chosen = (strong or sectors)[:4]
        favored_gics = [etf_to_gics[s["symbol"]] for s in chosen if s["symbol"] in etf_to_gics]
        cons = session.execute(
            select(SP500Constituent.symbol, SP500Constituent.name, SP500Constituent.gics_sector)
            .where(SP500Constituent.gics_sector.in_(favored_gics))
        ).all() if favored_gics else []
        cmap = {c[0]: {"name": c[1], "gics": c[2]} for c in cons}

    stocks, stock_engine = [], "momentum"
    # 모멘텀 프리필터(배치) → 후보 축소(펀더멘털 스코어링 속도)
    fac = _stock_factors(list(cmap.keys())) if cmap else pd.DataFrame()
    if not fac.empty:
        asc = (metric == "low_vol")
        fac = fac.sort_values("vol" if metric == "low_vol" else "mom6m", ascending=asc)
        cand = fac.head(_CANDIDATE_CAP)["symbol"].tolist()
        mom_map = dict(zip(fac["symbol"], fac["mom6m"]))
        vol_map = dict(zip(fac["symbol"], fac["vol"]))

        # (A) trading_america 펀더멘털 3팩터 + 시총 틸트 (기본)
        rows = []
        if use_ta:
            from . import stockpicker_ta as ta
            df = ta.pick(regime, cand, as_of=as_of)
            if df is not None and not df.empty:
                stock_engine = f"trading_america 펀더멘털 3팩터 + 시총틸트(국면 가중 {ta.regime_weights(regime)})"
                caps = {r["ticker"]: r.get("market_cap") for _, r in df.iterrows()}
                size_z = _zmap(caps)
                for _, r in df.iterrows():
                    sym = r["ticker"]
                    if sym not in cmap:
                        continue
                    total = float(r.get("total_score", 0))
                    final = total + size_tilt * size_z.get(sym, 0) * 10  # 시총 틸트(±~14점)
                    rows.append({
                        "symbol": sym, "name": cmap[sym]["name"], "gics": cmap[sym]["gics"],
                        "related_etf": gics_to_etf.get(cmap[sym]["gics"], ""),
                        "final": round(final, 1), "total": round(total, 1),
                        "value": round(float(r.get("value_score", 0)), 1),
                        "momentum": round(float(r.get("momentum_score", 0)), 1),
                        "dividend": round(float(r.get("dividend_score", 0)), 1),
                        "mcap": r.get("market_cap"), "per": _r1(r.get("trailing_per")),
                        "roe": _pct(r.get("roe")), "divy": _pct(r.get("div_yield")),
                        "mom6m": mom_map.get(sym),
                    })

        # (B) 폴백: 모멘텀/저변동성 + 시총 복합
        if not rows:
            stock_engine = "모멘텀/저변동성 + 시총 (TA 미가용 폴백)"
            caps = _market_caps(cand)
            size_z = _zmap(caps)
            base = {s: (-vol_map[s] if metric == "low_vol" else mom_map[s]) for s in cand if s in mom_map}
            base_z = _zmap(base)
            for sym in cand:
                if sym not in cmap:
                    continue
                final = base_z.get(sym, 0) + size_tilt * size_z.get(sym, 0)
                rows.append({
                    "symbol": sym, "name": cmap[sym]["name"], "gics": cmap[sym]["gics"],
                    "related_etf": gics_to_etf.get(cmap[sym]["gics"], ""),
                    "final": round(final, 2), "mom6m": mom_map.get(sym),
                    "vol": vol_map.get(sym), "mcap": caps.get(sym),
                })

        rows.sort(key=lambda x: x["final"], reverse=True)
        stocks = _diversify(rows, top_stocks)

    result = {
        "as_of": reg["as_of"], "regime": regime, "regime_kr": reg["regime_kr"],
        "confidence": reg["confidence"], "provisional": reg["provisional"],
        "stance": pb.get("stance", ""), "region": pb.get("region", ""),
        "style": pb.get("style", ""), "currency": pb.get("currency", ""),
        "stock_factor": pb.get("stock_factor", ""), "stock_metric": metric,
        "stock_engine": stock_engine, "size_tilt": size_tilt,
        "stock_sectors": [c["symbol"] for c in chosen],
        "index": index_recs, "sectors": sectors, "stocks": stocks,
    }
    _persist(result)
    return result


def _r1(v):
    return round(float(v), 1) if v is not None and not pd.isna(v) else None


def _pct(v):
    return round(float(v) * 100, 1) if v is not None and not pd.isna(v) else None


def _persist(res: dict) -> None:
    from sqlalchemy import delete
    today = date.fromisoformat(res["as_of"]) if res.get("as_of") else date.today()
    with SessionLocal() as session:
        session.execute(delete(Recommendation))  # 추천은 항상 '현재 1세트'만 유지
        items = [Recommendation(obs_date=today, regime=res["regime"], layer="index",
                                symbol=r["symbol"], rank=0, rationale=r["name"]) for r in res["index"]]
        for i, s in enumerate(res["sectors"]):
            items.append(Recommendation(obs_date=today, regime=res["regime"], layer="sector",
                                        symbol=s["symbol"], rank=i + 1,
                                        rationale=f"{s['name']} 6M모멘텀 {s['mom6m']}%"))
        for i, s in enumerate(res["stocks"]):
            cap = _fmt_cap(s.get("mcap"))
            if "total" in s:
                detail = (f"종합 {s['final']} (V{s['value']}/M{s['momentum']}/D{s['dividend']})"
                          f" · 시총 {cap} · ROE {s.get('roe')}% · 6M {s.get('mom6m')}%")
            else:
                detail = f"점수 {s['final']} · 6M {s.get('mom6m')}% 변동성 {s.get('vol')}% · 시총 {cap}"
            items.append(Recommendation(obs_date=today, regime=res["regime"], layer="ticker",
                                        symbol=s["symbol"], rank=i + 1,
                                        rationale=f"{s['name']} [{s['related_etf']}] {detail}"))
        session.add_all(items)
        session.commit()
