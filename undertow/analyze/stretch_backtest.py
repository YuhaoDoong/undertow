"""拉伸度校准回测 —— 纯确定性，无 I/O（数据由调用方喂进来）。

**为什么这个模块必须存在**：`stretch.py` 里的 CALIB 表是一组具体数字，谁都可以
拍一个。有了本模块，那张表就是**可重跑、可证伪**的——改了指标参数（MA_N/ATR_N/
分档边界）就重跑，数字自己会变。任何"我觉得超买该止盈"式的断言都不该进代码。

**方法学上刻意做对的三件事**（都是踩过的坑）：

  1. **局部去趋势**：超额 = 前瞻收益 − 该品种【过去 DRIFT 日】的漂移，而不是减全样本
     均值。后者会把一整段牛市的上涨当成"基准外收益"白送给抄底信号——用 2 年金银数据
     测出来的"超卖很灵"就是这么来的假象。

  2. **同 regime 内比较**：熊市里局部漂移是负的，减掉它会让熊市样本的超额整体抬高
     （实测熊市中性桶 +0.46%、牛市中性桶 −0.42%）。所以边缘一律定义为
     【该桶 − 同 regime 中性桶】，跨 regime 才可比。

  3. **不重叠子样本 + Welch 双样本 t**：相邻交易日的前瞻窗口高度重叠，直接做 t 检验
     会把 n 虚增几倍。故每 horizon 根取 1 根，且 t 检验的对象必须与"边缘"同源
     （桶 vs 中性桶的均值差），不能拿原始超额的单样本 t 来冒充。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .stretch import BANDS, MA_N, ATR_N, PCT_WINDOW, REGIME_N, band_of, stretch_series

DRIFT_N = 60        # 局部漂移窗口
DEFAULT_HZ = (2, 3, 5, 10)
NEUTRAL = "中性"


def _sma_series(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _pct_rank_series(xs, window=PCT_WINDOW):
    out = [None] * len(xs)
    for i in range(len(xs)):
        if xs[i] is None:
            continue
        hist = [x for x in xs[max(0, i - window):i] if x is not None]
        if len(hist) < window // 2:
            continue
        out[i] = sum(1 for x in hist if x < xs[i]) / len(hist)
    return out


def welch_t(a: list, b: list) -> float:
    """Welch 双样本 t（不假设等方差）。样本不足返回 0。"""
    if len(a) < 3 or len(b) < 3:
        return 0.0
    va, vb = statistics.variance(a), statistics.variance(b)
    den = math.sqrt(va / len(a) + vb / len(b))
    return (statistics.fmean(a) - statistics.fmean(b)) / den if den > 0 else 0.0


@dataclass(frozen=True)
class Sample:
    name: str
    i: int
    pctile: float
    regime: str                       # 牛 / 熊
    excess: dict = field(default_factory=dict)   # {horizon: 去趋势超额%}


def build_samples(name, highs, lows, closes, *, horizons=DEFAULT_HZ,
                  warmup=None, drift_n=DRIFT_N) -> list:
    """把一条价序转成回测样本。第 i 个样本只用 ≤i 的数据算信号，无前视。"""
    n = len(closes)
    warm = warmup if warmup is not None else max(PCT_WINDOW + MA_N, REGIME_N + 50)
    if n < warm + max(horizons) + 10:
        return []
    pr = _pct_rank_series(stretch_series(highs, lows, closes))
    m200 = _sma_series(closes, REGIME_N)
    out = []
    for i in range(warm, n - max(horizons)):
        if pr[i] is None or m200[i] is None or i < drift_n:
            continue
        d = (closes[i] / closes[i - drift_n] - 1) / drift_n      # 日均局部漂移
        out.append(Sample(name=name, i=i, pctile=pr[i],
                          regime="牛" if closes[i] > m200[i] else "熊",
                          excess={k: (closes[i + k] / closes[i] - 1) * 100 - d * k * 100
                                  for k in horizons}))
    return out


def calibrate(samples: list, *, horizon: int = 5, min_n: int = 50) -> dict:
    """按 (档位, regime) 算相对同 regime 中性桶的边缘 + Welch t。

    返回 {(band, regime): {"n","edge_pp","win_rate","t","mean_excess"}}。
    """
    out = {}
    for regime in ("牛", "熊"):
        pool = [s for s in samples if s.regime == regime]
        neu = [s for s in pool if band_of(s.pctile) == NEUTRAL]
        if len(neu) < min_n:
            continue
        neu_mean = statistics.fmean([s.excess[horizon] for s in neu])
        # 不重叠子样本：每 horizon 根取 1 根
        neu_nov = [s.excess[horizon] for s in neu if s.i % horizon == 0]
        for _, band in BANDS:
            tgt = [s for s in pool if band_of(s.pctile) == band]
            if len(tgt) < min_n:
                continue
            ex = [s.excess[horizon] for s in tgt]
            tgt_nov = [s.excess[horizon] for s in tgt if s.i % horizon == 0]
            out[(band, regime)] = {
                "n": len(tgt),
                "mean_excess": statistics.fmean(ex),
                # 边缘与 t 同源：都是"本桶 vs 同 regime 中性桶"的均值差
                "edge_pp": statistics.fmean(ex) - neu_mean,
                "win_rate": sum(1 for x in ex if x > 0) / len(ex) * 100,
                "t": 0.0 if band == NEUTRAL else welch_t(tgt_nov, neu_nov),
                "n_nov": len(tgt_nov),
            }
    return out


def render_table_md(cal: dict, *, horizon: int = 5, total: int = 0, span: str = "") -> str:
    L = [f"### 拉伸度校准表（+{horizon} 日）", ""]
    if total:
        L.append(f"*样本 {total:,}{('，' + span) if span else ''}；"
                 f"边缘 = 本桶 − 同 regime 中性桶（局部去趋势后）；t = Welch 双样本、不重叠子样本*")
        L.append("")
    L.append("| 档位 | regime | 触发 | 边缘 | 胜率 | t | 判定 |")
    L.append("|---|---|---:|---:|---:|---:|---|")
    order = [b for _, b in BANDS]
    for regime in ("牛", "熊"):
        for band in order:
            r = cal.get((band, regime))
            if not r:
                continue
            if band == NEUTRAL:
                verdict = "基准"
            elif abs(r["t"]) >= 2.0:
                verdict = "✅ 显著"
            elif abs(r["t"]) >= 1.5:
                verdict = "~ 边缘"
            else:
                verdict = "❌ 不显著"
            L.append(f"| {band} | {regime}市 | {r['n']} | **{r['edge_pp']:+.3f}pp** | "
                     f"{r['win_rate']:.0f}% | {r['t']:+.2f} | {verdict} |")
    return "\n".join(L)


def render_calib_literal(cal: dict) -> str:
    """输出可直接粘回 stretch.py CALIB 的 Python 字面量。"""
    L = ["CALIB = {"]
    for regime in ("牛", "熊"):
        for _, band in BANDS:
            r = cal.get((band, regime))
            if not r:
                continue
            L.append(f'    ("{band}", "{regime}"): '
                     f'({r["edge_pp"]:+.3f}, {r["win_rate"]:.0f}, {r["n"]}, {r["t"]:.2f}),')
    L.append("}")
    return "\n".join(L)


def horizon_profile(samples: list, *, bands=("极超卖", "极超买"),
                    horizons=DEFAULT_HZ) -> dict:
    """各档位在不同前瞻天数上的边缘 —— 看信号能持续多久。"""
    out = {}
    for band in bands:
        row = {}
        for k in horizons:
            tgt = [s.excess[k] for s in samples if band_of(s.pctile) == band]
            neu = [s.excess[k] for s in samples if band_of(s.pctile) == NEUTRAL]
            if len(tgt) < 50 or len(neu) < 50:
                continue
            row[k] = statistics.fmean(tgt) - statistics.fmean(neu)
        out[band] = row
    return out
