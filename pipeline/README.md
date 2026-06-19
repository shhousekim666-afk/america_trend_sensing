# ATS Pipeline — 미국 경기 국면 센싱 (Phase 1~3)

`지침.md` 1차 스펙, `youtube_report.md` 참조. 영상(2026-06-17, 버디버디) 프레임워크의 정량 구현.

## 구성
```
pipeline/
  config/
    indicators.json   # 경기지표 정의(6대 핵심+보조, 축/소스/변환/가중/반전) + TE 오버레이
    universe.json     # 지수·섹터 ETF + 국면별 로테이션 플레이북
  ats/
    config.py         # 설정/환경변수 로딩
    db.py models.py   # SQLAlchemy (SQLite 개발 / MariaDB 운영)
    sources/          # fred(과거 시계열) · yahoo(주가/ETF) · tradingeconomics(현재값+예측+차트PNG)
    collect.py        # 수집 오케스트레이션
    regime.py         # 국면 판정 엔진 + 백테스트 + 침체 트리거
    cli.py            # 진입점
  te_charts/          # TE 차트 PNG 아카이브
```

## 데이터 소스 (하이브리드 — 지침 §3.1)
- **Trading Economics** (사용자 지정, `ko.tradingeconomics.com`): 현재값 + **예측치(Forecast)** + 차트 PNG.
  TE 무료 API는 폐지(guest 410), 차트 JSON 엔드포인트는 차단 → 페이지 파싱.
- **FRED** (키 불필요 `fredgraph.csv`): 과거 시계열. TE가 표시하는 미국 매크로와 **동일 원본**(BLS/BEA/Fed).
- **yfinance**: 주가지수·섹터 ETF.

## 실행
```bash
python3 -m venv --system-site-packages .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m ats.cli collect    # 수집 → DB
./.venv/bin/python -m ats.cli regime      # 현재 국면
./.venv/bin/python -m ats.cli verify      # 영상 결론 대비 검증
./.venv/bin/python -m ats.cli backtest    # 연도별 국면(2000~)
./.venv/bin/python -m ats.cli evaluate    # NBER 침체 대비 정량검증(precision/recall/F1/시차/휘프소)
./.venv/bin/python -m ats.cli recommend   # 현재 국면 → 지수/섹터/종목(펀더멘털+시총, TA 기본)
./.venv/bin/python -m ats.cli recommend --no-ta  # 종목선별을 모멘텀+시총 폴백으로(빠름)
./.venv/bin/python -m ats.cli report      # 시각화 HTML 대시보드(탭: 대시보드/지표/설명)
```
운영 MariaDB: `export DATABASE_URL="mysql+pymysql://user:pw@host:3306/ats?charset=utf8mb4"`
TE 유료 키 보유 시(선택): `export TE_API_KEY="key:secret"`

## 국면 판정 로직 (지침 §2)
1. 지표별 방향부호(+1/0/-1): YoY / 3·12개월 MA 기울기, `invert`(상승=수축 지표) 적용.
2. 축별 확산지수(가중평균) → 임계 ±0.33 으로 선행/동행/후행 라벨.
3. (선행,동행,후행) 벡터를 4국면 원형과 **거리매칭** → 국면 + 신뢰도. 동률=전환(중립).
4. **지속성 필터**(3개월 확정)로 휘프소 방어 → `regime_s`(확정).
5. **침체 트리거**(§2.3): TE 실업률 Forecast + FRED 직전월 연속 상승 → 경보.

## 현재 상태 (2026-06 수집 기준)
- 현재 국면: **성장(Growth)**, 신뢰도 0.99 — 영상 결론과 ✅ 일치.
- NBER 정량검증(`evaluate`): **Precision 0.42 / Recall 0.71 / F1 0.53**, 2001(-8M 선행)·2008(+5M)·2020(+1M) 포착, 휘프소 raw 0.30→평활 0.05.
- **2024 오신호 해결**(금리차 역전 게이트 + 실업률 절대레벨 게이트) → 둔화로 정정.

## 전략 백테스트(`strategy`) — 레버리지 없이 SPY 초과 (2006~2026, 비용·리스크 차감)
거래비용 편도 0.3%(스프레드·슬리피지 포함), Sharpe는 무위험수익률(FRED DGS3MO 3M 국채) 차감한 **초과수익 기준**.
| 전략 | CAGR | MDD | Sharpe |
|---|---|---|---|
| SPY 단순보유(벤치) | 11.1% | -51% | 0.65 |
| **공격형 — 권고 바스켓(QQQ중심+침체방어)** ★ | **11.4%** | **-28%** | **0.72** |
| 균형형 — 분산 바스켓(낙폭최소·효율최고) | 9.3% | -25% | 0.63 |
| SPY 국면타이밍(순수방어) | 5.9% | -21% | 0.48 |
| 섹터 favored 상위4 등가 | 3.6% | -29% | 0.21 |

**결론(데이터 검증):**
1. **공격형(권고 바스켓)이 SPY를 CAGR·MDD·Sharpe 3개 모두 초과** — 좋은 국면+둔화까지 고베타 **QQQ 중심**, 금·채권 슬리브로 변동성 완화, 침체에만 안전자산 최대화. 레버리지 없음.
2. **균형형**은 분산 강화로 낙폭 최소·효율 우위이나 수익은 SPY 아래. 영상 지역 프레임 충실. 위험성향에 따라 선택.
3. **⚠ 정직 공시:** 선행축에 S&P500 주가가 포함돼 "주가↑→회복/성장→주식↑"의 일부 순환이 있다. **가격 선행지표를 빼면 공격형이 9.6%/-44%/0.57로 SPY에 진다** → 엣지의 약 절반은 추세추종이며 순수 매크로 알파는 제한적. 주가는 정당한 선행지표지만 "매크로 예측력"으로 과신 금지.
4. **섹터선택은 알파 없음** — 자산 국면 베타로 타는 게 최적. → 핵심은 "주식비중 + QQQ 틸트 타이밍".

## 엔진 핵심(개선 반영)
- 신뢰도: 연속 DI → 4국면 거리 → **softmax 확률**(양자화 제거, 중립 실작동).
- 레벨 게이트: 금리차 역전 중 +기여 차단 / 실업률 4%↓ 악화기여 반감(2024 오신호 방어).
- 침체 트리거 이원화: 실업률 연속상승 **OR** HY 신용스프레드(BAMLH0A0HYM2) 12M평균 25%↑.
- 비대칭 지속성: 침체(방어)는 2개월, 그 외 3개월 확정 → 침체 recall 보강 + 휘프소 억제.
- 추천(성장): 공격형 바스켓 총 주식비중 95%(QQQ45/SPY20/IWM15/EEM8/EFA7 + GLD5), 섹터 ETF 모멘텀 상위=XLK·XLE·XLB·XLI, 개별종목=시클리컬 4섹터(산업재/소재/금융/에너지) S&P500 모멘텀 상위 15.

## 지수 = 국면별 자산배분 바스켓 (공격형=권고, 균형형=참고)
지수 추천은 단순 나열이 아니라 **국면별 자산배분 바스켓**: 미국주식(QQQ/SPY/IWM/DIA) + 지역(EFA=DM/EEM=EM) + 안전자산(TLT/GLD/SHY), 각 목표비중·6M모멘텀·200DMA 상회 표시.
- **공격형**(`regime_index_basket`, 권고): 좋은 국면+둔화까지 **고베타 QQQ 중심**으로 주식을 채워 SPY 초과 수익(11.6%/-28%/0.85), 침체에만 안전자산 최대화.
- **균형형**(`regime_index_basket_balanced`, 참고): 분산 강화로 낙폭 최소(-25%)·Sharpe 최고(0.78), 영상 지역 프레임 충실. 수익은 SPY 아래.
**총 주식비중**이 RISK 베타(회복/성장 95%→둔화 75%→침체 25%)를 따라가 모델 핵심(위험노출 타이밍)을 노출.

## 종목 과열 반영
추천 종목에 **200DMA 이격도(1순위)·52주위치·RSI(14)** → 과열 경고 배지. 성장(Take-Profit) 국면에선 **과열 강도에 비례 감점**(가장 파라볼릭한 종목을 후순위로). 탈락 없이 표시+감점.

## 종목 선별(Phase 5, 섹터 일관 + 펀더멘털 + 시총)
1. 유니버스 = 현재 국면 favored 섹터 중 **양(+)모멘텀 상위 섹터**(섹터 패널과 일관) → 모멘텀 프리필터.
2. 점수 = **trading_america 펀더멘털 3팩터(value/momentum/dividend, 국면별 가중)** + **국면별 시가총액 틸트**(`regime_size_tilt`: 회복/둔화/침체=대형 선호) → 섹터분산 상위 N.
3. 표시: 종합점수·V/M/D·시총·ROE·6M모멘텀. (PER·배당 원값은 yfinance .info 부정확으로 비표시; 스코어에 z-score 반영됨)
- TA 미가용 시 모멘텀/저변동성+시총 복합으로 폴백(`--no-ta`).

## 알려진 한계 / 다음 단계
- PMI 과거 시계열 무료 미제공 → 선행 모멘텀은 FRED 신규주문 프록시, PMI는 현재값(50기준)만.
- TE 과거 시계열 직접 수집 불가 → FRED(동일 원본) 사용 + TE 차트 이미지 아카이브.
- 백테스트 정답 라벨(NBER) 자동 대조, 발표시점(vintage) 데이터 기반 무편향 백테스트는 미구현.
