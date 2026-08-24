"""斐波那契回撤 + 盈亏比闸门 的确定性测试（函数式，不依赖 pytest）。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.core.models import PriceSeries
from undertow.analyze.fibonacci import build_fibonacci
from undertow.analyze.outlook import KeyLevel
from undertow.analyze.risk_reward import build_risk_reward, RR_MIN


def _series_uptrend() -> PriceSeries:
    """构造一条'先跌到 4020、再涨到 4447、现价回到 4400'的序列（复刻范例）。
    低点在早段(idx5)、高点在后段(idx25) → 最近腿=上涨腿，起点 4020。"""
    lows, highs, closes, dates = [], [], [], []
    d0 = date(2026, 6, 1)
    for i in range(30):
        if i <= 5:
            base = 4120 - (4120 - 4020) * (i / 5)      # 4120 → 4020
        elif i <= 25:
            base = 4020 + (4447 - 4020) * ((i - 5) / 20)  # 4020 → 4447
        else:
            base = 4447 - (4447 - 4400) * ((i - 25) / 4)   # 4447 → 4400（回调）
        closes.append(base)
        highs.append(base + 3)
        lows.append(base - 3)
        dates.append(d0 + timedelta(days=i))
    # 精确钉住极值，避免 ±3 抖动改变 argmin/argmax
    lows[5] = 4020.0
    highs[25] = 4447.0
    return PriceSeries("GC=F", dates, closes, highs, lows)


def test_fib_uptrend_levels():
    s = _series_uptrend()
    fib = build_fibonacci(s, ratio=None, spot=4400.0, lookback=90)
    assert fib.ok, fib.note
    assert fib.direction == "up"
    assert abs(fib.swing_low - 4020.0) < 1e-6, fib.swing_low
    assert abs(fib.swing_high - 4447.0) < 1e-6, fib.swing_high
    # 0.5 回撤 = 4447 - 0.5*427 = 4233.5；0.618 = 4183.1；0.382 = 4283.9
    assert abs(fib.level(0.5) - 4233.5) < 0.1, fib.level(0.5)
    assert abs(fib.level(0.618) - 4183.126) < 0.5, fib.level(0.618)
    assert abs(fib.level(0.382) - 4283.886) < 0.5, fib.level(0.382)
    # 扩展 1.618 = 4447 + 0.618*427 = 4710.9
    assert abs(fib.level(1.618) - 4710.9) < 0.5, fib.level(1.618)
    # 现价 4400 在 0.236(4346.2) 与 摆动高 之间
    assert "0.236" in fib.current_zone or "摆动高" in fib.current_zone, fib.current_zone
    print("PASS test_fib_uptrend_levels")


def test_fib_small_leg_rejected():
    d0 = date(2026, 6, 1)
    dates = [d0 + timedelta(days=i) for i in range(30)]
    closes = [4000 + (i % 3) for i in range(30)]  # 幅度 <2%
    s = PriceSeries("GC=F", dates, closes, [c + 1 for c in closes], [c - 1 for c in closes])
    fib = build_fibonacci(s, spot=4001.0)
    assert not fib.ok, "过小摆动应被拒绝"
    assert "过小" in fib.note
    print("PASS test_fib_small_leg_rejected")


def test_fib_etf_anchor():
    s = _series_uptrend()
    # ratio = 真实期货价/ETF 价 = 4400/407 ≈ 10.81（GLD 口径）
    fib = build_fibonacci(s, ratio=10.81, spot=4400.0)
    assert fib.ok
    lv = next(x for x in fib.retracements if abs(x.ratio - 0.5) < 1e-9)
    assert lv.etf is not None and abs(lv.etf - 4233.5 / 10.81) < 0.1, lv.etf
    print("PASS test_fib_etf_anchor")


def test_rr_pullback_beats_chase():
    """核心：等回调的盈亏比必须高于现价追（'别追、等回调'纪律的定量印证）。"""
    s = _series_uptrend()
    fib = build_fibonacci(s, spot=4400.0)
    # 上方阻力墙 4600（商品口径）
    walls = [KeyLevel("看涨墙 4600", 425.0, 4600.0, "resistance", "")]
    plan = build_risk_reward(fib, o=None, key_levels=walls)
    assert plan.ok, plan.note
    assert plan.direction == "做多"
    chase = next(x for x in plan.setups if x.kind == "chase")
    pull = next(x for x in plan.setups if x.kind == "pullback")
    # 现价追：(4600-4400)/(4400-~4002.4) ≈ 0.50 < 1 → 差
    assert chase.rr < RR_MIN, chase.rr
    assert chase.grade == "差"
    # 等回调 0.5=4233.5：(4600-4233.5)/(4233.5-~4002.4) ≈ 1.59 → 中，且高于现价追
    assert pull.rr > chase.rr, (pull.rr, chase.rr)
    assert pull.entry < chase.entry, "回调入场应低于现价"
    assert pull.stop == chase.stop, "两情景共用起涨点止损"
    print(f"PASS test_rr_pullback_beats_chase (chase={chase.rr}, pull={pull.rr})")


def test_rr_target_falls_back_to_extension():
    """无结构墙位时，目标退回斐波扩展位，仍能算盈亏比。"""
    s = _series_uptrend()
    fib = build_fibonacci(s, spot=4400.0)
    plan = build_risk_reward(fib, o=None, key_levels=[])
    assert plan.ok
    chase = next(x for x in plan.setups if x.kind == "chase")
    # 目标应为扩展 1.272 (=4563.1) 或 1.618；label 含"扩展"
    assert "扩展" in chase.target_label or "摆动高" in chase.target_label, chase.target_label
    assert chase.rr > 0
    print("PASS test_rr_target_falls_back_to_extension")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
