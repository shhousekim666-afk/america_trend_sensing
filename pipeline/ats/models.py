"""DB 스키마 (지침 §3.3). SQLAlchemy 2.0 ORM. SQLite/MariaDB 공용."""

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IndicatorMeta(Base):
    """지표 메타 (config/indicators.json 미러)."""

    __tablename__ = "macro_indicator_meta"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    axis: Mapped[str] = mapped_column(String(16))  # leading/coincident/lagging
    source: Mapped[str] = mapped_column(String(32))  # fred/yahoo/tradingeconomics
    series_key: Mapped[str] = mapped_column(String(64))  # FRED id / yahoo symbol / TE indicator
    freq: Mapped[str] = mapped_column(String(16))
    is_core: Mapped[bool] = mapped_column(default=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    invert: Mapped[bool] = mapped_column(default=False)
    transform: Mapped[str] = mapped_column(String(32), default="")


class MacroSeries(Base):
    """매크로 시계열 관측치. vintage_date 로 발표시점 보존(백테스트용, 현재는 수집일)."""

    __tablename__ = "macro_series"
    __table_args__ = (UniqueConstraint("series_id", "obs_date", name="uq_series_date"),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(64), index=True)  # indicator id
    source: Mapped[str] = mapped_column(String(32))
    obs_date: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    vintage_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketPrice(Base):
    """지수/ETF/종목 일간 종가."""

    __tablename__ = "market_price"
    __table_args__ = (UniqueConstraint("symbol", "obs_date", name="uq_symbol_date"),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    obs_date: Mapped[date] = mapped_column(Date)
    close: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TEHeadline(Base):
    """Trading Economics 페이지 현재값/예측치 스냅샷 (사용자 지정 소스).

    과거 시계열은 macro_series(FRED), 여기는 TE 고유의 현재값+Forecast 만.
    §2.3 침체 트리거(실업률 Previous/Forecast)·PMI 50기준 확인용.
    """

    __tablename__ = "te_headline"
    __table_args__ = (UniqueConstraint("indicator_id", "captured_date", name="uq_te_capture"),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    indicator_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    latest_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_period: Mapped[str] = mapped_column(String(32), default="")  # 예: "5월"
    forecast: Mapped[str] = mapped_column(String(128), default="")  # TEForecast 배열 원문
    source_url: Mapped[str] = mapped_column(String(256), default="")
    captured_date: Mapped[date] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SP500Constituent(Base):
    """S&P500 구성종목 + GICS 섹터 (위키 소스). 종목 추천 유니버스."""

    __tablename__ = "sp500_constituent"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    gics_sector: Mapped[str] = mapped_column(String(64), index=True)
    sub_industry: Mapped[str] = mapped_column(String(128), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RegimeSnapshot(Base):
    """국면 판정 결과 (Phase 2 에서 기록)."""

    __tablename__ = "regime_snapshot"

    obs_date: Mapped[date] = mapped_column(Date, primary_key=True)
    leading_score: Mapped[float] = mapped_column(Float)
    coincident_score: Mapped[float] = mapped_column(Float)
    lagging_score: Mapped[float] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(24))  # recovery/growth/slowdown/recession/transition
    confidence: Mapped[float] = mapped_column(Float)
    is_provisional: Mapped[bool] = mapped_column(default=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class Recommendation(Base):
    """국면별 추천 (Phase 4 에서 기록)."""

    __tablename__ = "recommendation"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    obs_date: Mapped[date] = mapped_column(Date, index=True)
    regime: Mapped[str] = mapped_column(String(24))
    layer: Mapped[str] = mapped_column(String(16))  # index/style/currency/sector/ticker
    symbol: Mapped[str] = mapped_column(String(32))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str] = mapped_column(Text, default="")
