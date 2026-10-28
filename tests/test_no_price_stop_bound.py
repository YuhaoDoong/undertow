"""Codex 065 WP1：没有价格止损计划时，用联合全损界核对计划止损额度（用户 2026-10-23 澄清，政策 B 金额不改）。全部合成数据。"""
from __future__ import annotations

import pytest

from undertow.analyze import risk_policy as rp

E1, E2 = "2026-11-20", "2026-12-18"


def P(k, q, e=E1):
    return {"kind": "P", "strike": k, "expiry": e, "qty": q}


def row(pid, legs, cash, stop=None, fees=0.0, und="SLV"):
    return {"id": pid, "underlying": und, "stop_loss": stop, "payoff_max_loss": None, "legs": legs,
            "net_cash": cash, "future_fees": fees, "cash_basis": "合成"}


def stop_check(b):
    return next(c for c in b["checks"] if c["check"].endswith("单笔计划止损"))


def test_credit_spread_without_stop_passes_when_full_loss_within_stop_cap():
    """R=1000：止损上限 200；贷记 50/48P 收 0.20 → 全损界 180，无价格止损也通过（不是 N/A、不是 0）。"""
    b = rp.evaluate_book([row("a", [P(50, -1), P(48, 1)], 20.0)], {"equity": 1000.0})
    c = stop_check(b)
    assert c["status"] == "pass" and c["value"] == pytest.approx(180.0) and c["limit"] == 200
    assert c["note"].startswith(rp.NO_PRICE_STOP_BASIS)
    assert b["overall"] == "pass"


def test_full_loss_above_stop_cap_fails_not_exempt():
    """最大亏损 220 > 止损上限 200：不能仅凭「有限风险」免除计划 → fail（064 的 N/A 豁免已撤回）。"""
    c = stop_check(rp.evaluate_book([row("a", [P(50, -1), P(48, 1)], -20.0)], {"equity": 1000.0}))
    assert c["status"] == "fail" and c["value"] == pytest.approx(220.0)


def test_same_full_loss_passes_at_larger_R():
    """R=1500：止损上限 225；同一全损界 220 → 通过。"""
    c = stop_check(rp.evaluate_book([row("a", [P(50, -1), P(48, 1)], -20.0)], {"equity": 1500.0}))
    assert c["status"] == "pass" and c["limit"] == pytest.approx(225.0)


def test_existing_plan_kept_and_both_caps_checked_separately():
    """已有价格止损计划 150、理论风险 220（R=1000）：止损按计划 150 核（pass），理论风险按 300 上限核（pass）——不被全损界覆盖。"""
    b = rp.evaluate_book([row("a", [P(50, -1), P(48, 1)], -20.0, stop=150.0)], {"equity": 1000.0})
    s = stop_check(b)
    ml = next(c for c in b["checks"] if c["check"].endswith("单笔理论最大亏损"))
    assert s["value"] == 150.0 and s["note"] == "原组合计划止损" and s["status"] == "pass"
    assert ml["value"] == pytest.approx(220.0) and ml["limit"] == 300 and ml["status"] == "pass"


def test_debit_and_multi_member_unit_use_joint_bound():
    """借记同样适用；同标的两笔合成一个单元（不再有单笔原计划）→ 联合全损界只算一次。"""
    b = rp.evaluate_book([row("a", [P(52, 1), P(50, -1)], -60.0), row("b", [P(50, -1), P(48, 1)], 50.0)],
                         {"equity": 1000.0})
    c = stop_check(b)
    assert c["note"].startswith(rp.NO_PRICE_STOP_BASIS) and c["status"] == "pass"


def test_conservative_bound_over_cap_says_not_proven():
    """跨期保守界超限：只说按当前保守界未获准，不声称真实风险一定超限。"""
    legs = [lg for e in (E1, E2) for lg in (P(52, -1, e), P(50, 1, e))]
    b = rp.evaluate_book([row("a", legs, -40.0)], {"equity": 1000.0})
    c = stop_check(b)
    assert c["status"] == "fail" and "保守" in c["note"] and "不是已证明超限" in c["note"]


def test_unknown_bound_stays_incomplete():
    """风险界算不出 → 计划止损项 incomplete（不当无计划、不当 0）。"""
    b = rp.evaluate_book([row("a", [P(50, -1), P(48, 1)], None)], {"equity": 1000.0})
    assert b["overall"] == "incomplete"


def test_no_price_stop_bound_helper():
    assert rp.no_price_stop_bound("joint_bound", 180.0)[0] == 180.0
    assert rp.no_price_stop_bound("unbounded", None)[0] is None
    assert rp.no_price_stop_bound("unresolved", None)[0] is None
    assert rp.no_price_stop_bound("joint_bound", float("nan"))[0] is None
