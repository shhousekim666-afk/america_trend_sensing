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


def _rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    v = (100 - 100 / (1 + rs)).iloc[-1]
    return round(float(v), 0) if pd.notna(v) else None


def _stock_factors(symbols):
    """개별종목 6M 모멘텀·변동성 + 과열지표(200DMA 이격도·52주위치·RSI). yfinance 배치."""
    import yfinance as yf
    df = yf.download(symbols, period="14mo", progress=False, auto_adjust=True, threads=True)
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
        ma200 = s.tail(200).mean()
        dist200 = s.iloc[-1] / ma200 - 1 if ma200 else None              # 200DMA 이격도
        win = s.tail(252)
        rng = win.max() - win.min()
        pos52 = (s.iloc[-1] - win.min()) / rng if rng else 0.5           # 52주 위치(0~1)
        rows.append({"symbol": sym, "mom6m": round(float(mom6), 1), "vol": round(float(vol), 1),
                     "dist200": round(float(dist200) * 100, 1) if dist200 is not None else None,
                     "pos52": round(float(pos52), 2), "rsi": _rsi(s)})
    return pd.DataFrame(rows)


def _index_metrics(session, symbols):
    """지수 ETF 6M 모멘텀 + 200DMA 상회 여부(DB 종가)."""
    out = {}
    for sym in symbols:
        closes = [r[0] for r in session.execute(
            select(MarketPrice.close).where(MarketPrice.symbol == sym).order_by(MarketPrice.obs_date)
        ).all()]
        if len(closes) < 60:
            continue
        mom = (closes[-1] / closes[-1 - _TRADING_6M] - 1) * 100 if len(closes) > _TRADING_6M else None
        ma200 = sum(closes[-200:]) / min(200, len(closes))
        out[sym] = {"mom6m": round(mom, 1) if mom is not None else None,
                    "above_ma200": bool(closes[-1] > ma200)}
    return out


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
        # 지수 = 국면별 자산배분 바스켓(주식+지역+안전자산) + 200DMA·모멘텀·목표비중
        basket = uni.get("regime_index_basket", {}).get(regime, {})
        safe = set(uni.get("safe_assets", []))
        imx = _index_metrics(session, list(basket))
        index_recs = []
        for etf, wt in sorted(basket.items(), key=lambda x: -x[1]):
            m = imx.get(etf, {})
            index_recs.append({
                "symbol": etf, "name": uni["index_etfs"].get(etf, ""), "weight": wt,
                "asset": "안전자산" if etf in safe else "주식",
                "mom6m": m.get("mom6m"), "above_ma200": m.get("above_ma200"),
            })
        equity_pct = sum(wt for etf, wt in basket.items() if etf not in safe)

        # 섹터 ETF 모멘텀 랭킹(표시) + 종목 선별용 stock_gics ETF 모멘텀
        sec_syms = pb.get("sector_etfs", [])
        stock_gics_list = pb.get("stock_gics", [])
        stock_etfs = [gics_to_etf[g] for g in stock_gics_list if g in gics_to_etf]
        sec_mom = _etf_momentum_from_db(session, list(dict.fromkeys(sec_syms + stock_etfs)))
        sectors = sorted(
            [{"symbol": s, "name": uni["sector_etfs"].get(s, ""), "mom6m": sec_mom.get(s)} for s in sec_syms],
            key=lambda x: (x["mom6m"] is not None, x["mom6m"] or -999), reverse=True,
        )[:top_sectors]

        # ★ 종목 유니버스 = 플레이북 stock_gics(전략상 유리 업종)로 제한 → 모멘텀 순.
        #   (섹터 ETF 표시와 분리: 성장은 시클리컬 4섹터만 — 회복 섹터인 IT/반도체 누수 방지)
        ranked = sorted(stock_etfs, key=lambda s: (sec_mom.get(s) is not None, sec_mom.get(s) or -999), reverse=True)
        strong = [s for s in ranked if (sec_mom.get(s) or -999) > 0]
        chosen_etfs = strong or ranked[:4]
        favored_gics = [etf_to_gics[s] for s in chosen_etfs if s in etf_to_gics]
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
        dist_map = dict(zip(fac["symbol"], fac["dist200"]))
        pos_map = dict(zip(fac["symbol"], fac["pos52"]))
        rsi_map = dict(zip(fac["symbol"], fac["rsi"]))
        ohc = uni.get("overheat", {})
        gp = ohc.get("growth_penalty", 10)
        dist_z = _zmap({k: v for k, v in dist_map.items() if v is not None})  # 이격도 z(과열 강도)

        def _hot(sym):  # 과열: 200DMA 이격도 OR 52주위치 OR RSI
            d, p, rv = dist_map.get(sym), pos_map.get(sym), rsi_map.get(sym)
            return bool((d is not None and d > ohc.get("dist200_warn", 0.2) * 100)
                        or (p is not None and p > ohc.get("pos52w_warn", 0.92))
                        or (rv is not None and rv > ohc.get("rsi_warn", 70)))

        def _oh_fields(sym):
            return {"dist200": dist_map.get(sym), "pos52": pos_map.get(sym),
                    "rsi": rsi_map.get(sym), "overheat": _hot(sym)}

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
                    if regime == "growth":
                        final -= gp * max(0.0, dist_z.get(sym, 0))  # 과열 강도 비례 감점(Take-Profit)
                    rows.append({
                        "symbol": sym, "name": cmap[sym]["name"], "gics": cmap[sym]["gics"],
                        "related_etf": gics_to_etf.get(cmap[sym]["gics"], ""),
                        "final": round(final, 1), "total": round(total, 1),
                        "value": round(float(r.get("value_score", 0)), 1),
                        "momentum": round(float(r.get("momentum_score", 0)), 1),
                        "dividend": round(float(r.get("dividend_score", 0)), 1),
                        "mcap": r.get("market_cap"), "per": _r1(r.get("trailing_per")),
                        "roe": _pct(r.get("roe")), "divy": _pct(r.get("div_yield")),
                        "mom6m": mom_map.get(sym), **_oh_fields(sym),
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
                if regime == "growth":
                    final -= 0.8 * max(0.0, dist_z.get(sym, 0))  # 과열 강도 비례 감점(Take-Profit 실효화)
                rows.append({
                    "symbol": sym, "name": cmap[sym]["name"], "gics": cmap[sym]["gics"],
                    "related_etf": gics_to_etf.get(cmap[sym]["gics"], ""),
                    "final": round(final, 2), "mom6m": mom_map.get(sym),
                    "vol": vol_map.get(sym), "mcap": caps.get(sym), **_oh_fields(sym),
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
        "stock_sectors": chosen_etfs,
        "equity_pct": equity_pct,
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
        items = []
        for i, r in enumerate(res["index"]):
            trend = "200DMA↑" if r.get("above_ma200") else "200DMA↓"
            items.append(Recommendation(
                obs_date=today, regime=res["regime"], layer="index", symbol=r["symbol"], rank=i,
                rationale=f"{r['name']} · {r['asset']} · 목표 {r['weight']}% · 6M {r.get('mom6m')}% · {trend}"))
        for i, s in enumerate(res["sectors"]):
            items.append(Recommendation(obs_date=today, regime=res["regime"], layer="sector",
                                        symbol=s["symbol"], rank=i + 1,
                                        rationale=f"{s['name']} 6M모멘텀 {s['mom6m']}%"))
        for i, s in enumerate(res["stocks"]):
            cap = _fmt_cap(s.get("mcap"))
            hot = " ⚠과열" if s.get("overheat") else ""
            oh = f"이격 {s.get('dist200')}% · RSI {s.get('rsi')}"
            if "total" in s:
                detail = (f"종합 {s['final']} (V{s['value']}/M{s['momentum']}/D{s['dividend']})"
                          f" · 시총 {cap} · ROE {s.get('roe')}% · 6M {s.get('mom6m')}% · {oh}{hot}")
            else:
                detail = f"점수 {s['final']} · 6M {s.get('mom6m')}% 변동성 {s.get('vol')}% · 시총 {cap} · {oh}{hot}"
            items.append(Recommendation(obs_date=today, regime=res["regime"], layer="ticker",
                                        symbol=s["symbol"], rank=i + 1,
                                        rationale=f"{s['name']} [{s['related_etf']}] {detail}"))
        session.add_all(items)
        session.commit()
