"""Trading Economics 페이지 스크래퍼 (사용자 지정 소스).

TE 무료 API 폐지(guest 410), markets 차트 JSON 엔드포인트 차단(DNS) 확인됨.
→ 페이지 HTML 에서 (1) 현재값/기간 (2) 예측치 TEForecast 배열 (3) 한글 요약 메타,
  그리고 (4) TE 차트 PNG 이미지를 가져온다. 과거 시계열은 FRED(동일 원본)가 담당.
"""

import re
from datetime import date
from pathlib import Path

import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 30
_CHART_CDN = "https://d3fy651gv2fhd3.cloudfront.net/charts/{slug}.png"


def _slug_from_url(url: str) -> str:
    # https://ko.tradingeconomics.com/united-states/unemployment-rate -> united-states-unemployment-rate
    path = url.split("tradingeconomics.com/")[-1].strip("/")
    return path.replace("/", "-")


def fetch_headline(url: str) -> dict:
    """현재값/기간/예측치/요약 추출. 실패해도 빈 필드로 반환(비치명적)."""
    out = {"latest_value": None, "latest_period": "", "forecast": "", "meta_desc": ""}
    r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    html = r.text

    m = re.search(r'metaDesc"[^>]*content="([^"]+)"', html)
    if m:
        meta = m.group(1)
        out["meta_desc"] = meta
        # 현재값: 마지막 "숫자%/숫자포인트" (예: 실업률 4.30% / PMI 55.10포인트)
        vals = re.findall(r"(\d+(?:\.\d+)?)\s*(?:%|포인트|points|percent)", meta)
        if vals:
            out["latest_value"] = float(vals[-1])
        per = re.findall(r"(\d{4}년\s*\d+월|\d+월)", meta)
        if per:
            out["latest_period"] = per[-1].replace(" ", "")

    # 예측치: var TEForecast = [..] 중 비어있지 않은 것
    fcs = [x for x in re.findall(r"TEForecast\s*=\s*(\[[^\]]*\])", html) if x != "[]"]
    if fcs:
        out["forecast"] = fcs[0]
    return out


def fetch_chart_png(url: str, te_symbol: str, dest_dir: Path) -> Path | None:
    """TE 차트 이미지를 저장하고 경로 반환. 실패 시 None."""
    slug = _slug_from_url(url)
    chart_url = _CHART_CDN.format(slug=slug)
    params = {"s": te_symbol} if te_symbol else {}
    try:
        r = requests.get(chart_url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        if "image" not in r.headers.get("content-type", ""):
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{slug}.png"
        path.write_bytes(r.content)
        return path
    except requests.RequestException:
        return None


def fetch_chart_from_page(url: str, dest_dir: Path, fname: str, years: int = 10) -> Path | None:
    """TE 페이지 HTML에서 실제 차트 PNG(심볼 포함) URL을 추출해 저장. 실패 시 None.

    slug 단독 URL은 placeholder GIF(824B)만 반환하므로 반드시 페이지에서 ?s=<symbol> 포함 URL을 추출.
    years: 차트 기간(년). TE PNG 는 &d1=<시작일> 로 범위 제어(span/lastn 은 무시됨).
    """
    try:
        html = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT).text
        m = re.search(r"https://[^\"']*cloudfront\.net/charts/[^\"']*\.png\?s=[^\"'&]*", html)
        if not m:
            return None
        t = date.today()
        d1 = date(t.year - years, t.month, 1).isoformat()
        chart_url = f"{m.group(0)}&d1={d1}"  # 기간 정렬(10년)
        r = requests.get(chart_url, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        if "image" not in r.headers.get("content-type", "") or len(r.content) < 2000:
            return None  # placeholder/빈 이미지 거름
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{fname}.png"
        path.write_bytes(r.content)
        return path
    except requests.RequestException:
        return None


def today() -> date:
    return date.today()
