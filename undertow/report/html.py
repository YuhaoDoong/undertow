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
    # 近中分歧时【不】单挑综合"偏多/偏空"（会误导），并列近端+中期两枚徽章，综合分退为小 pill。
    if getattr(o, "horizon_split", False) and o.near_bias and o.mid_bias:
        nc = _BIAS_COLOR.get(o.near_bias, "#6e7781")
        mc = _BIAS_COLOR.get(o.mid_bias, "#6e7781")
        return (f'<span class="badge" style="background:{nc}">近端 {_esc(o.near_bias)}</span>'
                f'<span class="badge" style="background:{mc}">中期 {_esc(o.mid_bias)}</span>'
                f'<span class="pill">近中分歧</span>'
                f'<span class="pill">综合分 {o.bias_score:+.1f}·可信度 {_esc(o.confidence)}</span>')
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
        # 新仓纯净度：同样 +200 手，"成交203/OI+202"和"成交3029/OI-247"含义天差地别
        from undertow.analyze.flow import (PURITY_CLEAN, PURITY_IMPLAUSIBLE, PURITY_MIXED,
                                           purity_label, purity_reliability)
        pr = c.oi_conversion
        if pr is None:
            pcell = '<span style="color:#8c959f">—</span>'
        else:
            pcol = ("#cf222e" if pr > PURITY_IMPLAUSIBLE
                    else ("#1a7f37" if pr >= PURITY_CLEAN
                          else ("#9a6700" if pr >= PURITY_MIXED else "#8c959f")))
            pcell = (f'<span style="color:{pcol}">{min(pr,9.99):.2f}</span>'
                     f'<br><small style="color:{pcol}">{_esc(purity_label(pr))}</small>')
        rows.append(
            f'<tr><td class="r lvl">{c.strike:.1f}{wall}</td>'
            f'<td class="r" style="color:{col}">{c.d_oi:+,}</td>'
            f'<td class="r">{c.curr_oi:,}</td>'
            f'<td class="r">{c.curr_volume:,}</td>'
            f'<td class="r">{pcell}</td>'
            f'<td class="r">{c.delta:+.3f}</td>'
            f'<td class="r">{_esc(adj)}</td>'
            f'<td style="color:{col};font-weight:600">{_esc(c.judgment)}{sp}'
            f'<br><small style="color:#6e7781">可靠度 {_esc(purity_reliability(pr))}</small>'
            f'</td></tr>'
        )
    return (f'<div style="font-weight:600;margin:10px 0 2px">{side}</div>'
            "<table><tr><th class='r'>行权价</th><th class='r'>ΔOI</th><th class='r'>当前OI</th>"
            "<th class='r'>今成交</th><th class='r'>转化率</th>"
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
    """波动率面小块：ATM IV / 25Δ·10Δ 偏斜的日变化 + 买方确认判读（机构口径）。"""
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


_OS_COLOR = {"极超卖": "#0550ae", "强超卖": "#0969da", "偏超卖": "#54aeff",
             "中性": "#6e7781",
             "偏超买": "#ff8182", "强超买": "#cf222e", "极超买": "#a40e26"}


def stretch_pill(sr, *, compact: bool = False) -> str:
    """超买超卖的一枚小标签，供报告头部与索引页使用。

    刻意把【是否显著】压进标签本身：14 格里只有 5 格 |t|≥2，绝大多数读数只是参考。
    显著的加 ✅，不显著的加 ~，中性不加——这样扫一眼就知道该不该当真，
    而不是看到「强超买」三个字就脑补方向。
    """
    if sr is None or not getattr(sr, "ok", False):
        return ""
    col = _OS_COLOR.get(sr.band, "#6e7781")
    mark = ""
    if sr.band != "中性":
        mark = " ✅" if sr.reliable else " ~"
    pct = f"{sr.pctile*100:.0f}%"
    if compact:
        return (f'<span class="pill" style="background:{col};color:#fff">'
                f'{_esc(sr.band)}{mark} {pct}</span>')
    div = "　⚠️两维分歧" if getattr(sr, "diverge", "") else ""
    return (f'<span class="pill" style="background:{col};color:#fff">'
            f'{_esc(sr.band)}{mark}</span>'      # 不再重复"超买超卖"：卡片标题已有
            f'<span class="sub" style="margin-left:8px">合并分位 {pct}'
            f'（偏离 {sr.stretch_pctile*100:.0f}% / 回撤 {sr.dd_pctile*100:.0f}%）'
            f'{_esc(div)}</span>')


def render_resonance_banner(rr) -> str:
    """共振条：期权结构（主）与超买超卖（辅）是否同向。

    刻意做得克制——未校准的东西不配拿醒目配色。共振/背离用浅底细边，
    并且把"未校准 + COT 层反证"直接写在条里，避免读者按信号强度理解它。
    """
    if rr is None or not getattr(rr, "ok", False) or rr.state in ("无信号", "仅结构"):
        return ""
    tone = {"共振看多": ("#0969da", "#ddf4ff"), "共振看空": ("#cf222e", "#ffebe9"),
            "背离": ("#9a6700", "#fff8c5")}.get(rr.state, ("#6e7781", "#f6f8fa"))
    col, bg = tone
    return (f'<div style="margin-top:10px;padding:8px 10px;background:{bg};'
            f'border-left:3px solid {col};border-radius:4px">'
            f'<b style="color:{col}">共振层 · {_esc(rr.state)}</b>'
            f'<div class="sub" style="margin-top:3px">{_esc(rr.headline)}</div>'
            f'<div class="sub" style="margin-top:4px"><small>⚠️ {_esc(rr.caveat)}</small></div>'
            f'</div>')


from datetime import date as _d


def render_technicals_section(tr, sr, rr=None, *, cross=None, asof="", src="", today="", h4=None) -> str:
    """技术面卡片：超买超卖（拉伸度，回测校准）+ 趋势结构 + 传统指标读数。

    刻意的展示顺序：**先给回测校准过的拉伸度，再给传统指标**。因为过热分那五个
    分量彼此相关 0.79~0.93、"强超买"占 20.5% 的时间，读者很容易看到标签就脑补
    "要回调了"——而回测里超买侧根本没过显著性。两者分歧时显式告警。
    """
    if (tr is None or not getattr(tr, "ok", False)) and \
       (sr is None or not getattr(sr, "ok", False)):
        return ""
    C_OS, C_OB, C_NEU = "#0969da", "#cf222e", "#6e7781"   # 超卖蓝 / 超买红 / 中性灰

    parts = []
    # —— 主角：两维超买超卖 ——
    if sr is not None and getattr(sr, "ok", False):
        col = C_OS if "超卖" in sr.band else (C_OB if "超买" in sr.band else C_NEU)
        badge = (f'<span style="display:inline-block;padding:3px 12px;border-radius:12px;'
                 f'background:{col};color:#fff;font-weight:600">'
                 f'{_esc(sr.band)} · {_esc(sr.regime)}市</span>'
                 f'<span class="sub" style="margin-left:10px">合并分位 '
                 f'<b>{sr.pctile*100:.0f}%</b></span>')
        # 两个维度并排，各带自己的分位——分歧时读者能一眼看出是哪一维在说话
        def _dim(title, val_html, pct, hint):
            pc = C_OS if pct <= 0.25 else (C_OB if pct >= 0.75 else C_NEU)
            return (f'<td style="padding:6px 14px 6px 0;vertical-align:top">'
                    f'<div class="sub">{title}</div>'
                    f'<div style="font-size:15px;margin:2px 0">{val_html}</div>'
                    f'<div class="sub">分位 <b style="color:{pc}">{pct*100:.0f}%</b>'
                    f' · {hint}</div></td>')
        dims = (f'<table style="margin-top:10px;border:none"><tr>'
                + _dim("偏离度 · 离常态多远",
                       f'<b>{sr.stretch:+.2f}</b> 个 ATR（离 MA20 {sr.ma20:.2f}）',
                       sr.stretch_pctile, f"ATR14 {sr.atr:.2f}")
                + _dim("回撤度 · 从近期高点掉多少",
                       f'<b>{sr.drawdown_pct:+.2f}%</b>（{sr.drawdown:+.2f} 个 ATR）',
                       sr.dd_pctile, f"60日高 {sr.high_n:.2f}")
                + '</tr></table>')
        nums = dims

        diverge = ""
        if sr.diverge:
            diverge = (f'<div style="margin-top:10px;padding:8px 10px;background:#fff8c5;'
                       f'border-left:3px solid #9a6700;border-radius:4px">'
                       f'⚠️ <b>两维分歧</b>：{_esc(sr.diverge)}</div>')

        calib = ""
        if sr.edge_pp is not None and sr.band != "中性":
            ecol = C_OS if sr.edge_pp > 0 else C_OB
            if sr.reliable:
                tag = f'<b style="color:#1a7f37">显著（t={sr.t_stat:+.2f}）</b>'
            else:
                tag = f'<span style="color:{C_NEU}">未达显著（t={sr.t_stat:+.2f}）→ 仅参考</span>'
            calib = (f'<div style="margin-top:8px;padding:8px 10px;background:#f6f8fa;'
                     f'border-left:3px solid {ecol};border-radius:4px">'
                     f'<b>回测校准</b>：历史上处于此档位时，此后 5 日相对「什么都不做」'
                     f'<b style="color:{ecol}">{sr.edge_pp:+.2f}pp</b>，'
                     f'跑赢率 {sr.win_rate:.0f}%，n={sr.n_hist} · {tag}</div>')
        elif sr.band == "中性":
            calib = (f'<div class="sub" style="margin-top:8px">'
                     f'中性档即基准桶，无方向性边缘。</div>')
        parts.append(f'<div style="margin:4px 0 2px">{badge}</div>{nums}{diverge}{calib}')

    # —— 配角：传统指标 + 趋势结构，并在两者分歧时告警 ——
    if tr is not None and getattr(tr, "ok", False):
        conflict = ""
        if sr is not None and getattr(sr, "ok", False):
            heat_extreme = tr.heat in ("强超买", "强超卖", "偏超买", "偏超卖")
            stretch_flat = sr.band == "中性"
            if heat_extreme and stretch_flat:
                # 分歧的机理几乎总是同一个：RSI/KDJ/CCI 测的是"最近几根走得多急"，
                # 拉伸度测的是"离常态多远"。急但不远 → 过热分喊极端、拉伸度说中性。
                fast = "跌得急" if "超卖" in tr.heat else "涨得急"
                conflict = (f'<div style="margin-top:10px;padding:8px 10px;'
                            f'background:#fff8c5;border-left:3px solid #9a6700;border-radius:4px">'
                            f'⚠️ <b>与过热分分歧</b>：过热分说「{_esc(tr.heat)}」，'
                            f'但偏离度只在 {sr.stretch_pctile*100:.0f}% 分位、'
                            f'回撤度在 {sr.dd_pctile*100:.0f}% 分位，'
                            f'合并 {sr.pctile*100:.0f}% → 中性。'
                            f'<b>{fast}，但两个维度都不算极端</b>——'
                            f'RSI/KDJ/CCI 测的是最近几根走得多急。'
                            f'<b>以校准读数为准</b>：过热分的五个分量彼此相关 0.79~0.93，'
                            f'是同一信息数了四遍，其极端档占了两成时间。</div>')
        ind = []
        if tr.rsi6 is not None:
            ind.append(f"RSI6 <b>{tr.rsi6:.0f}</b>")
        if tr.rsi14 is not None:
            ind.append(f"RSI14 <b>{tr.rsi14:.0f}</b>")
        if tr.kdj is not None:
            ind.append(f"KDJ-J <b>{tr.kdj[2]:.0f}</b>")
        if tr.cci is not None:
            ind.append(f"CCI <b>{tr.cci:.0f}</b>")
        if tr.boll is not None:
            ind.append(f"布林%b <b>{tr.boll[3]:.2f}</b>")
        if tr.macd is not None:
            ind.append(f"MACD柱 <b>{tr.macd[2]:+.2f}</b>")
        parts.append(
            f'{conflict}'
            f'<div class="sub" style="margin-top:10px">'
            f'趋势结构：<b>{_esc(tr.trend)}</b> · '
            f'过热分 <b>{tr.heat_score:+d}</b>（{_esc(tr.heat)}）'
            f'<span style="color:{C_NEU}">— 已降级为参考，见下</span></div>'
            f'<div class="sub" style="margin-top:4px">' + " · ".join(ind) + "</div>")

    edu = ('<small><b>两个维度，问的不是同一个问题</b>：偏离度 =（现价−MA20）÷ATR14，'
           '问"离常态多远"；回撤度 =（现价−60日最高）÷ATR14，问"从近期高点掉多少"。'
           '两者分位序列相关仅 0.73（而回撤20日/区间位置与偏离度相关 0.91/0.95，'
           '是同一件事换个写法，故不采用）。档位由两维分位均值决定——合并后 '
           't 从 3.99/4.67 升到 4.86，确有增益。<br>'
           '<b>口径</b>：边缘 = +5日收益 − 过去60日局部漂移 − 同 regime 中性桶，'
           '即"比什么都不做多赚多少"；t 为 Welch 双样本、不重叠子样本。<br>'
           '<b>"跑赢率"不是"上涨概率"</b>：超卖档跑赢率 60~69%，但<b>绝对方向准确率'
           '只有 56~60%（基准 55~56%）</b>——差别在于前者扣掉了局部漂移。<br>'
           '<b>已知边界</b>：约 3 万样本（GLD/SLV/USO/QQQ/SPY，1993→2026）下 14 格里'
           '仅 5 格 |t|≥2，其中 4 格在超卖侧；<b>超买侧只能读作"追高性价比差"，'
           '不可读作"要反转"</b>。两维分歧时边缘只剩一致时的四成且不显著。'
           '<b>仅日线成立</b>——1H/4H 回测分离度全在 ±0.2pp 且符号不稳定，是噪音。'
           '重跑校准：<code>undertow backtest-stretch --emit --compare</code>。</small>')
    # —— 数据时效：技术面走的价格源与期权数据【不是同一套】，必须单独标注 ——
    # 2026-08-27 实测：CBOE 历史日线滞后两天（8/27 当天仍止于 8/25），
    # 而报告头部的时效横幅标的是期权数据的时效 —— 技术面这层滞后毫无提示，
    # 于是"两天前的深度超卖"冒充"今天的超卖"。
    vint = ""
    if asof:
        stale = bool(today and asof < today)
        vint = (f'<div class="sub" style="margin:2px 0 8px;'
                f'{"color:#bf8700;font-weight:700" if stale else ""}">'
                f'{"⚠️ " if stale else ""}技术面数据源：{_esc(src or "—")} · '
                f'截止 <b>{_esc(asof)}</b>'
                + (f'（今天是 {_esc(today)}，<b>滞后 {(_d.fromisoformat(today)-_d.fromisoformat(asof)).days} 天</b>，'
                   f'下方读数不代表今日状态）' if stale else '（当日）✅')
                + '</div>')
    # —— 穿越事件：末值答不了"有没有金叉"，而那正是交易者在看的 ——
    cx = ""
    if cross:
        bits = []
        for key, name in (("kdj", "KDJ"), ("macd", "MACD")):
            v = cross.get(key)
            if not v:
                continue
            ev = ""
            if v.get("event"):
                fresh = (v.get("days_ago") == 0)
                col = "#1a7f37" if v["event"] == "金叉" else "#cf222e"
                when = "今日刚穿" if fresh else f"{v.get('days_ago')} 根前"
                ev = (f'　<b style="color:{col}">{v["event"]}</b>（{when}）')
            else:
                ev = "　<span class=\"sub\">近 30 根无穿越</span>"
            bits.append(f'<li>{name}：{_esc(v["state"])}{ev}</li>')
        mp = cross.get("macd_params")
        note = (f'<div class="sub">MACD 参数 {mp}（不同看盘软件默认值不同，'
                f'快参数会更早出现金叉；这里用标准值）</div>' if mp else "")
        if bits:
            cx = f'<h3>金叉/死叉</h3><ul class="sub">{"".join(bits)}</ul>{note}'
    # —— 4H 层：**只展示，不进方向判定** ——
    # 早期回测已验证 1H/4H 对方向是噪音、只有日线站得住。加它是为了能对上用户
    # 看盘时实际在看的东西，绝不让它参与裁决 —— 否则又变成"指标互相打架"。
    h4h = ""
    if h4:
        k4, d4, j4 = h4["kdj"]
        f4, e4, hh4 = h4["macd"]
        rows = []
        for key, nm in (("kdj", "KDJ"), ("macd", "MACD")):
            v = (h4.get("cross") or {}).get(key)
            if not v:
                continue
            ev = ""
            if v.get("event"):
                col = "#1a7f37" if v["event"] == "金叉" else "#cf222e"
                when = "本根刚穿" if v.get("days_ago") == 0 else f"{v.get('days_ago')} 根前"
                ev = f'　<b style="color:{col}">{v["event"]}</b>（{when}）'
            rows.append(f'<li>{nm}：{_esc(v["state"])}{ev}</li>')
        h4h = (f'<h3>4 小时层 <span class="sub">（由 1h 按交易日分组聚合；'
               f'<b>仅展示，不参与方向判定</b>——早期回测已验证 1H/4H 对方向是噪音）</span></h3>'
               f'<div class="sub">截止 {h4["asof"]:%m-%d %H:%M}Z · {h4["n_bars"]} 根 · '
               f'收 {h4["close"]:.2f}　RSI6 {h4["rsi6"]:.0f} · RSI14 {h4["rsi14"]:.0f} · '
               f'KDJ K{k4:.1f}/D{d4:.1f}/J{j4:.1f} · MACD柱 {hh4:+.2f}</div>'
               f'<ul class="sub">{"".join(rows)}</ul>')
    return (f'<div class="card"><h2>技术面 · 超买超卖（回测校准）</h2>{vint}{cx}{h4h}'
            + "".join(parts) + render_resonance_banner(rr)
            + f'<div style="margin-top:10px">{edu}</div></div>')


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


def render_flow_section(fa, migration: dict | None = None) -> str:
    """期权资金流买卖方卡片。两份快照→买卖方表；仅一份→单快照异常活跃。"""
    if fa is None:
        return ""
    head = ('<h2>期权资金流 / 持仓异动（买方 vs 卖方）</h2>'
            + render_wall_zones(migration or {}))
    tilt = f'<div class="sub">净倾向：<b>{_esc(fa.flow_tilt)}</b></div>'
    # 裁决的【全部】理由 + 未校准标记必须落到用户眼前。
    # 旧版只渲染 reasons[0]，而"未经校准"提示常在 reasons[1] —— 于是
    # "每条理由强制带标注"只在对象级测试成立，用户看到的报告里没有。
    _cc = getattr(fa, "call", None)
    if _cc is not None:
        _rs = list(getattr(_cc, "reasons", []) or [])
        if len(_rs) > 1:
            tilt += ('<ul class="sub" style="margin:4px 0 0 18px">'
                     + "".join(f"<li>{_esc(r)}</li>" for r in _rs[1:]) + "</ul>")
        if not getattr(_cc, "calibrated", False):
            tilt += ('<div class="sub" style="color:#9a6700">⚠ 本层方向判定'
                     '<b>未经回测校准</b>——实测覆盖率/正确率权衡里没有任何门槛的 '
                     'Wilson 95% 区间下界超过 50%。</div>')
    # —— 净有效 Delta：与上面的"加权增仓"并列展示，两者性质不同，冲突时必须让读者看见 ——
    nd = getattr(fa, "net_delta_total", None)
    if nd is not None:          # 0.0 是合法读数，不得隐藏
        ndc = getattr(fa, "net_delta_call", 0.0)
        ndp = getattr(fa, "net_delta_put", 0.0)
        # ⚠️ 读结构化裁决，不在散文里搜"多"/"空"（弃权文案同时含两字）
        _c = getattr(fa, "call", None)
        _d = getattr(_c, "direction", "") if (_c and not getattr(_c, "abstain", True)) else ""
        clash = (nd > 0 and _d == "偏空") or (nd < 0 and _d == "偏多")
        warn = ('　<b style="color:#bf8700">⚠ 与上面的加权增仓倾向方向相反</b>'
                '——加权增仓是【推断】（先按 IV 判买卖方），净 Delta 是【观测】（纯算术，'
                '不需知道谁主动）。两者实测约 6 成日子不同向，本层未校准，'
                '不据此下结论，仅并列呈现。') if clash else ""
        tilt += (f'<div class="sub">净有效 Delta（Σ ΔOI×delta，观测口径）：'
                 f'<b style="color:{"#1a7f37" if nd > 0 else "#cf222e"}">{nd:+,.0f}</b>'
                 f'（call {ndc:+,.0f} / put {ndp:+,.0f}）{warn}</div>')
    if fa.prev_date and fa.changes:
        spread_html = ""
        if fa.spreads:
            items = "".join(f"<li><b>{_esc(s.name)}</b>：{_esc(s.detail)}</li>" for s in fa.spreads)
            spread_html = ('<div class="warn" style="margin:8px 0"><b>⚠️ 检测到疑似价差结构'
                           '（已从方向压力中扣除"封顶/保护腿"，避免误读为方向）</b>'
                           f'<ul>{items}</ul></div>')
        body = (f'<div class="sub">对比 {_esc(fa.prev_date)} → {_esc(fa.curr_date)} · '
                'OI增+IV升=买方抬价 / OI增+IV降=卖方写权'
                '<br><b>转化率</b> = |ΔOI| ÷ 今日成交量 —— 当天成交有多少真的沉淀成了持仓。'
                '≈1.0 是干净的新建仓（或干净的了结），这个 ΔOI 代表真实意愿；'
                '≪1.0 说明绝大部分是日内换手/对敲进出，同样一个 ΔOI 几乎不携带信息。'
                '⚠️ CME 会单列 PNT（场外协商成交）并从量里剔除，CBOE 延迟数据没有该字段，'
                '我们的转化率里仍混着这类成交，故<b>只用于降权、不用于加权</b>。</div>'
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


def _tilt_color(tilt: str) -> str:
    if tilt.startswith("偏空"):
        return _FLOW_COLOR["bearish"]
    if tilt.startswith("偏多"):
        return _FLOW_COLOR["bullish"]
    return "#6e7781"


def _expiry_flow_rows_html(changes, conv=None, top: int = 6, etf_symbol: str = "") -> str:
    """逐到期的紧凑 ΔOI 买卖方表（C/P 混排，取 |ΔOI| 最大的前 top 行）。

    有 conv（真实比值）时，行权价列显示【商品价 + ETF 行权价】——ETF 行权价才是
    实盘下单选腿用的数（如 SLV 30.5），商品价（银 67.0）作跨报告对照。
    """
    items = sorted(changes, key=lambda x: -abs(x.d_oi))[:top]
    if not items:
        return ""
    show_etf = conv is not None
    sym = _esc(etf_symbol)
    rows = []
    for c in sorted(items, key=lambda x: (x.kind, x.strike)):
        col = _FLOW_COLOR.get(c.bias, "#6e7781")
        k = "P" if c.kind == "P" else "C"
        stk = conv(c.strike) if conv else c.strike
        wall = f' <span style="color:#9467bd">🧱{_esc(c.on_wall)}</span>' if c.on_wall else ""
        sp = (f' <span style="color:#9467bd;font-weight:400">⟂{_esc(c.spread_note)}</span>'
              if getattr(c, "spread_note", "") else "")
        adj = f"{c.adj_iv_pp:+.2f}" if c.prev_iv > 0 else "—"
        etf_cell = (f'<td class="r lvl" style="color:#0969da;font-weight:600">{c.strike:.1f}</td>'
                    if show_etf else "")
        rows.append(
            f'<tr><td>{k}</td><td class="r lvl">{stk:.1f}{wall}</td>{etf_cell}'
            f'<td class="r" style="color:{col}">{c.d_oi:+,}</td>'
            f'<td class="r">{c.curr_oi:,}</td><td class="r">{_esc(adj)}</td>'
            f'<td style="color:{col};font-weight:600">{_esc(c.judgment)}{sp}</td></tr>'
        )
    etf_th = (f"<th class='r' style='color:#0969da'>{sym or 'ETF'}行权</th>"
              if show_etf else "")
    return ("<table><tr><th>C/P</th><th class='r'>行权价</th>" + etf_th
            + "<th class='r'>ΔOI</th>"
            "<th class='r'>当前OI</th><th class='r'>修正ΔIV</th><th>判断</th></tr>"
            + "".join(rows) + "</table>")


def render_expiry_ladder_section(slices, conv=None, unit: str = "", etf_symbol: str = "") -> str:
    """近周到期阶梯：逐到期（本周五/下周五/下下周五/月度OPEX）单独的墙位 + 买卖方。

    专服务短线定到期价差：想做 X 日到期的 SLV 熊市看涨价差，直接看 X 日那条的
    call 墙压在哪、当天范围内谁在卖 call。conv 把 ETF 行权价换算商品价（有真实比值时）；
    有 conv 时同时显示 ETF 行权价（etf_symbol，如 SLV 30.5）——实盘下单选腿用它。
    """
    if not slices:
        return ""
    u = _esc(unit)
    sym = _esc(etf_symbol)
    show_etf = conv is not None
    # 商品价旁的 ETF 行权价角标（raw = ETF 行权价原值）
    def _etf(raw):
        return (f' <span style="color:#0969da;font-weight:600">{sym}{raw:.1f}</span>'
                if show_etf else "")
    cards = []
    for s in slices:
        cw = conv(s.call_wall) if conv else s.call_wall
        pw = conv(s.put_wall) if conv else s.put_wall
        # 墙位行：主墙 + 并列前三（商品价 + ETF 行权价）
        def _walls(top, primary_oi):
            bits = [f"{(conv(k) if conv else k):.1f}{u}{_etf(k)}({v:,})" for k, v in top[:3]]
            return " · ".join(bits) if bits else "—"
        cwall = (f'<b style="color:{_FLOW_COLOR["bearish"]}">call墙 {cw:.1f}{u}</b>{_etf(s.call_wall)}'
                 f'（OI {s.call_wall_oi:,}）') if s.call_wall_oi > 0 else "call墙 —"
        pwall = (f'<b style="color:{_FLOW_COLOR["bullish"]}">put墙 {pw:.1f}{u}</b>{_etf(s.put_wall)}'
                 f'（OI {s.put_wall_oi:,}）') if s.put_wall_oi > 0 else "put墙 —"
        monthly_tag = ('<span class="pill" style="background:#bc4c001a;color:#bc4c00">月度OPEX</span>'
                       if s.is_monthly else "")
        pcr_note = f"PCR {s.pcr:.2f}" if s.total_call_oi > 0 else ""
        head = (f'<div style="font-weight:700;margin:12px 0 4px;font-size:15px">'
                f'{_esc(s.expiry.isoformat())} · <b>{_esc(s.label)}</b> '
                f'<span class="sub" style="font-weight:400">T-{s.days_out}</span> {monthly_tag}</div>')
        meta = (f'<div class="sub">{cwall} ｜ {pwall} ｜ {_esc(pcr_note)}'
                f'（总 Call {s.total_call_oi:,} / Put {s.total_put_oi:,}）</div>')
        top_walls = (f'<div class="sub" style="margin-top:2px">上方阻力群 {_walls(s.call_walls_top, s.call_wall_oi)}'
                     f' ｜ 下方支撑群 {_walls(s.put_walls_top, s.put_wall_oi)}</div>')
        if s.has_flow and s.changes:
            tcol = _tilt_color(s.flow_tilt)
            flow = (f'<div class="sub" style="margin-top:4px">当日范围买卖方：'
                    f'<b style="color:{tcol}">{_esc(s.flow_tilt)}</b></div>'
                    + _expiry_flow_rows_html(s.changes, conv=conv, etf_symbol=etf_symbol))
        else:
            flow = ('<div class="sub" style="margin-top:4px">买卖方：仅一份快照或该到期昨日无仓，'
                    'ΔOI 判定待下一份快照点亮（墙位不受影响）。</div>')
        cards.append(f'<div style="border-left:3px solid #d0d7de;padding-left:10px;margin:6px 0">'
                     f'{head}{meta}{top_walls}{flow}</div>')
    return (
        '<div class="card"><h2>近周到期阶梯（按周五定到期做价差用）</h2>'
        '<div class="sub">把 60 天混在一起的墙/资金流拆回【单个到期日】：想做某周五到期的价差，'
        '直接看那条的 call/put 墙压在哪、当天范围内谁在买卖。墙位口径同主报告，'
        'ΔOI 买卖方 = 该到期昨→今独立 diff。'
        + ('<b style="color:#0969da"> 蓝色为 ETF 行权价（实盘下单选腿用它）</b>，'
           '同行商品价作跨报告对照。' if slices and conv is not None else "")
        + '</div>'
        + "".join(cards)
        + '<div class="sub" style="margin-top:8px">越近的周度到期 IV 噪音越大；ETF 代理位点仅定性。'
          '只作波段级结构预警，非交易指令。</div></div>'
    )


_GRADE_COLOR = {"差": "#cf222e", "中": "#bc4c00", "优": "#1a7f37"}


def render_fib_rr_section(fib, plan, etf_symbol: str = "") -> str:
    """斐波那契回撤 + 盈亏比闸门（「先看盈亏比、别追、等回调」这套交易纪律的确定性落地）。"""
    if fib is None or not fib.ok:
        return ""
    sym = _esc(etf_symbol)
    fnum = (lambda v: f"{v:,.0f}") if fib.spot >= 500 else (lambda v: f"{v:,.1f}")
    show_etf = fib.ratio is not None

    def _etf(v):
        return (f' <span style="color:#0969da;font-weight:600">{sym}{v:.1f}</span>'
                if (show_etf and v is not None) else "")

    dir_cn = ("上涨腿（回撤=下方支撑，顺势=回调买）" if fib.direction == "up"
              else "下跌腿（回撤=上方阻力，顺势=反抽卖）")

    # 回撤位表
    rrows = []
    for lv in fib.retracements:
        star = ' <span style="color:#bc4c00">⭐关键区</span>' if lv.is_key else ""
        rrows.append(f'<tr><td>{_esc(lv.label)}{star}</td>'
                     f'<td class="r lvl">{fnum(lv.price)}{_etf(lv.etf)}</td><td>回撤</td></tr>')
    for lv in fib.extensions:
        rrows.append(f'<tr><td>{_esc(lv.label)}</td>'
                     f'<td class="r lvl">{fnum(lv.price)}{_etf(lv.etf)}</td><td>上行扩展目标</td></tr>')
    retr_tbl = ('<table><tr><th>比率</th><th class="r">价位</th><th>说明</th></tr>'
                + "".join(rrows) + "</table>")

    # 盈亏比情景表
    rr_block = ""
    if plan is not None and plan.ok:
        srows = []
        for s in plan.setups:
            gcol = _GRADE_COLOR.get(s.grade, "#6e7781")
            srows.append(
                f'<tr><td>{_esc(s.name)}</td>'
                f'<td class="r lvl">{fnum(s.entry)}{_etf(s.entry_etf)}</td>'
                f'<td class="r lvl">{fnum(s.stop)}{_etf(s.stop_etf)}</td>'
                f'<td class="r lvl">{fnum(s.target)}{_etf(s.target_etf)} '
                f'<span class="sub">{_esc(s.target_label)}</span></td>'
                f'<td class="r" style="color:{gcol};font-weight:700">{s.rr:.2f}</td>'
                f'<td style="color:{gcol};font-weight:600">{_esc(s.grade)}</td></tr>')
        rr_tbl = ('<table><tr><th>情景</th><th class="r">入场</th><th class="r">止损</th>'
                  '<th class="r">目标</th><th class="r">盈亏比</th><th>评级</th></tr>'
                  + "".join(srows) + "</table>")
        bias = (f'<div class="sub" style="margin-top:2px">{_esc(plan.bias_note)}</div>'
                if plan.bias_note else "")
        verds = "".join(f'<div class="sub">· {_esc(s.verdict)}</div>' for s in plan.setups)
        cav = "".join(f'<div class="sub">› {_esc(c)}</div>' for c in plan.caveats)
        rr_block = (f'<div style="font-weight:700;margin:12px 0 4px">盈亏比闸门（顺势 {_esc(plan.direction)}）</div>'
                    f'<div class="sub" style="margin-bottom:4px"><b>{_esc(plan.headline)}</b></div>'
                    f'{bias}{rr_tbl}{verds}'
                    f'<div class="sub" style="margin-top:6px;color:#6e7781">{cav}</div>')

    etf_hint = ('<b style="color:#0969da"> 蓝色为 ETF 行权价</b>；' if show_etf else "")
    return (
        '<div class="card"><h2>斐波那契回撤 + 盈亏比闸门</h2>'
        '<div class="sub">波段交易纪律的确定性落地：<b>先看盈亏比、别追高、等回调给出好盈亏比再动手</b>。'
        '摆动腿自动检测自真实期货日线；' + etf_hint
        + '目标取自结构墙位/斐波扩展，非价格预测，仅波段级情景参考。</div>'
        f'<div style="font-weight:700;margin:10px 0 4px">摆动腿：{_esc(dir_cn)}</div>'
        f'<div class="sub">{_esc(fib.note)}；现价 {fnum(fib.spot)}'
        + (f'（{sym}{fib.etf_spot:.1f}）' if fib.etf_spot else "")
        + f' · {_esc(fib.current_zone)}</div>'
        + retr_tbl + rr_block
        + '</div>'
    )


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


def render_credit_spread_section(cp) -> str:
    """方向性信用价差子卡：顺方向单侧卖方结构 + 盈亏 + 适配体检。模块输出，非交易指令。"""
    if cp is None:
        return ""
    fmt = (lambda v: f"{v:,.0f}") if (cp.spot or 0) >= 500 else (lambda v: f"{v:,.1f}")
    head = '<h2>方向性信用价差（顺向卖方 · 策略子模块，非交易指令）</h2>'

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
             f'style="background:{fcol}1a;color:{fcol};font-weight:700">适配度 {fit}/100 · '
             f'{_esc(cp.direction)}·{_esc(cp.spread_name)}</span></div>')

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
    if cp.breakeven is not None:
        pnl.append(f"盈亏平衡 {fmt(cp.breakeven)}")
    if cp.buffer_pct is not None:
        pnl.append(f"缓冲 {cp.buffer_pct:+.1f}%")
    if cp.iv_minus_rv is not None:
        pnl.append(f"IV−RV {cp.iv_minus_rv:+.1f}pp")
    pnl_html = (f'<div style="font-size:12.5px;margin:4px 0;padding:6px 8px;'
                f'background:#f6f8fa;border-radius:4px">📐 {"　·　".join(pnl)}</div>')

    reasons = "".join(f'<li>{_esc(r)}</li>' for r in cp.reasons)
    reasons_html = f'<ul style="margin:4px 0;font-size:12.5px">{reasons}</ul>' if reasons else ""
    cavs = "<small>" + " ".join("· " + _esc(c) for c in cp.caveats) + "</small>"
    edu = ('<div class="sub" style="color:#6e7781;margin-top:4px"><small>'
           '信用价差 = 顺方向卖一道墙（收权利金）+ 买更外侧一翼（封顶亏损）；赌价格不往反方向大动、'
           '时间价值衰减，怕方向突然反转。前置＝方向明确 + IV−RV 偏贵（期权贵于近期实际波动）；'
           '正Gamma（横盘钉住）对它是利好而非否决。盈亏比天然低，靠胜率+时间价值盈利。</small></div>')

    return (f'<div class="card">{head}{banner}{badge}{legs_tbl}{pnl_html}'
            f'{reasons_html}{cavs}{edu}</div>')


def render_concentration_html(cs) -> str:
    """大户集中度一行（机构口径 R10：前8大净空集中度上行=空头火力向大户集中）。"""
    if cs is None:
        return ""
    return (f'<div class="sub">大户集中度（CFTC 净口径，占 OI%）：{_esc(cs.note())}'
            f'——净空集中度上行 = 空头火力向大户集中</div>')


def render_verdict_section(v, display_name: str = "") -> str:
    """当日决策研判卡片：做空?/现价追?/短线/长线 四问的规则化结论（置于报告靠前）。

    确定性合成（近中分层＋资金流＋强信号＋斐波盈亏比闸门），无 LLM、数字来自上游。
    """
    if v is None or not getattr(v, "ok", False):
        return ""
    rows_data = [("做空？", v.short_answer), ("现价追？", v.chase_answer),
                 ("短线仓", v.swing_action), ("长线仓", v.core_action)]
    rows = ""
    for label, ans in rows_data:
        rows += (f'<tr>'
                 f'<td style="padding:6px 12px 6px 0;font-weight:700;white-space:nowrap;'
                 f'vertical-align:top;color:#0969da;width:1%">{_esc(label)}</td>'
                 f'<td style="padding:6px 0;line-height:1.65">{_esc(ans.strip())}</td></tr>')
    return (
        '<div class="card" style="border-left:4px solid #0969da">'
        f'<h2 style="margin-top:0">🧭 当日决策研判 · {_esc(display_name)}</h2>'
        f'<div style="font-size:16px;font-weight:800;margin:2px 0 10px;color:#0969da">{_esc(v.headline)}</div>'
        f'<table style="border-collapse:collapse;width:100%">{rows}</table>'
        f'<div class="sub" style="margin-top:10px;font-size:12px">{_esc(v.note)}</div>'
        '</div>'
    )


_DEFENSE_COLOR = {"进攻": "#1a7f37", "中性": "#6e7781", "中性偏防守": "#9a6700",
                  "短线偏防守": "#bc4c00", "恐慌防守": "#cf222e"}


def render_structure_section(sr, display_name: str = "") -> str:
    """结构读数卡片（机构口径）。**本卡片不含任何方向结论**。

    与投票层正交：只描述防守强度、位置、逐腿可靠度、证伪清单，因此不与
    偏多/偏空 打架。见 analyze/structure_read.py 模块 docstring。
    """
    if sr is None or not getattr(sr, "ok", False):
        return ""
    col = _DEFENSE_COLOR.get(sr.defense, "#6e7781")
    n_all, n_ok = len(sr.legs), len(sr.usable_legs)
    rows = "".join(
        f'<tr><td>{r.delta*100:.0f}Δ</td>'
        + "".join(f'<td style="text-align:right">'
                  f'{"—" if v is None else f"{v:+.2f}pp"}</td>'
                  for v in (r.d_call_pp, r.d_put_pp, r.d_skew_pp))
        + "</tr>" for r in sr.ladder)
    checks = "".join(
        f'<tr><td>{_esc(lab)}</td><td style="text-align:center">'
        f'{"❓" if ok is None else ("✅" if ok else "❌")}</td>'
        f'<td class="sub">{_esc(det)}</td></tr>'
        for lab, ok, det in sr.checklist)
    if sr.usable_legs:
        legs = "".join(
            f'<tr><td>{l.strike:g}</td><td>{l.kind}</td>'
            f'<td style="text-align:right">{l.d_oi:+,}</td>'
            f'<td style="text-align:right">{l.volume:,}</td>'
            f'<td style="text-align:right">{"—" if l.purity is None else f"{l.purity:.2f}"}</td>'
            f'<td style="text-align:right">{l.delta_adj_pp:+.2f}pp</td>'
            f'<td style="text-align:right">{l.effective_delta:+,.0f}</td>'
            f'<td>{_esc(l.interpretation)}</td></tr>'
            for l in sorted(sr.usable_legs, key=lambda x: -abs(x.d_oi))[:12])
        legs_html = (
            '<table><thead><tr><th>行权价</th><th>C/P</th><th>ΔOI</th><th>成交</th>'
            '<th>纯净度</th><th>Delta修正后</th><th>有效Δ</th><th>解读</th></tr></thead>'
            f'<tbody>{legs}</tbody></table>')
    else:
        legs_html = ('<div class="sub" style="padding:8px 0"><b>今日无可用腿——'
                     '没有方向性信息。</b>这是正常结果，不是数据缺失。</div>')
    why = "".join(f"<li>{_esc(w)}</li>" for w in sr.defense_why) or "<li>无显著偏斜/波动率变化</li>"
    return (
        '<div class="card">'
        f'<h2>结构读数{" · " + _esc(display_name) if display_name else ""}'
        f'（机构口径 · <b>不输出方向票</b>）</h2>'
        f'<div style="margin:6px 0 10px">防守强度 '
        f'<span class="pill" style="background:{col};color:#fff">{_esc(sr.defense)}</span>'
        f'　<span class="sub">逐腿 {n_all} 条中仅 <b>{n_ok}</b> 条载有效信息'
        f'（{sr.noise_ratio*100:.0f}% 噪音/低可靠度，属正常）</span></div>'
        f'<div class="sub" style="margin-bottom:10px">{_esc(sr.state_summary)}</div>'
        f'<h3>偏斜与波动率面</h3>'
        f'<div class="sub">ATM IV {sr.atm_iv_pp:.2f}%（{sr.d_atm_pp:+.2f}pp）'
        f'　25Δ skew {sr.skew25_pp:+.2f}pp（{sr.d_skew25_pp:+.2f}pp）'
        f'　10Δ skew {sr.skew10_pp:+.2f}pp（{sr.d_skew10_pp:+.2f}pp）</div>'
        '<table><thead><tr><th>Delta</th><th>Call 变化</th><th>Put 变化</th>'
        f'<th>Skew 变化</th></tr></thead><tbody>{rows}</tbody></table>'
        f'<h3>有效 Delta（观测口径 · Σ ΔOI×delta）</h3>'
        f'<div class="sub">Call <b>{sr.eff_delta_call:+,.0f}</b>'
        f' ／ Put <b>{sr.eff_delta_put:+,.0f}</b>'
        f' ／ 净 <b>{sr.eff_delta_total:+,.0f}</b>'
        f'　—— 纯算术，不需判断谁是主动方，与「按 IV 推断主动方」互为参照</div>'
        f'<h3>防守强度依据</h3><ul class="sub">{why}</ul>'
        f'<h3>趋势转折证伪清单</h3>'
        f'<div class="sub">真正的趋势下杀应<b>同时</b>出现放量 + ATM IV 大涨 + '
        f'主翼 put 大幅变贵；❓=数据不足测不了，不算「不满足」</div>'
        f'<table><tbody>{checks}</tbody></table>'
        f'<h3>可用腿（噪音与低可靠度贡献恒为 0）</h3>{legs_html}'
        '<div class="sub" style="margin-top:8px">⚠️ 两条反直觉的判读规则：'
        '<b>①远虚 Put 加保护 ≠ 看跌到那个价位</b>，只是防那段风险，位置比数量重要；'
        '<b>②成交量大 + OI 变化大 ≠ 有方向信息</b>——多数是调仓换月。</div>'
        '</div>')


def _val_badge(key: str) -> str:
    """取验证状态串；模块缺失时明说，绝不静默返回空串。"""
    try:
        from undertow.analyze.validation import badge
        return badge(key)
    except Exception as e:
        return f"⚠️ 验证状态读取失败（{type(e).__name__}）——不得作为交易依据"


def render_strong_signal_banner(ss, display_name: str = "", stale_note: str = "") -> str:
    """近端资金流强信号置顶红/绿告警（一边倒时才由 detect_strong_signal 产出）。

    动机：综合投票会把这种一边倒的领先信号对冲成"分歧/中性"而埋没（复盘 8/19 黄金），
    故独立置顶、显著标识。背离时额外提示"近端资金流领先、可能抢跑于慢因子"。
    """
    if ss is None:
        return ""
    up = ss.direction == "看涨"
    lowc = bool(getattr(ss, "low_confidence", False))
    # 低置信（方向裁决的软条件未过）→ 降为琥珀色"未校准异常"，不占红色置顶。
    # 检测口径不变，只是可执行性不同（codex review 2026-08-27）。
    if lowc:
        accent, bg = "#bf8700", "#fff8c5"
    else:
        accent = "#1a7f37" if up else "#cf222e"
        bg = "#e6f4ea" if up else "#ffebe9"
    arrow = "▲" if up else "▼"
    reasons = "".join(f'<li style="margin:2px 0">{_esc(r)}</li>' for r in ss.reasons)
    diverge = ""
    # 与近端一致、只和中期冲突 —— 这是最常见也最容易被误读成"自相矛盾"的情形，
    # 必须显式说出来（2026-08-27 QQQ 正是如此：近端偏空(弱)、中期偏多）。
    if not ss.diverges and getattr(ss, "conflicts_mid", False):
        diverge = (
            f'<div style="margin-top:8px;padding:8px 10px;background:#ddf4ff;'
            f'border-radius:6px;font-size:13px;color:#0a3069">'
            f'ℹ <b>与近端方向一致</b>（近端＝{_esc(ss.outlook_bias or "—")}），'
            f'<b>只与中期冲突</b>（中期＝{_esc(ss.mid_bias or "—")}）：'
            f'这不是自相矛盾，而是<b>时间尺度不同</b>——'
            f'中期看的是 COT 持仓与宏观（周频、慢），近端看的是当日期权链。'
            f'本层<b>未经回测校准</b>，不足以推翻中期结论；'
            f'合理读法是「上升背景里的短线风险窗口」。</div>'
        )
    elif ss.diverges:
        diverge = (
            f'<div style="margin-top:8px;padding:8px 10px;background:#fff8c5;'
            f'border-radius:6px;font-size:13px;color:#7d4e00">'
            f'⚠ <b>与近端方向不同向</b>（近端＝{_esc(ss.outlook_bias or "—")}）：'
            f'连当日的近端层都没站在这一侧。<b>本层未经回测校准</b>'
            f'（核心闸门需历史逐行 OI，免费源拿不到，正在用 signal_ledger 向前累积样本）——'
            f'"领先"只是一次黄金复盘得来的猜想，<b>没有统计证据</b>，'
            f'不足以据此推翻已校准的综合研判与超买超卖层。</div>'
        )
    if lowc:
        diverge = (f'<div style="margin-top:8px;padding:8px 10px;background:#fff8c5;'
                   f'border-radius:6px;font-size:13px;color:#7d4e00">'
                   f'⚠️ <b>本告警为「低置信」</b>：当日方向裁决的软条件未通过'
                   f'（压力比不足或两个口径反向），而这些阈值<b>全部未经校准</b>。'
                   f'按项目规矩，未校准的判据只记录、不正式裁决 —— '
                   f'因此本条<b>降级为观察项</b>，不作为可执行告警。</div>') + diverge
    if stale_note:
        diverge = (f'<div style="margin-top:8px;padding:8px 10px;background:#fff8c5;'
                   f'border-radius:6px;font-size:13px;color:#7d4e00">'
                   f'⚠️ <b>本告警已过期</b>：{_esc(stale_note)}'
                   f'——<b>不是今日可执行的信号</b>。</div>') + diverge
    name = f'{_esc(display_name)} · ' if display_name else ""
    return (
        f'<div class="card" style="border:2px solid {accent};background:{bg}">'
        f'<div style="font-size:20px;font-weight:800;color:{accent}">'
        f'⚡ {name}近端资金流 <span style="font-size:23px">{arrow} {_esc(ss.level)}{_esc(ss.direction)}</span></div>'
        f'<div class="sub" style="margin:4px 0 6px">期权端"一边倒"教科书组合 · '
        f'加权增仓比 {ss.pressure_ratio}× · 主翼买卖比 {ss.wing_ratio}×'
        f'{" · 波动率面追认" if ss.vol_confirms else ""}</div>'
        f'<ul style="margin:6px 0 0;padding-left:20px;font-size:13.5px">{reasons}</ul>'
        f'{diverge}'
        # 验证状态必须贴在结论旁边 —— 这个横幅是全报告最容易被当成
        # 可执行指令的地方（用户 2026-08-31：「照着做交易的时候，你又说这其实是不准的」）
        f'<div style="margin-top:8px;padding:7px 10px;background:#ffffffaa;'
        f'border-radius:6px;font-size:12.5px">{_esc(_val_badge("strong_signal_dir"))}</div>'
        f'<div class="sub" style="margin-top:8px;font-size:12px">'
        f'口径：近月主翼(20~45Δ)买卖方加权。⚠️ 它与加权增仓用同一套压力数（实测方向 100% 共线），**不是第二份独立证据**；方向裁决弃权时本告警不会出现 · 波段级情景预警，非交易指令</div>'
        f'</div>'
    )


def render_validation_table() -> str:
    """验证状态总览：哪些能信、哪些不能、还差多少样本才能定论。

    起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，
    你又说这其实是不准的。现在的瓶颈到底是什么？」
    在此之前，八个投票因子里只有一个有完整回测记录（样本量+p 值），
    其余权重是拍的，而报告却用「按回测校准的可信度加权」的说法呈现。
    这张表把每条判断的实际成绩摊开，包括不及格的。
    """
    from undertow.analyze.validation import REGISTRY
    rows = []
    for v in REGISTRY.values():
        if v.kind == "corr":
            icon = "✅" if v.significant else "🟡"
            score = f"r={v.r:+.3f}"
            extra = (("原始 p 显著但未做簇修正　" if v.status == "待簇修正" else "")
                     + (f"对照 r={v.r_control:+.3f}" if v.r_control is not None else ""))
        elif v.hits is None:
            # codex 2026-09-01 P0：这类条目 hits/p_value/baseline 全是 None。
            # 不得走命中率分支 —— f"{None:.0%}" 会 TypeError 让整份研报崩掉，
            # 而用 0 冒充又会把「没测过」显示成「测了，一次没中」。
            # 判据是 hits is None（不是 status）：对照型条目有 p 值、无命中数，
            # status 会是「样本不足」，走命中率分支照样 TypeError（2026-09-02）。
            icon = "⛔" if v.p_value is None else ("🟡" if not v.significant else "✅")
            score = f"n={v.n} · 对照型" if v.p_value is not None else f"n={v.n} · 未检验"
            extra = ("缺可用的零假设或前瞻收益，见 note 中的复现入口"
                     if v.p_value is None else
                     ("差异显著" if v.significant else "两组无法区分"))
        else:
            icon = {"已验证": "✅", "待簇修正": "🟠",
                    "样本不足": "🟡", "未验证": "⚠️"}.get(v.status, "⚠️")
            score = f"{v.hits}/{v.n} = {v.rate:.0%}"
            more = v.need_more
            extra = ("已达显著" if v.status == "已验证" else
                     ("原始 p 显著但未做簇修正" if v.status == "待簇修正" else
                      (f"还差 {more} 个样本" if more else "命中率贴近基准，难以证实")))
        color = "#1a7f37" if v.significant else "#bc4c00"
        p_cell = "—" if v.p_value is None else f"{v.p_value:.3f}"
        rows.append(
            f'<tr><td>{icon} {_esc(v.label)}</td>'
            f'<td class="r">{score}</td>'
            f'<td class="r">{p_cell}</td>'
            f'<td style="color:{color}">{_esc(extra)}</td>'
            f'<td class="sub">{_esc(v.caveat)}</td></tr>')
    return (
        '<div class="card"><h2>验证状态总览 · 哪些能信，哪些不能</h2>'
        '<div class="sub">每条会影响交易决策的判断，都要在这里登记实测成绩。'
        '未登记的一律不得作为依据。p ≥ 0.05 时给出「还差多少样本」，'
        '把「什么时候能信」变成具体数字。</div>'
        '<table style="margin-top:8px"><tr><th>判断</th><th>实测</th><th>p 值</th>'
        '<th>状态</th><th>已知问题</th></tr>' + "".join(rows) + '</table>'
        '<div class="sub" style="margin-top:8px;line-height:1.7">'
        '「还差多少样本」假设命中率原地不变，是<b>乐观下界</b>；'
        '要在 80% 功效下确证需要更多（强信号那条：乐观 +11，功效口径 +21）。'
        '<br>⚠️ 全部数字来自 2026-08-31 的回测，样本区间 2026-06-25 起。'
        '「已验证」不等于可以照着下单——闸门那条虽 p=0.044，但测了 10 个阈值，'
        'Bonferroni 校正后就不显著了。</div></div>')


def render_vintage_banner(sess_date: str, trade_date: str, today: str) -> str:
    """数据时效横幅。**过期的信号不得以"当前告警"的面目出现。**

    时序（见 flow.py 顶部约定）：快照对 (X, X+1) 描述交易日 X 的持仓变化，
    在 X+1 开盘前可读 → 可交易日 = X+1。若 X+1 已经过去，这份判读就已经错过。

    动机（2026-08-27 用户实测）：SLV 因 OCC 尚未结算 8/27 的 OI，管线仍拿
    (8/25, 8/26) 当"最新"，于是在 8/27 早晨弹出 ⚡极强看跌 —— 但它描述的是
    **8/25 交易日**、本该在 **8/26 开盘**交易，已过期一天，而报告毫无提示。
    同一批报告里 wti/qqq/tqqq/tlt 是 8/26 的数据、gold/silver 是 8/25 的，
    **混龄并排展示，看不出区别**。
    """
    if not (sess_date and trade_date and today):
        return ""
    fresh = trade_date >= today
    if fresh:
        return (f'<div class="sub" style="margin-top:4px">数据时效：'
                f'描述 <b>{_esc(sess_date)}</b> 交易日的持仓变化 · '
                f'可交易日 <b>{_esc(trade_date)}</b>（今日）✅</div>')
    return (
        f'<div style="margin-top:8px;padding:9px 11px;background:#fff8c5;'
        f'border-left:4px solid #bf8700;border-radius:6px;font-size:13px;color:#7d4e00">'
        f'⚠️ <b>数据已过期</b>：本报告基于 <b>{_esc(sess_date)}</b> 交易日的持仓变化，'
        f'其可交易时点是 <b>{_esc(trade_date)}</b> 开盘，而今天是 {_esc(today)} —— '
        f'<b>已经错过</b>。原因通常是该品种的 OCC 隔夜结算尚未落地，管线仍在用上一对快照。'
        f'下方任何方向判读与 ⚡ 告警<b>都不是今日可执行的信号</b>，'
        f'等新快照到位后会自动刷新。</div>'
    )


def render_report_html(o: Outlook, price_svg: str, oi_svg: str, cot_svg: str,
                       flow_html: str = "", macro_html: str = "", events_html: str = "",
                       tldr_html: str = "", strategy_html: str = "",
                       conc_html: str = "", volregime_html: str = "",
                       vol_analysis_html: str = "", expiry_html: str = "",
                       fib_html: str = "", strong_html: str = "", struct_html: str = "",
                       verdict_html: str = "", tech_html: str = "",
                       stretch_read=None, vintage_html: str = "",
                       indicators_html: str = "",
                       expiry_html2: str = "", summary_html: str = "",
                       layers_html: str = "", gate_html: str = "",
                       cost_html: str = "", backmonth_html: str = "",
                       ratio_html: str = "", credit_wall_html: str = "",
                       wall_hist_html: str = "",
                       wall_spread_html: str = "",
                       smc_html: str = "") -> str:
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
        f'{vintage_html}'
        f'{basis_line}'
        f'<div style="margin:10px 0">{_bias_badge(o)}</div>'
        + (f'<div style="margin:8px 0 2px">{stretch_pill(stretch_read)}</div>'
           if stretch_read is not None else "")
        + f'<div class="sub">环境：{_esc(o.regime)}</div></div>'
    )
    body = (
        # 可交易信息闸门排在最前 —— 它决定下面所有方向结论该给多少信任。
        # 横盘日信号胜率 50%，有行情日 70~100%；不先说清今天属于哪种，
        # 后面的「⚡极强看跌」会被当成可执行结论（用户 2026-08-31 的原话：
        # 「照着做交易的时候，你又说这其实是不准的」）。
        f'{gate_html}'
        # 成本闸门紧跟可交易闸门：前者说今天有没有信息，后者说这信息值不值得下单。
        # 用户 2026-08-31 那晚四笔方向基本都对却全亏，差的就是这张表。
        f'{cost_html}'
        f'{strong_html}'
        # ── 板块顺序（用户 2026-08-29 指定，2026-08-31 在最前面插入分层）──────
        #   ① 期权结构按到期分层  ② 期权关键点位  ③ 综合研判  ④ 大白话  ⑤ 增仓按到期拆开
        #   先看"墙在哪"，再看"怎么判"，然后是人话，最后才是拆解。
        #   此前顺序是 结构读数→决策研判→到期拆开→指标说明→大白话→…→关键位点，
        #   关键位点排到第 7 位，而它恰恰是最该先看的东西。
        #   2026-08-31 追加分层卡片置于最顶：关键位点表里的墙来自混算口径，
        #   而混算会造出实盘不存在的墙（见 render_wall_layers_section），
        #   所以必须先让人看到"这墙属于哪个到期层"，再看汇总表。
        f'{layers_html}'
        # 墙位历史图紧跟分层卡（用户 2026-08-31：「这个历史墙位图我觉得挺重要，
        # 可以放进研报里，期权结构的下面」）。它回答静态墙位表答不了的问题：
        # 这道墙守住过没有、被破过几次、破的时候有没有信号。
        f'{wall_hist_html}'
        # 技术面第二视角紧跟墙位历史 —— 两者都在回答"关键位在哪"，
        # 一个用持仓、一个用 K 线结构，放一起便于对照（但不合并计票）
        f'{smc_html}'
        # 卖方价差紧跟期权结构 —— 它直接拿上面那张卡的墙位下单（用户 2026-08-31
        # 「在研报里加上我们刚做的模块，就在期权结构下面」）
        f'{wall_spread_html}'
        f'{credit_wall_html}'
        f'<div class="card"><h2>③ 期权关键点位（吸附/支撑/阻力/翻转）</h2>{_levels_table(o)}'
        f'<div class="chart">{price_svg}</div>'
        f'<div class="chart">{oi_svg}</div></div>'
        f'{summary_html}'
        f'{verdict_html}'
        + (f'<div class="card">{indicators_html}</div>' if indicators_html else "") +
        f'{tldr_html}'
        + (f'<div class="card">{expiry_html2}</div>' if expiry_html2 else "") +
        f'{struct_html}'
        f'{events_html}'
        # 紧跟价格图：先看价在哪，再看它离自己的常态有多远
        f'{tech_html}'
        f'{flow_html}'
        f'{expiry_html}'
        f'{vol_analysis_html}'
        f'{volregime_html}'
        f'<div class="card"><h2>方向因子投票（按回测可信度加权）</h2>{_votes_table(o)}</div>'
        f'{macro_html}'
        f'<div class="card"><h2>持仓结构</h2>{conc_html}<div class="chart">{cot_svg}</div></div>'
        f'<div class="card"><h2>情景推演（规则化 if-then，非点位预言）</h2>{_scenarios_html(o)}</div>'
        f'{strategy_html}'
        f'{fib_html}'
        f'{ratio_html}'
        f'{backmonth_html}'
        + render_validation_table() +
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


def _near_color(ns) -> str:
    """badge 底色跟【近端】走 —— 综合已不显示，颜色不该再由它决定。"""
    if not isinstance(ns, (int, float)):
        return "#6e7781"
    return "#1a7f37" if ns >= 0.8 else ("#cf222e" if ns <= -0.8 else "#6e7781")


def _facts_html(fx: dict) -> str:
    """索引卡的【事实块】：墙在哪、比昨天厚了还是薄了、昨天新建仓 call/put 谁多、最大几笔在哪。

    动机（用户 2026-08-28）：索引页原来每个品种都是「不做空 · 回调买 · 长线拿住」，
    八个品种一模一样，用户原话「都没有意义」。这里改成只说事实、说人话，
    不出自造词（不写「加权增仓」「delta 口径」这类只有我自己懂的词）。
    """
    if not fx:
        return ""
    rows = []

    def _n(v):
        return f"{v:,.0f}"

    def _chg(v):
        return f"持平" if v == 0 else (f"加厚 {_n(v)}" if v > 0 else f"减薄 {_n(-v)}")

    wall = []
    for side, label, col in (("put", "下方支撑", "#1a7f37"), ("call", "上方阻力", "#cf222e")):
        k = fx.get(f"{side}_wall")
        if not k:
            continue
        oi, ch = fx.get(f"{side}_wall_oi", 0), fx.get(f"{side}_wall_chg", 0)
        emph = "font-weight:700" if abs(ch) >= 5000 else ""
        frm = fx.get(f"{side}_wall_from")
        if frm:
            dirw = "下移" if frm > k else "上移"
            move = f'<b style="color:#bf8700">从 {frm:g} {dirw}到 {k:g}</b>'
        else:
            move = f'<b>{k:g}</b>'
        wall.append(f'<span style="color:{col};{emph}">{label} {move} '
                    f'{_n(oi)} 张（{_chg(ch)}）</span>')
    if wall:
        rows.append("🧱 " + "　｜　".join(wall))

    # 持续墙：排除临近到期后的承接/压制区。上面那行的墙可能被 0DTE 劫持 ——
    # 当日到期的墙收盘即归零，它是当天的 pin，不是下周的承接区。
    pw = fx.get("persist") or {}
    if pw.get("put_wall") or pw.get("call_wall"):
        bits = []
        if pw.get("put_wall"):
            bits.append(f'<span style="color:#1a7f37">下方承接 '
                        f'<b>{pw["put_wall"]:g}</b> {_n(pw["put_wall_oi"])} 张</span>')
        if pw.get("call_wall"):
            bits.append(f'<span style="color:#cf222e">上方压制 '
                        f'<b>{pw["call_wall"]:g}</b> {_n(pw["call_wall_oi"])} 张</span>')
        # ⚠️ 两侧都要比。上一版只比 put：若 put 相同而 call 墙因 0DTE 改变，
        # 页面不会警告（codex 2026-08-29 P1-8）。
        diff = []
        if pw.get("put_wall") is not None and pw["put_wall"] != fx.get("put_wall"):
            diff.append("下方")
        if pw.get("call_wall") is not None and pw["call_wall"] != fx.get("call_wall"):
            diff.append("上方")
        note = ("" if not diff else
                f'　<b style="color:#bf8700">⚠️ {"、".join(diff)}与上面的墙不同 —— '
                f'上面那个含临近到期，收盘即失效</b>')
        rows.append(f'⏳ <b>{pw.get("min_dte", 7)} 天以上才算数</b>（波段用）：'
                    + "　｜　".join(bits) + note)

    # ⚠️ 报 call/put 张数比会误导 —— 白银 2026-08-28 的教训：index 写
    # 「call 是 put 的 2.2 倍」看着像看涨，可那批 call（69C/67C）全是【卖方压制】，
    # 卖上方 call 是看跌动作。当天白银 -4.38%。所以主行必须按【买卖方】分侧，
    # call/put 张数只作为括号里的补充。
    be, bu = fx.get("bear_add"), fx.get("bull_add")
    ca, pa = fx.get("call_add"), fx.get("put_add")
    if be is not None and bu is not None and (be or bu):
        if be > bu * 1.3:
            who = f"<b>看跌侧是看涨侧的 {be / max(bu, 1):.1f} 倍</b>"
        elif bu > be * 1.3:
            who = f"<b>看涨侧是看跌侧的 {bu / max(be, 1):.1f} 倍</b>"
        else:
            who = "两边势均力敌"
        raw = (f"；张数上 call +{_n(ca)} / put +{_n(pa)}"
               if ca is not None and pa is not None else "")
        rows.append(f"📊 昨天新建仓（按买卖方分侧）：看跌 +{_n(be)} 张 vs "
                    f"看涨 +{_n(bu)} 张（{who}{raw}）")
        # 「看跌侧＝买put＋卖call」的定义已在页尾图例里，此处不重复（精简）
        # 与综合结论相反时【自己说出来】。用户 2026-08-27 的核心抱怨就是
        # 「指标之间互相打架」——藏起来不会让矛盾消失，只会让人自己撞上。
        # 白银 2026-08-28 正是这样：综合写「偏多」，持仓流是看跌 2.4 倍，当天 -4.38%。
        # ⚠️ 比对对象必须是【中期】，不是综合、也不是近端：
        #   · 和近端比是同义反复 —— 增仓本来就是近端的组成部分；
        #   · 综合已从索引页移除（它就是把两层压成一个字的那个东西）。
        mid = fx.get("mid_bias", "")
        lean = 1 if bu > be * 1.3 else (-1 if be > bu * 1.3 else 0)
        msign = 1 if "偏多" in mid else (-1 if "偏空" in mid else 0)
        if lean and msign and lean != msign:
            rows.append(f'<span style="color:#bf8700;font-weight:700">'
                        f'　　⚠️ 这与中期结论「{_esc(mid)}」方向相反 —— '
                        f'短线资金和中期持仓不同步，按你自己的持仓周期择一为主</span>')

    # 到期一致性：index 上只给【一个标记】，四个桶的明细留给品种研报
    # （用户 2026-08-29：「index 可以不用拆开这么多到期桶，如果全部同向可以
    #   额外标注一下。我觉得合并已经很有价值了。品种内部研报可以再拆开」）
    # 🧱 墙附近三个区域的原始读数 —— **只摆事实，不下结论**（codex 2026-08-29 P0）。
    #    上一版在这里输出「保护向墙下搬家 / 在给跌破定价 / 下一道防线」+ 红色高亮，
    #    用的还是那个回测 30 次只中 1 次、已决定不上线的预测器的门槛 ——
    #    换个名字上线而已。现在不分类、不预测、不用方向色。
    # 🧱 put 墙附近三区域读数已移出 index（用户 2026-08-31：「index 里现在信息
    # 太过复杂，稍微精简一点」）。它自己的文案就写着「这里不做任何判断」——
    # 纯研究性内容不该占索引页的位置，改由品种报告的资金流一节承载。
    # fx["migration"] 仍会算出并传下来，只是不在这里渲染。

    # 💵 卖方价差候选：index 只给一行最优腿位，明细在品种报告
    # （用户 2026-08-31：「在研报里加上我们刚做的模块」+「index 精简一点」）
    cw = fx.get("credit_wall")
    if cw:
        bits = []
        for tier, lab, col in (("conservative", "稳健", "#1a7f37"),
                               ("aggressive", "激进", "#cf222e")):
            d = cw.get(tier)
            if not d:
                continue
            bits.append(f'<span style="color:{col}"><b>{lab}</b> '
                        f'卖{d["sell"]:g}/买{d["buy"]:g}{d["kind"]} '
                        f'{d["expiry"]}（{d["dte"]}天）收 ${d["credit"]:.0f} '
                        f'占用 ${d["occ"]:.0f} 缓冲 {d["buffer"]:.1f}%</span>')
        if bits:
            rows.append('💵 <b>卖方价差候选</b>　' + '　｜　'.join(bits))
    elif fx.get("credit_wall_blocked"):
        rows.append(f'<span style="color:#6e7781">💵 卖方价差：'
                    f'{_esc(fx["credit_wall_blocked"])}</span>')

    # 主力到期：谁占了当天增仓的大头 —— 由数据决定，不由固定分桶决定
    dom = fx.get("dominant")
    if dom:
        when = ("当日到期" if dom["dte"] == 0 else
                ("次日到期" if dom["dte"] == 1 else f'{dom["dte"]}天后到期'))
        w = "看跌" if dom["sign"] < 0 else ("看涨" if dom["sign"] > 0 else "两边平")
        col = "#cf222e" if dom["sign"] < 0 else ("#1a7f37" if dom["sign"] > 0 else "#6e7781")
        cut = dom.get("cut") or 0
        cut_txt = (f'　<span style="color:#6e7781">（同期该到期还减仓 {cut:,} 张）</span>'
                   if cut else "")
        rows.append(f'🎯 <b>新增仓位最集中在 {_esc(dom["expiry"])}</b>（{when}），'
                    f'占当天<b>新增</b> <b>{dom["share"]*100:.0f}%</b>'
                    f'<span style="color:#6e7781;font-size:11px">（40% 门槛未校准）</span>'
                    f'　<b style="color:{col}">{w} {dom["ratio"]:.1f}×</b>{cut_txt}')
    elif fx.get("exp_split"):
        rows.append('<span style="color:#6e7781">🎯 无单一主力到期 —— '
                    '增仓分散在多个到期上</span>')

    sp = fx.get("exp_split") or []
    agree = fx.get("exp_agreement")          # agree / conflict / insufficient
    if agree == "insufficient":
        rows.append('<span style="color:#6e7781">📆 有效到期桶不足，'
                    '无法判断跨到期一致性</span>')
    elif sp and agree:
        if agree == "conflict":
            rows.append('<b style="color:#bf8700">📆 各到期方向不一致</b>'
                        '<span style="color:#6e7781">'
                        ' —— 上面这个合并读数是把矛盾抹平后的结果，别当强信号看'
                        '（明细见品种研报）</span>')
        else:
            n_b = len([b for b in sp if b["sign"]])
            word = "看跌" if sp[0]["sign"] < 0 else "看涨"
            rows.append(f'<b style="color:#1a7f37">✅ {n_b} 个到期桶【全部同向{word}】'
                        f'</b><span style="color:#6e7781">'
                        f' —— 不是某个到期的孤立现象，是全曲线共识</span>')

    legs = fx.get("big_legs") or []
    if legs:
        parts = []
        for g in legs:
            d = abs(g.get("delta") or 0)
            # 说清这笔到底有多少【实际方向暴露】——这是判断 put 增仓是真看空
            # 还是买张彩票防黑天鹅的关键，不能只报张数。
            # ⚠️ 判据必须是 Delta 不是离现价的%：同样 -2%，今天到期的 Delta 近 0，
            #    一个月后到期的可能有 0.3。用%会把两者混为一谈。
            days = g.get("dte")
            # ⚠️ 2026-08-28 黄金的教训：我把 413P/408P（次日到期、Δ0.10/Δ0.04、
            # 共 7.8 万张、IV 被买涨 +3.63pp）标成「彩票」，说它几乎没方向暴露。
            # 次日 GLD -3.24%，收 408.89，正好打穿 408。判断反了：
            #   · 长到期 + 低 Δ  = 真尾部险，防黑天鹅，方向信息弱
            #   · 短到期 + 低 Δ + 大量新开仓 = 只有【立刻】大跌/大涨才值钱，
            #     方向信息最强。用只适用于长到期的判据去贴短到期，
            #     会把最强的信号贴成最弱的。
            if days is not None and days <= 2 and d < 0.15:
                tag = "⚠️赌急跌" if g["kind"] == "P" else "⚠️赌急涨"
            elif d < 0.10:
                tag = "远期尾部险"
            elif d < 0.30:
                tag = "半仓"
            else:
                tag = "实打实"
            when = "今日到期" if days == 0 else (f"{days}天后到期" if days is not None else "")
            parts.append(f'<b>{g["strike"]:g}{g["kind"]}</b> +{_n(g["d_oi"])}'
                         f'（{g["pct"]:+.0f}%·{when}·Δ{d:.2f}·{tag}）')
        rows.append("🎯 最大几笔：" + " · ".join(parts))

    if not rows:
        return ""
    return ('<div style="margin-top:7px;font-size:12.5px;line-height:1.75;color:#24292f">'
            + "<br>".join(rows) + "</div>")


def render_summary_card(it: dict) -> str:
    """研报顶部的「综合研判」卡片 —— 与 index 卡片同源同内容。

    用户 2026-08-29：「详细研报里，综合研判更新一下。可以就是 index 里的。」
    此前研报里的「当日决策研判」是 verdict 的四问（不做空/别追/短线/长线），
    与 index 上摆的事实（近端/中期分层、六组 label、墙形态、增仓分侧）完全脱节 ——
    同一份数据两套说法，正是用户反复抱怨的"指标互相打架"的观感来源。
    """
    nb, mb = it.get("near_bias", ""), it.get("mid_bias", "")
    ns, ms = it.get("near_score"), it.get("mid_score")
    _f = lambda v: f"({v:+.1f})" if isinstance(v, (int, float)) else ""
    _sgn = lambda t: 1 if "偏多" in t else (-1 if "偏空" in t else 0)
    split = bool(nb and mb and _sgn(nb) * _sgn(mb) < 0)
    near_edge = (isinstance(ns, (int, float)) and _sgn(nb) == 0 and abs(ns) >= 0.5)
    edge = (f'　<b style="color:#bf8700">近端实为{"偏空" if ns < 0 else "偏多"}倾向，'
            f'仅差 {abs(abs(ns) - 0.8):.1f} 未过门槛</b>' if near_edge else "")
    labels_div = ""
    if it.get("labels"):
        from undertow.analyze.indicators import render_pills as _pills
        labels_div = _pills(it["labels"], _esc, scores=it.get("scores"))
    return (
        '<div class="card">'
        '<h2>② 综合研判（与索引页同源）</h2>'
        f'<span class="badge" style="background:'
        f'{"#bf8700" if (split or near_edge) else _near_color(ns)}">'
        f'近端 {_esc(nb or "—")}{_f(ns)} ｜ 中期 {_esc(mb or "—")}{_f(ms)}'
        f'{" ⚠分歧" if split else ""}</span>{edge}'
        f'{labels_div}'
        f'{_facts_html(it.get("facts") or {})}'
        '</div>')


def render_index_html(items: list[dict], asof: str, *, family_notes=None,
                      ratio_html: str = "", events=None, today=None) -> str:
    """多品种综合研报（每品种一句话摘要 + 强信号置顶），非仅链接。

    items=[{name, fn, bias, conf, summary, signal}]，signal 为 StrongSignal|None。
    有强信号的品种置顶并显著标识；其余按可信度默认顺序。
    """
    # ── 临近事件横幅（用户 2026-09-02：重要数据新闻要进 index）──────────
    # index 是每天第一眼看的页面，而 ADP/非农/CPI/FOMC 会在几分钟内改写方向。
    # 只放 3 天内的 🔴高 / 🟡中，再远的留给品种研报里的事件雷达。
    ev_html = ""
    if events and today:
        near = [e for e in events if 0 <= e.days_until(today) <= 3]
        near = [e for e in near if str(getattr(e, "importance", "")).lower()
                in ("high", "medium")] or near[:4]
        if near:
            rows = []
            for e in sorted(near, key=lambda x: (x.days_until(today), str(x)))[:6]:
                d = e.days_until(today)
                when = "今天" if d == 0 else f"T+{d}"
                hi = str(getattr(e, "importance", "")).lower() == "high"
                col = "#cf222e" if hi else "#bf8700"
                t = getattr(e, "time_et", "") or ""
                rows.append(
                    f'<div style="padding:3px 0"><b style="color:{col}">'
                    f'{"🔴" if hi else "🟡"} {when}</b> '
                    f'<code>{_esc(str(getattr(e, "date", ""))[:10])} {_esc(t)}ET</code> '
                    f'{_esc(getattr(e, "name", "") or str(e))}'
                    + (f' <span class="sub">预测 {_esc(e.forecast)}'
                       f'{" / 前值 " + _esc(e.previous) if getattr(e, "previous", "") else ""}'
                       f'</span>' if getattr(e, "forecast", "") else "")
                    + '</div>')
            ev_html = (
                '<div class="card" style="border-left:4px solid #cf222e">'
                '<h2>⚠️ 临近数据/事件 · 三天内</h2>'
                '<div class="sub" style="line-height:1.7">'
                '这些时点会在几分钟内改写方向，也会让期权 IV 与墙位失真。'
                '事件前后请主动降置信、缩仓位；卖方价差尤其要避开在数据前开新仓。'
                '</div><div style="margin-top:8px;font-size:13px">'
                + "".join(rows) + '</div></div>')

    # 强信号品种置顶（看涨绿/看跌红），醒目
    alerts = []
    for it in items:
        ss = it.get("signal")
        if ss is None:
            continue
        up = ss.direction == "看涨"
        accent = "#1a7f37" if up else "#cf222e"
        bg = "#e6f4ea" if up else "#ffebe9"
        arrow = "▲" if up else "▼"
        div = ' · 与综合背离(近端领先)' if ss.diverges else ""
        if getattr(ss, "low_confidence", False):
            div += ' · ⚠️低置信（方向裁决本身没过门槛）'
        alerts.append(
            f'<a class="card" style="display:block;text-decoration:none;color:inherit;'
            f'border:2px solid {accent};background:{bg}" href="{_esc(it["fn"])}">'
            f'<div style="font-size:16px;font-weight:800;color:{accent}">'
            f'⚡ {_esc(it["name"])} 近端新建仓一边倒 {arrow} {_esc(ss.level)}{_esc(ss.direction)}</div>'
            f'<div class="sub" style="margin-top:3px">'
            f'{"看跌" if not up else "看涨"}方向的新建仓是反方向的 {ss.pressure_ratio}× · '
            f'贴近现价那几档买盘/卖盘 {ss.wing_ratio}×{_esc(div)}<br>'
            f'<span style="color:#6e7781;font-size:11px">'
            f'⚠️ 倍数受分母影响很大：2026-08-12 黄金看跌量 250,241 / 看涨 694 = 360.8×，'
            f'而 8/28 看跌 116,492 / 看涨 2,178 = 53.5× —— '
            f'8/12 的绝对量更大，倍数却高 6.7 倍。看倍数也要看绝对量。'
            f'</span></div></a>'
        )

    cards = []
    for it in items:
        name, fn = it["name"], it["fn"]
        bias, conf = it["bias"], it["conf"]
        summary = it.get("summary", "")
        color = _BIAS_COLOR.get(bias, "#6e7781")
        sig_pill = ""
        ss = it.get("signal")
        if ss is not None:
            up = ss.direction == "看涨"
            sig_pill = (f'<span class="pill" style="background:{"#1a7f37" if up else "#cf222e"};'
                        f'color:#fff">⚡{_esc(ss.level)}{_esc(ss.direction)}</span>')
        os_pill = stretch_pill(it.get("stretch"), compact=True)
        # 时效标：同一页里各品种的 OCC 结算进度可能不同（混龄），必须一眼可见
        vt = ""
        td, tdy = it.get("trade_date", ""), it.get("today", "")
        if td and tdy and td < tdy:
            vt = (f'<span class="pill" style="background:#bf8700;color:#fff">'
                  f'⚠️过期·数据止于 {_esc(td)}</span>')
        # 近端/中期分层上索引页：综合 bias 是两层投票的合成，只看它会把
        # "近端偏空、中期偏多"这种分歧压成一个字，正是矛盾感的来源之一。
        nb, mb = it.get("near_bias", ""), it.get("mid_bias", "")
        layer_pill = ""
        split = False
        if nb or mb:
            # ⚠️ 必须比方向【符号】，不能比字符串："偏多" 与 "偏多(弱)" 同向、强弱不同，
            # 字符串不等会把它误判成方向冲突。
            _sgn = lambda t: 1 if "偏多" in t else (-1 if "偏空" in t else 0)
            split = bool(nb and mb and _sgn(nb) * _sgn(mb) < 0)
            # ⚠️ **带上分数**。白银 2026-08-28 就栽在这：近端 −0.7 差 0.1 没够到
            # −0.8 的门槛，被显示成"中性"，于是 split=False、分层 pill 也不出现，
            # 用户只看到一个"偏多"。可它的实际构成是：看跌 1 票全来自 Flow，
            # 看涨 3 票全来自 Macro，Gamma 内部抵消 —— 那天白银 −4.38%。
            # 门槛本身是拍的，−0.7 与 0.0 都叫"中性"却是两回事，必须让人看见数字。
            ns, ms = it.get("near_score"), it.get("mid_score")
            _f = lambda v: f"({v:+.1f})" if isinstance(v, (int, float)) else ""
            # 分数已明确偏向、但没够到门槛 → 单独标出来，别让它悄悄变成"中性"
            near_edge = (isinstance(ns, (int, float)) and _sgn(nb) == 0
                         and abs(ns) >= 0.5)
            edge_note = (f'　<b style="color:#bf8700">近端实为'
                         f'{"偏空" if ns < 0 else "偏多"}倾向，仅差 '
                         f'{abs(abs(ns) - 0.8):.1f} 未过门槛</b>' if near_edge else "")
            layer_pill = (f'<span class="pill" style="background:'
                          f'{"#bf8700" if (split or near_edge) else "#57606a"};color:#fff">'
                          f'近 {_esc(nb or "—")}{_f(ns)} / 中 {_esc(mb or "—")}{_f(ms)}'
                          f'{" ⚠分歧" if split else ""}</span>{edge_note}')
        # 结论行去模板化：原来八个品种全是「不做空 · 长线拿住」，改为直接摆事实。
        verdict_div = _facts_html(it.get("facts") or {})
        # 六组互不同源的指标，各一枚小标签（用户 2026-08-29 要求）
        labels_div = ""
        if it.get("labels"):
            from undertow.analyze.indicators import render_pills as _pills
            labels_div = _pills(it["labels"], _esc, scores=it.get("scores"))
        summary_div = (f'<div class="sub" style="margin-top:4px;line-height:1.5">{_esc(summary)}</div>'
                       if summary else "")
        cards.append(
            f'<a class="card" style="display:block;text-decoration:none;color:inherit" href="{_esc(fn)}">'
            f'<h1 style="font-size:17px">{_esc(name)}</h1>'
            # 每个品种都写出近端和中期，哪怕一样也分开写（用户 2026-08-29）。
            # 只给一个综合字，正是"近端偏空、中期偏多"被压成"偏多"的来源；
            # TLT/SPY/IWM 此前就因为两层同向而只显示一个"偏多"。
            # ⚠️ 不再显示「综合」——用户 2026-08-29：「你的综合偏多就算了吧，
            # 这个 label 干嘛用呢？有近端和中期了」。综合正是把两层压成一个字的
            # 那个东西：白银近端 −0.7 看跌、中期 +3.1 看涨，合成出来一个"偏多"，
            # 而它那天 −4.38%。两层摊开，读者自己按持仓周期取舍。
            # （综合分仍在内部用于 verdict/策略层，只是不再上索引页。）
            + (f'<span class="badge" style="background:'
               f'{"#bf8700" if (split or near_edge) else _near_color(ns)}">'
               f'近端 {_esc(nb or "—")}{_f(ns)} ｜ 中期 {_esc(mb or "—")}{_f(ms)}'
               f'{" ⚠分歧" if split else ""}</span>')
            + f'<span class="pill">可信度 {_esc(conf)}</span>{vt}{sig_pill}{os_pill}{edge_note}'
            f'{labels_div}'
            f'{verdict_div}'
            f'{summary_div}'
            f'</a>'
        )

    # 同族不一致提示：紧跟强信号告警之后 —— 它常常正是解释强信号该不该信的那一层
    fam_block = ""
    if family_notes:
        from undertow.analyze.family import render_html as _fam_html
        fam_block = _fam_html(family_notes, _esc)

    alert_block = ""
    if alerts:
        alert_block = (f'<div class="card" style="background:none;border:none;padding:6px 2px 0">'
                       f'<h2 style="margin:0;font-size:15px">⚡ 强信号告警（近端资金流一边倒）</h2></div>'
                       + "".join(alerts))
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>综合研判 {_esc(asof)}</title><style>{_CSS}</style></head>'
        f'<body><div class="wrap"><div class="card"><h1>大宗商品综合研报</h1>'
        f'<div class="sub">{_esc(asof)} · 各品种摘要 + 强信号告警 · 点击进入详情</div></div>'
        # 事件横幅排在强信号之前 —— 数据公布会在几分钟内推翻结构读数，
        # 先让人看见"今晚有雷"，再看方向判断（用户 2026-09-02）
        f'{ev_html}{alert_block}{fam_block}{"".join(cards)}'
        + ratio_html + _horizon_legend() +
        '<div class="foot">undertow · 规则化情景工具，非投资建议 · 纯标准库生成</div></div></body></html>'
    )


def _horizon_legend() -> str:
    """index 底部图例：近端/中期到底怎么分的、分数门槛在哪。

    用户 2026-08-29：「现在中期和近端的定义，你写在 index 最后面，搞个图例。
    每个品种都要写出中期和近端，哪怕一样也要区分写」
    """
    return (
        '<div class="card" style="background:#f6f8fa">'
        '<h2 style="margin:0 0 6px;font-size:15px">📖 图例：近端 / 中期怎么分的</h2>'
        '<table style="width:100%;border-collapse:collapse;font-size:12.5px">'
        '<tr style="background:#fff"><th style="text-align:left;padding:5px 8px">层</th>'
        '<th style="text-align:left;padding:5px 8px">由哪些指标投票</th>'
        '<th style="text-align:left;padding:5px 8px">数据频率</th>'
        '<th style="text-align:left;padding:5px 8px">对谁最重要</th></tr>'
        '<tr><td style="padding:5px 8px"><b>近端</b></td>'
        '<td style="padding:5px 8px">🧱 结构（墙位/伽马）＋ 💰 增仓（当日资金流）</td>'
        '<td style="padding:5px 8px">日频</td>'
        '<td style="padding:5px 8px">短线、高杠杆期权 —— <b>账户小的时候主战场在这里</b></td></tr>'
        '<tr><td style="padding:5px 8px"><b>中期</b></td>'
        '<td style="padding:5px 8px">🏦 大资金（CFTC 持仓）＋ 🌍 宏观（利率/美元）</td>'
        '<td style="padding:5px 8px">周频 / 月频</td>'
        '<td style="padding:5px 8px">底仓、远期期权</td></tr>'
        '</table>'
        '<div class="sub" style="margin-top:8px"><b>分数门槛</b>（两层各自算，'
        '正数看多、负数看空）：'
        '<code>≥+2.0 偏多</code>　<code>≥+0.8 偏多(弱)</code>　'
        '<code>−0.8 ~ +0.8 中性</code>　<code>≤−0.8 偏空(弱)</code>　'
        '<code>≤−2.0 偏空</code>　'
        '两侧都有强票且净分小 → <code>分歧(双向)</code></div>'
        '<div class="sub" style="margin-top:6px">'
        '「综合」是两层合起来投票的结果，<b>不是两层的平均</b>；'
        '近端与中期方向相反时标 ⚠分歧，此时按你自己的持仓周期择一为主。<br>'
        '分数就在括号里 —— <b>门槛是拍的，−0.7 和 0.0 都叫「中性」却是两回事</b>，'
        '所以贴着门槛（|分|≥0.5 却仍判中性）时会单独标出来。</div>'
        '<div class="sub" style="margin-top:6px;color:#9a6700">'
        '⚠️ <b>这个划分是按【数据更新频率】分的，不是按【结论对未来多少天有效】分的。</b>'
        '近端层里混着当日到期和 45 天后到期的仓位；中期层的 COT 是上周五的持仓。'
        '所以「近端」≈「日频数据得出的结论」，不等于「只对明天有效」。'
        '真正按时域重新定义是待办事项。</div>'
        '</div>')


# ————————————————————————————————————————————————————————— 实盘持仓评价

def _advice_html(advice: list) -> str:
    """建议板块（权衡/参考口径，非投资指令）。⚠ 开头的高亮显示。"""
    if not advice:
        return ""
    items = []
    for a in advice:
        warn = a.startswith("⚠")
        col = "#9a6700" if warn else "#24292f"
        items.append(f'<li style="margin:4px 0;color:{col}">{_esc(a)}</li>')
    return ('<div style="margin-top:10px"><div style="font-weight:700;font-size:13px;margin-bottom:2px">'
            '💡 建议 <small>（权衡/参考，非投资指令）</small></div>'
            f'<ul style="margin:2px 0 0;padding-left:20px;font-size:12.5px;line-height:1.6">{"".join(items)}</ul></div>')


def _align_color(a: str) -> str:
    if a == "顺势":
        return "#1a7f37"
    if a == "逆势":
        return "#b62324"
    return "#6e7781"


_SEV_COLOR = {"高": "#cf222e", "中": "#9a6700", "低": "#57606a"}
_SEV_BG = {"高": "#ffebe9", "中": "#fff8c5", "低": "#f6f8fa"}


def _health_html(health) -> str:
    if not health:
        return ""
    n_hi = sum(1 for f in health if f.severity == "高")
    rows = []
    for f in health:
        col = _SEV_COLOR.get(f.severity, "#57606a")
        bg = _SEV_BG.get(f.severity, "#f6f8fa")
        rows.append(
            f'<div style="border-left:4px solid {col};background:{bg};border-radius:0 8px 8px 0;'
            f'padding:8px 12px;margin:8px 0">'
            f'<div style="font-weight:700;color:{col}">[{_esc(f.severity)}] {_esc(f.title)}</div>'
            f'<div style="font-size:12.5px;margin:3px 0">{_esc(f.detail)}</div>'
            f'<div style="font-size:12px;color:#57606a">参考：{_esc(f.suggestion)}</div></div>')
    head = f'🩺 持仓体检（{len(health)} 条' + (f'，含 {n_hi} 条高危' if n_hi else '') + '）'
    return f'<div class="card"><h1>{head}</h1>{"".join(rows)}</div>'


def _greeks_pills(g) -> str:
    """净 Gamma / Theta 的展示。

    光有净 Δ 说不清两件事：这个 Δ 有多脆、这份仓位每天在流血还是收租。

      净Γ < 0（贷方价差，收权金）跳空时 Δ 朝不利方向加速，止损来不及；
      净Γ > 0（借方价差，付权金）跳空对你有利，代价是每天付 Θ。

    ⚠️ 别把"有卖腿"等同于负 Gamma：借方价差里买的那腿更接近平值、gamma 更大，
       整体是**正** Gamma。判断依据是收权金还是付权金。

    Θ 是价签。它相对**净资产**的比例比绝对值更该看 —— 每天 −6.5 美元在
    $6800 的账户上是噪声，在 $68 的账户上是每天 −9.5%。
    """
    out = []
    ng = getattr(g, "net_gamma", None)
    nt = getattr(g, "net_theta", None)
    if ng is not None and abs(ng) > 1e-9:
        # 负 Gamma 要显眼 —— 它是跳空日的真正敌人
        warn = ' style="background:#fff3cd;color:#856404"' if ng < 0 else ""
        out.append(f'<span class="pill"{warn}>净Γ {ng:+.1f}</span>')
    if nt is not None and abs(nt) > 1e-9:
        out.append(f'<span class="pill">净Θ {nt:+.0f}/天</span>')
    return "".join(out)


def render_account_html(review, assets=None, health=None) -> str:
    """实盘持仓理论评价 → 自包含 HTML（本地私有，落 gitignore 的 data/account/）。

    review=PortfolioReview, assets=AccountAssets|None。只作波段级风险情景复盘，非投资建议。
    """
    cards: list[str] = []
    if not review.ok or (not review.groups and not review.unmapped):
        cards.append('<div class="card"><h1>实盘持仓评价</h1><div class="sub">当前无持仓或无法评价</div></div>')
    else:
        head_bits = [f'<div class="sub">基准日 {_esc(review.asof.isoformat())}</div>']
        if assets is not None:
            cash = "、".join(f"{_esc(k)} {v:,.0f}" for k, v in assets.cash_by_ccy.items()) or "—"
            head_bits.append(f'<div class="sub">净资产 ${assets.net_assets:,.0f} · 购买力 '
                             f'${assets.buy_power:,.0f} · 可用现金 {cash}</div>')
        cards.append(
            '<div class="card"><h1>实盘持仓理论评价</h1>'
            f'<div class="sub" style="font-size:14px;color:#24292f;margin:6px 0"><b>{_esc(review.headline)}</b></div>'
            + "".join(head_bits) +
            '<div class="warn" style="margin-top:10px">只作<b>波段级风险情景复盘</b>，非投资建议、非交易指令；'
            '理论中值来自 BS（无 bid/ask），方向与数字来自上游确定性模块。执行永远由你在券商端完成。</div></div>')

        hh = _health_html(health)
        if hh:
            cards.append(hh)

        for g in review.groups:
            d = "—" if g.net_delta is None else f"{g.net_delta:+.0f}"
            pnl = "—" if g.total_pnl is None else f"{g.total_pnl:+,.0f}"
            bcol = _BIAS_COLOR.get(g.bias, "#6e7781")
            rows = []
            for lg in g.legs:
                dte = "—"
                if lg.expiry is not None and lg.dte is not None:
                    dte = f"{_esc(lg.expiry.isoformat())}<br><small>{lg.dte}天</small>"
                acol = _align_color(lg.align)
                pnl_l = "—" if lg.pnl is None else f"{lg.pnl:+,.0f}"
                pcol = "#1a7f37" if (lg.pnl or 0) > 0 else ("#b62324" if (lg.pnl or 0) < 0 else "#6e7781")
                flagtxt = ("<br>" + "<br>".join(f'<span style="color:#b62324">⚠ {_esc(x)}</span>' for x in lg.flags)) if lg.flags else ""
                rows.append(
                    f'<tr><td><b>{_esc(lg.name)}</b><br><small>{_esc(lg.side)} × {lg.qty:g}</small></td>'
                    f'<td>{dte}</td><td>{_esc(lg.moneyness)}</td>'
                    f'<td><small>{_esc(lg.wall_note or "—")}</small></td>'
                    f'<td style="color:{acol};font-weight:700">{_esc(lg.align)}</td>'
                    f'<td class="r" style="color:{pcol}">{pnl_l}</td>'
                    f'<td><small>{_esc(lg.comment)}{flagtxt}</small></td></tr>')
            combo_html = ""
            if g.combos:
                sp = []
                for c in g.combos:
                    mp = "—" if c.max_profit is None else f'<b style="color:#1a7f37">{c.max_profit:+,.0f}</b>'
                    ml = ('<b style="color:#9a6700">风险未封顶</b>' if c.max_loss is None
                          else f'<b style="color:#b62324">{c.max_loss:-,.0f}</b>')
                    sp.append(f'<div class="scn"><b>{_esc(c.label)}</b> '
                              f'<span class="pill">{_esc(c.stance)}</span> '
                              f'<small>{_esc(c.expiry_label)} · {c.qty} 组</small>'
                              f'<div class="t">{_esc(c.note)}</div>'
                              f'<div class="t">最大盈 {mp} · 最大亏 {ml}</div></div>')
                combo_html = "".join(sp)
            stance_html = (f'<div class="warn" style="margin:8px 0;background:#ddf4ff;border-color:#54aeff66">'
                           f'<b>整体姿态</b>：{_esc(g.stance)}'
                           + (f'<br><b>资金</b>：{_esc(g.capital_note)}' if g.capital_note else "")
                           + '</div>') if g.stance else ""
            cards.append(
                f'<div class="card"><h1>{_esc(g.display_name)}<span class="pill">{_esc(g.underlying)}</span></h1>'
                f'<div style="margin:6px 0"><span class="badge" style="background:{bcol}">{_esc(g.bias)}</span>'
                f'<span class="pill">净Δ {d}</span>{_greeks_pills(g)}'
                f'<span class="pill">浮盈亏 {pnl}</span></div>'
                + (f'<div class="sub">🧭 {_esc(g.verdict_head)}</div>' if g.verdict_head else "")
                + (f'<div class="sub">📈 报价源：{_esc(g.price_note)}</div>' if getattr(g, "price_note", "") else "")
                + stance_html + combo_html +
                '<table><thead><tr><th>持仓</th><th>到期</th><th>价性</th><th>行权 vs 墙</th>'
                '<th>顺逆</th><th class="r">浮盈亏</th><th>评价</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>'
                + _advice_html(g.advice)
                + f'<div class="sub" style="margin-top:8px">{_esc(g.summary)}</div></div>')

        if review.unmapped:
            items = "".join(f'<li>{_esc(lg.name)}（{_esc(lg.side)} {lg.qty:g}）</li>' for lg in review.unmapped)
            cards.append('<div class="card"><h2>未接入研判的标的（无 undertow 期权代理，仅列出）</h2>'
                         f'<ul>{items}</ul></div>')

    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>实盘持仓评价</title><style>' + _CSS + '</style></head>'
        '<body><div class="wrap">' + "".join(cards) +
        '<div class="foot">undertow · 实盘理论复盘，只读、非投资建议 · 本地私有未入 git</div></div></body></html>'
    )


_WALL_LAYER_ORDER = (("near", "近端 ≤14天", 1, 14), ("mid", "中端 15-45天", 15, 45),
                     ("far", "远端 >45天", 46, 3650))


def render_wall_layers_section(ga, ladder=None, bands=None, agree=None,
                               conv=None, unit: str = "", etf_symbol: str = "") -> str:
    """期权结构按到期分层 —— 近端置顶。

    起因（用户 2026-08-31）：「期权结构是不是可以分几页，短期近端的期权感觉可以单独拿出来」
    查下去发现不只是排版问题，是口径错：analyze_gamma 把 horizon 内所有到期等权加总，
    同日 GLD 混算 put 墙 400 在近端(405)/中端(350) 任何一层都不是最大值，是加总产物；
    SLV 混算说支撑 55(-8.9%)，本周实际在 60(-0.7%)，照混算读会以为下方有 8.9% 缓冲。
    """
    layers = getattr(ga, "layers", None)
    if not layers:
        return ""
    u = _esc(unit)
    sym = _esc(etf_symbol)

    def _p(v):
        return f"{(conv(v) if conv else v):.1f}{u}"

    def _etf(raw):
        return (f' <span style="color:#0969da;font-weight:600">{sym}{raw:.1f}</span>'
                if conv is not None else "")

    rows = []
    for i, (key, lab, _lo, _hi) in enumerate(_WALL_LAYER_ORDER):
        L = layers.get(key)
        if L is None:
            continue
        is_main = (key == "near")
        cw = (f'<b style="color:{_FLOW_COLOR["bearish"]}">{_p(L.call_wall)}</b>{_etf(L.call_wall)}'
              f' <span class="sub">({L.call_wall_oi:,})</span>') if L.call_wall_oi > 0 else '<span class="sub">—</span>'
        pw = (f'<b style="color:{_FLOW_COLOR["bullish"]}">{_p(L.put_wall)}</b>{_etf(L.put_wall)}'
              f' <span class="sub">({L.put_wall_oi:,})</span>') if L.put_wall_oi > 0 else '<span class="sub">—</span>'
        tag = ('<span class="pill" style="background:#1a7f371a;color:#1a7f37">主口径</span>'
               if is_main else ('<span class="pill" style="background:#8250df1a;color:#8250df">持仓层</span>'
                                if key == "mid" else
                                '<span class="pill" style="background:#57606a1a;color:#57606a">仅背景</span>'))
        style = ('background:#1a7f370d;font-weight:600' if is_main else
                 ('color:#57606a' if key == "far" else ''))
        rows.append(
            f'<tr style="{style}"><td>{_esc(lab)} {tag}</td><td>{pw}</td><td>{cw}</td>'
            f'<td class="sub">C {L.total_call_oi:,} / P {L.total_put_oi:,}</td></tr>')

    agree_html = ""
    if agree:
        bits = []
        for side, label in (("put", "put 墙"), ("call", "call 墙")):
            ok, txt = agree.get(side, (False, ""))
            icon = "✅" if ok else "⚠️"
            color = "#1a7f37" if ok else "#bc4c00"
            bits.append(f'<div style="margin:3px 0"><b style="color:{color}">{icon} {label}</b>'
                        f' <span class="sub">{_esc(txt)}</span></div>')
        agree_html = ('<div style="margin:10px 0;padding:8px 10px;background:#f6f8fa;'
                      'border-radius:6px">' + "".join(bits) + '</div>')

    ladder_html = ""
    if ladder:
        lr = []
        tot_oi = sum(x.oi for x in ladder)
        tot_exp = sum(x.expiring for x in ladder)
        for st in ladder:
            bar = '█' * max(1, int(st.share * 60))
            # 当日到期部分今晚就消失，画成空心段 —— 明天这一档只剩实心的部分
            solid = '█' * max(0, int((st.share * (1 - st.expiring_share)) * 60))
            hollow = '░' * max(0, len(bar) - len(solid))
            exp_note = (f' <span style="color:#bc4c00">今日到期 {st.expiring:,}'
                        f'（{st.expiring_share:.0%}）</span>' if st.expiring else "")
            gap = (f'<div class="sub" style="color:#bc4c00;padding-left:16px">'
                   f'〰 往下 {st.gap_after:.1f}% 无有效支撑</div>' if st.gap_after else "")
            lr.append(f'<div style="font-family:ui-monospace,monospace;font-size:13px">'
                      f'{_p(st.strike)}{_etf(st.strike)} '
                      f'<span class="sub">{st.dist_pct:+.1f}%</span> '
                      f'{st.oi:,} <span class="sub">({st.share:.0%})</span> '
                      f'<span style="color:#0969da">{solid}</span>'
                      f'<span style="color:#bc4c00">{hollow}</span>{exp_note}</div>{gap}')
        decay = ""
        if tot_exp:
            decay = (f'<div class="sub" style="margin-top:6px;color:#bc4c00">'
                     f'⚠️ 阶梯合计 {tot_oi:,} 张，其中 <b>{tot_exp:,} 张（{tot_exp / tot_oi:.0%}）'
                     f'今日到期</b>，收盘后这部分支撑消失（图中空心段）。'
                     f'明天的下方支撑要按 {tot_oi - tot_exp:,} 张读。</div>')
        ladder_html = ('<div style="margin-top:12px"><b>近端下方支撑阶梯（≤14天到期）</b>'
                       '<div class="sub">逐档列出价格往下走会碰到什么；真空区 = 一旦跌破上一档，'
                       '到下一档之间没有东西挡。实心=明天还在，空心=今日到期、今晚消失。</div>'
                       '<div style="margin-top:6px">' + "".join(lr) + '</div>' + decay + '</div>')

    bands_html = ""
    if bands and bands.get("total"):
        bb = []
        for x in bands["bands"]:
            w = max(2, int(x["share"] * 100))
            bb.append(f'<div style="display:flex;align-items:center;gap:8px;margin:2px 0">'
                      f'<span class="sub" style="width:80px">{_esc(x["label"])}</span>'
                      f'<span style="display:inline-block;height:12px;width:{w * 2}px;'
                      f'background:#0969da;border-radius:2px"></span>'
                      f'<span class="sub">{x["share"]:.0%}（{x["oi"]:,}）</span></div>')
        bands_html = ('<div style="margin-top:12px"><b>支撑堆在哪</b>'
                      '<div class="sub">近端下方持仓按跌幅分区。堆在脚下 = 阶梯式，'
                      '堆在深渊 = 中间没东西接。</div>'
                      '<div style="margin-top:6px">' + "".join(bb) + '</div></div>')

    basis = _esc(getattr(ga, "wall_basis", "") or "")
    bl = ""
    if getattr(ga, "blended_put_wall_oi", 0) > 0:
        bl = (f'<div class="sub" style="margin-top:8px">对照：旧的 45 天混算口径给 '
              f'put 墙 {_p(ga.blended_put_wall)}（{ga.blended_put_wall_oi:,}）· '
              f'call 墙 {_p(ga.blended_call_wall)}（{ga.blended_call_wall_oi:,}）。'
              f'混算把明天到期和 45 天后到期等权相加，若与上表近端不一致，说明它给的是'
              f'各层加总出来的位置，实盘那里并没有那么厚的墙。</div>')

    return (
        '<div class="card"><h2>① 期权结构 · 按到期分层（近端置顶）</h2>'
        f'<div class="sub">主墙位取<b>近端 ≤14天</b>（{basis}）—— 方向判断问的是"这周会不会破位"，'
        '跨月加总的墙答不了这个问题。中端是多数持仓所在，远端只作背景，不参与本周判断。</div>'
        '<table style="margin-top:8px"><tr><th>到期层</th><th>put 墙（下方支撑）</th>'
        '<th>call 墙（上方阻力）</th><th>该层持仓</th></tr>'
        + "".join(rows) + '</table>'
        + agree_html + ladder_html + bands_html + bl
        + '<div class="sub" style="margin-top:10px">墙 = 该层该侧 OI 最大的行权价（现价 ±15% 内）。'
          'OI 是持仓不是方向，厚墙只说明"很多人在这有仓"，不等于价格一定停在那。</div></div>'
    )


def render_tradeable_gate(ti, display_name: str = "") -> str:
    """可交易信息闸门横幅 —— 放在报告最顶，决定下面所有方向结论该给多少信任。

    起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，
    你又说这其实是不准的」。横盘日信号胜率 50%（掷硬币），有行情日 70~100%，
    整体不显著是被横盘日稀释的。这个闸门用盘前已知的压力倍数把那类日子标出来。
    """
    if not ti:
        return ""
    ev = ti.get("evidence", {})
    ok = ti.get("tradeable")
    ratio = ti.get("ratio", 0.0)
    rtxt = "∞" if ratio == float("inf") else f"{ratio:.1f}×"
    if ok:
        bg, bd, fg, icon, head = "#1a7f370d", "#1a7f37", "#1a7f37", "✅", "今天有可交易信息"
    else:
        bg, bd, fg, icon, head = "#bc4c000d", "#bc4c00", "#bc4c00", "⛔", "今天没有可交易信息"
    # 证据行：样本量必须和结论同时出现，不得只写结论
    eb = ""
    if ev:
        eb = (f'<div class="sub" style="margin-top:8px;line-height:1.7">'
              f'闸门实测（{ev.get("n_pairs")} 品种-日 / {ev.get("n_clusters")} 个日期簇，'
              f'快照盘前 → 当日开盘→收盘）：'
              f'<b>放行组 {ev.get("passed_n")} 笔胜率 {ev.get("passed_hit",0):.0%}、'
              f'顺向 {ev.get("passed_ret",0):+.2f}%/笔</b>；'
              f'拦掉组 {ev.get("blocked_n")} 笔胜率 {ev.get("blocked_hit",0):.0%}、'
              f'顺向 {ev.get("blocked_ret",0):+.2f}%。Fisher p={ev.get("fisher_p")}。'
              f'<br><span style="color:#bc4c00">⚠️ 共测了 {ev.get("n_thresholds_tested")} 个阈值，'
              f'Bonferroni 校正后 p={ev.get("bonferroni_p")} 不再显著 —— '
              f'这个闸门尚待样本外验证，不是已确证的规则。</span>'
              f'　拦掉组 41% 不代表可以反着做（n=29，与 50% 的差异本身不显著），'
              f'正确读法是「这些日子没信息」。</div>')
    return (f'<div class="card" style="background:{bg};border-left:4px solid {bd}">'
            f'<h2 style="margin-top:0;color:{fg}">{icon} {_esc(head)}'
            f'{" · " + _esc(display_name) if display_name else ""}</h2>'
            f'<div style="font-size:15px;margin:6px 0"><b>多空压力比 {rtxt}</b>'
            f'　·　可判定率 {ti.get("decidable",0):.0%}'
            f'　·　倾向 {_esc(ti.get("side","—"))}</div>'
            f'<div class="sub" style="line-height:1.7">{_esc(ti.get("reason",""))}</div>'
            f'{eb}</div>')


def render_cost_gate(rows, exp_move=None, spot: float = 0.0, side: str = "",
                     conv=None, unit: str = "", etf_symbol: str = "") -> str:
    """成本闸门：这天大概走多少 vs 这张要走多少才不亏，并排放。

    起因（用户 2026-08-31 那晚四笔全亏，方向却基本都对）：
      SLV 9/2 60P 需标的跌 1.9% 才回本，当天只跌 0.75% → 方向对也白做。
      GLD 410C 持有 1 分钟割掉亏 11%，那 11% 就是点差本身。
    只给方向不给这两个数，等于让人拿着对的判断去买错的合约。
    """
    if not rows:
        return ""
    em = exp_move or rows[0][1].exp_move
    sym = _esc(etf_symbol)

    def _p(v):
        return f"{(conv(v) if conv else v):.1f}{_esc(unit)}"

    trs = []
    any_ok = False
    for be, v in rows:
        any_ok = any_ok or v.ok
        c = "#1a7f37" if v.ok else "#bc4c00"
        icon = "✅" if v.ok else "⛔"
        theta_warn = (' <span style="color:#bc4c00">拿不过夜</span>'
                      if be.theta_share > 0.25 else "")
        trs.append(
            f'<tr><td>{_p(be.strike)}{be.kind}'
            + (f' <span style="color:#0969da">{sym}{be.strike:g}</span>' if conv else "")
            + f'</td><td>{be.expiry:%m/%d}<span class="sub">（{be.dte}天）</span></td>'
            f'<td class="r">${be.cost:,.0f}</td>'
            f'<td class="r">{be.spread_pct:.0%}</td>'
            f'<td class="r">{be.delta:+.2f}</td>'
            f'<td class="r">{be.theta_share:.0%}{theta_warn}</td>'
            f'<td class="r" style="color:{c};font-weight:600">{v.need_pct:.2f}%</td>'
            f'<td style="color:{c}">{icon}</td></tr>')

    head_c = "#1a7f37" if any_ok else "#bc4c00"
    head_t = ("这天的典型幅度够覆盖成本" if any_ok
              else "这天的典型幅度覆盖不了任何一张的成本")
    weak = ('<span style="color:#bc4c00">（样本仅 %d 笔，只作量级参考）</span>' % em.n
            if em.weak else f'（n={em.n}）')
    return (
        '<div class="card"><h2>成本闸门 · 这天能走多少 vs 这张要走多少</h2>'
        f'<div style="font-size:15px;margin:6px 0;color:{head_c}"><b>{_esc(head_t)}</b></div>'
        f'<div class="sub" style="line-height:1.8">'
        f'<b>预期波动 {em.pct:.2f}%</b>　—— 可判定率落在 {_esc(em.band)} 档 {weak}，'
        f'该档 {em.p_over_1pct:.0%} 的日子波动超过 1%。'
        f'<br>可判定率预告的是<b>幅度</b>不是方向：实测 r=+0.243（对波动）'
        f' vs r=+0.053（对命中率）—— 高低两组命中率一样（65% vs 66%），'
        f'波动差一倍（0.55% vs 1.09%）。</div>'
        '<table style="margin-top:10px"><tr><th>合约</th><th>到期</th><th>一张</th>'
        '<th>点差</th><th>Δ</th><th>θ/权利金</th><th>回本需走</th><th></th></tr>'
        + "".join(trs) + '</table>'
        '<div class="sub" style="margin-top:8px;line-height:1.7">'
        '回本需走 =（点差 + |θ|×持有天数 + 手续费/100）÷|Δ|÷现价，即标的要往'
        f'{_esc(side) or "顺向"}走多少，这笔才刚好不亏。用快照盘口（盘前已知），'
        '实际下单时点差可能不同。<br>'
        '<b>θ/权利金</b> 是每天损耗掉的比例——超过 25% 的基本拿不过夜，'
        '近月便宜的代价全在这一栏。<br>'
        '⚠️ 预期波动表是在同一批数据上的第二次找模式（先测倍数闸门、再测可判定率），'
        '多重比较风险仍在，需样本外验证。方向对了也可能因为幅度不够而亏，'
        '这张表只解决幅度，不解决方向。</div></div>')


def render_backmonth(scan, spot: float = 0.0, conv=None, unit: str = "",
                     etf_symbol: str = "") -> str:
    """远月结构异动卡（playbook R16）。

    【时间尺度隔离纪律】这张卡只作长期背景：不进综合分、不进日度方向研判、
    不改任何近月位点。机构完全可以一边持有远月上行尾部、一边在近月做空 ——
    两个时间尺度的仓位并存不矛盾。标注文字是硬要求，由测试锁住。
    """
    if scan is None or scan.empty:
        return ""
    sym = _esc(etf_symbol)
    u = _esc(unit)

    def _p(v):
        return f"{(conv(v) if conv else v):.1f}{u}"

    trs = []
    for m in scan.moves:
        g = "新建" if m.prev_oi == 0 else f"{m.growth * 100:,.0f}%"
        col = "#cf222e" if m.kind == "C" else "#1a7f37"
        tag = ('<span class="pill" style="background:#8250df1a;color:#8250df">'
               '新布局</span>' if m.new_build else "")
        trs.append(
            f'<tr><td><b style="color:{col}">{_p(m.strike)}{m.kind}</b>'
            + (f' <span style="color:#0969da">{sym}{m.strike:g}</span>' if conv else "")
            + f'</td><td class="sub">{m.expiry} · {m.dte}天</td>'
            f'<td class="r">{m.moneyness * 100:+.0f}%</td>'
            f'<td class="r sub">{m.prev_oi:,}</td>'
            f'<td class="r">{m.curr_oi:,}</td>'
            f'<td class="r" style="font-weight:600">+{m.d_oi:,}</td>'
            f'<td class="r">{g}</td><td>{_esc(m.label)} {tag}</td></tr>')
    return (
        '<div class="card" style="border-left:4px solid #8250df">'
        '<h2>远月结构异动 · 月度级配置信号</h2>'
        '<div style="margin:6px 0;padding:8px 10px;background:#8250df14;border-radius:6px">'
        '<b style="color:#8250df">⚠️ 与本周方向无关。</b>'
        '<span class="sub">这张卡不进综合分、不进当日方向研判、不改任何近月位点。'
        '机构可以一边持有远月上行尾部、一边在近月做空——两个时间尺度的仓位并存不矛盾。'
        '日度决策仍以近月结构 + 资金流为准。</span></div>'
        f'<div class="sub" style="margin:8px 0">扫描 {scan.scanned:,} 个 &gt;45 天合约，'
        f'触发 {len(scan.moves)} 条（门槛：单日 OI 增幅 ≥50% 且增量 ≥1,000 手，'
        f'双门槛缺一不可）　·　<b>{_esc(scan.tilt)}</b></div>'
        '<table><tr><th>合约</th><th>到期</th><th>距现价</th><th>昨 OI</th>'
        '<th>今 OI</th><th>ΔOI</th><th>增幅</th><th>性质</th></tr>'
        + "".join(trs) + '</table>'
        '<div class="sub" style="margin-top:8px;line-height:1.7">'
        '只读 OI 结构，不做买卖方判定：远月报价稀疏、深虚合约一个最小跳动即反推出'
        '巨大 IV 变化，「OI↑+IV↓=卖方」那套机械判定在这里不可用（同 R15 的理由）。'
        '所以这里只说「有人在这个位置堆了多少仓」，不说是买方还是卖方。</div></div>')


def render_ratio_spreads(rs, conv=None, unit: str = "", etf_symbol: str = "") -> str:
    """比例价差卡（playbook R15）—— 逐腿判定会把它读反的结构。"""
    if not rs:
        return ""
    sym = _esc(etf_symbol)

    def _p(v):
        return f"{(conv(v) if conv else v):.1f}{_esc(unit)}"

    items = []
    for x in rs[:8]:
        col = "#cf222e" if x.kind == "C" else "#1a7f37"
        tail = ('<span class="pill" style="background:#bc4c001a;color:#bc4c00">'
                '极端尾部</span>' if x.tail else "")
        items.append(
            f'<div style="border-left:3px solid {col};padding-left:10px;margin:8px 0">'
            f'<div style="font-weight:600">{_esc(x.name)} · {x.expiry} {tail}</div>'
            f'<div class="sub" style="line-height:1.7">买 {_p(x.near_strike)}'
            + (f'<span style="color:#0969da">({sym}{x.near_strike:g})</span>' if conv else "")
            + f' × {x.near_doi:,}　卖 {_p(x.far_strike)}'
            + (f'<span style="color:#0969da">({sym}{x.far_strike:g})</span>' if conv else "")
            + f' × {x.far_doi:,}</div>'
            f'<div class="sub">{_esc(x.detail)}</div></div>')
    return (
        '<div class="card"><h2>比例价差（1:2）· 逐腿会读反的结构</h2>'
        '<div class="sub" style="line-height:1.7">买一份近腿、卖两份远腿。它表达的是'
        '<b>「走到某个位置为止」</b>，不是无限看涨/看跌——而逐腿判定会把卖出的'
        '两份读成强烈反向压力，方向正好读反。深虚合约还有第二重问题：绝对价格极小，'
        '一个最小跳动就反推出巨大 IV 变化，「OI↑+IV↓=卖方」在那里系统性失效，'
        '只能读数量结构。</div>'
        + "".join(items) +
        '<div class="sub" style="margin-top:8px;line-height:1.7">'
        '⚠️ 检出依据是 <b>ΔOI 与当日成交量同时配成 1:2</b>。只用 ΔOI 会大量捡到巧合：'
        '2026-08-31 零假设检验（随机打乱 ΔOI 后重跑）显示纯 ΔOI 口径的真实/随机比只有 '
        '1.0~1.5x（TQQQ 是 1.0x，等于全是巧合）；加成交量确认后升到 2.9~6.2x。'
        '即便如此仍有残余噪音（QQQ 随机基线约 4.5 个），条目数不多时才可信。</div></div>')


def render_credit_wall(verdicts: dict, spot: float = 0.0, conv=None,
                       unit: str = "", etf_symbol: str = "",
                       buying_power: float | None = None,
                       net_assets: float | None = None) -> str:
    """墙位卖方价差候选（analyze/credit_wall）—— 紧跟期权结构，因为它直接用墙位下单。

    用户 2026-08-31：「事实证明破墙很难，我们根据墙进行卖方价差，胜率应该很高。」
    回测证实，但要三道闸门：极强信号(≥5×)、加总墙够厚(≥10%)、同到期二次确认。

    仓位不作为筛选条件（用户同日：「如果因为仓位选择了更差的交易，反而得不偿失」）：
    三档全列，占用标出来，选哪档由人定。
    """
    from undertow.analyze.credit_wall import (RISK_TIERS, OFFSET_TRADEOFF,
                                              STRATEGY_VALIDATED)
    if not verdicts:
        return ""
    if not STRATEGY_VALIDATED:
        # 2026-08-31 codex review 后重做回测：三档全部负簇均 ROI，策略停用。
        # 卡片保留，但只讲「为什么停用」——留作反例，防止日后凭「82% 胜率」
        # 的记忆把它重新打开。
        rows = "".join(
            f'<tr><td>{_esc(t["label"])}档</td><td class="r">{t["n"]}</td>'
            f'<td class="r">{t["clusters"]}</td><td class="r">{t["win_rate"]:.0%}</td>'
            f'<td class="r" style="color:#cf222e">{t["per_trade_pct"]:+.2f}%</td>'
            f'<td class="r">{t["perm_p"]:.3f}</td>'
            f'<td class="r">[{t["ci95"][0]:+.1f}%, {t["ci95"][1]:+.1f}%]</td>'
            f'<td class="r">{t["total_pnl"]:+.0f}</td></tr>'
            for t in RISK_TIERS.values())
        return (
            '<div class="card" style="border-left:4px solid #cf222e">'
            '<h2>墙位卖方价差 <b>v1</b> · ⛔ 失败记录（已停用，保留作反例）</h2>'
            '<div class="sub" style="line-height:1.8">'
            '最初的回测给出「稳健档 82% 胜率 +2.84%/笔、激进档 63% +9.99%/笔」，'
            'codex 审查指出四处方法论错误，逐条修掉后<b>三档全部翻负</b>：'
            '<br>① <b>策略定义漂移</b>：一个信号下多个到期各算一笔，而报告只取年化'
            '最高的那个 —— 统计的策略和执行的策略不是同一个'
            '<br>② <b>DTE 错一天</b>：用 OI 所属日算到期天数，0DTE 被当成 3DTE'
            '<br>③ <b>向后取价</b>：到期日缺收盘价时取之后最近一天，把到期后的价格'
            '变动带进内在价值 —— look-ahead'
            '<br>④ <b>品种-日当独立样本</b>：金银同日相关 0.89、QQQ/TQQQ 0.99'
            '</div>'
            '<table style="margin-top:10px"><tr><th>档位</th><th>笔数</th>'
            '<th>日期簇</th><th>胜率</th><th>簇均 ROI</th><th>置换 p</th>'
            '<th>95% CI</th><th>总 PnL</th></tr>' + rows + '</table>'
            '<div class="sub" style="margin-top:8px;line-height:1.8">'
            '置换检验问的是<b>「日期簇净收益是否 &gt; 0」</b>，不是「胜率是否 &gt; 50%」。'
            '胜率高不等于赚钱 —— 上表 53~67% 的胜率配的是 -5.6~-31.7% 的簇均 ROI。'
            '<br>九个组合（3 种选择规则 × 3 档）全部负簇均 ROI，置换 p 全部 &gt; 0.7。'
            '<br>「墙难破」这个观察本身仍成立（破墙率 5~35%），'
            '<b>但权利金覆盖不了破墙时的损失</b>。'
            '<br>回测可复现：<code>scripts/backtest_credit_wall.py</code>，'
            '逐笔账本在 <code>data/backtest/</code>。</div></div>')
    sym = _esc(etf_symbol)

    def _p(v):
        return f"{(conv(v) if conv else v):.1f}{_esc(unit)}"

    blocks = []
    any_ok = False
    for key in ("conservative", "balanced", "aggressive"):
        v = verdicts.get(key)
        if v is None:
            continue
        t = RISK_TIERS[key]
        col = {"conservative": "#1a7f37", "balanced": "#8250df",
               "aggressive": "#cf222e"}[key]
        ev = (f'<span class="sub">实测 {t["n"]} 笔 · 胜率 {t["win_rate"]:.0%} · '
              f'破墙 {t["break_rate"]:.0%} · 单笔 {t["per_trade_pct"]:+.2f}% · '
              f'年化 {t["annual_pct"]:+.0f}% · 占用中位 ${t["median_occupancy"]:.0f}</span>')
        # 仓位按 Kelly 判，不按净资产固定百分比 —— 用户 2026-08-31：
        # 「为了这个 10%，却可能放弃更优的交易而选择次优，这反而放大了风险」
        kel = ""
        if net_assets and v.ok and v.spreads:
            try:
                from undertow.analyze.sizing import kelly as _kel, size as _sz, ruin_probability
                _k = _kel(t["win_rate"], t["per_trade_pct"], t.get("win_roi_pct", 12.0))
                _occ = v.spreads[0].occupancy
                _v2 = _sz(net_assets, _occ, _k, buying_power=buying_power)
                import math as _m
                _n_ruin = max(1, _m.ceil(net_assets / _occ))
                _rp = ruin_probability(t["win_rate"], _n_ruin)
                _c = "#1a7f37" if _v2.over_kelly <= 1.5 else "#bc4c00"
                kel = (f'<div style="margin:5px 0;padding:6px 9px;background:#f6f8fa;'
                       f'border-radius:5px;font-size:12.5px">'
                       f'<b>仓位</b>　盈亏比 {_k.odds:.2f}　Kelly {_k.kelly:.0%}'
                       f'（${net_assets * _k.kelly:.0f}）　'
                       f'<span style="color:{_c}">1 组 ${_occ:.0f} = 净资产 '
                       f'{_occ / net_assets:.0%}，Kelly 的 {_v2.over_kelly:.1f} 倍</span>'
                       f'　·　连续 {_n_ruin} 次全损打光的概率上限 '
                       f'<b>{_rp:.2%}</b></div>')
            except Exception:
                kel = ""
        cfg = (f'<span class="sub">卖腿{"墙内" if t["offset"] < 0 else "墙外"}'
               f'{abs(t["offset"]) * 100:.0f}% · 宽 {t["width"] * 100:.1f}% · '
               f'{t["dte"][0]}~{t["dte"][1]} 天</span>')
        if not v.ok:
            blocks.append(
                f'<div style="border-left:3px solid #d0d7de;padding-left:10px;margin:10px 0">'
                f'<div style="font-weight:700;color:#57606a">{_esc(t["label"])}档 ⛔ 无候选</div>'
                f'{cfg}<div class="sub">{_esc(v.reason)}</div></div>')
            continue
        any_ok = True
        rows = []
        for sp in v.spreads[:3]:
            afford = ("" if buying_power is None else
                      ('<span style="color:#1a7f37">✓可下</span>'
                       if sp.occupancy <= buying_power
                       else f'<span style="color:#bc4c00">需${sp.occupancy:.0f}</span>'))
            rows.append(
                f'<tr><td><b>卖 {_p(sp.sell_strike)}{sp.kind}</b> / 买 {_p(sp.buy_strike)}{sp.kind}'
                + (f' <span style="color:#0969da">{sym}{sp.sell_strike:g}/{sp.buy_strike:g}</span>'
                   if conv else "")
                + f'</td><td class="sub">{sp.expiry}<br>{sp.dte}天</td>'
                f'<td class="r">+${sp.credit:.0f}</td>'
                f'<td class="r">${sp.occupancy:.0f}</td>'
                f'<td class="r">{sp.roi * 100:.0f}%</td>'
                f'<td class="r">{sp.buffer_pct:.1f}%</td>'
                f'<td class="r">-${sp.max_loss:.0f}</td>'
                f'<td>{afford}</td></tr>')
        blocks.append(
            f'<div style="border-left:3px solid {col};padding-left:10px;margin:12px 0">'
            f'<div style="font-weight:700;color:{col}">{_esc(t["label"])}档</div>'
            f'{cfg}<br>{ev}{kel}'
            f'<div class="sub" style="margin:4px 0">{_esc(t["note"])}</div>' 
            '<table style="margin-top:6px"><tr><th>腿位</th><th>到期</th><th>收权利金</th>'
            '<th>占用</th><th>ROI</th><th>缓冲</th><th>最大亏</th><th></th></tr>'
            + "".join(rows) + '</table></div>')

    trade = "".join(
        f'<tr><td>{"墙内" if o < 0 else ("墙上" if o == 0 else "墙外")}'
        f'{abs(o) * 100:.0f}%</td><td class="r">{cw:.0%}</td><td class="r">{br:.0%}</td>'
        f'<td class="r">{wr:.0%}</td><td class="r">{pt:+.2f}%</td>'
        f'<td class="r">{an:+.0f}%</td></tr>'
        for o, cw, br, wr, pt, an in OFFSET_TRADEOFF)

    head = ("✅ 今日有卖方价差候选" if any_ok else "⛔ 今日无卖方价差候选")
    return (
        '<div class="card"><h2>墙位卖方价差 <b>v1</b> · 候选腿位（已停用）</h2>'
        f'<div style="font-size:15px;margin:6px 0"><b>{head}</b></div>'
        '<div class="sub" style="line-height:1.7">三道闸门：极强信号（压力比 ≥5×）、'
        '近端加总墙厚度 ≥10%、该到期自己的墙落在同一位置且 ≥20%。'
        '中等信号(2~5×)的亏损笔达 -1250/-499/-260，是极强信号(最惨 -24)的 50 倍，'
        '所以第一道闸门不能松。</div>'
        + "".join(blocks) +
        '<div style="margin-top:14px"><b>卖腿位置的权衡</b>'
        '<div class="sub">权利金和风险是同一枚硬币 —— 这张表是「不能一味卖近」的凭证</div>'
        '<table style="margin-top:6px"><tr><th>卖腿位置</th><th>权利金/宽度</th>'
        '<th>破墙率</th><th>胜率</th><th>单笔</th><th>年化</th></tr>'
        + trade + '</table></div>'
        '<div class="sub" style="margin-top:10px;line-height:1.7">'
        '⚠️ <b>提前平仓实测更差</b>：破墙率只有 5~35%，而平仓要双向吃点差，'
        '不如让它到期作废（卖墙上：持有到期 +40% vs 赚50%平 +5%）。'
        '这与「卖方收 50% 就跑」的通行说法相反，但数据如此。<br>'
        '⚠️ 所有阈值在同一批数据（2026-06-25~08-31）上选出，多重比较风险实打实；'
        '年化是「单笔 × 365/持有天数」的外推，未扣信号空窗期。'
        '真实表现一定比表里的数字差。<br>'
        '⚠️ <b>仓位按 Kelly 判，不按净资产固定百分比。</b>'
        '净资产 $264 × 10% = $26，买不起任何一张价差（最小占用 $86）——'
        '固定百分比对小账户不是风险管理，是强制你去选负期望的廉价合约。'
        '期权价差 1 组是最小不可分单位，超过 Kelly 时只有「按 1 组做」或「不做」，'
        '没有第三个选项。<br>'
        '⚠️ <b>回测按「持有到期、用收盘价算内在价值」结算，实盘拿不到这个收益</b>'
        '（codex 2026-08-31 P0-4）：美式期权卖方从卖出到到期随时可能被指派'
        '（深度实值、除息前、借券困难时概率更高）；到期日若落在卖腿与买腿之间'
        '（如卖61/买60、标的收在 60.5），卖腿被行权需接货、买腿保护不到，'
        '$264 的账户会触发券商强平——而「强平」恰恰意味着到期 payoff 无法实现。'
        '券商具体规则请自行向长桥确认，本工具不做断言。</div></div>')


def render_wall_zones(mg: dict) -> str:
    """put 墙附近三个区域的原始读数 —— **只摆事实，不下结论**（codex 2026-08-29 P0）。

    上一版在 index 里输出「保护向墙下搬家 / 在给跌破定价 / 下一道防线」+ 红色高亮，
    用的还是那个回测 30 次只中 1 次、已决定不上线的预测器的门槛 —— 换个名字上线而已。
    现在不分类、不预测、不用方向色。

    2026-08-31 从 index 移到品种报告（用户：「index 里现在信息太过复杂」）：
    它是研究性内容，不该占索引页的位置，但也不该消失。
    """
    if not mg or not (mg.get("at") or mg.get("below")):
        return ""

    def _z(name, z, extra=""):
        if not z or not z.get("doi"):
            return ""
        bits = []
        if z.get("buy_put"):
            bits.append(f'买 put {z["buy_put"]:+,}')
        if z.get("sell_put"):
            bits.append(f'卖 put {z["sell_put"]:+,}')
        iv = z.get("iv")
        ivs = f'，IV {iv:+.1f}pp' if isinstance(iv, (int, float)) else ""
        ks = (f'　<span style="color:#6e7781">'
              f'{"、".join(f"{k:g}" for k in z["strikes"])}</span>'
              if z.get("strikes") else "")
        return f'<div style="margin:3px 0">{name}{extra}：' + '　'.join(bits) + ivs + ks + '</div>'

    wp = mg.get("wall_pct")
    gap = mg.get("iv_gap_below_minus_at")
    return (
        '<div style="margin:10px 0;padding:9px 11px;border-left:3px solid #57606a;'
        'background:#f6f8fa;line-height:1.7">'
        '<b style="font-size:13px">🧱 put 墙附近三个区域的读数</b>'
        '<span style="color:#6e7781;font-size:11.5px">（原始事实，不含判断）</span>'
        + _z("墙上方（更贴身）", mg.get("above"))
        + _z(f'<b>{mg["wall"]:g}</b>（墙上）', mg.get("at"),
             f'{f"，{wp:+.1f}%" if wp is not None else ""}')
        + _z("墙下方", mg.get("below"))
        + (f'<div style="color:#6e7781;font-size:11.5px">'
           f'墙下方与墙上的 IV 变化之差：{gap:+.1f}pp</div>' if gap is not None else "")
        + '<div style="color:#6e7781;font-size:11.5px">'
          '⚠️ 这里不做任何判断。把它读成「要跌破了」是**过度解读** —— '
          '按同类结构做的预测器回测 30 次只中 1 次，已决定不上线。'
          '</div></div>')

def render_wall_history(rows: list[dict], display_name: str = "") -> str:
    """墙位历史卡片（用户 2026-08-31 要求，2026-09-02 接线）。

    静态的当日墙位表只说"墙在哪"，答不了"这墙守住过没有"。
    图里三件事叠在一起看：半透明日 K（不标涨跌色）+ 收盘折线 + 三道结构墙，
    再叠上信号标记 —— 于是"破墙那天有没有信号"变成一眼可见。

    ⚠️ 墙用的是 gamma.structural_walls()（全范围 + 近端到期占比门槛），
    不是 band 内相对最大 —— 后者会把较小的局部档位画成墙（见 gamma 文件头）。
    """
    if not rows:
        return ""
    from undertow.report.viz import wall_history_svg
    svg = wall_history_svg(rows, title=f"{display_name} 墙位历史")
    if not svg:
        return ""
    n_fired = sum(1 for r in rows if (r.get("sig") or [None, None, False])[2:3] == [True])
    n_mark = sum(1 for r in rows if r.get("sig"))
    return (
        '<div class="card"><h2>② 墙位历史 · 这墙守住过没有</h2>'
        '<div class="sub" style="line-height:1.7">'
        '墙取自 <code>structural_walls()</code>：全行权价范围内、近端到期占比 ≥15% '
        '的 OI 堆积（滤掉长期对冲与尾部保险的堆积）。'
        '<b>不是</b>「现价 ±band 内最大」——那种口径带内总有最大值，'
        '会把较小的局部档位画成墙。<br>'
        f'信号标记：实心 ▲▼ = 已开火（过全部闸门，共 {n_fired} 天）；'
        f'空心 △▽ = 仅压力比 ≥10× 但未开火（合计标记 {n_mark} 天）。'
        '<b>两者必须分开看</b> —— 未开火的那些从没在报告里弹过告警，'
        '把它们当信号数会数出两倍多。'
        '</div>'
        f'<div class="chart">{svg}</div></div>')


def render_wall_spread(v, display_name: str = "", events=None) -> str:
    """墙位卖方价差候选（v3 三步法）。put 与 call 分开列，不合成铁鹰。

    铁鹰在长桥收两份保证金（组合保证金只认 Covered Call/Put），
    资金效率腰斩且仍不如单边 put —— 见 docs/broker/longbridge_margin.md。
    做哪边、做不做，由用户自己决定；这里只提供事实。
    """
    if v is None:
        return ""
    be_lo, be_hi = (v.params or {}).get("breakeven_rate", (0, 0))
    ad_lo, ad_hi = (v.params or {}).get("adverse_rate", (0, 0))
    warn = (
        '<div style="background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;'
        'padding:10px 12px;margin:10px 0;font-size:12.5px;line-height:1.75">'
        f'⚠️ <b>这套规则尚未通过生死线，输出候选 ≠ 建议下单。</b><br>'
        f'按期权定价，盈亏平衡要求破墙率低于 <b>{be_lo:.1%}~{be_hi:.1%}</b>；'
        f'而唯一的逆势样本（call 侧，样本期白银 +9.7%）实测持有到期破墙率是 '
        f'<b>{ad_lo:.0%}~{ad_hi:.0%}</b>。<br>'
        'put 侧全程 0 破墙、年化 +400%~580%，那是样本期单边上涨送的，不是策略挣的。'
        '<b>整套东西未经跌市验证。</b>'
        '</div>')
    if not v.ok:
        return ('<div class="card"><h2>② 墙位卖方价差 <b>v3</b> · 今日无候选</h2>'
                f'<div class="sub" style="line-height:1.8">{_esc(v.reason)}</div>'
                f'{warn}</div>')

    # 到期日撞 🔴高影响事件 → 标注（2026-09-03 启示 12：白银候选 9/4 到期正撞非农，
    # 第二步的 DTE 2~4 完全没看事件日历）。只标不禁：决定权在用户。
    hi_days = {}
    for e in (events or []):
        if str(getattr(e, "importance", "")).lower() == "high":
            hi_days.setdefault(getattr(e, "date", None), []).append(
                getattr(e, "name", "") or str(e))

    def rows(cands, side_name):
        if not cands:
            return f'<tr><td colspan="9" class="sub">{side_name} 无合格候选</td></tr>'
        out = []
        for c in cands[:4]:
            pos = "墙上" if c.offset == 0 else f"墙内{c.offset}档"
            clash = hi_days.get(c.expiry)
            exp_cell = (f'{c.expiry} <b style="color:#cf222e">🔴撞{_esc(clash[0][:12])}</b>'
                        if clash else f'{c.expiry}')
            out.append(
                f'<tr><td>{_esc(side_name)}</td>'
                f'<td>{c.sell:g} / {c.buy:g}</td>'
                f'<td class="r">{exp_cell}</td><td class="r">{c.dte}天</td>'
                f'<td class="r">{c.wall:g}<span class="sub">（{_esc(pos)}·{_esc(c.wall_rule)}）</span></td>'
                f'<td class="r">{c.buffer_pct:.1f}%</td>'
                f'<td class="r">${c.credit:.1f}<span class="sub">净${c.net_credit:.1f}</span></td>'
                f'<td class="r">${c.occupancy:.0f}</td>'
                f'<td class="r">{c.roi:+.1%}<span class="sub">日均${c.net_daily:.2f}</span></td></tr>')
        return "".join(out)

    return (
        '<div class="card"><h2>② 墙位卖方价差 <b>v3</b> · 今日候选</h2>'
        f'<div class="sub" style="line-height:1.75">{_esc(v.reason)}。'
        'put 与 call 分开列出，<b>不合成铁鹰</b> —— 长桥的组合保证金只认 '
        'Covered Call/Put，铁鹰收两份保证金、资金效率腰斩，且即便按标准一份'
        '也只有单边 put 的 41%。做哪边由你决定。</div>'
        f'{warn}'
        '<table><thead><tr><th>方向</th><th>卖/买</th><th class="r">到期</th>'
        '<th class="r">DTE</th><th class="r">墙位</th><th class="r">缓冲</th>'
        '<th class="r">权利金</th><th class="r">占用</th><th class="r">ROI</th>'
        '</tr></thead><tbody>'
        + rows(v.puts, "卖 put") + rows(v.calls, "卖 call") +
        '</tbody></table>'
        '<div class="sub" style="margin-top:8px;line-height:1.7">'
        '<b>出场规则（第三步定版）</b>：只在<b>收盘越过卖腿</b>时平仓；'
        '换墙不平、浮盈不平。实测：破卖腿即平在逆势侧砍掉 76% 亏损，'
        '顺势侧一次都不触发；而换墙即平两侧都亏，还把最差单笔从 −$2 恶化到 −$45。'
        '<br>推导全过程见 <code>docs/wall_spread_3steps.md</code>。'
        '</div></div>')


def render_smc(zones, spot: float, display_name: str = "",
               ratio: float | None = None) -> str:
    """4H 供需结构区（SMC）—— 独立于期权链的技术面视角。

    用户 2026-09-03：「期权结构落后一天，同时容易噪声，而且大多代表大资金和
    机构的观点，他们也有可能错的。」这张卡就是那个第二视角。

    ⛔ 只做展示，不进方向投票、不给期权墙加权 —— 实测两者同源（都跟着价格走），
    重合率与随机行权价无差别，控制缓冲后破墙率差异消失。详见 smc.py 文件头。
    """
    if not zones:
        return ""
    rows = []
    for z in sorted(zones, key=lambda x: -x.mid):
        d = (z.mid / spot - 1) * 100 if spot else 0.0
        is_dem = z.kind == "需求"
        col = "#1a7f37" if is_dem else "#cf222e"
        conv = (f'<span class="sub">≈{z.lo*ratio:.0f}~{z.hi*ratio:.0f}</span>'
                if ratio else "")
        here = ' <b style="color:#0969da">← 现价在此区内</b>' if z.lo <= spot <= z.hi else ""
        rows.append(
            f'<tr><td style="color:{col}">{"需求/支撑" if is_dem else "供给/阻力"}</td>'
            f'<td class="r">{z.lo:.2f} ~ {z.hi:.2f} {conv}</td>'
            f'<td class="r">{d:+.1f}%</td>'
            f'<td class="r">{(z.hi-z.lo)/z.mid*100:.1f}%</td>'
            f'<td class="sub">{_esc(z.source)}{here}</td></tr>')
    return (
        '<div class="card"><h2>4H 供需结构区 · 技术面第二视角</h2>'
        '<div class="sub" style="line-height:1.75">'
        '取 4 小时 K 线的<b>订单块</b>，按 LuxAlgo SMC 的 Pine 源码口径实现：'
        '结构被收盘价突破时，回看这一段里波幅极值那根 K 线的完整高低区间；'
        '真实波幅超过 2 倍 ATR(200) 的异常 K 线会被排除；'
        '区间被最高价或最低价越过即作废。按<b>距现价远近</b>各取上下两个'
        '（与「找墙取最近三道」同构）。'
        '<br>公允价值缺口在 LuxAlgo 里默认关闭，此处同样不用。'
        '<br>⛔ <b>只作参考，不参与方向投票、也不给期权墙加权。</b>'
        '实测它与期权墙同源（持仓本就跟着价格走），重合率与随机行权价无差别，'
        '控制缓冲后破墙率差异消失 —— 重合是重复计数，不是相互印证。'
        '它的价值在于：期权 OI 是前一交易日结算的滞后量，K 线结构是当下的。'
        '</div>'
        '<table><thead><tr><th>类型</th><th class="r">区间</th>'
        '<th class="r">距现价</th><th class="r">区宽</th><th>来源</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>')
