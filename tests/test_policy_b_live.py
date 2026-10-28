"""实盘政策 B 自适应修订 r1（Codex 065 WP3；用户 2026-10-28 确认启用）：大仓线 max(15%·R, $150)、R ≤ 0 禁新增、按分取额度。
POLICY_B 原样保留供复现。全部合成金额。"""
import pytest

from undertow.analyze import r_budget as rb
from undertow.analyze import risk_policy as rp

L, B = rp.LIVE_POLICY, rp.POLICY_B
OK = {"latched": False}


def pos(i, ml, u="GLD", sl=None):
    return {"id": i, "underlying": u, "payoff_max_loss": ml, "stop_loss": ml if sl is None else sl, "state": "open"}


def big(b):
    return next(c for c in b["checks"] if c["check"] == "大仓数量")


@pytest.mark.parametrize("R,thr", [(500, 150), (1000, 150), (1500, 225), (2000, 300), (5000, 750), (1999.99, 299.99)])
def test_threshold_table_is_half_max_loss_cap(R, thr):
    assert rp.big_threshold_usd(L, R) == pytest.approx(thr)
    assert rp.big_threshold_usd(L, R) == rp.floor_cents(0.5 * rp.limit_usd(L["max_loss"], R))
    assert rp.big_threshold_usd(B, R) == 150.0                             # 旧 B 固定线原样


def test_strictly_greater_and_cent_boundary():
    """R=1999.99 → 线 299.99，风险 300 属大仓；R=2000 → 线 300，风险 300 不属大仓（等于不算）。"""
    assert big(rp.evaluate_book([pos("a", 300.0, sl=10)], {"equity": 1999.99}, L))["value"] == 1
    assert big(rp.evaluate_book([pos("a", 300.0, sl=10)], {"equity": 2000.0}, L))["value"] == 0


def test_two_cross_underlying_250_old_vs_new():
    """R=2000，GLD 与 QQQ 各 250：旧固定线下两个大仓（fail）；新线 300 下都不是大仓。类别/全场仍分别检查。"""
    book = [pos("g", 250.0, "GLD", sl=100), pos("q", 250.0, "QQQ", sl=100)]
    old = rp.evaluate_book(book, {"equity": 2000.0}, B)
    new = rp.evaluate_book(book, {"equity": 2000.0}, L)
    assert big(old)["value"] == 2 and big(old)["status"] == "fail"
    assert big(new)["value"] == 0 and big(new)["status"] == "pass" and new["overall"] == "pass"


def test_lower_R_reclassifies_and_freezes_new_but_does_not_close():
    book = [pos("g", 250.0, "GLD", sl=100), pos("q", 250.0, "QQQ", sl=100)]
    b = rp.evaluate_book(book, {"equity": 1000.0}, L)
    assert big(b)["status"] == "fail"                                       # 只报告、冻结新增；不自动平仓
    r = rp.evaluate_budget(candidate={"id": "c", "underlying": "SPY", "unit_stop_loss": 10, "unit_payoff_max_loss": 10,
                                      "qty_requested": 1}, open_positions=book, capital={"equity": 1000.0}, pause=OK, policy=L)
    assert r["allowed_units"] == 0 and any("现有账本已超限" in x for x in r["reasons"])


def test_same_underlying_split_counted_once():
    P = lambda k, q: {"kind": "P", "strike": k, "expiry": "2026-11-20", "qty": q}
    book = [{"id": "a", "underlying": "SLV", "stop_loss": None, "payoff_max_loss": None, "legs": [P(50, -1), P(48, 1)],
             "net_cash": 20.0, "future_fees": 0.0},
            {"id": "b", "underlying": "SLV", "stop_loss": None, "payoff_max_loss": None, "legs": [P(50, -1), P(48, 1)],
             "net_cash": 20.0, "future_fees": 0.0}]
    b = rp.evaluate_book(book, {"equity": 1000.0}, L)
    assert big(b)["value"] == 1                                              # 联合 360 > 150：一个单元、一个大仓


def test_nonpositive_R_blocks_new_in_live_only():
    c = {"id": "c", "underlying": "GLD", "unit_stop_loss": 50, "unit_payoff_max_loss": 50, "qty_requested": 1}
    r = rp.evaluate_budget(candidate=c, open_positions=[], capital={"equity": 0.0}, pause=OK, policy=L)
    assert r["budget_status"] == "fail" and r["allowed_units"] == 0
    assert any(x["check"] == "当前权益 R" and x["status"] == "fail" for x in r["checks"])
    old = rp.evaluate_budget(candidate=c, open_positions=[], capital={"equity": 0.0}, pause=OK, policy=B)
    assert old["policy_version"] == rp.VERSION_B and old["checks"][1]["status"] == "pass"   # 旧版行为原样复现
    unk = rp.evaluate_budget(candidate=c, open_positions=[], capital={"equity": None}, pause=OK, policy=L)
    assert unk["budget_status"] == "incomplete"


def test_cent_floor_limits_only_in_live():
    assert rp.policy_limit(L, L["stop_loss"], 1999.99) == 299.99
    assert rp.policy_limit(B, B["stop_loss"], 1999.99) == pytest.approx(299.9985)


def test_other_rules_unchanged_and_old_version_frozen():
    for k in ("stop_loss", "max_loss", "categories", "account", "take_profit_gain_frac", "pause", "structures",
              "planned_trial_capital_usd"):
        assert L[k] == B[k]
    assert B["big_position"] == {"threshold_usd": 150.0, "max_count": 1} and B["version"] == "risk-policy-v2-live-trial-B-20261014"
    assert L["amends"] == rp.VERSION_B and rp.VERSION_B in L["state_lineage"]
    assert L["R_basis"] == rb.R_BASIS_VERSION
    m = rp.POLICY_B_LIVE_META
    assert m["big_position"]["confirmed_by"] == "用户" and "确认" in m["big_position"]["source"]
    assert any("峰值回撤" in x for x in m["_open"])                         # 峰值闸门未启用


def test_trial_state_from_policy_b_still_valid():
    from undertow import trial_cli as tcli
    assert tcli._check_identity({"policy_version": rp.VERSION_B}) is None
    assert tcli._check_identity({"policy_version": rp.VERSION_B_LIVE}) is None
    assert tcli._check_identity({"policy_version": "risk-policy-v9-other"}) is not None


def test_aggregate_uses_budget_R_not_net_assets():
    from types import SimpleNamespace as NS
    from undertow.analyze.risk_aggregate import aggregate
    a = aggregate(NS(groups=[], unmapped=[]), NS(net_assets=5000.0), asof="d", policy=L, R=None)
    assert a["overall"] == "incomplete" and a["R_budget"] is None          # R 未知不拿净资产顶替
    a2 = aggregate(NS(groups=[], unmapped=[]), NS(net_assets=5000.0), asof="d", policy=L, R=1000.0)
    assert a2["R_budget"] == 1000.0 and "600" in a2["account_limit"]
