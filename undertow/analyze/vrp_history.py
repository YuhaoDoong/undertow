"""波动率溢价（VRP）的**跨周期稳定性检验** —— 回答「这个 edge 能不能穿越牛熊」。

与 `volregime.py` 的分工：
    volregime.py    今天的波动率环境：IV 分位、ATM IV − RV、偏买方/卖方
    vrp_history.py  **历史上**这个溢价稳不稳定、在熊市里还在不在

为什么必须单独做这件事：卖期权的收益很容易被误读成 edge，实际上是**方向敞口的伪装**。
一个简单的判别法 —— 看它与标的年度涨跌的相关性：

    「裸卖 put 的胜率」与年度涨跌相关性 ≈ +0.9   → 就是做多，不是策略
    「VRP（IV − 未来实际波动）」相关性  ≈ −0.35  → 与方向基本无关，是真 edge

VRP 用的是 **IV 指数 vs 其后 N 日的已实现波动**，即「当时市场收的保费」减
「事后真正发生的波动」。为正说明卖方收贵了。这是**前视对齐**的：
把当日 IV 和它所承保的那段未来波动放在一起比，而不是和过去的波动比 ——
后者是常见但错误的算法（会把波动率的自相关误当成溢价）。

诚实边界：
  - 重叠窗口使样本量虚高，独立样本约 = 天数 / 窗口长度，显著性远低于表面。
  - 卖方收益分布左偏：多数日子赚小钱，少数日子巨亏。**均值为正不代表能扛住那几天。**
  - VRP 为正是「毛溢价」；真要拿到手必须对冲 delta，而对冲成本会吃掉相当一部分。
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

_ANNUALIZE = math.sqrt(252.0)


@dataclass(frozen=True)
class YearStat:
    year: int
    n: int
    mean_vrp: float          # pp
    median_vrp: float
    positive_share: float
    underlying_return: float  # 当年标的涨跌 %

    @property
    def verdict(self) -> str:
        if self.mean_vrp > 2:
            return "🟢"
        if self.mean_vrp < 0:
            return "🔴"
        return "⚪"


@dataclass(frozen=True)
class VrpHistory:
    index_name: str
    window: int
    n_pairs: int
    start: date
    end: date
    mean_vrp: float
    median_vrp: float
    positive_share: float
    year_stats: list[YearStat] = field(default_factory=list)
    corr_with_direction: float | None = None
    caveats: list[str] = field(default_factory=list)

    @property
    def negative_years(self) -> list[int]:
        return [y.year for y in self.year_stats if y.mean_vrp < 0]

    @property
    def independent_samples(self) -> int:
        """去掉重叠后的有效样本数 —— 显著性该按这个看，不是 n_pairs。"""
        return max(self.n_pairs // self.window, 1)

    @property
    def regime_robust(self) -> bool:
        """跨周期稳健的判定：多数年份为正，且与方向不高度相关。"""
        if not self.year_stats:
            return False
        pos_years = sum(1 for y in self.year_stats if y.mean_vrp > 0)
        low_corr = self.corr_with_direction is None or abs(self.corr_with_direction) < 0.5
        return pos_years / len(self.year_stats) >= 0.7 and low_corr


def forward_realized_vol(dates: list[date], closes: list[float],
                         window: int = 21) -> dict[date, float]:
    """每个观测日**其后** `window` 个交易日的年化已实现波动（pp）。

    前视对齐是这个模块的关键 —— 当日收盘观测 IV，它承保的是**次日起**的波动，
    拿它去比过去的波动是答非所问。严格未来：不含观测当日已实现的那根收益。
    """
    # r[k] = dates[k+1] 相对 dates[k] 的对数收益（r 比 dates 短 1）
    r = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out: dict[date, float] = {}
    # 观测日 dates[j] 收盘后承保的是 dates[j+1..j+window] 的收益 = r[j:j+window]，纯未来。
    for j in range(len(dates) - window):
        seg = r[j:j + window]
        if len(seg) == window:
            out[dates[j]] = st.pstdev(seg) * _ANNUALIZE * 100
    return out


def _corr(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(a) != len(b):
        return None
    ma, mb = st.mean(a), st.mean(b)
    sa, sb = st.pstdev(a), st.pstdev(b)
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / (sa * sb)


def assess_vrp_history(*, iv_series: list[tuple[date, float]],
                       px_dates: list[date], px_closes: list[float],
                       index_name: str, window: int = 21) -> VrpHistory:
    """把 IV 指数历史与标的价格历史对齐，算逐年 VRP 与方向相关性。"""
    iv = dict(iv_series)
    fwd = forward_realized_vol(px_dates, px_closes, window)
    pairs = sorted((d, iv[d], fwd[d]) for d in fwd if d in iv)
    if len(pairs) < window * 2:
        return VrpHistory(index_name=index_name, window=window, n_pairs=len(pairs),
                          start=px_dates[0], end=px_dates[-1], mean_vrp=0.0,
                          median_vrp=0.0, positive_share=0.0,
                          caveats=["样本不足，无法评估"])

    by_year: dict[int, list[float]] = defaultdict(list)
    for d, v, f in pairs:
        by_year[d.year].append(v - f)

    px = dict(zip(px_dates, px_closes))
    ys: list[YearStat] = []
    for y in sorted(by_year):
        days = [d for d in px_dates if d.year == y]
        ret = (px[days[-1]] / px[days[0]] - 1) * 100 if len(days) > 1 else 0.0
        v = by_year[y]
        ys.append(YearStat(year=y, n=len(v), mean_vrp=st.mean(v),
                           median_vrp=st.median(v),
                           positive_share=sum(1 for x in v if x > 0) / len(v),
                           underlying_return=ret))

    allv = [x for v in by_year.values() for x in v]
    corr = _corr([y.mean_vrp for y in ys], [y.underlying_return for y in ys])

    caveats = [
        f"重叠窗口：{len(pairs)} 个配对样本里独立的约 {len(pairs) // window} 个，"
        f"显著性远低于表面",
        "VRP 为毛溢价 —— 实际拿到手须对冲 delta，对冲成本会吃掉相当一部分",
        "收益分布左偏：多数日子赚小钱，少数日子巨亏；均值为正≠扛得住那几天",
    ]
    return VrpHistory(index_name=index_name, window=window, n_pairs=len(pairs),
                      start=pairs[0][0], end=pairs[-1][0],
                      mean_vrp=st.mean(allv), median_vrp=st.median(allv),
                      positive_share=sum(1 for x in allv if x > 0) / len(allv),
                      year_stats=ys, corr_with_direction=corr, caveats=caveats)


def render_markdown(h: VrpHistory, display_name: str) -> str:
    """终端 Markdown。"""
    L = [f"## {display_name} — 波动率溢价跨周期检验（{h.index_name}）", "",
         f"口径：当日 {h.index_name} 减去**其后 {h.window} 个交易日**的已实现波动（前视对齐）。",
         f"配对样本 {h.n_pairs}（{h.start} → {h.end}），"
         f"**独立样本约 {h.independent_samples} 个**。", "",
         f"- 全样本均值 **{h.mean_vrp:+.2f}pp**　中位 {h.median_vrp:+.2f}pp　"
         f"为正占比 {h.positive_share * 100:.1f}%",
         f"- 与标的年度涨跌相关性 **{h.corr_with_direction:+.2f}**"
         if h.corr_with_direction is not None else "- 相关性样本不足",
         "", "| 年份 | 样本 | 均值VRP | 中位 | >0占比 | 标的年涨跌 | |",
         "|---:|---:|---:|---:|---:|---:|:--|"]
    for y in h.year_stats:
        L.append(f"| {y.year} | {y.n} | **{y.mean_vrp:+.2f}pp** | {y.median_vrp:+.2f} | "
                 f"{y.positive_share * 100:.1f}% | {y.underlying_return:+.1f}% | {y.verdict} |")
    L += ["", f"**跨周期稳健性判定：{'✅ 稳健' if h.regime_robust else '❌ 不稳健'}**"]
    if h.negative_years:
        L.append(f"　为负的年份：{h.negative_years}")
    L += ["",
          "> 判别法：如果一个「卖方 edge」与标的涨跌高度正相关，那它就是**方向敞口的伪装**，",
          "> 不是策略。VRP 与方向低相关，才是真正跨牛熊的部分。", "",
          "**注意事项**"]
    L += [f"- {c}" for c in h.caveats]
    return "\n".join(L)
