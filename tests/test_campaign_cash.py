"""Codex 063 N1 / 064 §5：现金事件按本轮唯一分配（含已离场腿）、三把尺子分开。全部合成流水，不读账户、不联网。

断言不预设「通过」：期望值都由 fixture 的流水推导（Codex 064：正确数字仍可能因别的原因不通过）。"""
import pytest

from undertow.analyze import campaign_cash as cc
from undertow.analyze import net_risk as nr
from undertow.analyze import risk_aggregate as ra
from undertow.analyze import risk_policy as rp

W0 = "2026-02-01T00:00:00+00:00"


def sym(k, kind="P", exp="260320", root="XYZ"):
    return f"{root}{exp}{kind}{int(round(k * 1000)):08d}.US"


def leg(k, q, kind="P", exp="2026-03-20", root="XYZ"):
    return {"kind": kind, "strike": float(k), "expiry": exp, "qty": q,
            "symbol": sym(k, kind, exp[2:4] + exp[5:7] + exp[8:10], root)}


def flow(t, s, amt, fee=False):
    return {"time": t, "symbol": s, "balance": f"{amt:.2f}",
            "flow_name": ("Option Sell Fee" if fee else ("Option Sell Transaction" if amt > 0 else "Option Buy Transaction"))}


# —— 独立合成的换腿例（虚构标的 XYZ、虚构日期与金额，不对应任何真实成交）：
#    起点组合单买 32P 卖 30P；之后先买 28P、隔几十秒再分两笔卖出 32P；30P 空头沿用。
#    另有一组更早到期的同标的独立交易（另开另平），不应并入本轮。
P32, P30, P28 = sym(32), sym(30), sym(28)
Q30, Q28 = sym(30, exp="260316"), sym(28, exp="260316")
ROLL = [flow("2026-03-02T15:01:07Z", P30, 104.0), flow("2026-03-02T15:01:07Z", P32, -238.0),
        flow("2026-03-03T10:30:00Z", P32, -1.62, fee=True), flow("2026-03-03T10:30:00Z", P30, -1.58, fee=True),
        flow("2026-03-09T14:20:11Z", P28, -150.0),
        flow("2026-03-09T14:21:40Z", P32, 320.0), flow("2026-03-09T14:21:40Z", P32, 320.0),
        flow("2026-03-09T15:02:30Z", Q30, 130.0), flow("2026-03-09T15:02:30Z", Q28, -30.0),
        flow("2026-03-10T10:30:00Z", P28, -1.57, fee=True), flow("2026-03-10T10:30:00Z", P32, -1.63, fee=True),
        flow("2026-03-10T10:30:00Z", Q28, -0.81, fee=True), flow("2026-03-10T10:30:00Z", Q30, -0.84, fee=True),
        flow("2026-03-16T18:55:02Z", Q30, -120.0), flow("2026-03-16T18:55:02Z", Q28, 2.0),
        flow("2026-03-17T10:30:00Z", Q30, -0.81, fee=True), flow("2026-03-17T10:30:00Z", Q28, -0.84, fee=True)]
HELD = [{"id": "xyz0320", "underlying": "XYZ", "legs": [leg(28, 2), leg(30, -2)]}]
STAGE = {"start": "2026-03-09T14:20:11Z", "inherited": [{"symbol": P30, "qty": -2, "low": 1.70, "ref": 1.78, "high": 1.84}]}


def _expect_campaign():
    return round(sum(float(r["balance"]) for r in ROLL if r["symbol"] in (P32, P30, P28)), 2)


def test_roll_keeps_exited_leg_cash_and_excludes_independent_trade():
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0)
    assert r["quality"] == "bound" and r["problems"] == []
    assert r["campaign_cash"] == pytest.approx(_expect_campaign())          # 已离场 32P 的两笔卖出都在
    assert r["exited"] == [P32] and set(r["linked"]) == {P28, P30, P32}
    assert r["independent_rows"] == sum(1 for x in ROLL if x["symbol"] in (Q30, Q28) and "Fee" not in x["flow_name"])
    assert r["duplicate_keys"] == 1                                         # 两条同秒同额：两笔分笔，按两笔计
    assert sum(r["by_row"].values()) == pytest.approx(r["campaign_cash"])   # 现金守恒


def test_roll_three_rulers_and_policy_uses_stage_conservative_end():
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors={"xyz0320": "2026-03-02T15:01:07Z"},
                     stages={"xyz0320": STAGE})
    assert r["quality"] == "verified"
    st = r["stages"]["xyz0320"]
    real = -150.0 - 1.57                                                    # 新阶段的新腿：28P 买入 + 其费用
    assert st["real_cash"] == pytest.approx(real)
    for k, px in (("low", 1.70), ("ref", 1.78), ("high", 1.84)):
        assert st["cash"][k] == pytest.approx(real + 2 * 100 * px)
        assert st["old_stage"][k] + st["cash"][k] == pytest.approx(r["campaign_cash"])   # 内部估值不改变真实总账
    assert st["policy_endpoint"] == "low" and r["policy_cash_by_row"]["xyz0320"] == pytest.approx(st["cash"]["low"])
    legs = HELD[0]["legs"]
    ff = 4 * cc.FEE_PER_CONTRACT
    camp = nr.unit_risk(legs, net_cash=r["campaign_cash"], future_fees=ff)
    new = nr.unit_risk(legs, net_cash=st["cash"]["low"], future_fees=ff)
    width = 2 * 2 * 100
    assert camp["max_loss"] == pytest.approx(max(0, width - r["campaign_cash"] + ff))
    assert new["max_loss"] == pytest.approx(width - st["cash"]["low"] + ff)
    assert new["max_loss"] > camp["max_loss"]                               # 旧阶段利润不抵新阶段风险
    held_only = sum(float(x["balance"]) for x in ROLL if x["symbol"] in (P30, P28))   # 旧口径：只认在场合约的流水
    old_wrong = nr.unit_risk(legs, net_cash=held_only, future_fees=ff)
    assert old_wrong["max_loss"] == pytest.approx(width - held_only + ff) and old_wrong["max_loss"] > new["max_loss"]


def test_roll_policy_verdicts_follow_numbers_not_presumed():
    book = [dict(HELD[0], legs=[dict(x) for x in HELD[0]["legs"]], stop_loss=None, payoff_max_loss=None,
                 future_fees=4 * cc.FEE_PER_CONTRACT)]
    plans = {"live|XYZ|2026-03-20|P|30/28": {"campaign_start": "2026-03-02T15:01:07Z", "stage": STAGE}}
    notes = ra.apply_campaign_cash(book, {"rows": ROLL, "window_start": W0, "plans": plans})
    assert notes == [] or all("XYZ" in n for n in notes)
    R = 1240.0
    b = rp.evaluate_book(book, {"equity": R}, rp.LIVE_POLICY)
    ml = nr.unit_risk(book[0]["legs"], net_cash=book[0]["net_cash"], future_fees=book[0]["future_fees"])["max_loss"]
    lim = {c["check"].split("·")[-1]: c for c in b["checks"]}
    assert lim["单笔理论最大亏损"]["value"] == pytest.approx(ml)
    assert lim["单笔理论最大亏损"]["status"] == ("pass" if ml <= rp.policy_limit(rp.LIVE_POLICY, rp.LIVE_POLICY["max_loss"], R) else "fail")
    stop = next(c for c in b["checks"] if c["check"].endswith("单笔计划止损"))
    assert stop["value"] == pytest.approx(ml)                               # 未设价格止损 → 按全损界核对
    assert stop["status"] == ("pass" if ml <= rp.policy_limit(rp.LIVE_POLICY, rp.LIVE_POLICY["stop_loss"], R) else "fail")


def test_codex063_shared_middle_strike_counted_once():
    """Codex 063 反例：+110P/−100P 与 −100P/+90P 两行共用 100P；真实总现金 −200、零费蝶式最大亏损 200。"""
    e = "2026-11-20"
    a = {"id": "a", "underlying": "XYZ", "legs": [leg(110, 1, exp=e), leg(100, -1, exp=e)], "net_cash": -300.0,
         "future_fees": 0.0, "stop_loss": None, "payoff_max_loss": None}
    b = {"id": "b", "underlying": "XYZ", "legs": [leg(100, -1, exp=e), leg(90, 1, exp=e)], "net_cash": 100.0,
         "future_fees": 0.0, "stop_loss": None, "payoff_max_loss": None}
    s = lambda k: sym(k, exp="261120")
    rows = [flow("2026-10-01T14:00:00Z", s(110), -600.0), flow("2026-10-01T14:00:00Z", s(100), 600.0),
            flow("2026-10-01T14:00:00Z", s(90), -200.0)] + \
           [flow("2026-10-02T10:00:00Z", s(k), 0.0, fee=True) for k in (110, 100, 90)]
    book = [a, b]
    ra.apply_campaign_cash(book, {"rows": rows, "window_start": W0, "plans": {}})
    truth = sum(float(r["balance"]) for r in rows)
    assert sum(p["net_cash"] for p in book) == pytest.approx(truth)          # 旧实现两行各认领一次 → +400
    _, rep = rp._net_units(book)
    expected = nr.unit_risk(a["legs"] + b["legs"], net_cash=truth, future_fees=0.0)["max_loss"]
    assert rep[0]["risk"]["max_loss"] == pytest.approx(expected)
    assert book[0]["net_cash"] == pytest.approx(-600 + 600) and book[1]["net_cash"] == pytest.approx(-200)  # 共用腿只归一行


def test_codex064_internal_price_never_changes_true_total():
    """Codex 064 合成例（零费、每股 ×100）：先 +100C/−110C 买 6 卖 3；后买 120C 付 1、卖原 100C 收 9；保留 −110C。"""
    e, x = "2026-11-20", "261120"
    s = lambda k: sym(k, "C", x)
    rows = [flow("2026-10-01T14:00:00Z", s(100), -600.0), flow("2026-10-01T14:00:00Z", s(110), 300.0),
            flow("2026-10-05T14:00:00Z", s(120), -100.0), flow("2026-10-05T14:00:05Z", s(100), 900.0)] + \
           [flow(f"2026-10-0{d}T10:00:00Z", s(k), 0.0, fee=True) for d, k in ((2, 100), (2, 110), (6, 120), (6, 100))]
    held = [{"id": "r", "underlying": "XYZ", "legs": [leg(110, -1, "C", e), leg(120, 1, "C", e)]}]
    stage = {"start": "2026-10-05T14:00:00Z",
             "inherited": [{"symbol": s(110), "qty": -1, "low": 3.8, "ref": 4.0, "high": 4.2}]}
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"r": "2026-10-01T14:00:00Z"}, stages={"r": stage})
    assert r["campaign_cash"] == pytest.approx(500.0) and r["exited"] == [s(100)]
    camp = nr.unit_risk(held[0]["legs"], net_cash=r["campaign_cash"], future_fees=0.0)
    assert camp["max_loss"] == pytest.approx(500.0)                         # 到期最坏 −5/股
    st = r["stages"]["r"]
    for k, old, new in (("low", 220, 720), ("ref", 200, 700), ("high", 180, 680)):
        assert st["old_stage"][k] == pytest.approx(old)
        risk = nr.unit_risk(held[0]["legs"], net_cash=st["cash"][k], future_fees=0.0)["max_loss"]
        assert risk == pytest.approx(new) and old - risk == pytest.approx(-500.0)
    assert st["policy_endpoint"] == "low"                                   # 沿用空头取低价 → 新阶段风险最大，不是「取 ask」


def test_same_contract_earlier_round_unanchored_is_conservative():
    """同合约更早的独立轮次在窗口内：未登记起点 → 取可行起点里现金最少者，不会比真实本轮更乐观。"""
    s50 = sym(50)
    held = [{"id": "h", "underlying": "XYZ", "legs": [leg(50, 1)]}]
    for first_round in ((-100.0, 150.0), (-100.0, 50.0)):                  # 早轮赚钱 / 早轮亏钱
        rows = [flow("2026-09-10T14:00:00Z", s50, first_round[0]), flow("2026-09-12T14:00:00Z", s50, first_round[1]),
                flow("2026-10-01T14:00:00Z", s50, -200.0)] + \
               [flow(t, s50, 0.0, fee=True) for t in ("2026-09-11T10:00:00Z", "2026-09-13T10:00:00Z", "2026-10-02T10:00:00Z")]
        bound = cc.unit_cash("XYZ", held, rows, window_start=W0)
        true = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"h": "2026-10-01T14:00:00Z"})
        assert true["quality"] == "verified" and true["campaign_cash"] == pytest.approx(-200.0)
        assert bound["quality"] == "bound" and bound["campaign_cash"] <= true["campaign_cash"] + 1e-9
        assert bound["inferred_starts"]["h"].startswith("2026-09-10")


@pytest.mark.parametrize("mutate,why", [
    (lambda rows: rows.__setitem__(0, dict(rows[0], balance=None)), "金额"),
    (lambda rows: rows.__setitem__(0, dict(rows[0], time="2026-03-02 15:01:07")), "时刻"),
])
def test_bad_rows_make_unit_incomplete(mutate, why):
    rows = [dict(x) for x in ROLL]
    mutate(rows)
    r = cc.unit_cash("XYZ", HELD, rows, window_start=W0)
    assert r["quality"] == "incomplete" and any(why in p for p in r["problems"]) and r["campaign_cash"] is None


def test_window_coverage_and_anchor_problems():
    late = cc.unit_cash("XYZ", HELD, ROLL, window_start="2026-03-05T00:00:00+00:00")   # 30P 建仓在窗口外
    assert late["quality"] == "incomplete" and any(P30 in p for p in late["problems"])
    edge = cc.unit_cash("XYZ", HELD, ROLL, window_start="2026-03-02T00:00:00+00:00")   # 最早关联成交在窗口首日
    assert edge["quality"] == "incomplete" and any("窗口首日" in p for p in edge["problems"])
    early = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors={"xyz0320": "2026-01-15T00:00:00Z"})
    assert early["quality"] == "incomplete" and any("早于流水窗口" in p for p in early["problems"])
    after = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors={"xyz0320": "2026-03-05T00:00:00Z"})
    assert after["quality"] == "incomplete" and any("登记晚了" in p for p in after["problems"])


def test_pending_fees_estimated_then_replaced_by_actual():
    s50 = sym(50)
    held = [{"id": "h", "underlying": "XYZ", "legs": [leg(50, -2)]}]
    rows = [flow("2026-10-01T14:00:00Z", s50, 200.0)]
    r = cc.unit_cash("XYZ", held, rows, window_start=W0)
    assert r["fee_estimate"] == pytest.approx(2 * cc.FEE_PER_CONTRACT) and r["pending_fee_rows"] == 1
    assert r["campaign_cash"] == pytest.approx(200.0 - 2 * cc.FEE_PER_CONTRACT)
    r2 = cc.unit_cash("XYZ", held, rows + [flow("2026-10-02T10:00:00Z", s50, -1.61, fee=True)], window_start=W0)
    assert r2["fee_estimate"] == 0 and r2["campaign_cash"] == pytest.approx(200.0 - 1.61)


def test_fee_row_straddling_start_counts_in_full():
    s50 = sym(50)
    held = [{"id": "h", "underlying": "XYZ", "legs": [leg(50, 1)]}]
    rows = [flow("2026-10-01T14:00:00Z", s50, -100.0), flow("2026-10-01T15:00:00Z", s50, 120.0),
            flow("2026-10-01T16:00:00Z", s50, -90.0), flow("2026-10-02T10:00:00Z", s50, -2.4, fee=True)]
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"h": "2026-10-01T16:00:00Z"})
    assert r["campaign_cash"] == pytest.approx(-90.0 - 2.4) and any("全额计入" in n for n in r["notes"])


def test_partial_exit_same_contract_counted_once():
    s50, s48 = sym(50), sym(48)
    held = [{"id": "h", "underlying": "XYZ", "legs": [leg(48, 1), leg(50, -1)]}]
    rows = [flow("2026-10-01T14:00:00Z", s50, 240.0), flow("2026-10-01T14:00:00Z", s48, -160.0),
            flow("2026-10-05T14:00:00Z", s50, -90.0), flow("2026-10-05T14:00:00Z", s48, 60.0)] + \
           [flow("2026-10-06T10:00:00Z", s, 0.0, fee=True) for s in (s50, s48)]
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"h": "2026-10-01T14:00:00Z"})
    assert r["campaign_cash"] == pytest.approx(240 - 160 - 90 + 60) and r["exited"] == []
    b = cc.unit_cash("XYZ", held, rows, window_start=W0)                    # 未登记起点：源无数量，保守界可能更松
    assert b["quality"] == "bound" and b["campaign_cash"] <= r["campaign_cash"]


def test_cash_unreadable_makes_budget_incomplete_not_cost_basis():
    book = [dict(HELD[0], legs=[dict(x) for x in HELD[0]["legs"]], net_cash=-40.0, future_fees=3.2,
                 stop_loss=None, payoff_max_loss=None)]
    notes = ra.apply_campaign_cash(book, {"error": "LongbridgeUnavailable"})
    assert book[0]["net_cash"] is None and notes
    assert rp.evaluate_book(book, {"equity": 900.0}, rp.LIVE_POLICY)["overall"] == "incomplete"


def test_hold_risk_from_now():
    legs = [leg(28, 2), leg(30, -2)]
    v = -(1.10 * 200)                                       # 合成：空头价差此刻可平仓值（负债）
    h = nr.hold_risk(legs, v, fee_now=3.2, fee_future=3.2)
    assert h["max_loss"] == pytest.approx(v - (-400.0))     # Vnow − Vmin，平仓费两端相抵
    assert nr.hold_risk(legs, None, fee_now=0, fee_future=0)["max_loss"] is None


def test_plan_key_and_plan_validation():
    assert cc.plan_key_for_legs("XYZ", [leg(28, 2), leg(30, -2)]) == "live|XYZ|2026-03-20|P|30/28"
    assert cc.plan_key_for_legs("XYZ", [leg(28, 2), leg(30, -1)]) is None
    from undertow.analyze import risk_watch as rw
    base = {"schema": rw.PLAN_SCHEMA, "plans": {"k": {}}}
    ok = {"campaign_start": "2026-03-02T15:01:07Z", "stage": STAGE}
    assert rw.validate_plans(dict(base, plans={"k": ok})) is None
    assert "campaign_start" in rw.validate_plans(dict(base, plans={"k": {"campaign_start": "2026-03-02 15:01"}}))
    bad = {"stage": dict(STAGE, inherited=[dict(STAGE["inherited"][0], low=2.0)])}
    assert "low ≤ ref ≤ high" in rw.validate_plans(dict(base, plans={"k": bad}))


def test_stage_leg_must_match_holding():
    st = dict(STAGE, inherited=[dict(STAGE["inherited"][0], qty=-1)])
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, stages={"xyz0320": st})
    assert r["quality"] == "incomplete" and any("不符" in p for p in r["problems"])
