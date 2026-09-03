"""出场规则 —— 枝形吊灯止损 + ADX 衰减 + 冷却期（取自 DMI/ADX Dashboard v3）。

与 `risk.py` 的三段式止损并列，是另外两种出场思路。

枝形吊灯（Chandelier Exit，Chuck LeBeau）
------------------------------------------
    多头止损 = highest(high, 22) − 3×ATR
    空头止损 = lowest(low, 22) + 3×ATR

关键差别在**锚点**：三段式追踪从**当前价**算，吊灯从**近期最高点**算。
所以吊灯不会因为一根回调就收紧 —— 只有创新高时才上移。
在有噪声的趋势里，这比从当前价追踪宽容得多。

ADX 衰减出场
------------
    peak = 进场后 ADX 的最高值
    衰减% = (peak − 当前 ADX) / peak × 100
    衰减 ≥ 30% → 平仓

思路是新颖的：**与价格无关，只看趋势强度是否见顶**。
价格还在涨但 ADX 从峰值掉了三成，说明推力已经散了。

⚠️ ADX 是 DX 的二次平滑（rma 套 rma），本身滞后很重。
「ADX 见顶」这件事被确认时，往往价格已经走完一段。
这条规则救得了拖泥带水的横盘，救不了急转。

冷却期
------
平仓后 N 根内不再开仓，防止在震荡里反复进出被手续费磨死。

⛔ 备用层：不进研报、不进方向投票。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from undertow.analyze.ta import highest, lowest

CHANDELIER_LEN, CHANDELIER_MULT = 22, 3.0
INITIAL_STOP_MULT = 2.0
ADX_DECAY_PCT = 30.0
COOLDOWN_BARS = 5


@dataclass(frozen=True)
class ChandelierStop:
    direction: int
    stop: float
    entry_px: float

    def update(self, hh: float | None, ll: float | None, atr_val: float, *,
               mult: float = CHANDELIER_MULT) -> "ChandelierStop":
        """只朝有利方向移动。hh/ll 是回看窗口内的最高/最低。"""
        if self.direction == 1:
            if hh is None:
                return self
            return replace(self, stop=max(self.stop, hh - atr_val * mult))
        if ll is None:
            return self
        return replace(self, stop=min(self.stop, ll + atr_val * mult))

    def hit(self, low: float, high: float) -> bool:
        return low <= self.stop if self.direction == 1 else high >= self.stop


def open_chandelier(direction: int, entry_px: float, atr_val: float, *,
                    init_mult: float = INITIAL_STOP_MULT) -> ChandelierStop:
    """初始止损用固定 ATR 倍数，之后交给吊灯接管（脚本 baseStop 的行为）。"""
    return ChandelierStop(direction, entry_px - direction * atr_val * init_mult, entry_px)


def chandelier_levels(highs, lows, *, n: int = CHANDELIER_LEN
                      ) -> tuple[list[float | None], list[float | None]]:
    return highest(highs, n), lowest(lows, n)


@dataclass
class AdxDecay:
    """进场后跟踪 ADX 峰值，衰减超阈值即出场。"""
    peak: float

    def update(self, adx_val: float) -> None:
        self.peak = max(self.peak, adx_val)

    def decay_pct(self, adx_val: float) -> float:
        return (self.peak - adx_val) / self.peak * 100.0 if self.peak > 0 else 0.0

    def should_exit(self, adx_val: float, *, thresh: float = ADX_DECAY_PCT) -> bool:
        return self.decay_pct(adx_val) >= thresh


def cooldown_ok(bar: int, last_exit_bar: int | None, *,
                bars: int = COOLDOWN_BARS) -> bool:
    return last_exit_bar is None or (bar - last_exit_bar) >= bars
