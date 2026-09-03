"""趋势类指标的**描述性**回溯 —— 强制真实成交假设。

存在的理由
----------
2026-09-03：用户给了 KivancOzbilgic 的 "SuperTrend STRATEGY"（indicator 的
strategy 包装版）。对比时发现我前一轮的实测用【信号根收盘价】当成交价，
而 Pine 的 `strategy.entry` 实际是**次根开盘**成交。重算后 6 个组合里 5 个变差：

    SLV 1d Supertrend   +83.7 → +69.0   （−14.6pp）
    GLD 1h Supertrend    +4.8 →  +0.3   （ −4.5pp）

「跑赢买入持有 3.9 个点」的结论直接翻成「跑输 10.6 个点」。

所以本模块把三条假设**焊死**，不留可选项：

  ① 成交价 = 信号确认根的**下一根开盘**（不是信号根收盘）
  ② 每次翻转计一次往返成本（默认 0.10%，ETF 佣金+点差的保守估计）
  ③ always-in-market 多空反转 —— 与 `strategy.entry` 的行为一致
     （反向信号自动平仓反手，没有空仓状态）
  ④ **权益递推（复利），不是把各段百分比相加**
  ⑤ 买入持有基准与策略**同期**：从策略首次可成交时点起算

④⑤ 是 2026-09-03 codex review 指出的（P1-2 / P1-3），都会直接改变结论：

    ④ +100% 后 −50%，相加报 +50%，实际权益是 0%。误差可正可负。
    ⑤ 策略在首次 flip 的次根开盘才有敞口，基准却从 closes[0] 起算。
       若信号前标的下跌，策略靠空仓躲过，会被记成"跑赢买入持有"。

还有一处段落配对的 bug（P1-4）：末根产生的 flip 无法成交时，
原实现连带把仍存续的上一段一起丢掉 —— "最新一根刚翻转"是最常见的场景，
整个当前持仓段会凭空消失。现在先过滤出可成交的 flip 再两两配对。

⚠️ TradingView 的 strategy 测试器**默认 0 手续费 0 滑点**
（脚本的 `strategy()` 没写 commission_type / slippage）。
在那上面看到的净利润曲线，是本模块假设 ①②③ 全部关掉的样子。

⛔ 这不是策略回测，是描述性统计。样本普遍只有个位数到几十段，
   换个成交假设就能翻盘 —— 这件事本身就是结论。任何"某某指标有效"
   的说法都得先过 analyze/validation.py 的检验。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 每次翻转的往返成本（百分点）。ETF 佣金 + 点差的保守估计。
DEFAULT_COST_PCT = 0.10


@dataclass(frozen=True)
class Segment:
    """一段持仓。"""
    entry_idx: int
    exit_idx: int
    direction: int          # 1=多 −1=空
    entry_px: float
    exit_px: float
    ret_pct: float          # 已按方向计正负，未扣成本
    is_open: bool = False   # 末段尚未平仓，exit_px 是最新收盘的市值标记

    @property
    def bars(self) -> int:
        return self.exit_idx - self.entry_idx


@dataclass(frozen=True)
class Result:
    segments: list[Segment]
    cost_pct: float
    bh_pct: float           # 与策略**同期**的买入持有

    @property
    def n(self) -> int:
        return len(self.segments)

    @property
    def gross_pct(self) -> float:
        """毛收益，**按权益递推**（复利），不是各段相加。"""
        eq = 1.0
        for s in self.segments:
            eq *= 1 + s.ret_pct / 100
        return (eq - 1) * 100

    @property
    def total_cost_pct(self) -> float:
        """成本对权益的实际拖累，同样是递推不是相加。"""
        if not self.segments:
            return 0.0
        drag = (1 - self.cost_pct / 100) ** self.n
        return (1 - drag) * 100

    @property
    def net_pct(self) -> float:
        eq = 1.0
        for s in self.segments:
            eq *= (1 + s.ret_pct / 100) * (1 - self.cost_pct / 100)
        return (eq - 1) * 100

    @property
    def buy_hold_pct(self) -> float:
        return self.bh_pct

    @property
    def vs_buy_hold(self) -> float:
        return self.net_pct - self.bh_pct

    @property
    def win_rate(self) -> float:
        """⚠️ 趋势跟踪天生低胜率靠厚尾，**用胜率评价这类指标是错的**。
        这里给出只为完整，判断请看 net_pct 与 profit_factor。"""
        if not self.segments:
            return 0.0
        return sum(1 for s in self.segments if s.ret_pct > 0) / self.n * 100

    @property
    def profit_factor(self) -> float:
        w = sum(s.ret_pct for s in self.segments if s.ret_pct > 0)
        ls = sum(s.ret_pct for s in self.segments if s.ret_pct <= 0)
        return (w / abs(ls)) if ls else float("inf")


def run(opens: list[float], closes: list[float],
        flips: list[tuple[int, int]], *,
        cost_pct: float = DEFAULT_COST_PCT) -> Result:
    """按【次根开盘成交】回溯一串翻转信号。

    flips 是 [(信号确认根的下标, 新方向)]，由 supertrend.flips / ut_bot.flips 给出。

    先过滤出**能成交**的 flip（次根开盘存在），再两两配对成段；
    最后一段持有到序列末尾，用最新收盘做市值标记（is_open=True）。
    进场价一律次根开盘，绝不退化成信号根收盘 —— 那正是要防的事。
    """
    if not closes or not opens:
        return Result([], cost_pct, 0.0)
    # 只保留能真正进场的信号；不可成交的 flip 不得连累上一段（codex P1-4）
    tradable = [(i, d) for i, d in flips if 0 <= i + 1 < len(opens) and opens[i + 1] > 0]
    segs: list[Segment] = []
    for k, (i, d) in enumerate(tradable):
        ep = opens[i + 1]
        if k + 1 < len(tradable):
            j = tradable[k + 1][0]
            if j <= i or j + 1 >= len(opens):
                continue
            xp, is_open = opens[j + 1], False
        else:
            j, xp, is_open = len(closes) - 1, closes[-1], True
        if j <= i:
            continue
        segs.append(Segment(i, j, d, ep, xp,
                            (xp / ep - 1) * 100 * (1 if d == 1 else -1), is_open))
    # 基准与策略同期：从首次可成交时点起算（codex P1-3）
    if segs:
        bh = (closes[-1] / segs[0].entry_px - 1) * 100
    else:
        bh = 0.0
    return Result(segs, cost_pct, bh)
