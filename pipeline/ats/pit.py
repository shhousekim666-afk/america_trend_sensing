"""PIT(발표시점, point-in-time) 백테스트.

각 결정월 T 에 대해 **그 시점에 실제로 알려져 있던 vintage 값**으로 국면을 재분류한다.
개정 데이터로 만든 현재 백테스트가 얼마나 낙관적인지(look-ahead 편향)를 정량 비교한다.

- 매크로: ALFRED vintage (FRED_API_KEY 필요)
- 주가(sp500): 개정이 없으므로 DB 월말값 그대로 사용
- 국면 분류/평활/검증/백테스트는 기존 엔진을 그대로 재사용(monthly/hist 주입)
"""
import pandas as pd

from .config import load_indicators
from .db import SessionLocal
from .regime import _load_monthly, _smooth, classify_history, evaluate
from .sources import alfred
from .strategy import backtest_strategy

# 시장·비개정 지표: 개정이 없어 vintage 불필요(일간은 vintage 수가 한도 초과) → 현재값=발표시점값.
# 개정되는 건 월간 매크로(고용/GDP/물가/생산/소매 등)뿐 → 이들만 ALFRED vintage 사용.
_MARKET_SERIES = {"t10y2y", "hy_spread", "initial_claims", "sp500"}
_DI_AXES = {"leading", "coincident", "lagging"}


def build_pit_hist(key: str, start: str = "2006-01-01", end: str | None = None) -> pd.DataFrame:
    """발표시점 국면 타임라인(index=결정월말, cols=[L,C,Lag,regime,confidence,regime_s,provisional])."""
    inds = load_indicators()["indicators"]
    fred_inds = [i for i in inds if i.get("source") == "fred" and i.get("series_id")
                 and i.get("axis") in _DI_AXES]  # trigger 등 비-DI 지표 제외
    revised = [i for i in fred_inds if i["id"] not in _MARKET_SERIES]
    with SessionLocal() as session:  # 시장·비개정 지표(+주가)는 DB 현재값 그대로
        db_monthly = _load_monthly(session)
    vint, fallback = {}, set()
    for i in revised:  # 개정 월간 매크로만 vintage 수집(실패 시 DB 현재값 폴백)
        try:
            v = alfred.fetch_vintages(i["series_id"], key)
            if v:
                vint[i["id"]] = v
            else:
                fallback.add(i["id"])
        except Exception:
            fallback.add(i["id"])
    revised = [i for i in revised if i["id"] in vint]

    end_ts = pd.Timestamp(end) if end else db_monthly.get("sp500", pd.Series(dtype=float)).index.max()
    decision_months = pd.date_range(start=start, end=end_ts, freq="ME")

    rows = []
    for T in decision_months:
        asof = T.strftime("%Y-%m-%d")
        monthly = {}
        # 개정 월간: T 시점 vintage 재구성
        for i in revised:
            pv = alfred.pit_values(vint[i["id"]], asof)
            if not pv:
                continue
            s = pd.Series(pv)
            s.index = pd.to_datetime(s.index)
            monthly[i["id"]] = s[s.index <= T].sort_index().resample("ME").last()
        # 시장·비개정(+주가) + vintage 실패 폴백: DB 현재값을 T까지 절단
        for sid in _MARKET_SERIES | fallback:
            if sid in db_monthly:
                monthly[sid] = db_monthly[sid][db_monthly[sid].index <= T]
        df = classify_history(monthly=monthly).dropna(subset=["regime"])
        if df.empty:
            continue
        last = df.iloc[-1]
        rows.append((T, last["L"], last["C"], last["Lag"], last["regime"]))

    if not rows:
        return pd.DataFrame()
    hist = pd.DataFrame([r[1:] for r in rows], columns=["L", "C", "Lag", "regime"],
                        index=pd.DatetimeIndex([r[0] for r in rows]))
    hist["regime_s"] = _smooth(list(hist["regime"]), n=3)  # 인과적 지속성(과거만 사용)
    reg = hist["regime"]
    hist["confidence"] = 0.0
    hist["provisional"] = (reg != reg.shift(1)) | (reg != reg.shift(2))
    return hist


def _bt_summary(bt: dict) -> dict:
    """백테스트 결과에서 전략별 핵심 지표만 추림."""
    if not bt or "error" in bt:
        return {"error": bt.get("error", "?") if bt else "없음"}
    out = {}
    bench = bt.get("benchmark")
    if bench:
        out["SPY_benchmark"] = {"cagr": bench.get("cagr"), "mdd": bench.get("mdd"),
                                "sharpe": bench.get("sharpe")}
    for v in bt.get("variants", []):
        out[v.get("key", v.get("label", "?"))] = {
            "cagr": v.get("cagr"), "mdd": v.get("mdd"), "sharpe": v.get("sharpe")}
    return out


def run_pit(start: str = "2006-01-01") -> dict:
    """PIT vs 개정 데이터 비교(NBER 검증 + 전략 백테스트). 키 없으면 error."""
    key = alfred.api_key()
    if not key:
        return {"error": "FRED_API_KEY 미설정 — ALFRED 발표시점 데이터 접근 불가. "
                         "https://fredaccount.stlouisfed.org 에서 무료 발급 후 환경변수 설정."}
    pit = build_pit_hist(key, start=start)
    if pit.empty:
        return {"error": "PIT 재구성 실패(vintage 데이터 없음)"}
    return {
        "pit_months": int(len(pit)),
        "pit_eval": evaluate(hist=pit),
        "rev_eval": evaluate(),
        "pit_strategy": _bt_summary(backtest_strategy(start=start, hist=pit)),
        "rev_strategy": _bt_summary(backtest_strategy(start=start)),
    }
