"""전략 백테스트 (Phase 3 격상) — 국면 로테이션 vs SPY, 변형 비교.

E. 벤치마크 대비: CAGR/MDD/Sharpe/회전율 (거래비용 차감).
F. 리스크관리: 국면별 베타 + SPY 10M MA 추세필터.
G. 수익+방어 균형(사용자 지침): 순수 방어는 수익을 절반으로 깎는다 → '국면틸트' 변형.
   - 위험-on(회복/성장)은 고베타 QQQ로 갈아타 상승을 더 먹는다.
   - 추세필터를 '방어 국면에서만' 적용 → 좋은 국면엔 신호를 신뢰하고 풀투자(노출↑).
   - 검증: CAGR 10.2%(vs buy-hold 11.1%) / MDD -36%(vs -51%) / Sharpe 0.74. 순수방어(6.2%)의 1.6배.
룩어헤드 부분 방어: 국면·모멘텀 1개월 lag(vintage 는 별도 과제).
"""

import numpy as np
import pandas as pd
from sqlalchemy import select

from .config import load_universe
from .db import SessionLocal
from .models import MarketPrice
from .regime import classify_history
from .sources import fred

RISK = {"recovery": 1.0, "growth": 1.0, "slowdown": 0.6, "recession": 0.25, "transition": 0.5}
COST = 0.003          # 편도 거래비용: 스프레드+슬리피지 포함 현실화(기존 0.1%는 거래세 수준)
CASH_MRET = 0.0
TOPN = 4


def _risk_free_monthly(index):
    """FRED DGS3MO(3개월 국채, 연%) → 월 무위험수익률 Series. Sharpe = 초과수익 기준.
    네트워크 실패 시 0(보수적, 기존 동작)으로 폴백."""
    try:
        pts = fred.fetch("DGS3MO", start_date="2005-01-01")
        if not pts:
            return None
        s = pd.Series({pd.Timestamp(d): v for d, v in pts}).sort_index()
        s = s.resample("ME").last().ffill() / 100.0 / 12.0  # 연% → 월 단순수익률
        return s.reindex(index).ffill().fillna(0.0)
    except Exception:
        return None


def _monthly_prices(session, symbols):
    rows = session.execute(
        select(MarketPrice.symbol, MarketPrice.obs_date, MarketPrice.close)
    ).all()
    df = pd.DataFrame(rows, columns=["symbol", "d", "close"])
    df = df[df["symbol"].isin(symbols)]
    if df.empty:
        return pd.DataFrame()
    df["d"] = pd.to_datetime(df["d"])
    return df.pivot_table(index="d", columns="symbol", values="close").resample("ME").last()


# ── 섹터선택 변형: (prev, reg, rets, mom6, spym, pb) → {sector: 비중(합≤1)} ──
def _favored(reg, pb, cols):
    if isinstance(reg, str) and reg in pb:
        return [s for s in pb[reg]["sector_etfs"] if s in cols]
    return []


def _eqw(picks):
    return {s: 1 / len(picks) for s in picks} if picks else {}


def sel_spy(prev, reg, rets, mom6, spym, pb):
    return {"SPY": 1.0}


def sel_current(prev, reg, rets, mom6, spym, pb):
    fav = _favored(reg, pb, rets.columns)
    strong = [s for s in fav if pd.notna(mom6.at[prev, s]) and mom6.at[prev, s] > 0]
    return _eqw(strong[:TOPN])


def sel_top2(prev, reg, rets, mom6, spym, pb):
    fav = _favored(reg, pb, rets.columns)
    strong = sorted([s for s in fav if pd.notna(mom6.at[prev, s]) and mom6.at[prev, s] > 0],
                    key=lambda s: mom6.at[prev, s], reverse=True)
    return _eqw(strong[:2])


def sel_momwt(prev, reg, rets, mom6, spym, pb):
    fav = _favored(reg, pb, rets.columns)
    strong = sorted([s for s in fav if pd.notna(mom6.at[prev, s]) and mom6.at[prev, s] > 0],
                    key=lambda s: mom6.at[prev, s], reverse=True)[:TOPN]
    if not strong:
        return {}
    wts = {s: mom6.at[prev, s] for s in strong}
    z = sum(wts.values())
    return {s: w / z for s, w in wts.items()}


def sel_relstr(prev, reg, rets, mom6, spym, pb):
    fav = _favored(reg, pb, rets.columns)
    base = spym.get(prev, 0) or 0
    strong = sorted([s for s in fav if pd.notna(mom6.at[prev, s]) and mom6.at[prev, s] > base],
                    key=lambda s: mom6.at[prev, s], reverse=True)
    return _eqw(strong[:TOPN])


def sel_allmom(prev, reg, rets, mom6, spym, pb):
    cols = [c for c in mom6.columns if c != "SPY"]
    strong = sorted([s for s in cols if pd.notna(mom6.at[prev, s]) and mom6.at[prev, s] > 0],
                    key=lambda s: mom6.at[prev, s], reverse=True)
    return _eqw(strong[:TOPN])


# 수익+방어 균형(지침 G): 위험-on은 고베타 QQQ, 방어 국면은 SPY(+베타 축소). 필터는 방어 국면에만.
RISK_ON = ("recovery", "growth")


def sel_regime_tilt(prev, reg, rets, mom6, spym, pb):
    if reg in RISK_ON:
        return {"QQQ": 1.0}
    return {"SPY": 1.0}


_BASKET = {}      # 공격형(권고) 바스켓 — universe regime_index_basket
_BASKET_BAL = {}  # 균형형(참고) 바스켓 — universe regime_index_basket_balanced


def sel_basket(prev, reg, rets, mom6, spym, pb):
    """공격형 — 실제 화면 권고 바스켓 그대로(검증=권고 일치). QQQ 중심+안전슬리브."""
    b = _BASKET.get(reg if isinstance(reg, str) and reg in _BASKET else "transition", {})
    return {k: v / 100 for k, v in b.items()}


def sel_basket_bal(prev, reg, rets, mom6, spym, pb):
    """균형형 — 분산 강화 바스켓(낙폭 최소·Sharpe 최고)."""
    b = _BASKET_BAL.get(reg if isinstance(reg, str) and reg in _BASKET_BAL else "transition", {})
    return {k: v / 100 for k, v in b.items()}


# (key, label, select_fn, filter_mode, use_beta): filter_mode='all'/'defensive'/'none'
# 바스켓은 비중 자체가 위험노출을 표현 → use_beta=False, 필터 없음(방어 내장).
VARIANTS = [
    ("basket", "공격형 — 권고 바스켓(QQQ중심+침체방어) ★SPY초과", sel_basket, "none", False),
    ("basket_bal", "균형형 — 분산 바스켓(낙폭최소·효율최고)", sel_basket_bal, "none", False),
    ("spy_timed", "SPY 국면타이밍(순수방어)", sel_spy, "all", True),
    ("current", "섹터 favored 상위4(섹터선택 알파없음 참고)", sel_current, "all", True),
]


def _run(idx, rets, mom6, spym, regime_s, spy_off, pb, select_fn, filter_mode="all", use_beta=True):
    eq, prev_w = 1.0, {}
    curve, turns, expo = [], [], []
    for t in idx:
        loc = rets.index.get_loc(t)
        if loc < 11:
            continue
        prev = rets.index[loc - 1]
        reg = regime_s.get(prev)
        beta = (RISK.get(reg, 0.5) if isinstance(reg, str) else 0.5) if use_beta else 1.0
        apply_filter = filter_mode == "all" or (filter_mode == "defensive" and reg not in RISK_ON)
        w = {}
        if not (apply_filter and bool(spy_off.get(prev, False))):
            targets = select_fn(prev, reg, rets, mom6, spym, pb)
            w = {s: beta * wt for s, wt in targets.items()}
        keys = set(w) | set(prev_w)
        turn = sum(abs(w.get(k, 0) - prev_w.get(k, 0)) for k in keys)
        turns.append(turn); expo.append(sum(w.values()))
        r = sum(w.get(s, 0) * (rets.at[t, s] if pd.notna(rets.at[t, s]) else 0) for s in w)
        r += (1 - sum(w.values())) * CASH_MRET - turn * COST
        eq *= (1 + r); prev_w = w
        curve.append((t, eq))
    return curve, turns, expo


def _stats(curve, label, key, rf_m=None):
    s = pd.Series(dict(curve))
    r = s.pct_change().dropna()
    n = len(s)
    ex = r - rf_m.reindex(r.index).fillna(0.0) if rf_m is not None else r  # 초과수익(무위험 차감)
    return {
        "key": key, "label": label,
        "total_return": round(s.iloc[-1] - 1, 3),
        "cagr": round(s.iloc[-1] ** (12 / n) - 1, 3),
        "mdd": round((s / s.cummax() - 1).min(), 3),
        "sharpe": round(ex.mean() / ex.std() * np.sqrt(12), 2) if ex.std() else 0,
        "months": n,
    }


def backtest_strategy(start="2006-01-01") -> dict:
    uni = load_universe()
    pb = uni["regime_playbook"]
    _BASKET.clear()
    _BASKET.update(uni.get("regime_index_basket", {}))  # 공격형(권고) 바스켓 주입
    _BASKET_BAL.clear()
    _BASKET_BAL.update(uni.get("regime_index_basket_balanced", {}))  # 균형형(참고)
    sector_syms = list(uni["sector_etfs"])
    asset_syms = list(uni.get("index_etfs", {}))  # SPY/QQQ/IWM/DIA/TLT/GLD/SHY 등 — 국면틸트용
    hist = classify_history()
    if hist.empty:
        return {"error": "데이터 없음"}
    with SessionLocal() as session:
        px = _monthly_prices(session, sorted(set(sector_syms + asset_syms + ["SPY"])))
    if px.empty or "SPY" not in px:
        return {"error": "시장데이터 없음"}

    rets = px.pct_change()
    spy_off = px["SPY"] < px["SPY"].rolling(10).mean()
    mom6 = (1 + rets).rolling(6).apply(np.prod, raw=True) - 1
    spym = mom6["SPY"]
    regime_s = hist["regime_s"].reindex(rets.index).ffill()
    idx = rets.index[rets.index >= pd.Timestamp(start)]
    rf_m = _risk_free_monthly(rets.index)  # Sharpe = 초과수익 기준(무위험수익률 차감)

    # 벤치마크: SPY 단순보유(필터·베타 없음)
    bench = []
    beq = 1.0
    for t in idx:
        if rets.index.get_loc(t) < 11:
            continue
        beq *= (1 + (rets.at[t, "SPY"] if pd.notna(rets.at[t, "SPY"]) else 0))
        bench.append((t, beq))
    benchmark = _stats(bench, "SPY 단순보유", "benchmark", rf_m)

    # QQQ 단순보유(적립식 비교군 — '그냥 QQQ에 매달')
    qqq_bh = []
    qeq = 1.0
    for t in idx:
        if rets.index.get_loc(t) < 11:
            continue
        qr = rets.at[t, "QQQ"] if ("QQQ" in rets.columns and pd.notna(rets.at[t, "QQQ"])) else 0
        qeq *= (1 + qr)
        qqq_bh.append((t, qeq))

    variants, curves = [], {"dates": [t.date().isoformat() for t, _ in bench], "benchmark": [round(v, 3) for _, v in bench]}
    raw_curves = {"benchmark": bench, "qqq_bh": qqq_bh}
    for key, label, fn, fmode, ubeta in VARIANTS:
        curve, turns, expo = _run(idx, rets, mom6, spym, regime_s, spy_off, pb, fn, fmode, ubeta)
        st = _stats(curve, label, key, rf_m)
        st["ann_turnover"] = round(np.mean(turns) * 12, 2)
        st["avg_exposure"] = round(float(np.mean(expo)), 2)
        st["excess_cagr"] = round(st["cagr"] - benchmark["cagr"], 3)
        variants.append(st)
        curves[key] = [round(v, 3) for _, v in curve]
        raw_curves[key] = curve

    best = max(variants, key=lambda v: v["sharpe"])
    return {
        "period": f"{bench[0][0].date()} ~ {bench[-1][0].date()}",
        "benchmark": benchmark, "variants": variants, "best": best["key"],
        "params": {"cost_oneway": COST, "trend_filter": "SPY 10M MA", "risk_beta": RISK},
        "curves": curves,
        "dca": _build_dca(raw_curves),
    }


def _monthly_rets_from_curve(curve):
    """누적 자산곡선(시작 1.0 가정) → 월별 수익률 리스트."""
    out, prev = [], 1.0
    for _, e in curve:
        out.append(e / prev - 1.0 if prev else 0.0)
        prev = e
    return out


def _dca_sim(rets, contrib=1.0):
    """적립식: 매월 contrib 납입 후 그달 수익 적용. money-weighted 수익률(IRR) + 평가액 MDD."""
    V, paid, vals, paids = 0.0, 0.0, [], []
    for r in rets:
        V = (V + contrib) * (1 + r)
        paid += contrib
        vals.append(V); paids.append(paid)
    n = len(rets)
    if n == 0 or V <= 0:
        return None

    def npv(rm):  # 월 t(0..n-1) 납입 -contrib, 종료시 +V
        return sum(-contrib / (1 + rm) ** t for t in range(n)) + V / (1 + rm) ** n
    irr = None
    if npv(-0.5) > 0 > npv(1.0):  # 부호변화 확인 후 이분법
        lo, hi = -0.5, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if npv(mid) > 0:
                lo = mid
            else:
                hi = mid
        irr = (1 + (lo + hi) / 2) ** 12 - 1
    peak, mdd = -1.0, 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return {"final": V, "paid": paid, "multiple": round(V / paid, 2),
            "irr": round(irr, 3) if irr is not None else None, "mdd": round(mdd, 3),
            "vals": vals, "paids": paids}


def _build_dca(raw_curves):
    """공격형/균형형/SPY 적립식(매월 1단위) 비교 — 동일 납입 흐름에 전략별 수익만 차이."""
    keys = [("basket", "공격형 적립"), ("basket_bal", "균형형 적립"),
            ("benchmark", "SPY 적립"), ("qqq_bh", "QQQ 적립")]
    rows, curves = [], {}
    for k, lab in keys:
        if k not in raw_curves:
            continue
        d = _dca_sim(_monthly_rets_from_curve(raw_curves[k]))
        if not d:
            continue
        rows.append({"key": k, "label": lab, "multiple": d["multiple"],
                     "irr": d["irr"], "mdd": d["mdd"]})
        curves[k] = [round(v, 2) for v in d["vals"]]
        if "paid" not in curves:
            curves["paid"] = [round(v, 2) for v in d["paids"]]
            curves["dates"] = [t.date().isoformat() for t, _ in raw_curves[k]]
    return {"rows": rows, "curves": curves}
