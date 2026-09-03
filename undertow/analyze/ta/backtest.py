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
    buy_hold_pct: float

    @property
    def n(self) -> int:
        return len(self.segments)

    @property
    def gross_pct(self) -> float:
        return sum(s.ret_pct for s in self.segments)

    @property
    def total_cost_pct(self) -> float:
        return self.n * self.cost_pct

    @property
    def net_pct(self) -> float:
        return self.gross_pct - self.total_cost_pct

    @property
    def vs_buy_hold(self) -> float:
        return self.net_pct - self.buy_hold_pct

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
    **进场价一律是次根开盘**，拿不到就丢弃这一段，不退化成收盘价 —— 那正是要防的事。

    末段尚未平仓（没有下一个反向信号），它的 exit_px 用**最新收盘**做市值标记，
    并置 is_open=True。这不是破例：进场价是真实成交、必须用开盘，
    而未平仓头寸的估值本来就该用最新价。
    """
    segs: list[Segment] = []
    last = len(flips) - 1
    for k, (i, d) in enumerate(flips):
        if i + 1 >= len(opens):
            continue
        ep = opens[i + 1]
        if ep <= 0:
            continue
        if k == last:                                  # 未平仓，市值标记
            j, xp, is_open = len(closes) - 1, closes[-1], True
        else:
            j = flips[k + 1][0]
            if j + 1 >= len(opens) or j <= i:
                continue
            j, xp, is_open = j, opens[j + 1], False
        segs.append(Segment(i, j, d, ep, xp,
                            (xp / ep - 1) * 100 * (1 if d == 1 else -1), is_open))
    bh = (closes[-1] / closes[0] - 1) * 100 if closes and closes[0] else 0.0
    return Result(segs, cost_pct, bh)
