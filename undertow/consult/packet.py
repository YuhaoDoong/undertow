"""咨询上下文包（consultation packet）——模型无关的 AI 接入层。

把 undertow 的确定性研判 + 持仓评价 + 体检 + 用户问题，装进一个**JSON 可序列化**的包，
外加一段渲染好的 prompt。任何 AI（本地的 Claude、或用户自己接的 GPT/Gemini/本地模型）
都能消费这个包来"给意见"——但**所有数字都在包里、由上游确定性模块算好，AI 只解读、不臆算**。

这就是"开放 API 给其它 AI"的载体：
  - `undertow consult` 命令打印这个包（--json 机器用 / markdown 人用）；
  - `undertow serve` 用标准库 http.server 把它暴露成本地只读 HTTP 端点。

**边界**：只读、只给波段级风险情景，绝不下单；AI 的输出不构成投资建议。
"""
from __future__ import annotations

import dataclasses
from datetime import date

# 交给 AI 的硬规则（放进包里，任何模型都看得到）
GUIDANCE = [
    "所有数字（价位、盈亏比、盈亏平衡、Delta、最大盈亏、资金）都已在本包内由 undertow "
    "确定性模块算好；你只解读、组织与权衡，**不得自行改动或臆算任何数字**（LLM 不碰算术）。",
    "方向研判以 instruments[].bias/near_bias/mid_bias/verdict_head 为准；持仓结构以 "
    "portfolio.groups[].combos 为准；风险以 healthcheck 为准。",
    "输出是**波段级风险情景与权衡参考，不是投资建议、不是交易指令**；是否下单、下什么，由用户自己决定。",
    "你（及任何接入的 AI）**只读**：绝不代替用户下单/撤单/改单；执行永远由用户在券商端完成。",
    "如信息不足以回答（如缺某标的的期权代理、快照过期），如实说明，不要编造。",
]


def _jsonable(obj):
    """把 dataclass / date / 嵌套结构转成 JSON 安全对象。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return str(obj)


def _instrument_brief(ctx) -> dict:
    return {
        "etf_symbol": ctx.etf_symbol, "display_name": ctx.display_name,
        "spot": ctx.spot, "call_wall": ctx.call_wall, "put_wall": ctx.put_wall,
        "zero_gamma": ctx.zero_gamma, "bias": ctx.bias,
        "near_bias": ctx.near_bias, "mid_bias": ctx.mid_bias,
        "verdict_head": ctx.verdict_head, "proxy_quality": ctx.proxy_quality,
        "spot_source": getattr(ctx, "spot_source", "snapshot"),
        "price_note": getattr(ctx, "price_note", ""),
    }


def _portfolio_brief(review) -> dict:
    groups = []
    for g in review.groups:
        groups.append({
            "underlying": g.underlying, "display_name": g.display_name,
            "net_delta": g.net_delta, "total_pnl": g.total_pnl,
            "bias": g.bias, "verdict_head": g.verdict_head,
            "stance": g.stance, "capital_note": g.capital_note,
            "combos": [_jsonable(c) for c in g.combos],
            "legs": [{
                "name": l.name, "symbol": l.symbol, "side": l.side, "qty": l.qty,
                "kind": l.kind, "strike": l.strike,
                "expiry": l.expiry.isoformat() if l.expiry else None, "dte": l.dte,
                "moneyness": l.moneyness, "align": l.align, "pnl": l.pnl,
                "pos_delta": l.pos_delta, "flags": list(l.flags), "comment": l.comment,
            } for l in g.legs],
            "advice": list(g.advice), "summary": g.summary,
        })
    return {"headline": review.headline, "asof": review.asof.isoformat(),
            "groups": groups,
            "unmapped": [l.name for l in review.unmapped]}


def build_consult_packet(*, review, health, contexts, capital=None,
                         question: str = "", mode: str = "review",
                         pre_trade=None, asof: date) -> dict:
    """组装咨询包。

    review：当前持仓 PortfolioReview；health：list[HealthFinding]；
    contexts：{ETF根: InstrumentContext}；capital：AccountCapital|None；
    question：用户的问题；mode："review"（复盘现仓）/"pre_trade"（开仓前问诊）；
    pre_trade：{"spec":..., "review":PortfolioReview, "health":[...]}（拟开仓的评价，可选）。
    """
    packet = {
        "schema": "undertow.consult/v1",
        "mode": mode,
        "asof": asof.isoformat(),
        "question": question,
        "guidance": GUIDANCE,
        "account": ({
            "buy_power": capital.buy_power, "net_assets": capital.net_assets,
            "cash_usd": capital.cash_usd,
        } if capital is not None else None),
        "instruments": {k: _instrument_brief(v) for k, v in contexts.items()},
        "portfolio": _portfolio_brief(review),
        "healthcheck": [_jsonable(f) for f in (health or [])],
    }
    if pre_trade is not None:
        packet["pre_trade"] = {
            "spec": pre_trade.get("spec", ""),
            "portfolio": _portfolio_brief(pre_trade["review"]) if pre_trade.get("review") else None,
            "healthcheck": [_jsonable(f) for f in pre_trade.get("health", [])],
        }
    packet["prompt"] = render_prompt(packet)
    return packet


def render_prompt(packet: dict) -> str:
    """把包渲染成一段可直接投喂任意 LLM 的 prompt（人也可读）。"""
    L = []
    L.append("你是一名期权持仓风控助手，基于下方 undertow 确定性引擎给出的结构化上下文，"
             "帮用户复盘/研判持仓或评估拟开仓。请严格遵守：")
    for g in packet["guidance"]:
        L.append(f"  - {g}")
    L.append("")
    if packet.get("account"):
        a = packet["account"]
        L.append(f"【账户】净资产 ${a['net_assets']:,.0f} · 购买力 ${a['buy_power']:,.0f} · "
                 f"美元现金 ${a['cash_usd']:,.0f}")
    L.append(f"【基准日】{packet['asof']}　【模式】{packet['mode']}")
    L.append("")
    L.append("【品种研判】")
    for k, ins in packet["instruments"].items():
        L.append(f"  · {ins['display_name']}({k})：综合 {ins['bias']}（近 {ins['near_bias']}/"
                 f"中 {ins['mid_bias']}）；现价 {ins['spot']:.2f}；put墙 {ins['put_wall']:.1f}/"
                 f"call墙 {ins['call_wall']:.1f}；决策：{ins['verdict_head']}")
    L.append("")
    L.append("【当前持仓】" + packet["portfolio"]["headline"])
    for g in packet["portfolio"]["groups"]:
        L.append(f"  ▸ {g['display_name']}：整体姿态 {g['stance']}")
        if g["capital_note"]:
            L.append(f"    资金：{g['capital_note']}")
        for c in g["combos"]:
            ml = "风险未封顶" if c["max_loss"] is None else f"最大亏 ${abs(c['max_loss']):,.0f}"
            mp = "—" if c["max_profit"] is None else f"最大盈 ${c['max_profit']:,.0f}"
            L.append(f"    - {c['label']}〔{c['stance']}〕{c['note']}（{c['expiry_label']}）→ {mp} / {ml}")
        for a in g["advice"]:
            L.append(f"    建议：{a}")
    if packet["healthcheck"]:
        L.append("")
        L.append("【持仓体检】")
        for f in packet["healthcheck"]:
            L.append(f"  [{f['severity']}] {f['title']}：{f['detail']} → 参考：{f['suggestion']}")
    if packet.get("pre_trade"):
        pt = packet["pre_trade"]
        L.append("")
        L.append(f"【拟开仓评估】spec={pt['spec']}")
        if pt.get("portfolio"):
            for g in pt["portfolio"]["groups"]:
                for c in g["combos"]:
                    ml = "风险未封顶" if c["max_loss"] is None else f"最大亏 ${abs(c['max_loss']):,.0f}"
                    mp = "—" if c["max_profit"] is None else f"最大盈 ${c['max_profit']:,.0f}"
                    L.append(f"  - {c['label']}〔{c['stance']}〕{c['note']} → {mp} / {ml}")
        for f in pt.get("healthcheck", []):
            L.append(f"  体检[{f['severity']}] {f['title']}：{f['detail']}")
    L.append("")
    L.append(f"【用户的问题】{packet['question'] or '（未指定，请给出该持仓的整体风控复盘与权衡）'}")
    L.append("")
    L.append("请用中文回答：先给一句结论，再分点解释（结构/方向/风险/资金），"
             "最后明确这是波段级情景参考、非投资建议、执行由用户自定。")
    return "\n".join(L)
