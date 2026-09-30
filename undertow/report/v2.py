"""新研报体系 v2：完整的期权结构 + 验证中的方向判断（用户 2026-09-30）。

用户原话：
- 「你新建个研报体系吧，先只放期权墙。后面我们证实哪些信号有用，就放什么上去。之前的研报也照常出。」
- 「你之前的期权墙怎么没放上去？之前的期权关键点位，墙位历史可视化。然后方向判断也先放上去，注明还在验证截断。
   这样一个完整的期权结构。同样也是一个index 界面，然后点击品种进入详细界面」

准入规则（写死在 SECTIONS，渲染时逐项判定）：
- observation：持仓结构、数据事实，不表达方向判断 —— 写明口径即可上（对应 claims 的 obs.*）。
- validating：方向判断，仍在前瞻预登记检验中 —— 只有 claims 里理由为 prospective_study、且有冻结规则版本（prereg_ref）
  的主张才能以「验证中」身份展示，醒目标注，不进任何结论、不作开仓依据。
- prediction：已通过检验的方向或信号 —— 必须是 T1。目前没有。
旧研报（cli report）照常生成，与本体系互不影响。本模块只格式化，数值全来自 analyze 层。
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape as _esc
from types import SimpleNamespace

from undertow.analyze import claims as _claims
from undertow.report.html import render_wall_overview_html
from undertow.report.markdown import render_wall_overview_md

VERSION = "report-v2-20260930b"
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
_LEDGER = _ROOT / "data/history/direction_ledger"


@dataclass(frozen=True)
class Section:
    sid: str
    title: str
    role: str                         # observation | validating | prediction
    claim_ids: tuple = ()
    basis: str = ""


SECTIONS = (
    Section("walls", "期权墙总览（按到期拆分）", "observation", ("obs.walls",),
            "OI 为前一交易日结算的存量，不表示买卖方向，也不宣称支撑/压力已被验证。"),
    Section("layers", "期权结构分层（近 / 中 / 远端）", "observation", ("obs.walls",),
            "与旧研报同一套分层计算；墙属于哪个到期层。"),
    Section("levels", "期权关键点位 + 价格图 + 近价 OI 分布", "observation", ("obs.walls",),
            "与旧研报同一套关键位计算；「支撑/阻力/翻转」是沿用的称谓，位置本身未经检验。"),
    Section("history", "墙位历史（这道墙守住过没有）", "observation", ("obs.walls",),
            "历史墙位与日 K 的对照图，只作描述。"),
    Section("direction", "方向判断（验证中）", "validating", ("conviction.h1.v1", "skew_reading.v1"),
            "方向族 D：冻结的预登记规则，2026-09-29 起前瞻积累，检验尚未完成。"),
)


def section_allowed(sec: Section) -> tuple[bool, str]:
    if sec.role == "observation":
        return True, "观测类（不表达方向判断）"
    if sec.role == "validating":
        bad = []
        for cid in sec.claim_ids:
            c = _claims.CLAIMS.get(cid)
            if c is None or c.reason != "prospective_study" or not c.prereg_ref:
                bad.append(cid); continue
            # 冻结身份须真实存在（Codex 031：不能只认非空 prereg_ref）——引用的协议/清单文件都在，且台账里有这一规则版本的目录
            if not all((_ROOT / r).exists() for r in c.evidence_refs) or not (_LEDGER / c.prereg_ref).is_dir():
                bad.append(cid)
        if bad:
            return False, f"主张 {bad} 未登记为前瞻预登记研究，或冻结身份（协议文件 / 台账规则版本目录）核对不上，不能以「验证中」展示"
        return True, "验证中（前瞻预登记检验未完成）——只展示、不进结论"
    if sec.role == "prediction":
        tiers = {cid: _claims.tier_of(cid) for cid in sec.claim_ids}
        if sec.claim_ids and all(t == "T1" for t in tiers.values()):
            return True, "已通过预登记检验（T1）"
        return False, f"未达 T1：{tiers}"
    return False, f"未知角色 {sec.role}"


def _allowed() -> dict:
    return {s.sid: section_allowed(s) for s in SECTIONS}


_CSS = ('<style>body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:1180px;margin:0 auto;padding:16px;'
        'color:#1f2328;background:#fff}.card{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:12px 0}'
        '.sub{color:#57606a;font-size:13px}table{border-collapse:collapse;width:100%;font-size:13px}'
        'td,th{border-bottom:1px solid #eaeef2;padding:5px 6px;text-align:left;vertical-align:top}.r{text-align:right}'
        '.pill{border-radius:10px;padding:1px 7px;font-size:12px}h1{font-size:20px}h2{font-size:16px}'
        '.warn{background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:8px 10px;margin:6px 0}'
        '.chart svg{max-width:100%;height:auto}a{color:#0969da}</style>')


def _page(title: str, body: str) -> str:
    return ('<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            f'<title>{_esc(title)}</title><meta name="viewport" content="width=device-width,initial-scale=1">'
            + _CSS + '</head><body>' + body + '</body></html>')


def _rules_card(generated_at: str) -> str:
    al = _allowed()
    return ('<div class="card"><b>准入规则</b>：观测类（持仓结构等数据事实）写明口径即可上；方向判断只有两种身份——'
            '<b>已通过预登记检验（T1）</b>，或<b>验证中</b>（醒目标注，只展示、不进结论、不作开仓依据）。'
            '<b>目前没有任何方向信号通过检验。</b>旧研报照常生成，其中大量指标尚未验证。'
            '<div class="sub" style="margin-top:6px">'
            + "；".join(f'{_esc(s.title)}（{"✅" if al[s.sid][0] else "⛔"} {_esc(al[s.sid][1])}）' for s in SECTIONS)
            + f'</div><div class="sub">生成于 {_esc(generated_at)} · {VERSION} · 只读，不构成投资建议</div></div>')


def _dir_text(d: dict | None) -> str:
    if not d:
        return "—"
    parts = []
    if d.get("conviction"):
        parts.append(f'多层同向：{d["conviction"]["reading"]}')
    if d.get("skew"):
        parts.append(f'偏斜：{d["skew"]["reading"]}')
    return "；".join(parts) or "—"


def render_index_html(day: str, items: list, *, generated_at: str) -> str:
    """首页：每个品种一行（现价、≤14 天最大 put/call OI、验证中的方向读数），点击进入详情页。"""
    rows = []
    for it in items:
        name = f'{_esc(it["name"])}（{_esc(it["symbol"])}）'
        if it["status"] != "ok":
            rows.append(f'<tr><td>{name}</td><td colspan="4" class="sub">⚠️ {_esc(it.get("why", "数据不可用"))}</td></tr>')
            continue
        ov = it["overview"]
        pw = "、".join(f"{k:g}（{v:,}）" for k, v in ov["agg_put_top"][:2]) or "—"
        cw = "、".join(f"{k:g}（{v:,}）" for k, v in ov["agg_call_top"][:2]) or "—"
        rows.append(f'<tr><td><a href="{_esc(it["detail_href"])}"><b>{name}</b></a></td><td class="r">{ov["spot"]:.2f}</td>'
                    f'<td>{pw}</td><td>{cw}</td><td>{_esc(_dir_text(it.get("direction")))}'
                    f' <span class="pill" style="background:#fff8c5;color:#7d4e00">验证中</span></td></tr>')
    body = (f'<h1>undertow 研报 v2 · {_esc(day)}</h1>' + _rules_card(generated_at)
            + '<div class="card"><h2>品种一览（点击进入详情）</h2>'
            '<div class="sub">现价为快照中的 ETF 价；≤14 天 put/call OI 前 2 为存量，不表示买卖方向；方向读数处于验证阶段，不作依据。</div>'
            '<table><tr><th>品种</th><th class="r">现价</th><th>≤14 天 put OI 前 2</th><th>≤14 天 call OI 前 2</th>'
            '<th>方向判断（验证中）</th></tr>' + "".join(rows) + '</table></div>')
    return _page(f"undertow 研报 v2 · {day}", body)


def _levels_rows(levels: list) -> str:
    from undertow.report.html import _levels_table
    return _levels_table(SimpleNamespace(key_levels=levels))


def _direction_card(d: dict | None) -> str:
    warn = ('<div class="warn">⚠️ <b>验证中</b>：以下是方向族 D 的冻结规则读数（预登记，2026-09-29 起前瞻积累）。'
            '检验尚未完成，<b>不能据此判断涨跌、不作开仓依据</b>；是否有效由事后统计决定，不看单日对错。</div>')
    if not d:
        return f'<div class="card"><h2>⑤ 方向判断（验证中）</h2>{warn}<div class="sub">本品种今日没有方向族 D 的记录。</div></div>'
    rows = []
    c = d.get("conviction")
    if c:
        f = c.get("features") or {}
        lay = " · ".join(f"{k}={f.get(k)}" for k in ("S", "F", "V", "H1"))
        rows.append(f'<tr><td>多层同向（conviction-h1-v1）</td><td><b>{_esc(c["reading"])}</b></td>'
                    f'<td class="sub">{_esc(lay)}；描述交易日 {_esc(c.get("quote_day") or "")}，冻结于 {_esc((c.get("recorded_at") or "")[:16])}Z</td></tr>')
    s = d.get("skew")
    if s:
        f = s.get("features") or {}
        det = (f'到期 {f.get("expiry")}；ATM IV {f.get("atm_iv_prev_pp")}→{f.get("atm_iv_curr_pp")}；'
               f'25Δ 偏斜 {f.get("skew25_prev_pp")}→{f.get("skew25_curr_pp")}；10Δ {f.get("skew10_prev_pp")}→{f.get("skew10_curr_pp")}')
        rows.append(f'<tr><td>偏斜读数（skew-reading-v1）</td><td><b>{_esc(s["reading"])}</b></td><td class="sub">{_esc(det)}</td></tr>')
    prog = d.get("progress") or {}
    ptxt = (f'累计：已记录 {prog.get("sessions", 0)} 个交易日，其中多层同向事件 {prog.get("events", 0)} 次'
            f'（起点 {_esc(prog.get("start", ""))}）；收益由统计脚本按冻结规则计分，本页不展示单日对错。')
    return (f'<div class="card"><h2>⑤ 方向判断（验证中）</h2>{warn}<table><tr><th>读数</th><th>今日</th><th>分量与时点</th></tr>'
            + "".join(rows) + f'</table><div class="sub" style="margin-top:6px">{ptxt}</div></div>')


def render_detail_html(day: str, it: dict, *, generated_at: str, index_href: str) -> str:
    """详情页：① 墙总览 ② 分层 ③ 关键点位与图 ④ 墙位历史 ⑤ 方向判断（验证中）。"""
    al = _allowed()
    title = f'{it["name"]}（{it["symbol"]}）'
    top = (f'<div class="sub"><a href="{_esc(index_href)}">← 返回品种一览</a></div>'
           f'<h1>{_esc(title)} · 研报 v2 · {_esc(day)}</h1>')
    if it["status"] != "ok":
        return _page(title, top + f'<div class="card">⚠️ {_esc(it.get("why", "数据不可用"))}</div>')
    meta = (f'<div class="sub">快照抓取 {_esc(it.get("captured_at") or "未知")}；'
            + (f'商品价换算比值 {it["ratio"]:.4f}（期货前收 ÷ ETF 快照价）' if it.get("ratio") else "无商品价换算")
            + f' · {VERSION}</div>')
    parts = [top, meta]
    if al["walls"][0]:
        parts.append(render_wall_overview_html(it["overview"], conv=it.get("conv"), etf_symbol=it["symbol"])
                     .replace("<h2>期权墙总览", "<h2>① 期权墙总览", 1))
    if al["layers"][0] and it.get("layers_html"):
        parts.append(it["layers_html"].replace("<h2>① 期权结构", "<h2>② 期权结构", 1))
    if al["levels"][0] and it.get("levels") is not None:
        parts.append('<div class="card"><h2>③ 期权关键点位 + 价格图 + 近价 OI 分布</h2>'
                     '<div class="sub">与旧研报同一套计算；「支撑/阻力/翻转」是沿用的称谓，位置本身未经检验。</div>'
                     + _levels_rows(it["levels"])
                     + f'<div class="chart">{it.get("price_svg") or ""}</div><div class="chart">{it.get("oi_svg") or ""}</div></div>')
    if al["history"][0] and it.get("history_html"):
        parts.append(it["history_html"].replace("<h2>③ 墙位历史", "<h2>④ 墙位历史", 1))
    if al["direction"][0]:
        parts.append(_direction_card(it.get("direction")))
    parts.append(f'<div class="sub">生成于 {_esc(generated_at)} · 只读，不构成投资建议</div>')
    return _page(title, "".join(parts))


def render_md(day: str, items: list, *, generated_at: str) -> str:
    L = [f"# undertow 研报 v2 · {day}", "",
         "> 观测类写明口径即可上；方向判断只有「T1 已验证」与「验证中」两种身份。目前没有方向信号通过检验。旧研报照常生成。",
         f"> 生成于 {generated_at} · {VERSION}", ""]
    for s in SECTIONS:
        ok, why = section_allowed(s)
        L.append(f"- 栏目「{s.title}」：{'✅' if ok else '⛔'} {why}")
    L.append("")
    for it in items:
        if it["status"] != "ok":
            L += [f"## {it['name']}（{it['symbol']}）", f"⚠️ {it.get('why', '数据不可用')}", ""]
            continue
        L.append(render_wall_overview_md(it["overview"], f"{it['name']}（{it['symbol']}）"))
        L.append(f"> 方向判断（验证中，不作依据）：{_dir_text(it.get('direction'))}\n")
    return "\n".join(L)
