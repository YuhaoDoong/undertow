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
    """一条腿的实时盘口。bid/ask 缺失时为 None——单边空档是常态，不许用 last 顶替。

    Codex 008 G04：报价要带身份。bid_size/ask_size=None 表示数量未知（只能作报价估值）；
    出场一侧数量为 0 → 该价不可成交，exit_price 返回 None。quote_time 为 None 表示源不提供时间
    （长桥 depth 即如此），不能据此判断报价新旧。
    """
    symbol: str
    qty: int                      # 正=多头，负=空头
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    source: str = ""
    quote_time: str | None = None

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2.0 if (self.bid and self.ask) else None

    def exit_size(self) -> float | None:
        return self.bid_size if self.qty > 0 else self.ask_size

    def exit_price(self) -> float | None:
        """平掉这条腿的成交价：多头卖给买盘(bid)，空头买自卖盘(ask)。该侧挂单量为 0 → None（不可成交）。"""
        sz = self.exit_size()
        if sz is not None and sz <= 0:
            return None
        return self.bid if self.qty > 0 else self.ask

    @property
    def root(self) -> str:
        import re
        m = re.match(r"([A-Z]+)", self.symbol.upper())
        return m.group(1) if m else self.symbol


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
    roots: tuple = ()                  # 标的根代码（按产品判收市时刻）
    size_unverified: bool = False      # 有腿的出场数量未知 → 可平仓价只是报价估值
    time_unknown: bool = False         # 报价源不给时间


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
    zero = [l.symbol for l in legs if l.exit_size() is not None and l.exit_size() <= 0]
    if zero:
        warns.append(f"出场一侧挂单量为 0：{'、'.join(zero)} —— 该价不可成交，可平仓价算不出")
    if ev is None:
        warns.append("盘口单边缺失，算不出真实可平仓价——此时任何浮盈都不可信")
    # App 口径与可平仓口径的差距：这是最容易让人误判的一项
    gap = (lv - ev) if (lv is not None and ev is not None) else None
    # ⚠️ 这里必须是 `ev is not None` 而不是 `ev`：可平仓价【恰好归零】是最需要告警的时刻，
    # 用真值判断会把 0.0 当成缺失，在最该喊的时候闭嘴。（codex review 2026-08-26）
    if gap is not None and ev is not None and abs(gap) / max(abs(ev), 1e-9) * 100 >= gap_warn_pct:
        warns.append(f"App(last)口径比真实可平仓高 ${gap:,.0f}"
                     f"（{abs(gap)/max(abs(ev),1e-9)*100:.0f}%）——止损判定别看 App")
    to_stop = None
    if ev is not None and stop is not None:
        to_stop = (ev - stop) / max(abs(ev), 1e-9) * 100
        if ev <= stop:
            warns.append(f"⚠️ 已触及止损线：可平仓 ${ev:,.0f} ≤ 止损 ${stop:,.0f}")
        elif to_stop < 15:
            warns.append(f"接近止损线：还剩 {to_stop:.0f}%")
    if ev is not None and target is not None and ev >= target:
        warns.append(f"✅ 已达止盈线：可平仓 ${ev:,.0f} ≥ 目标 ${target:,.0f}")
    return PositionCheck(
        ok=True, name=name, cost=cost, exit_value=ev, mid_value=mv, last_value=lv,
        pnl_exit=(ev - cost) if (ev is not None and cost is not None) else None,
        pnl_last=(lv - cost) if (lv is not None and cost is not None) else None,
        gap=gap, stop=stop, target=target, to_stop_pct=to_stop, warnings=warns,
        roots=tuple(sorted({l.root for l in legs})),
        size_unverified=any(l.exit_size() is None for l in legs),
        time_unknown=any(l.quote_time is None for l in legs))


def market_session(now=None, roots=()) -> tuple[str, str]:
    """美股【期权】市场时段。返回 (状态, 提示语)；状态 ∈ 盘中 / 休市 / 部分收市 / 未知。

    期权没有夜盘。收盘后取到的 bid/ask 是昨夜残留挂单，**不是能成交的价**：点差异常放大，
    「真实可平仓价」系统性偏低，据以判止损会误触发（2026-08-27 ET03:27 实测 TQQQ 差 $17）。

    Codex 008 G04：旧实现只看「周一到周五 09:30–16:00」，2026-11-27 14:00（提前收市后）与
    2026-12-25 12:00（圣诞休市）都被判为盘中，报告随即写「止损判定用本表」。现在：
    - 交易日与核心收市来自预存日历（core.market_calendar），覆盖外 → 未知，不猜；
    - 各根代码的期权收市来自 core.option_products（16:15 类 / 16:00 类；提前收市日 13:15 / 13:00），
      未认证的根代码按标的核心收市（较早，保守）；
    - 带时区的 now 一律先转 ET；不带时区的 now 按 ET 解释（约定）。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from undertow.core import market_calendar as mc
    from undertow.core import option_products as op
    ET = ZoneInfo("America/New_York")
    if now is None:
        et = datetime.now(ET)
    elif now.tzinfo is None:
        et = now.replace(tzinfo=ET)
    else:
        et = now.astimezone(ET)
    d = et.date()
    td = mc.is_trading_day(d)
    if td is None:
        return "未知", f"⚠️ ET {d} 不在预存交易日历覆盖内（{mc.VERSION}）：无法判定是否开市"
    if not td:
        why = "周末休市" if d.weekday() >= 5 else "交易所休市日"
        return "休市", f"⚠️ 现在是 ET {et:%a %H:%M}，**{why}**"
    core = mc.close_time(d); early = core != mc.REGULAR_CLOSE
    core_m = int(core[:2]) * 60 + int(core[3:])
    hm = et.hour * 60 + et.minute
    if hm < 570:
        return "休市", f"⚠️ 现在是 ET {et:%H:%M}，**期权盘未开**（09:30 ET 开盘）"
    closes = {}
    for r in (roots or ("*",)):
        m = op.option_close_minutes(r, early) if r != "*" else None
        closes[r] = m if m is not None else core_m
    open_r = [r for r, m in closes.items() if hm < m]
    tag = f"（{'提前收市日 ' if early else ''}核心收市 {core} ET）"
    if len(open_r) == len(closes):
        return "盘中", ("" if not early else f"注意：今天是提前收市日{tag}")
    if not open_r:
        return "休市", f"⚠️ 现在是 ET {et:%H:%M}，**期权已收市**{tag}"
    closed = [r for r in closes if r not in open_r]
    return "部分收市", (f"⚠️ 现在是 ET {et:%H:%M}：{'、'.join(closed)} 期权已收市，"
                        f"{'、'.join(open_r)} 仍在交易（16:15 类）{tag}")


def render_md(checks: list, net_assets: float | None = None, now=None) -> str:
    roots = tuple(sorted({r for c in checks if c.ok for r in c.roots}))
    sess, warn = market_session(now, roots)
    title = "持仓实时体检" + {"盘中": "（长桥实时盘口 · 按【真实可平仓价】计）",
                            "休市": "（⚠️ 休市时段 · 报价不可成交）",
                            "部分收市": "（⚠️ 部分品种期权已收市 · 这些腿报价不可成交）",
                            "未知": "（⚠️ 交易时段未知 · 不作止损依据）"}[sess]
    L = [f"# {title}", ""]
    if warn and sess == "盘中":
        L.append(f"> {warn}")
        L.append("")
    elif warn:
        L.append(f"> {warn}。表中 bid/ask 可能是**收市后残留挂单**，点差被放大，"
                 f"「真实可平仓价」会系统性偏低。")
        L.append("> **此时不得据本表判止损**——要判止损请在开盘后重跑。"
                 "参考时可看「中价」，它受宽点差影响较小。")
        L.append("")
    L.append("> 多头腿按 bid 卖、空头腿按 ask 买回——这才是「现在就走能拿回多少」。")
    sized = all(not c.size_unverified for c in checks if c.ok)
    L.append("> 券商 App 的持仓盈亏用 last 价，对流动性差的腿会系统性高估。"
             + ("**止损判定用本表。**" if (sess == "盘中" and sized) else ""))
    if any(c.time_unknown for c in checks if c.ok):
        L.append("> 报价时间未知（源不提供），无法判断新旧；数量为报价挂单量，不保证指定张数都能按此价成交。")
    if not sized:
        L.append("> ⚠️ 部分腿出场一侧挂单量未知：「真实可平仓」只是**报价估值**，不作止损依据。")
    L.append("")
    L.append("| 持仓 | 成本 | 真实可平仓 | 中价 | App(last) | 盈亏(可平仓) | 盈亏(App) | 距止损 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    tot = 0.0
    missing = []          # 算不出可平仓价的持仓——净清算价值必须显式标为不完整
    for c in checks:
        if not c.ok:
            continue
        if c.exit_value is None:
            missing.append(c.name)
        f = lambda v: f"${v:,.0f}" if v is not None else "—"
        pl = f"{c.pnl_exit:+,.0f}" if c.pnl_exit is not None else "—"
        pa = f"{c.pnl_last:+,.0f}" if c.pnl_last is not None else "—"
        ts = f"{c.to_stop_pct:.0f}%" if c.to_stop_pct is not None else "—"
        L.append(f"| {c.name} | {f(c.cost)} | **{f(c.exit_value)}** | {f(c.mid_value)} | "
                 f"{f(c.last_value)} | {pl} | {pa} | {ts} |")
        if c.exit_value is not None:       # 恰好为 0 也要计入，不能用真值判断
            tot += c.exit_value
    L.append("")
    # Codex 008 G03：这里加总的是【有符号的可平仓价值】= 现在全平能收回(+)/需付出(−)的现金，
    # 旧文案称「总敞口」：卖方结构平仓要付钱，于是出现「敞口 −5%」。它不是风险、不是最大亏损、不是资金占用。
    # 缺价成员可能是负值（空头要买回），所以已算部分既不是上界也不是下界。
    pct = (lambda v: f"（净资产 {v/net_assets*100:+.1f}%）" if (net_assets is not None and net_assets > 0) else "")
    if missing:
        L.append(f"**⚠️ 净清算价值不完整：已算部分 ${tot:+,.0f}{pct(tot)}，"
                 f"{len(missing)} 笔拿不到盘口、未计入** —— {'、'.join(missing[:2])}")
        L.append("> 缺的那笔若是空头，全平还要再付钱：**已算部分既不是上界也不是下界**。")
    elif any(c.ok for c in checks):
        L.append(f"**净清算价值（现在全平，有符号）：${tot:+,.0f}{pct(tot)}**")
    L.append("> 这是「现在全平收回/付出多少现金」，**不是风险敞口、不是最大亏损、不是资金占用**；"
             "限额请看体检里的最大亏损与止损情景。")
    L.append("")
    for c in checks:
        for w in c.warnings:
            L.append(f"- {c.name}：{w}")
    return "\n".join(L)


# ── 品种累计台账 ────────────────────────────────────────────────────
# 为什么单列：券商显示的「成本价」在部分减仓后会被改写——它把已实现盈亏摊进剩余
# 持仓，得到的是【该轮的整体打平价】，不是你实际付出的价格。
# 2026-08-26 实测：SLV 70C 实际每张付 1.05，券商显示 1.89 = (3×1.05 − 2×0.63)/1。
# 拿 1.89 去判断「亏了多少」会同时错两次：既不是本仓成本，也不含更早那轮的盈利。
#
# 唯一不会骗人的是**现金流水**：进出账是事实，与任何摊销口径无关。
# 三个数各回答一个问题，不可互相替代：
#     已实现   —— 已经落袋/已经亏掉的，不可再变（沉没，决策时无视）
#     可平仓   —— 现在就走能拿回多少（**唯一影响当下决策的数**）
#     累计     —— 这个品种从头到尾赚没赚（复盘用）


@dataclass(frozen=True)
class Ledger:
    """⚠️ `net_cash_flow` **不是「已实现盈亏」**（codex review 2026-08-26 指出的口径错误）。

    它是这些合约至今全部进出账的净额，其中**包含仍未平仓头寸的建仓支出**。
    钱确实已经离开账户、不会再变，但那部分对应的损益尚未实现——把它叫「已实现盈亏」
    会让人以为剩余持仓的成本已经结清。真正的已实现盈亏需按成交批次配对已平数量计算，
    本模块不做（数据源没有批次匹配信息）。
    """
    underlying: str
    net_cash_flow: float     # 至今全部进出账净额（含手续费）。负=净投入
    closeable: float | None  # 当前持仓真实可平仓价值；盘口缺失时为 None（不许用 0 代替）
    exit_fee: float = 0.0    # 平掉剩余持仓还要付的手续费

    @property
    def total(self) -> float | None:
        """若现在全平，这些合约从头到尾的【生命周期损益】。

        可平仓价算不出时返回 None —— 缺价就是缺价，用 0 会把「行情拿不到」
        显示成「持仓已归零」，进而算出一个假的最终亏损。
        """
        if self.closeable is None:
            return None
        return self.net_cash_flow + self.closeable - self.exit_fee


def build_ledger(underlying: str, cash_rows: list, closeable: float | None,
                 exit_fee: float = 0.0) -> Ledger:
    """从现金流水汇总这些合约的净进出账。cash_rows 需已按目标合约过滤。

    closeable 传 None 表示盘口缺失、算不出——务必原样传入，不要先折成 0。
    """
    flow = 0.0
    for r in cash_rows:
        try:
            flow += float(r.get("balance", 0) or 0)
        except (TypeError, ValueError):
            continue
    return Ledger(underlying=underlying, net_cash_flow=flow,
                  closeable=closeable, exit_fee=exit_fee)


def render_ledger_md(ledgers: list) -> str:
    if not ledgers:
        return ""
    L = ["", "### 生命周期台账（按真实现金流水，与券商成本价无关）", "",
         "| 合约组 | 至今净现金流 | 当前可平仓 | 平仓费 | **若现在全平的生命周期损益** |",
         "|---|---:|---:|---:|---:|"]
    for g in ledgers:
        cl = f"{g.closeable:+,.2f}" if g.closeable is not None else "**盘口缺失·不可计算**"
        tot = f"**{g.total:+,.2f}**" if g.total is not None else "**不可计算**"
        L.append(f"| {g.underlying} | {g.net_cash_flow:+,.2f} | {cl} | "
                 f"{-g.exit_fee:,.2f} | {tot} |")
    L.append("")
    L.append("> 「至今净现金流」= 这些合约全部进出账净额（含手续费）。钱已离开账户、不会再变，"
             "**但它不等于「已实现盈亏」**——其中含仍未平仓头寸的建仓支出，那部分损益尚未实现。")
    L.append("> 决策只看「当前可平仓」；净现金流属沉没，评价这笔交易好坏时才用生命周期损益。")
    L.append("> 券商的「成本价」在部分减仓后会被改写成该轮打平价，既非实付价、也不含更早轮次，"
             "**不可用来判断亏了多少**。")
    return "\n".join(L)
