"""交易灵魂档案的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：档案读写往返、限额对持仓的确定性核查、无档案时优雅降级。
"""
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.portfolio import review_portfolio, InstrumentContext, AccountCapital
from undertow.soul.profile import (SoulProfile, Rule, Weakness, Lesson, Limits,
                                   load_profile, save_profile, check_against_profile,
                                   render_profile_md)


@dataclass
class _Pos:
    symbol: str
    name: str
    quantity: float
    cost_price: float


def _ctx(spot=61.2):
    return InstrumentContext(
        etf_symbol="SLV", display_name="白银 Silver (COMEX)", spot=spot,
        call_wall=70.0, put_wall=55.0, zero_gamma=None,
        bias="偏多", near_bias="中性", mid_bias="偏多", verdict_head="",
        proxy_quality="good", greeks=None)


def _profile(**over):
    lim = Limits(**{"max_risk_per_trade_pct": 10.0, "max_concentration_pct": 40.0,
                    "min_rr": 1.0, "min_seller_edge_pp": 10.0, "min_dte_hold_short": 7,
                    "max_loss_per_trade_pct": 20.0,
                    "forbid_liquidation_risk": True, **over})
    return SoulProfile(updated="2026-08-25", owner="t", phase="重建期",
                       north_star="守规则优先于赚倍数",
                       rules=[Rule(id="rr_gate", text="R:R<1 不做", why="历史教训")],
                       weaknesses=[Weakness(id="get_even", name="回本心态")],
                       lessons=[Lesson(when="2026-07", what="逆势加注", outcome="爆仓",
                                       lesson="题材失效要出")],
                       limits=lim)


def _wheel():
    return [_Pos("SLV260826P61000.US", "SLV 61 Put", -4, 0.46),
            _Pos("SLV260826P60000.US", "SLV 60 Put", 4, 0.27)]


def test_roundtrip_save_load():
    """档案写盘再读回，字段不丢。"""
    p = _profile()
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "profile.json"
        save_profile(p, fp)
        got = load_profile(fp)
    assert got is not None and got.ok
    assert got.north_star == p.north_star
    assert got.limits.max_risk_per_trade_pct == 10.0
    assert got.rules[0].id == "rr_gate" and got.weaknesses[0].name == "回本心态"
    assert got.lessons[0].outcome == "爆仓"
    print("PASS test_roundtrip_save_load")


def test_missing_profile_returns_none():
    """无档案 → None，渲染也不崩。"""
    with tempfile.TemporaryDirectory() as d:
        assert load_profile(Path(d) / "nope.json") is None
    assert "尚未建立" in render_profile_md(None)
    print("PASS test_missing_profile_returns_none")


def test_violations_detected():
    """小账户 + 大仓位 → 集中度/单笔风险/盈亏比/近到期空头腿 全部命中。"""
    cap = AccountCapital(buy_power=6.0, net_assets=630.0, cash_usd=6.0)
    rv = review_portfolio(_wheel(), {"SLV": _ctx()}, asof=date(2026, 8, 25), capital=cap)
    vios = check_against_profile(rv, cap, _profile())
    ids = {v.rule_id for v in vios}
    assert "max_concentration_pct" in ids, ids
    assert "max_risk_per_trade_pct" in ids, ids
    assert "min_seller_edge_pp" in ids, ids   # 卖方结构走胜率边际闸门，不走 R:R
    assert "min_dte_hold_short" in ids, ids       # 61P 剩 1 天 < 7
    assert any(v.severity == "违反铁律" for v in vios)
    print(f"PASS test_violations_detected → {sorted(ids)}")


def test_compliant_position_no_violation():
    """大账户 + 小仓位 + 远到期 → 不触碰任何限额。"""
    cap = AccountCapital(buy_power=50000.0, net_assets=100000.0, cash_usd=50000.0)
    pos = [_Pos("SLV261218P55000.US", "SLV 55 Put", -1, 2.0),
           _Pos("SLV261218P50000.US", "SLV 50 Put", 1, 0.5)]
    rv = review_portfolio(pos, {"SLV": _ctx()}, asof=date(2026, 8, 25), capital=cap)
    vios = check_against_profile(rv, cap, _profile(min_rr=0.2, min_seller_edge_pp=None))
    assert not vios, [v.title for v in vios]
    print("PASS test_compliant_position_no_violation")


def test_no_profile_no_violations():
    """没有档案时不产生任何核查结果（不强加规则）。"""
    cap = AccountCapital(buy_power=6.0, net_assets=630.0, cash_usd=6.0)
    rv = review_portfolio(_wheel(), {"SLV": _ctx()}, asof=date(2026, 8, 25), capital=cap)
    assert check_against_profile(rv, cap, None) == []
    print("PASS test_no_profile_no_violations")


def test_two_tier_size_limits():
    """双层限额：止损风险(软) 与 最大亏损(硬跳空口径) 分别检查。"""
    from undertow.analyze.healthcheck import stop_risk
    cap = AccountCapital(buy_power=400.0, net_assets=440.0, cash_usd=400.0)
    # 1张 60/59 收权金0.38：止损亏≈$38(8.6%✅)、最大亏$62(14%✅) → 两条都过
    pos = [_Pos("SLV260918P60000.US", "60P", -1, 1.76),
           _Pos("SLV260918P59000.US", "59P", 1, 1.38)]
    rv = review_portfolio(pos, {"SLV": _ctx()}, asof=date(2026, 8, 26), capital=cap)
    c = rv.groups[0].combos[0]
    sr = stop_risk(c)
    assert abs(sr - 38.0) < 1e-6, sr                 # 收权金×100×张
    assert abs(c.capital_at_risk - 62.0) < 1e-6, c.capital_at_risk
    ids = {v.rule_id for v in check_against_profile(rv, cap, _profile())}
    assert "max_risk_per_trade_pct" not in ids, ids   # 8.6% < 10%
    assert "max_loss_per_trade_pct" not in ids, ids   # 14% < 20%
    print(f"PASS test_two_tier_size_limits → 止损 ${sr:.0f}(8.6%) / 最大亏 ${c.capital_at_risk:.0f}(14%) 均通过")


def test_max_loss_tier_catches_gap_risk():
    """最大亏损超 20% 硬上限 → 即使软止损合规也拦下（跳空防线）。"""
    cap = AccountCapital(buy_power=400.0, net_assets=440.0, cash_usd=400.0)
    pos = [_Pos("SLV260918P60000.US", "60P", -3, 1.76),
           _Pos("SLV260918P59000.US", "59P", 3, 1.38)]   # 3张：止损$114(26%)、最大亏$186(42%)
    rv = review_portfolio(pos, {"SLV": _ctx()}, asof=date(2026, 8, 26), capital=cap)
    ids = {v.rule_id for v in check_against_profile(rv, cap, _profile())}
    assert "max_loss_per_trade_pct" in ids, ids
    print("PASS test_max_loss_tier_catches_gap_risk")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
