"""滚动收租硬闸门 —— 回归测试。

锚点是这个模块存在的理由：外部 SOP 里「别把虚值压没」是一句提示，拦不住任何
一次具体滚动。本项目的墙位卖方价差正是死于同一形态（胜率很高、单次亏损吃掉
几十次盈利）。所以这里每条闸门都必须真的能拦下，且**永远不建议再滚一轮**。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze import roll_guard as rg   # noqa: E402

D0 = date(2026, 6, 1)


def _rounds(n, *, strike0=110.0, drift=0.0, credit=0.5, delta=0.20, kind="C"):
    """造 n 轮滚动；drift = 每轮行权价挪动量（跟着标的走）。"""
    return [rg.RollRound(on=D0 + timedelta(days=30 * i),
                         strike=strike0 + drift * i, spot=100.0,
                         credit=credit, kind=kind, delta=delta)
            for i in range(n)]


def test_empty_history_is_not_an_error():
    v = rg.assess([], spot=100.0)
    assert v.action == "继续" and v.rounds == 0
    print("PASS test_empty_history_is_not_an_error")


def test_thin_cushion_hard_stops():
    """垫子被压薄必须【停止】，不是提示 —— 这正是原 SOP 拦不住的那一步。"""
    v = rg.assess(_rounds(3, strike0=101.0), spot=100.0)   # 垫子 1%
    assert v.action == "停止", v.headline
    assert any("垫子" in s and "赌方向" in s for s in v.stops), v.stops
    assert v.cushion_pct < rg.CUSHION_STOP_PCT

    warn = rg.assess(_rounds(3, strike0=102.5), spot=100.0)  # 2.5% → 预警区
    assert warn.action == "预警", warn.headline
    print("PASS test_thin_cushion_hard_stops")


def test_delta_gate_is_independent_of_distance():
    """|Δ| 比距离更可靠：它含了波动率与剩余时间。

    垫子够厚但 Δ 已经很高（高波动/临近到期）时，同样要拦。
    """
    v = rg.assess(_rounds(2, strike0=115.0, delta=0.40), spot=100.0)
    assert v.cushion_pct > rg.CUSHION_WARN_PCT, "这一例垫子是够的"
    assert v.action == "停止", "但 Δ 已经越界，必须停"
    assert any("|Δ|" in s for s in v.stops)
    print("PASS test_delta_gate_is_independent_of_distance")


def test_round_count_forces_reset():
    """滚太多轮要求重置 —— 每轮都锚在上一轮结果上，路径依赖会累积。"""
    assert rg.assess(_rounds(6), spot=100.0).action == "预警"
    v = rg.assess(_rounds(10), spot=100.0)
    assert v.action == "停止"
    assert any("重置" in s for s in v.stops), v.stops
    print("PASS test_round_count_forces_reset")


def test_rounds_to_recover_quantifies_picking_up_pennies():
    """本模块最该看的一个数：要再滚多少轮才赚得回一次被打穿。

    每轮收 0.5、被打穿亏 10 → 需要 20 轮。这把"捡钢镚"从比喻变成整数。
    """
    v = rg.assess(_rounds(4, credit=0.5), spot=100.0, breach_loss=10.0)
    assert abs(v.rounds_to_recover - 20.0) < 1e-6, v.rounds_to_recover
    assert v.max_loss_usd == 1000.0
    assert "赚得回一次被打穿" in v.headline
    # 累计 4×0.5=2.0，只有一次亏损 10.0 的 20% → 该出声
    assert any("累计收租" in r for r in v.reasons), v.reasons
    print("PASS test_rounds_to_recover_quantifies_picking_up_pennies")


def test_breach_loss_is_not_assumed():
    """裸卖时亏损理论无上限，模块不替使用者假设 —— 不给就不算这个数。"""
    v = rg.assess(_rounds(3), spot=100.0)
    assert v.rounds_to_recover is None and v.max_loss_usd is None
    assert "赚得回" not in v.headline
    print("PASS test_breach_loss_is_not_assumed")


def test_put_side_cushion_is_measured_downward():
    """卖 put 时垫子在下方 —— 符号搞反会把最危险的一轮判成最安全的。"""
    safe = rg.assess([rg.RollRound(D0, strike=90.0, spot=100.0, credit=0.5,
                                   kind="P", delta=0.20)], spot=100.0)
    assert safe.cushion_pct > 0 and safe.action == "继续", safe.headline
    danger = rg.assess([rg.RollRound(D0, strike=99.5, spot=100.0, credit=0.5,
                                     kind="P", delta=0.20)], spot=100.0)
    assert danger.action == "停止", danger.headline
    print("PASS test_put_side_cushion_is_measured_downward")


def test_strike_drift_is_reported_as_cost_not_achievement():
    """行权价跟着标的挪 = 拿垫子换租金，必须这样叙述，不能说成"跟上了行情"。"""
    v = rg.assess(_rounds(4, strike0=110.0, drift=2.0), spot=100.0)
    assert v.strike_drift_pct is not None and v.strike_drift_pct > 0
    assert any("拿垫子换租金" in r for r in v.reasons), v.reasons
    print("PASS test_strike_drift_is_reported_as_cost_not_achievement")


def test_never_recommends_rolling_again():
    """风险约束模块的底线：任何输出都不得出现"再滚一轮"式的鼓励。"""
    for v in (rg.assess(_rounds(2), spot=100.0),
              rg.assess(_rounds(10, strike0=101.0), spot=100.0, breach_loss=10.0)):
        md = rg.render_md(v)
        assert "从不建议再滚一轮" in md
        for bad in ("建议继续滚", "可以再滚", "值得再滚"):
            assert bad not in md
    print("PASS test_never_recommends_rolling_again")


def test_stop_outranks_warn():
    """同时触发多条时，停止必须压过预警 —— 不能被"还能滚"的措辞盖过去。"""
    v = rg.assess(_rounds(8, strike0=101.0, delta=0.40), spot=100.0)
    assert v.action == "停止"
    assert v.headline.startswith("⛔"), v.headline
    print("PASS test_stop_outranks_warn")
