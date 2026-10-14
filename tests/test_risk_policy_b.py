"""实盘试行政策 B（Codex 043 WP2 合成验收矩阵）。"""
import pytest

from undertow.analyze import risk_policy as rp

CAP = {"equity": 500.0, "asof": "2026-10-14"}
OK = {"latched": False}


def cand(sl=200.0, ml=300.0, u="GLD", **kw):
    return {"id": "c", "underlying": u, "unit_stop_loss": sl, "unit_payoff_max_loss": ml, "qty_requested": 1, **kw}


def pos(i, ml, u="GLD", sl=None, state="open"):
    return {"id": i, "underlying": u, "payoff_max_loss": ml, "stop_loss": ml if sl is None else sl, "state": state}


def ev(c, opens, cap=CAP, pause=OK):
    return rp.evaluate_budget(candidate=c, open_positions=opens, capital=cap, pause=pause)


def test_empty_book_one_full_size_trade_passes_policy_but_operation_unverified():
    r = ev(cand(), [])
    assert r["budget_status"] == "pass" and r["allowed_units"] == 1 and r["operation_status"] == "unverified"
    assert r["policy_version"] == rp.VERSION_B


def test_second_big_position_rejected_even_if_room():
    r = ev(cand(sl=100, ml=160, u="SPY"), [pos("a", 160, u="QQQ")], cap={"equity": 5000.0})
    assert r["allowed_units"] == 0 and any(c["check"] == "大仓数量" and c["status"] == "fail" for c in r["checks"])


def test_other_category_total_150():
    r = ev(cand(sl=60, ml=60, u="USO"), [pos("a", 60, u="QQQ"), pos("b", 60, u="TLT")])
    assert r["allowed_units"] == 0 and any(c["check"] == "类别合计（other）" and c["value"] == 180 for c in r["checks"])


def test_metals_long_and_short_do_not_net():
    r = ev(cand(sl=110, ml=110, u="SLV"), [pos("a", 200, u="GLD")])
    assert r["allowed_units"] == 0 and any(c["check"] == "类别合计（metals）" and c["value"] == 310 for c in r["checks"])


def test_two_units_of_140_is_one_big_position_and_whole_trade_limits():
    r = ev(cand(sl=90, ml=140, u="GLD", qty_requested=2), [])
    assert r["allowed_units"] == 2                                           # 280 ≤ 300 金银、止损 180 ≤ 200
    r = ev(cand(sl=90, ml=140, u="GLD", qty_requested=2), [pos("a", 160, u="SPY")])
    assert r["allowed_units"] == 1                                           # 第 2 组让它变成大仓，而已有一笔大仓


def test_static_category_budget_is_450_not_500():
    r = ev(cand(sl=100, ml=100, u="USO"), [pos("a", 150, u="GLD"), pos("b", 140, u="GLD"), pos("c", 150, u="QQQ")])
    assert r["allowed_units"] == 0                                           # 其它已满 150


@pytest.mark.parametrize("opens,cap,pause", [
    ([pos("a", None)], CAP, OK),                                             # 现有持仓缺金额
    (None, CAP, OK),                                                         # 现有持仓未知
    ([], {"equity": None}, OK),                                              # 资金快照缺失
    ([], {"equity": 500.0, "stale": True}, OK),                              # 资金读数过期
    ([], CAP, None),                                                         # 暂停状态未知
])
def test_incomplete_is_never_pass(opens, cap, pause):
    r = ev(cand(), opens, cap, pause)
    assert r["budget_status"] == "incomplete" and r["allowed_units"] == 0


def test_paused_gives_zero_and_floor_does_not_override():
    r = ev(cand(sl=50, ml=50), [], cap={"equity": 200.0}, pause={"latched": True, "reason": "回撤 60%"})
    assert r["budget_status"] == "fail" and r["allowed_units"] == 0


def test_same_direction_new_tranche_evaluated_against_open_risk_no_swap():
    r = ev(cand(sl=100, ml=100, u="GLD"), [pos("a", 150, u="GLD")])
    assert r["allowed_units"] == 1                                           # 150 + 100 ≤ 300，逐项扣除在场风险
    r = ev(cand(sl=100, ml=160, u="GLD"), [pos("a", 150, u="GLD")])
    assert r["allowed_units"] == 0                                           # 310 > 300


def test_duplicate_import_idempotent_and_pending_reserves_budget():
    r = ev(cand(sl=100, ml=100, u="GLD"), [pos("a", 150), pos("a", 150)])
    assert r["allowed_units"] == 1                                           # 同 id 只算一次
    r = ev(cand(sl=100, ml=100, u="GLD"), [pos("a", 150), pos("p", 100, state="selected_pending")])
    assert r["allowed_units"] == 0                                           # 已选入待执行暂占额度


def test_limits_scale_above_floor_when_capital_grows():
    assert rp.limit_usd(rp.POLICY_B["stop_loss"], 500) == 200 and rp.limit_usd(rp.POLICY_B["stop_loss"], 2000) == 300
    assert rp.limit_usd(rp.POLICY_B["account"], 1000) == 600 and rp.limit_usd(rp.POLICY_B["account"], None) is None


def test_buying_power_checked_separately():
    r = ev(cand(unit_occupancy=400.0), [], cap={"equity": 500.0, "buying_power": 300.0})
    assert r["budget_status"] == "pass" and r["operation_status"] == "insufficient_buying_power"


def test_v1_policy_frozen():
    assert rp.POLICY["version"] == "risk-policy-v1-20260926" and rp.POLICY["max_loss_frac"] == 0.20


def test_floor_does_not_create_money():
    r = ev(cand(sl=50, ml=80), [], cap={"equity": 0.66})
    assert r["budget_status"] == "pass" and r["operation_status"] == "insufficient_equity"


def test_evaluate_book_reports_current_state():
    b = rp.evaluate_book([pos("a", 160, u="GLD"), pos("b", 170, u="SPY")], {"equity": 500.0})
    assert b["overall"] == "fail" and any(c["check"] == "大仓数量" and c["status"] == "fail" for c in b["checks"])
    assert rp.evaluate_book([pos("a", None)], {"equity": 500.0})["overall"] == "incomplete"
