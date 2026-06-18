"""FRED 과거 시계열 — 키 없는 fredgraph.csv 엔드포인트.

TE가 표시하는 미국 매크로(실업률=BLS 등)와 동일 원본을 안정적으로 제공.
하이브리드 전략의 '과거 시계열' 담당(현재값/예측은 TE 오버레이).
"""

from datetime import date, datetime

import requests

_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_TIMEOUT = 30


def fetch(series_id: str, start_date: str = "2000-01-01") -> list[tuple[date, float]]:
    """(obs_date, value) 리스트. 결측치('.')는 건너뜀."""
    r = requests.get(_BASE, params={"id": series_id, "cosd": start_date}, timeout=_TIMEOUT)
    r.raise_for_status()
    out: list[tuple[date, float]] = []
    lines = r.text.strip().splitlines()
    for line in lines[1:]:  # 헤더 스킵
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d_str, v_str = parts[0].strip(), parts[1].strip()
        if not v_str or v_str == ".":
            continue
        try:
            out.append((datetime.strptime(d_str, "%Y-%m-%d").date(), float(v_str)))
        except ValueError:
            continue
    return out
