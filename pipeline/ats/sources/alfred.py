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
                   observation_start: str = "1998-01-01") -> list[dict]:
    """시리즈의 전체 vintage 이력(wide 포맷).

    output_type=2 는 각 관측행에 vintage 별 컬럼을 준다:
      {"date":"2008-11-01", "PAYEMS_20081205":"...", "PAYEMS_20090109":"...", ...}
    컬럼명 끝 8자리(YYYYMMDD)가 발표(vintage)일. asof 이하 최신 vintage 값이 그 시점의 값.
    """
    r = requests.get(_BASE, params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "output_type": 2,  # 모든 vintage(wide)
        "observation_start": observation_start,
        "realtime_start": "1998-01-01", "realtime_end": "9999-12-31",
    }, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json().get("observations", [])


def pit_values(observations: list[dict], asof: str) -> dict[str, float]:
    """asof(YYYY-MM-DD) 시점에 알려져 있던 관측값 {obs_date: value}.

    각 관측행에서 vintage일 ≤ asof 인 컬럼 중 가장 최신 vintage 값을 취한다(그 시점의 값).
    """
    asof_c = asof.replace("-", "")
    out = {}
    for row in observations:
        od = row.get("date")
        best_dt, best_v = "", None
        for k, val in row.items():
            if k == "date" or val in (".", "", None):
                continue
            vd = k.rsplit("_", 1)[-1]  # SERIES_YYYYMMDD → YYYYMMDD
            if len(vd) == 8 and vd.isdigit() and vd <= asof_c and vd > best_dt:
                best_dt, best_v = vd, val
        if best_v is not None:
            try:
                out[od] = float(best_v)
            except ValueError:
                pass
    return out
