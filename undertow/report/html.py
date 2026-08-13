"""HTML 报告组装（渲染层，零依赖）。

把 outlook 综合研判 + 三张 SVG 图，拼成一个【自包含 HTML 文件】：
浏览器双击即看，GitHub 也能渲染。只用内联 CSS/SVG，不引任何前端库。
"""
from __future__ import annotations

from undertow.analyze.outlook import Outlook
from undertow.core.calendar import CATEGORY_LABEL

_BIAS_COLOR = {
    "偏多": "#1a7f37", "偏多(弱)": "#3fb950",
    "偏空": "#b62324", "偏空(弱)": "#e5534b",
    "中性": "#6e7781", "分歧(双向)": "#bf8700",
}
_KIND_COLOR = {
    "resistance": "#2ca02c", "support": "#d62728", "flip": "#9467bd",
    "resistance2": "#74b874", "support2": "#e08a8b",  # 次墙：主墙色的柔和版
    "pin": "#8a6d3b", "flow": "#1f77b4",
}
_VOTE_COLOR = {"看多": "#1a7f37", "看空": "#b62324", "中性": "#6e7781"}
_FLOW_COLOR = {"bearish": "#b62324", "bullish": "#1a7f37", "neutral": "#6e7781"}

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f6f8fa;color:#24292f;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 60px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:16px 18px;margin:14px 0;
 box-shadow:0 1px 2px rgba(27,31,36,.04)}
h1{font-size:21px;margin:0 0 4px} h2{font-size:15px;margin:2px 0 10px;color:#57606a}
.sub{color:#57606a;font-size:13px;margin:2px 0}
.badge{display:inline-block;color:#fff;border-radius:999px;padding:3px 12px;font-weight:700;font-size:14px}
.pill{display:inline-block;background:#eaeef2;border-radius:6px;padding:1px 7px;font-size:12px;margin-left:6px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
th,td{border-bottom:1px solid #eaeef2;padding:6px 8px;text-align:left;vertical-align:top}
th{color:#57606a;font-weight:600;background:#f6f8fa}
td.r,th.r{text-align:right;white-space:nowrap}
.lvl{font-weight:700}
.scn{border-left:4px solid #d0d7de;padding:6px 12px;margin:10px 0;background:#f6f8fa;border-radius:0 8px 8px 0}
.scn b{font-size:13px}
.scn .t{color:#57606a;font-size:12.5px;margin:2px 0}
.warn{background:#fff8c5;border:1px solid #d4a72c66;border-radius:8px;padding:10px 14px;font-size:12.5px}
.warn li{margin:3px 0}
.chart{overflow-x:auto;margin:8px 0}
.foot{color:#8b949e;font-size:11.5px;margin-top:18px;text-align:center}
small{color:#8b949e}
"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _bias_badge(o: Outlook) -> str:
    color = _BIAS_COLOR.get(o.bias, "#6e7781")
    return (f'<span class="badge" style="background:{color}">{_esc(o.bias)}</span>'
            f'<span class="pill">可信度 {_esc(o.confidence)}</span>'
            f'<span class="pill">综合分 {o.bias_score:+.1f}</span>')


def _votes_table(o: Outlook) -> str:
    rows = []
    for v in o.votes:
        c = _VOTE_COLOR.get(v.direction, "#6e7781")
        rows.append(
            f"<tr><td>{_esc(v.layer)}</td><td>{_esc(v.factor)}</td>"
            f'<td style="color:{c};font-weight:600">{_esc(v.direction)}</td>'
            f'<td class="r">{v.weight:.2f}</td><td>{_esc(v.reliability)}</td>'
            f"<td><small>{_esc(v.detail)}</small></td></tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6"><small>当前无触发的方向性因子。</small></td></tr>')
    return ("<table><tr><th>层</th><th>因子</th><th>方向</th><th class='r'>权重</th>"
            "<th>可信度</th><th>依据</th></tr>" + "".join(rows) + "</table>")


def _levels_table(o: Outlook) -> str:
    rows = []
    for k in o.key_levels:
        c = _KIND_COLOR.get(k.kind, "#24292f")
        com = f"{k.commodity_level:,.0f}" if k.commodity_level is not None else "—"
        rows.append(
            f'<tr><td class="lvl" style="color:{c}">{_esc(k.label)}</td>'
            f'<td class="r">{k.etf_level:.1f}</td><td class="r">{com}</td>'
            f"<td><small>{_esc(k.note)}</small></td></tr>"
        )
    return ("<table><tr><th>关键位</th><th class='r'>ETF价</th><th class='r'>≈商品</th>"
            "<th>说明</th></tr>" + "".join(rows) + "</table>")


def _scenarios_html(o: Outlook) -> str:
    out = []
    for i, sc in enumerate(o.scenarios):
        tag = " （与综合倾向一致）" if i == 0 else ""
        out.append(
            f'<div class="scn"><b>{_esc(sc.name)}</b>{tag}'
            f'<div class="t">▸ 触发：{_esc(sc.trigger)}</div>'
            f'<div class="t">▸ 演化：{_esc(sc.path)}</div>'
            f'<div class="t">▸ 失效：{_esc(sc.invalidation)}</div></div>'
        )
    return "".join(out)


def _caveats_html(o: Outlook) -> str:
    items = "".join(f"<li>{_esc(c)}</li>" for c in o.caveats)
    return f'<div class="warn"><b>⚠️ 用法与局限</b><ul>{items}</ul></div>'


def _flow_kind_table_html(changes, kind: str) -> str:
    items = sorted([c for c in changes if c.kind == kind], key=lambda x: -abs(x.d_oi))[:14]
    if not items:
        return ""
    side = "Put（下方保护/支撑）" if kind == "P" else "Call（上方压制/突破）"
    rows = []
    for c in sorted(items, key=lambda x: x.strike):
        col = _FLOW_COLOR.get(c.bias, "#6e7781")
        wall = f' <span style="color:#9467bd">🧱{_esc(c.on_wall)}</span>' if c.on_wall else ""
        adj = f"{c.adj_iv_pp:+.2f}pp" if c.prev_iv > 0 else "—"
        sp = f' <span style="color:#9467bd;font-weight:400">⟂{_esc(c.spread_note)}</span>' if c.spread_note else ""
        rows.append(
            f'<tr><td class="r lvl">{c.strike:.1f}{wall}</td>'
            f'<td class="r" style="color:{col}">{c.d_oi:+,}</td>'
            f'<td class="r">{c.curr_oi:,}</td><td class="r">{c.delta:+.3f}</td>'
            f'<td class="r">{_esc(adj)}</td>'
            f'<td style="color:{col};font-weight:600">{_esc(c.judgment)}{sp}</td></tr>'
        )
    return (f'<div style="font-weight:600;margin:10px 0 2px">{side}</div>'
            "<table><tr><th class='r'>行权价</th><th class='r'>ΔOI</th><th class='r'>当前OI</th>"
            "<th class='r'>精确Delta</th><th class='r'>Delta修正ΔIV</th><th>判断</th></tr>"
            + "".join(rows) + "</table>")


def _unusual_table_html(fa) -> str:
    if not fa.unusual:
        return "<small>近月、现价附近无明显异常活跃。</small>"
    rows = []
    for u in fa.unusual[:12]:
        cp = "P" if u.kind == "P" else "C"
        ratio = "∞" if u.vol_oi_ratio == float("inf") else f"{u.vol_oi_ratio:.1f}x"
        rows.append(
            f'<tr><td class="r">{u.strike:.1f}</td><td>{cp}</td>'
            f'<td class="r">{u.open_interest:,}</td><td class="r">{u.volume:,}</td>'
            f'<td class="r">{_esc(ratio)}</td><td class="r">{u.delta:+.3f}</td>'
            f'<td class="r">{u.iv*100:.0f}%</td></tr>'
        )
    return ("<table><tr><th class='r'>行权价</th><th>C/P</th><th class='r'>OI</th>"
            "<th class='r'>今成交</th><th class='r'>量/OI</th><th class='r'>Delta</th>"
            "<th class='r'>IV</th></tr>" + "".join(rows) + "</table>")


def _vol_surface_html(v) -> str:
    """波动率面小块：ATM IV / 25Δ·10Δ 偏斜的日变化 + 买方确认判读（作者口径）。"""
    if v is None:
        return ""
    head = (f'<div style="font-weight:600;margin:10px 0 2px">波动率面'
            f'（到期 {v.curr.expiry}，T-{v.curr.days_out}）——期权端是否确认价格</div>')
    if v.prev is None:
        return (head + f'<div class="sub">当日水平：ATM IV {v.curr.atm_iv_pp:.1f} · '
                f'25Δ skew {v.curr.skew25_pp:+.2f}pp · 10Δ {v.curr.skew10_pp:+.2f}pp'
                f'（{_esc(v.verdict)}）</div>')
    atm_col = "#c62828" if v.d_atm_pp <= -0.3 else ("#2e7d32" if v.d_atm_pp >= 0.3 else "#6e7781")
    rows = (
        f"<tr><td>现价变动</td><td class='r'>{v.d_spot_pct:+.2f}%</td><td></td></tr>"
        f"<tr><td>ATM IV</td><td class='r'>{v.prev.atm_iv_pp:.1f} → {v.curr.atm_iv_pp:.1f}</td>"
        f"<td class='r' style='color:{atm_col};font-weight:600'>{v.d_atm_pp:+.2f}pp</td></tr>"
        f"<tr><td>25Δ Put-Call skew</td><td class='r'>{v.prev.skew25_pp:+.2f} → {v.curr.skew25_pp:+.2f}pp</td>"
        f"<td class='r'>{v.d_skew25_pp:+.2f}</td></tr>"
        f"<tr><td>10Δ Put-Call skew</td><td class='r'>{v.prev.skew10_pp:+.2f} → {v.curr.skew10_pp:+.2f}pp</td>"
        f"<td class='r'>{v.d_skew10_pp:+.2f}</td></tr>")
    return (head
            + "<table><tr><th>指标</th><th class='r'>昨日 → 今日</th><th class='r'>Δ</th></tr>"
            + rows + "</table>"
            + f'<div class="sub" style="margin-top:4px"><b>判读：{_esc(v.verdict)}</b></div>'
            + '<small>事件日（非农/CPI/FOMC 兑现后）IV 回落含事件溢价释放的机械成分，判读要打折；'
              '偏斜是否收敛比 ATM IV 单独一条更干净。</small>')


def render_vol_regime_section(vr) -> str:
    """波动率环境卡片：期权偏贵/偏便宜 → 波段级"买方 vs 卖方"倾向。"""
    if vr is None or not getattr(vr, "has_content", False):
        return ""
    palette = {"偏卖方": "#8250df", "偏买方": "#0969da", "中性": "#6e7781"}
    col = palette.get(vr.stance, "#6e7781")
    badge = (f'<span style="display:inline-block;padding:3px 12px;border-radius:12px;'
             f'background:{col};color:#fff;font-weight:600">倾向：{_esc(vr.stance)}</span>')
    # 数字行
    cells = []
    if vr.iv_index_name and vr.iv_index_latest is not None:
        pct = f"，近1年分位 {vr.iv_pct:.0f}%" if vr.iv_pct is not None else ""
        chg = f"，20日 {vr.iv_chg_20d:+.1f}pp" if vr.iv_chg_20d is not None else ""
        cells.append(f"{_esc(vr.iv_index_name)} <b>{vr.iv_index_latest:.1f}</b>{pct}{chg}")
    if vr.atm_iv_pp is not None:
        cells.append(f"近月 ATM IV <b>{vr.atm_iv_pp:.1f}</b>")
    if vr.rv_pp is not None:
        cells.append(f"近20日实际波动 RV <b>{vr.rv_pp:.1f}</b>")
    if vr.iv_minus_rv is not None:
        dcol = "#8250df" if vr.iv_minus_rv >= 2 else ("#0969da" if vr.iv_minus_rv <= -2 else "#6e7781")
        cells.append(f'IV−RV <b style="color:{dcol}">{vr.iv_minus_rv:+.1f}pp</b>')
    nums = f'<div class="sub" style="margin-top:8px">' + " · ".join(cells) + "</div>"
    reasons = "".join(f"<li>{_esc(r)}</li>" for r in vr.reasons)
    reasons_html = f"<ul style='margin:8px 0'>{reasons}</ul>" if reasons else ""
    caveats = "".join(f"<li>{_esc(c)}</li>" for c in vr.caveats)
    caveats_html = (f'<div class="sub" style="margin-top:4px"><small><ul>{caveats}</ul></small></div>'
                    if caveats else "")
    edu = ('<small>买方=买入期权：看对方向或波动放大才赚，怕横盘 / IV 回落；'
           '卖方=卖出期权：收权利金、赌横盘 / IV 回落，怕突发大行情。'
           '判据：IV 分位（相对自身历史贵不贵）+ ATM IV−RV（相对标的实际波动贵不贵）。</small>')
    return (f'<div class="card"><h2>波动率环境 · 期权买方还是卖方</h2>'
            f'<div style="margin:4px 0 2px">{badge}</div>{nums}{reasons_html}{caveats_html}{edu}</div>')


def render_vol_analysis_section(vr, vol_svg: str = "") -> str:
    """波动率速览卡：聚焦最近这段时间的波动率水平 + 近1年走势曲线。

    上半：现值 / 近1年分位 / 近20日趋势 / 当前 IV−RV（数据到最新交易日）。
    下半：波动率指数近1年曲线 + 近段均值参照。
    （VRP 卖方溢价的「穿越牛熊」属长周期分析，不在每日报告展开；数据落盘 data/history/vrp/，
     随时可用 `undertow vol` 查看。）
    """
    has_now = vr is not None and getattr(vr, "has_content", False)
    if not has_now and not vol_svg:
        return ""
    parts = ['<div class="card"><h2>波动率速览 · 最近水平</h2>']

    # —— 上半：最近波动率水平（数据到最新）——
    if has_now:
        if vr.iv_pct is not None:
            if vr.iv_pct >= 70:
                lvl, lc = "偏高", "#c62828"
            elif vr.iv_pct <= 30:
                lvl, lc = "偏低", "#2e7d32"
            else:
                lvl, lc = "中位", "#6e7781"
            badge = (f'<span style="display:inline-block;padding:3px 12px;border-radius:12px;'
                     f'background:{lc};color:#fff;font-weight:600">最近波动率：{lvl}</span>')
        else:
            badge = ""
        cells = []
        if vr.iv_index_name and vr.iv_index_latest is not None:
            cells.append(f"{_esc(vr.iv_index_name)} 现值 <b>{vr.iv_index_latest:.1f}</b>")
        if vr.iv_pct is not None:
            cells.append(f"近1年分位 <b>{vr.iv_pct:.0f}%</b>")
        if vr.iv_chg_20d is not None:
            if vr.iv_chg_20d >= 1:
                tr = f"近20日抬升 <b>{vr.iv_chg_20d:+.1f}pp</b>（扩张）"
            elif vr.iv_chg_20d <= -1:
                tr = f"近20日回落 <b>{vr.iv_chg_20d:+.1f}pp</b>（收敛）"
            else:
                tr = f"近20日走平 <b>{vr.iv_chg_20d:+.1f}pp</b>"
            cells.append(tr)
        if vr.iv_minus_rv is not None:
            dcol = "#8250df" if vr.iv_minus_rv >= 2 else ("#0969da" if vr.iv_minus_rv <= -2 else "#6e7781")
            cells.append(f'当前 IV−RV <b style="color:{dcol}">{vr.iv_minus_rv:+.1f}pp</b>'
                         f'（正=期权贵于近期实际波动）')
        nums = f'<div class="sub" style="margin-top:8px">' + " · ".join(cells) + "</div>" if cells else ""
        parts.append(f'<div style="margin:4px 0 2px">{badge}</div>{nums}')

    # —— 波动率指数近1年曲线 ——
    if vol_svg:
        parts.append(f'<div class="chart">{vol_svg}</div>')

    parts.append(
        '<small>IV 现值/近1年分位/近20日趋势 = 最近波动率相对自身历史贵不贵、在扩张还是收敛；'
        'IV−RV = 期权相对标的近期实际波动的溢价（正=偏贵）。曲线为波动率指数近1年走势，'
        '虚线为近段均值——现值在均值之上即近期偏贵。事件日（CPI/非农/FOMC 兑现）IV 回落含事件溢价'
        '机械释放，判读打折。</small>')
    parts.append('</div>')
    return "".join(parts)


def render_flow_section(fa) -> str:
    """期权资金流买卖方卡片。两份快照→买卖方表；仅一份→单快照异常活跃。"""
    if fa is None:
        return ""
    head = '<h2>期权资金流 / 持仓异动（买方 vs 卖方）</h2>'
    tilt = f'<div class="sub">净倾向：<b>{_esc(fa.flow_tilt)}</b></div>'
    if fa.prev_date and fa.changes:
        spread_html = ""
        if fa.spreads:
            items = "".join(f"<li><b>{_esc(s.name)}</b>：{_esc(s.detail)}</li>" for s in fa.spreads)
            spread_html = ('<div class="warn" style="margin:8px 0"><b>⚠️ 检测到疑似价差结构'
                           '（已从方向压力中扣除"封顶/保护腿"，避免误读为方向）</b>'
                           f'<ul>{items}</ul></div>')
        body = (f'<div class="sub">对比 {_esc(fa.prev_date)} → {_esc(fa.curr_date)} · '
                'OI增+IV升=买方抬价 / OI增+IV降=卖方写权</div>'
                + _vol_surface_html(fa.vol)
                + spread_html
                + _flow_kind_table_html(fa.changes, "P")
                + _flow_kind_table_html(fa.changes, "C"))
    else:
        note = ("仅一份快照，ΔOI/ΔIV 的买卖方判定需要明天第二份快照才点亮。下方为今日单快照"
                "异常活跃（量/OI 高 = 多为当日新建仓，是 ΔOI 异动的先兆）："
                if not fa.prev_date else "近月无超阈值异动；下方为今日异常活跃：")
        body = (f'<div class="warn" style="margin-bottom:8px">{_esc(note)}</div>'
                + _vol_surface_html(fa.vol) + _unusual_table_html(fa))
    return f'<div class="card">{head}{tilt}{body}</div>'


def render_macro_section(ma) -> str:
    """宏观背景卡片（实际利率/美元/通胀预期 → 金银利多/利空）。"""
    if ma is None or not ma.drivers:
        return ""
    color = _BIAS_COLOR.get(ma.macro_bias, "#6e7781")
    rows = []
    for d in ma.drivers:
        c = _VOTE_COLOR.get("看多" if d.vote_sign > 0 else ("看空" if d.vote_sign < 0 else "中性"), "#6e7781")
        lean = "利多" if d.vote_sign > 0 else ("利空" if d.vote_sign < 0 else "中性")
        chg = f"{d.chg_20d:+.2f}"
        rows.append(
            f'<tr><td>{_esc(d.name)}</td><td class="r">{d.latest:.2f}{_esc(d.unit)}</td>'
            f'<td class="r">{_esc(chg)}</td>'
            f'<td style="color:{c};font-weight:600">{_esc(lean)}</td>'
            f'<td>{_esc(d.reliability)}</td></tr>'
        )
    vol_html = ""
    if ma.vol is not None:
        v = ma.vol
        vol_html = (f'<div class="sub" style="margin-top:8px">波动率 <b>{_esc(v.name)} {v.latest:.1f}</b>'
                    f'（近20日 {v.chg_20d:+.1f}，1年分位 {v.percentile_1y:.0f}%）— {_esc(v.note)}</div>')
    return (
        '<div class="card"><h2>宏观背景（基本面驱动 · FRED + CBOE 波动率）</h2>'
        f'<div class="sub">宏观倾向 <b style="color:{color}">{_esc(ma.macro_bias)}</b>'
        f'（分 {ma.macro_score:+.1f}）· 数据 {_esc(ma.asof)}</div>'
        "<table><tr><th>指标</th><th class='r'>最新</th><th class='r'>近20日Δ</th>"
        "<th>对金银</th><th>可信度</th></tr>" + "".join(rows) + "</table>"
        f'{vol_html}'
        '<div class="sub" style="margin-top:6px">实际利率↓/美元↓ → 利多金银；波动率高位=区间放大、'
        '追单谨慎。宏观为背景维度，与持仓·期权微观结构共振时才加重。</div></div>'
    )


_CAT_COLOR = {"fed": "#8250df", "data": "#bf3989", "cot": "#0969da", "opex": "#bc4c00", "other": "#57606a"}


def render_events_section(events, today) -> str:
    """事件雷达卡片：未来关键节点（FOMC/数据/COT/到期），临近催化剂主动降置信。"""
    if not events:
        return ""
    rows = []
    has_ff = False
    for e in events:
        tm = e.tminus(today)
        urge = "#cf222e" if (e.days_until(today) <= 2 and e.importance == "high") else "#57606a"
        cat = CATEGORY_LABEL.get(e.category, e.category)
        cat_c = _CAT_COLOR.get(e.category, "#57606a")
        when = f"{e.date.isoformat()}" + (f" {e.time_et} ET" if e.time_et and e.time_et != "—" else "")
        scope = " ".join(e.instruments) if e.instruments else "全局"
        tag = ' <small>(FF)</small>' if e.source == "ff" else ""
        if e.source == "ff":
            has_ff = True
        cons = e.consensus()
        cons_html = f'<div class="t" style="color:#0969da">{_esc(cons)}</div>' if cons else ""
        note = f'<div class="t">{_esc(e.note)}</div>' if e.note else ""
        rows.append(
            f'<tr><td class="r" style="font-weight:700;color:{urge}">{e.mark} {tm}</td>'
            f'<td>{_esc(when)}</td>'
            f'<td><span class="pill" style="background:{cat_c}1a;color:{cat_c}">{_esc(cat)}</span> '
            f'{_esc(e.name)}{tag}{cons_html}{note}</td>'
            f'<td><small>{_esc(scope)}</small></td></tr>'
        )
    ff_note = ('（标 <small>(FF)</small> 含预测/前值，来自 ForexFactory/FairEconomy 公开日历 feed）'
               if has_ff else "")
    return (
        '<div class="card"><h2>事件雷达（美东日历 · 临近催化剂请降置信）</h2>'
        '<table><tr><th class="r">倒计时</th><th>时点</th><th>事件</th><th>影响</th></tr>'
        + "".join(rows) + "</table>"
        f'<div class="sub" style="margin-top:6px">🔴高 / 🟡中影响。{ff_note}FOMC·CPI·非农前后跳空风险大，'
        '期权到期(OPEX)前 Gamma/OI 墙会失真——事件窗口内的位点研判仅供风险预案，勿当点位预言。</div></div>'
    )


def render_tldr_section(blocks: list[tuple[str, str]]) -> str:
    """大白话速读卡片：分块摘要（方向/关键位/持仓异动/对手盘警示），
    对手盘警示块标红——它是与研判方向相反的最强证据，读报先看反方。"""
    if not blocks:
        return ""
    parts = []
    for title, text in blocks:
        warn = "对手盘" in title
        tag = (f'<b style="color:#c62828">【{_esc(title)}】</b>' if warn
               else f'<b>【{_esc(title)}】</b>')
        style = "margin:7px 0;font-size:15px;line-height:1.85"
        if warn:
            style += ";background:#fdecea;border-left:3px solid #c62828;padding:6px 10px;border-radius:4px"
        parts.append(f'<p style="{style}">{tag}{_esc(text)}</p>')
    return ('<div class="card"><h2>大白话速读</h2>' + "".join(parts) +
            '<small>由关键位与当日数据自动拼句；措辞保留不确定性，非点位预言。</small></div>')


_STATUS_COL = {"待命": "#6e7781", "未触发": "#6e7781", "触发观察中": "#9a6700",
               "结构条件已满足": "#0969da", "情景作废": "#c62828"}
_STATUS_PREFIX_COL = (("追认成立", "#0969da"), ("破位日", "#9a6700"),
                      ("已破位", "#9a6700"), ("突破测试中", "#9a6700"),
                      ("破位测试中", "#9a6700"))


def _status_col(status: str) -> str:
    if status in _STATUS_COL:
        return _STATUS_COL[status]
    for pre, col in _STATUS_PREFIX_COL:
        if status.startswith(pre):
            return col
    return "#6e7781"


def _label_w_pct(text: str) -> float:
    """标签宽度估算（占容器 %）：10px 字体，CJK≈10px/字、ASCII≈6px，容器按 ~660px 估。"""
    px = sum(10 if ord(c) > 0x2E80 else 6 for c in text) + 6
    return 100.0 * px / 660.0


def _price_rail(s, spot: float, fmt) -> str:
    """情景价格轨道：止损(红)/入场区(蓝带)/现价(黑点)/止盈(绿)。

    所有点标签统一放轨道下方，贪心分行避让：同行相邻标签若估算宽度重叠，
    自动落到下一行——保证文字永不重叠（行高 13px，容器高度随行数伸缩）。
    """
    pts = [s.invalidation, s.entry_ref, spot] + [v for v, _ in s.targets]
    if s.entry_lo is not None:
        pts += [s.entry_lo, s.entry_hi]
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    lo -= span * 0.08
    hi += span * 0.08
    pos = lambda v: 100.0 * (v - lo) / (hi - lo)

    RAIL_Y = 22          # 轨道纵坐标
    el: list[str] = []
    marks: list[tuple[float, str, str, bool]] = []   # (pos%, 文本, 颜色, 加粗)

    # 入场区蓝带（带内文字单独放最上行，不参与下方避让）
    if s.entry_lo is not None:
        l, r = pos(s.entry_lo), pos(s.entry_hi)
        el.append(f'<div style="position:absolute;left:{l:.1f}%;width:{max(r - l, 0.8):.1f}%;'
                  f'top:{RAIL_Y - 6}px;height:14px;background:#0969da26;'
                  'border:1px solid #0969da55;border-radius:3px"></div>')
        c = (l + r) / 2
        w = _label_w_pct(f"入场区 {fmt(s.entry_lo)}–{fmt(s.entry_hi)}")
        c = min(max(c, w / 2), 100 - w / 2)
        el.append(f'<div style="position:absolute;left:{c:.1f}%;top:0;'
                  'transform:translateX(-50%);font-size:10px;color:#0969da;white-space:nowrap">'
                  f'入场区 {fmt(s.entry_lo)}–{fmt(s.entry_hi)}</div>')
    else:
        marks.append((pos(s.entry_ref), f"触发 {fmt(s.entry_ref)}", "#0969da", True))
        el.append(f'<div style="position:absolute;left:{pos(s.entry_ref):.1f}%;'
                  f'top:{RAIL_Y - 8}px;height:18px;width:2px;background:#0969da;'
                  'transform:translateX(-50%)"></div>')

    marks.append((pos(s.invalidation), f"止损 {fmt(s.invalidation)}", "#c62828", True))
    el.append(f'<div style="position:absolute;left:{pos(s.invalidation):.1f}%;'
              f'top:{RAIL_Y - 8}px;height:18px;width:2px;background:#c62828;'
              'transform:translateX(-50%)"></div>')
    for i, (v, _lbl) in enumerate(s.targets[:2], 1):
        marks.append((pos(v), f"止盈{i} {fmt(v)}", "#2e7d32", True))
        el.append(f'<div style="position:absolute;left:{pos(v):.1f}%;'
                  f'top:{RAIL_Y - 8}px;height:18px;width:2px;background:#2e7d32;'
                  'transform:translateX(-50%)"></div>')
    marks.append((pos(spot), f"现价 {fmt(spot)}", "#24292f", False))
    el.append(f'<div style="position:absolute;left:{pos(spot):.1f}%;top:{RAIL_Y - 2}px;'
              'width:7px;height:7px;background:#24292f;border-radius:50%;'
              'transform:translateX(-50%)"></div>')

    # 贪心分行：按 x 排序，放不进第一行（与该行上一标签重叠）就落下一行
    rows_end: list[float] = []
    label_y0 = RAIL_Y + 14
    for p, text, color, bold in sorted(marks, key=lambda m: m[0]):
        w = _label_w_pct(text)
        c = min(max(p, w / 2), 100 - w / 2)   # 贴边标签夹回容器内
        start = c - w / 2
        row = 0
        while row < len(rows_end) and rows_end[row] > start - 1.0:
            row += 1
        if row == len(rows_end):
            rows_end.append(0.0)
        rows_end[row] = c + w / 2
        weight = ";font-weight:600" if bold else ""
        el.append(f'<div style="position:absolute;left:{c:.1f}%;top:{label_y0 + row * 13}px;'
                  f'transform:translateX(-50%);font-size:10px;color:{color}{weight};'
                  f'white-space:nowrap">{_esc(text)}</div>')

    height = label_y0 + len(rows_end) * 13 + 2
    return (f'<div style="position:relative;height:{height}px;margin:6px 2px 2px">'
            f'<div style="position:absolute;left:0;right:0;top:{RAIL_Y}px;height:2px;'
            'background:#d0d7de"></div>' + "".join(el) + "</div>")


def render_strategy_section(sp, timeline_svg: str = "") -> str:
    """策略情景参数化卡片（期货）：裁决横幅 → 否决票 → 每个情景一张"交易票"
    子卡（状态/触发/价格轨道/止盈止损方案）。模块输出，非交易指令。"""
    if sp is None:
        return ""
    fmt = (lambda v: f"{v:,.0f}") if sp.spot >= 500 else (lambda v: f"{v:,.1f}")
    dir_col = {"做空": "#c62828", "做多": "#2e7d32", "观望": "#6e7781"}.get(sp.direction, "#6e7781")
    head = (f'<h2>策略情景参数化（期货 · 模块输出，非交易指令）</h2>'
            f'<div style="margin:6px 0"><span class="pill" '
            f'style="background:{dir_col}1a;color:{dir_col};font-weight:700">方向：{_esc(sp.direction)}</span>'
            f' <small>{_esc(sp.direction_source)}</small></div>')

    # 裁决横幅置顶：先给结论，再看细节
    v_col = "#c62828" if ("不开枪" in sp.verdict or "无有效" in sp.verdict) else (
        "#0969da" if "信号在位" in sp.verdict else "#6e7781")
    verdict = (f'<div style="margin:8px 0;padding:8px 12px;border-left:4px solid {v_col};'
               f'background:{v_col}12;border-radius:4px;font-weight:600">'
               f'模块裁决：{_esc(sp.verdict)}</div>')

    meta = []
    if sp.atr is not None:
        meta.append(f"波幅口径：{_esc(sp.atr_note)} = {fmt(sp.atr)}（{sp.atr_pct:.1f}%/日），缓冲区按其缩放")
    if sp.sizing_note:
        meta.append(_esc(sp.sizing_note))
    meta_html = f'<div class="sub">{"　·　".join(meta)}</div>' if meta else ""

    vet = ""
    if sp.vetoes:
        chips = "".join(f'<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;'
                        f'border:1px solid #c6282855;background:#c628280d;color:#c62828;'
                        f'border-radius:10px;font-size:12px">✕ {_esc(v)}</span>' for v in sp.vetoes)
        vet = (f'<div style="margin:6px 0"><b style="color:#c62828;font-size:13px">'
               f'实时层否决票 ×{len(sp.vetoes)}</b><div>{chips}</div></div>')

    tickets = []
    for s in sp.scenarios:
        st_col = _status_col(s.status)
        dead = s.status == "情景作废"
        header = (f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
                  f'<b{" style=text-decoration:line-through" if dead else ""}>{_esc(s.name)}</b>'
                  f'<span style="font-size:11px;color:#6e7781">{_esc(s.stance)}</span>'
                  f'<span style="margin-left:auto;padding:2px 10px;border-radius:10px;font-size:12px;'
                  f'font-weight:600;color:{st_col};background:{st_col}1a">{_esc(s.status)}</span></div>')
        trig = (f'<div style="font-size:12.5px;color:#57606a;margin:3px 0">'
                f'触发（日收盘为准）：{_esc(s.trigger)}'
                f'{"　·　" + _esc(s.status_note) if s.status_note else ""}</div>')
        rail = _price_rail(s, sp.spot, fmt)
        exit_html = (f'<div style="font-size:12.5px;margin:4px 0;padding:6px 8px;'
                     f'background:#f6f8fa;border-radius:4px">🎯 {_esc(s.exit_plan)}</div>'
                     if s.exit_plan else "")
        inval = (f'<div style="font-size:11.5px;color:#6e7781">失效判定：{_esc(s.invalidation_note)}</div>'
                 if s.invalidation_note else "")
        tickets.append(f'<div style="border:1px solid #d0d7de;border-radius:6px;'
                       f'padding:8px 12px;margin:8px 0{";opacity:.55" if dead else ""}">'
                       f'{header}{trig}{rail}{exit_html}{inval}</div>')

    opts = f'<div class="sub" style="color:#6e7781">🧩 {_esc(sp.options_note)}</div>'
    cavs = "<small>" + " ".join(_esc(c) for c in sp.caveats) + "</small>"
    tl = f'<div class="chart">{timeline_svg}</div>' if timeline_svg else ""
    return (f'<div class="card">{head}{verdict}{meta_html}{vet}{tl}'
            f'{"".join(tickets)}{opts}{cavs}</div>')


def render_strategy_hub(proposals) -> str:
    """策略总纲（统筹层）：把各独立策略子模块的适配结论汇成一张调度表。"""
    if not proposals:
        return ""
    n_ap = sum(1 for p in proposals if p.applicable)
    rows = []
    for p in proposals:
        ico = "✅" if p.applicable else "—"
        col = "#2e7d32" if p.applicable else "#6e7781"
        tag = (f'<span style="padding:1px 8px;border-radius:10px;font-size:11px;'
               f'color:{col};background:{col}1a;white-space:nowrap">{_esc(p.tag)}</span>')
        rows.append(
            f'<tr style="border-top:1px solid #eaeef2">'
            f'<td style="padding:5px 8px;white-space:nowrap">{ico} <b>{_esc(p.name)}</b></td>'
            f'<td style="padding:5px 8px">{tag}</td>'
            f'<td style="padding:5px 8px;color:#57606a">{_esc(p.headline)}</td></tr>')
    return (f'<div class="card"><h2>策略总纲（统筹 · 多子模块调度）</h2>'
            f'<div class="sub">系统评估 {len(proposals)} 个独立策略子模块，其中 '
            f'<b>{n_ap}</b> 个适配当前信号。各子模块独立判断、下方分卡展开——模块输出，非交易指令。</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0">'
            f'{"".join(rows)}</table></div>')


def render_condor_section(cp) -> str:
    """铁鹰策略子卡：结构映射 + 盈亏结构 + 适配度体检。模块输出，非交易指令。"""
    if cp is None:
        return ""
    fmt = (lambda v: f"{v:,.0f}") if (cp.spot or 0) >= 500 else (lambda v: f"{v:,.1f}")
    head = '<h2>铁鹰（区间卖方 · 策略子模块，非交易指令）</h2>'

    if not cp.applicable:
        why = "".join(f'<li>{_esc(r)}</li>' for r in cp.reasons)
        cav = "<small>" + " ".join(_esc(c) for c in cp.caveats) + "</small>"
        return (f'<div class="card">{head}'
                f'<div style="margin:8px 0;padding:8px 12px;border-left:4px solid #6e7781;'
                f'background:#6e77810f;border-radius:4px;color:#57606a">{_esc(cp.headline)}</div>'
                f'{"<ul>" + why + "</ul>" if why else ""}{cav}</div>')

    fit = cp.fit_score
    fcol = "#2e7d32" if fit >= 75 else ("#d97706" if fit >= 55 else "#6e7781")
    banner = (f'<div style="margin:8px 0;padding:8px 12px;border-left:4px solid {fcol};'
              f'background:{fcol}12;border-radius:4px;font-weight:600">{_esc(cp.headline)}</div>')
    badge = (f'<div style="margin:6px 0"><span class="pill" '
             f'style="background:{fcol}1a;color:{fcol};font-weight:700">适配度 {fit}/100 · {_esc(cp.condor_type)}</span></div>')

    leg_rows = []
    for lg in cp.legs:
        acol = "#c62828" if lg.action == "卖出" else "#2e7d32"
        leg_rows.append(
            f'<tr style="border-top:1px solid #eaeef2">'
            f'<td style="padding:4px 8px;color:{acol};font-weight:600">{lg.action} {lg.kind}</td>'
            f'<td style="padding:4px 8px">{fmt(lg.strike)}</td>'
            f'<td style="padding:4px 8px;color:#57606a">{lg.delta:+.3f}</td>'
            f'<td style="padding:4px 8px;color:#57606a">{lg.iv_pp:.1f}%</td>'
            f'<td style="padding:4px 8px;color:#57606a">{lg.bs_price:.3f}</td></tr>')
    legs_tbl = (f'<table style="width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0">'
                f'<tr style="color:#8a919a;font-size:11px"><th style="text-align:left;padding:2px 8px">腿</th>'
                f'<th style="text-align:left;padding:2px 8px">行权</th>'
                f'<th style="text-align:left;padding:2px 8px">Δ</th>'
                f'<th style="text-align:left;padding:2px 8px">IV</th>'
                f'<th style="text-align:left;padding:2px 8px">BS理论价</th></tr>'
                f'{"".join(leg_rows)}</table>')

    pnl = []
    if cp.max_profit is not None:
        pnl.append(f"理论净收 <b>${cp.max_profit:,.0f}</b>")
    if cp.max_loss is not None:
        pnl.append(f"最大亏损 ${cp.max_loss:,.0f}")
    if cp.rr is not None:
        pnl.append(f"盈亏比 {cp.rr:.2f}:1")
    if cp.be_lo is not None and cp.be_hi is not None:
        pnl.append(f"盈亏平衡 {fmt(cp.be_lo)}/{fmt(cp.be_hi)}")
    if cp.centering is not None:
        pnl.append(f"居中度 {cp.centering:.2f}")
    pnl_html = (f'<div style="font-size:12.5px;margin:4px 0;padding:6px 8px;'
                f'background:#f6f8fa;border-radius:4px">📐 {"　·　".join(pnl)}</div>')

    reasons = "".join(f'<li>{_esc(r)}</li>' for r in cp.reasons)
    reasons_html = f'<ul style="margin:4px 0;font-size:12.5px">{reasons}</ul>' if reasons else ""
    cavs = "<small>" + " ".join("· " + _esc(c) for c in cp.caveats) + "</small>"
    edu = ('<div class="sub" style="color:#6e7781;margin-top:4px"><small>'
           '铁鹰 = 卖出区间上下两道墙（收权利金）+ 买入更外侧两翼（限制亏损）；'
           '赌价格横盘在盈亏平衡区内、IV 回落，怕突发单边大行情。适配度综合卖腿 delta、'
           '净收/翼宽、skew 摩擦、居中度打分。</small></div>')

    return (f'<div class="card">{head}{banner}{badge}{legs_tbl}{pnl_html}'
            f'{reasons_html}{cavs}{edu}</div>')


def render_concentration_html(cs) -> str:
    """大户集中度一行（作者口径 R10：前8大净空集中度上行=空头火力向大户集中）。"""
    if cs is None:
        return ""
    return (f'<div class="sub">大户集中度（CFTC 净口径，占 OI%）：{_esc(cs.note())}'
            f'——净空集中度上行 = 空头火力向大户集中</div>')


def render_report_html(o: Outlook, price_svg: str, oi_svg: str, cot_svg: str,
                       flow_html: str = "", macro_html: str = "", events_html: str = "",
                       tldr_html: str = "", strategy_html: str = "",
                       conc_html: str = "", volregime_html: str = "",
                       vol_analysis_html: str = "") -> str:
    if o.commodity_symbol and o.commodity_spot is not None:
        # 真实期货价为主，ETF 代理为辅
        price_line = (f'真实价 <b>{o.commodity_spot:,.1f}</b>（{_esc(o.commodity_symbol)} 期货）'
                      f' · 期权代理 {_esc(o.proxy_symbol)} {o.spot:.2f}')
        basis_line = f'<div class="sub">位点换算：{_esc(o.commodity_basis)}</div>'
    else:
        com = f"（≈商品 {o.commodity_spot:,.0f}）" if o.commodity_spot is not None else ""
        price_line = f'代理 {_esc(o.proxy_symbol)} · 现价 {o.spot:.2f}{com}'
        basis_line = ""
    head = (
        f'<div class="card"><h1>{_esc(o.display_name)} · 综合研判</h1>'
        f'<div class="sub">{price_line} · 数据 {_esc(o.asof)}</div>'
        f'{basis_line}'
        f'<div style="margin:10px 0">{_bias_badge(o)}</div>'
        f'<div class="sub">环境：{_esc(o.regime)}</div></div>'
    )
    body = (
        f'{tldr_html}'
        f'{events_html}'
        f'<div class="card"><h2>关键位点（吸附/支撑/阻力/翻转）</h2>{_levels_table(o)}'
        f'<div class="chart">{price_svg}</div>'
        f'<div class="chart">{oi_svg}</div></div>'
        f'{flow_html}'
        f'{vol_analysis_html}'
        f'{volregime_html}'
        f'<div class="card"><h2>方向因子投票（按回测可信度加权）</h2>{_votes_table(o)}</div>'
        f'{macro_html}'
        f'<div class="card"><h2>持仓结构</h2>{conc_html}<div class="chart">{cot_svg}</div></div>'
        f'<div class="card"><h2>情景推演（规则化 if-then，非点位预言）</h2>{_scenarios_html(o)}</div>'
        f'{strategy_html}'
        f'<div class="card">{_caveats_html(o)}</div>'
    )
    foot = ('<div class="foot">undertow · 规则化情景工具，非投资建议 · '
            '数据 CFTC + CBOE（ETF 代理）· 纯标准库生成</div>')
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_esc(o.display_name)} 综合研判</title><style>{_CSS}</style></head>'
        f'<body><div class="wrap">{head}{body}{foot}</div></body></html>'
    )


def render_index_html(items: list[tuple[str, str, str, str]], asof: str) -> str:
    """多品种索引页。items=[(display_name, filename, bias, confidence)]。"""
    cards = []
    for name, fn, bias, conf in items:
        color = _BIAS_COLOR.get(bias, "#6e7781")
        cards.append(
            f'<a class="card" style="display:block;text-decoration:none;color:inherit" href="{_esc(fn)}">'
            f'<h1 style="font-size:17px">{_esc(name)}</h1>'
            f'<span class="badge" style="background:{color}">{_esc(bias)}</span>'
            f'<span class="pill">可信度 {_esc(conf)}</span></a>'
        )
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>综合研判 {_esc(asof)}</title><style>{_CSS}</style></head>'
        f'<body><div class="wrap"><div class="card"><h1>大宗商品综合研判</h1>'
        f'<div class="sub">{_esc(asof)} · 点击进入各品种</div></div>{"".join(cards)}'
        '<div class="foot">undertow · 纯标准库生成</div></div></body></html>'
    )
