"""DMI / ADX + 趋势质量评分 —— 取自 "DMI/ADX Trend Dashboard v3"（用户 2026-09-03）。

Pine 的 `ta.dmi(diLen, adxLen)` 展开是：

    upMove   = high − high[1]
    downMove = low[1] − low
    +DM = (upMove > downMove and upMove > 0) ? upMove : 0
    −DM = (downMove > upMove and downMove > 0) ? downMove : 0
    +DI = 100 × rma(+DM, n) / rma(tr, n)
    −DI = 100 × rma(−DM, n) / rma(tr, n)
    DX  = 100 × |+DI − −DI| / (+DI + −DI)
    ADX = rma(DX, adxLen)

注意 ±DM 的互斥条件：**同一根 K 线不可能同时产生 +DM 和 −DM**，
两个方向都动时只记更大的那个。这是 DMI 与"内外包线"类指标的关键区别。

ADX 是 DX 的二次平滑（rma 套 rma），**滞后很重**。
它衡量的是趋势"强度"而非方向 —— ADX 高只说明在单边走，不说明往哪走。

⚠️ 趋势评分是同源重复计数
---------------------------
脚本的 `trendScore` 满分 100，其中：

    ADX 水平    30 分  ┐
    DI 差       20 分  ├ 75 分全部来自 DMI/ADX 家族
    ADX 斜率    25 分  ┘
    EMA 斜率    25 分  ← 唯一的独立成分

跟上一份脚本的 `bullScore` 是同一个毛病（100 分里 60 分同源），
只是这里更隐蔽：ADX 水平与 ADX 斜率读起来像两件事，其实是同一条曲线的
值与导数。**"四项确认"实际只有两个独立信息源。**

可取之处：`activeWeight` 会随开关自动重新归一化，比硬编码权重好。

⛔ 备用层：不进研报、不进方向投票。
"""
from __future__ import annotations

from dataclasses import dataclass

from undertow.analyze.ta import ema, rma, true_range

DI_LEN, ADX_LEN = 14, 14
ADX_THRESH, ADX_EXTREME = 20, 40
DI_SPREAD_MIN = 10.0
W_ADX, W_SPREAD, W_ADX_SLOPE, W_EMA_SLOPE = 30.0, 20.0, 25.0, 25.0
EMA_SLOPE_LEN, EMA_SLOPE_CAP = 5, 2.0


@dataclass(frozen=True)
class DmiReading:
    di_plus: float
    di_minus: float
    adx: float
    score: float            # 趋势质量 0~100
    tier: str               # High / Med / Low

    @property
    def spread(self) -> float:
        return abs(self.di_plus - self.di_minus)

    @property
    def bullish(self) -> bool:
        return self.di_plus > self.di_minus

    @property
    def trending(self) -> bool:
        """ADX 达阈值。⚠️ 只说明在单边走，不说明方向。"""
        return self.adx >= ADX_THRESH

    @property
    def size_mult(self) -> float:
        """脚本的按分数调仓：≥70 加到 1.5 倍，<45 减半。阈值与倍数都是拍脑袋的。"""
        return 1.5 if self.score >= 70 else (1.0 if self.score >= 45 else 0.5)


def dmi(highs, lows, closes, *, di_len: int = DI_LEN, adx_len: int = ADX_LEN
        ) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """返回 (+DI, −DI, ADX)，等长，前段为 None。"""
    n = len(highs)
    plus_dm, minus_dm = [0.0] * n, [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        # 互斥：两个方向都动时只记更大的那个
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
    trur = rma(true_range(highs, lows, closes), di_len)
    sp = rma(plus_dm, di_len)
    sm = rma(minus_dm, di_len)

    dip: list[float | None] = []
    dim: list[float | None] = []
    dx: list[float | None] = []
    for i in range(n):
        if trur[i] is None or sp[i] is None or sm[i] is None or trur[i] == 0:
            dip.append(None); dim.append(None); dx.append(None)
            continue
        p, m = 100 * sp[i] / trur[i], 100 * sm[i] / trur[i]
        dip.append(p); dim.append(m)
        s = p + m
        dx.append(100 * abs(p - m) / (s if s else 1))

    idx = [i for i, v in enumerate(dx) if v is not None]
    adx_vals = rma([dx[i] for i in idx], adx_len) if idx else []
    adx: list[float | None] = [None] * n
    for k, i in enumerate(idx):
        if k < len(adx_vals):
            adx[i] = adx_vals[k]
    return dip, dim, adx


def trend_score(adx_val: float, spread: float, adx_slope: float,
                ema_slope_aligned: float, *,
                w_adx: float = W_ADX, w_spread: float = W_SPREAD,
                w_adx_slope: float = W_ADX_SLOPE,
                w_ema_slope: float = W_EMA_SLOPE) -> float:
    """脚本的 trendScore。权重可关，`activeWeight` 自动归一化。

    ⚠️ 四项里三项同源（见文件头）。这个分数不是四重确认，是两重。
    """
    active = w_adx + w_spread + w_adx_slope + w_ema_slope
    if active <= 0:
        return 0.0
    raw = (min(adx_val / ADX_EXTREME, 1.0) * w_adx
           + min(spread / 30.0, 1.0) * w_spread
           + (min(adx_slope / 5.0, 1.0) if adx_slope > 0 else 0.0) * w_adx_slope
           + max(min(ema_slope_aligned / EMA_SLOPE_CAP, 1.0), 0.0) * w_ema_slope)
    return raw / active * 100.0


def read(highs, lows, closes, *, ema_len: int = 50, **kw) -> DmiReading | None:
    dip, dim, adx = dmi(highs, lows, closes, **kw)
    if not adx or adx[-1] is None or dip[-1] is None or dim[-1] is None:
        return None
    e = ema(closes, ema_len)
    slope = 0.0
    if len(e) > EMA_SLOPE_LEN and e[-1] is not None and e[-1 - EMA_SLOPE_LEN] is not None:
        base = e[-1 - EMA_SLOPE_LEN]
        if base:
            raw = (e[-1] - base) / base * 100.0
            state = 1 if (e[-1] is not None and closes[-1] > e[-1]) else -1
            slope = raw * state
    a_slope = (adx[-1] - adx[-4]) if (len(adx) > 3 and adx[-4] is not None) else 0.0
    sc = trend_score(adx[-1], abs(dip[-1] - dim[-1]), a_slope, slope)
    tier = "High" if sc >= 70 else ("Med" if sc >= 45 else "Low")
    return DmiReading(dip[-1], dim[-1], adx[-1], sc, tier)
