"""随机指标（Stochastic）+ 多周期版本 —— 取自 "EMA Trend + MTF Stochastic Strategy"。

用户 2026-09-03 提供该脚本。本模块只移植其中的**指标部分**；
整套策略为什么不整体移植，见 docs/ta_modules.md 的分析（一句话：14 个可调参数）。

    k = sma(stoch(close, high, low, len), smoothK)
    d = sma(k, smoothD)

MTF 版额外套一层线性回归：

    [linreg(kk, len, 0), linreg(dd, len, 0)]

⚠️ `ta.linreg` 不是滞后平均，拟合直线比均线更早转向。转向早不等于预测对。

⚠️ MTF 取数必须 `lookahead=barmerge.lookahead_off`（脚本写对了）。
关掉前瞻后，高周期的值只在高周期 K 线**收盘后**才更新 —— 于是低周期图上
看到的 MTF 线是**阶梯状**的，信号会在高周期收盘那一刻扎堆。这是正确行为，
但读图时别把阶梯的垂直段当成"突然转向"。

⛔ 备用层：不进研报、不进方向投票。
"""
from __future__ import annotations

from dataclasses import dataclass

from undertow.analyze.ta import highest, linreg, lowest, sma

LEN, SMOOTH_K, SMOOTH_D = 11, 3, 3
UP_LINE, LOW_LINE = 80, 20


@dataclass(frozen=True)
class StochReading:
    k: float
    d: float
    mtf_k: float | None
    mtf_d: float | None

    @property
    def overbought(self) -> bool:
        return self.k > UP_LINE

    @property
    def oversold(self) -> bool:
        return self.k < LOW_LINE


def raw_stoch(closes: list[float], highs: list[float], lows: list[float],
              n: int = LEN) -> list[float | None]:
    """Pine ta.stoch = 100 × (close − lowest(low,n)) / (highest(high,n) − lowest(low,n))。"""
    hh, ll = highest(highs, n), lowest(lows, n)
    out: list[float | None] = []
    for i in range(len(closes)):
        if hh[i] is None or ll[i] is None:
            out.append(None)
            continue
        rng = hh[i] - ll[i]
        out.append(50.0 if rng == 0 else (closes[i] - ll[i]) / rng * 100)
    return out


def _sma_sparse(xs: list[float | None], n: int) -> list[float | None]:
    """对含 None 前缀的序列做 SMA，按原位置贴回。"""
    idx = [i for i, v in enumerate(xs) if v is not None]
    if not idx:
        return [None] * len(xs)
    vals = sma([xs[i] for i in idx], n)
    out: list[float | None] = [None] * len(xs)
    for k, i in enumerate(idx):
        if k < len(vals):
            out[i] = vals[k]
    return out


def stoch_kd(closes, highs, lows, *, n: int = LEN, smooth_k: int = SMOOTH_K,
             smooth_d: int = SMOOTH_D) -> tuple[list[float | None], list[float | None]]:
    """返回 (k, d)。"""
    k = _sma_sparse(raw_stoch(closes, highs, lows, n), smooth_k)
    d = _sma_sparse(k, smooth_d)
    return k, d


def stoch_linreg(closes, highs, lows, *, n: int = LEN, smooth_k: int = SMOOTH_K,
                 smooth_d: int = SMOOTH_D
                 ) -> tuple[list[float | None], list[float | None]]:
    """脚本 f_stoch() 的返回：对 k/d 各套一层 linreg(len, 0)。MTF 分支用它。"""
    k, d = stoch_kd(closes, highs, lows, n=n, smooth_k=smooth_k, smooth_d=smooth_d)

    def _lr(xs):
        idx = [i for i, v in enumerate(xs) if v is not None]
        if len(idx) < n:
            return [None] * len(xs)
        vals = linreg([xs[i] for i in idx], n)
        out: list[float | None] = [None] * len(xs)
        for j, i in enumerate(idx):
            if j < len(vals):
                out[i] = vals[j]
        return out

    return _lr(k), _lr(d)


def align_mtf(base_ts: list, mtf_ts: list, mtf_vals: list[float | None]
              ) -> list[float | None]:
    """把高周期序列按 `lookahead_off` 的语义对齐到低周期时间轴。

    规则：**第 k 根高周期 K 线，只有在第 k+1 根开始之后才可见。**
    末根永远不可见 —— 它可能还没收盘。

    ⚠️ 2026-09-03 修掉的前瞻偏差
    ----------------------------
    原实现用 `mtf_ts[j] <= t` 判定可见，这是错的，因为**长桥的 ts 是 K 线的
    开始时间**，不是结束时间：

        1d  末根 ts = 2026-09-02 04:00   （当天 ET 00:00，要到 20:00 才收盘）
        4h  盘中 ts = 2026-09-02 16:30
        1h  末根 ts = 2026-09-02 19:30

    于是 4h 在 16:30 那根就能读到当天日线的收盘价 —— **泄露 3.5 小时**。
    1h→4h 同理泄露 1 小时（4h 的 ts 是组内最后一根 1h 的开始时间，
    该组实际要到那根 1h 收盘才结束）。

    用「下一根已开始」判定可见，正是 Pine `lookahead_off` 的实际行为：
    高周期 bar 未完成时返回**上一个已完成** bar 的值。
    """
    out: list[float | None] = []
    k = -1
    for t in base_ts:
        while k + 2 < len(mtf_ts) and mtf_ts[k + 2] <= t:
            k += 1
        out.append(mtf_vals[k] if 0 <= k < len(mtf_vals) else None)
    return out


def read_mtf(symbol: str, tf: str) -> StochReading | None:
    """当前周期的 k/d + 上一级周期的 linreg k/d。上一级不存在时 mtf_* 为 None。"""
    from undertow.analyze.ta.frames import MTF_PARENT, bars

    try:
        b = bars(symbol, tf)
    except Exception:
        return None
    c = [x["close"] for x in b]; h = [x["high"] for x in b]; l = [x["low"] for x in b]
    k, d = stoch_kd(c, h, l)
    if not k or k[-1] is None or d[-1] is None:
        return None

    mk = md = None
    parent = MTF_PARENT.get(tf)
    if parent:
        try:
            pb = bars(symbol, parent)
            pc = [x["close"] for x in pb]; ph = [x["high"] for x in pb]
            pl = [x["low"] for x in pb]
            pk, pd = stoch_linreg(pc, ph, pl)
            ts = [x["ts"] for x in b]; pts = [x["ts"] for x in pb]
            mk = align_mtf(ts, pts, pk)[-1]
            md = align_mtf(ts, pts, pd)[-1]
        except Exception:
            pass
    return StochReading(k[-1], d[-1], mk, md)
