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


def save_journal(entries: list[JournalEntry], path: Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"entries": [asdict(e) for e in entries]},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


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
