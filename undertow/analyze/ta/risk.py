"""分阶段 ATR 止损 + 风险仓位 —— 取自 "EMA Trend + MTF Stochastic Strategy"。

这是那个脚本里**唯一值得单独拿出来的东西**：与具体指标无关的通用风控结构。
入场信号可以换（Supertrend、UT Bot、我们自己的墙位），这套出场逻辑照用。

三段式止损
----------
    ① 初始       止损 = 进场价 ∓ 1.5×ATR
    ② 浮盈达 1R   止损上移到**进场价**（保本），不再亏
    ③ 浮盈达 1.5R 开始 1.5×ATR 追踪，只收紧不放松

比固定止损和纯追踪都好的地方：固定止损锁不住利润，纯追踪在刚进场的噪声里
就被扫出去。阶梯结构让头寸先有呼吸空间，赚到一定程度才开始收紧。

风险仓位
--------
    手数 = (权益 × 风险%) ÷ 止损距离

每笔亏损固定为权益的 1%，手数由止损距离反推 —— 止损远则手数少。
这比"固定手数"或"固定比例仓位"都正确：后两者在波动大时风险敞口自动放大。

⚠️ 原脚本的一处实质 bug（本模块已修）
--------------------------------------
    entryPrice := close
    strategy.entry("Long", strategy.long, qty=qty)

`entryPrice` 记的是**信号根收盘**，但 `strategy.entry` 在**次根开盘**成交。
于是后面

    currentR = (close - entryPrice) / entryStopDist

的基准是个从未成交过的价格。次根跳空高开时，实际成本更高，而 R 用更低的
entryPrice 算 → **过早判定已盈利 1R** → 把止损"移到保本"，而那个位置
对实际成本来说是亏损的。跳空越大错得越多。

本模块的 `Position` 强制传入**实际成交价**，不接受信号价。

⛔ 备用层：未经检验，不进研报、不参与方向投票。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

ATR_STOP_MULT = 1.5
BREAKEVEN_R = 1.0
TRAIL_START_R = 1.5
TRAIL_ATR_MULT = 1.5
MAX_BARS = 60
RISK_PCT = 1.0

INITIAL, BREAKEVEN, TRAILING = "初始止损", "已保本", "追踪中"


@dataclass(frozen=True)
class Position:
    """一个持仓的止损状态机。

    entry_px 必须是**实际成交价**（次根开盘），不是信号根收盘 —— 见文件头。
    """
    direction: int          # 1=多 −1=空
    entry_px: float
    stop_dist: float        # 初始止损距离 = ATR × 倍数
    stop: float
    entry_bar: int
    stage: str = INITIAL

    def r_multiple(self, px: float) -> float:
        """当前浮盈是多少个 R。"""
        if self.stop_dist <= 0:
            return 0.0
        return (px - self.entry_px) * self.direction / self.stop_dist

    def update(self, px: float, atr_val: float, *,
               breakeven_r: float = BREAKEVEN_R,
               trail_start_r: float = TRAIL_START_R,
               trail_mult: float = TRAIL_ATR_MULT) -> "Position":
        """按当前价推进止损。止损**只朝有利方向移动**，绝不放松。"""
        r = self.r_multiple(px)
        stop, stage = self.stop, self.stage
        if r >= trail_start_r:
            cand = px - self.direction * atr_val * trail_mult
            stop = max(stop, cand) if self.direction == 1 else min(stop, cand)
            stage = TRAILING
        elif r >= breakeven_r:
            stop = (max(stop, self.entry_px) if self.direction == 1
                    else min(stop, self.entry_px))
            stage = BREAKEVEN
        return replace(self, stop=stop, stage=stage)

    def hit(self, low: float, high: float) -> bool:
        """本根是否触及止损。用最高/最低价判定，不是收盘价 ——
        收盘价判定会漏掉盘中被扫的情况，回测显著虚高。"""
        return low <= self.stop if self.direction == 1 else high >= self.stop

    def timed_out(self, bar: int, max_bars: int = MAX_BARS) -> bool:
        return max_bars > 0 and (bar - self.entry_bar) >= max_bars


def open_position(direction: int, entry_px: float, atr_val: float, bar: int, *,
                  stop_mult: float = ATR_STOP_MULT) -> Position:
    dist = atr_val * stop_mult
    return Position(direction, entry_px, dist, entry_px - direction * dist, bar)


def size(equity: float, stop_dist: float, *, risk_pct: float = RISK_PCT) -> float:
    """风险仓位：每笔亏损固定为权益的 risk_pct%。止损距离为零时返回 0。"""
    if stop_dist <= 0 or equity <= 0:
        return 0.0
    return equity * (risk_pct / 100) / stop_dist
