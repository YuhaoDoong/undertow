"""Codex 008 G03：缺短腿、裸卖、日历价差、缺 Greek 的组合 —— 汇总与限额都必须显示未完成，不通过。"""
from datetime import date
from types import SimpleNamespace as NS

from undertow.analyze.risk_aggregate import aggregate, render_md


def _c(label, max_loss, cap, legs=2, defined=True, credit=None, expiry="2026-10-02"):
    lg = [NS(symbol=f"SLV{expiry}{label}{i}", qty=(-1 if i == 0 else 1)) for i in range(legs)]   # 合约身份（Codex 044 P1-1）
    return NS(label=label, max_loss=max_loss, capital_at_risk=cap, legs=lg,
              defined_risk=defined, net_credit=credit, qty=1, max_profit=None, stance="", note="",
              expiry_label=expiry, underlying="SLV")


def _g(combos, delta=10.0, incomplete=None):
    return NS(underlying="SLV", combos=combos, net_delta=delta, net_gamma=None, net_theta=None,
              incomplete=incomplete or {})


def _rev(groups, unmapped=()):
    return NS(groups=groups, unmapped=list(unmapped))


CAP = NS(net_assets=1000.0)


def test_complete_defined_risk_passes():
    a = aggregate(_rev([_g([_c("熊市看涨价差", 80.0, 80.0, credit=0.2)])]), CAP, asof=date(2026, 9, 28))
    assert a["overall"] == "pass" and a["totals"]["max_loss"]["value"] == 80.0


def test_naked_call_is_unbounded_and_incomplete():
    a = aggregate(_rev([_g([_c("单腿卖call 60", None, None, legs=1, defined=False)])]), CAP, asof="d")
    t = a["totals"]["max_loss"]
    assert t["value"] is None and t["unbounded"] and a["overall"] == "incomplete"
    assert "未完成核查" in render_md(a) and "无上限" in render_md(a)


def test_calendar_unknown_is_incomplete_not_zero():
    a = aggregate(_rev([_g([_c("熊市看涨价差", 80.0, 80.0, credit=0.2),
                           _c("日历价差(卖近买远)", None, None, defined=False)])]), CAP, asof="d")
    t = a["totals"]["max_loss"]
    assert t["value"] is None and t["known_part"] == 80.0 and a["overall"] == "incomplete"


def test_unmapped_leg_missing_short_is_incomplete():
    a = aggregate(_rev([_g([_c("熊市看涨价差", 80.0, 80.0, credit=0.2)])], unmapped=[NS(name="XYZ 空头")]),
                  CAP, asof="d")
    assert a["overall"] == "incomplete" and "XYZ 空头" in a["totals"]["max_loss"]["missing"]


def test_capital_unknown_never_passes():
    a = aggregate(_rev([_g([_c("熊市看涨价差", 80.0, 80.0, credit=0.2)])]), None, asof="d")
    assert a["overall"] == "incomplete"


def test_over_limit_fails_even_if_others_unknown():
    a = aggregate(_rev([_g([_c("熊市看涨价差", 300.0, 300.0, credit=0.2),
                           _c("日历价差(卖近买远)", None, None, defined=False)])]), CAP, asof="d")
    assert a["overall"] == "fail"


def test_missing_greek_kept_signed_and_flagged():
    a = aggregate(_rev([_g([_c("熊市看涨价差", 80.0, 80.0, credit=0.2)], delta=None,
                           incomplete={"pos_delta": ["SLV 57P"]})]), CAP, asof="d")
    assert a["greeks"]["SLV"]["delta"] is None and a["greeks"]["SLV"]["incomplete"]


def test_group_greeks_none_when_leg_missing():
    from undertow.analyze.portfolio import complete_sum
    legs = [NS(name="a", pos_delta=10.0), NS(name="b", pos_delta=None)]
    assert complete_sum(legs, "pos_delta") == (None, ["b"])
    assert complete_sum([NS(name="a", pos_delta=-5.0)], "pos_delta") == (-5.0, [])


def _hg(combos):
    return NS(underlying="SLV", display_name="白银", combos=combos, legs=[], bias="偏多", net_delta=None,
              net_gamma=None, net_theta=None, spot=58.0)


def _codes(g, cap):
    from undertow.analyze.healthcheck import check_group
    try:
        return {f.code: f for f in check_group(g, cap)}
    except AttributeError as e:                 # 夹具缺字段时让它显式失败，不静默跳过
        raise AssertionError(f"夹具缺字段：{e}")


def test_healthcheck_concentration_unknown_and_zero_net():
    cal = _c("日历价差(卖近买远)", None, None, defined=False)
    spread = _c("熊市看涨价差", 80.0, 80.0, credit=0.2)
    f = _codes(_hg([spread, cal]), NS(net_assets=1000.0, buy_power=1000.0))
    assert "CONCENTRATION_UNKNOWN" in f, "未知风险不得折成 0 后静默通过"
    f0 = _codes(_hg([spread]), NS(net_assets=0.0, buy_power=0.0))
    assert f0["CONCENTRATION"].severity == "高" and "净资产 ≤0" in f0["CONCENTRATION"].title
    fh = _codes(_hg([_c("熊市看涨价差", 600.0, 600.0, credit=0.2), cal]), NS(net_assets=1000.0, buy_power=1000.0))
    assert "实际更高" in fh["CONCENTRATION"].detail


# —— Codex 044 包 A 端到端：真实 Combo/PositionReview 格式 → aggregate → 候选预算 ——

def _leg(sym, kind, strike, exp, qty, cost):
    from undertow.analyze.portfolio import PositionReview
    return PositionReview(symbol=sym, name=sym, underlying="GLD", kind=kind, side="", qty=qty, strike=strike,
                          expiry=date.fromisoformat(exp), dte=None, cost_price=cost, est_value=None, pnl=None,
                          pos_delta=None, moneyness="", dist_pct=None, wall_note="", align="")


def _bear_call(exp, ks=400, kb=402):
    from undertow.analyze.portfolio import Combo
    tag = exp.replace("-", "")[2:]
    return Combo(underlying="GLD", expiry_label=exp, label="熊市看涨价差(收权金)", stance="保守做空",
                 legs=[_leg(f"GLD{tag}C{ks}000.US", "C", ks, exp, -1, 1.0),
                       _leg(f"GLD{tag}C{kb}000.US", "C", kb, exp, 1, 0.2)],
                 qty=1, net_credit=0.8, max_profit=80.0, max_loss=120.0, defined_risk=True, capital_at_risk=120.0, note="")


def _gld(combos):
    return NS(underlying="GLD", combos=combos, net_delta=0.0, net_gamma=0.0, net_theta=0.0, incomplete={})


def test_same_label_different_expiries_are_three_positions():
    """044 反例：三笔 GLD 熊市看涨价差，到期 10/16、10/23、10/30，每笔含费 123.20。旧版 id=标的·策略名 → 只算 123.20、pass。"""
    from undertow.analyze import risk_policy as rp
    a = aggregate(_rev([_gld([_bear_call(d) for d in ("2026-10-16", "2026-10-23", "2026-10-30")])]),
                  NS(net_assets=500.0), asof="2026-10-14", policy=rp.POLICY_B)
    assert len({b["id"] for b in a["book"]}) == 3
    mc = next(c for c in a["checks"] if c["item"] == "类别合计（metals）")
    assert mc["value"] == 369.6 and mc["status"] == "fail" and a["overall"] == "fail"
    r = rp.evaluate_budget(candidate={"id": "new", "underlying": "SLV", "unit_stop_loss": 10, "unit_payoff_max_loss": 10,
                                      "qty_requested": 1}, open_positions=a["book"], capital={"equity": 500.0},
                           pause={"latched": False})
    assert r["allowed_units"] == 0                                         # 整本账已超金银上限 → 新增冻结


def test_same_label_two_big_positions_count_two():
    from undertow.analyze import risk_policy as rp
    big = [_bear_call("2026-10-16", 400, 402), _bear_call("2026-10-16", 405, 407)]
    big = [c.__class__(**{**c.__dict__, "max_loss": 160.0, "net_credit": 0.4}) for c in big]
    a = aggregate(_rev([_gld(big)]), NS(net_assets=5000.0), asof="d", policy=rp.POLICY_B)
    bc = next(c for c in a["checks"] if c["item"] == "大仓数量")
    assert bc["value"] == 2 and bc["status"] == "fail"


def test_leg_without_identity_is_incomplete_not_merged():
    from undertow.analyze import risk_policy as rp
    a = aggregate(_rev([_g([_c("熊市看涨价差", 80.0, 80.0, credit=0.2)])]), CAP, asof="d", policy=rp.POLICY_B)
    assert a["overall"] == "pass"
    anon = NS(**{**_c("熊市看涨价差", 80.0, 80.0, credit=0.2).__dict__, "legs": [object(), object()]})
    a = aggregate(_rev([_g([anon, anon])]), CAP, asof="d", policy=rp.POLICY_B)
    assert a["overall"] == "incomplete" and all(b["id"] is None for b in a["book"])
    mc = next(c for c in a["checks"] if c["item"] == "类别合计（metals）")
    assert mc["status"] == "incomplete"                                    # 合计缺成员：不显示 pass
