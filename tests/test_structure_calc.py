"""结构计算器（Codex 015 提交二，016 修正）：
- 合法数据下模型情景的腿、净收、税费前盈亏平衡与迁移前旧模块一致；
- 报价质量在计算器入口实际生效（倒挂/非有限/缺失/越界 → 报价情景未知，不出假风险）；
- 含费盈亏平衡代回同一到期收益公式净损益为 0；缓冲与之同源；费用取单一定义。"""
from datetime import date, timedelta

import pytest

from undertow.analyze import structure_calc as sc
from undertow.analyze.condor import assess_condor
from undertow.analyze.credit_spread import assess_credit_spread
from undertow.core.models import OptionContract, OptionsSnapshot

TODAY = date(2026, 9, 28)
EXP = TODAY + timedelta(days=21)


def _snap(quotes="ok", spot=100.0):
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
            bid, ask = {"ok": (round(px * 0.95, 2), round(px * 1.05, 2)), "none": (0.0, 0.0),
                        "crossed": (10.0, 1.0), "nan": (float("nan"), 1.0)}[quotes]
            cs.append(OptionContract(expiry=EXP, strike=float(k), kind=kind, open_interest=2000, volume=100,
                                     gamma=0.01, delta=d, iv=0.25, bid=bid, ask=ask))
    return OptionsSnapshot("gold", "GLD", spot, "t", cs)


class _VR:
    stance, iv_minus_rv = "偏卖方", 6.0


class _OUT:
    def __init__(self, bias):
        self.near_bias, self.bias, self.mid_bias = bias, bias, bias
        self.confidence, self.regime, self.horizon_split = "中", "", False


@pytest.mark.parametrize("side,bias", [("P", "偏多"), ("C", "偏空")])
def test_credit_spread_model_matches_legacy(side, bias):
    new = sc.credit_spread_calc(_snap(), TODAY, side, iv_minus_rv=6.0)
    old = assess_credit_spread(snap=_snap(), vr=_VR(), outlook=_OUT(bias), today=TODAY)
    assert old.applicable and new.computable and new.model.status == "ok"
    assert [(l.strike, l.kind) for l in new.legs] == [(l.strike, l.kind) for l in old.legs]
    assert new.model.credit == pytest.approx(old.net_credit) and new.width == pytest.approx(old.width)
    assert new.model.breakevens_pre_fee[0] == pytest.approx(old.breakeven)     # 税费前与旧口径相同
    assert new.model.max_loss == pytest.approx(old.max_loss + new.fee)        # 新口径含费


def test_condor_model_matches_legacy_and_no_stance_gate():
    new = sc.condor_calc(_snap(), TODAY)
    old = assess_condor(snap=_snap(), vr=_VR(), today=TODAY)
    assert old.applicable and new.computable and new.model.status == "ok"
    assert [(l.strike, l.kind) for l in new.legs] == [(l.strike, l.kind) for l in old.legs]
    assert new.model.credit == pytest.approx(old.net_credit)
    assert new.model.breakevens_pre_fee == pytest.approx([old.be_lo, old.be_hi])
    assert not assess_condor(snap=_snap(), vr=type("V", (), {"stance": "偏买方"})(), today=TODAY).applicable


def test_direction_and_iv_gates_are_gone():
    old = assess_credit_spread(snap=_snap(), vr=type("V", (), {"stance": "中性", "iv_minus_rv": -3.0})(),
                               outlook=_OUT("中性"), today=TODAY)
    assert not old.applicable
    for side in ("P", "C"):
        assert sc.credit_spread_calc(_snap(), TODAY, side, iv_minus_rv=-3.0).computable


@pytest.mark.parametrize("side", ["P", "C"])
@pytest.mark.parametrize("which", ["model", "quote"])
def test_fee_inclusive_breakeven_gives_zero_pnl(side, which):
    """Codex 016 F16-03：所示（含费）盈亏平衡代回同一静态到期收益公式 → 净损益 0。"""
    c = sc.credit_spread_calc(_snap(), TODAY, side)
    s = getattr(c, which)
    assert s.status == "ok"
    assert sc.expiry_pnl(c, s.breakevens[0], s.credit) == pytest.approx(0.0, abs=1e-6)
    assert sc.expiry_pnl(c, s.breakevens_pre_fee[0], s.credit) == pytest.approx(-c.fee, abs=1e-6)  # 税费前点仍亏费用
    # 缓冲与含费盈亏平衡同源
    b = s.breakevens[0]
    want = 100 * ((c.spot - b) if side == "P" else (b - c.spot)) / c.spot
    assert s.buffer_pct[0] == pytest.approx(want)


def test_condor_fee_inclusive_breakevens_zero_pnl():
    c = sc.condor_calc(_snap(), TODAY)
    for b in c.model.breakevens:
        assert sc.expiry_pnl(c, b, c.model.credit) == pytest.approx(0.0, abs=1e-6)


def test_crossed_quotes_do_not_produce_risk_numbers():
    """Codex 016 F16-01 反例：各腿 bid=10、ask=1 → 旧实现输出保守净收 9、最大亏损 −$396.80。"""
    c = sc.credit_spread_calc(_snap("crossed"), TODAY, "P")
    assert c.computable and c.model.status == "ok"                  # 模型情景仍可算
    assert c.quote.status == "crossed" and "倒挂" in c.quote.reason
    assert c.quote.credit is None and c.quote.max_loss is None and c.quote.breakevens == []
    assert all(l.quote_issue == "crossed" for l in c.legs)


def test_non_finite_and_missing_quotes():
    n = sc.credit_spread_calc(_snap("nan"), TODAY, "C")
    assert n.quote.status == "non_finite" and n.quote.max_loss is None and n.model.status == "ok"
    m = sc.credit_spread_calc(_snap("none"), TODAY, "P")
    assert m.quote.status == "missing" and m.quote.max_loss is None and m.model.status == "ok"
    assert all(l.bid is None and l.ask is None for l in m.legs)


def test_credit_out_of_bounds_is_flagged_not_clipped():
    s = sc._scenario(6.0, 5.0, 3.2, lambda x: [97 - x])
    assert s.status == "out_of_bounds" and s.max_loss is None and "界限冲突" in s.reason
    s2 = sc._scenario(-0.5, 5.0, 3.2, lambda x: [97 - x])
    assert s2.status == "out_of_bounds" and s2.credit is None


def test_no_breakeven_when_credit_below_fees():
    s = sc._scenario(0.02, 5.0, 3.2, lambda x: [97 - x])
    assert s.status == "no_breakeven" and s.breakevens == [] and s.max_loss == pytest.approx((5 - 0.02) * 100 + 3.2)


def test_fee_uses_single_definition(monkeypatch):
    from undertow.analyze.shadow import CONFIG
    assert sc.FEE_PER_LEG_ROUND_TRIP * 2 == CONFIG["fee_round_trip"]
    monkeypatch.setattr(sc, "FEE_PER_LEG_ROUND_TRIP", 5.0)
    c = sc.credit_spread_calc(_snap(), TODAY, "P")
    assert c.fee == 10.0 and sc.expiry_pnl(c, c.model.breakevens[0], c.model.credit) == pytest.approx(0.0, abs=1e-6)


def test_missing_legs_fail_with_rule_scoped_reason():
    empty = OptionsSnapshot("gold", "GLD", 100.0, "t", [])
    c = sc.credit_spread_calc(empty, TODAY, "C")
    assert not c.computable and "按选腿规则" in c.reason
    assert not sc.condor_calc(empty, TODAY).computable


def test_calculator_render_real_output():
    from undertow.report.html import render_structure_calculators
    html = render_structure_calculators([sc.credit_spread_calc(_snap(), TODAY, "P", iv_minus_rv=6.0),
                                         sc.credit_spread_calc(_snap("crossed"), TODAY, "C"),
                                         sc.condor_calc(_snap("none"), TODAY)])
    assert "方向由你决定" in html and "盈亏平衡（含费）" in html and "税费前" in html
    assert "倒挂" in html and "-396" not in html and "$-" not in html
    assert "研究性启发式" in html
    for banned in ("✅", "勉强适配", "结构偏弱", "）适配", "不开枪", "⭐", "正是铁鹰", "卖方有正溢价"):
        assert banned not in html, banned
