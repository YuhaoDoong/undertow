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
    r = ev(cand(sl=90, ml=140, u="GLD", qty_requested=2), [pos("a", 160, u="SPY")], cap={"equity": 1000.0})
    assert r["allowed_units"] == 1                                           # 第 2 组让它变成大仓，而已有一笔大仓
    # R=500 时 SPY 160 本身已超「其它 ≤150」：整本账已超限 → 新增 0（Codex 044 P1-5；旧版只看候选类别会放行）
    r = ev(cand(sl=90, ml=140, u="GLD", qty_requested=2), [pos("a", 160, u="SPY")])
    assert r["allowed_units"] == 0 and any("现有账本已超限" in x for x in r["reasons"])


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


# —— 开仓收益风险比下限（payoff-floor-v1，用户 2026-10-14）——

def test_payoff_floor_blocks_the_0_07_credit_case():
    """10/08 实例：GLD 370/365P 收 0.07，含费最大收益 3.8 / 最大亏损 496.2 → 0.77% < 10%，打平胜率 99.2%。"""
    g = rp.payoff_floor(3.8, 496.2)
    assert g["status"] == "fail" and g["ratio"] == round(3.8 / 496.2, 4) and g["breakeven_win_rate"] == round(496.2 / 500, 4)
    assert "更远到期" in g["note"] and g["version"] == rp.PAYOFF_FLOOR_VERSION


def test_payoff_floor_boundary_and_unknown():
    assert rp.payoff_floor(10.0, 100.0)["status"] == "pass"            # 恰好 10% 放行
    assert rp.payoff_floor(9.99, 100.0)["status"] == "fail"
    assert rp.payoff_floor(0.0, 100.0)["status"] == "fail"             # 合法 0 不当缺失
    for g, l in ((None, 100.0), (5.0, None), (float("nan"), 100.0), (5.0, 0.0), (5.0, -1.0)):
        assert rp.payoff_floor(g, l)["status"] == "incomplete"          # 未知不是通过
    assert rp.payoff_floor(150.0, 350.0)["breakeven_win_rate"] == 0.7
    g = rp.payoff_floor(10.0, 100.0)                                       # G/L = .1：两点 90.91%，按 TP90 91.74%（Codex 044 探针）
    assert g["breakeven_win_rate"] == 0.9091 and g["breakeven_win_rate_tp90"] == 0.9174


# —— Codex 044 包 A：逻辑身份与完整账本准入 ——

def test_existing_other_over_limit_blocks_new_metals_candidate():
    """044 反例：已有 QQQ 190 > 其它 150，evaluate_book fail；旧 evaluate_budget 对新增 GLD 50 却 pass 1 组。"""
    old = [pos("old", 190, u="QQQ", sl=100)]
    assert rp.evaluate_book(old, CAP)["overall"] == "fail"
    r = ev(cand(sl=50, ml=50, u="GLD"), old)
    assert r["budget_status"] == "fail" and r["allowed_units"] == 0
    r = ev(cand(sl=50, ml=50, u="QQQ"), [pos("old", 310, u="GLD", sl=100)])    # 反向：金银已超，新增其它也 0
    assert r["allowed_units"] == 0


def test_existing_two_big_positions_block_small_candidate():
    two_big = [pos("a", 160, u="GLD", sl=100), pos("b", 170, u="QQQ", sl=100)]
    r = ev(cand(sl=50, ml=50, u="GLD"), two_big, cap={"equity": 5000.0})
    big = next(c for c in r["checks"] if c["check"] == "大仓数量")
    assert r["allowed_units"] == 0 and big["status"] == "fail" and big["value"] == 2


def test_duplicate_id_conflict_and_missing_id_are_incomplete():
    r = ev(cand(sl=50, ml=50), [pos("a", 100), pos("a", 120)])               # 同 id 内容冲突：不挑一条
    assert r["budget_status"] == "incomplete" and "冲突" in r["reasons"][0]
    r = ev(cand(sl=50, ml=50), [{"underlying": "GLD", "payoff_max_loss": 100, "stop_loss": 100}] * 2)
    assert r["budget_status"] == "incomplete"                                # 缺 id 不能被 None 去重成一笔
    r = ev(cand(sl=50, ml=50), [pos("a", -100)])                              # 负风险会抵消别的风险 → 拒绝
    assert r["budget_status"] == "incomplete"
    b = rp.evaluate_book([pos("a", 100), pos("a", 120)], CAP)
    assert b["overall"] == "incomplete"


@pytest.mark.parametrize("q", [0, -1, 1.5, True, "2"])
def test_bad_requested_quantity_is_incomplete(q):
    r = ev(cand(qty_requested=q), [])
    assert r["budget_status"] == "incomplete" and r["allowed_units"] == 0


def test_budget_and_book_agree_on_same_book():
    book = [pos("a", 120, u="GLD"), pos("b", 100, u="SLV")]
    r = ev(cand(sl=100, ml=100, u="GLD"), book)                             # 120+100+100 = 320 > 300
    b = rp.evaluate_book(book + [pos("c", 100, u="GLD")], CAP)
    assert r["allowed_units"] == 0 and b["overall"] == "fail"


def test_shadow_adapter_keeps_insufficient_equity():
    """044 P2-6：权益 100、理论损失 123.20：底层 insufficient_equity，旧适配层只剩 budget_status=pass、n=1。"""
    from unittest.mock import patch
    from undertow.analyze import shadow_exec as sx
    row = {"key": "synthetic", "instrument": "gold", "session": "2026-10-14"}
    leg = {"status": "candidate", "leg_id": "L", "rule": "A", "side": "C", "sell": 100, "buy": 102,
           "expiry": "2026-10-30", "width_usd": 200}
    with patch.object(sx.sh, "window_leg", return_value={"status": "valid", "credit": {"conservative": 80}}):
        r = sx.candidate(row, leg, net_assets=100, account_open_max_loss=0, cluster_open={},
                         policy=rp.POLICY_B, open_positions=[], pause={"latched": False})
    assert r["budget_status"] == "pass" and r["policy_allowed_units"] == 1
    assert r["operation_status"] == "insufficient_equity"
    # 收 80 付费 3.2：含费最大收益 76.8 / 最大亏损 123.2 = 62% → 下限通过；实盘预检 = 预算 + 下限
    assert r["payoff_floor"]["status"] == "pass" and r["live_precheck"] == "pass"
    with patch.object(sx.sh, "window_leg", return_value={"status": "valid", "credit": {"conservative": 7}}):
        r = sx.candidate(row, leg, net_assets=500, account_open_max_loss=0, cluster_open={},
                         policy=rp.POLICY_B, open_positions=[], pause={"latched": False})
    assert r["budget_status"] == "pass" and r["payoff_floor"]["status"] == "fail" and r["live_precheck"] == "fail"
