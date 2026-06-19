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

RISK = {"recovery": 1.0, "growth": 1.0, "slowdown": 0.6, "recession": 0.25, "transition": 0.5}
COST = 0.001
CASH_MRET = 0.0
TOPN = 4


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


# (key, label, select_fn, filter_mode): filter_mode='all' 항상필터 / 'defensive' 방어국면만
VARIANTS = [
    ("regime_tilt", "국면틸트(QQQ공격+선택필터) ★수익방어균형", sel_regime_tilt, "defensive"),
    ("spy_timed", "SPY 국면타이밍(순수방어)", sel_spy, "all"),
    ("current", "섹터 favored 상위4 등가", sel_current, "all"),
    ("top2", "섹터 상위2 집중", sel_top2, "all"),
    ("momwt", "섹터 모멘텀가중 상위4", sel_momwt, "all"),
    ("relstr", "섹터 상대강도(SPY초과)", sel_relstr, "all"),
    ("allmom", "섹터 전체모멘텀 상위4(국면무시)", sel_allmom, "all"),
]


def _run(idx, rets, mom6, spym, regime_s, spy_off, pb, select_fn, filter_mode="all"):
    eq, prev_w = 1.0, {}
    curve, turns, expo = [], [], []
    for t in idx:
        loc = rets.index.get_loc(t)
        if loc < 11:
            continue
        prev = rets.index[loc - 1]
        reg = regime_s.get(prev)
        beta = RISK.get(reg, 0.5) if isinstance(reg, str) else 0.5
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


def _stats(curve, label, key):
    s = pd.Series(dict(curve))
    r = s.pct_change().dropna()
    n = len(s)
    return {
        "key": key, "label": label,
        "total_return": round(s.iloc[-1] - 1, 3),
        "cagr": round(s.iloc[-1] ** (12 / n) - 1, 3),
        "mdd": round((s / s.cummax() - 1).min(), 3),
        "sharpe": round(r.mean() / r.std() * np.sqrt(12), 2) if r.std() else 0,
        "months": n,
    }


def backtest_strategy(start="2006-01-01") -> dict:
    uni = load_universe()
    pb = uni["regime_playbook"]
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

    # 벤치마크: SPY 단순보유(필터·베타 없음)
    bench = []
    beq = 1.0
    for t in idx:
        if rets.index.get_loc(t) < 11:
            continue
        beq *= (1 + (rets.at[t, "SPY"] if pd.notna(rets.at[t, "SPY"]) else 0))
        bench.append((t, beq))
    benchmark = _stats(bench, "SPY 단순보유", "benchmark")

    variants, curves = [], {"dates": [t.date().isoformat() for t, _ in bench], "benchmark": [round(v, 3) for _, v in bench]}
    for key, label, fn, fmode in VARIANTS:
        curve, turns, expo = _run(idx, rets, mom6, spym, regime_s, spy_off, pb, fn, fmode)
        st = _stats(curve, label, key)
        st["ann_turnover"] = round(np.mean(turns) * 12, 2)
        st["avg_exposure"] = round(float(np.mean(expo)), 2)
        st["excess_cagr"] = round(st["cagr"] - benchmark["cagr"], 3)
        variants.append(st)
        curves[key] = [round(v, 3) for _, v in curve]

    best = max(variants, key=lambda v: v["sharpe"])
    return {
        "period": f"{bench[0][0].date()} ~ {bench[-1][0].date()}",
        "benchmark": benchmark, "variants": variants, "best": best["key"],
        "params": {"cost_oneway": COST, "trend_filter": "SPY 10M MA", "risk_beta": RISK},
        "curves": curves,
    }
