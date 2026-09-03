"""Supertrend —— TradingView 同名脚本（KivancOzbilgic 版）的移植。

用户 2026-09-03 提供源码。默认参数 ATR(10)、倍数 3.0、源 hl2。

结构：**两条棘轮轨 + 状态机**

    up = hl2 − 3×ATR      下轨（多头时显示，充当移动止损）
    dn = hl2 + 3×ATR      上轨（空头时显示）

「棘轮」指两条轨各自只许朝一个方向走：

    up := close[1] > up1 ? max(up, up1) : up      上一根收在下轨上方 → 下轨只抬不落
    dn := close[1] < dn1 ? min(dn, dn1) : dn      上一根收在上轨下方 → 上轨只压不升

翻转判定用的是**前一根**的轨，不是当根：

    trend := trend == -1 and close > dn1 ? 1 :
             trend ==  1 and close < up1 ? -1 : trend

这个 `dn1`/`up1` 的下标很容易在移植时写错成当根值。写错了信号会提前一根 ——
在 4h 上就是提前 4 小时，回测会凭空多出一截收益。

⛔ 备用层：不进研报、不进方向投票。
"""
from __future__ import annotations

from dataclasses import dataclass

from undertow.analyze.ta import atr

PERIOD, MULT = 10, 3.0


@dataclass(frozen=True)
class SupertrendReading:
    trend: int              # 1=多 −1=空
    line: float             # 当前生效的那条轨（多头看下轨，空头看上轨）
    up: float               # 下轨
    dn: float               # 上轨
    flipped: bool           # 当根刚翻转
    dist_pct: float         # 价格距生效轨的百分比距离（止损空间）

    @property
    def label(self) -> str:
        return "多" if self.trend == 1 else "空"


def supertrend(highs: list[float], lows: list[float], closes: list[float], *,
               period: int = PERIOD, mult: float = MULT,
               atr_method: str = "rma"
               ) -> tuple[list[float | None], list[float | None], list[int | None]]:
    """返回 (up, dn, trend) 三条等长序列。

    atr_method: "rma" 对应脚本 changeATR=true（默认），"sma" 对应 false。
    """
    a = atr(highs, lows, closes, period, method=atr_method)
    src = [(h + l) / 2 for h, l in zip(highs, lows)]          # hl2
    up: list[float | None] = []
    dn: list[float | None] = []
    tr: list[int | None] = []
    pu = pd = None
    t = 1                                                      # Pine: trend = 1
    for i in range(len(closes)):
        if a[i] is None:
            up.append(None); dn.append(None); tr.append(None)
            continue
        u = src[i] - mult * a[i]
        d = src[i] + mult * a[i]
        u1 = pu if pu is not None else u                       # nz(up[1], up)
        d1 = pd if pd is not None else d
        if i > 0 and closes[i - 1] > u1:
            u = max(u, u1)
        if i > 0 and closes[i - 1] < d1:
            d = min(d, d1)
        # ⚠️ 与【前一根】的轨比较，不是当根 —— 写成当根信号会提前一根
        if t == -1 and closes[i] > d1:
            t = 1
        elif t == 1 and closes[i] < u1:
            t = -1
        up.append(u); dn.append(d); tr.append(t)
        pu, pd = u, d
    return up, dn, tr


def read(highs, lows, closes, **kw) -> SupertrendReading | None:
    up, dn, tr = supertrend(highs, lows, closes, **kw)
    if not tr or tr[-1] is None:
        return None
    t = tr[-1]
    line = up[-1] if t == 1 else dn[-1]
    prev = tr[-2] if len(tr) > 1 else None
    px = closes[-1]
    return SupertrendReading(
        trend=t, line=line, up=up[-1], dn=dn[-1],
        flipped=(prev is not None and prev != t),
        dist_pct=(px - line) / px * 100)


def flips(highs, lows, closes, **kw) -> list[tuple[int, int]]:
    """所有翻转点 [(下标, 新方向)]。用来数信号频率。"""
    _, _, tr = supertrend(highs, lows, closes, **kw)
    out = []
    for i in range(1, len(tr)):
        if tr[i] is not None and tr[i - 1] is not None and tr[i] != tr[i - 1]:
            out.append((i, tr[i]))
    return out
