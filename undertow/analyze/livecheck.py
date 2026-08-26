"""持仓实时体检 —— 纯确定性，无 I/O（行情由调用方喂入）。

**为什么需要它**：券商 App 的「持仓盈亏」用 last 价算。对流动性差的腿，last 往往
就是你自己成交的那个价，于是显示的浮盈是【卖不掉的浮盈】。

2026-08-26 TQQQ 76/80 实测，同一时刻三个口径：
    App（last）      1.68 - 0.68 = 1.00 = $100  →  +$10  ✅
    中价             1.52 - 0.67 = 0.85 = $ 85  →  -$5
    真实可平仓        1.37 - 0.70 = 0.67 = $ 67  →  -$23  ❌
差 $33。若拿 App 的数去比对止损线，会系统性地晚动手。

故本模块一律按【真实可平仓价】计价：
    多头腿按 bid 卖出、空头腿按 ask 买回 —— 这才是「现在就走能拿回多少」。
中价同时给出作参照（组合单常能成交在中价附近，做市商直接对价差报价），
但**止损判定必须用可平仓价**，那是最坏情形下的真实处境。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LegQuote:
    """一条腿的实时盘口。bid/ask 缺失时为 None——单边空档是常态，不许用 last 顶替。"""
    symbol: str
    qty: int                      # 正=多头，负=空头
    bid: float | None = None
    ask: float | None = None
    last: float | None = None

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2.0 if (self.bid and self.ask) else None

    def exit_price(self) -> float | None:
        """平掉这条腿的成交价：多头卖给买盘(bid)，空头买自卖盘(ask)。"""
        return self.bid if self.qty > 0 else self.ask


@dataclass(frozen=True)
class PositionCheck:
    ok: bool
    name: str = ""
    cost: float | None = None          # 建仓成本（每股口径 × 100）
    exit_value: float | None = None    # 真实可平仓价值（$）
    mid_value: float | None = None     # 中价口径（$）
    last_value: float | None = None    # last 口径（$，即 App 显示）
    pnl_exit: float | None = None
    pnl_last: float | None = None
    gap: float | None = None           # last 口径 与 可平仓口径 的差额
    stop: float | None = None          # 止损线（$，可平仓口径）
    target: float | None = None        # 止盈线（$）
    to_stop_pct: float | None = None   # 距止损还有多少（占当前可平仓值）
    note: str = ""
    warnings: list = field(default_factory=list)


def _value(legs, price_fn) -> float | None:
    """按给定取价方式汇总组合价值（$）。任一腿缺价则返回 None——不猜。"""
    tot = 0.0
    for l in legs:
        p = price_fn(l)
        if p is None:
            return None
        tot += p * l.qty * 100
    return tot


def check_position(name: str, legs: list, *, cost: float | None = None,
                   stop: float | None = None, target: float | None = None,
                   gap_warn_pct: float = 15.0) -> PositionCheck:
    """把一组腿的实时盘口翻成「现在走能拿回多少 / 离止损多远」。"""
    if not legs:
        return PositionCheck(ok=False, note="无持仓腿")
    ev = _value(legs, lambda l: l.exit_price())
    mv = _value(legs, lambda l: l.mid)
    lv = _value(legs, lambda l: l.last)
    warns = []
    if ev is None:
        warns.append("盘口单边缺失，算不出真实可平仓价——此时任何浮盈都不可信")
    # App 口径与可平仓口径的差距：这是最容易让人误判的一项
    gap = (lv - ev) if (lv is not None and ev is not None) else None
    if gap is not None and ev and abs(gap) / max(abs(ev), 1e-9) * 100 >= gap_warn_pct:
        warns.append(f"App(last)口径比真实可平仓高 ${gap:,.0f}"
                     f"（{abs(gap)/max(abs(ev),1e-9)*100:.0f}%）——止损判定别看 App")
    to_stop = None
    if ev is not None and stop is not None and ev:
        to_stop = (ev - stop) / abs(ev) * 100
        if to_stop <= 0:
            warns.append(f"⚠️ 已触及止损线：可平仓 ${ev:,.0f} ≤ 止损 ${stop:,.0f}")
        elif to_stop < 15:
            warns.append(f"接近止损线：还剩 {to_stop:.0f}%")
    if ev is not None and target is not None and ev >= target:
        warns.append(f"✅ 已达止盈线：可平仓 ${ev:,.0f} ≥ 目标 ${target:,.0f}")
    return PositionCheck(
        ok=True, name=name, cost=cost, exit_value=ev, mid_value=mv, last_value=lv,
        pnl_exit=(ev - cost) if (ev is not None and cost is not None) else None,
        pnl_last=(lv - cost) if (lv is not None and cost is not None) else None,
        gap=gap, stop=stop, target=target, to_stop_pct=to_stop, warnings=warns)


def render_md(checks: list, net_assets: float | None = None) -> str:
    L = ["# 持仓实时体检（长桥实时盘口 · 按【真实可平仓价】计）", ""]
    L.append("> 多头腿按 bid 卖、空头腿按 ask 买回——这才是「现在就走能拿回多少」。")
    L.append("> 券商 App 的持仓盈亏用 last 价，对流动性差的腿会系统性高估。**止损判定用本表。**")
    L.append("")
    L.append("| 持仓 | 成本 | 真实可平仓 | 中价 | App(last) | 盈亏(可平仓) | 盈亏(App) | 距止损 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    tot = 0.0
    for c in checks:
        if not c.ok:
            continue
        f = lambda v: f"${v:,.0f}" if v is not None else "—"
        pl = f"{c.pnl_exit:+,.0f}" if c.pnl_exit is not None else "—"
        pa = f"{c.pnl_last:+,.0f}" if c.pnl_last is not None else "—"
        ts = f"{c.to_stop_pct:.0f}%" if c.to_stop_pct is not None else "—"
        L.append(f"| {c.name} | {f(c.cost)} | **{f(c.exit_value)}** | {f(c.mid_value)} | "
                 f"{f(c.last_value)} | {pl} | {pa} | {ts} |")
        if c.exit_value:
            tot += c.exit_value
    L.append("")
    if net_assets:
        L.append(f"**总敞口（可平仓口径）：${tot:,.0f} = 净资产 {tot/net_assets*100:.1f}%**")
        L.append("")
    for c in checks:
        for w in c.warnings:
            L.append(f"- {c.name}：{w}")
    return "\n".join(L)
