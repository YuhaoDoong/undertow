"""UT Bot Alerts —— TradingView 同名脚本（QuantNomad 版）的移植。

用户 2026-09-03 提供源码，并问它与 Supertrend 有什么区别。答案见
docs/ta_modules.md 的对比表；一句话：**同一族，UT Bot 是单轨高敏版**。

结构：**一条追踪止损线 + 状态机**

    nLoss = Key × ATR(10)          Key 默认 **1.0**（Supertrend 是 3.0）

    stop := src > stop[1] and src[1] > stop[1] ? max(stop[1], src − nLoss)   多头追踪
          : src < stop[1] and src[1] < stop[1] ? min(stop[1], src + nLoss)   空头追踪
          : src > stop[1]                      ? src − nLoss                  刚翻多
          :                                      src + nLoss                  刚翻空

Supertrend 维护上下两条轨、由 trend 决定显示哪条；UT Bot 只有一条线，
它自己在价格两侧翻来翻去。前两个分支是「顺势时棘轮收紧」，后两个是「翻转时重置」。

源码里两处值得注意
------------------
1. `ema = ema(src, 1)` —— **周期 1 的 EMA 就是 src 本身**（alpha=2/(1+1)=1）。
   作者绕这一圈只是为了能用 `crossover()` 函数。
2. `buy = src > xATRTrailingStop and above`，而 `above = crossover(ema, stop)`
   已经蕴含 `src > stop`。**前半个条件是冗余的**，buy ≡ above。
   移植时照实现即可，但不必当成两个条件去理解。

还有一处**真实的不一致**：`pos` 的判定两边都用 `stop[1]`，而 `above/below`
用的是 `crossover(ema, stop)` 即当根的 stop。当根 stop 已经更新过了。
实测两者结论一致（因为 stop 更新后必然落在 src 的另一侧），但读源码时容易看岔。

Heikin Ashi 选项默认关闭（`h = input(false)`），本模块同样默认用普通 close。

⛔ 备用层：不进研报、不进方向投票。
"""
from __future__ import annotations

from dataclasses import dataclass

from undertow.analyze.ta import atr

PERIOD, KEY = 10, 1.0


@dataclass(frozen=True)
class UtBotReading:
    pos: int                # 1=多 −1=空 0=未定
    stop: float             # 追踪止损线
    flipped: bool
    dist_pct: float

    @property
    def label(self) -> str:
        return {1: "多", -1: "空"}.get(self.pos, "未定")


def heikin_ashi(opens, highs, lows, closes):
    """平均K线。UT Bot 的可选源，默认不用。"""
    ho, hc = [], []
    for i in range(len(closes)):
        c = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
        o = (opens[i] + closes[i]) / 2 if i == 0 else (ho[-1] + hc[-1]) / 2
        ho.append(o); hc.append(c)
    return hc


def ut_bot(highs: list[float], lows: list[float], closes: list[float], *,
           period: int = PERIOD, key: float = KEY,
           src: list[float] | None = None
           ) -> tuple[list[float | None], list[int | None]]:
    """返回 (stop, pos) 两条等长序列。src 默认用 closes。"""
    a = atr(highs, lows, closes, period)
    s = src if src is not None else closes
    stop: list[float | None] = []
    pos: list[int | None] = []
    ps: float | None = None
    p = 0
    for i in range(len(s)):
        if a[i] is None:
            stop.append(None); pos.append(None)
            continue
        nloss = key * a[i]
        s1 = ps if ps is not None else 0.0        # Pine: nz(stop[1], 0)
        sp = s[i - 1] if i > 0 else s[i]
        if s[i] > s1 and sp > s1:
            cur = max(s1, s[i] - nloss)           # 多头棘轮收紧
        elif s[i] < s1 and sp < s1:
            cur = min(s1, s[i] + nloss)           # 空头棘轮收紧
        elif s[i] > s1:
            cur = s[i] - nloss                    # 刚翻多，重置
        else:
            cur = s[i] + nloss                    # 刚翻空，重置
        if i > 0:
            if sp < s1 and s[i] > s1:
                p = 1
            elif sp > s1 and s[i] < s1:
                p = -1
        stop.append(cur); pos.append(p); ps = cur
    return stop, pos


def read(highs, lows, closes, **kw) -> UtBotReading | None:
    st, ps = ut_bot(highs, lows, closes, **kw)
    if not ps or ps[-1] is None or st[-1] is None:
        return None
    prev = ps[-2] if len(ps) > 1 else None
    px = closes[-1]
    return UtBotReading(
        pos=ps[-1], stop=st[-1],
        flipped=(prev is not None and prev != ps[-1] and prev != 0),
        dist_pct=(px - st[-1]) / px * 100)


def flips(highs, lows, closes, **kw) -> list[tuple[int, int]]:
    _, ps = ut_bot(highs, lows, closes, **kw)
    out = []
    for i in range(1, len(ps)):
        if ps[i] is not None and ps[i - 1] is not None and ps[i] != ps[i - 1] and ps[i - 1] != 0:
            out.append((i, ps[i]))
    return out
