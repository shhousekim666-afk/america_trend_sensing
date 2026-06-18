"""CLI 진입점.

  python -m ats.cli collect     # 데이터 수집(FRED+TE+yahoo) → DB
  python -m ats.cli regime      # 현재 국면 분석
  python -m ats.cli backtest    # 연도별 국면 백테스트
  python -m ats.cli evaluate    # NBER 침체 대비 정량 검증(precision/recall/시차/휘프소)
  python -m ats.cli strategy    # 전략 백테스트 vs SPY(CAGR/MDD/Sharpe/회전율, 비용·리스크 차감)
  python -m ats.cli verify      # 영상 결론(2026-06 성장) 대비 검증
  python -m ats.cli recommend   # 현재 국면 → 지수/섹터/종목 추천
  python -m ats.cli report      # 결과 시각화 HTML 대시보드 생성/열기
  python -m ats.cli update      # 전체 파이프라인 일괄(수집→국면→추천→리포트), 주기실행/CI용
"""

import sys

from .config import DATABASE_URL


def cmd_collect():
    from .collect import run_all
    run_all()


def cmd_regime():
    from .regime import current_regime, persist_history
    r = current_regime()
    if "error" in r:
        print(r["error"]); return
    n = persist_history()  # regime_snapshot 테이블에 전체 타임라인 저장(API용)
    print("\n" + "=" * 56)
    print(f"  현재 경기 국면: {r['regime_kr']} ({r['regime']})")
    print(f"  기준일: {r['as_of']}  |  신뢰도: {r['confidence']}  |  잠정: {r['provisional']}")
    print("=" * 56)
    s = r["scores"]
    print(f"  3축 확산지수  선행={s['leading']:+.2f}  동행={s['coincident']:+.2f}  후행={s['lagging']:+.2f}")
    t = r["recession_trigger"]
    print(f"  침체 트리거: {'⚠ 경보' if t['alert'] else '정상'}  | {t['detail']}")
    print(f"  (국면 타임라인 {n}개월 regime_snapshot 저장)")
    print()


def cmd_recommend():
    from .recommend import recommend
    use_ta = "--no-ta" not in sys.argv  # 기본=trading_america 펀더멘털, --no-ta 면 모멘텀 폴백
    r = recommend(use_ta=use_ta)
    if "error" in r:
        print(r["error"]); return
    print("\n" + "=" * 60)
    print(f"  투자 추천 — {r['regime_kr']}({r['regime']}) 국면  기준 {r['as_of']}  신뢰도 {r['confidence']}")
    print("=" * 60)
    print(f"  스탠스 : {r['stance']}")
    print(f"  지역/스타일/통화 : {r['region']} | {r['style']} | {r['currency']}")
    print(f"\n  ▶ 자산배분 바스켓 (총 주식비중 {r.get('equity_pct')}%)")
    for x in r["index"]:
        trend = "▲" if x.get("above_ma200") else "▽"
        print(f"      {x['symbol']:5} {x['name']:22} {x['asset']:5} 목표 {x['weight']:>2}%  6M {x.get('mom6m')}% {trend}200DMA")
    print(f"\n  ▶ 섹터 ETF (6M 모멘텀 랭킹)")
    for s in r["sectors"]:
        print(f"      {s['symbol']:5} {s['name']:28} 6M {s['mom6m']}%")
    print(f"\n  ▶ S&P500 개별종목  (유니버스: {', '.join(r.get('stock_sectors', []))} | 시총틸트 {r.get('size_tilt')})")
    print(f"     엔진: {r.get('stock_engine')}")
    for i, s in enumerate(r["stocks"], 1):
        from .recommend import _fmt_cap
        cap = _fmt_cap(s.get("mcap"))
        hot = " ⚠과열" if s.get("overheat") else ""
        if "total" in s:
            score = (f"종합 {s['final']} (V{s['value']}/M{s['momentum']}/D{s['dividend']}) "
                     f"시총 {cap} 6M {s.get('mom6m')}% 이격 {s.get('dist200')}%")
        else:
            score = f"점수 {s['final']} 6M {s.get('mom6m')}% 시총 {cap} 이격 {s.get('dist200')}%"
        print(f"      {i:2}. {s['symbol']:6} {s['name']:22} {s['gics']:12} {score}{hot}")
    print()


def cmd_backtest():
    from .regime import backtest_yearly
    df = backtest_yearly()
    if df.empty:
        print("데이터 없음 — 먼저 collect 실행"); return
    print("\n연도별 경기 국면 백테스트 (회=회복 성=성장 둔=둔화 침=침체 ·=전환)\n")
    with __import__("pandas").option_context("display.max_rows", None, "display.width", 120):
        print(df.to_string())
    print()


def cmd_verify():
    from .regime import current_regime
    r = current_regime()
    if "error" in r:
        print(r["error"]); return
    expected = "growth"  # 영상(2026-06-17, 버디버디) 결론: 성장 국면
    ok = r["regime"] == expected
    print("\n[영상 검증] 출처: 까다로운 6월 FOMC (2026-06-17, Money Comics/버디버디)")
    print(f"  영상 결론   : 성장(growth) — '성장 국면입니다, 여러분' (영상 68:35)")
    print(f"  엔진 판정   : {r['regime_kr']}({r['regime']})  신뢰도 {r['confidence']}")
    print(f"  → 검증 {'✅ 일치' if ok else '❌ 불일치 (파라미터 재점검 필요)'}")
    if not ok:
        s = r["scores"]
        print(f"     3축: 선행={s['leading']:+.2f} 동행={s['coincident']:+.2f} 후행={s['lagging']:+.2f}")
    print()


def cmd_evaluate():
    from .regime import evaluate
    r = evaluate()
    if "error" in r:
        print(r["error"]); return
    print("\n[NBER 침체(USREC) 대비 국면 판정 검증]  2000~현재")
    print(f"  Precision {r['precision']}  Recall {r['recall']}  F1 {r['f1']}"
          f"   (TP {r['tp']} / FP {r['fp']} / FN {r['fn']} / TN {r['tn']}, {r['months']}개월)")
    print(f"  휘프소율  raw {r['whipsaw_raw']}  →  평활 {r['whipsaw_smoothed']}")
    print("  침체 에피소드 시차(음수=선행, 월):")
    for e in r["lead_lag"]:
        d = e["delta_m"]
        tag = "미포착" if d is None else (f"{d:+d}개월" + (" 선행" if d < 0 else " 지연" if d > 0 else " 동시"))
        print(f"    NBER {e['nber']} → {tag}")
    print()


def cmd_strategy():
    from .strategy import backtest_strategy
    r = backtest_strategy()
    if "error" in r:
        print(r["error"]); return
    print(f"\n[전략 백테스트] {r['period']}  (비용 편도 {r['params']['cost_oneway']*100}%, 추세필터 {r['params']['trend_filter']})")
    print(f"  {'':32}{'CAGR':>7}{'MDD':>7}{'Sharpe':>8}{'회전':>6}{'노출':>6}")
    b = r["benchmark"]
    print(f"  {b['label']:30}{b['cagr']*100:>6.1f}%{b['mdd']*100:>6.0f}%{b['sharpe']:>8.2f}      (벤치마크)")
    for s in sorted(r["variants"], key=lambda x: x["sharpe"], reverse=True):
        star = " ★최적" if s["key"] == r["best"] else ""
        print(f"  {s['label']:30}{s['cagr']*100:>6.1f}%{s['mdd']*100:>6.0f}%{s['sharpe']:>8.2f}"
              f"{s['ann_turnover']:>5.1f}{s['avg_exposure']*100:>5.0f}%{star}")
    print(f"  → 위험조정성과(Sharpe) 최적: {r['best']}")
    print()


def cmd_report():
    from .report import build
    import subprocess
    p = build()
    print(f"\n대시보드 생성: {p}")
    try:
        subprocess.run(["open", str(p)], check=False)  # macOS 기본 브라우저로 열기
    except Exception:
        pass


def cmd_update():
    """주기 실행/CI용 — 전체 파이프라인 일괄. 각 단계 독립 실패 허용(리포트는 항상 시도)."""
    import traceback
    from .collect import run_all
    from .regime import persist_history
    from .recommend import recommend
    from .report import build
    no_open = True  # CI: 브라우저 자동열기 안 함
    steps = [("수집", run_all), ("국면저장", persist_history),
             ("추천", lambda: recommend()), ("리포트", build)]
    for name, fn in steps:
        try:
            fn(); print(f"[update] {name} ✓")
        except Exception as e:
            print(f"[update] {name} ✗ {type(e).__name__}: {e}")
            traceback.print_exc()
    _ = no_open


_CMDS = {"collect": cmd_collect, "regime": cmd_regime, "backtest": cmd_backtest,
         "verify": cmd_verify, "recommend": cmd_recommend, "report": cmd_report,
         "evaluate": cmd_evaluate, "strategy": cmd_strategy, "update": cmd_update}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in _CMDS:
        print(__doc__)
        print(f"DB: {DATABASE_URL}")
        return
    _CMDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
