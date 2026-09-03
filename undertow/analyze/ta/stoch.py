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
    """Pine ta.stoch = 100 × (close − lowest(low,n)) / (highest(high,n) − lowest(low,n))。

    ⚠️ **有意偏离**：区间为零（最高=最低，完全平坦）时 Pine 会除零得 na，
    我们返回 50。理由是后续 SMA/linreg 遇到 na 会整段中断，而完全平坦区间
    在日内低周期上并不罕见（停牌、极低流动性）。代价是会造出一个 Pine 里
    不存在的中性值 —— 这一点 codex review 提过（P2-3），此处明确记为偏离，
    不是"与 Pine 一致"。
    """
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


def align_mtf(base_ts: list, mtf_close_ts: list, mtf_vals: list[float | None]
              ) -> list[float | None]:
    """把高周期序列按 `lookahead_off` 的语义对齐到低周期时间轴。

    规则：**第 k 根高周期 K 线在它的收盘时刻之后才可见。**
    对应 TradingView 的说法 ——「fills the gaps with the **last confirmed
    values** on historical bars」。

    ⚠️ 必须传 close_ts，不能传 ts
    ---------------------------------
    长桥的 `ts` 是 K 线**开盘**时间。2026-09-03 起 `frames.bars()` 会给每根
    附上 `close_ts`，跨周期比较一律用它。这里有两处坑：

      · 用 ts 判定 → 直接泄露未来。日线 ts=当天 04:00，4h 盘中 ts=16:30，
        于是 4h 在 16:30 就读到了当天日线的收盘价（泄露 3.5 小时）。
      · 用「下一根 ts 已开始」判定 → 不泄露了，但会**错移信号**：
        合成 4h 的 ts 是组内最后一根 1h 的开盘，组 1（ts=16:30）实际 17:30
        就收盘了，却要等到组 2 开始（19:30）才释放，晚 2 小时；
        而且末根永远不可见 —— 一根已确定收盘、只是没有后继的历史 K 线
        不该被永久屏蔽。（codex review P1-5）
    """
    out: list[float | None] = []
    k = -1
    for t in base_ts:
        while k + 1 < len(mtf_close_ts) and mtf_close_ts[k + 1] <= t:
            k += 1
        out.append(mtf_vals[k] if 0 <= k < len(mtf_vals) else None)
    return out


def read_mtf(symbol: str, tf: str) -> StochReading | None:
    """当前周期的 k/d + 上一级周期的 linreg k/d。上一级不存在时 mtf_* 为 None。"""
    from undertow.collect.longbridge_kline import KlineUnavailable
    from undertow.analyze.ta.frames import MTF_PARENT, bars

    try:
        b = bars(symbol, tf)
    except (KlineUnavailable, ValueError):
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
            ts = [x["ts"] for x in b]
            pcts = [x["close_ts"] for x in pb]
            mk = align_mtf(ts, pcts, pk)[-1]
            md = align_mtf(ts, pcts, pd)[-1]
        except (KlineUnavailable, ValueError, KeyError):
            mk = md = None      # 取不到父周期是正常；程序错误必须往外抛
    return StochReading(k[-1], d[-1], mk, md)
