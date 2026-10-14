"""spread_econ：价差经济量唯一来源（Codex 043 WP1 验收）。"""
import math

import pytest

from undertow.analyze import spread_econ as se


def test_w5_c270_payoff_vs_2c_liquidation_not_truncated():
    e = se.vertical(side="C", k_sell=380, k_buy=385, entry_credit=2.70, fee_round_trip=3.2,
                    stop_rule={"kind": "credit_multiple", "mult": 2.0})
    assert e["complete"] and e["structure"] == "credit"
    assert e["entry_net_payoff_max_loss"] == 233.2 and e["entry_net_max_gain"] == 266.8
    assert e["planned_stop_loss_usd"] == 273.2 and e["stop_value"] == 5.4      # 不再被截成 230
    assert e["stop_exceeds_payoff_max_loss"] is True


def test_w4_credit_range_for_200_300_caps_and_w5_empty():
    assert se.credit_range_for_stop_cap(width=4, stop_mult=2, cap_usd=200, max_loss_cap_usd=300, fee_round_trip=3.2) == (1.032, 1.968)
    assert se.credit_range_for_stop_cap(width=5, stop_mult=2, cap_usd=200, max_loss_cap_usd=300, fee_round_trip=3.2) is None
    for c in (1.032, 1.968):                                                     # 端点相等时两条都恰好满足
        e = se.vertical(side="C", k_sell=380, k_buy=384, entry_credit=c, fee_round_trip=3.2,
                        stop_rule={"kind": "credit_multiple", "mult": 2.0})
        assert e["planned_stop_loss_usd"] <= 200 + 1e-9 and e["entry_net_payoff_max_loss"] <= 300 + 1e-9


def test_debit_and_qty_and_usd_stop():
    e = se.vertical(side="C", k_sell=154, k_buy=144, entry_credit=-4.2, qty=2, fee_round_trip=3.2,
                    stop_rule={"kind": "usd", "amount": 200})
    assert e["structure"] == "debit"
    assert e["entry_net_payoff_max_loss"] == 846.4 and e["entry_net_max_gain"] == 1153.6
    assert e["planned_stop_loss_usd"] == 200                                    # 整笔，不是每组
    assert se.loss_at_value(e, e["stop_value"]) == pytest.approx(200)
    assert se.remaining_loss_from_current_value(e, -5.0) == 1000.0              # 价值 5 → 归零，2 组
    assert se.remaining_loss_from_current_value(e, 0.5) is None                 # 借记价值 > 0：报价不成立


def test_zero_pnl_and_legal_zero_fee_and_missing_fee():
    e = se.vertical(side="P", k_sell=375, k_buy=373, entry_credit=0.41, fee_round_trip=0.0)
    assert e["complete"] and e["entry_net_max_gain"] == 41.0                     # 0 费用是合法值，不当缺失
    assert se.loss_at_value(e, 0.41) == 0.0
    m = se.vertical(side="P", k_sell=375, k_buy=373, entry_credit=0.41, fee_round_trip=None)
    assert not m["complete"] and m["entry_net_max_gain"] is None and m["entry_net_payoff_max_loss"] is None
    assert se.loss_at_value(m, 0.2) is None


@pytest.mark.parametrize("kw", [dict(entry_credit=math.nan), dict(entry_credit=-0.4), dict(entry_credit=2.5),
                                dict(qty=0), dict(qty=1.5), dict(k_buy=375), dict(side="X")])
def test_invalid_inputs_are_incomplete_not_zero(kw):
    args = dict(side="P", k_sell=375, k_buy=373, entry_credit=0.41, fee_round_trip=3.2, qty=1) | kw
    e = se.vertical(**args)
    assert e["complete"] is False and e["entry_net_max_gain"] is None and e["entry_net_payoff_max_loss"] is None


def test_stop_rule_mismatch_reported():
    e = se.vertical(side="C", k_sell=154, k_buy=144, entry_credit=-4.2, fee_round_trip=3.2,
                    stop_rule={"kind": "credit_multiple", "mult": 2.0})
    assert not e["complete"] and "stop_rule" in e["error"] and e["planned_stop_loss_usd"] is None


def test_paper_economics_delegates_to_single_source():
    from scripts import paper_trades as pt
    for args in (("C", 380, 385, 2.7, 1, 3.2), ("C", 154, 144, -4.2, 2, 3.2), ("P", 54, 50, -1.14, 1, 3.2)):
        side, ks, kb, c, q, f = args
        e, s = pt.economics(*args), se.vertical(side=side, k_sell=ks, k_buy=kb, entry_credit=c, qty=q, fee_round_trip=f)
        assert e["max_gain_usd"] == s["entry_net_max_gain"] and e["max_loss_usd"] == s["entry_net_payoff_max_loss"]
        assert e["breakeven"] == s["breakeven"]
