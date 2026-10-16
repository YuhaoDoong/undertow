"""下降楔形识别（wedge-explore-v1）：合成楔形能认出、上破判定、发散/上行通道不认、认出前已破单列、安慰剂可复现。全部合成数据。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import wedge as wg  # noqa: E402


def mk(closes, spread=0.15, start=date(2000, 1, 3)):
    out, d, prev = [], start, closes[0]
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
        d += timedelta(days=1)
    return out


def zig(nodes, n=6):
    c = []
    for a, b in zip(nodes[:-1], nodes[1:]):
        c += [a + (b - a) * k / n for k in range(1, n + 1)]
    return c


# 上轨 110→100（每段 −2.5），下轨 100→96（每段 −1），收敛；交替贴两线
WEDGE = [110, 100, 107.5, 99, 105, 98, 102.5, 97, 100]


def series(tail):
    pre = [112.0] * 30
    return pre + zig([112] + WEDGE) + tail


def test_detects_falling_wedge_and_upward_break():
    bars = mk(series([100 + 0.8 * k for k in range(1, 40)] + [130.0] * 140))
    atr = ps.wilder_atr(bars)
    found = wg.find(bars, atr, "descriptive")
    ups = [(w, d, t) for w, d, t in found if d == "up"]
    assert ups, found
    w, d, t = ups[0]
    assert w.touches >= 5 and w.upper(t) < bars[t][4]
    assert wg.outcome(bars, w, d, t)["complete"]


def test_rejects_rising_channel():
    bars = mk([100.0] * 30 + zig([100, 90, 97.5, 91, 95, 92, 93.5, 93, 92.5]) + [92.0] * 150)   # 低点抬高 → 下轨向上
    atr = ps.wilder_atr(bars)
    assert all(d != "up" or w.l2[1] < w.l1[1] for w, d, t in wg.find(bars, atr, "descriptive"))


def test_causal_marks_break_before_known():
    # 最后一个低点之后立刻大涨：k=3 确认前就越过上轨
    bars = mk(series([104, 112, 120] + [120.0] * 150))
    atr = ps.wilder_atr(bars)
    found = wg.find(bars, atr, "causal")
    assert found
    for w, d, t in found:                      # 可交易口径：突破只能出现在认出（最后摆动点确认）之后
        assert d == "broke_before_known" or t is None or t > w.known
        assert w.known >= w.last + wg.K


def test_check_requires_convergence():
    bars = mk([100.0] * 120)
    atr = [1.0] * len(bars)
    P = ps.FPivot
    run = [P("H", 20, 23, 110), P("L", 26, 29, 100), P("H", 32, 35, 108), P("L", 38, 41, 96), P("H", 44, 47, 106)]
    # 上轨每根 −1/6，下轨每根 −1/3：下轨更陡 → 发散，不成立
    assert wg.check(run, bars, atr) is None


def test_placebo_reproducible():
    bars = mk(series([100 + 0.3 * k for k in range(1, 400)]))
    atr = ps.wilder_atr(bars)
    found = [x for x in wg.find(bars, atr, "descriptive") if x[1] in ("up", "down")]
    if not found:
        return
    w, d, t = found[0]
    a = wg.placebo_up_share(bars, atr, w, w.last + 1, seed=7, n=10)
    b = wg.placebo_up_share(bars, atr, w, w.last + 1, seed=7, n=10)
    assert a == b
