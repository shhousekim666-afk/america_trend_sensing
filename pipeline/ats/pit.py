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


def build_pit_hist(start: str = "2006-01-01", end: str | None = None,
                   key: str | None = None, use_vintage: bool = True) -> pd.DataFrame:
    """결정월별로 '그 시점에 알려진 데이터'만으로 국면을 재구성한 타임라인.

    use_vintage=True  → 개정 월간 매크로는 ALFRED 발표시점(vintage) 값(진짜 PIT).
    use_vintage=False → 모든 지표를 DB 현재(개정)값으로(단 재구성 방식은 동일).
    → 두 모드의 **유일한 차이는 데이터 값(발표시점 vs 개정)** 뿐이므로 순수 look-ahead 편향만 격리.
    index=결정월말, cols=[L,C,Lag,regime,confidence,regime_s,provisional].
    """
    inds = load_indicators()["indicators"]
    fred_inds = [i for i in inds if i.get("source") == "fred" and i.get("series_id")
                 and i.get("axis") in _DI_AXES]  # trigger 등 비-DI 지표 제외
    with SessionLocal() as session:
        db_monthly = _load_monthly(session)
    vint = {}
    if use_vintage:  # 개정되는 월간 매크로만 vintage 수집(시장·비개정 지표는 현재값=발표시점값)
        for i in fred_inds:
            if i["id"] in _MARKET_SERIES:
                continue
            try:
                v = alfred.fetch_vintages(i["series_id"], key)
                if v:
                    vint[i["id"]] = v
            except Exception:
                pass

    end_ts = pd.Timestamp(end) if end else db_monthly.get("sp500", pd.Series(dtype=float)).index.max()
    decision_months = pd.date_range(start=start, end=end_ts, freq="ME")

    rows = []
    for T in decision_months:
        asof = T.strftime("%Y-%m-%d")
        monthly = {}
        for i in fred_inds:
            sid = i["id"]
            if sid in vint:  # 발표시점 vintage 재구성
                pv = alfred.pit_values(vint[sid], asof)
                if pv:
                    s = pd.Series(pv)
                    s.index = pd.to_datetime(s.index)
                    monthly[sid] = s[s.index <= T].sort_index().resample("ME").last()
                    continue
            if sid in db_monthly:  # 시장·비개정 or 현재값 모드: DB 값을 T까지 절단
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
    # 동일한 재구성 방식으로 두 타임라인 생성 — 유일한 차이는 값(발표시점 vs 개정).
    pit = build_pit_hist(start=start, key=key, use_vintage=True)
    rev = build_pit_hist(start=start, use_vintage=False)
    if pit.empty or rev.empty:
        return {"error": "PIT 재구성 실패(vintage 데이터 없음)"}
    return {
        "pit_months": int(len(pit)),
        "rev_months": int(len(rev)),
        "pit_eval": evaluate(hist=pit),
        "rev_eval": evaluate(hist=rev),
        "pit_strategy": _bt_summary(backtest_strategy(start=start, hist=pit)),
        "rev_strategy": _bt_summary(backtest_strategy(start=start, hist=rev)),
    }
