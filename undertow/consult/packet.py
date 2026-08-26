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
    "若本包含 thesis（用户的事前判断）：**必须先基于确定性数据形成你自己的独立判读，再逐条对照**——"
    "指出①哪些依据与数据一致、②哪些分歧（数据说了什么相反的）、③用户遗漏了什么、"
    "④用户看到但数据未反映的（这可能是真 edge，也可能是错觉）。**绝不因为用户已有判断就顺着找证据**；"
    "结论要能是『不做』——不做是完全正常的结局，且比勉强找理由做更有价值。",
    "若本包含 soul（用户专属交易体系）：**它优先于一切**——不得建议任何违反其铁律的做法；"
    "当用户的提问本身流露出档案里记录的已知弱点（如回本心态、追损、想一击翻倍），**要直接点出来**，"
    "而不是顺着回答。诚实优先于顺从。",
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
        "technicals": _tech_brief(getattr(ctx, "technicals", None)),
    }


def _tech_brief(tr) -> dict | None:
    if tr is None or not getattr(tr, "ok", False):
        return None
    return {
        "headline": tr.headline, "trend": tr.trend,
        "heat": tr.heat, "heat_score": tr.heat_score,
        "rsi6": tr.rsi6, "rsi14": tr.rsi14,
        "kdj_j": tr.kdj[2] if tr.kdj else None,
        "cci": tr.cci, "bias6": tr.bias6,
        "boll_pctb": tr.boll[3] if tr.boll else None,
        "macd_above_zero": (tr.macd[0] > 0) if tr.macd else None,
        "rets": tr.rets,
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


def _news_brief(digests) -> list:
    """把 NewsDigest 列表转 JSON 安全结构。"""
    out = []
    for dg in digests or []:
        out.append({
            "instrument": dg.instrument, "display_name": dg.display_name,
            "alert": dg.alert, "alert_level": dg.alert_level,
            "events": [{"date": e.date.isoformat(), "days_until": e.days_until(dg.asof),
                        "name": e.name, "importance": e.importance,
                        "forecast": e.forecast, "previous": e.previous} for e in dg.events],
            "news": [{"date": it.published_date.isoformat() if it.published_date else None,
                      "title": it.title} for it in dg.items],
        })
    return out


def _soul_brief(profile, violations) -> dict | None:
    """用户专属交易体系（灵魂档案）+ 当前纪律核查结果。"""
    if profile is None or not getattr(profile, "ok", False):
        return None
    lim = profile.limits
    return {
        "north_star": profile.north_star, "phase": profile.phase,
        "rules": [{"severity": r.severity, "text": r.text, "why": r.why} for r in profile.rules],
        "weaknesses": [{"name": w.name, "trigger": w.trigger, "counter": w.counter}
                       for w in profile.weaknesses],
        "limits": {k: v for k, v in _jsonable(lim).items() if v not in (None, False)},
        "violations": [{"severity": v.severity, "title": v.title, "detail": v.detail}
                       for v in (violations or [])],
    }


def _thesis_brief(th) -> dict | None:
    """用户的事前判断（开仓前落盘，供 AI 逐条检验；AI 须先独立判读再对照）。"""
    if not th:
        return None
    return {k: getattr(th, k, "") for k in
            ("id", "date", "instrument", "direction", "rationale",
             "time_frame", "invalidation", "target", "confidence")}


def build_consult_packet(*, review, health, contexts, capital=None,
                         question: str = "", mode: str = "review",
                         pre_trade=None, news=None, soul=None, thesis=None, asof: date) -> dict:
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
        "news": _news_brief(news),
        "soul": _soul_brief(*(soul if soul else (None, None))),
        "thesis": _thesis_brief(thesis),
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
    sl = packet.get("soul")
    if sl:
        L.append("【用户专属交易体系（灵魂档案）—— 回答必须遵守这些规则，不得建议破戒】")
        if sl.get("north_star"):
            L.append(f"  总纲：{sl['north_star']}")
        if sl.get("phase"):
            L.append(f"  当前阶段：{sl['phase']}")
        for r in sl.get("rules", []):
            L.append(f"  [{r['severity']}] {r['text']}")
        if sl.get("limits"):
            lim = "、".join(f"{k}={v}" for k, v in sl["limits"].items())
            L.append(f"  机器限额：{lim}")
        if sl.get("weaknesses"):
            L.append("  已知弱点（提问里若出现这些苗头，请直接点出来）：")
            for w in sl["weaknesses"]:
                L.append(f"    · {w['name']}｜触发：{w['trigger']}｜对策：{w['counter']}")
        if sl.get("violations"):
            L.append("  ⚠️ 当前持仓已触碰的自定纪律：")
            for v in sl["violations"]:
                L.append(f"    · [{v['severity']}] {v['title']}：{v['detail']}")
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
        tc = ins.get("technicals")
        if tc:
            L.append(f"      技术面：{tc['trend']} · 短线{tc['heat']}(过热分{tc['heat_score']:+d})"
                     + (f" · RSI6 {tc['rsi6']:.0f}" if tc.get("rsi6") is not None else "")
                     + (f"/KDJ-J {tc['kdj_j']:.0f}" if tc.get("kdj_j") is not None else "")
                     + (f"/CCI {tc['cci']:.0f}" if tc.get("cci") is not None else ""))
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
    if packet.get("news"):
        L.append("")
        L.append("【事件感知（新闻 + 临近关键事件 · 只作背景/催化剂旁证，不改判方向）】")
        for nb in packet["news"]:
            if nb["alert"]:
                L.append(f"  {nb['display_name']}：{nb['alert']}")
            for e in nb["events"][:6]:
                imp = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(e["importance"], "")
                fp = f"（预测 {e['forecast'] or '—'}/前值 {e['previous'] or '—'}）" if (e["forecast"] or e["previous"]) else ""
                L.append(f"    · {e['date']}（{e['days_until']}天后）{imp} {e['name']}{fp}")
            for it in nb["news"][:5]:
                L.append(f"    - [{it['date'] or '—'}] {it['title']}")
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
    th = packet.get("thesis")
    if th:
        L.append("")
        L.append("【用户的事前判断（开仓前落盘）—— 请先用上面的确定性数据形成你自己的独立判读，再逐条对照】")
        L.append(f"  方向：{th.get('direction','')}")
        if th.get("rationale"):
            L.append(f"  依据：{th['rationale']}")
        if th.get("time_frame"):
            L.append(f"  时间预期：{th['time_frame']}")
        if th.get("invalidation"):
            L.append(f"  失效条件：{th['invalidation']}")
        if th.get("target"):
            L.append(f"  目标：{th['target']}")
        L.append("  → 请给出：①你的独立判读　②逐条对照（一致/分歧/遗漏/用户独有）　"
                 "③前置检查是否通过（soul 限额）　④结论：实盘 / 模拟(判断可行但前置未过) / 否决(判断本身不成立)，并给理由")
    L.append("")
    L.append(f"【用户的问题】{packet['question'] or '（未指定，请给出该持仓的整体风控复盘与权衡）'}")
    L.append("")
    L.append("请用中文回答：先给一句结论，再分点解释（结构/方向/风险/资金），"
             "最后明确这是波段级情景参考、非投资建议、执行由用户自定。")
    return "\n".join(L)
