"""structure-v0：普通结构突破 + 双顶标签（合成数据）。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import structure_patterns as sp  # noqa: E402


def mk_hl(path_closes, wick=0.3, start=date(2001, 1, 1)):
    out, d, prev = [], start, path_closes[0]
    for c in path_closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, prev, max(prev, c) + wick, min(prev, c) - wick, c))
        prev = c; d += timedelta(days=1)
    return out


def legs(*nodes, n=5, flat=20):
    c = [nodes[0]] * flat
    for a, b in zip(nodes[:-1], nodes[1:]):
        c += [a + (b - a) * k / n for k in range(1, n + 1)]
    return c


def test_double_top_break_is_subset_of_ordinary_break_and_prefix_safe():
    # 上升背景 → 两个近似等高的顶 → 跌破中间低点
    closes = legs(80, 90, 84, 95, 89, 101, 94, 101.2, 92, 85, n=5)
    bars = mk_hl(closes)
    ev = sp.breaks(bars)
    bear = [e for e in ev if e["direction"] == "bearish"]
    dt = [e for e in bear if e["double"] and e["double"]["is_pattern"]]
    assert dt, bear
    e = dt[-1]
    assert bars[e["t"]][4] < e["level"] and e["level_conf"] <= e["t"] and e["last_conf"] <= e["t"]
    # 前缀不变性：截到 t+1 根得到同一个事件
    ev2 = sp.breaks(bars[:e["t"] + 1])
    assert any(x["t"] == e["t"] and x["level_idx"] == e["level_idx"] for x in ev2)


def test_each_level_breaks_once_and_outcome_window():
    closes = legs(80, 90, 84, 95, 89, 101, 94, 101.2, 92, 85, 80, 70, n=5)
    bars = mk_hl(closes)
    ev = sp.breaks(bars)
    keys = [(e["direction"], e["level_idx"]) for e in ev]
    assert len(keys) == len(set(keys))
    e = ev[-1]
    atr = sp.ps.wilder_atr(bars)
    o = sp.outcome(bars, atr, e["t"], e["direction"])
    assert o is None or set(o["survive"]) == {"2", "1", "3"}
    assert sp.outcome(bars, atr, len(bars) - 3, "bearish") is None          # 窗口不足
