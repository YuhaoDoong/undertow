"""分析层数学的单元测试（纯函数，不依赖网络）。

构造若干合成 CotReport，验证净变化分解与历史分位/z-score 计算正确。
运行: python -m pytest tests/ -q   或   python tests/test_positioning.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.core.models import CategoryChange, CotReport, TraderCategory
from undertow.analyze.positioning import analyze
from undertow.analyze.signals import generate_signals


def _mk(d: date, mm_long: int, mm_short: int, ch: CategoryChange | None = None) -> CotReport:
    flat = TraderCategory(0, 0, 0)
    return CotReport(
        instrument="test",
        report_date=d,
        market_name="TEST",
        open_interest=100_000,
        open_interest_change=0,
        managed_money=TraderCategory(mm_long, mm_short, 0),
        other_reportables=flat,
        swap_dealers=flat,
        producer_merchant=flat,
        nonreportable=flat,
        changes={"managed_money": ch or CategoryChange(0, 0, 0),
                 "other_reportables": CategoryChange(0, 0, 0),
                 "swap_dealers": CategoryChange(0, 0, 0),
                 "producer_merchant": CategoryChange(0, 0, 0),
                 "nonreportable": CategoryChange(0, 0, 0)},
    )


def test_change_decomposition_short_cover_vs_active():
    # 净多 +5000，全部来自空头回补 -> driver 应为"空头回补"，持续性弱
    h = [_mk(date(2024, 1, 2), 10_000, 8_000),
         _mk(date(2024, 1, 9), 10_000, 3_000, CategoryChange(long=0, short=-5_000))]
    an = analyze(h)
    d = an.categories["managed_money"].decomposition
    assert d.net_change == 5_000
    assert d.driver == "空头回补"
    assert d.conviction == "弱"

    # 净多 +5000，全部来自主动加多 -> "主动加多"，持续性强
    h2 = [_mk(date(2024, 1, 2), 10_000, 8_000),
          _mk(date(2024, 1, 9), 15_000, 8_000, CategoryChange(long=5_000, short=0))]
    an2 = analyze(h2)
    d2 = an2.categories["managed_money"].decomposition
    assert d2.driver == "主动加多"
    assert d2.conviction == "强"


def test_percentile_and_zscore_extremes():
    # 构造递增净多序列，最后一期为历史最高 -> 分位应为 100%
    hist = []
    base = date(2024, 1, 7)
    for i in range(20):
        d = date(base.year, base.month, base.day)
        hist.append(_mk(d, 10_000 + i * 1_000, 5_000))
        # 用周序仅作占位，日期需唯一
        base = date(2024, 1 + (i // 4), 1 + (i % 4) * 7 + 1)
    an = analyze(hist)
    mm = an.categories["managed_money"]
    assert mm.net_percentile == 100.0
    assert mm.net_zscore > 1.0


def test_crowded_long_signal_fires():
    # 历史净多一路走高至极值 -> 应触发投机资金多头拥挤(反指回调风险)
    hist = []
    for i in range(40):
        # 唯一日期
        d = date(2023, 1 + (i // 4) % 12 + 1, 1 + (i % 4) * 7 + 1)
        hist.append(_mk(d, 5_000 + i * 2_000, 3_000))
    an = analyze(hist)
    sigs = generate_signals(an)
    codes = {s.code for s in sigs}
    assert "MM_CROWDED_LONG" in codes


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
