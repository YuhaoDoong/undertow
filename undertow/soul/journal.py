"""交易日记（trading journal）——记录"发生了什么"，与档案的"规则"互补。

档案（profile）回答**我该怎么做**；日记回答**我实际做了什么、为什么、结果如何、当时什么心情**。
两者一起才构成完整的交易体系：规则需要证据来演进，证据来自逐笔如实的记录。

每条日记包含：
  · **成交明细**（时间/合约/方向/张数/成交价/金额/手续费）——可从券商成交与流水**自动抓取**
  · **账户变化**（净资产/购买力 前后对比）
  · **当时的市场语境**（现价、研判、临近事件）
  · **复盘分析**（为什么这么做、哪条规则在起作用、做对了什么/做错了什么）
  · **盖棺定论**（一句话结论）
  · **心情**（如实写——情绪是交易数据的一部分，事后回看最有价值）

**隐私**：含真实成交与资金，落 gitignore 的 `data/soul/journal.json`，绝不进公开仓库。
**只读**：本模块只记录与分析，不下单。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_PATH = Path("data/soul/journal.json")


@dataclass(frozen=True)
class Trade:
    time: str                  # HH:MM（当地/ET，写清楚即可）
    symbol: str
    side: str                  # buy / sell
    qty: float
    price: float
    amount: float = 0.0        # 现金流（正=收入，负=支出）
    fee: float = 0.0           # 手续费（正数）
    note: str = ""


@dataclass(frozen=True)
class Thesis:
    """**事前判断**——开仓前写下，事后如实标记对错。

    这是回答"我到底有没有 edge"的唯一诚实方式：不靠回忆（记忆会挑出看对的），
    靠开仓前就落盘的白纸黑字。**关键设计：判断对错与交易盈亏【分开记】**——
    两者可以背离（判断对但仓位错→亏钱），而这个背离本身就是最重要的诊断信息。
    """
    id: str
    date: str                  # 写下判断的日期（必须早于或等于开仓日）
    instrument: str
    direction: str             # 看涨 / 看跌 / 中性震荡
    rationale: str = ""        # 依据（越具体越好，事后才能检验是哪条依据错了）
    time_frame: str = ""       # 时间预期
    invalidation: str = ""     # 失效条件（最重要——没有它就无法证伪）
    target: str = ""           # 目标位
    confidence: str = ""       # 信心（高/中/低）
    # —— 以下事后填 ——
    execution: str = "待定"     # 实盘 / 模拟(判断可行但前置未过) / 否决(判断本身不成立)
    veto_reason: str = ""      # 未实盘的原因（前置哪条没过 / 判断哪里不成立）
    outcome: str = "未验证"     # 对 / 错 / 部分对 / 未验证
    scored_at: str = ""
    review: str = ""           # 事后评述：哪条依据成立/不成立
    trade_pnl: float | None = None   # 对应交易的盈亏（可与 outcome 背离）


@dataclass(frozen=True)
class JournalEntry:
    date: str
    title: str = ""
    trades: list = field(default_factory=list)     # list[Trade]
    realized_pnl: float | None = None              # 已实现盈亏（不含未平仓）
    fees: float = 0.0
    net_assets_before: float | None = None
    net_assets_after: float | None = None
    buy_power_before: float | None = None
    buy_power_after: float | None = None
    context: str = ""                              # 当时的市场语境
    rules_applied: list = field(default_factory=list)   # 起作用的规则 id/描述
    analysis: str = ""                             # 复盘分析
    verdict: str = ""                              # 盖棺定论（一句话）
    mood: str = ""                                 # 心情（如实写）
    tags: list = field(default_factory=list)


def load_journal(path: Path | None = None) -> list[JournalEntry]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for r in raw.get("entries", []):
        out.append(JournalEntry(
            date=r.get("date", ""), title=r.get("title", ""),
            trades=[Trade(**t) for t in r.get("trades", [])],
            realized_pnl=r.get("realized_pnl"), fees=float(r.get("fees", 0) or 0),
            net_assets_before=r.get("net_assets_before"),
            net_assets_after=r.get("net_assets_after"),
            buy_power_before=r.get("buy_power_before"),
            buy_power_after=r.get("buy_power_after"),
            context=r.get("context", ""), rules_applied=r.get("rules_applied", []),
            analysis=r.get("analysis", ""), verdict=r.get("verdict", ""),
            mood=r.get("mood", ""), tags=r.get("tags", [])))
    return sorted(out, key=lambda e: e.date, reverse=True)


def save_journal(entries: list[JournalEntry], path: Path | None = None,
                 theses: list | None = None) -> Path:
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": [asdict(e) for e in entries]}
    if theses is not None:
        payload["theses"] = [asdict(t) for t in theses]
    else:
        payload["theses"] = [asdict(t) for t in load_theses(p)]
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_theses(path: Path | None = None) -> list[Thesis]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Thesis(**t) for t in raw.get("theses", [])]


def _hit(ts):
    n = len(ts)
    if not n:
        return None
    return (sum(1 for t in ts if t.outcome == "对")
            + 0.5 * sum(1 for t in ts if t.outcome == "部分对")) / n


def thesis_stats(theses: list) -> dict:
    """判断命中率 vs 交易盈亏——**两者背离才是关键诊断**。

    **模拟样本也计入命中率**：判断可行但被前置规则拦下的（仓位/冷静期/集中度），
    判断质量与实盘同样有效，且更干净（无执行滑点与仓位干扰）。这样样本积累快得多，
    "我到底有没有 edge" 才有可能被回答。
    """
    scored = [t for t in theses if t.outcome in ("对", "错", "部分对")]
    n = len(scored)
    live = [t for t in scored if t.execution == "实盘"]
    paper = [t for t in scored if t.execution == "模拟"]
    with_pnl = [t for t in live if t.trade_pnl is not None]
    diverge = [t for t in with_pnl if (t.outcome == "对" and t.trade_pnl < 0)
               or (t.outcome == "错" and t.trade_pnl > 0)]
    missed = [t for t in paper if t.outcome == "对"]          # 判断对但没做
    should_veto = [t for t in live if t.outcome == "错"]       # 判断错却做了
    return {
        "total": len(theses), "scored": n,
        "hit_rate": _hit(scored), "hit_live": _hit(live), "hit_paper": _hit(paper),
        "n_live": len(live), "n_paper": len(paper),
        "diverge_count": len(diverge),
        "diverge_pnl": sum(t.trade_pnl for t in diverge) if diverge else 0.0,
        "missed_count": len(missed), "should_veto_count": len(should_veto),
        "enough_sample": n >= 30,
        "note": ("样本 ≥30，可初步谈命中率" if n >= 30 else
                 f"样本仅 {n} 笔（实盘{len(live)}/模拟{len(paper)}），"
                 f"**远不足以区分运气与能力**——按『没有 edge』管理风险"),
    }


def render_theses_md(theses: list) -> str:
    st = thesis_stats(theses)
    L = ["## 🎯 事前判断记录", ""]
    L.append(f"*共 {st['total']} 条，已验证 {st['scored']} 条　·　{st['note']}*")
    if st["hit_rate"] is not None:
        L.append(f"*命中率 {st['hit_rate']*100:.0f}%（仅供参考，样本不足时无统计意义）*")
    if st["hit_live"] is not None or st["hit_paper"] is not None:
        hl = f"{st['hit_live']*100:.0f}%" if st["hit_live"] is not None else "—"
        hp = f"{st['hit_paper']*100:.0f}%" if st["hit_paper"] is not None else "—"
        L.append(f"*实盘命中 {hl}（{st['n_live']}笔）　·　模拟命中 {hp}（{st['n_paper']}笔）"
                 f"—— 两者差异大说明【选择做哪笔】本身有问题*")
    if st["diverge_count"]:
        L.append(f"*⚠️ **判断对但亏钱/判断错但赚钱** {st['diverge_count']} 笔，"
                 f"合计 {st['diverge_pnl']:+,.0f} —— 这个背离说明问题不在判断力*")
    if st["missed_count"] or st["should_veto_count"]:
        L.append(f"*判断对但没做 {st['missed_count']} 笔（前置是否过严？）　·　"
                 f"判断错却做了 {st['should_veto_count']} 笔（该拦没拦住）*")
    L.append("")
    for t in theses:
        icon = {"对": "✅", "错": "❌", "部分对": "🟡"}.get(t.outcome, "⏳")
        exe = {"实盘": "💵实盘", "模拟": "📝模拟", "否决": "🚫否决"}.get(t.execution, "⏳待定")
        L.append(f"### {icon} {t.date}　{t.instrument}　**{t.direction}**　`{t.outcome}`　{exe}")
        if t.veto_reason:
            L.append(f"- **未实盘原因**：{t.veto_reason}")
        if t.rationale:
            L.append(f"- **依据**：{t.rationale}")
        if t.time_frame:
            L.append(f"- **时间预期**：{t.time_frame}")
        if t.invalidation:
            L.append(f"- **失效条件**：{t.invalidation}")
        if t.target:
            L.append(f"- **目标**：{t.target}")
        if t.trade_pnl is not None:
            L.append(f"- **对应交易盈亏**：{t.trade_pnl:+,.2f}")
        if t.review:
            L.append(f"- **事后评述**：{t.review}")
        L.append("")
    return "\n".join(L)


def capture_trades(executions, cash_flows=None, day: str = "") -> list[Trade]:
    """从券商成交（+资金流水补金额/费用）自动抓当日成交明细。纯函数。

    executions: [{time, symbol, side, quantity, price}, ...]
    cash_flows: [{time, flow_name, balance, symbol, description}, ...]（可选，用于补金额与费用）
    """
    amt, fee = {}, {}
    for c in (cash_flows or []):
        sym = (c.get("symbol") or "").strip()
        name = (c.get("flow_name") or "")
        try:
            bal = float(c.get("balance", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not sym:
            continue
        if "Fee" in name:
            fee[sym] = fee.get(sym, 0.0) + abs(bal)
        elif "Transaction" in name:
            amt[sym] = amt.get(sym, 0.0) + bal
    out = []
    for e in executions:
        t = str(e.get("time", ""))
        if day and not t.startswith(day):
            continue
        sym = e.get("symbol", "")
        try:
            qty, px = float(e.get("quantity", 0)), float(e.get("price", 0))
        except (TypeError, ValueError):
            continue
        out.append(Trade(time=t[11:16], symbol=sym, side=str(e.get("side", "")).lower(),
                         qty=qty, price=px, amount=amt.get(sym, 0.0), fee=fee.get(sym, 0.0)))
    return sorted(out, key=lambda x: x.time)


def render_entry_md(e: JournalEntry) -> str:
    L = [f"## {e.date}　{e.title}" if e.title else f"## {e.date}", ""]
    if e.context:
        L.append(f"**当时语境**：{e.context}")
        L.append("")
    if e.trades:
        L.append("**成交明细**")
        L.append("")
        L.append("| 时间 | 合约 | 方向 | 张数 | 成交价 | 金额 | 费用 |")
        L.append("|---|---|---|---:|---:|---:|---:|")
        for t in e.trades:
            L.append(f"| {t.time} | `{t.symbol}` | {'买入' if t.side=='buy' else '卖出'} | "
                     f"{t.qty:g} | {t.price:.2f} | {t.amount:+,.2f} | {t.fee:.2f} |")
        L.append("")
    bits = []
    if e.realized_pnl is not None:
        bits.append(f"已实现盈亏 **{e.realized_pnl:+,.2f}**")
    if e.fees:
        bits.append(f"手续费 {e.fees:.2f}")
    if e.net_assets_before is not None and e.net_assets_after is not None:
        bits.append(f"净资产 {e.net_assets_before:,.2f} → **{e.net_assets_after:,.2f}**")
    if e.buy_power_before is not None and e.buy_power_after is not None:
        bits.append(f"购买力 {e.buy_power_before:,.2f} → **{e.buy_power_after:,.2f}**")
    if bits:
        L.append("　·　".join(bits))
        L.append("")
    if e.rules_applied:
        L.append("**起作用的规则**：" + "、".join(f"`{r}`" for r in e.rules_applied))
        L.append("")
    if e.analysis:
        L.append("**复盘**")
        L.append("")
        L.append(e.analysis)
        L.append("")
    if e.verdict:
        L.append(f"> **盖棺定论**：{e.verdict}")
        L.append("")
    if e.mood:
        L.append(f"> 💭 **心情**：{e.mood}")
        L.append("")
    if e.tags:
        L.append("　".join(f"`#{t}`" for t in e.tags))
        L.append("")
    return "\n".join(L)


def render_journal_md(entries: list[JournalEntry], limit: int = 0) -> str:
    L = ["# 交易日记（本地私有 · 只读记录）", ""]
    if not entries:
        L.append("- 暂无记录。用 `undertow journal --capture` 抓当日成交起头。")
        return "\n".join(L)
    tot = sum(e.realized_pnl or 0 for e in entries)
    L.append(f"*共 {len(entries)} 条记录　累计已实现盈亏 **{tot:+,.2f}***")
    L.append("")
    show = entries[:limit] if limit else entries
    for e in show:
        L.append(render_entry_md(e))
        L.append("---")
        L.append("")
    return "\n".join(L)
