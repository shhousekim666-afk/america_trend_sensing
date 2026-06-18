"""경기 국면 판정 엔진 (Phase 2) + 백테스트(Phase 3).

지침 §2: 선행/동행/후행 3축의 방향(확산지수) → 4국면 거리매칭 + 신뢰도 + 중립(Transition).
원작자 순차 게이트(§1.5.2)는 detail 로 병기. 침체 트리거(§2.3)는 TE Forecast 로 오버레이.
"""

import json
import math

import numpy as np
import pandas as pd
from sqlalchemy import select

from .config import load_indicators
from .db import SessionLocal
from .models import MacroSeries, TEHeadline

AXES = ["leading", "coincident", "lagging"]
KOR = {"recovery": "회복", "growth": "성장", "slowdown": "둔화",
       "recession": "침체", "transition": "전환(중립)"}

# 국면 원형 벡터 (선행, 동행, 후행) ∈ {-1,0,1}  — 지침 §2.2
ARCHETYPES = {
    "recovery":  (1, 0, -1),
    "growth":    (1, 1, 1),
    "slowdown":  (-1, 0, 1),
    "recession": (-1, -1, -1),
}
DI_THRESHOLD = 0.33       # 확산지수 라벨 임계(표시용)
SOFTMAX_BETA = 1.6        # 신뢰도 연속화 softmax 민감도
TRANSITION_MIN_PROB = 0.5  # 최상위 확률 미만이면 중립
TRANSITION_MARGIN = 0.15   # 1·2위 확률차 미만이면 중립


def _match(L, C, Lag):
    """연속 축 DI 벡터 → 4국면 거리 → softmax 확률 → (국면, 신뢰도, 거리, 확률).

    정수라벨 거리매칭은 신뢰도가 양자화돼 Transition 이 거의 안 떴음(에이전트 공통 지적).
    연속 DI 로 유클리드 거리 + softmax 하여 중간 신뢰도/중립이 실제 작동하게 함.
    """
    vec = (L, C, Lag)
    dists = {r: math.dist(vec, arc) for r, arc in ARCHETYPES.items()}
    ex = {r: math.exp(-SOFTMAX_BETA * d * d) for r, d in dists.items()}
    z = sum(ex.values()) or 1.0
    probs = {r: ex[r] / z for r in ex}
    ranked = sorted(probs, key=probs.get, reverse=True)
    top, second = ranked[0], ranked[1]
    if probs[top] < TRANSITION_MIN_PROB or (probs[top] - probs[second]) < TRANSITION_MARGIN:
        regime = "transition"
    else:
        regime = top
    return regime, round(probs[top], 2), dists, probs


def _contribution(s: pd.Series, ind: dict) -> pd.Series:
    """지표 시계열 → 경기 기여 부호(invert + 레벨 게이트 적용). 2024 오신호 방어 포함."""
    raw = _direction(s, ind.get("transform", ""))
    eff = -raw if ind.get("invert") else raw
    if ind.get("gate_inverted_curve"):          # 금리차 역전(level<0) 중 +기여 차단
        eff = eff.where(s >= 0, eff.clip(upper=0))
    low = ind.get("gate_low_unemployment")      # 낮은 실업률의 악화 기여 반감
    if low is not None:
        eff = eff.mask((s < low) & (eff < 0), eff * 0.5)
    return eff


def _load_monthly(session) -> dict[str, pd.Series]:
    """지표별 월말 시계열 로드."""
    rows = session.execute(
        select(MacroSeries.series_id, MacroSeries.obs_date, MacroSeries.value)
    ).all()
    out: dict[str, pd.Series] = {}
    df = pd.DataFrame(rows, columns=["sid", "d", "v"])
    if df.empty:
        return out
    df["d"] = pd.to_datetime(df["d"])
    for sid, g in df.groupby("sid"):
        s = g.set_index("d")["v"].sort_index()
        s = s.resample("ME").last()  # 월말
        out[sid] = s
    return out


def _direction(s: pd.Series, transform: str) -> pd.Series:
    """지표 시계열 → 월별 방향 부호(+1/0/-1)."""
    if transform == "yoy":
        d = s - s.shift(12)
    elif transform == "3m_ma_slope":
        ma = s.rolling(3, min_periods=2).mean()
        d = ma - ma.shift(3)
    elif transform == "12m_ma_slope":
        ma = s.rolling(12, min_periods=6).mean()
        d = ma - ma.shift(3)
    else:  # 기타: 1개월 변화
        d = s.diff()
    return np.sign(d)


def classify_history() -> pd.DataFrame:
    """월별 국면 타임라인. index=월말, cols=[L,C,Lag,regime,confidence,provisional]."""
    cfg = load_indicators()
    inds = cfg["indicators"]
    with SessionLocal() as session:
        monthly = _load_monthly(session)

    # 지표별 경기 기여(invert + 레벨 게이트 적용)
    dir_signed: dict[str, tuple[pd.Series, str, float]] = {}
    for ind in inds:
        sid = ind["id"]
        if sid not in monthly:
            continue
        eff = _contribution(monthly[sid], ind)
        dir_signed[sid] = (eff, ind["axis"], ind.get("weight", 1.0))

    if not dir_signed:
        return pd.DataFrame()

    full_idx = pd.DatetimeIndex(sorted(set().union(*[s.index for s, _, _ in dir_signed.values()])))

    # 축별 확산지수(가중 평균 부호)
    axis_di = {ax: pd.Series(0.0, index=full_idx) for ax in AXES}
    axis_w = {ax: pd.Series(0.0, index=full_idx) for ax in AXES}
    for sid, (sign, ax, w) in dir_signed.items():
        s = sign.reindex(full_idx)
        mask = s.notna()
        axis_di[ax] = axis_di[ax].add((s.fillna(0) * w).where(mask, 0), fill_value=0)
        axis_w[ax] = axis_w[ax].add(pd.Series(np.where(mask, w, 0.0), index=full_idx), fill_value=0)

    di = {}
    for ax in AXES:
        di[ax] = (axis_di[ax] / axis_w[ax].replace(0, np.nan))

    df = pd.DataFrame({"L": di["leading"], "C": di["coincident"], "Lag": di["lagging"]})
    df = df.dropna(how="all")

    regimes, confs = [], []
    for _, row in df.iterrows():
        if row[["L", "C", "Lag"]].isna().any():
            regimes.append(np.nan); confs.append(np.nan); continue
        r, c, _, _ = _match(row["L"], row["C"], row["Lag"])  # 연속 DI + softmax
        regimes.append(r); confs.append(c)

    df["regime"] = regimes
    df["confidence"] = confs
    # 지속성 필터(§2.4): 새 국면이 N개월 연속 유지돼야 확정 전이 → 휘프소 방어
    df["regime_s"] = _smooth(regimes, n=3)
    reg = df["regime"]
    df["provisional"] = (reg != reg.shift(1)) | (reg != reg.shift(2))
    return df.round(2)


def _smooth(regimes: list, n: int = 3, n_fast: int = 2, fast=("recession",)) -> list:
    """비대칭 지속성 평활(§2.4): 침체(방어)는 n_fast 개월에 빠르게 확정, 나머지는 n 개월.

    대칭 n=3 은 짧고 들쭉날쭉한 침체(2001)를 놓침 → 방어 신호만 빠르게 켜 recall 보강.
    """
    out, confirmed, run, prev = [], None, 0, None
    for r in regimes:
        if isinstance(r, float):  # NaN
            out.append(confirmed); continue
        run = run + 1 if r == prev else 1
        prev = r
        need = n_fast if r in fast else n
        if confirmed is None or (run >= need and r != confirmed):
            confirmed = r
        out.append(confirmed)
    return out


def _recession_trigger(session) -> dict:
    """침체 경보 이원화(§2.3 + 투자전문가 권고): 실업률 연속상승 OR HY스프레드 급확대."""
    monthly = _load_monthly(session)
    s = monthly.get("unrate")
    hy = monthly.get("hy_spread")
    out = {"alert": False, "unemp_alert": False, "hy_alert": False, "detail": ""}
    parts = []

    if s is not None and len(s) >= 3:
        te = session.execute(
            select(TEHeadline).where(TEHeadline.indicator_id == "unrate")
            .order_by(TEHeadline.captured_date.desc())
        ).scalars().first()
        last, prev = s.iloc[-1], s.iloc[-3]
        rising = last > prev
        fc0 = None
        if te and te.forecast:
            try:
                fc0 = json.loads(te.forecast)[0]
            except (json.JSONDecodeError, IndexError):
                fc0 = None
        fc_rising = (fc0 is not None and fc0 > last)
        out["unemp_alert"] = bool(rising and fc_rising)
        parts.append(f"실업률 {last:.2f}(2M전 {prev:.2f},{'상승' if rising else '안정'})/"
                     f"Forecast {fc0}({'상승' if fc_rising else '안정'})")

    if hy is not None and len(hy) >= 13:
        hy_last = hy.iloc[-1]
        hy_avg = hy.iloc[-13:-1].mean()  # 직전 12개월 평균
        out["hy_alert"] = bool(hy_last > hy_avg * 1.25)  # 12M평균 25%↑ = 신용경색
        parts.append(f"HY스프레드 {hy_last:.2f}(12M평균 {hy_avg:.2f},"
                     f"{'급확대⚠' if out['hy_alert'] else '안정'})")

    out["alert"] = out["unemp_alert"] or out["hy_alert"]
    out["detail"] = " / ".join(parts)
    return out


def current_regime() -> dict:
    """최신 국면 + 근거 + 침체 트리거. 판정/신뢰도는 explain_current(최신 가용 데이터) 기준으로 통일."""
    ex = explain_current()
    if not ex:
        return {"error": "데이터 없음 — 먼저 수집(collect) 실행"}
    hist = classify_history().dropna(subset=["regime"])
    last = hist.iloc[-1] if not hist.empty else None
    with SessionLocal() as session:
        trig = _recession_trigger(session)
    return {
        "as_of": hist.index[-1].date().isoformat() if last is not None else "",
        "regime": ex["regime"],
        "regime_kr": ex["regime_kr"],
        "confidence": ex["confidence"],
        "provisional": bool(last["provisional"]) if last is not None else True,
        "scores": ex["di"],  # {leading, coincident, lagging}
        "recession_trigger": trig,
    }


def persist_history() -> int:
    """국면 타임라인을 regime_snapshot 테이블에 저장(상위 API/대시보드용)."""
    from sqlalchemy import delete
    from .models import RegimeSnapshot
    hist = classify_history().dropna(subset=["regime"])
    if hist.empty:
        return 0
    with SessionLocal() as session:
        session.execute(delete(RegimeSnapshot))
        for idx, row in hist.iterrows():
            session.add(RegimeSnapshot(
                obs_date=idx.date(),
                leading_score=float(row["L"]), coincident_score=float(row["C"]),
                lagging_score=float(row["Lag"]), regime=row["regime_s"],
                confidence=float(row["confidence"]), is_provisional=bool(row["provisional"]),
                detail=f"raw={row['regime']}",
            ))
        session.commit()
    return len(hist)


def explain_current() -> dict:
    """현재 국면 판정 근거: 지표별 방향 → 축 확산지수 → 4국면 거리매칭."""
    cfg = load_indicators()
    with SessionLocal() as session:
        monthly = _load_monthly(session)
    if not monthly:
        return {}

    axis_num = {a: 0.0 for a in AXES}
    axis_den = {a: 0.0 for a in AXES}
    per = []
    for ind in cfg["indicators"]:
        sid = ind["id"]
        if sid not in monthly:
            continue
        raw = _direction(monthly[sid], ind.get("transform", "")).dropna()  # 값 방향(게이트 전)
        eff_s = _contribution(monthly[sid], ind).dropna()                  # 경기 기여(게이트 후)
        if raw.empty or eff_s.empty:
            continue
        raw_dir = int(raw.iloc[-1])
        eff_val = float(eff_s.iloc[-1])                  # ±1 또는 게이트로 ±0.5/0
        ungated = -raw_dir if ind.get("invert") else raw_dir
        w = ind.get("weight", 1.0)
        ax = ind["axis"]
        axis_num[ax] += eff_val * w
        axis_den[ax] += w
        per.append({
            "id": sid, "name": ind["name"], "axis": ax, "axis_kr": AXIS_KR.get(ax, ax),
            "cur": round(float(monthly[sid].dropna().iloc[-1]), 2),
            "raw_dir": raw_dir, "effect": int(np.sign(eff_val)), "weight": w,
            "gated": eff_val != ungated, "invert": bool(ind.get("invert")),
            "transform": ind.get("transform", ""),
        })

    di = {a: (axis_num[a] / axis_den[a] if axis_den[a] else 0.0) for a in AXES}
    lab = {a: (1 if di[a] > DI_THRESHOLD else -1 if di[a] < -DI_THRESHOLD else 0) for a in AXES}
    regime, conf, dists, probs = _match(di["leading"], di["coincident"], di["lagging"])
    return {
        "per": per, "di": di, "label": lab,
        "vec": (round(di["leading"], 2), round(di["coincident"], 2), round(di["lagging"], 2)),
        "dists": {r: round(d, 2) for r, d in dists.items()},
        "probs": {r: round(p, 2) for r, p in probs.items()},
        "regime": regime, "regime_kr": KOR.get(regime, regime), "confidence": conf,
    }


# axis 한글 (report 와 공유)
AXIS_KR = {"leading": "선행", "coincident": "동행", "lagging": "후행"}


def evaluate() -> dict:
    """NBER 침체(FRED USREC) 정답 대비 정량 검증: precision/recall/F1·시차·휘프소율."""
    from .sources import fred
    hist = classify_history().dropna(subset=["regime"]).copy()
    if hist.empty:
        return {"error": "데이터 없음"}
    usrec = {(d.year, d.month): int(v) for d, v in fred.fetch("USREC", "2000-01-01")}

    tp = fp = fn = tn = 0
    for idx, row in hist.iterrows():
        key = (idx.year, idx.month)
        if key not in usrec:
            continue
        actual = usrec[key] == 1
        pred = row["regime_s"] == "recession"
        tp += actual and pred
        fp += pred and not actual
        fn += actual and not pred
        tn += not actual and not pred
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # 휘프소율: 전월 대비 국면 변경 비율 (raw vs 평활)
    raw_chg = (hist["regime"] != hist["regime"].shift(1)).sum()
    sm_chg = (hist["regime_s"] != hist["regime_s"].shift(1)).sum()
    n = len(hist)

    # NBER 침체 에피소드별 시차(lead-lag): 우리 첫 침체판정 - NBER 개시 (음수=선행)
    months = sorted(usrec)
    episodes, in_rec = [], False
    for (y, m) in months:
        if usrec[(y, m)] == 1 and not in_rec:
            episodes.append((y, m)); in_rec = True
        elif usrec[(y, m)] == 0:
            in_rec = False
    pred_recession = {(i.year, i.month) for i, r in hist["regime_s"].items() if r == "recession"}

    def mnum(ym):
        return ym[0] * 12 + ym[1]
    lags = []
    for ep in episodes:
        cands = [p for p in pred_recession if abs(mnum(p) - mnum(ep)) <= 12]
        if cands:
            first = min(cands, key=mnum)
            lags.append({"nber": f"{ep[0]}-{ep[1]:02d}", "delta_m": mnum(first) - mnum(ep)})
        else:
            lags.append({"nber": f"{ep[0]}-{ep[1]:02d}", "delta_m": None})

    return {
        "precision": round(precision, 2), "recall": round(recall, 2), "f1": round(f1, 2),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "months": n,
        "whipsaw_raw": round(raw_chg / n, 3), "whipsaw_smoothed": round(sm_chg / n, 3),
        "lead_lag": lags,
    }


def backtest_yearly() -> pd.DataFrame:
    """연도별 우세 국면 + 월별 시퀀스."""
    hist = classify_history().dropna(subset=["regime"])
    if hist.empty:
        return pd.DataFrame()
    hist = hist.copy()
    hist["year"] = hist.index.year
    sym = {"recovery": "회", "growth": "성", "slowdown": "둔", "recession": "침", "transition": "·"}
    rows = []
    for yr, g in hist.groupby("year"):
        dominant = g["regime_s"].mode().iloc[0]
        seq_s = "".join(sym.get(r, "?") for r in g["regime_s"])
        seq_raw = "".join(sym.get(r, "?") for r in g["regime"])
        rows.append({"year": yr, "확정국면": KOR.get(dominant, dominant),
                     "월별(확정)": seq_s, "월별(raw)": seq_raw})
    return pd.DataFrame(rows).set_index("year")
