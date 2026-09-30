"""新研报体系 v2：只放「已证实」的内容（用户 2026-09-30：「你新建个研报体系吧，先只放期权墙。后面我们证实哪些信号有用，就放什么上去。之前的研报也照常出。」）

准入规则（写死在 SECTIONS）：
- observation（持仓结构、数据事实，不表达方向判断）：写明数据口径即可上。当前只有「期权墙总览」。
- prediction（方向或信号类）：必须在 analyze/claims.py 里是 T1（通过预登记检验）才能上；T2/T3、未登记一律不上。
  这由 section_allowed() 在渲染时逐项判定，不靠人工记得。
旧研报（cli report）照常生成，与本体系互不影响。只吃 analyze 层结果，不取数、不下单。
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape as _esc

from undertow.analyze import claims as _claims
from undertow.report.html import render_wall_overview_html
from undertow.report.markdown import render_wall_overview_md

VERSION = "report-v2-20260930"


@dataclass(frozen=True)
class Section:
    sid: str
    title: str
    role: str                     # "observation" | "prediction"
    claim_id: str | None = None   # prediction 必填：对应 claims.py 的主张
    basis: str = ""               # 为什么能上（口径/证据）


SECTIONS = (
    Section("walls", "期权墙总览（按到期拆分）", "observation",
            basis="期权持仓结构展示：OI 为前一交易日结算的存量，不表达方向判断，也不宣称支撑/压力已被验证。"),
)


def section_allowed(sec: Section) -> tuple[bool, str]:
    """准入判定：observation 直接允许；prediction 必须是 claims 里的 T1。"""
    if sec.role == "observation":
        return True, "观测类（不表达方向判断）"
    if sec.role == "prediction":
        tier = _claims.tier_of(sec.claim_id) if sec.claim_id else None
        if tier == "T1":
            return True, f"主张 {sec.claim_id} 为 T1（已通过预登记检验）"
        return False, f"主张 {sec.claim_id} 为 {tier or '未登记'}，未达 T1，不上本研报"
    return False, f"未知角色 {sec.role}"


def render_html(day: str, items: list, *, generated_at: str) -> str:
    """items：[{inst, name, symbol, status, overview, conv, captured_at, why}]。"""
    allowed = [(s, *section_allowed(s)) for s in SECTIONS]
    head = ('<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            f'<title>undertow 研报 v2 · {_esc(day)}</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:1100px;margin:0 auto;padding:16px;'
            'color:#1f2328;background:#fff}.card{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:12px 0}'
            '.sub{color:#57606a;font-size:13px}table{border-collapse:collapse;width:100%;font-size:13px}'
            'td,th{border-bottom:1px solid #eaeef2;padding:5px 6px;text-align:left;vertical-align:top}'
            '.pill{border-radius:10px;padding:1px 7px;font-size:12px}h1{font-size:20px}h2{font-size:16px}</style></head><body>')
    intro = (f'<h1>undertow 研报 v2 · {_esc(day)}</h1>'
             '<div class="card"><b>准入规则</b>：本研报只放已证实的内容。观测类（持仓结构等数据事实）写明口径即可上；'
             '方向或信号类必须在主张登记表里达到 T1（通过预登记检验）才能上。'
             '<b>目前没有任何方向信号达到 T1，所以本研报不给方向判断。</b>旧研报照常生成，其中大量指标尚未验证。'
             '<div class="sub" style="margin-top:6px">栏目：'
             + "；".join(f'{_esc(s.title)}（{"✅" if ok else "⛔"} {_esc(why)}）' for s, ok, why in allowed)
             + f'</div><div class="sub">生成于 {_esc(generated_at)} · {VERSION} · 只读，不构成投资建议</div></div>')
    body = []
    walls_ok = next(ok for s, ok, _ in allowed if s.sid == "walls")
    for it in items:
        if it["status"] != "ok":
            body.append(f'<div class="card"><h2>{_esc(it["name"])}（{_esc(it["symbol"])}）</h2>'
                        f'<div class="sub">⚠️ {_esc(it.get("why", "数据不可用"))}</div></div>')
            continue
        block = f'<h2 style="margin-top:22px">{_esc(it["name"])}（{_esc(it["symbol"])}）</h2>'
        block += (f'<div class="sub">快照抓取 {_esc(it.get("captured_at") or "未知")}；'
                  + (f'商品价换算比值 {it["ratio"]:.4f}（期货前收 ÷ ETF 前收）' if it.get("ratio") else "无商品价换算")
                  + '</div>')
        if walls_ok:
            block += render_wall_overview_html(it["overview"], conv=it.get("conv"), etf_symbol=it["symbol"])
        body.append(block)
    return head + intro + "".join(body) + "</body></html>"


def render_md(day: str, items: list, *, generated_at: str) -> str:
    L = [f"# undertow 研报 v2 · {day}", "",
         "> 只放已证实的内容：观测类写明口径即可上；方向/信号类须为 T1。目前没有方向信号达到 T1，本研报不给方向判断。旧研报照常生成。",
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
        L.append(f"> 快照抓取 {it.get('captured_at') or '未知'}；"
                 + (f"商品价换算比值 {it['ratio']:.4f}（期货前收 ÷ ETF 快照价）" if it.get("ratio") else "无商品价换算") + "\n")
    return "\n".join(L)
