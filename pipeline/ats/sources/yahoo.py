"""야후 파이낸스 — 지수/ETF/종목 일간 종가 (yfinance)."""

from datetime import date

import yfinance as yf


def fetch(symbol: str, start_date: str = "2005-01-01") -> list[tuple[date, float]]:
    """(obs_date, close) 리스트. 실패 시 빈 리스트(비치명적)."""
    df = yf.download(
        symbol, start=start_date, progress=False, auto_adjust=True, threads=False
    )
    if df is None or df.empty:
        return []
    close = df["Close"]
    # yfinance 1.x 는 단일 심볼도 MultiIndex/DataFrame 으로 줄 수 있음 → 1열 squeeze
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    out: list[tuple[date, float]] = []
    for idx, val in close.items():
        d = idx.date() if hasattr(idx, "date") else idx
        if val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        out.append((d, f))
    return out
