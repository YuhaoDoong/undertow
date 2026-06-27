"""持仓分析（纯确定性计算，无 I/O）。

核心产出 PositioningAnalysis：对每个交易者类别给出
  - 当前净头寸、占 OI 比例
  - 周环比净变化，及其"来源分解"（加多 / 空头回补 / 主动加空 / 多头了结）
  - 当前净头寸在历史 lookback 窗口内的分位数与 z-score（拥挤度）
这些都是文章里反复用到的判断要素，但全部用历史数据客观量化。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

from undertow.core.models import CategoryChange, CotReport, TraderCategory


@dataclass(frozen=True)
class ChangeDecomposition:
    """周环比净变化的来源分解。

    净变化 = Δlong - Δshort。同样的"净多增加"，可能来自主动加多（持续性强），
    也可能来自空头回补（持续性差）——文章特别强调要区分这一点。
    """

    net_change: int
    long_change: int
    short_change: int
    spread_change: int

    @property
    def driver(self) -> str:
        dl, ds = self.long_change, self.short_change
        if self.net_change == 0:
            return "净持平"
        if self.net_change > 0:
            # 净多增加：主导来自加多还是空头回补？
            return "主动加多" if dl >= abs(ds) and dl > 0 else "空头回补"
        else:
            # 净空增加：主导来自加空还是多头了结？
            return "主动加空" if ds >= abs(dl) and ds > 0 else "多头了结"

    @property
    def conviction(self) -> str:
        """持续性强弱的粗判：主动建仓 > 被动平仓。"""
        return "强" if self.driver in ("主动加多", "主动加空") else "弱"


@dataclass(frozen=True)
class CategoryStats:
    name: str
    net: int
    gross: int
    long: int
    short: int
    spread: int
    net_pct_of_oi: float  # 净头寸 / 总持仓
    decomposition: ChangeDecomposition
    net_percentile: float | None  # 当前净头寸在历史窗口的分位 (0~100)
    net_zscore: float | None
    net_history_min: int | None
    net_history_max: int | None


@dataclass(frozen=True)
class PositioningAnalysis:
    instrument: str
    market_name: str
    report_date: date
    prev_date: date | None
    open_interest: int
    open_interest_change: int
    lookback_used: int
    categories: dict[str, CategoryStats]


def _decompose(change: CategoryChange) -> ChangeDecomposition:
    return ChangeDecomposition(
        net_change=change.net,
        long_change=change.long,
        short_change=change.short,
        spread_change=change.spread,
    )


def _percentile_rank(value: float, series: list[float]) -> float:
    """value 在 series 中的分位（含等于），0~100。"""
    if not series:
        return float("nan")
    below = sum(1 for x in series if x <= value)
    return 100.0 * below / len(series)


def analyze(history: list[CotReport]) -> PositioningAnalysis:
    """对一组按时间升序的 COT 周报做分析，聚焦最新一期。"""
    if not history:
        raise ValueError("history 为空，无法分析")

    latest = history[-1]
    prev = history[-2] if len(history) >= 2 else None

    # 每个类别的历史净头寸序列（用于分位/z-score）
    net_series: dict[str, list[int]] = {name: [] for name, _ in latest.iter_categories()}
    for rep in history:
        for name, cat in rep.iter_categories():
            net_series[name].append(cat.net)

    oi = latest.open_interest or 1
    categories: dict[str, CategoryStats] = {}
    for name, cat in latest.iter_categories():
        series = net_series[name]
        decomposition = _decompose(latest.changes.get(name, CategoryChange(0, 0, 0)))

        if len(series) >= 2:
            mean = statistics.fmean(series)
            stdev = statistics.pstdev(series)
            z = (cat.net - mean) / stdev if stdev > 0 else 0.0
            pct = _percentile_rank(cat.net, series)
            hmin, hmax = min(series), max(series)
        else:
            z = pct = None
            hmin = hmax = None

        categories[name] = CategoryStats(
            name=name,
            net=cat.net,
            gross=cat.gross,
            long=cat.long,
            short=cat.short,
            spread=cat.spread,
            net_pct_of_oi=100.0 * cat.net / oi,
            decomposition=decomposition,
            net_percentile=pct,
            net_zscore=z,
            net_history_min=hmin,
            net_history_max=hmax,
        )

    return PositioningAnalysis(
        instrument=latest.instrument,
        market_name=latest.market_name,
        report_date=latest.report_date,
        prev_date=prev.report_date if prev else None,
        open_interest=latest.open_interest,
        open_interest_change=latest.open_interest_change,
        lookback_used=len(history),
        categories=categories,
    )
