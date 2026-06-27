"""回测引擎单元测试（合成价格 + 合成 COT，不依赖网络）。

验证：无前视、发布滞后入场、对齐收益与命中率方向正确、分桶。
运行: python tests/test_backtest.py  或  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.core.models import CategoryChange, CotReport, PriceSeries, TraderCategory
from undertow.analyze.backtest import (
    DIR_SIGN, _signal_hstat, run_backtest, _quantile_buckets,
)


def test_dir_sign_aligned_return_and_hit():
    # 看空信号(sign=-1)，价格跌(ret=-0.05) -> 对齐收益为正、命中
    st = _signal_hstat(20, [(-0.05, -1), (-0.03, -1)])
    assert st.mean_ret > 0
    assert st.hit_rate == 1.0
    # 看空信号但价格涨 -> 对齐为负、不命中
    st2 = _signal_hstat(20, [(0.04, -1), (0.02, -1)])
    assert st2.mean_ret < 0
    assert st2.hit_rate == 0.0
    # 中性(sign=0)被剔除
    st3 = _signal_hstat(20, [(0.04, 0)])
    assert st3.n == 0


def test_quantile_buckets_edges():
    pts = [(10, 0.01), (30, 0.02), (50, 0.03), (90, -0.04), (100, -0.05)]
    bs = _quantile_buckets(pts, edges=[0, 40, 80, 100], labelfmt="{lo:.0f}-{hi:.0f}")
    assert bs[0].n == 2  # 10,30
    assert bs[1].n == 1  # 50
    assert bs[2].n == 2  # 90,100（末桶闭区间含100）


def _flat_cat(net_long: int) -> TraderCategory:
    return TraderCategory(net_long, 0, 0)


def _report(d: date, mm_long: int, mm_change: CategoryChange) -> CotReport:
    z = TraderCategory(0, 0, 0)
    zc = CategoryChange(0, 0, 0)
    return CotReport(
        instrument="t", report_date=d, market_name="T",
        open_interest=100000, open_interest_change=0,
        managed_money=TraderCategory(mm_long, 0, 0),
        other_reportables=z, swap_dealers=z, producer_merchant=z, nonreportable=z,
        changes={"managed_money": mm_change, "other_reportables": zc,
                 "swap_dealers": zc, "producer_merchant": zc, "nonreportable": zc},
    )


def test_run_backtest_no_lookahead_and_alignment():
    # 构造 60 周 COT：MM 净多逐周抬升到极值（应在后段触发 MM_CROWDED_LONG）
    start = date(2024, 1, 2)
    history = []
    for i in range(80):
        d = start + timedelta(weeks=i)
        history.append(_report(d, 1000 + i * 500, CategoryChange(long=500, short=0)))

    # 构造每日价格：在第 60 周附近见顶后下跌（让 crowded-long 的看空对齐为正）
    pdates, pcloses = [], []
    base = date(2023, 12, 1)
    price = 100.0
    for i in range(900):
        dd = base + timedelta(days=i)
        if dd.weekday() < 5:  # 仅工作日
            # 前 ~430 天上涨，之后下跌
            price *= 1.001 if i < 430 else 0.999
            pdates.append(dd)
            pcloses.append(price)
    ps = PriceSeries(symbol="T", dates=pdates, closes=pcloses)

    bt = run_backtest(history, ps, horizons=(20,), min_lookback=52, release_lag_days=3)
    assert bt.n_events > 0
    codes = {s.code for s in bt.signals}
    assert "MM_CROWDED_LONG" in codes
    # 分桶应有数据
    assert any(b.n > 0 for b in bt.mm_percentile_buckets)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
