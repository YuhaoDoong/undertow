"""买方价差 100% 含费提醒 v2（用户 2026-10-23；Codex 063-B / 063 T 验收）。全部是合成数据。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from undertow.analyze import risk_watch as rw
from undertow.analyze import spread_econ as se

ET = ZoneInfo("America/New_York")


def R(**kw):
    base = dict(side="P", k_sell=100.0, k_buy=105.0, cost=1.0, qty=1, entry_fees=1.6, exit_fees=1.6, value_exec=None)
    base.update(kw)
    return se.debit_roi_review(**base)


def test_codex_synthetic_boundary_2048():
    """063-B：1 组、付 1.00、入场/平仓费各 1.60 → 实际投入 101.60，触发净价 2.048（可挂 2.05）。边界前/正好/之后。"""
    r = R()
    assert (r["invest_usd"], r["trigger_value"], r["trigger_quote"], r["status"]) == (101.6, 2.048, 2.05, "no_quote")
    assert not R(value_exec=2.04)["triggered"] and R(value_exec=2.04)["status"] == "below"
    on = R(value_exec=2.048)
    assert on["triggered"] and on["profit_usd"] == 101.6 and on["roi"] == 1.0 and on["status"] == "triggered"
    assert R(value_exec=2.06)["triggered"]


def test_old_v1_denominator_differs():
    """旧 v1 把往返费都加进分母（理论最大亏损）→ 2.064；与 v2 的 2.048 是不同规则，不是「略保守」。"""
    e = se.vertical(side="P", k_sell=100, k_buy=105, entry_credit=-1.0, qty=1, fee_round_trip=3.2)
    L = e["entry_net_payoff_max_loss"]
    assert -se.loss_at_value(e, -2.064) == pytest.approx(L) and R()["trigger_value"] == 2.048


def test_multi_group_with_actual_fees():
    """2 组、付 0.73、实际入场费 3.19、预计平仓费 3.20：V* = 1.46 + (6.38 + 3.20)/200 = 1.5079。"""
    r = R(k_sell=50.0, k_buy=52.0, cost=0.73, qty=2, entry_fees=3.19, exit_fees=3.2, entry_fee_source="actual")
    assert r["invest_usd"] == 149.19 and r["trigger_value"] == pytest.approx(1.5079, abs=1e-4) and r["trigger_quote"] == 1.51
    assert not R(k_sell=50.0, k_buy=52.0, cost=0.73, qty=2, entry_fees=3.19, exit_fees=3.2, value_exec=1.50)["triggered"]
    assert R(k_sell=50.0, k_buy=52.0, cost=0.73, qty=2, entry_fees=3.19, exit_fees=3.2, value_exec=1.51)["triggered"]


def test_missing_fees_or_quote_never_triggers_or_folds_zero():
    m = R(entry_fees=None, value_exec=3.0)
    assert m["status"] == "incomplete" and not m["triggered"] and m["invest_usd"] is None and "入场费" in m["why"]
    assert R(exit_fees=float("nan"), value_exec=3.0)["status"] == "incomplete"
    assert R(value_exec=None)["status"] == "no_quote" and not R(value_exec=None)["triggered"]
    assert R(side="P", k_sell=105.0, k_buy=100.0)["status"] == "not_applicable"           # 贷记不适用


def test_expensive_spread_unreachable_but_floor_can_pass():
    """063-B：宽 2、付 1.10、费各 1.60 → 最大可平仓 ROI ≈ 77.78%，100% 不可达（适用性，不换阈值）；10% 收益风险比下限仍可通过。"""
    from undertow.analyze import risk_policy as rp
    r = R(side="C", k_sell=102.0, k_buy=100.0, cost=1.10, value_exec=1.99)
    assert r["unreachable"] and r["max_roi"] == pytest.approx(0.7778, abs=1e-4) and not r["triggered"] and r["status"] == "unreachable"
    e = se.vertical(side="C", k_sell=102, k_buy=100, entry_credit=-1.10, qty=1, fee_round_trip=3.2)
    assert rp.payoff_floor(e["entry_net_max_gain"], e["entry_net_payoff_max_loss"])["status"] == "pass"


def test_scenarios_and_hold_close_breakeven():
    """剩余满额 / 归零回吐（无费、到期口径的幅度）；持有-现平打平价：熊市 put K买 − V，牛市 call K买 + V。"""
    r = R(k_sell=50.0, k_buy=52.0, cost=0.73, qty=2, value_exec=1.41)
    assert (r["remaining_gross_usd"], r["giveback_gross_usd"], r["hold_close_breakeven"]) == (118.0, 282.0, 50.59)
    c = R(side="C", k_sell=165.0, k_buy=160.0, cost=1.28, value_exec=2.24)
    assert c["hold_close_breakeven"] == 162.24


def test_tick_rounding_not_below_threshold():
    r = R(cost=1.02, entry_fees=0.0, exit_fees=0.0)               # V* = 2.04 恰在价位上：不因浮点误差进到 2.05
    assert r["trigger_value"] == 2.04 and r["trigger_quote"] == 2.04


def uso(**kw):
    base = dict(key="live|USO|2026-11-20|C|165/160", scope="live", label="USO 160/165C", underlying="USO",
                expiry="2026-11-20", side="C", k_sell=165.0, k_buy=160.0, entry_credit=-1.20, qty=1,
                fee_round_trip=3.2, value=-1.30, spot=162.0, tdays_to_expiry=11)
    base.update(kw)
    return rw.SpreadView(**base)


def tp(v, plan=None):
    return [i for i in rw.evaluate_spread(v, plan, today="2026-10-15", now_hm="11:00", close_hm="16:00")
            if i.key.startswith("take_profit")]


def test_live_uses_closeable_not_mid():
    """多头 bid 2.40 / 空头 ask 0.00 可平 2.40 < 2.448 → 不报，即使中价已过线（中价只作参考）。"""
    assert tp(uso(value=-2.40)) == []
    assert tp(uso(value=-2.40, value_assumption="buy_bid_missing")) == []


def test_live_actual_entry_fee_from_plan():
    """计划核定实际入场费 1.65 → 分母用实际（不再标估计）；缺省按往返费模型一半并标估计。"""
    i = tp(uso(value=-2.46), {"entry_fees_usd": 1.65})
    r = i[0].numbers["debit_roi_review"]
    assert r["invest_usd"] == 121.65 and r["entry_fee_source"] == "actual" and "估计" not in i[0].title
    j = tp(uso(value=-2.46))
    assert j[0].numbers["debit_roi_review"]["entry_fee_source"] == "model" and "入场费为估计" in j[0].title


def test_live_unit_note_not_presented_as_whole_round():
    """同标的还有其它腿：子结构达标也只说「整轮 ROI 未核实」，不冒充整轮 ROI（063 T3）。"""
    i = tp(uso(value=-2.50, unit_note="USO 同时有 2 组/条在场价差或腿"))
    assert i and "整轮 ROI 未核实" in i[0].title and "ROI 1" not in i[0].title


def test_unit_notes_from_grouping():
    from undertow import riskwatch_cli as rc
    live = [{"root": "SLV"}, {"root": "SLV"}, {"root": "USO"}]
    assert set(rc._unit_notes(live, [])) == {"SLV"}
    assert set(rc._unit_notes([{"root": "USO"}], ["USO261030C170000.US"])) == {"USO"}


def test_paper_and_live_share_formula():
    """同一输入在模拟仓入口与实盘巡检入口结果一致（往返费模型各一半）。"""
    from scripts import paper_trades as pt
    p = {"side": "C", "k_sell": 165.0, "k_buy": 160.0, "entry_credit": -1.20, "qty": 1, "fee_round_trip": 3.2,
         "structure": "debit", "advisory_rule": "debit-roi100-review-v2-20261023"}
    a = pt.advisory_view(p, -2.46, None, datetime(2026, 10, 23, 10, 0, tzinfo=ET))
    r = rw.debit_review(uso(value=-2.46), None)
    assert (a["pnl_usd"], a["risk_cost_usd"], a["triggered"]) == (r["profit_usd"], r["invest_usd"], r["triggered"]) == (122.8, 121.6, True)


def test_paper_v1_history_still_reproducible():
    """已挂 v1 的模拟仓仍按 v1（分母含往返费理论最大亏损）评估，不被 v2 静默改写。"""
    from scripts import paper_trades as pt
    p = {"side": "C", "k_sell": 165.0, "k_buy": 160.0, "entry_credit": -1.20, "qty": 1, "fee_round_trip": 3.2,
         "structure": "debit", "advisory_rule": "debit-roi100-review-v1"}
    a = pt.advisory_view(p, -2.46, None, None)
    assert a["risk_cost_usd"] == 123.2 and a["triggered"] is False                     # v1 边界 2.464


def test_paper_book_cell_v2():
    from scripts import paper_book as pb
    r = {"structure": "debit", "advisory_rule": "debit-roi100-review-v2-20261023", "side": "C", "k_sell": 165.0,
         "k_buy": 160.0, "credit": -1.20, "qty": 1, "fee_round_trip": 3.2, "econ": {}}
    assert "102%" in pb._advisory_cell(r, 124.4, None) and "提醒讨论止盈" in pb._advisory_cell(r, 124.4, None)
    assert "估值不完整" in pb._advisory_cell(r, 124.4, "buy_bid_missing")
    u = {**r, "side": "C", "k_sell": 102.0, "k_buy": 100.0, "credit": -1.10}
    assert "不可达" in pb._advisory_cell(u, 10.0, None)
