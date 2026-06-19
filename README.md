# America Trend Sensing — 미국 경기 국면 센싱 & 투자 신호 대시보드

NH 백찬규 센터장의 **선행/동행/후행 → 4국면(회복·성장·둔화·침체) 로테이션** 방법론을 정량화한 매크로 국면 판정 + 투자 신호 시스템. 매월 자동 갱신되어 GitHub Pages 대시보드로 배포됩니다.

> ⚠️ **교육·연구용입니다. 투자 권유가 아닙니다.** 백테스트(거래비용·무위험수익률 차감), **공격형 권고 바스켓**(좋은 국면+둔화까지 고베타 QQQ 중심, 침체에만 방어)은 **레버리지 없이 SPY를 CAGR·MDD·Sharpe 3개 모두 초과**합니다(11.4% / -28% / 0.72 vs 11.1% / -51% / 0.65). 더 안정적인 **균형형**(9.3% / -25% / 0.63)도 제공. 단, 초과수익의 상당 부분은 가격 추세추종에서 오며(선행축에 주가 포함) 순수 매크로 알파는 제한적입니다(상세는 대시보드 검증탭의 정직 공시). 단독 매매신호로 쓰지 말고 전술적 자산배분(TAA)의 비중·틸트 다이얼로 사용하세요.

## 무엇을 하나
1. **국면 판정** — FRED(과거 시계열) + Trading Economics(현재값·예측) 매크로 지표 → 3축 확산지수 → 4국면 + 신뢰도
2. **투자 신호** — 국면별 지수/스타일/통화/섹터 플레이북 + S&P500 종목(펀더멘털 3팩터 + 시가총액)
3. **검증** — NBER 침체 대비 정량검증(precision/recall) + 전략 백테스트 vs SPY(비용·리스크 차감)
4. **대시보드** — 탭형 HTML(대시보드 / 지표 / 검증 / 설명)

자세한 설계는 [지침.md](지침.md), 기술 문서는 [pipeline/README.md](pipeline/README.md).

## 로컬 실행
```bash
cd pipeline
python3 -m venv --system-site-packages .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m ats.cli update     # 수집→국면→추천→리포트 일괄
open report.html
```
개별 명령: `collect · regime · recommend · evaluate · strategy · backtest · report`

## 주기 업데이트 + 배포 (GitHub Actions → Pages)
`.github/workflows/update.yml` 이 자동 처리:
- **매월 25일**(데이터 발표 후) 국면 갱신 + **매주 월요일** 가격/추천 갱신 (cron)
- 파이프라인 실행 → `report.html` 생성 → **GitHub Pages 로 배포**
- 수동 실행: Actions 탭 → Run workflow

### 최초 설정
1. 이 저장소를 GitHub 에 push
2. **Settings → Pages → Source = "GitHub Actions"** 로 지정
3. Actions 탭에서 워크플로 1회 수동 실행 → 발행되는 Pages URL 에서 대시보드 확인

> 무료 계정은 **public 저장소**에서만 Pages 가 동작합니다(private 은 유료 플랜 필요).

## 데이터 소스
- **FRED**(키 불필요): 과거 매크로 시계열 13종
- **Trading Economics**(`ko.tradingeconomics.com`): 현재값·예측치·차트
- **yfinance**: 지수/섹터 ETF·개별종목
- **Wikipedia**: S&P500 구성종목·GICS

운영 MariaDB 사용 시 `export DATABASE_URL="mysql+pymysql://..."` (기본 SQLite).
trading_america(별도 프로젝트)가 있으면 종목선별을 펀더멘털 3팩터로 강화(없으면 모멘텀+시총 폴백).
