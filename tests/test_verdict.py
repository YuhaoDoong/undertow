"""当日决策研判合成的确定性测试（函数式，不依赖 pytest）。

锚定：复现"做空?/现价追?/短线/长线"四问的规则化结论，且不自相矛盾——
尤其逆势微腿（上升趋势里的回调/下降趋势里的反抽）不能被套成"顺腿追"。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.verdict import build_verdict
from undertow.analyze.fibonacci import FibAnalysis, FibLevel
from undertow.analyze.risk_reward import RiskRewardPlan, Setup
from undertow.analyze.flow import StrongSignal


class _O:
    def __init__(self, near, mid, bias):
        self.near_bias, self.mid_bias, self.bias = near, mid, bias


class _FA:
    flow_tilt = ""


def _fib(direction, zone):
    return FibAnalysis(ok=True, direction=direction, swing_low=4020.0, swing_high=4660.0,
                       swing_low_date=date(2026, 8, 19), swing_high_date=date(2026, 8, 21),
                       leg_pct=7.7, spot=4648.0, etf_spot=None, ratio=None,
                       retracements=[FibLevel(0.5, 4494.0, None, "retr", "0.5", True)],
                       extensions=[], current_zone=zone, note="")


def _setup(kind, direction, rr, grade, entry=4648.0, entry_label="现价 4648"):
    return Setup(kind=kind, name=kind, direction=direction, entry=entry, entry_label=entry_label,
                 stop=4000.0, stop_label="", target=4900.0, target_label="", rr=rr, grade=grade, verdict="")


def _plan(direction, chase_rr, chase_grade, pull_rr=None, pull_grade=None):
    setups = [_setup("chase", direction, chase_rr, chase_grade)]
    if pull_rr is not None:
        setups.append(_setup("pullback", direction, pull_rr, pull_grade,
                             entry=4494.0, entry_label="斐波 0.5 回撤 4494"))
    return RiskRewardPlan(ok=True, direction=direction, spot=4648.0, setups=setups)


def test_uptrend_extended_take_profit():
    """上升趋势、腿顶、追多 R:R 差 → 不做空 / 别追 / 短线止盈 / 长线拿住（复刻黄金）。"""
    o = _O("中性", "偏多", "偏多(弱)")
    fib = _fib("up", "现价位于 0.236–摆动高(0) 之间")
    plan = _plan("做多", 0.3, "差", 1.4, "中")
    v = build_verdict(o, _FA(), None, fib, plan)
    assert v.ok
    assert "不做空" in v.headline and "短线止盈" in v.headline, v.headline
    assert "别追" in v.chase_answer and "回调" in v.chase_answer, v.chase_answer
    assert "获利了结" in v.swing_action or "止盈" in v.swing_action, v.swing_action
    assert "底仓" in v.core_action, v.core_action
    print(f"PASS test_uptrend_extended_take_profit → {v.headline}")


def test_no_short_when_trend_intact():
    """趋势未坏时"做空?"必须答"不是做空位置"，绝不建议逆势空。"""
    o = _O("中性", "偏多", "偏多")
    fib = _fib("up", "现价位于 0.382–0.5 之间")
    plan = _plan("做多", 1.5, "中", 2.5, "优")
    v = build_verdict(o, _FA(), None, fib, plan)
    assert "不是做空位置" in v.short_answer, v.short_answer
    print("PASS test_no_short_when_trend_intact")


def test_counter_trend_pullback_is_buy_not_short():
    """下跌微腿 + 中期偏多 = 回调（买点），不能说成"现价做空"（复刻 QQQ 修复）。"""
    o = _O("偏多(弱)", "偏多", "偏多")
    fib = _fib("down", "现价位于 0.382–0.5 之间")
    plan = _plan("做空", 0.6, "差", 1.3, "中")   # 顺下跌腿是做空票，但不该被采信为方向
    v = build_verdict(o, _FA(), None, fib, plan)
    assert "回调" in v.chase_answer and "做多" in v.chase_answer, v.chase_answer
    assert "做空" not in v.chase_answer.replace("别追空", ""), v.chase_answer
    assert "回调买" in v.headline, v.headline
    assert "不做空" in v.headline, v.headline
    print(f"PASS test_counter_trend_pullback_is_buy_not_short → {v.headline}")


def test_strong_bearish_allows_short():
    """近端⚡强看跌 → 做空有支持、短线可跟空。"""
    o = _O("偏空", "偏空", "偏空")
    fib = _fib("down", "现价位于 0.5–0.618 之间")
    plan = _plan("做空", 1.2, "中")
    ss = StrongSignal("看跌", "极强", 5.0, 6.0, 30000, True, ["x"], False, "偏空")
    v = build_verdict(o, _FA(), ss, fib, plan)
    assert "可空" in v.headline or "跟空" in v.swing_action, v.headline
    assert "强看跌" in v.short_answer, v.short_answer
    assert "跟空" in v.swing_action, v.swing_action
    print(f"PASS test_strong_bearish_allows_short → {v.headline}")


def test_weak_short_when_mid_neutral():
    """近端偏空、中期中性 → 轻仓短空（弱势跟随，不是趋势空）。"""
    o = _O("偏空(弱)", "中性", "偏空(弱)")
    fib = _fib("down", "现价位于 0.236–摆动低 之间")
    plan = _plan("做空", 0.8, "差", 1.3, "中")
    v = build_verdict(o, _FA(), None, fib, plan)
    assert "轻仓短空" in v.headline, v.headline
    assert "中期未背书" in v.short_answer or "弱势跟随" in v.short_answer, v.short_answer
    print(f"PASS test_weak_short_when_mid_neutral → {v.headline}")


def test_no_fib_degrades_gracefully():
    """无斐波腿/盈亏比闸门时不崩、给保守措辞。"""
    o = _O("中性", "偏多", "偏多(弱)")
    v = build_verdict(o, _FA(), None, None, None)
    assert v.ok
    assert "观望" in v.chase_answer, v.chase_answer
    print("PASS test_no_fib_degrades_gracefully")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
