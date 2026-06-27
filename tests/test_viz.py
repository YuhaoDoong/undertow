"""SVG 可视化单元测试：图元生成、不崩溃、空数据降级。

不校验像素，只确保产出是结构完整的 SVG 且关键图元在。
运行: python tests/test_viz.py  或  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.report import viz


def _well_formed(svg: str) -> bool:
    return svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_price_levels_svg():
    base = date(2026, 1, 1)
    dates = [base + timedelta(days=i) for i in range(60)]
    closes = [100 + i * 0.3 for i in range(60)]
    svg = viz.price_levels_svg(dates, closes,
                               [("call墙", 130.0, viz.C_RES), ("put墙", 95.0, viz.C_SUP)],
                               spot=118.0, title="价格")
    assert _well_formed(svg)
    assert "polyline" in svg          # 有价格折线
    assert "call墙" in svg            # 有关键位标签
    assert "现价" in svg


def test_oi_walls_svg():
    rows = [(95.0, 100, 6000), (100.0, 2000, 2000), (110.0, 5000, 100)]
    svg = viz.oi_walls_svg(rows, spot=100.0, call_wall=110.0, put_wall=95.0, title="墙")
    assert _well_formed(svg)
    assert "<rect" in svg             # 有条形


def test_cot_history_svg():
    base = date(2024, 1, 1)
    dates = [base + timedelta(weeks=i) for i in range(40)]
    nets = [10000 - i * 200 for i in range(40)]
    svg = viz.cot_net_history_svg(dates, nets, percentile=42.0, title="净持仓")
    assert _well_formed(svg)
    assert "polyline" in svg
    assert "42%" in svg               # 分位写进标题


def test_empty_inputs_degrade_gracefully():
    assert _well_formed(viz.price_levels_svg([], [], [], spot=0.0))
    assert _well_formed(viz.oi_walls_svg([], spot=0.0, call_wall=None, put_wall=None))
    assert _well_formed(viz.cot_net_history_svg([], [], percentile=None))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
