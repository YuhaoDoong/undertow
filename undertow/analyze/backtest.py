"""COT 信号回测 / 事件研究（确定性计算，无 I/O）。

目的：把前面"凭经验"的信号和阈值，用历史价格验证、校准。

方法（务必理解其局限，已在报告中标注）:
  - 逐周重算信号，**只用当周及之前的数据**（无前视偏差）。
  - 入场用"报告发布滞后"：COT 数据截止周二、周五才发布，故入场取
    report_date + release_lag 之后的第一个交易日（默认 +3 日 ≈ 周五）。
  - 前瞻收益：从入场起 5/10/20 个交易日（≈1/2/4 周）的价格代理收益。
  - 每个信号：触发次数、各期限前瞻收益均值/中位、方向命中率，并与
    **无条件基线**（所有周）对比——只有显著优于基线才算有信息量。
  - 另做 Managed Money 净持仓分位的**分桶研究**：看"越拥挤→前瞻收益越低"
    这个反指逻辑是否成立、阈值该设在哪。

局限：样本仅 ~3 年、前瞻窗口重叠（非独立）、价格用 ETF 代理。结论为**指示性**，
不是严谨统计显著性检验。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import timedelta

from undertow.core.models import CotReport, PriceSeries
from undertow.analyze.positioning import analyze
from undertow.analyze.signals import generate_signals, net_bias

# 信号方向 -> 期望价格方向符号
DIR_SIGN = {"bullish": +1, "risk-up": +1, "bearish": -1, "risk-down": -1, "neutral": 0}
DEFAULT_HORIZONS = (5, 10, 20)  # 交易日
DEFAULT_RELEASE_LAG_DAYS = 3
DEFAULT_MIN_LOOKBACK = 52  # 至少 1 年历史才开始评估（分位才有意义）


@dataclass(frozen=True)
class HorizonStat:
    horizon_days: int
    n: int
    mean_ret: float    # 基线=原始前瞻收益；信号=【对齐收益】(顺信号方向，看空时取负)
    median_ret: float
    hit_rate: float | None  # 方向命中率；中性/无方向时 None


@dataclass(frozen=True)
class SignalBacktest:
    code: str
    direction: str  # 固定方向信号显示其方向；逐周变向的显示"可变"
    occurrences: int
    by_horizon: dict[int, HorizonStat]


@dataclass(frozen=True)
class BucketStat:
    label: str
    n: int
    mean_fwd: float
    median_fwd: float


@dataclass(frozen=True)
class BacktestResult:
    instrument: str
    price_symbol: str
    price_quality: str
    n_events: int
    date_from: str
    date_to: str
    horizons: tuple[int, ...]
    primary_horizon: int
    baseline: dict[int, HorizonStat]
    signals: list[SignalBacktest]
    mm_percentile_buckets: list[BucketStat]  # 按 MM 净分位分桶的前瞻收益
    bias_buckets: list[BucketStat]           # 按综合 bias(偏多/中性/偏空) 分桶


@dataclass
class _Event:
    date: object
    codes_dirs: list[tuple[str, str]]
    rets: dict[int, float]
    mm_pct: float | None
    bias: str


def _raw_hstat(h: int, rets: list[float]) -> HorizonStat:
    """无方向的原始前瞻收益统计（用于基线）。"""
    if not rets:
        return HorizonStat(h, 0, float("nan"), float("nan"), None)
    return HorizonStat(h, len(rets), statistics.fmean(rets), statistics.median(rets), None)


def _signal_hstat(h: int, pairs: list[tuple[float, int]]) -> HorizonStat:
    """方向对齐统计。pairs=[(前瞻收益, 该次方向符号)]。

    对齐收益 = 收益 × 方向符号（看空信号价格跌则为正），即"顺信号方向交易"的收益。
    命中 = 价格方向与信号方向一致。这样逐周变向的信号也能正确评估。
    """
    valid = [(r, s) for r, s in pairs if s != 0]
    if not valid:
        return HorizonStat(h, 0, float("nan"), float("nan"), None)
    aligned = [r * s for r, s in valid]
    hits = sum(1 for r, s in valid if (r > 0) == (s > 0) and r != 0) / len(valid)
    return HorizonStat(h, len(valid), statistics.fmean(aligned), statistics.median(aligned), hits)


def run_backtest(
    history: list[CotReport],
    price: PriceSeries,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    release_lag_days: int = DEFAULT_RELEASE_LAG_DAYS,
    min_lookback: int = DEFAULT_MIN_LOOKBACK,
) -> BacktestResult:
    primary = horizons[-1]
    events: list[_Event] = []

    for i in range(min_lookback, len(history)):
        sub = history[: i + 1]
        an = analyze(sub)
        sigs = generate_signals(an)
        d = history[i].report_date

        entry_idx = price.index_on_or_after(d + timedelta(days=release_lag_days))
        if entry_idx is None:
            continue
        rets: dict[int, float] = {}
        for h in horizons:
            r = price.forward_return(entry_idx, h)
            if r is not None:
                rets[h] = r
        if not rets:
            continue

        events.append(_Event(
            date=d,
            codes_dirs=[(s.code, s.direction) for s in sigs],
            rets=rets,
            mm_pct=an.categories["managed_money"].net_percentile,
            bias=net_bias(sigs),
        ))

    # —— 无条件基线 ——
    baseline = {h: _raw_hstat(h, [e.rets[h] for e in events if h in e.rets]) for h in horizons}

    # —— 每个信号（按出现的所有方向；逐周变向用各次自身方向对齐）——
    code_dirs: dict[str, set[str]] = {}
    for e in events:
        for code, direction in e.codes_dirs:
            code_dirs.setdefault(code, set()).add(direction)

    signals: list[SignalBacktest] = []
    for code in sorted(code_dirs):
        dirs = code_dirs[code]
        disp_dir = next(iter(dirs)) if len(dirs) == 1 else "可变"
        # 收集该信号每次出现的 (收益, 方向符号)
        n_occ = 0
        by_h: dict[int, HorizonStat] = {}
        per_h_pairs: dict[int, list[tuple[float, int]]] = {h: [] for h in horizons}
        for e in events:
            d = next((dd for cc, dd in e.codes_dirs if cc == code), None)
            if d is None:
                continue
            n_occ += 1
            sign = DIR_SIGN.get(d, 0)
            for h in horizons:
                if h in e.rets:
                    per_h_pairs[h].append((e.rets[h], sign))
        for h in horizons:
            by_h[h] = _signal_hstat(h, per_h_pairs[h])
        signals.append(SignalBacktest(code, disp_dir, n_occ, by_h))

    # —— MM 净分位分桶（前瞻=primary 期限）——
    mm_pts = [(e.mm_pct, e.rets[primary]) for e in events if e.mm_pct is not None and primary in e.rets]
    mm_buckets = _quantile_buckets(mm_pts, edges=[0, 20, 40, 60, 80, 100],
                                   labelfmt="净分位 {lo:.0f}-{hi:.0f}%")

    # —— 综合 bias 分桶 ——
    bias_buckets: list[BucketStat] = []
    for label in ("偏多", "中性/分歧", "偏空"):
        rs = [e.rets[primary] for e in events if e.bias == label and primary in e.rets]
        if rs:
            bias_buckets.append(BucketStat(label, len(rs), statistics.fmean(rs), statistics.median(rs)))

    return BacktestResult(
        instrument=history[-1].instrument,
        price_symbol=price.symbol,
        price_quality="",
        n_events=len(events),
        date_from=str(events[0].date) if events else "",
        date_to=str(events[-1].date) if events else "",
        horizons=horizons,
        primary_horizon=primary,
        baseline=baseline,
        signals=signals,
        mm_percentile_buckets=mm_buckets,
        bias_buckets=bias_buckets,
    )


def _quantile_buckets(points: list[tuple[float, float]], edges: list[float], labelfmt: str) -> list[BucketStat]:
    """按第一维(0~100 的分位值)落入 edges 区间分桶，统计第二维(收益)。"""
    out: list[BucketStat] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        # 末桶闭区间含 100
        rs = [r for v, r in points if (lo <= v < hi) or (hi == edges[-1] and v == hi)]
        label = labelfmt.format(lo=lo, hi=hi)
        if rs:
            out.append(BucketStat(label, len(rs), statistics.fmean(rs), statistics.median(rs)))
        else:
            out.append(BucketStat(label, 0, float("nan"), float("nan")))
    return out
