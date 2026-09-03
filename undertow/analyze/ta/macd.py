"""MACD —— CM_Ult_MacD_MTF 口径（ChrisMoody，2014-04-10）。

用户 2026-09-03 提供了该脚本源码。移植时有**一处必须注意的口径差异**：

    ⚠️ signal = sma(macd, 9)   ← 这个脚本用的是 **SMA**

TradingView 内置的 MACD、以及绝大多数教科书写法，signal 线用的是
**EMA(macd, 9)**。CM 这个脚本用 SMA，两者的柱状图在拐点附近能差出一整根的
方向。既然用户是照着这个脚本在 TradingView 上看盘，本模块**默认跟它一致**
（signal_ma="sma"），同时保留 "ema" 选项供对照。口径不一致就是对不上盘 ——
2026-08-27 技术面滞后两天那次，就是因为我们和用户 App 不同源。

四色柱（脚本的核心价值）
------------------------
脚本把柱状图按【零轴上下】×【比上一根变强还是变弱】分成四色：

    aqua    hist > 0 且 hist > hist[1]   零上走强 → 多头强化
    blue    hist > 0 且 hist < hist[1]   零上走弱 → 多头衰减（顶部预警）
    red     hist ≤ 0 且 hist < hist[1]   零下走弱 → 空头强化
    maroon  hist ≤ 0 且 hist > hist[1]   零下走强 → 空头衰减（底部修复）

意义在于：**颜色翻转早于零轴穿越，也早于金叉死叉**。blue 出现时价格常还在涨，
但动能已经在减速。这是它比裸看金叉多出来的那点信息。

⛔ 现状：本模块**不进研报、不进方向投票**。未经统计检验的指标一律只备着。
"""
from __future__ import annotations

from dataclasses import dataclass

from undertow.analyze.ta import ema, sma

FAST, SLOW, SIGNAL = 12, 26, 9

# 四色柱的状态名。键是 (零上?, 变强?)
BULL_STRONG = "多头强化"    # aqua
BULL_FADE = "多头衰减"      # blue
BEAR_STRONG = "空头强化"    # red
BEAR_FADE = "空头衰减"      # maroon


@dataclass(frozen=True)
class MacdReading:
    """某一根 K 线上的 MACD 读数。"""
    macd: float
    signal: float
    hist: float
    state: str              # 四色柱状态
    above_signal: bool      # MACD 线在信号线上方（脚本里 macd_IsAbove）
    cross: str | None       # "金叉" | "死叉" | None，仅当根发生

    @property
    def above_zero(self) -> bool:
        return self.macd > 0


def macd_series(closes: list[float], *, fast: int = FAST, slow: int = SLOW,
                signal: int = SIGNAL, signal_ma: str = "sma"
                ) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """返回 (macd, signal, hist) 三条等长序列，前段不足窗口处为 None。

    signal_ma: "sma" 跟 CM 脚本（默认），"ema" 跟 TradingView 内置 MACD。
    """
    if signal_ma not in ("sma", "ema"):
        raise ValueError(f'signal_ma 须为 "sma" 或 "ema"，收到 {signal_ma!r}')
    f, s = ema(closes, fast), ema(closes, slow)
    m: list[float | None] = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(f, s)]

    # signal 只在 macd 的非 None 段上计算，再按原位置贴回去
    idx = [i for i, v in enumerate(m) if v is not None]
    fn = sma if signal_ma == "sma" else ema
    sig_compact = fn([m[i] for i in idx], signal) if idx else []
    sig: list[float | None] = [None] * len(m)
    for k, i in enumerate(idx):
        if k < len(sig_compact):
            sig[i] = sig_compact[k]

    hist: list[float | None] = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(m, sig)]
    return m, sig, hist


def hist_state(hist: float, prev: float | None) -> str:
    """四色柱状态。prev 为 None（首根）时按"变强"处理，与脚本 na 行为一致。"""
    up = prev is None or hist > prev
    if hist > 0:
        return BULL_STRONG if up else BULL_FADE
    return BEAR_FADE if up else BEAR_STRONG


def read(closes: list[float], **kw) -> MacdReading | None:
    """最后一根 K 线的读数。数据不足返回 None。"""
    m, s, h = macd_series(closes, **kw)
    if not h or h[-1] is None or m[-1] is None or s[-1] is None:
        return None
    prev = h[-2] if len(h) > 1 else None
    cross = None
    if len(m) > 1 and m[-2] is not None and s[-2] is not None:
        # Pine ta.crossover(a,b) 要求**当根严格 a>b**、前根 a<=b。
        # 用 >= 判定当根会把「刚好相等」也报成金叉（codex P2-5）。
        if m[-1] > s[-1] and m[-2] <= s[-2]:
            cross = "金叉"
        elif m[-1] < s[-1] and m[-2] >= s[-2]:
            cross = "死叉"
    return MacdReading(macd=m[-1], signal=s[-1], hist=h[-1],
                       state=hist_state(h[-1], prev),
                       above_signal=m[-1] >= s[-1], cross=cross)


def read_multi(symbol: str, tfs: tuple[str, ...] = ("1h", "4h", "1d"),
               **kw) -> dict[str, MacdReading]:
    """多周期读数。

    ⚠️ 默认**不含 15m** —— 用户 2026-09-03 明确 15m 只用于入场出场时机，
    不用于判断方向。要看 15m 请显式传入，并且只当择时用。
    """
    from undertow.collect.longbridge_kline import KlineUnavailable
    from undertow.analyze.ta.frames import bars

    out = {}
    for tf in tfs:
        try:
            c = [b["close"] for b in bars(symbol, tf)]
        except (KlineUnavailable, ValueError):
            continue        # 取不到数据是正常情况；程序错误必须往外抛
        r = read(c, **kw)
        if r is not None:
            out[tf] = r
    return out
