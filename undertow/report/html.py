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
                + spread_html
                + _flow_kind_table_html(fa.changes, "P")
                + _flow_kind_table_html(fa.changes, "C"))
    else:
        note = ("仅一份快照，ΔOI/ΔIV 的买卖方判定需要明天第二份快照才点亮。下方为今日单快照"
                "异常活跃（量/OI 高 = 多为当日新建仓，是 ΔOI 异动的先兆）："
                if not fa.prev_date else "近月无超阈值异动；下方为今日异常活跃：")
        body = f'<div class="warn" style="margin-bottom:8px">{_esc(note)}</div>' + _unusual_table_html(fa)
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
    for e in events:
        tm = e.tminus(today)
        urge = "#cf222e" if (e.days_until(today) <= 2 and e.importance == "high") else "#57606a"
        cat = CATEGORY_LABEL.get(e.category, e.category)
        cat_c = _CAT_COLOR.get(e.category, "#57606a")
        when = f"{e.date.isoformat()}" + (f" {e.time_et} ET" if e.time_et and e.time_et != "—" else "")
        scope = " ".join(e.instruments) if e.instruments else "全局"
        note = f'<div class="t">{_esc(e.note)}</div>' if e.note else ""
        rows.append(
            f'<tr><td class="r" style="font-weight:700;color:{urge}">{e.mark} {tm}</td>'
            f'<td>{_esc(when)}</td>'
            f'<td><span class="pill" style="background:{cat_c}1a;color:{cat_c}">{_esc(cat)}</span> '
            f'{_esc(e.name)}{note}</td>'
            f'<td><small>{_esc(scope)}</small></td></tr>'
        )
    return (
        '<div class="card"><h2>事件雷达（美东日历 · 临近催化剂请降置信）</h2>'
        '<table><tr><th class="r">倒计时</th><th>时点</th><th>事件</th><th>影响</th></tr>'
        + "".join(rows) + "</table>"
        '<div class="sub" style="margin-top:6px">🔴高 / 🟡中影响。FOMC·CPI·非农前后跳空风险大，'
        '期权到期(OPEX)前 Gamma/OI 墙会失真——事件窗口内的位点研判仅供风险预案，勿当点位预言。</div></div>'
    )


def render_report_html(o: Outlook, price_svg: str, oi_svg: str, cot_svg: str,
                       flow_html: str = "", macro_html: str = "", events_html: str = "") -> str:
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
        f'{events_html}'
        f'<div class="card"><h2>关键位点（吸附/支撑/阻力/翻转）</h2>{_levels_table(o)}'
        f'<div class="chart">{price_svg}</div>'
        f'<div class="chart">{oi_svg}</div></div>'
        f'{flow_html}'
        f'<div class="card"><h2>方向因子投票（按回测可信度加权）</h2>{_votes_table(o)}</div>'
        f'{macro_html}'
        f'<div class="card"><h2>持仓结构</h2><div class="chart">{cot_svg}</div></div>'
        f'<div class="card"><h2>情景推演（规则化 if-then，非点位预言）</h2>{_scenarios_html(o)}</div>'
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
