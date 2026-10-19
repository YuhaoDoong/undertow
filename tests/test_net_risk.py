"""同标的整体风险（Codex 061/062，用户 2026-10-19）：净额、跨到期保守界、部分平仓重算、政策 B 接入。全部是合成数据。"""
from __future__ import annotations

import pytest

from undertow.analyze import net_risk as nr
from undertow.analyze import risk_policy as rp

E1, E2 = "2026-11-20", "2026-12-18"


def P(k, q, e=E1):
    return {"kind": "P", "strike": k, "expiry": e, "qty": q}


def C(k, q, e=E1):
    return {"kind": "C", "strike": k, "expiry": e, "qty": q}


def test_codex_counterexample_gross_1200_net_200_gain_800():
    """061 合成反例：110/100/90 put 蝶式零费用。拆成 熊市 put 110/100 付 6（各自最大亏损 600）+ 牛市 put 100/90 收 4（600）
    → 各笔相加 1200；共同到期情景最大亏损 = 净借记 200，最大收益在 100 处 800。"""
    a = nr.unit_risk([P(110, 1), P(100, -1)], net_cash=-600, future_fees=0)
    b = nr.unit_risk([P(100, -1), P(90, 1)], net_cash=400, future_fees=0)
    assert a["max_loss"] + b["max_loss"] == 1200
    u = nr.unit_risk([P(110, 1), P(100, -1), P(100, -1), P(90, 1)], net_cash=-200, future_fees=0)
    assert (u["bound_type"], u["max_loss"], u["max_gain"], u["best_at"]) == ("joint_bound", 200, 800, 100)


def test_split_order_and_label_do_not_change_result():
    """062 验收 6：同一实际净持仓，拆单/排序/重复合约分写，结果不变；腿与现金不算两次。"""
    one = nr.unit_risk([P(52, 2), P(50, -4), P(48, 2)], net_cash=-88.39, future_fees=6.4)
    split = nr.unit_risk([P(48, 1), P(50, -1), P(52, 2), P(50, -3), P(48, 1)], net_cash=-88.39, future_fees=6.4)
    assert one["max_loss"] == split["max_loss"] == 94.79 and one["net_legs"] == split["net_legs"]


def test_unbounded_and_closing_protection_increases_risk():
    """062 验收 4：熊市 call 价差平掉保护腿 → 剩裸空 call，右尾无上限；平仓不等于减险。"""
    w = nr.what_if([C(100, -1), C(105, 1)], 300.0, [C(105, -1)], 50.0, future_fee_per_contract=0.8)
    assert w["before"]["bound_type"] == "joint_bound" and w["before"]["max_loss"] == pytest.approx(201.6)
    assert w["after"]["bound_type"] == "unbounded" and w["after"]["max_loss"] is None


def test_close_short_only_leaves_long_put_risk_equal_cash():
    """只回补短腿：剩多头 put，最大亏损 = 至今净投入 + 未来费用（价值下界 0）。"""
    w = nr.what_if([P(52, 2), P(50, -2)], -149.19, [P(50, 2)], -2 * 100 * 0.60 - 1.6, future_fee_per_contract=0.8)
    assert w["after"]["net_legs"] == [{"kind": "P", "strike": 52.0, "expiry": E1, "qty": 2}]
    assert w["after"]["max_loss"] == pytest.approx(149.19 + 121.6 + 1.6)


def test_long_calendar_far_protection_not_zero():
    """062 验收 1：同行权价 long call calendar——近月到期时远月多头不按 0，不判裸空 call 无界；保守界 = 净借记 + 费用。"""
    r = nr.unit_risk([C(100, -1, E1), C(100, 1, E2)], net_cash=-250, future_fees=1.6)
    assert r["bound_type"] == "conservative_bound" and r["max_loss"] == 251.6
    assert any("最近到期日" in a for a in r["assumptions"])


def test_diagonal_ratio_far_naked_is_unresolved_not_proven_unbounded():
    """062 验收 2：远月空 call 张数多于近月多头（比例对角）→ 保守界无穷 → unresolved（不说已证明无界、不当 0）。"""
    r = nr.unit_risk([C(100, 1, E1), C(105, -2, E2)], net_cash=300, future_fees=2.4)
    assert r["bound_type"] == "unresolved" and r["max_loss"] is None


def test_far_same_direction_spreads_bound_equals_sum():
    """三笔同向熊市 call 价差、三个到期：没有抵消，跨期保守界 = 各笔之和（远月组按自身到期最小损益兜底，不按 ≤S 放大）。"""
    legs = [lg for e in ("2026-10-16", "2026-10-23", "2026-10-30") for lg in (C(400, -1, e), C(402, 1, e))]
    r = nr.unit_risk(legs, net_cash=3 * 80 - 3 * 1.6, future_fees=3 * 1.6)
    assert r["bound_type"] == "conservative_bound" and r["max_loss"] == pytest.approx(369.6)


def test_missing_inputs_unresolved_not_zero():
    assert nr.unit_risk([P(50, 1)], net_cash=None)["bound_type"] == "unresolved"
    assert nr.unit_risk([{"kind": "P", "strike": None, "expiry": E1, "qty": 1}], net_cash=-10)["max_loss"] is None


def test_stop_bound_only_for_nonnegative_value_structures():
    fly = nr.unit_risk([P(52, 2), P(50, -4), P(48, 2)], net_cash=-88.39, future_fees=6.4)
    assert nr.stop_bound(fly) == 94.79
    credit = nr.unit_risk([P(50, -1), P(48, 1)], net_cash=31, future_fees=1.6)
    assert nr.stop_bound(credit) is None                       # 卖方价差价值可为负：止损待补，不拿到期最大亏损冒充


# —— 政策 B 接入 ——

def _row(pid, legs, cash, fees, und="SLV"):
    return {"id": pid, "underlying": und, "stop_loss": None, "payoff_max_loss": None, "legs": legs,
            "net_cash": cash, "future_fees": fees, "cash_basis": "合成"}


CAP, PAUSE = {"equity": 664.0}, {"latched": False}


def test_book_nets_same_underlying_and_counts_once():
    book = [_row("a", [P(52, 2), P(50, -2)], -149.19, 3.2),
            _row("b", [P(50, -2), P(48, 2)], 60.8, 3.2)]
    b = rp.evaluate_book(book, CAP)
    ml = next(c for c in b["checks"] if c["check"].endswith("单笔理论最大亏损"))
    assert ml["value"] == pytest.approx(94.79) and b["overall"] == "pass"
    gross = rp.evaluate_book([dict(book[0], legs=None, payoff_max_loss=152.39, stop_loss=152.39),
                              dict(book[1], legs=None, payoff_max_loss=342.4, stop_loss=342.4)], CAP)
    assert gross["overall"] == "fail"                          # 无腿明细的旧口径：各笔相加（保守上界）


def test_different_deliverables_not_netted():
    book = [_row("a", [P(52, 2), P(50, -2)], -149.19, 3.2, "SLV"), _row("b", [P(50, -2), P(48, 2)], 60.8, 3.2, "SIVR")]
    _, rep = rp._net_units(rp._book_rows(book)[0])
    assert {r["unit"] for r in rep} == {"SLV", "SIVR"}
    assert rp.unit_key("slv.US") == rp.unit_key("SLV") == "SLV"


def test_budget_enumerates_non_monotonic_feasible_set():
    """061 §3：对冲时风险不随组数单调，逐个枚举、给确切可行集合。R=1000；QQQ 已有一笔大仓（160 > 150）；
    SLV 现仓 52/50P ×2（152.39，也是大仓 → 现有账本 2 笔大仓已超）。加 50/49 牛市 put（每组收 0.05）：
    1 组 150.59 仍是大仓 → 不通过；2–4 组 148.79/146.99/145.19 → 通过；5 组 S=0 处净空 put → 243.39 → 不通过。
    旧版试 1 组失败就 break，会报 0 组。"""
    book = [_row("a", [P(52, 2), P(50, -2)], -149.19, 3.2),
            {"id": "q", "underlying": "QQQ", "stop_loss": 80.0, "payoff_max_loss": 160.0}]
    cand = {"id": "c", "underlying": "SLV", "unit_stop_loss": 96.6, "unit_payoff_max_loss": 96.6, "qty_requested": 6,
            "unit_legs": [P(50, -1), P(49, 1)], "unit_cash": 5 - 1.6, "unit_future_fees": 1.6}
    r = rp.evaluate_budget(candidate=cand, open_positions=book, capital={"equity": 1000.0}, pause=PAUSE)
    assert r["feasible_units"] == [2, 3, 4] and r["allowed_units"] == 4 and r["budget_status"] == "pass"
    assert any("不连续" in x for x in r["reasons"]) and any("已超限" in x for x in r["reasons"])


def test_budget_without_legs_keeps_old_behavior():
    r = rp.evaluate_budget(candidate={"id": "c", "underlying": "SLV", "unit_stop_loss": 100, "unit_payoff_max_loss": 100,
                                      "qty_requested": 5}, open_positions=[], capital=CAP, pause=PAUSE)
    assert r["feasible_units"] == [1, 2] and r["allowed_units"] == 2 and r.get("netting") is None  # 止损 300 > 200


def test_pending_group_stop_is_incomplete_not_fail():
    """跨期净额单元没有整组止损规则 → 单笔计划止损 incomplete，预算 incomplete（不是通过，也不是确定的 fail）。"""
    book = [_row("cal", [C(100, -1, E1), C(100, 1, E2)], -50, 1.6, "QQQ")]
    b = rp.evaluate_book(book, CAP)
    st = next(c for c in b["checks"] if c["check"].endswith("单笔计划止损"))
    assert st["status"] == "incomplete" and b["overall"] == "incomplete"


def test_apply_cash_ledger_overrides_cost_basis():
    from undertow.analyze.risk_aggregate import apply_cash_ledger
    book = [{"id": "a", "underlying": "SLV", "legs": [dict(P(52, 2), symbol="S52"), dict(P(50, -2), symbol="S50")],
             "net_cash": -146.0, "future_fees": 3.2, "cash_basis": "成本价"}]
    notes = apply_cash_ledger(book, [{"symbol": "S52", "balance": "-263.60"}, {"symbol": "S50", "balance": "114.41"},
                                     {"symbol": "OTHER", "balance": "999"}], since="2026-07-21")
    assert notes == [] and book[0]["net_cash"] == pytest.approx(-149.19) and "现金流水" in book[0]["cash_basis"]


def test_spread_candidate_carries_legs_and_cash():
    from undertow.analyze import spread_pricing as sp
    spec = {"kind": "P", "long": 48.0, "short": 50.0, "debit": False}
    econ = {"unit_max_loss": 171.2, "qty": 2, "net": -0.32}
    c = sp.budget_candidate(spec, econ, "SLV", "x", expiry="2026-10-30")
    assert c["unit_legs"] == [P(48.0, 1, "2026-10-30"), P(50.0, -1, "2026-10-30")]
    assert c["unit_cash"] == pytest.approx(32 - 1.6) and c["unit_future_fees"] == pytest.approx(1.6)
