"""Codex 008 G03：缺短腿、裸卖、日历价差、缺 Greek 的组合 —— 汇总与限额都必须显示未完成，不通过。"""
from datetime import date
from types import SimpleNamespace as NS

from undertow.analyze.risk_aggregate import aggregate, render_md


def _c(label, max_loss, cap, legs=2, defined=True, credit=None):
    return NS(label=label, max_loss=max_loss, capital_at_risk=cap, legs=[object()] * legs,
              defined_risk=defined, net_credit=credit, qty=1, max_profit=None, stance="", note="",
              expiry_label="2026-10-02", underlying="SLV")


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
