"""수집 오케스트레이션: 설정 → 소스 어댑터 → DB(full refresh)."""

from datetime import date

from sqlalchemy import delete

from .config import PRICE_START_DATE, ROOT_DIR, load_indicators, load_universe
from .db import SessionLocal, init_db
from .models import IndicatorMeta, MacroSeries, MarketPrice, SP500Constituent, TEHeadline
from .sources import fred, sp500, tradingeconomics as te, yahoo

CHART_DIR = ROOT_DIR / "te_charts"


def _sync_indicator_meta(session, indicators: list[dict]) -> None:
    for ind in indicators:
        key = ind.get("series_id") or ind.get("symbol") or ""
        row = session.get(IndicatorMeta, ind["id"])
        if row is None:
            row = IndicatorMeta(id=ind["id"])
            session.add(row)
        row.name = ind["name"]
        row.axis = ind["axis"]
        row.source = ind["source"]
        row.series_key = key
        row.freq = ind["freq"]
        row.is_core = ind.get("core", False)
        row.weight = ind.get("weight", 1.0)
        row.invert = ind.get("invert", False)
        row.transform = ind.get("transform", "")
    session.commit()


def _refresh_series(session, indicator_id: str, source: str, points: list[tuple[date, float]]) -> int:
    session.execute(delete(MacroSeries).where(MacroSeries.series_id == indicator_id))
    session.add_all(
        [MacroSeries(series_id=indicator_id, source=source, obs_date=d, value=v) for d, v in points]
    )
    session.commit()
    return len(points)


def _refresh_prices(session, symbol: str, points: list[tuple[date, float]]) -> int:
    session.execute(delete(MarketPrice).where(MarketPrice.symbol == symbol))
    session.add_all([MarketPrice(symbol=symbol, obs_date=d, close=c) for d, c in points])
    session.commit()
    return len(points)


def collect_macro(session, cfg: dict) -> None:
    start = cfg.get("start_date", "2000-01-01")
    print("\n[1/4] 매크로 시계열 (과거: FRED / 주가: yahoo)")
    for ind in cfg["indicators"]:
        try:
            if ind["source"] == "fred":
                pts = fred.fetch(ind["series_id"], start)
            elif ind["source"] == "yahoo":
                pts = yahoo.fetch(ind["symbol"], start)
            else:
                print(f"  - {ind['id']:14} SKIP (source={ind['source']})")
                continue
            n = _refresh_series(session, ind["id"], ind["source"], pts)
            last = pts[-1] if pts else ("-", "-")
            print(f"  - {ind['id']:14} {ind['source']:6} {n:5}건  last={last[0]} {last[1]}")
        except Exception as e:
            print(f"  - {ind['id']:14} ERROR {type(e).__name__}: {e}")


def collect_te_overlays(session, cfg: dict) -> None:
    print("\n[2/4] TE 오버레이 (현재값+예측치+차트PNG)  ← 사용자 지정 소스")
    for ov in cfg.get("te_overlays", []):
        try:
            head = te.fetch_headline(ov["url"])
            png = te.fetch_chart_png(ov["url"], ov.get("te_symbol", ""), CHART_DIR)
            session.execute(
                delete(TEHeadline).where(
                    TEHeadline.indicator_id == ov["id"],
                    TEHeadline.captured_date == te.today(),
                )
            )
            session.add(
                TEHeadline(
                    indicator_id=ov["id"],
                    name=ov["name"],
                    latest_value=head["latest_value"],
                    latest_period=head["latest_period"],
                    forecast=head["forecast"],
                    source_url=ov["url"],
                    captured_date=te.today(),
                )
            )
            session.commit()
            print(
                f"  - {ov['id']:10} 현재={head['latest_value']} ({head['latest_period']}) "
                f"예측={head['forecast']} chart={'OK' if png else '-'}"
            )
        except Exception as e:
            print(f"  - {ov['id']:10} ERROR {type(e).__name__}: {e}")


def collect_market(session) -> None:
    uni = load_universe()
    symbols = list(uni["index_etfs"]) + list(uni["sector_etfs"])
    print(f"\n[3/4] 시장 데이터 (지수/섹터 ETF {len(symbols)}개)")
    for sym in symbols:
        try:
            pts = yahoo.fetch(sym, PRICE_START_DATE)
            n = _refresh_prices(session, sym, pts)
            print(f"  - {sym:6} {n:5}건")
        except Exception as e:
            print(f"  - {sym:6} ERROR {type(e).__name__}: {e}")


def collect_te_charts(cfg: dict) -> None:
    pages = cfg.get("te_chart_pages", {})
    print(f"\n[+] TE 원본 차트 (전 지표 {len(pages)}개)")
    for iid, url in pages.items():
        path = te.fetch_chart_from_page(url, CHART_DIR, iid)
        print(f"  - {iid:14} {'OK' if path else '실패'}")


def collect_sp500(session) -> None:
    print("\n[4/4] S&P500 구성종목 + GICS 섹터 (위키)")
    try:
        rows = sp500.fetch_constituents()
        session.execute(delete(SP500Constituent))
        session.add_all([
            SP500Constituent(symbol=r["symbol"], name=r["name"],
                             gics_sector=r["gics_sector"], sub_industry=r["sub_industry"])
            for r in rows
        ])
        session.commit()
        print(f"  - {len(rows)}종목 적재")
    except Exception as e:
        print(f"  - ERROR {type(e).__name__}: {e}")


def run_all() -> None:
    init_db()
    cfg = load_indicators()
    with SessionLocal() as session:
        _sync_indicator_meta(session, cfg["indicators"])
        collect_macro(session, cfg)
        collect_te_overlays(session, cfg)
        collect_te_charts(cfg)
        collect_market(session)
        collect_sp500(session)
    print(f"\n완료. TE 차트: {CHART_DIR}")
