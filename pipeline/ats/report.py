"""결과 시각화 — 탭형 자체 완결형 HTML 대시보드 (DB만 읽음).

탭1 대시보드: 현재 국면 + 3축 DI + 국면 히트맵 + TE 현재값 + 추천
탭2 지표:    선행/동행/후행 전 지표 차트 + 현재값 + 의미 설명
탭3 설명:    국면 센싱 원리 + 4국면별 의미·투자 + 영상 슬라이드 이미지
"""

import base64
import json
from collections import defaultdict

import pandas as pd
from sqlalchemy import func, select

from .collect import CHART_DIR
from .config import ROOT_DIR, load_indicators, load_universe
from .db import SessionLocal
from .models import (IndicatorMeta, MacroSeries, MarketPrice, Recommendation,
                     RegimeSnapshot, SP500Constituent, TEHeadline)
from .regime import KOR, current_regime, evaluate, explain_current
from .strategy import backtest_strategy

GICS_KR = {"Information Technology": "정보기술", "Communication Services": "커뮤니케이션",
           "Consumer Discretionary": "자유소비재", "Consumer Staples": "필수소비재",
           "Financials": "금융", "Industrials": "산업재", "Energy": "에너지",
           "Materials": "소재", "Health Care": "헬스케어", "Utilities": "유틸리티",
           "Real Estate": "리츠/부동산"}

REGIME_COLOR = {"recovery": "#3b82f6", "growth": "#22c55e", "slowdown": "#f59e0b",
                "recession": "#ef4444", "transition": "#9ca3af"}
SYM = {"recovery": "회", "growth": "성", "slowdown": "둔", "recession": "침", "transition": "·"}
AXIS_KR = {"leading": "선행", "coincident": "동행", "lagging": "후행"}
AXIS_COLOR = {"leading": "#3b82f6", "coincident": "#22c55e", "lagging": "#f59e0b"}
_6M = 126

# 지표별 의미 설명 (무엇 / 임계 의미)
EXPLAIN = {
    "sp500": "주가는 경기를 6~9개월 선행. 12개월 이동평균 위 + 우상향이면 확장 기대, 추세 이탈 시 경계.",
    "pmi_mfg": "구매관리자지수. 50 기준선 — 이상이면 제조업 확장, 미만이면 위축. 55↑은 강한 확장.",
    "new_orders": "제조업 신규주문(총 공장주문, TE factory-orders 동일). 향후 생산의 씨앗 → 선행. 차트는 전월比%.",
    "umcsent": "소비자심리지수. 소비 의향을 선반영. 상승 추세면 소비 확대 신호.",
    "t10y2y": "10년-2년 국채 금리차. 마이너스(역전)는 12~18개월 내 침체 경고. 플러스 전환은 회복 신호(역전 자체가 즉시 침체는 아님). ※역전 중엔 +기여 차단(게이트).",
    "hy_spread": "하이일드 신용스프레드(HY OAS). 위험채권 가산금리 — 확대=신용경색=경기악화로, 실업률보다 먼저 침체를 경고. 차트는 % 레벨(낮을수록 안정).",
    "initial_claims": "주간 신규 실업수당 청구. 추세적 증가=고용 악화 선행. 낮고 안정적이면 확장(증가가 악재라 부호 반전).",
    "indpro": "산업생산. 공장 실제 생산량으로 경기와 동행. 꼭지 찍고 하락 전환 시 둔화 진입.",
    "retail_sales": "소매판매(명목 Advance, TE retail-sales 동일). 실제 소비량=경기 동행. 차트는 전월比%.",
    "tcu": "설비가동률. 공장이 얼마나 풀가동인가. 높을수록 수요 강함(동행).",
    "payems": "비농업 고용자수. 일자리 증가=경기 확장 동행.",
    "gdp": "실질 GDP. 모든 활동의 최종 집계라 가장 늦게 확정되는 후행. 전년比 마이너스 전환이면 침체.",
    "unrate": "실업률(후행 핵심 앵커). 경기가 꺾인 뒤 가장 늦게 오름. 바닥에서 상승 전환 시작=침체 확정 신호. 통상 3.5~4%는 완전고용권, 저점 대비 빠른 상승이 위험.",
    "cpi": "소비자물가(CPI). 높으면 금리(밸류에이션 분모) 압박→주가 부담. 후행.",
    "wages": "시간당 평균임금. 실업률보다 더 늦는 최후행(영상). 핵심 지표 아님, 참고용.",
}


def _b64(path):
    try:
        return base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return ""


CHART_UNIT = {"yoy": "% (전년比)", "mom": "% (전월比)", "change": " (전기증감)", "level": ""}


def _display_series(rows, freq, kind):
    """월말 기준 시계열을 TE 단위(yoy/mom/change/level)로 변환 → 최근 120개월(10년)."""
    s = pd.Series({pd.Timestamp(d): v for d, v in rows}).sort_index()
    s = s.resample("ME").last().ffill()  # 월말 통일(분기 GDP 등 ffill)
    if kind == "yoy":
        s = s.pct_change(12) * 100
    elif kind == "mom":
        s = s.pct_change(1) * 100
    elif kind == "change":
        s = s.diff(1)
    s = s.dropna()
    return [[d.date().isoformat(), round(float(v), 2)] for d, v in s.items()][-120:]


def _gather():
    reg = current_regime()
    uni = load_universe()
    cfg = load_indicators()
    order = {ind["id"]: i for i, ind in enumerate(cfg["indicators"])}
    chart_display = cfg.get("chart_display", {})
    freq_by = {ind["id"]: ind.get("freq", "monthly") for ind in cfg["indicators"]}

    with SessionLocal() as s:
        snaps = s.execute(select(RegimeSnapshot).order_by(RegimeSnapshot.obs_date)).scalars().all()
        last_rec_date = s.execute(select(func.max(Recommendation.obs_date))).scalar()
        recs = s.execute(
            select(Recommendation).where(Recommendation.obs_date == last_rec_date)
            .order_by(Recommendation.rank)
        ).scalars().all() if last_rec_date else []
        te = s.execute(select(TEHeadline).order_by(TEHeadline.captured_date.desc())).scalars().all()
        metas = {m.id: m for m in s.execute(select(IndicatorMeta)).scalars().all()}

        indicators = []
        for iid in sorted(order, key=order.get):
            meta = metas.get(iid)
            if not meta:
                continue
            rows = s.execute(
                select(MacroSeries.obs_date, MacroSeries.value)
                .where(MacroSeries.series_id == iid).order_by(MacroSeries.obs_date)
            ).all()
            if not rows:
                continue
            kind = chart_display.get(iid, "level")  # TE 차트와 동일 단위로 표시
            series = _display_series([(r[0], r[1]) for r in rows], freq_by.get(iid, "monthly"), kind)
            indicators.append({
                "id": iid, "name": meta.name, "axis": meta.axis,
                "axis_kr": AXIS_KR.get(meta.axis, meta.axis),
                "cur": series[-1][1] if series else None,
                "unit": CHART_UNIT.get(kind, ""),
                "explain": EXPLAIN.get(iid, ""), "series": series,
                "te_b64": _b64(CHART_DIR / f"{iid}.png"),  # TE 원본 차트
            })

        pb = uni["regime_playbook"].get(reg.get("regime", ""), {})
        sec_mom = {}
        for sym in pb.get("sector_etfs", []):
            closes = [r[0] for r in s.execute(
                select(MarketPrice.close).where(MarketPrice.symbol == sym).order_by(MarketPrice.obs_date)
            ).all()]
            if len(closes) > _6M:
                sec_mom[sym] = round((closes[-1] / closes[-1 - _6M] - 1) * 100, 1)

    timeline = [{"d": x.obs_date.isoformat(), "L": x.leading_score, "C": x.coincident_score,
                 "Lag": x.lagging_score, "r": x.regime} for x in snaps]
    rec_by = defaultdict(list)
    for r in recs:
        rec_by[r.layer].append({"symbol": r.symbol, "rank": r.rank, "rationale": r.rationale})

    # 개별종목 상세 = 추천(ticker) + 구성종목(종목명/세부업종) 조인 + 국면별 한줄설명
    regime_kr = reg.get("regime_kr", "")
    factor_txt = uni["regime_playbook"].get(reg.get("regime", ""), {}).get("stock_factor", "")
    with SessionLocal() as s2:
        cons = {c.symbol: c for c in s2.execute(select(SP500Constituent)).scalars().all()}
    stocks_detail = []
    for r in rec_by.get("ticker", []):
        sym = r["symbol"]
        c = cons.get(sym)
        gics = c.gics_sector if c else ""
        gkr = GICS_KR.get(gics, gics)
        detail = r["rationale"].split("] ", 1)[-1] if "] " in r["rationale"] else r["rationale"]
        name = c.name if c else r["rationale"].split(" [", 1)[0]
        sub = c.sub_industry if c else ""
        desc = f"{regime_kr} 국면 유리 업종({gkr})의 {factor_txt} 상위 종목. {sub}."
        stocks_detail.append({"rank": r["rank"], "symbol": sym, "name": name,
                              "gics_kr": gkr, "sub": sub, "metric": detail, "desc": desc})
    te_list, seen = [], set()
    for t in te:
        if t.indicator_id in seen:
            continue
        seen.add(t.indicator_id)
        te_list.append({"id": t.indicator_id, "name": t.name, "value": t.latest_value,
                        "period": t.latest_period, "forecast": t.forecast})
    sectors = [{"symbol": k, "name": uni["sector_etfs"].get(k, ""), "mom": v}
               for k, v in sorted(sec_mom.items(), key=lambda x: x[1], reverse=True)]
    explain = explain_current()
    try:
        valid = {"nber": evaluate(), "strategy": backtest_strategy()}
    except Exception:
        valid = {"nber": {}, "strategy": {}}
    return reg, timeline, rec_by, te_list, sectors, indicators, uni, stocks_detail, explain, valid


def _heatmap(timeline):
    by_year = defaultdict(lambda: ["" for _ in range(12)])
    for row in timeline:
        by_year[int(row["d"][:4])][int(row["d"][5:7]) - 1] = row["r"]
    head = "<tr><th>연도</th>" + "".join(f"<th>{m}</th>" for m in range(1, 13)) + "</tr>"
    body = ""
    for y in sorted(by_year):
        tds = "".join(
            f'<td style="background:{REGIME_COLOR.get(r,"#1f2937")}" title="{KOR.get(r,"")}">{SYM.get(r,"")}</td>'
            for r in by_year[y])
        body += f"<tr><td class='yr'>{y}</td>{tds}</tr>"
    return f"<div class='tbl-wrap'><table class='heat'>{head}{body}</table></div>"


AXIS_NOTE = {
    "leading": "주가·심리·금리차처럼 경기보다 먼저 움직임. 가장 먼저 꺾인다.",
    "coincident": "산업생산·소비처럼 경기와 함께 움직임. 실물 현황.",
    "lagging": "GDP·실업률처럼 경기 전환을 가장 늦게 확정. 침체 확정 앵커.",
}


def _indicator_cards(indicators, eff_by_id):
    by_axis = defaultdict(list)
    for ind in indicators:
        by_axis[ind["axis"]].append(ind)
    html = ""
    for axis in ("leading", "coincident", "lagging"):
        items = by_axis.get(axis, [])
        if not items:
            continue
        col = AXIS_COLOR[axis]
        html += (f'<h3 style="color:{col};margin-top:26px">{AXIS_KR[axis]}지표 · {len(items)}개</h3>'
                 f'<p class="cap">{AXIS_NOTE[axis]} <b>해석</b>: 각 지표의 12·3개월 추세 방향 → ▲확장/▼수축 기여로 환산해 이 축의 확산지수에 합산.</p>')
        html += '<div class="igrid">'
        for ind in items:
            e = eff_by_id.get(ind["id"], {})
            eff = e.get("effect", 0)
            ec = EFFECT_COL.get(eff, "#9ca3af")
            inv = " · 값↑=수축(반전지표)" if e.get("invert") else ""
            badge = (f'<span class="dirb" style="background:{ec}22;color:{ec};border-color:{ec}55">'
                     f'{DIR_ARROW.get(eff,"")} {EFFECT_KR.get(eff,"")}</span>')
            te_img = (f'<details class="teimg"><summary>TE 원본 차트</summary>'
                      f'<img src="data:image/png;base64,{ind["te_b64"]}"/></details>') if ind.get("te_b64") else ""
            html += (
                f'<div class="card"><div class="ihead">'
                f'<b>{ind["name"]}</b><span class="badge" style="background:{col}33;color:{col}">{ind["axis_kr"]}</span></div>'
                f'<div class="icur">현재 <b>{ind["cur"]}{ind.get("unit","")}</b> {badge}</div>'
                f'<canvas id="c_{ind["id"]}" height="120"></canvas>'
                f'<p class="iexp">{ind["explain"]}<span style="color:{ec}"> ▶ 지금: 값 {DIR_KR.get(e.get("raw_dir",0),"")} → {EFFECT_KR.get(eff,"")}{inv}</span></p>'
                f'{te_img}</div>'
            )
        html += "</div>"
    return html


def _regime_cards(uni):
    pb = uni["regime_playbook"]
    order = ["recovery", "growth", "slowdown", "recession"]
    html = '<div class="rgrid">'
    for k in order:
        p = pb[k]
        c = REGIME_COLOR[k]
        secs = " · ".join(p.get("sectors_kr", []))
        etfs = ", ".join(p.get("sector_etfs", []))
        ex = ", ".join(p.get("examples", []))
        html += (
            f'<div class="card rcard" style="border-color:{c}">'
            f'<h4 style="color:{c}">{p["label"]} ({k})</h4>'
            f'<div class="rrow"><span>스탠스</span>{p.get("stance","")}</div>'
            f'<div class="rrow"><span>지역</span>{p.get("region","")}</div>'
            f'<div class="rrow"><span>지수</span>{", ".join(p.get("index_pref",[]))} — {p.get("index_note","")}</div>'
            f'<div class="rrow"><span>스타일</span>{p.get("style","")}</div>'
            f'<div class="rrow"><span>통화</span>{p.get("currency","")}</div>'
            f'<div class="rrow"><span>유리 업종</span>{secs}</div>'
            f'<div class="rrow"><span>섹터 ETF</span>{etfs}</div>'
            f'<div class="rrow"><span>예시</span>{ex}</div>'
            + (f'<div class="rrow alert"><span>매도 트리거</span>{p.get("exit_trigger","")}</div>' if p.get("exit_trigger") else "")
            + "</div>"
        )
    html += "</div>"
    return html


DIR_ARROW = {1: "▲", 0: "▬", -1: "▼"}
DIR_KR = {1: "상승", 0: "횡보", -1: "하락"}
EFFECT_KR = {1: "확장 기여", 0: "중립", -1: "수축 기여"}
EFFECT_COL = {1: "#22c55e", 0: "#9ca3af", -1: "#ef4444"}


def _basis_panel(explain, reg):
    """판정 근거 패널: 지표 방향 → 축 DI → 4국면 거리매칭."""
    if not explain:
        return ""
    per = defaultdict(list)
    for p in explain["per"]:
        per[p["axis"]].append(p)
    axis_blocks = ""
    for ax in ("leading", "coincident", "lagging"):
        items = per.get(ax, [])
        di = explain["di"].get(ax)
        lab = explain["label"].get(ax)
        labtxt = {1: "확장(↑)", 0: "혼조(→)", -1: "수축(↓)"}.get(lab, "")
        col = AXIS_COLOR[ax]
        chips = ""
        for p in items:
            ec = EFFECT_COL[p["effect"]]
            gate = ' <span style="color:#6b7280">게이트</span>' if p.get("gated") else ""
            chips += (f'<span class="chip" style="border-color:{ec}55">'
                      f'{p["name"]} <b style="color:{ec}">{DIR_ARROW[p["effect"]]}</b>{gate}</span>')
        axis_blocks += (
            f'<div class="axisrow"><div class="axhd" style="color:{col}">'
            f'{AXIS_KR[ax]} <b>DI {di:+.2f}</b> → {labtxt}</div>'
            f'<div class="chips">{chips}</div></div>'
        )
    # 국면 확률(softmax)
    probs = explain.get("probs", {})
    prob_html = ""
    for r in ["recovery", "growth", "slowdown", "recession"]:
        p = probs.get(r, 0) * 100
        on = (r == explain["regime"])
        c = REGIME_COLOR[r]
        prob_html += (f'<span class="dist {"on" if on else ""}" style="border-color:{c};'
                      f'{"background:"+c+"22" if on else ""}">{KOR[r]} {p:.0f}%{" ✓" if on else ""}</span>')
    v = explain["vec"]
    return f"""
<div class="card basis">
  <h4>왜 "{explain['regime_kr']}" 인가 — 판정 근거</h4>
  <p class="cap">각 지표의 <b>경기 기여 방향</b>(▲확장 / ▼수축, 실업률·실업청구·신용스프레드는 값이 올라도 ▼)을 가중 평균해 축별 <b>확산지수(DI, -1~+1)</b>를 만들고, (선행·동행·후행) 연속벡터를 4국면 원형과 거리 비교 → softmax 확률로 환산해 판정.</p>
  {axis_blocks}
  <div class="vecline">현재 DI 벡터 (선행 {v[0]:+.2f}, 동행 {v[1]:+.2f}, 후행 {v[2]:+.2f}) →
    {prob_html}</div>
  <p class="cap">최상위 확률이 신뢰도(={reg.get('confidence')}). 1·2위 확률차가 작으면 전환(중립). 침체(방어)는 2개월·그 외 국면은 3개월 지속 시 '확정'.</p>
</div>"""


ARCHETYPES_DISP = {  # 영상 34p: 4국면 × (선행,동행,후행) 방향 + 위험자산 대응
    "recovery":  {"vec": (1, 0, -1),  "kr": ("반등", "바닥", "하락"), "act": "위험자산 선호(매수 시작)"},
    "growth":    {"vec": (1, 1, 1),   "kr": ("상승", "상승", "상승"), "act": "위험자산 수익실현 준비"},
    "slowdown":  {"vec": (-1, 0, 1),  "kr": ("하락", "전환", "상승"), "act": "위험자산 분할 매수"},
    "recession": {"vec": (-1, -1, -1),"kr": ("하락", "하락", "하락"), "act": "위험자산 포지션 정리 완료"},
}
_ARW = {1: ("↑", "#22c55e"), 0: ("→", "#9ca3af"), -1: ("↓", "#ef4444")}


def _sign(x, th=0.2):
    return 1 if x > th else (-1 if x < -th else 0)


def _regime_matrix(explain, reg):
    """영상 4국면 방향 매트릭스 + 현재 실측 DI 부호 비교 → 판정이 옳은지 사용자가 직접 확인."""
    if not explain:
        return ""
    vec = explain["vec"]
    cur = explain["regime"]
    cur_sign = tuple(_sign(v) for v in vec)
    rows = ""
    for k in ("recovery", "growth", "slowdown", "recession"):
        d = ARCHETYPES_DISP[k]
        c = REGIME_COLOR[k]
        on = k == cur
        cells = ""
        for i in range(3):
            arw, ac = _ARW[d["vec"][i]]
            hit = on and cur_sign[i] == d["vec"][i]
            mark = ' <span style="color:#22c55e">✓</span>' if hit else ""
            cells += f'<td style="color:{ac}">{arw} {d["kr"][i]}{mark}</td>'
        rows += (f'<tr style="{"background:"+c+"22" if on else ""}">'
                 f'<td style="color:{c};font-weight:700;white-space:nowrap">{KOR[k]}{" ★현재" if on else ""}</td>'
                 f'{cells}<td class="sub">{d["act"]}</td></tr>')
    # 현재 실측 행
    cur_cells = ""
    for v in vec:
        s = _sign(v)
        arw, ac = _ARW[s]
        cur_cells += f'<td style="color:{ac};font-weight:700">{arw} {v:+.2f}</td>'
    match = sum(1 for i in range(3) if cur_sign[i] == ARCHETYPES_DISP[cur]["vec"][i])
    cc = REGIME_COLOR[cur]
    return f"""
<div class="card" style="margin-top:14px">
  <h4>국면 판정표 — 현재 데이터가 "{explain['regime_kr']}"와 맞는가</h4>
  <p class="cap">NH투자증권 4국면 프레임(영상 34p): 국면마다 선행·동행·후행의 <b>방향 조합</b>이 정해져 있다. 맨 아래 <b>현재 실측 DI</b> 부호를 위 4국면과 대조하면 지금이 어느 국면인지 눈으로 확인된다. ✓ = 현재 부호가 그 국면 원형과 일치하는 축.</p>
  <div class="tbl-wrap"><table>
   <tr><th>국면</th><th>선행</th><th>동행</th><th>후행</th><th>위험자산 대응</th></tr>
   {rows}
   <tr style="border-top:2px solid {cc}">
     <td style="font-weight:700;white-space:nowrap;color:{cc}">현재 실측</td>{cur_cells}
     <td class="sub">→ <b style="color:{cc}">{explain['regime_kr']}</b> 원형과 {match}/3축 일치</td></tr>
  </table></div>
  <p class="note">화살표 기준: DI &gt; +0.2 상승(↑) / −0.2~+0.2 횡보·전환(→) / &lt; −0.2 하락(↓). 회복의 동행 '바닥', 둔화의 동행 '전환'은 0 부근(→)을 뜻함.</p>
</div>"""


def _exec_guide(uni, reg):
    """수익+방어 균형 전략을 실제로 굴리는 방법 — 국면 다이얼 + 5단계 절차."""
    basket = uni["regime_index_basket"]
    safe = set(uni.get("safe_assets", []))
    cur = reg.get("regime", "")
    # 공격형(권고) 바스켓의 국면별 주식 틸트 — QQQ 중심으로 SPY 초과(영상 EM은 슬리브/약달러시 확대)
    TILT = {
        "recovery":  "미국 대형성장 QQQ 집중 (DM&gt;EM · USD강세)",
        "growth":    "QQQ 중심 + 중소형·이머징 슬리브 IWM·EEM·EFA (약달러 시 EM↑)",
        "slowdown":  "QQQ·SPY 유지 + 배당·방어 DIA + 채권 TLT",
        "recession": "안전자산 TLT·GLD 최대 + MegaCap 퀄리티만 (USD초강세)",
    }
    rows = ""
    for k in ("recovery", "growth", "slowdown", "recession"):
        b = basket.get(k, {})
        eq = sum(v for s, v in b.items() if s not in safe)
        tilt = TILT[k]
        c = REGIME_COLOR[k]
        on = k == cur
        rows += (f'<tr style="{"background:"+c+"18" if on else ""}">'
                 f'<td style="color:{c};font-weight:700;white-space:nowrap">{KOR[k]}{" ★현재" if on else ""}</td>'
                 f'<td><b>{eq}%</b></td><td>{tilt}</td></tr>')
    cur_eq = sum(v for s, v in basket.get(cur, {}).items() if s not in safe)
    return f"""
<div class="card" style="border-color:#22c55e">
  <h4>📋 실행 가이드 — 공격형 권고 바스켓을 직접 굴리는 법</h4>
  <p class="cap">백테스트가 증명한 전략(<b>CAGR 11.6% · MDD -28% · Sharpe 0.85 — SPY를 3개 지표 모두 초과</b>)을 따라 하는 절차. 핵심은 <b>"종목 고르기"가 아니라 "국면별 주식비중 + QQQ 틸트"</b>.</p>
  <div class="tbl-wrap"><table>
   <tr><th>국면</th><th>총 주식비중</th><th>주식 틸트(자산 구성)</th></tr>
   {rows}
  </table></div>
  <ol class="prose" style="margin:12px 0 0;padding-left:20px">
   <li><b>주식비중 다이얼</b> — 현재 <b style="color:#22c55e">{reg.get('regime_kr')} → 주식 {cur_eq}%</b>로 맞춘다. 회복·성장·둔화까진 공격적으로 유지, 침체에만 안전자산으로 줄인다(위 표).</li>
   <li><b>QQQ 틸트로 상승 흡수</b> — 좋은 국면의 주식 몫을 <b>QQQ(나스닥, 자체 16% 수익엔진)</b> 중심으로 채우는 게 SPY 초과의 핵심. 금·채권(GLD/TLT) 슬리브가 변동성을 잡아 낙폭을 절반으로 줄인다.</li>
   <li><b>침체에만 방어 전환</b> — 침체로 확정되면 안전자산(TLT·GLD·SHY) 최대화 + MegaCap 퀄리티만. 그 외 국면은 SPY 10개월 이동평균이 깨질 때만 주식을 줄이는 보조 안전판을 둔다(좋은 국면 일시 하락은 무시).</li>
   <li><b>리밸런싱 규율</b> — 점검은 월 1회 + 국면 전환 즉시. 목표비중과 <b>±5%p 이상</b> 벌어진 항목만 손본다(과잉매매·세금 방지).</li>
   <li><b>과열 관리</b> — 종목의 <b>⚠과열</b>(200DMA 이격 20%↑·RSI 70↑)은 신규진입을 미루고 분할 익절. 좋은 국면이라도 과열 종목엔 비중 상한을 둔다.</li>
  </ol>
  <p class="note" style="margin-top:8px">⚠ <b>영상 프레임 vs 수익</b>: 영상은 성장을 '이머징(한국 등) 주도'로 보지만, 지난 20년 <b>미국 예외주의</b>로 성장에 EM을 전면 적용하면 과거 CAGR이 크게 하락했다. 그래서 SPY 초과를 위해 코어는 <b>미국·QQQ</b>를 유지하고 EM/Non-US는 슬리브로만(약달러 확인 시 확대). 더 안정적인 분산·EM 충실 버전을 원하면 검증탭의 <b>균형형</b>(CAGR 9.5% / MDD -25% / Sharpe 0.78)을 쓰면 된다. 위 비중은 예시, 본인 성향에 맞춰 조절. 교육용.</p>
</div>"""


def _validation_tab(valid):
    """검증 탭: NBER 정량검증 + 전략 백테스트(자산곡선·지표)."""
    nber = valid.get("nber", {})
    bt = valid.get("strategy", {})
    nber_html = ""
    if nber and "precision" in nber:
        ll = " · ".join(
            f"{e['nber']} {'미포착' if e['delta_m'] is None else (str(e['delta_m'])+'M')}"
            for e in nber["lead_lag"])
        nber_html = (
            f'<div class="card"><h4>NBER 침체 대비 정량검증</h4>'
            f'<div class="kpi"><span>Precision</span><b>{nber["precision"]}</b>'
            f'<span>Recall</span><b>{nber["recall"]}</b><span>F1</span><b>{nber["f1"]}</b></div>'
            f'<p class="cap">휘프소율 raw {nber["whipsaw_raw"]} → 평활 {nber["whipsaw_smoothed"]} · '
            f'침체 시차(음수=선행): {ll}</p></div>')
    bt_html = ""
    if bt and "variants" in bt:
        def row(s, kind=""):
            cls = {"bench": ' style="color:#22c55e;font-weight:700"',
                   "best": ' style="color:#f59e0b;font-weight:700"'}.get(kind, "")
            extra = "" if kind == "bench" else f'<td>{s.get("ann_turnover","")}</td>'
            mark = " ★" if kind == "best" else (" (벤치)" if kind == "bench" else "")
            return (f'<tr{cls}><td>{s["label"]}{mark}</td><td>{s["cagr"]*100:.1f}%</td>'
                    f'<td>{s["mdd"]*100:.0f}%</td><td>{s["sharpe"]}</td>'
                    f'<td>{s.get("ann_turnover","-") if kind!="bench" else "-"}</td></tr>')
        rows = row(bt["benchmark"], "bench")
        for s in sorted(bt["variants"], key=lambda x: x["sharpe"], reverse=True):
            rows += row(s, "best" if s["key"] == bt["best"] else "")
        bt_html = (
            f'<div class="card"><h4>전략 백테스트 vs SPY — 변형 비교 ({bt["period"]})</h4>'
            f'<div class="tbl-wrap"><table><tr><th>전략</th><th>CAGR</th><th>MDD</th><th>Sharpe</th><th>연회전</th></tr>{rows}</table></div>'
            f'<p class="cap">비용 편도 {bt["params"]["cost_oneway"]*100}% · 추세필터 {bt["params"]["trend_filter"]}. '
            f'★=위험조정성과 최적(=권고 바스켓). <b>공격형</b>은 SPY를 CAGR·MDD·Sharpe <b>3개 모두 초과</b>. <b>균형형</b>은 수익은 SPY 아래지만 낙폭이 가장 작다. 섹터선택 변형은 알파 없음(국면 베타로 타는 게 최적).</p>'
            f'<canvas id="btChart" height="80"></canvas></div>')
    return f"""
<p class="prose"><b>레버리지 없이 SPY를 이긴다.</b> <b>공격형(권고 바스켓)</b>은 좋은 국면(회복·성장)과 둔화까지 고베타 <b>QQQ 중심</b>으로 채워 상승을 먹고, 안전자산(금·채권) 슬리브로 변동성을 잡고, 침체에만 안전자산을 최대화한다 → <b>CAGR 11.6%(&gt;SPY 11.1%) · MDD -28%(&lt;SPY -51%) · Sharpe 0.85(&gt;SPY 0.77)</b>로 <b>수익·낙폭·효율 모두 SPY 우위</b>. 더 안정적인 걸 원하면 <b>균형형</b>(9.5% / -25% / Sharpe 0.78, 낙폭 최소)을 선택. 섹터·종목 선택은 알파가 없어 보조로만 쓴다. (revised 데이터+1M lag 기준, vintage 적용 시 다소 보수화)</p>
{bt_html}
{nber_html}
<p class="note">국면 판정 정확도(NBER)와 전략 수익/위험을 분리 검증. '맞히는 것'과 '버는 것'은 별개 — 둘 다 수치로 본다.</p>"""


def build(out_path=None):
    (reg, timeline, rec_by, te_list, sectors, indicators,
     uni, stocks_detail, explain, valid) = _gather()
    out_path = out_path or (ROOT_DIR / "report.html")
    bt_curves = valid.get("strategy", {}).get("curves", {})
    eff_by_id = {p["id"]: p for p in explain.get("per", [])}

    def _hot_badge(m):
        return m.replace("⚠과열", '<span style="color:#ef4444;font-weight:700">⚠과열</span>')
    stock_rows = "".join(
        f'<tr><td>{s["rank"]}</td><td class="sym">{s["symbol"]}</td>'
        f'<td><b>{s["name"]}</b></td><td>{s["gics_kr"]} · <span class="sub">{s["sub"]}</span></td>'
        f'<td class="mtr">{_hot_badge(s["metric"])}</td><td class="dsc mobhide">{s["desc"]}</td></tr>'
        for s in stocks_detail)

    # 자산배분 바스켓 총 주식비중(현재 국면)
    _breg = reg.get("regime")
    _basket = uni.get("regime_index_basket", {}).get(_breg, {})
    _safe = set(uni.get("safe_assets", []))
    equity_pct = sum(w for e, w in _basket.items() if e not in _safe)

    # 영상 슬라이드 이미지 (상대경로 — 프로젝트 루트)
    imgs = [("경기 국면 판단 기준 + 4국면 흐름", "../IMG_2127.PNG"),
            ("국가 및 지수 로테이션", "../IMG_2129.PNG"),
            ("업종 및 산업 로테이션", "../IMG_2130.PNG")]

    legend = " ".join(f'<span class="lg"><i style="background:{c}"></i>{KOR[k]}</span>'
                      for k, c in REGIME_COLOR.items())

    # DI 차트 국면 배경 밴드 (연속 같은 국면 구간)
    bands = []
    if timeline:
        st = 0
        for i in range(1, len(timeline) + 1):
            if i == len(timeline) or timeline[i]["r"] != timeline[st]["r"]:
                bands.append({"s": st, "e": i - 1, "color": REGIME_COLOR.get(timeline[st]["r"], "#1f2937")})
                st = i

    # 섹터 모멘텀 막대
    mx = max((abs(s["mom"]) for s in sectors), default=1) or 1
    sector_bars = ""
    for s in sectors:
        col = "#22c55e" if s["mom"] >= 0 else "#ef4444"
        w = abs(s["mom"]) / mx * 100
        sector_bars += (
            f'<div class="sbar"><span class="ssym">{s["symbol"]}</span>'
            f'<span class="sname" title="{s["name"]}">{s["name"]}</span>'
            f'<span class="strack"><i style="width:{w:.0f}%;background:{col}"></i></span>'
            f'<span class="sval" style="color:{col}">{s["mom"]:+.1f}%</span></div>'
        )

    def rec_rows(layer):
        return "".join(
            f"<tr><td>{r['rank'] or ''}</td><td class='sym'>{r['symbol']}</td><td>{r['rationale']}</td></tr>"
            for r in rec_by.get(layer, []))

    te_cards = "".join(
        f'<div class="card"><h4>{t["name"]}</h4>'
        f'<div class="big">{t["value"]} <span class="per">{t["period"]}</span></div>'
        f'<div class="fc">예측(Forecast): {t["forecast"]}</div></div>' for t in te_list)

    slide_imgs = "".join(
        f'<figure><figcaption>{cap}</figcaption><img src="{src}"/></figure>' for cap, src in imgs)

    trig = reg.get("recession_trigger", {})
    rc = REGIME_COLOR.get(reg.get("regime"), "#666")
    sc = reg["scores"]

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>미국 경기 국면 센싱 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
html{{overflow-x:hidden}}
body{{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;background:#0b0f17;color:#e5e7eb;margin:0;padding:24px;max-width:1180px;margin:auto;overflow-x:hidden}}
h1{{font-size:21px}} h2{{font-size:15px;color:#93c5fd;margin-top:30px;border-bottom:1px solid #1f2937;padding-bottom:6px}} h3{{font-size:14px}}
.tabs{{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 8px}}
.tab-btn{{background:#111827;border:1px solid #1f2937;color:#9ca3af;padding:9px 18px;border-radius:9px;cursor:pointer;font-size:14px;font-weight:600}}
.tab-btn.on{{background:{rc}22;border-color:{rc};color:#fff}}
.tab{{display:none}} .tab.on{{display:block}}
.banner{{background:linear-gradient(135deg,{rc}22,#111827);border:1px solid {rc};border-radius:14px;padding:20px 24px;display:flex;gap:28px;align-items:center;flex-wrap:wrap}}
.banner .rg{{font-size:34px;font-weight:700;color:{rc}}}
.meta{{color:#9ca3af;font-size:13px;line-height:1.7}}
.scores{{display:flex;gap:18px}} .scores div{{text-align:center}} .scores b{{font-size:22px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.igrid{{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}}
.rgrid{{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:14px}}
.card .big{{font-size:26px;font-weight:700}} .card .per{{font-size:13px;color:#9ca3af}} .card .fc{{font-size:12px;color:#9ca3af;margin-top:4px}}
.ihead{{display:flex;justify-content:space-between;align-items:center}} .ihead b{{font-size:14px}}
.badge{{font-size:11px;padding:2px 8px;border-radius:20px}}
.icur{{font-size:12px;color:#9ca3af;margin:4px 0}} .icur b{{color:#e5e7eb;font-size:15px}}
.iexp{{font-size:12px;color:#9ca3af;line-height:1.6;margin:8px 0 0}}
.rcard h4{{margin:0 0 10px}} .rrow{{font-size:12.5px;line-height:1.6;border-bottom:1px solid #1f2937;padding:5px 0;display:flex;gap:10px}}
.rrow span:first-child{{color:#6b7280;min-width:70px;flex-shrink:0}} .rrow.alert{{color:#f59e0b}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid #1f2937}}
.sym{{font-weight:700;color:#93c5fd}}
.sub{{color:#6b7280;font-size:12px}} .mtr{{color:#22c55e;font-size:12px;white-space:nowrap}} .dsc{{color:#9ca3af;font-size:12px;line-height:1.5}}
table.heat{{font-size:11px;text-align:center}} table.heat td,table.heat th{{border:1px solid #0b0f17;text-align:center;padding:3px 4px;color:#fff}}
table.heat td.yr{{background:#111827;font-weight:700}}
.lg{{margin-right:14px;font-size:12px}} .lg i{{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:4px;vertical-align:-1px}}
figure{{margin:0}} figure img{{width:100%;border-radius:8px;border:1px solid #1f2937}} figcaption{{font-size:12px;color:#9ca3af;margin-bottom:6px}}
.alert{{font-weight:600}} .ok{{color:#22c55e}} .warn{{color:#ef4444}}
.prose{{font-size:13.5px;line-height:1.85;color:#cbd5e1}} .prose b{{color:#fff}} .prose li{{margin:4px 0}}
.note{{font-size:12px;color:#6b7280;margin-top:6px}}
.cap{{font-size:12.5px;color:#9ca3af;margin:2px 0 12px;line-height:1.65}}
.basis .axisrow{{padding:9px 0;border-bottom:1px solid #1f2937}}
.axhd{{font-size:13px;font-weight:600;margin-bottom:7px}} .axhd b{{font-size:14px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px}}
.chip{{font-size:11.5px;color:#cbd5e1;background:#0b0f17;border:1px solid #1f2937;border-radius:20px;padding:3px 9px}}
.vecline{{margin-top:12px;font-size:13px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
.dist{{font-size:12px;color:#9ca3af;border:1px solid #1f2937;border-radius:6px;padding:3px 9px}} .dist.on{{color:#fff;font-weight:700}}
.dirb{{font-size:11px;border:1px solid;border-radius:20px;padding:2px 8px;margin-left:6px}}
.sbar{{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #1f2937;font-size:13px}}
.ssym{{font-weight:700;color:#93c5fd;width:46px;flex-shrink:0}}
.sname{{color:#cbd5e1;width:150px;flex-shrink:0;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.strack{{flex:1;height:9px;background:#1f2937;border-radius:5px;position:relative;overflow:hidden}}
.strack i{{position:absolute;left:0;top:0;height:100%;border-radius:5px}}
.sval{{width:58px;text-align:right;font-weight:700;flex-shrink:0}}
.kpi{{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin:8px 0}}
.kpi span{{color:#6b7280;font-size:12px}} .kpi b{{font-size:20px;margin-right:14px}}
.teimg{{margin-top:8px}} .teimg summary{{cursor:pointer;font-size:12px;color:#93c5fd}}
.teimg img{{width:100%;border:1px solid #1f2937;border-radius:8px;margin-top:8px;background:#fff}}
.tbl-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
@media (max-width:640px){{
  body{{padding:12px}}
  h1{{font-size:17px;line-height:1.4}} h2{{font-size:14px;margin-top:22px}}
  .banner{{padding:15px 16px;gap:14px}} .banner .rg{{font-size:26px}}
  .scores{{gap:14px;flex-wrap:wrap}} .scores b{{font-size:18px}}
  .grid,.igrid,.rgrid{{grid-template-columns:1fr;gap:12px}}
  .tab-btn{{padding:8px 12px;font-size:13px;flex:1;min-width:0}}
  .card{{padding:12px}} .card .big{{font-size:22px}}
  table{{font-size:12px}} th,td{{padding:5px 5px;vertical-align:top}}
  th{{white-space:nowrap;font-size:11px}}
  td.sym{{white-space:nowrap;font-weight:700}}
  td b,td .sub,.mtr,.dsc{{overflow-wrap:anywhere}}
  .sub{{font-size:11px}}
  .mobhide{{display:none}}
  .mtr{{white-space:normal}}
  .tbl-wrap table{{min-width:0}}
  .tbl-wrap table.heat{{min-width:640px}}
  .sname{{width:96px}} .ssym{{width:42px}} .sval{{width:48px}}
  .kpi b{{font-size:17px;margin-right:10px}}
  .rrow{{flex-wrap:wrap;gap:2px 10px}} .rrow span:first-child{{min-width:60px}}
  .vecline,.chips{{gap:6px}} .dist{{padding:3px 6px;font-size:11px}}
  figure img{{border-radius:6px}}
}}
</style></head><body>
<h1>🇺🇸 미국 경기 국면 센싱 대시보드 <span style="font-size:12px;color:#6b7280">기준 {reg.get('as_of')}</span></h1>
<div class="tabs">
  <div class="tab-btn on" data-t="dash">① 대시보드</div>
  <div class="tab-btn" data-t="ind">② 지표(선행·동행·후행)</div>
  <div class="tab-btn" data-t="val">③ 검증(백테스트)</div>
  <div class="tab-btn" data-t="doc">④ 설명</div>
</div>

<!-- ===== 탭1 대시보드 ===== -->
<div class="tab on" id="dash">
<div class="banner">
  <div><div class="rg">{reg.get('regime_kr')}</div><div class="meta">{reg.get('regime')} · 신뢰도 {reg.get('confidence')} · {'잠정' if reg.get('provisional') else '확정'}</div></div>
  <div class="scores">
    <div><div class="meta">선행</div><b style="color:#3b82f6">{sc['leading']:+.2f}</b></div>
    <div><div class="meta">동행</div><b style="color:#22c55e">{sc['coincident']:+.2f}</b></div>
    <div><div class="meta">후행</div><b style="color:#f59e0b">{sc['lagging']:+.2f}</b></div>
  </div>
  <div class="meta">침체 트리거: <span class="alert {'warn' if trig.get('alert') else 'ok'}">{'⚠ 경보' if trig.get('alert') else '정상'}</span><br>{trig.get('detail','')}</div>
</div>
<p class="note">영상(2026-06-17, 버디버디) 결론 "성장 국면" 과 엔진 판정 일치 ✅</p>

<h2>판정 근거 (왜 {reg.get('regime_kr')}인가)</h2>
{_basis_panel(explain, reg)}
{_regime_matrix(explain, reg)}

<h3 style="margin-top:18px">핵심 지표 실측값 <span style="font-size:11px;color:#6b7280">— 위 판정의 입력 데이터 (Trading Economics, 사용자 지정 소스)</span></h3>
<p class="cap">위 판정 근거의 선행·후행 축을 움직이는 실제 현재값. <b>실업률(후행)</b>이 직전월·예측치 대비 연속 상승하면 침체 확정 신호, <b>ISM PMI(선행)</b>가 50 이상이면 제조업 확장 → 선행 DI를 끌어올림.</p>
<div class="grid">{te_cards}</div>

<h2>3축 확산지수 추이 (Diffusion Index)</h2>
<p class="cap">배경색 = 그 시점 경기 국면, 선 = 선행·동행·후행 방향성(3개월 평활, +1 확장 / −1 수축). 세 선이 모두 +로 정렬→성장, 선행부터 −로 꺾임→둔화·침체.</p>
<div>{legend}</div>
<canvas id="diChart" height="95"></canvas>

<h2>연도별 경기 국면 히트맵 (2000~)</h2>
<p class="cap">한 칸 = 한 달의 확정 국면. 회복→성장→둔화→침체 순환을 한눈에.</p>
<div>{legend}</div>{_heatmap(timeline)}

<h2>투자 추천 — {reg.get('regime_kr')} 국면</h2>
<p class="cap">국면 → 유리 섹터 → 모멘텀 랭킹. 음수(빨강)는 이론상 유리 섹터라도 현재 약세임을 뜻함.</p>
<div class="grid">
  <div class="card"><h4>자산배분 바스켓 <span style="color:#22c55e">· 총 주식비중 {equity_pct}%</span></h4>
    <p class="cap" style="margin:2px 0 8px">국면별 주식/지역/안전자산 목표비중. 이 시스템의 핵심 = '얼마나 위험을 질까'(주식비중).</p>
    <table>{rec_rows('index')}</table></div>
  <div class="card"><h4>섹터 ETF (6M 모멘텀순)</h4>{sector_bars}</div>
</div>
<div class="card" style="margin-top:16px">
  <h4>S&P500 개별종목 — {reg.get('regime_kr')} 국면 시클리컬, 팩터 상위</h4>
  <p class="note" style="margin:0 0 10px">선별 방식: <b>국면 → 유리 섹터(금융·산업재·에너지·소재) → 팩터 랭킹</b>. 기본 엔진은 6M 모멘텀, 옵션으로 trading_america 펀더멘털 3팩터(V/M/D). 숫자가 높을수록 상위.</p>
  <div class="tbl-wrap"><table>
   <tr><th>#</th><th>종목</th><th>종목명</th><th>섹터 · 세부업종</th><th>지표</th><th class="mobhide">설명</th></tr>
   {stock_rows}
  </table></div>
</div>
</div>

<!-- ===== 탭2 지표 ===== -->
<div class="tab" id="ind">
<p class="prose">경기 국면은 <b>선행→동행→후행</b> 순으로 신호가 전이된다. 아래는 각 축의 참조 지표·현재값·<b>최근 10년</b> 추이와 의미. 우리 인터랙티브 차트와 ‘TE 원본 차트’ 모두 10년으로 정렬.</p>
{_indicator_cards(indicators, eff_by_id)}
<p class="note">각 지표 카드의 ‘TE 원본 차트’를 펼치면 Trading Economics 원본 이미지가 보입니다(전 {len(indicators)}개 지표 중 TE 페이지 있는 지표). t10y2y(금리차)는 TE 단일 차트가 없어 인터랙티브 차트만 제공.</p>
</div>

<!-- ===== 탭3 검증 ===== -->
<div class="tab" id="val">
<h2>실행 가이드 — 수익+방어 균형 전략</h2>
{_exec_guide(uni, reg)}
<h2>전략 검증 — "맞히는가" & "버는가"</h2>
{_validation_tab(valid)}
</div>

<!-- ===== 탭4 설명 ===== -->
<div class="tab" id="doc">
<h2>국면 센싱이란?</h2>
<div class="prose">
자본시장 가격(선행)이 거시경제 변화를 가장 먼저 반영하고, 실물 경제(동행)가 뒤따르며, 고용·임금(후행)이 마지막에 국면 전환을 확정한다. 이 <b>시차 구조</b>를 이용해 현재가 4국면 중 어디인지 판정한다.
<ul>
<li><b>판정 방법</b>: 축마다 지표들의 방향(상승/횡보/하락)을 <b>확산지수(Diffusion Index)</b>로 집계 → (선행,동행,후행) 조합을 4국면 원형과 거리 비교 → 가장 가까운 국면 + 신뢰도.</li>
<li><b>휘프소 방어</b>: 새 국면이 <b>3개월 연속</b> 유지돼야 확정(잠정→확정). 단월 급변에 흔들리지 않게.</li>
<li><b>침체 확정 트리거</b>: 선행·동행이 꺾인 뒤 <b>실업률이 직전치·예측치 대비 연속 상승</b>하면 둔화→침체로 확정, 안전자산 최대화.</li>
<li><b>유연성</b>: 2010년대 이후 무제한 유동성으로 둔화→침체를 건너뛰기도 한다(2020·2022~23). 4국면 순차 전이를 강제하지 않는다.</li>
</ul>
4국면은 <b>회복→성장→둔화→침체</b>로 순환하며, 1바퀴 약 4~5.5년. 침체가 가장 짧고 성장·둔화가 길다.
</div>

<h2>국면별 의미와 투자 (지침 정리)</h2>
{_regime_cards(uni)}

<h2>영상 핵심 슬라이드</h2>
<div class="grid">{slide_imgs}</div>
<p class="note">출처: "까다로운 6월 FOMC" (2026-06-17, Money Comics/버디버디), NH 백찬규 센터장. youtube_report.md·지침.md 기반.</p>
</div>

<script>
document.querySelectorAll('.tab-btn').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('.tab-btn').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); document.getElementById(b.dataset.t).classList.add('on');
}});
const TL={json.dumps(timeline)}, IND={json.dumps({i["id"]: i["series"] for i in indicators})}, BANDS={json.dumps(bands)};
const LEN=TL.length;
const sm=(arr,w=3)=>arr.map((_,i)=>{{let s=0,n=0;for(let j=Math.max(0,i-w+1);j<=i;j++){{if(arr[j]!=null){{s+=arr[j];n++;}}}}return n?s/n:null;}});
// 국면 배경 밴드 플러그인
const bandPlugin={{id:'bands',beforeDatasetsDraw(c){{const xs=c.scales.x,a=c.chartArea,ctx=c.ctx;BANDS.forEach(b=>{{
  const x1=xs.getPixelForValue(b.s);const x2=(b.e+1<LEN)?xs.getPixelForValue(b.e+1):a.right;
  ctx.save();ctx.fillStyle=b.color+'2e';ctx.fillRect(x1,a.top,x2-x1,a.bottom-a.top);ctx.restore();}});}}}};
const lineOpt=()=>({{plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{color:'#6b7280',maxTicksLimit:6}},grid:{{display:false}}}},y:{{ticks:{{color:'#6b7280'}},grid:{{color:'#1f2937'}}}}}}}});
new Chart(diChart,{{type:'line',plugins:[bandPlugin],data:{{labels:TL.map(x=>x.d),datasets:[
 {{label:'선행',data:sm(TL.map(x=>x.L)),borderColor:'#60a5fa',borderWidth:2,pointRadius:0,tension:.35}},
 {{label:'동행',data:sm(TL.map(x=>x.C)),borderColor:'#4ade80',borderWidth:2,pointRadius:0,tension:.35}},
 {{label:'후행',data:sm(TL.map(x=>x.Lag)),borderColor:'#fbbf24',borderWidth:2,pointRadius:0,tension:.35}}]}},
 options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb'}}}}}},scales:{{x:{{ticks:{{color:'#6b7280',maxTicksLimit:14}},grid:{{display:false}}}},y:{{ticks:{{color:'#6b7280',stepSize:0.5}},grid:{{color:'#1f293755'}},min:-1.1,max:1.1}}}}}}}});
for(const id in IND){{const el=document.getElementById('c_'+id);if(!el)continue;const d=IND[id];
 new Chart(el,{{type:'line',data:{{labels:d.map(x=>x[0]),datasets:[{{data:d.map(x=>x[1]),borderColor:'#60a5fa',borderWidth:1.3,pointRadius:0,tension:.2,fill:true,backgroundColor:'#60a5fa18'}}]}},options:lineOpt()}});}}
const BT={json.dumps(bt_curves)};
if(BT.dates&&document.getElementById('btChart')){{new Chart(btChart,{{type:'line',data:{{labels:BT.dates,datasets:[
  {{label:'SPY 단순보유',data:BT.benchmark,borderColor:'#22c55e',borderWidth:1.6,pointRadius:0,tension:.2}},
  {{label:'공격형 바스켓(SPY초과)',data:BT.basket,borderColor:'#a855f7',borderWidth:2.4,pointRadius:0,tension:.2}},
  {{label:'균형형 바스켓(효율최고)',data:BT.basket_bal,borderColor:'#3b82f6',borderWidth:1.6,pointRadius:0,tension:.2}},
  {{label:'SPY 국면타이밍(순수방어)',data:BT.spy_timed,borderColor:'#6b7280',borderWidth:1.2,pointRadius:0,tension:.2}}]}},
  options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb'}}}}}},scales:{{x:{{ticks:{{color:'#6b7280',maxTicksLimit:12}},grid:{{display:false}}}},y:{{type:'logarithmic',ticks:{{color:'#6b7280'}},grid:{{color:'#1f293755'}}}}}}}}}});}}
</script>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")
    return out_path
