"""S&P500 구성종목 + GICS 섹터 — 위키피디아(키 불필요)."""

import io

import pandas as pd
import requests

_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_constituents() -> list[dict]:
    r = requests.get(_URL, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_html(io.StringIO(r.text))[0]
    out = []
    for _, row in df.iterrows():
        out.append({
            "symbol": str(row["Symbol"]).replace(".", "-").strip(),  # BRK.B -> BRK-B (yahoo)
            "name": str(row["Security"]).strip(),
            "gics_sector": str(row["GICS Sector"]).strip(),
            "sub_industry": str(row.get("GICS Sub-Industry", "")).strip(),
        })
    return out
