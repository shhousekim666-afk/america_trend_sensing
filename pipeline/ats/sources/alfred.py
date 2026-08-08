"""ALFRED(발표시점/vintage) 데이터 소스 — PIT(point-in-time) 백테스트용.

FRED 의 개정 데이터가 아니라 **각 시점에 실제로 알려져 있던 값**을 받는다.
키 없는 fredgraph 는 vintage 파라미터를 무시하므로(현재 개정값만 반환),
ALFRED 정식 API(무료 키 필요)를 사용한다.

키 설정: 환경변수 FRED_API_KEY (https://fredaccount.stlouisfed.org 에서 무료 발급).
키가 없으면 PIT 기능만 비활성화되고 기존 키리스 파이프라인은 그대로 동작한다.
"""
import os

import requests

_BASE = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT = 40


def api_key() -> str | None:
    k = os.environ.get("FRED_API_KEY", "").strip()
    return k or None


def fetch_vintages(series_id: str, key: str,
                   observation_start: str = "1998-01-01") -> list[tuple]:
    """시리즈의 전체 vintage 이력.

    output_type=2(모든 관측 × 각 realtime 구간) → (obs_date, rt_start, rt_end, value) 리스트.
    rt_start ≤ asof ≤ rt_end 인 값이 asof 시점에 알려져 있던 값이다.
    """
    r = requests.get(_BASE, params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "output_type": 2,  # 모든 vintage
        "observation_start": observation_start,
        "realtime_start": "1998-01-01", "realtime_end": "9999-12-31",
    }, timeout=_TIMEOUT)
    r.raise_for_status()
    out = []
    for o in r.json().get("observations", []):
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            out.append((o["date"], o["realtime_start"], o["realtime_end"], float(v)))
        except (ValueError, KeyError):
            continue
    return out


def pit_values(vintages: list[tuple], asof: str) -> dict[str, float]:
    """asof(YYYY-MM-DD) 시점에 알려져 있던 관측값 {obs_date: value}.

    같은 obs_date 에 여러 vintage 가 있으면 asof 를 포함하는 realtime 구간의 값을 취한다.
    """
    out = {}
    for od, rs, re_, val in vintages:
        if rs <= asof <= re_:
            out[od] = val
    return out
