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

from undertow.analyze.livecheck import (LegQuote, build_ledger, check_position,
                                        render_ledger_md, render_md)


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


# ── 品种累计台账 ──────────────────────────────────────────────────

def _slv_flows():
    """2026-08-26 SLV 70C/73C 的真实流水（含 8/20-8/21 那轮盈利的往返）。"""
    return [
        {"symbol": "SLV260918C70000.US", "balance": "-336.00"},   # 8/20 买
        {"symbol": "SLV260918C70000.US", "balance": "-2.41"},
        {"symbol": "SLV260918C70000.US", "balance": "+384.00"},   # 8/21 卖 → 第一轮赚
        {"symbol": "SLV260918C70000.US", "balance": "-2.43"},
        {"symbol": "SLV260918C70000.US", "balance": "-105.00"},   # 8/24 买 3 张
        {"symbol": "SLV260918C70000.US", "balance": "-105.00"},
        {"symbol": "SLV260918C70000.US", "balance": "-105.00"},
        {"symbol": "SLV260918C70000.US", "balance": "-2.42"},
        {"symbol": "SLV260918C70000.US", "balance": "+126.00"},   # 8/25 卖 2 张 → 亏损已实现
        {"symbol": "SLV260918C70000.US", "balance": "-1.61"},
        {"symbol": "SLV260918C73000.US", "balance": "+36.00"},    # 8/26 卖保护腿
    ]


def test_ledger_matches_hand_computed_total():
    """台账必须复现手工核对的 -97.47 —— 这是当日的锚定值。"""
    g = build_ledger("SLV", _slv_flows(), closeable=18.0, exit_fee=1.60)
    assert abs(g.realized - (-113.87)) < 1e-9, g.realized
    assert abs(g.total - (-97.47)) < 1e-9, g.total
    print("PASS test_ledger_matches_hand_computed_total")


def test_ledger_ignores_broker_cost_basis():
    """台账只吃现金流水，不接受任何「成本价」输入——券商的 1.89 是摊销产物。

    实付每张 1.05，券商显示 1.89 = (3×1.05 − 2×0.63)/1，是把已实现亏损摊进了
    剩余持仓。用它判断亏损会同时错两次：既非实付价，也不含更早那轮的 +43.16。
    """
    import inspect
    src = inspect.getsource(build_ledger)
    assert "cost" not in src.lower(), "build_ledger 不应接触成本价"
    print("PASS test_ledger_ignores_broker_cost_basis")


def test_ledger_bad_rows_are_skipped_not_crashed():
    rows = [{"symbol": "X", "balance": "10.0"}, {"symbol": "X", "balance": None},
            {"symbol": "X"}, {"symbol": "X", "balance": "abc"}]
    g = build_ledger("X", rows, closeable=0.0)
    assert abs(g.realized - 10.0) < 1e-9
    print("PASS test_ledger_bad_rows_are_skipped_not_crashed")


def test_ledger_render_warns_about_cost_basis():
    md = render_ledger_md([build_ledger("SLV", _slv_flows(), 18.0, 1.60)])
    assert "不可用来判断亏了多少" in md
    assert "沉没成本" in md
    assert render_ledger_md([]) == ""
    print("PASS test_ledger_render_warns_about_cost_basis")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
