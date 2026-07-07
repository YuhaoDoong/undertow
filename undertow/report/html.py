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


def render_tldr_section(text: str) -> str:
    """大白话速读卡片：一段话讲清现价、阻力支撑、多空分界与环境。"""
    if not text:
        return ""
    return ('<div class="card"><h2>大白话速读</h2>'
            f'<div style="font-size:15px;line-height:1.9">{_esc(text)}</div>'
            '<small>由关键位与当日数据自动拼句；措辞保留不确定性，非点位预言。</small></div>')


def render_strategy_section(sp) -> str:
    """策略情景参数化卡片（期货）：方向→情景→触发/失效/目标/盈亏比 + 否决票。"""
    if sp is None:
        return ""
    fmt = (lambda v: f"{v:,.0f}") if sp.spot >= 500 else (lambda v: f"{v:,.1f}")
    dir_col = {"做空": "#c62828", "做多": "#2e7d32", "观望": "#6e7781"}.get(sp.direction, "#6e7781")
    head = (f'<h2>策略情景参数化（期货 · 模块输出，非交易指令）</h2>'
            f'<div style="margin:6px 0"><span class="pill" '
            f'style="background:{dir_col}1a;color:{dir_col};font-weight:700">方向：{_esc(sp.direction)}</span>'
            f' <small>{_esc(sp.direction_source)}</small></div>')
    atr_line = ""
    if sp.atr is not None:
        atr_line = (f'<div class="sub">波幅口径：{_esc(sp.atr_note)} = {fmt(sp.atr)}'
                    f'（{sp.atr_pct:.1f}%/日）——所有缓冲区按其缩放</div>')
    vet = ""
    if sp.vetoes:
        items = "".join(f'<li>{_esc(v)}</li>' for v in sp.vetoes)
        vet = (f'<div style="margin:8px 0 2px;font-weight:600;color:#c62828">'
               f'实时层否决票 ×{len(sp.vetoes)}</div><ul style="margin:2px 0 6px">{items}</ul>')
    scen_rows = []
    for s in sp.scenarios:
        zone = (f"{fmt(s.entry_lo)} – {fmt(s.entry_hi)}"
                if s.entry_lo is not None else f"{fmt(s.entry_ref)}（触发价参考）")
        tg = "；".join(f"{fmt(v)}（{_esc(lbl)}，R:R≈{r:.1f}）"
                       for (v, lbl), r in zip(s.targets, s.rr)) or "—"
        scen_rows.append(
            f'<tr><td><b>{_esc(s.name)}</b><div class="t">{_esc(s.stance)}</div></td>'
            f'<td>{_esc(s.trigger)}</td>'
            f'<td class="r">{zone}</td>'
            f'<td class="r">{fmt(s.invalidation)}<div class="t">{_esc(s.invalidation_note)}</div></td>'
            f'<td>{tg}</td>'
            f'<td><b>{_esc(s.status)}</b><div class="t">{_esc(s.status_note)}</div></td></tr>')
    table = ("<table><tr><th>情景</th><th>触发条件（日收盘为准）</th><th class='r'>参考区</th>"
             "<th class='r'>失效线</th><th>目标（盈亏比）</th><th>状态</th></tr>"
             + "".join(scen_rows) + "</table>") if scen_rows else ""
    verdict = f'<div class="sub" style="margin-top:6px"><b>模块裁决：{_esc(sp.verdict)}</b></div>'
    sizing = f'<div class="sub">{_esc(sp.sizing_note)}</div>' if sp.sizing_note else ""
    opts = f'<div class="sub" style="color:#6e7781">🧩 {_esc(sp.options_note)}</div>'
    cavs = "<small>" + " ".join(_esc(c) for c in sp.caveats) + "</small>"
    return f'<div class="card">{head}{atr_line}{vet}{table}{verdict}{sizing}{opts}{cavs}</div>'


def render_report_html(o: Outlook, price_svg: str, oi_svg: str, cot_svg: str,
                       flow_html: str = "", macro_html: str = "", events_html: str = "",
                       tldr_html: str = "", strategy_html: str = "") -> str:
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
        f'<div class="card"><h2>方向因子投票（按回测可信度加权）</h2>{_votes_table(o)}</div>'
        f'{macro_html}'
        f'<div class="card"><h2>持仓结构</h2><div class="chart">{cot_svg}</div></div>'
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
