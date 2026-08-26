"""拉伸度 —— 用波动率单位度量的超买超卖。纯确定性，无 I/O。

**为什么另起一个指标，而不是继续用 technicals 的过热分**

过热分把 RSI6 / KDJ-J / CCI / 布林%b / BIAS6 五个分量打分求和。2026-08 的回测暴露了
三个结构性问题（诊断脚本见 `undertow backtest-stretch --diagnose`）：

  1. 五个分量彼此相关 0.79~0.93 —— 它们测的都是"价格在近期区间的哪个位置"。
     求和不是"五个指标共振"，是**一个指标被数了四遍**，放大分数而不增加信息。
  2. "强超买(≥+4)"占了 20.5% 的时间。一个五分之一时间都在触发的信号不叫极端。
  3. 98% 的"强超买"发生在 MA20 之上，corr(过热分, BIAS)=0.65 —— 它其实是趋势强度
     的另一种写法。

拉伸度只做一件事：**(现价 − MA20) / ATR14**，即"偏离均线几个 ATR"。再对它自己的
历史取滚动分位，所以"极端"按定义就是罕见的（最高/最低 5% 就是 5% 的时间）。

**为什么用 ATR 归一化**：它让读数跨品种、跨波动率环境可比（白银 IV 是黄金两倍，
同样 3% 的偏离意义完全不同）；更重要的是，ATR 正是期权盈亏平衡距离的天然单位——
"离均线 2.5 个 ATR"可以直接换算成"买方需要标的走多远"。

**已知边界（回测实测，不是猜的）**：

  * **日线有效，1 小时 / 4 小时线是噪音**。1H/4H 上各候选信号的高低位分离度全在
    ±0.2pp 以内且符号不稳定（4H 12,661 样本 / 1H 61,000+ 样本），别在更短周期用。

  * **只有超卖侧过得了显著性检验**。29,672 样本、Welch 双样本 t（不重叠子样本）：
        极超卖-牛 +1.06pp (t=+2.42)　强超卖-牛 +0.55pp (t=+2.37)　极超卖-熊 +1.05pp (t=+2.20)
    超买侧六个桶方向全对且单调，但最强的也只有 t=-1.66（极超买-牛），**达不到 2.0**。
    所以超买只能当「追高性价比差」的参考，不能当信号用，更不能当反转信号。

  * **超卖侧牛熊几乎一样强**（牛 +1.059pp / 熊 +1.049pp）——说明它不是牛市红利。
    用 2 年金银数据测出的「超卖很灵」确实是 regime 假象，但拉长到 33 年、扣掉同
    regime 中性桶之后边缘依然在，且牛熊对称。

  * **口径**：拉伸度是两个"价格距离"的比值，**量纲自动抵消**，所以它是少数几个能从
    标的直接搬到杠杆 ETF 上的读数——NQ=F 的拉伸度 ≈ TQQQ 的拉伸度（分子分母同步 ×3）。
    这跟行权价/点位那套必须换算的口径不一样，别混淆：位点要换算，拉伸度不用。
    （但 3 倍 ETF 的波动损耗会让长期读数缓慢漂移，多周尺度上仍以标的为准。）

  * 历史教训：早先版本用单样本 t（检验"本桶超额≠0"）而非双样本 t（检验"本桶≠什么都
    不做"），把一批噪音判成显著，包括一条"熊市偏超买反向 t=+2.61"的伪结论。改 CALIB
    一律用 `undertow backtest-stretch --emit` 重跑，不要手改数字。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

MA_N = 20          # 拉伸度的基准均线
ATR_N = 14         # 波动率单位
PCT_WINDOW = 250   # 滚动分位窗口（约一年）
REGIME_N = 200     # 牛熊分界均线
MIN_BARS = REGIME_N + 30

# 分位分档（上界，左闭右开）。与回测的分桶一一对应。
BANDS = [
    (0.05, "极超卖"), (0.10, "强超卖"), (0.25, "偏超卖"),
    (0.75, "中性"), (0.90, "偏超买"), (0.95, "强超买"), (1.01, "极超买"),
]

# ── 回测校准表 ──────────────────────────────────────────────────────────
# 来源：undertow backtest-stretch（GLD/SLV/USO/QQQ/SPY 日线，1993→2026，29,522 样本）
# 口径：+5日收益 − 过去60日局部漂移 − 同 regime 中性桶均值 → "相对什么都不做多赚多少"
# t 值用不重叠子样本（每 5 根取 1 根）计算，避免重叠窗口虚增显著性。
# 数值单位：百分点(pp)。改指标参数后必须重跑本表，别手改。
CALIB_ASOF = "2026-08-26"
CALIB: dict[tuple[str, str], tuple[float, float, int, float]] = {
    # (档位, regime): (5日边缘pp, 胜率%, 样本数, Welch双样本t)
    ("极超卖", "牛"): (+1.059, 62, 735, 2.42),
    ("强超卖", "牛"): (+0.546, 56, 749, 2.37),
    ("偏超卖", "牛"): (+0.211, 52, 2549, 1.21),
    ("中性", "牛"): (+0.000, 47, 9712, 0.00),
    ("偏超买", "牛"): (-0.206, 42, 3080, -1.56),
    ("强超买", "牛"): (-0.355, 41, 1232, -1.09),
    ("极超买", "牛"): (-0.321, 43, 1515, -1.66),
    ("极超卖", "熊"): (+1.049, 66, 982, 2.20),
    ("强超卖", "熊"): (+0.697, 65, 693, 0.53),
    ("偏超卖", "熊"): (+0.017, 56, 1895, 0.24),
    ("中性", "熊"): (+0.000, 58, 4696, 0.00),
    ("偏超买", "熊"): (+0.212, 59, 1223, -0.06),
    ("强超买", "熊"): (-0.277, 51, 351, -0.40),
    ("极超买", "熊"): (-0.718, 50, 260, -0.73),
}
T_SIGNIFICANT = 2.0    # |t| ≥ 此值才当作可用信号；否则标"样本不足/不显著"


@dataclass(frozen=True)
class StretchRead:
    ok: bool
    stretch: float | None = None      # (现价−MA20)/ATR14，正=在均线之上
    pctile: float | None = None       # 该值在自身过去 PCT_WINDOW 根中的分位 0~1
    band: str = ""                    # 极超卖/强超卖/偏超卖/中性/偏超买/强超买/极超买
    regime: str = ""                  # 牛 / 熊（MA200 之上/之下）
    ma20: float | None = None
    ma200: float | None = None
    atr: float | None = None
    edge_pp: float | None = None      # 回测校准：该(档位,regime)的 5 日边缘
    win_rate: float | None = None
    n_hist: int | None = None
    t_stat: float | None = None
    reliable: bool = False            # |t| ≥ T_SIGNIFICANT
    headline: str = ""
    note: str = ""


def _sma(xs, n):
    return statistics.fmean(xs[-n:]) if len(xs) >= n else None


def _atr(highs, lows, closes, n=ATR_N):
    if len(closes) < n + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    return statistics.fmean(trs[-n:])


def stretch_series(highs, lows, closes, ma_n=MA_N, atr_n=ATR_N) -> list:
    """逐根的拉伸度序列（不足回看期为 None）。第 i 位只用 ≤i 的数据，无前视。"""
    n = len(closes)
    out: list = [None] * n
    # 滚动均线
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= ma_n:
            s -= closes[i - ma_n]
        if i < ma_n - 1:
            continue
        ma = s / ma_n
        a = _atr(highs[max(0, i - atr_n * 3):i + 1], lows[max(0, i - atr_n * 3):i + 1],
                 closes[max(0, i - atr_n * 3):i + 1], atr_n)
        if a:
            out[i] = (closes[i] - ma) / a
    return out


def pct_rank_last(series, window=PCT_WINDOW) -> float | None:
    """末值在过去 window 个有效值中的百分位（0~1）。窗口不足一半则返回 None。"""
    if not series or series[-1] is None:
        return None
    hist = [x for x in series[max(0, len(series) - 1 - window):-1] if x is not None]
    if len(hist) < window // 2:
        return None
    return sum(1 for x in hist if x < series[-1]) / len(hist)


def band_of(pctile: float) -> str:
    for hi, name in BANDS:
        if pctile < hi:
            return name
    return "极超买"


def analyze_stretch(series) -> StretchRead:
    """从 PriceSeries 算拉伸度读数 + 回测校准解读。"""
    if series is None or len(series.closes) < MIN_BARS:
        got = 0 if series is None else len(series.closes)
        return StretchRead(ok=False, note=f"价序 {got} 根 < {MIN_BARS}，拉伸度跳过"
                                          f"（需 MA200 定 regime + 分位窗口）")
    c = series.closes
    h = series.highs if (series.highs and len(series.highs) == len(c)) else c
    lo = series.lows if (series.lows and len(series.lows) == len(c)) else c

    ss = stretch_series(h, lo, c)
    val, pct = ss[-1], pct_rank_last(ss)
    ma20, ma200, atr = _sma(c, MA_N), _sma(c, REGIME_N), _atr(h, lo, c)
    if val is None or pct is None or ma200 is None:
        return StretchRead(ok=False, note="拉伸度或其滚动分位数据不足")

    regime = "牛" if c[-1] > ma200 else "熊"
    band = band_of(pct)
    cal = CALIB.get((band, regime))
    edge, wr, nh, t = cal if cal else (None, None, None, None)
    reliable = bool(t is not None and abs(t) >= T_SIGNIFICANT)

    # 解读：一律基于回测数字，不做"要反转了"这类无据断言
    if band == "中性":
        head = f"{band}（分位 {pct*100:.0f}%）—— 无方向性边缘"
    elif edge is None:
        head = f"{band}（分位 {pct*100:.0f}%）—— 无校准数据"
    elif not reliable:
        head = (f"{band}·{regime}市（分位 {pct*100:.0f}%）—— 历史边缘 {edge:+.2f}pp/5日，"
                f"但 t={t:.2f} 未达显著，**当参考不当信号**")
    elif edge > 0:
        head = (f"{band}·{regime}市（分位 {pct*100:.0f}%）—— 历史上此后 5 日跑赢"
                f"什么都不做 {edge:+.2f}pp，胜率 {wr:.0f}%（n={nh}, t={t:.2f}）")
    else:
        head = (f"{band}·{regime}市（分位 {pct*100:.0f}%）—— 历史上此后 5 日跑输"
                f"{edge:+.2f}pp，胜率 {wr:.0f}%（n={nh}, t={t:.2f}）；"
                f"**是「追高性价比差」，不是「要跌了」**")

    note = f"拉伸度 = (现价−MA{MA_N})/ATR{ATR_N} = {val:+.2f} 个 ATR"
    if regime == "熊" and band in ("偏超买", "强超买"):
        note += "　⚠️ 熊市中的超买是反弹动能，牛市那套「追高性价比差」在这里不适用"
    return StretchRead(ok=True, stretch=val, pctile=pct, band=band, regime=regime,
                       ma20=ma20, ma200=ma200, atr=atr, edge_pp=edge, win_rate=wr,
                       n_hist=nh, t_stat=t, reliable=reliable, headline=head, note=note)


def render_md(sr: StretchRead, display_name: str = "") -> str:
    if not sr.ok:
        return f"- 拉伸度：{sr.note}"
    L = [f"**拉伸度**{('（' + display_name + '）') if display_name else ''}：{sr.headline}",
         "",
         f"- 现价距 MA{MA_N}：**{sr.stretch:+.2f} 个 ATR**"
         f"（MA{MA_N}={sr.ma20:.2f}，ATR{ATR_N}={sr.atr:.2f}）",
         f"- 自身分位：**{sr.pctile*100:.0f}%** → {sr.band}",
         f"- Regime：MA{REGIME_N}={sr.ma200:.2f} → **{sr.regime}市**"]
    # 中性桶本身就是基准（边缘恒为 0），列出来只会让人误读成"有个 +0.00 的信号"
    if sr.edge_pp is not None and sr.band != "中性":
        flag = "" if sr.reliable else f"（t={sr.t_stat:+.2f} 未达 2.0，参考级）"
        L.append(f"- 回测校准（{CALIB_ASOF}）：5 日边缘 **{sr.edge_pp:+.2f}pp**、"
                 f"胜率 {sr.win_rate:.0f}%、n={sr.n_hist}{flag}")
    L.append(f"- {sr.note}")
    L.append("")
    L.append("> 口径：边缘 = +5日收益 − 过去60日局部漂移 − 同 regime 中性桶，即「比什么都不做多赚多少」。")
    L.append("> **本指标只在日线成立**：1H/4H 回测分离度 ±0.2pp 且符号不稳定，属噪音。")
    return "\n".join(L)
