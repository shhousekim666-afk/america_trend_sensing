"""국면 엔진 핵심 로직 단위 테스트(C3) — 파라미터/지표 변경 시 회귀 탐지.

실행: cd pipeline && ./.venv/bin/python -m pytest -q
외부 소스(FRED/yfinance/DB) 없이 순수 함수만 검증.
"""
import pandas as pd

from ats.regime import ARCHETYPES, _contribution, _direction, _match, _smooth


# ── _match: DI 벡터 → 국면 거리매칭 + softmax ──
def test_match_archetype_corners():
    # 각 국면 원형 벡터를 넣으면 그 국면이 최상위로 나와야 한다
    for regime, vec in ARCHETYPES.items():
        out, conf, dists, probs = _match(*vec)
        assert out == regime, f"{vec} → {out} (기대 {regime})"
        assert 0.0 <= conf <= 1.0
        assert abs(sum(probs.values()) - 1.0) < 1e-9  # 확률 합 = 1
        assert dists[regime] == min(dists.values())   # 자기 원형이 최단거리


def test_match_growth_strong():
    # 모든 축이 강하게 +1 → 성장, 신뢰도 높음
    out, conf, _, _ = _match(1.0, 1.0, 1.0)
    assert out == "growth"
    assert conf > 0.5


def test_match_ambiguous_is_transition():
    # 원점(모든 축 0) → 어느 원형과도 등거리 → 전환(중립)
    out, _, _, _ = _match(0.0, 0.0, 0.0)
    assert out == "transition"


# ── _smooth: 비대칭 지속성 평활 ──
def test_smooth_requires_persistence():
    # 단발 성장은 확정 안 됨(n=3 필요), 3연속이면 확정
    seq = ["recovery", "recovery", "recovery", "growth", "recovery", "recovery"]
    out = _smooth(seq, n=3, n_fast=2)
    assert out[3] == "recovery"   # 단발 growth 무시
    assert out[-1] == "recovery"


def test_smooth_recession_is_faster():
    # 침체는 n_fast=2 로 더 빨리 확정(방어 recall)
    seq = ["growth", "growth", "growth", "recession", "recession", "growth"]
    out = _smooth(seq, n=3, n_fast=2, fast=("recession",))
    assert out[4] == "recession"  # 2연속 만에 침체 확정


def test_smooth_nan_carries_forward():
    out = _smooth(["growth", "growth", "growth", float("nan")], n=3)
    assert out[-1] == "growth"    # NaN 은 직전 확정값 유지


# ── _contribution: invert + 레벨 게이트 ──
def test_contribution_invert():
    # 상승 추세 + invert(실업률↑=수축) → 수축 기여(-1)
    s = pd.Series(range(20), dtype=float)
    eff = _contribution(s, {"transform": "", "invert": True})
    assert eff.iloc[-1] == -1.0


def test_contribution_gate_inverted_curve():
    # 금리차가 음수(역전)일 때 상승(+기여)을 0으로 차단
    s = pd.Series([-2.0, -1.5, -1.0, -0.5], dtype=float)  # 상승하지만 전부 음수레벨
    eff = _contribution(s, {"transform": "", "gate_inverted_curve": True})
    assert (eff.dropna() <= 0).all()


def test_direction_sign():
    s = pd.Series([1, 2, 3, 4], dtype=float)
    assert _direction(s, "").iloc[-1] == 1.0
    s2 = pd.Series([4, 3, 2, 1], dtype=float)
    assert _direction(s2, "").iloc[-1] == -1.0
