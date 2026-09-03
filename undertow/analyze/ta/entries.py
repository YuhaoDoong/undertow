"""入场触发 —— 回调入场 vs 裸交叉（取自 DMI/ADX Trend Dashboard v3）。

回调入场是这份脚本里最有价值的设计，也正好是我们研报里
「回调买·不追」那句话的程序化版本：

    ① regime 成立（DMI 方向 + ADX 达标 + EMA 过滤）
    ② 价格**跌破**快 EMA(9) → 标记「回调中」
    ③ 价格**重新站上**快 EMA(9) → 才进场

对比裸 DI 交叉（`crossover(+DI, −DI)` 即刻进场）：后者在趋势启动时追高，
前者等第一次回调结束。代价是会错过不回调的单边行情。

⚠️ regime 一旦失效，回调标记必须清掉（脚本的 `if not longRegime: longPullback := false`）。
否则会拿着几十根之前的旧标记，在 regime 重新成立的第一根就误触发。

⛔ 备用层：不进研报、不进方向投票。
"""
from __future__ import annotations

from undertow.analyze.ta import ema

FAST_EMA_LEN = 9


def _cross(a: list[float | None], b: list[float | None], i: int) -> int:
    """+1 = a 上穿 b，−1 = a 下穿 b，0 = 无。"""
    if i < 1 or None in (a[i], b[i], a[i - 1], b[i - 1]):
        return 0
    if a[i] > b[i] and a[i - 1] <= b[i - 1]:
        return 1
    if a[i] < b[i] and a[i - 1] >= b[i - 1]:
        return -1
    return 0


def pullback(closes: list[float], regime: list[int], *,
             fast_len: int = FAST_EMA_LEN, grace: int = 0) -> list[int]:
    """回调入场信号。regime 为每根的 1/−1/0。返回等长的 1/−1/0。

    grace=0 是原脚本行为：regime 一断标记立刻清掉。
    **实测这让入场几乎无法触发** —— 2026-09-03 四品种×两周期：
    跌破 EMA9 共 14~31 次/组合，其中 regime 仍成立的只有 **0~4 次**。

    根因是设计上的内在矛盾：regime 要求趋势强（ADX≥20、DI差>10），
    而回调本身就削弱这两个指标 —— 回调一发生 regime 几乎必然失效。
    「等回调」和「要求回调时趋势仍强」是互斥的两个要求。

    grace>0 允许 regime 在回调期间断开至多 grace 根仍保留标记，
    这是对原脚本的修正，不是原样移植。
    """
    fast = ema(closes, fast_len)
    long_pb = short_pb = False
    long_gap = short_gap = 0
    out = [0] * len(closes)
    for i in range(len(closes)):
        c = _cross(closes, fast, i)
        if regime[i] == 1 and c == -1:
            long_pb, long_gap = True, 0
        elif regime[i] != 1:
            long_gap += 1
            if long_gap > grace:
                long_pb = False
        else:
            long_gap = 0
        if regime[i] == -1 and c == 1:
            short_pb, short_gap = True, 0
        elif regime[i] != -1:
            short_gap += 1
            if short_gap > grace:
                short_pb = False
        else:
            short_gap = 0
        # 触发时只要求方向未反转，不要求 regime 仍满强度
        if long_pb and c == 1 and regime[i] != -1:
            out[i], long_pb = 1, False
        elif short_pb and c == -1 and regime[i] != 1:
            out[i], short_pb = -1, False
    return out


def di_crossover(di_plus: list[float | None], di_minus: list[float | None],
                 regime: list[int]) -> list[int]:
    """裸 DI 交叉入场（脚本的 usePullbackEntry=false 分支）。"""
    out = [0] * len(di_plus)
    for i in range(len(di_plus)):
        c = _cross(di_plus, di_minus, i)
        if c == 1 and regime[i] == 1:
            out[i] = 1
        elif c == -1 and regime[i] == -1:
            out[i] = -1
    return out


def regime_from_dmi(di_plus, di_minus, adx, closes, ema_slow, *,
                    adx_thresh: float = 20.0, spread_min: float = 10.0,
                    use_ema: bool = True) -> list[int]:
    """脚本的 longRegime / shortRegime 合成一个 1/−1/0 序列。"""
    out = [0] * len(closes)
    for i in range(len(closes)):
        if None in (di_plus[i], di_minus[i], adx[i]):
            continue
        if adx[i] < adx_thresh or abs(di_plus[i] - di_minus[i]) <= spread_min:
            continue
        e = ema_slow[i]
        if di_plus[i] > di_minus[i] and (not use_ema or (e is not None and closes[i] > e)):
            out[i] = 1
        elif di_minus[i] > di_plus[i] and (not use_ema or (e is not None and closes[i] < e)):
            out[i] = -1
    return out
