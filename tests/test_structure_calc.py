"""结构计算器（Codex 015 提交二）：合法数据下与迁移前旧模块的结构数值一致；缺报价 → 未知；缺腿 → 明确不可计算。"""
from datetime import date, timedelta

import pytest

from undertow.analyze import structure_calc as sc
from undertow.analyze.condor import assess_condor
from undertow.analyze.credit_spread import assess_credit_spread
from undertow.core.models import OptionContract, OptionsSnapshot

TODAY = date(2026, 9, 28)
EXP = TODAY + timedelta(days=21)


def _snap(quotes=True, spot=100.0):
    cs = []
    for k in range(80, 121):
        if k == 100:
            continue
        for kind in ("P", "C"):
            otm = (kind == "P" and k < spot) or (kind == "C" and k > spot)
            dist = abs(k - spot)
            d = max(0.02, 0.5 - 0.035 * dist) * (1 if kind == "C" else -1)
            if not otm:
                d = (1 - abs(d)) * (1 if kind == "C" else -1)
            px = max(0.05, 3.0 - 0.14 * dist) if otm else dist + 1.0
            cs.append(OptionContract(expiry=EXP, strike=float(k), kind=kind, open_interest=2000, volume=100,
                                     gamma=0.01, delta=d, iv=0.25,
                                     bid=round(px * 0.95, 2) if quotes else 0.0,
                                     ask=round(px * 1.05, 2) if quotes else 0.0))
    return OptionsSnapshot("gold", "GLD", spot, "t", cs)


class _VR:
    stance, iv_minus_rv = "偏卖方", 6.0


class _OUT:
    def __init__(self, bias):
        self.near_bias, self.bias, self.mid_bias = bias, bias, bias
        self.confidence, self.regime, self.horizon_split = "中", "", False


@pytest.mark.parametrize("side,bias", [("P", "偏多"), ("C", "偏空")])
def test_credit_spread_numbers_match_legacy_when_legacy_applicable(side, bias):
    new = sc.credit_spread_calc(_snap(), TODAY, side, iv_minus_rv=6.0)
    old = assess_credit_spread(snap=_snap(), vr=_VR(), outlook=_OUT(bias), today=TODAY)
    assert old.applicable and new.computable
    assert [(l.strike, l.kind) for l in new.legs] == [(l.strike, l.kind) for l in old.legs]
    assert new.credit_bs == pytest.approx(old.net_credit) and new.width == pytest.approx(old.width)
    assert new.breakevens[0] == pytest.approx(old.breakeven)
    assert new.max_loss_bs == pytest.approx(old.max_loss + new.fee)          # 新口径含费用
    assert new.credit_conservative == pytest.approx(new.legs[0].bid - new.legs[1].ask)


def test_credit_spread_calc_ignores_direction_and_iv_gates():
    """旧模块在「中性」或 IV−RV < 2 时判不适配；计算器照算（方向与溢价判断未经验证，不作门槛）。"""
    old = assess_credit_spread(snap=_snap(), vr=type("V", (), {"stance": "中性", "iv_minus_rv": -3.0})(),
                               outlook=_OUT("中性"), today=TODAY)
    assert not old.applicable
    for side in ("P", "C"):
        assert sc.credit_spread_calc(_snap(), TODAY, side, iv_minus_rv=-3.0).computable


def test_condor_numbers_match_legacy_and_no_stance_gate():
    new = sc.condor_calc(_snap(), TODAY)
    old = assess_condor(snap=_snap(), vr=_VR(), today=TODAY)
    assert old.applicable and new.computable
    assert [(l.strike, l.kind) for l in new.legs] == [(l.strike, l.kind) for l in old.legs]
    assert new.credit_bs == pytest.approx(old.net_credit)
    assert new.breakevens == pytest.approx([old.be_lo, old.be_hi])
    assert not assess_condor(snap=_snap(), vr=type("V", (), {"stance": "偏买方"})(), today=TODAY).applicable
    assert new.computable                                                        # 波动率 stance 不再是门槛


def test_missing_quotes_are_unknown_not_zero():
    c = sc.credit_spread_calc(_snap(quotes=False), TODAY, "P")
    assert c.computable and c.credit_bs is not None
    assert c.credit_conservative is None and c.max_loss_conservative is None
    assert all(l.bid is None and l.ask is None for l in c.legs)


def test_missing_legs_fail_with_specific_reason():
    empty = OptionsSnapshot("gold", "GLD", 100.0, "t", [])
    c = sc.credit_spread_calc(empty, TODAY, "C")
    assert not c.computable and "到期" in c.reason
    assert not sc.condor_calc(empty, TODAY).computable


def test_calculator_render_has_no_fit_or_direction_verdicts():
    from undertow.report.html import render_structure_calculators
    html = render_structure_calculators([sc.credit_spread_calc(_snap(), TODAY, "P", iv_minus_rv=6.0),
                                         sc.condor_calc(_snap(quotes=False), TODAY)])
    assert "方向由你决定" in html and "未知" in html
    for banned in ("✅", "勉强适配", "结构偏弱", "）适配", "不开枪", "⭐", "正是铁鹰", "卖方有正溢价"):
        assert banned not in html, banned
