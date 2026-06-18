"""trading_america 종목 스코어링 엔진 연동 어댑터 (참조).

분업: america_trend_sensing 이 매크로 4국면 + 유리 섹터를 결정 → 그 국면의
전략 가중치(value/momentum/dividend)로 trading_america 의 펀더멘털 3팩터 스코어링
(score_all + select_top: 사전필터·섹터분산 포함)을 호출해 개별종목을 선별한다.

- TA_DIR: trading_america 경로(기본 ../../trading_america, 환경변수 TA_DIR 로 override).
- live(as_of=None): TA data_collector.fetch_ticker 로 현재 펀더멘털 수집(느림, 종목당 ~0.5s).
- backtest(as_of="YYYY-MM-DD"): TA backtest.features.build_feature_row 로 PIT 수집(historical.db).
- TA 미존재/임포트 실패 시 None 반환 → recommend.py 가 모멘텀 폴백 사용.
"""

import os
import sys
from pathlib import Path

import pandas as pd

from .config import load_universe

TA_DIR = Path(os.environ.get("TA_DIR", str(Path(__file__).resolve().parents[2] / "trading_america")))


def available() -> bool:
    return (TA_DIR / "analyzer.py").exists() and (TA_DIR / "backtest" / "scoring.py").exists()


def _ensure_path():
    p = str(TA_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def regime_weights(regime: str) -> dict:
    m = load_universe().get("regime_to_strategy_weights", {})
    return m.get(regime, {"value": 0.34, "momentum": 0.33, "dividend": 0.33})


def pick(regime: str, tickers: list[str], top_n: int = 15, as_of: str | None = None) -> pd.DataFrame | None:
    """국면 가중치로 TA 스코어링 → TOP N. 실패 시 None."""
    if not available() or not tickers:
        return None
    cwd = os.getcwd()
    try:
        _ensure_path()
        os.chdir(TA_DIR)  # TA 가 상대경로(cache/historical/...) 사용 → CWD 고정 필요
        import logging
        logging.disable(logging.INFO)
        from backtest.scoring import score_all  # noqa: E402
        if as_of:
            from backtest.features import build_feature_row  # noqa: E402
            rows = [build_feature_row(t, as_of) for t in tickers]
        else:
            from data_collector import fetch_ticker  # noqa: E402
            rows = [fetch_ticker(t) for t in tickers]
        rows = [r for r in rows if r]
        if not rows:
            return None
        df = pd.DataFrame(rows)
        weights = regime_weights(regime)
        scored = score_all(df, skip_pre_filter=False, strategy_weights=weights, keep_all=True)
        cols = [c for c in ["ticker", "sector", "total_score", "value_score",
                            "momentum_score", "dividend_score",
                            "market_cap", "trailing_per", "roe", "div_yield", "altman_z"]
                if c in scored.columns]
        return scored[cols].reset_index(drop=True)  # 전체 반환(상위 선별은 호출측에서 사이즈틸트 후)
    except Exception:
        return None
    finally:
        os.chdir(cwd)
