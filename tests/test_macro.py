"""宏观层单元测试（合成 FRED 序列，纯计算）。

验证：实际利率↓/美元↓/通胀↑ → 利多金；方向阈值；energy 只用美元。
运行: python tests/test_macro.py  或  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading_intel.analysis.macro import analyze_macro, series_ids_for


def _series(start_val, end_val, n=25):
    base = date(2026, 1, 1)
    # 前 n-21 天持平在 start，最后 20 天线性到 end（保证 20 日变化 = end-start）
    vals = [start_val] * (n - 21) + [start_val + (end_val - start_val) * i / 20 for i in range(21)]
    return [(base + timedelta(days=i), v) for i, v in enumerate(vals)]


def test_metal_all_bullish():
    smap = {
        "DFII10": _series(2.50, 2.00),      # 实际利率↓0.5pp → 利多
        "DTWEXBGS": _series(105.0, 103.0),  # 美元↓~1.9% → 利多
        "T10YIE": _series(2.00, 2.12),      # 通胀预期↑0.12pp → 利多
    }
    ma = analyze_macro(smap, asset_class="metal")
    assert ma.macro_bias == "偏多"
    assert ma.macro_score > 0
    assert all(d.vote_sign == 1 for d in ma.drivers)


def test_metal_bearish_real_yield_and_dollar():
    smap = {
        "DFII10": _series(2.00, 2.40),      # 实际利率↑ → 利空
        "DTWEXBGS": _series(100.0, 102.0),  # 美元↑ → 利空
        "T10YIE": _series(2.10, 2.10),      # 通胀预期持平 → 中性(0)
    }
    ma = analyze_macro(smap, asset_class="metal")
    assert ma.macro_bias == "偏空"
    ry = next(d for d in ma.drivers if d.key == "real_yield")
    assert ry.vote_sign == -1
    be = next(d for d in ma.drivers if d.key == "breakeven")
    assert be.vote_sign == 0  # 持平不投票


def test_below_threshold_is_neutral():
    smap = {"DFII10": _series(2.00, 2.02),  # +0.02pp < 0.05 阈值 → 中性
            "DTWEXBGS": _series(100.0, 100.1),  # +0.1% < 0.4% → 中性
            "T10YIE": _series(2.00, 2.00)}
    ma = analyze_macro(smap, asset_class="metal")
    assert ma.macro_bias == "中性"
    assert all(d.vote_sign == 0 for d in ma.drivers)


def test_energy_only_dollar():
    assert series_ids_for("energy") == ["DTWEXBGS"]
    smap = {"DTWEXBGS": _series(105.0, 103.0)}  # 美元↓ → 利多油(弱)
    ma = analyze_macro(smap, asset_class="energy")
    assert len(ma.drivers) == 1 and ma.drivers[0].key == "dollar"
    assert ma.drivers[0].vote_sign == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
