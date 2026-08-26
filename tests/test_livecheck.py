"""持仓实时体检的确定性测试。

核心事实（2026-08-26 TQQQ 76/80 实测，同一时刻三个口径）：
    App(last)   1.68-0.68 = $100  →  +$10  ✅ 看着在赚
    中价        1.52-0.67 = $ 85  →  -$5
    真实可平仓   1.37-0.70 = $ 67  →  -$23  ❌ 其实在亏
差 $33。止损判定若看 App，会系统性晚动手——本模块就是为了消除这个偏差。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.livecheck import LegQuote, check_position, render_md


def _tqqq():
    return [LegQuote("TQQQ76C", 1, bid=1.37, ask=1.67, last=1.68),
            LegQuote("TQQQ80C", -1, bid=0.64, ask=0.70, last=0.68)]


def test_exit_value_uses_bid_for_long_ask_for_short():
    """多头按 bid 卖、空头按 ask 买回——这是唯一诚实的出场口径。"""
    c = check_position("spread", _tqqq(), cost=90.0)
    assert abs(c.exit_value - (1.37 - 0.70) * 100) < 1e-6, c.exit_value
    assert abs(c.last_value - (1.68 - 0.68) * 100) < 1e-6
    assert abs(c.mid_value - (1.52 - 0.67) * 100) < 1e-6
    print("PASS test_exit_value_uses_bid_for_long_ask_for_short")


def test_app_optimism_is_flagged():
    """App 口径高出真实可平仓 15% 以上必须告警——这正是当日 $33 的缺口。"""
    c = check_position("spread", _tqqq(), cost=90.0)
    assert c.pnl_exit < 0 < c.pnl_last, (c.pnl_exit, c.pnl_last)   # 一个亏一个赚
    assert any("App" in w for w in c.warnings), c.warnings
    assert abs(c.gap - 33.0) < 1e-6, c.gap
    print("PASS test_app_optimism_is_flagged")


def test_missing_side_returns_none_not_guess():
    """单边空档时不许拿 last 顶替——算不出就说算不出。"""
    legs = [LegQuote("A", 1, bid=None, ask=1.67, last=1.68),
            LegQuote("B", -1, bid=0.64, ask=0.70, last=0.68)]
    c = check_position("x", legs, cost=90.0)
    assert c.exit_value is None and c.mid_value is None
    assert c.last_value is not None            # last 口径仍可算，但只作参考
    assert any("单边缺失" in w for w in c.warnings)
    print("PASS test_missing_side_returns_none_not_guess")


def test_stop_and_target_flags():
    c = check_position("spread", _tqqq(), cost=90.0, stop=80.0)
    assert any("已触及止损" in w for w in c.warnings), c.warnings
    c2 = check_position("spread", _tqqq(), cost=90.0, stop=45.0)
    assert not any("已触及止损" in w for w in c2.warnings)
    assert c2.to_stop_pct > 0
    c3 = check_position("spread", _tqqq(), cost=90.0, target=60.0)
    assert any("已达止盈" in w for w in c3.warnings)
    print("PASS test_stop_and_target_flags")


def test_short_only_position():
    """纯空头：平仓要按 ask 买回，价值为负。"""
    c = check_position("naked", [LegQuote("S", -1, bid=0.64, ask=0.70, last=0.68)], cost=-68.0)
    assert abs(c.exit_value - (-70.0)) < 1e-6
    print("PASS test_short_only_position")


def test_render_contains_exit_basis_warning():
    md = render_md([check_position("spread", _tqqq(), cost=90.0, stop=45.0)], net_assets=436.77)
    assert "真实可平仓" in md and "止损判定用本表" in md
    assert "总敞口" in md
    print("PASS test_render_contains_exit_basis_warning")


def test_empty_legs():
    c = check_position("x", [])
    assert not c.ok
    print("PASS test_empty_legs")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
