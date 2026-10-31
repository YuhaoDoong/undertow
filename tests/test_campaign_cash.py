"""Codex 063 N1 / 064 §5 / 067 P0：现金事件按本轮唯一分配（含已离场腿）、三把尺子分开、成员须登记。
全部合成流水（虚构标的 XYZ、虚构日期与金额），不读账户、不联网。

断言不预设「通过」：期望值都由 fixture 的流水推导（Codex 064：正确数字仍可能因别的原因不通过）。
campaign-cash v2（Codex 067）：同秒闭包与「可行起点取最少」都不能证明成员完整 → 成员来自私有登记，说不清就 incomplete。"""
import pytest

from undertow.analyze import campaign_cash as cc
from undertow.analyze import net_risk as nr
from undertow.analyze import risk_aggregate as ra
from undertow.analyze import risk_policy as rp
from undertow.analyze import risk_watch as rw

W0 = "2026-02-01T00:00:00+00:00"


def sym(k, kind="P", exp="260320", root="XYZ"):
    return f"{root}{exp}{kind}{int(round(k * 1000)):08d}.US"


def leg(k, q, kind="P", exp="2026-03-20", root="XYZ"):
    return {"kind": kind, "strike": float(k), "expiry": exp, "qty": q,
            "symbol": sym(k, kind, exp[2:4] + exp[5:7] + exp[8:10], root)}


def flow(t, s, amt, fee=False):
    return {"time": t, "symbol": s, "balance": f"{amt:.2f}",
            "flow_name": ("Option Sell Fee" if fee else ("Option Sell Transaction" if amt > 0 else "Option Buy Transaction"))}


# —— 换腿例：起点组合单买 32P 卖 30P；之后先买 28P、隔几十秒再分两笔卖出 32P；30P 空头沿用。
#    另有一组更早到期的同标的独立交易（另开另平），登记为独立后不并入本轮。
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
ANCHOR = {"xyz0320": "2026-03-02T15:01:07Z"}
MEMBERS = {"xyz0320": {"campaign_members": [P32], "campaign_independent": [Q30, Q28]}}
STAGE = {"start": "2026-03-09T14:20:11Z", "inherited": [{"symbol": P30, "qty": -2, "low": 1.70, "ref": 1.78, "high": 1.84}]}


def _expect_campaign():
    return round(sum(float(r["balance"]) for r in ROLL if r["symbol"] in (P32, P30, P28)), 2)


def test_roll_keeps_exited_leg_cash_and_excludes_registered_independent_trade():
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors=ANCHOR, members=MEMBERS)
    assert r["quality"] == "verified" and r["problems"] == []
    assert r["campaign_cash"] == pytest.approx(_expect_campaign())          # 已离场 32P 的两笔卖出都在
    assert r["exited"] == [P32] and set(r["linked"]) == {P28, P30, P32}
    assert r["independent_rows"] == sum(1 for x in ROLL if x["symbol"] in (Q30, Q28) and "Fee" not in x["flow_name"])
    assert r["duplicate_keys"] == 1                                         # 两条同秒同额：两笔分笔，按两笔计
    assert sum(r["by_row"].values()) == pytest.approx(r["campaign_cash"])   # 现金守恒
    assert r["membership_basis"] == "registered-private" and r["assumptions"]


def test_unregistered_exited_leg_or_independent_trade_is_incomplete():
    """离场旧腿没登记成员 / 独立交易没登记 → 起点后有说不清的同标的成交 → incomplete（不是猜一个归属）。"""
    no_exit = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors=ANCHOR,
                           members={"xyz0320": {"campaign_independent": [Q30, Q28]}})
    no_ind = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors=ANCHOR,
                          members={"xyz0320": {"campaign_members": [P32]}})
    for r, s in ((no_exit, P32), (no_ind, Q30)):
        assert r["quality"] == "incomplete" and r["campaign_cash"] is None
        assert any("既不是登记成员" in p and s in p for p in r["problems"])


def test_missing_anchor_is_incomplete_with_candidate_hint_only():
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, members=MEMBERS)
    assert r["quality"] == "incomplete" and r["campaign_cash"] is None
    c = r["candidates"]["xyz0320"]
    assert c["earliest_held_trade"].startswith("2026-03-02") and P32 in c["same_second_linked"]   # 同秒闭包只作提示


def test_roll_three_rulers_and_policy_uses_stage_worst_end():
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors=ANCHOR, stages={"xyz0320": STAGE}, members=MEMBERS)
    assert r["quality"] == "verified"
    st = r["stages"]["xyz0320"]
    real = -150.0 - 1.57                                                    # 新阶段的新腿：28P 买入 + 其费用
    assert st["real_cash"] == pytest.approx(real)
    for k, px in (("low", 1.70), ("ref", 1.78), ("high", 1.84)):
        assert st["cash"][k] == pytest.approx(real + 2 * 100 * px)
        assert st["old_stage"][k] + st["cash"][k] == pytest.approx(r["campaign_cash"])   # 内部估值不改变真实总账
    assert st["policy_endpoint"] == "per-leg-worst"
    assert st["policy_cash"] == pytest.approx(st["cash"]["low"])            # 只有沿用空头 → 逐腿最差端 = low
    assert r["policy_cash_by_row"]["xyz0320"] == pytest.approx(st["policy_cash"])
    legs = HELD[0]["legs"]
    ff = 4 * cc.FEE_PER_CONTRACT
    camp = nr.unit_risk(legs, net_cash=r["campaign_cash"], future_fees=ff)
    new = nr.unit_risk(legs, net_cash=st["policy_cash"], future_fees=ff)
    width = 2 * 2 * 100
    assert camp["max_loss"] == pytest.approx(max(0, width - r["campaign_cash"] + ff))
    assert new["max_loss"] == pytest.approx(width - st["policy_cash"] + ff)
    assert new["max_loss"] > camp["max_loss"]                               # 旧阶段利润不抵新阶段风险
    held_only = sum(float(x["balance"]) for x in ROLL if x["symbol"] in (P30, P28))   # 旧口径：只认在场合约的流水
    old_wrong = nr.unit_risk(legs, net_cash=held_only, future_fees=ff)
    assert old_wrong["max_loss"] == pytest.approx(width - held_only + ff) and old_wrong["max_loss"] > new["max_loss"]


def test_roll_policy_verdicts_follow_numbers_not_presumed():
    book = [dict(HELD[0], legs=[dict(x) for x in HELD[0]["legs"]], stop_loss=None, payoff_max_loss=None,
                 future_fees=4 * cc.FEE_PER_CONTRACT)]
    plans = {"live|XYZ|2026-03-20|P|30/28": {"campaign_start": ANCHOR["xyz0320"], "stage": STAGE,
                                              **MEMBERS["xyz0320"]}}
    notes = ra.apply_campaign_cash(book, {"rows": ROLL, "window_start": W0, "plans": plans})
    assert all("XYZ" in n for n in notes) and book[0]["net_cash"] is not None
    R = 1240.0
    b = rp.evaluate_book(book, {"equity": R}, rp.LIVE_POLICY)
    ml = nr.unit_risk(book[0]["legs"], net_cash=book[0]["net_cash"], future_fees=book[0]["future_fees"])["max_loss"]
    lim = {c["check"].split("·")[-1]: c for c in b["checks"]}
    assert lim["单笔理论最大亏损"]["value"] == pytest.approx(ml)
    assert lim["单笔理论最大亏损"]["status"] == ("pass" if ml <= rp.policy_limit(rp.LIVE_POLICY, rp.LIVE_POLICY["max_loss"], R) else "fail")
    stop = next(c for c in b["checks"] if c["check"].endswith("单笔计划止损"))
    assert stop["value"] == pytest.approx(ml)                               # 未设价格止损 → 按全损界核对
    assert stop["status"] == ("pass" if ml <= rp.policy_limit(rp.LIVE_POLICY, rp.LIVE_POLICY["stop_loss"], R) else "fail")


def test_unregistered_plan_makes_budget_incomplete_with_registration_hint():
    book = [dict(HELD[0], legs=[dict(x) for x in HELD[0]["legs"]], net_cash=-40.0, stop_loss=None,
                 payoff_max_loss=None, future_fees=3.2)]
    notes = ra.apply_campaign_cash(book, {"rows": ROLL, "window_start": W0, "plans": {}})
    assert book[0]["net_cash"] is None and any("--campaign-start" in n for n in notes)
    assert rp.evaluate_book(book, {"equity": 900.0}, rp.LIVE_POLICY)["overall"] == "incomplete"


def test_codex063_shared_middle_strike_counted_once_and_id_order_invariant():
    """Codex 063 反例：+110P/−100P 与 −100P/+90P 两行共用 100P；真实总现金 −200、零费蝶式最大亏损 200。
    Codex 067：行 id 改名/排序不改变单元总现金与政策总现金。"""
    e = "2026-11-20"
    s = lambda k: sym(k, exp="261120")
    rows = [flow("2026-10-01T14:00:00Z", s(110), -600.0), flow("2026-10-01T14:00:00Z", s(100), 600.0),
            flow("2026-10-01T14:00:00Z", s(90), -200.0)] + \
           [flow("2026-10-02T10:00:00Z", s(k), 0.0, fee=True) for k in (110, 100, 90)]
    truth = sum(float(r["balance"]) for r in rows)
    totals = set()
    for ida, idb in (("a", "b"), ("z", "b"), ("b", "a")):
        a = {"id": ida, "underlying": "XYZ", "legs": [leg(110, 1, exp=e), leg(100, -1, exp=e)], "net_cash": -300.0,
             "future_fees": 0.0, "stop_loss": None, "payoff_max_loss": None}
        b = {"id": idb, "underlying": "XYZ", "legs": [leg(100, -1, exp=e), leg(90, 1, exp=e)], "net_cash": 100.0,
             "future_fees": 0.0, "stop_loss": None, "payoff_max_loss": None}
        plans = {"live|XYZ|2026-11-20|P|100/110": {"campaign_start": "2026-10-01T14:00:00Z"},
                 "live|XYZ|2026-11-20|P|100/90": {"campaign_start": "2026-10-01T14:00:00Z"}}
        book = [a, b]
        ra.apply_campaign_cash(book, {"rows": rows, "window_start": W0, "plans": plans})
        assert sum(p["net_cash"] for p in book) == pytest.approx(truth)      # 旧实现两行各认领一次 → +400
        totals.add(round(sum(p["net_cash"] for p in book), 2))
        _, rep = rp._net_units(book)
        expected = nr.unit_risk(a["legs"] + b["legs"], net_cash=truth, future_fees=0.0)["max_loss"]
        assert rep[0]["risk"]["max_loss"] == pytest.approx(expected)
    assert totals == {round(truth, 2)}


def test_shared_contract_with_stage_is_incomplete_not_order_dependent():
    e = "2026-11-20"
    s = lambda k: sym(k, exp="261120")
    rows = [flow("2026-10-01T14:00:00Z", s(110), -600.0), flow("2026-10-01T14:00:00Z", s(100), 600.0),
            flow("2026-10-01T14:00:00Z", s(90), -200.0)]
    a = {"id": "a", "legs": [leg(110, 1, exp=e), leg(100, -1, exp=e)]}
    b = {"id": "b", "legs": [leg(100, -1, exp=e), leg(90, 1, exp=e)]}
    stage = {"start": "2026-10-03T14:00:00Z", "inherited": [{"symbol": s(100), "qty": -1, "low": 5, "ref": 5, "high": 5}]}
    for st_id in ("a", "b"):
        r = cc.unit_cash("XYZ", [a, b], rows, window_start=W0, anchors={"a": rows[0]["time"], "b": rows[0]["time"]},
                         stages={st_id: stage})
        assert r["quality"] == "incomplete" and any("多行共同认领" in p for p in r["problems"])


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
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"r": "2026-10-01T14:00:00Z"}, stages={"r": stage},
                     members={"r": {"campaign_members": [s(100)]}})
    assert r["campaign_cash"] == pytest.approx(500.0) and r["exited"] == [s(100)]
    camp = nr.unit_risk(held[0]["legs"], net_cash=r["campaign_cash"], future_fees=0.0)
    assert camp["max_loss"] == pytest.approx(500.0)                         # 到期最坏 −5/股
    st = r["stages"]["r"]
    for k, old, new in (("low", 220, 720), ("ref", 200, 700), ("high", 180, 680)):
        assert st["old_stage"][k] == pytest.approx(old)
        risk = nr.unit_risk(held[0]["legs"], net_cash=st["cash"][k], future_fees=0.0)["max_loss"]
        assert risk == pytest.approx(new) and old - risk == pytest.approx(-500.0)
    assert st["policy_cash"] == pytest.approx(st["cash"]["low"])            # 沿用空头取低价 → 新阶段风险最大，不是「取 ask」


def test_mixed_direction_inherited_legs_take_per_leg_worst_not_synchronous_endpoint():
    """Codex 067：沿用一空一多时，low/ref/high 三个同步端点不覆盖最差组合；最差 = 空头 low + 多头 high。"""
    e, x = "2026-11-20", "261120"
    s = lambda k: sym(k, "P", x)
    rows = [flow("2026-10-01T14:00:00Z", s(100), 500.0), flow("2026-10-01T14:00:00Z", s(95), -300.0),
            flow("2026-10-05T14:00:00Z", s(90), -50.0)] + \
           [flow(f"2026-10-0{d}T10:00:00Z", s(k), 0.0, fee=True) for d, k in ((2, 100), (2, 95), (6, 90))]
    held = [{"id": "r", "legs": [leg(100, -1, exp=e), leg(95, 1, exp=e), leg(90, 1, exp=e)]}]
    stage = {"start": "2026-10-05T14:00:00Z",
             "inherited": [{"symbol": s(100), "qty": -1, "low": 4.0, "ref": 4.5, "high": 5.0},
                           {"symbol": s(95), "qty": 1, "low": 2.0, "ref": 2.5, "high": 3.0}]}
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"r": "2026-10-01T14:00:00Z"}, stages={"r": stage})
    st = r["stages"]["r"]
    real = -50.0
    worst = real + 100 * 4.0 - 100 * 3.0                                    # 空头按 low 卖出、多头按 high 买入
    assert st["policy_cash"] == pytest.approx(worst)
    assert min(st["cash"].values()) > worst                                  # 同步端点（low/low、high/high）都更乐观
    grid = [real + 100 * a - 100 * b for a in (4.0, 4.5, 5.0) for b in (2.0, 2.5, 3.0)]
    assert st["policy_cash"] == pytest.approx(min(grid))


# —— Codex 067 三个反例（probe_067.py）转成回归：修前 verified/bound/通过校验，修后为真实值或显式 incomplete ——

def _x(day, sec, s, cash, fee=False):
    return {"time": f"2026-10-{day:02d}T14:00:{sec:02d}Z", "symbol": s, "balance": str(cash),
            "flow_name": "Option Fee" if fee else "Option Transaction"}


def _s(k, kind="P"):
    return f"XYZ261120{kind}{k * 1000:08d}.US"


def _l(k, q, kind="P"):
    return dict(kind=kind, strike=k, qty=q, expiry="2026-11-20", symbol=_s(k, kind))


def test_codex067_split_order_old_protective_leg_not_dropped():
    """旧保护腿 110P 买入与卖出都和在场腿不同秒：v1 同秒闭包丢掉它的净支出 −300，报 +100 / verified。"""
    x, y, z = _s(110), _s(100), _s(90)
    held = [dict(id="current", legs=[_l(100, -1), _l(90, 1)])]
    tx = [_x(5, 0, x, -600), _x(5, 1, y, 200), _x(6, 0, z, -100), _x(6, 1, x, 300)]
    tx += [_x(7, 0, s, 0, True) for s in (x, y, z)]
    true_cash = sum(float(t["balance"]) for t in tx)                        # −200
    r = cc.unit_cash("XYZ", held, tx, window_start="2026-09-01T00:00:00Z", anchors={"current": tx[0]["time"]})
    assert r["quality"] == "incomplete" and r["campaign_cash"] is None      # 起点登记不能证明成员齐全
    assert any(x in p for p in r["problems"])
    ok = cc.unit_cash("XYZ", held, tx, window_start="2026-09-01T00:00:00Z", anchors={"current": tx[0]["time"]},
                      members={"current": {"campaign_members": [x]}})
    assert ok["quality"] == "verified" and ok["campaign_cash"] == pytest.approx(true_cash)
    ml = nr.unit_risk(held[0]["legs"], net_cash=ok["campaign_cash"], future_fees=0)["max_loss"]
    assert ml == pytest.approx(1200.0)                                      # v1 报 900


def test_codex067_union_of_row_minima_not_reported_as_bound():
    """两行各取最少现金再并集：v1 报 bound −300（真实当前轮 −600）。v2 未登记起点 → incomplete；登记后 = 真实值。"""
    x, y = _s(110, "C"), _s(90)
    rows = [dict(id="A", legs=[_l(110, 1, "C")]), dict(id="B", legs=[_l(90, 1)])]
    tx = [_x(5, 0, x, -1000), _x(5, 0, y, 100), _x(6, 0, x, 1300), _x(7, 0, y, -100),
          _x(8, 0, x, -500), _x(9, 0, y, -100)]
    tx += [_x(12, 0, s, 0, True) for s in (x, y)]
    r = cc.unit_cash("XYZ", rows, tx, window_start="2026-09-01T00:00:00Z")
    assert r["quality"] == "incomplete" and r["campaign_cash"] is None and set(r["candidates"]) == {"A", "B"}
    ok = cc.unit_cash("XYZ", rows, tx, window_start="2026-09-01T00:00:00Z",
                      anchors={"A": tx[4]["time"], "B": tx[5]["time"]})
    assert ok["quality"] == "verified" and ok["campaign_cash"] == pytest.approx(-600.0)


def test_codex067_duplicate_inherited_leg_rejected():
    """同一沿用空头登记两次：v1 计划校验返回 None、政策现金 300（应 100）。"""
    held = [dict(id="current", legs=[_l(100, -1), _l(90, 1)])]
    tx = [_x(5, 0, _s(100), 200), _x(5, 0, _s(90), -100)]
    tx += [_x(6, 0, s, 0, True) for s in (_s(100), _s(90))]
    inherit = dict(symbol=_s(100), qty=-1, low=2, ref=2, high=2)
    stage = dict(start="2026-10-05T14:00:00Z", inherited=[inherit, dict(inherit)])
    assert rw._campaign_fields_problem(dict(stage=stage)) is not None
    r = cc.unit_cash("XYZ", held, tx, window_start="2026-09-01T00:00:00Z", anchors={"current": tx[0]["time"]},
                     stages={"current": stage})
    assert r["quality"] == "incomplete" and any("不止一次" in p for p in r["problems"])
    assert r["policy_cash_by_row"].get("current") != 300


def test_inherited_leg_must_exist_before_stage_and_new_leg_must_be_new():
    held = [dict(id="h", legs=[_l(100, -1), _l(90, 1)])]
    tx = [_x(5, 0, _s(100), 200), _x(8, 0, _s(90), -100)]
    late = dict(start="2026-10-04T14:00:00Z", inherited=[dict(symbol=_s(100), qty=-1, low=2, ref=2, high=2)])
    r = cc.unit_cash("XYZ", held, tx, window_start="2026-09-01T00:00:00Z", anchors={"h": tx[0]["time"]},
                     stages={"h": late})
    assert r["quality"] == "incomplete" and any("该时点已存在" in p for p in r["problems"])
    early = dict(start="2026-10-09T14:00:00Z", inherited=[dict(symbol=_s(100), qty=-1, low=2, ref=2, high=2)])
    r2 = cc.unit_cash("XYZ", held, tx, window_start="2026-09-01T00:00:00Z", anchors={"h": tx[0]["time"]},
                      stages={"h": early})
    assert r2["quality"] == "incomplete" and any("不是新阶段的新腿" in p for p in r2["problems"])


def test_inherited_contract_held_by_two_rows_cannot_be_inherited_twice():
    """同一合约两行都持有、两行都登记沿用它（每条都等于本行张数）：会重复计虚拟收入 → 共认领 + 新阶段 → incomplete。"""
    e = "2026-11-20"
    s = lambda k: sym(k, exp="261120")
    a = {"id": "a", "legs": [leg(100, -1, exp=e), leg(90, 1, exp=e)]}
    b = {"id": "b", "legs": [leg(100, -1, exp=e), leg(85, 1, exp=e)]}
    rows = [flow("2026-10-01T14:00:00Z", s(100), 300.0), flow("2026-10-01T14:00:00Z", s(100), 300.0),
            flow("2026-10-06T14:00:00Z", s(90), -100.0), flow("2026-10-06T14:00:00Z", s(85), -60.0)]
    stg = {"start": "2026-10-06T14:00:00Z", "inherited": [{"symbol": s(100), "qty": -1, "low": 2, "ref": 2, "high": 2}]}
    r = cc.unit_cash("XYZ", [a, b], rows, window_start=W0,
                     anchors={"a": "2026-10-01T14:00:00Z", "b": "2026-10-01T14:00:00Z"}, stages={"a": stg, "b": stg})
    assert r["quality"] == "incomplete" and any("多行共同认领" in p for p in r["problems"])


def test_same_second_unrelated_trade_does_not_join_campaign():
    """同秒成交的另一到期独立交易：v1 同秒闭包会把它并入；v2 不登记就 incomplete，登记独立后不计入。"""
    s50, s48, o50 = sym(50), sym(48), sym(50, exp="260417")
    held = [{"id": "h", "legs": [leg(48, 1), leg(50, -1)]}]
    rows = [flow("2026-03-02T15:00:00Z", s50, 200.0), flow("2026-03-02T15:00:00Z", s48, -120.0),
            flow("2026-03-02T15:00:00Z", o50, 900.0), flow("2026-03-04T15:00:00Z", o50, -100.0)]
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"h": rows[0]["time"]})
    assert r["quality"] == "incomplete" and any(o50 in p for p in r["problems"])
    ok = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"h": rows[0]["time"]},
                      members={"h": {"campaign_independent": [o50]}})
    assert ok["campaign_cash"] == pytest.approx(200.0 - 120.0 - 2 * 1 * cc.FEE_PER_CONTRACT)   # 两笔费用未入账 → 估计


def test_same_contract_closed_then_reopened_anchor_excludes_earlier_round():
    """同合约更早一轮已平、后重开：起点登记在重开处 → 早轮不计；早轮在起点前，不需要登记独立。"""
    s50 = sym(50)
    held = [{"id": "h", "underlying": "XYZ", "legs": [leg(50, 1)]}]
    for first_round in ((-100.0, 150.0), (-100.0, 50.0)):                  # 早轮赚钱 / 早轮亏钱
        rows = [flow("2026-09-10T14:00:00Z", s50, first_round[0]), flow("2026-09-12T14:00:00Z", s50, first_round[1]),
                flow("2026-10-01T14:00:00Z", s50, -200.0)] + \
               [flow(t, s50, 0.0, fee=True) for t in ("2026-09-11T10:00:00Z", "2026-09-13T10:00:00Z", "2026-10-02T10:00:00Z")]
        r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors={"h": "2026-10-01T14:00:00Z"})
        assert r["quality"] == "verified" and r["campaign_cash"] == pytest.approx(-200.0)
        assert cc.unit_cash("XYZ", held, rows, window_start=W0)["quality"] == "incomplete"   # 没登记不再给「保守界」


@pytest.mark.parametrize("mutate,why", [
    (lambda rows: rows.__setitem__(0, dict(rows[0], balance=None)), "金额"),
    (lambda rows: rows.__setitem__(0, dict(rows[0], time="2026-03-02 15:01:07")), "时刻"),
])
def test_bad_rows_make_unit_incomplete(mutate, why):
    rows = [dict(x) for x in ROLL]
    mutate(rows)
    r = cc.unit_cash("XYZ", HELD, rows, window_start=W0, anchors=ANCHOR, members=MEMBERS)
    assert r["quality"] == "incomplete" and any(why in p for p in r["problems"]) and r["campaign_cash"] is None


def test_window_coverage_and_anchor_problems():
    late = cc.unit_cash("XYZ", HELD, ROLL, window_start="2026-03-05T00:00:00+00:00", anchors=ANCHOR, members=MEMBERS)
    assert late["quality"] == "incomplete" and any(P30 in p for p in late["problems"])     # 30P 建仓在窗口外
    early = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors={"xyz0320": "2026-01-15T00:00:00Z"}, members=MEMBERS)
    assert early["quality"] == "incomplete" and any("早于流水窗口" in p for p in early["problems"])
    after = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors={"xyz0320": "2026-03-10T00:00:00Z"}, members=MEMBERS)
    assert after["quality"] == "incomplete" and any("登记晚了" in p for p in after["problems"])
    ghost = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors=ANCHOR,
                         members={"xyz0320": {"campaign_members": [P32, sym(26)], "campaign_independent": [Q30, Q28]}})
    assert ghost["quality"] == "incomplete" and any("没有成交" in p and sym(26) in p for p in ghost["problems"])


def test_pending_fees_estimated_then_replaced_by_actual():
    s50 = sym(50)
    held = [{"id": "h", "underlying": "XYZ", "legs": [leg(50, -2)]}]
    rows = [flow("2026-10-01T14:00:00Z", s50, 200.0)]
    a = {"h": rows[0]["time"]}
    r = cc.unit_cash("XYZ", held, rows, window_start=W0, anchors=a)
    assert r["fee_estimate"] == pytest.approx(2 * cc.FEE_PER_CONTRACT) and r["pending_fee_rows"] == 1
    assert r["campaign_cash"] == pytest.approx(200.0 - 2 * cc.FEE_PER_CONTRACT)
    r2 = cc.unit_cash("XYZ", held, rows + [flow("2026-10-02T10:00:00Z", s50, -1.61, fee=True)], window_start=W0, anchors=a)
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
    base = {"schema": rw.PLAN_SCHEMA, "plans": {"k": {}}}
    ok = {"campaign_start": "2026-03-02T15:01:07Z", "stage": STAGE, **MEMBERS["xyz0320"]}
    assert rw.validate_plans(dict(base, plans={"k": ok})) is None
    assert "campaign_start" in rw.validate_plans(dict(base, plans={"k": {"campaign_start": "2026-03-02 15:01"}}))
    bad = {"stage": dict(STAGE, inherited=[dict(STAGE["inherited"][0], low=2.0)])}
    assert "low ≤ ref ≤ high" in rw.validate_plans(dict(base, plans={"k": bad}))
    assert "重复" in rw.validate_plans(dict(base, plans={"k": {"campaign_members": [P32, P32.replace(".US", "")]}}))
    assert "既是成员又是独立" in rw.validate_plans(dict(base, plans={"k": {"campaign_members": [P32],
                                                                          "campaign_independent": [P32]}}))
    assert "代码列表" in rw.validate_plans(dict(base, plans={"k": {"campaign_members": ["SLV"]}}))


def test_stage_leg_must_match_holding():
    st = dict(STAGE, inherited=[dict(STAGE["inherited"][0], qty=-1)])
    r = cc.unit_cash("XYZ", HELD, ROLL, window_start=W0, anchors=ANCHOR, stages={"xyz0320": st}, members=MEMBERS)
    assert r["quality"] == "incomplete" and any("不符" in p for p in r["problems"])
