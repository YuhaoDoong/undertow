"""Codex 069 B3：未入账费用按逐笔成交的真实张数估计，费用按方向覆盖，成员/费用质量分列（campaign-cash v3）。
全部合成数据（虚构标的 XYZ、虚构日期与金额、合成 R），不读账户、不联网。

v2 的错：未入账费用 = 当前在场最大张数 × 待入账流水行数。部分减仓后会低估（先买 10 后卖 9、剩 1：模型费 15.2 被估成 1.6），
本该超出计划止损上限的单元按 192.4 通过 200，且仍标 verified。"""
import random

import pytest

from undertow.analyze import campaign_cash as cc
from undertow.analyze import net_risk as nr
from undertow.analyze import risk_aggregate as ra
from undertow.analyze import trial_reconcile as tr

W0 = "2026-02-01T00:00:00+00:00"
C50 = "XYZ260320C00050000"


def cash(code, side, amt, t):
    return {"balance": f"{amt:.2f}", "currency": "USD", "flow_name": f"Option {side.capitalize()} Transaction",
            "symbol": f"{code}.US", "time": t}


def fee(code, side, amt, t):
    return {"balance": f"{amt:.2f}", "currency": "USD", "flow_name": f"Option {side.capitalize()} Fee",
            "symbol": f"{code}.US", "time": t}


def ex(order, sym, side, qty, px, t):
    return {"order_id": order, "price": f"{px}", "quantity": f"{qty}", "side": side.capitalize(), "symbol": sym, "time": t}


HELD1 = [{"id": "h", "underlying": "XYZ", "legs": [{"kind": "C", "strike": 50.0, "expiry": "2026-03-20", "qty": 1,
                                                      "symbol": f"{C50}.US"}]}]
T_BUY, T_SELL = "2026-03-02T15:00:00Z", "2026-03-03T15:00:00Z"
# 先买 10 张 @1.90（−1,900），后卖 9 张 @1.90（+1,710），剩 1 张；两笔费用都未入账
ROWS_069 = [cash(C50, "buy", -1900.0, T_BUY), cash(C50, "sell", 1710.0, T_SELL)]
EXS_069 = [ex("o1", f"{C50}.US", "buy", 10, 1.90, T_BUY), ex("o2", f"{C50}.US", "sell", 9, 1.90, T_SELL)]
ANCHOR = {"h": T_BUY}


def test_codex069_partial_reduction_uses_fill_quantities_not_current_size():
    q = tr.flow_quantities(ROWS_069, EXS_069)["qty"]
    assert q == {0: 10, 1: 9}
    r = cc.unit_cash("XYZ", HELD1, ROWS_069, window_start=W0, anchors=ANCHOR, quantities=q)
    assert r["quality"] == "verified" and r["fee_quality"] == "estimated" and r["membership_quality"] == "registered-private"
    assert r["pending_fee_qty"] == 19 and r["fee_estimate"] == pytest.approx(19 * cc.FEE_PER_CONTRACT)   # 15.20，不是 1.60
    assert r["campaign_cash"] == pytest.approx(-190.0 - 15.2)
    ml = nr.unit_risk(HELD1[0]["legs"], net_cash=r["campaign_cash"], future_fees=1 * cc.FEE_PER_CONTRACT)["max_loss"]
    assert ml == pytest.approx(206.0)
    cap = max(0.15 * 1000, 200.0)                       # 合成 R=1,000 时的计划止损上限
    assert ml > cap                                      # 正确口径不通过；v2 的 192.4 会通过
    v2_wrong = -190.0 - 2 * 1 * cc.FEE_PER_CONTRACT
    assert -v2_wrong + cc.FEE_PER_CONTRACT == pytest.approx(192.4) and -v2_wrong + cc.FEE_PER_CONTRACT < cap


def test_pending_fee_without_quantity_is_incomplete_and_visible():
    """张数未知 → 没有可证明的费用上界 → incomplete，不退回当前张数、不按每行 1 张。"""
    for q, note in ((None, "成交明细读取失败：X"), ({0: 10}, None), ({}, None)):
        r = cc.unit_cash("XYZ", HELD1, ROWS_069, window_start=W0, anchors=ANCHOR, quantities=q, quantity_note=note)
        assert r["quality"] == "incomplete" and r["campaign_cash"] is None and r["fee_quality"] == "unknown"
        assert any("张数未知" in p for p in r["problems"])
        if note:
            assert any(note in p for p in r["problems"])


def test_quantities_irrelevant_once_fees_posted():
    rows = ROWS_069 + [fee(C50, "buy", -8.10, "2026-03-03T10:30:00Z"), fee(C50, "sell", -7.30, "2026-03-04T10:30:00Z")]
    r = cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors=ANCHOR)
    assert r["quality"] == "verified" and r["fee_quality"] == "actual" and r["fee_estimate"] == 0
    assert r["campaign_cash"] == pytest.approx(-190.0 - 8.10 - 7.30)


def test_buy_and_sell_fees_posted_same_instant_both_counted():
    """v2 不分方向：同一时刻入账的买费与卖费，第二条覆盖不到任何成交 → 被当「找不到成交」丢掉（低估费用）。"""
    t_fee = "2026-03-03T10:30:00Z"
    rows = [cash(C50, "buy", -380.0, "2026-03-02T14:00:00Z"), cash(C50, "sell", 190.0, "2026-03-02T15:00:00Z"),
            fee(C50, "buy", -1.62, t_fee), fee(C50, "sell", -0.81, t_fee)]
    r = cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors={"h": rows[0]["time"]})
    assert r["quality"] == "verified" and r["fee_quality"] == "actual"
    assert r["campaign_cash"] == pytest.approx(-380.0 + 190.0 - 1.62 - 0.81)
    assert not any("找不到对应成交" in n for n in r["notes"])


def test_one_side_fee_posted_other_side_estimated_by_its_own_quantity():
    rows = ROWS_069 + [fee(C50, "buy", -8.10, "2026-03-03T10:30:00Z")]           # 买费已到，卖费（9 张）未到
    r = cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors=ANCHOR, quantities={1: 9})
    assert r["quality"] == "verified" and r["pending_fee_qty"] == 9 and r["pending_fee_rows"] == 1
    assert r["campaign_cash"] == pytest.approx(-190.0 - 8.10 - 9 * cc.FEE_PER_CONTRACT)


def test_sided_fee_does_not_cover_opposite_side_trade():
    """卖费不能顶替买入的费用：只有卖费入账时，买入那笔仍算未入账（要张数）。"""
    rows = ROWS_069 + [fee(C50, "sell", -7.30, "2026-03-04T10:30:00Z")]
    bad = cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors=ANCHOR)
    assert bad["quality"] == "incomplete" and any("张数未知" in p for p in bad["problems"])
    ok = cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors=ANCHOR, quantities={0: 10})
    assert ok["campaign_cash"] == pytest.approx(-190.0 - 7.30 - 10 * cc.FEE_PER_CONTRACT)


def test_result_is_idempotent_and_row_order_invariant():
    rows = ROWS_069 + [fee(C50, "buy", -8.10, "2026-03-03T10:30:00Z")]
    exs = EXS_069
    base = cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors=ANCHOR, quantities=tr.flow_quantities(rows, exs)["qty"])
    assert base == cc.unit_cash("XYZ", HELD1, rows, window_start=W0, anchors=ANCHOR,
                                quantities=tr.flow_quantities(rows, exs)["qty"])
    rng = random.Random(69)
    for _ in range(5):
        perm = rows[:]
        rng.shuffle(perm)
        ex2 = exs[:]
        rng.shuffle(ex2)
        r = cc.unit_cash("XYZ", HELD1, perm, window_start=W0, anchors=ANCHOR, quantities=tr.flow_quantities(perm, ex2)["qty"])
        assert r["campaign_cash"] == pytest.approx(base["campaign_cash"]) and r["pending_fee_qty"] == base["pending_fee_qty"]


def test_flow_quantities_combo_partial_fills_and_unmatched():
    """组合单（成交 symbol 为标的、净价行 + 各腿行）、同一订单分笔成交合并成一条流水、配不上的流水不给张数。"""
    L, S = "XYZ260320C00048000", "XYZ260320C00052000"
    t = "2026-03-05T15:00:01Z"
    rows = [cash(L, "buy", -3.10 * 3 * 100, t), cash(S, "sell", 1.20 * 3 * 100, t),
            cash(C50, "buy", -1.50 * 7 * 100, "2026-03-06T15:00:00Z"),
            cash(C50, "sell", 99.0, "2026-03-07T15:00:00Z")]                          # 没有对应成交
    exs = [ex("c1", "XYZ.US", "buy", 3, 1.90, t), ex("c1", "XYZ.US", "buy", 3, 3.10, t), ex("c1", "XYZ.US", "buy", 3, 1.20, t),
           ex("s1", f"{C50}.US", "buy", 4, 1.50, "2026-03-06T15:00:00Z"),
           ex("s1", f"{C50}.US", "buy", 3, 1.50, "2026-03-06T15:00:01Z")]
    fq = tr.flow_quantities(rows, exs)
    assert fq["qty"] == {0: 3, 1: 3, 2: 7}
    assert 3 not in fq["qty"] and fq["problems"]


def test_apply_campaign_cash_reads_executions_or_reports_why_not():
    """入口层：给成交明细 → 配张数；读失败 / 没读 → 张数未知、净现金 None，原因进提示。（计划键只认垂直价差，故用价差行。）"""
    L, S = "XYZ260320C00048000", "XYZ260320C00052000"
    t = "2026-03-05T15:00:01Z"
    rows = [cash(L, "buy", -3.10 * 10 * 100, t), cash(S, "sell", 1.20 * 10 * 100, t),
            cash(L, "sell", 3.10 * 9 * 100, "2026-03-06T15:00:01Z"), cash(S, "buy", -1.20 * 9 * 100, "2026-03-06T15:00:01Z")]
    exs = [ex("c1", "XYZ.US", "buy", 10, 1.90, t), ex("c1", "XYZ.US", "buy", 10, 3.10, t), ex("c1", "XYZ.US", "buy", 10, 1.20, t),
           ex("c2", "XYZ.US", "sell", 9, 1.90, "2026-03-06T15:00:01Z"), ex("c2", "XYZ.US", "sell", 9, 3.10, "2026-03-06T15:00:01Z"),
           ex("c2", "XYZ.US", "sell", 9, 1.20, "2026-03-06T15:00:01Z")]
    legs = [{"kind": "C", "strike": 48.0, "expiry": "2026-03-20", "qty": 1, "symbol": f"{L}.US"},
            {"kind": "C", "strike": 52.0, "expiry": "2026-03-20", "qty": -1, "symbol": f"{S}.US"}]
    key = cc.plan_key_for_legs("XYZ", legs)
    plans = {key: {"campaign_start": t}}
    base = {"rows": rows, "window_start": W0, "plans": plans}
    b1 = [{"id": "v", "underlying": "XYZ", "legs": [dict(x) for x in legs]}]
    ra.apply_campaign_cash(b1, dict(base, executions=exs, executions_since="2026-03-01"))
    d = b1[0]["cash_detail"]
    assert d["quality"] == "verified" and d["pending_fee_qty"] == 38
    assert b1[0]["net_cash"] == pytest.approx(-190.0 - 38 * cc.FEE_PER_CONTRACT)
    for extra, why in (({"executions_error": "boom"}, "成交明细读取失败：boom"), ({}, "没有读取成交明细")):
        b2 = [{"id": "v", "underlying": "XYZ", "legs": [dict(x) for x in legs]}]
        notes = ra.apply_campaign_cash(b2, dict(base, **extra))
        assert b2[0]["net_cash"] is None and b2[0]["cash_detail"]["fee_quality"] == "unknown"
        assert any(why in n for n in notes)
