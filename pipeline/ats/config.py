"""설정 로딩 + 환경변수. 프레임워크 정의는 config/*.json 에 데이터로 보관."""

import json
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent  # pipeline/
CONFIG_DIR = ROOT_DIR / "config"

# 개발: SQLite(무설정) / 운영: 환경변수로 MariaDB 지정
# 예) export DATABASE_URL="mysql+pymysql://user:pw@localhost:3306/ats?charset=utf8mb4"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{ROOT_DIR / 'ats.db'}")

# Trading Economics 키(PMI/Forecast용). 형식 "key:secret". 없으면 PMI는 프록시 대체.
TE_API_KEY = os.environ.get("TE_API_KEY", "").strip()

# 시장데이터 수집 시작일(일간 데이터 경량화)
PRICE_START_DATE = os.environ.get("PRICE_START_DATE", "2005-01-01")


def load_json(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def load_indicators() -> dict:
    return load_json("indicators.json")


def load_universe() -> dict:
    return load_json("universe.json")
