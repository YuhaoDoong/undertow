"""当日研判（证据门控，Codex 015）行为测试：结论字段只由有决策权限的主张产生。

不镜像实现：固定输入、只改变 T2/T3 输入（研判方向、强信号、斐波腿、自动盈亏比、增仓层），
可决策字段必须完全不变；无 T1 时任何观察组合都不产生已验证方向；T2 只在适用范围内展示。
"""
from datetime import date, timedelta

import pytest

from undertow.analyze import claims as cl
from undertow.analyze.fibonacci import FibAnalysis, FibLevel
from undertow.analyze.flow import StrongSignal
from undertow.analyze.risk_reward import RiskRewardPlan, Setup
from undertow.analyze.verdict import NO_T1, build_verdict

DECISION = ("headline", "short_answer", "chase_answer", "swing_action", "core_action")


class _O:
    def __init__(self, near, mid):
        self.near_bias, self.mid_bias, self.bias = near, mid, mid


class _FA:
    flow_tilt = ""


def _fib(direction, zone):
    return FibAnalysis(ok=True, direction=direction, swing_low=4020.0, swing_high=4660.0,
                       swing_low_date=date(2026, 8, 19), swing_high_date=date(2026, 8, 21),
                       leg_pct=7.7, spot=4648.0, etf_spot=None, ratio=None,
                       retracements=[FibLevel(0.5, 4494.0, None, "retr", "0.5", True)],
                       extensions=[], current_zone=zone, note="")


def _rr(grade, rr):
    s = Setup(kind="chase", name="chase", direction="做多", entry=4648.0, entry_label="现价", stop=4000.0,
              stop_label="", target=4900.0, target_label="", rr=rr, grade=grade, verdict="")
    return RiskRewardPlan(ok=True, direction="做多", spot=4648.0, setups=[s], note="")


def _sig(direction, level="极强", low=False):
    try:
        return StrongSignal(direction=direction, level=level, low_confidence=low)
    except TypeError:
        class S:
            pass
        s = S(); s.direction, s.level, s.low_confidence = direction, level, low
        return s


OBSERVATION_VARIANTS = [
    (_O("偏多", "偏多"), None, None, None),
    (_O("偏空", "偏空"), _sig("看跌"), _fib("down", "0.236"), _rr("优", 3.0)),
    (_O("偏多(弱)", "偏空"), _sig("看涨"), _fib("up", "摆动高"), _rr("差", 0.4)),
    (_O("中性", "中性"), _sig("看跌", low=True), _fib("up", "0.5"), None),
    (_O("", ""), None, None, _rr("中", 1.2)),
]


def _decision(v):
    return tuple(getattr(v, k) for k in DECISION)


def test_t2_t3_inputs_do_not_change_decision_fields():
    base = _decision(build_verdict(*OBSERVATION_VARIANTS[0][:1], _FA(), *OBSERVATION_VARIANTS[0][1:]))
    for o, sig, fib, rr in OBSERVATION_VARIANTS:
        for inst, fdir in (("gold", "偏多"), ("gold", "偏空"), ("wti", "偏空"), (None, None)):
            v = build_verdict(o, _FA(), sig, fib, rr, instrument=inst, flow_direction=fdir)
            assert _decision(v) == base, (o.near_bias, o.mid_bias, inst, fdir)


@pytest.mark.parametrize("o,sig,fib,rr", OBSERVATION_VARIANTS)
def test_no_t1_means_no_validated_direction_whatever_observations_say(o, sig, fib, rr):
    v = build_verdict(o, _FA(), sig, fib, rr)
    assert v.headline == NO_T1 and NO_T1 in v.evidence
    text = " ".join(_decision(v))
    for banned in ("可空", "跟空", "跟多", "可考虑波段空", "不是做空位置", "别追", "追不划算",
                   "现价结构占优", "长线拿住", "长线减", "已校准的中期", "不开枪"):
        assert banned not in text, banned
    assert "不等于市场不适合交易" in v.short_answer        # 模型无依据 ≠ 市场不值得交易
    assert "持仓风险监测照常" in v.swing_action             # 不因 T1 为空自动平现仓


def test_t2_shown_only_in_scope_and_never_in_decision_fields():
    g = build_verdict(_O("偏多", "偏多"), _FA(), None, None, None, instrument="gold", flow_direction="偏空")
    assert len(g.exploratory) == 1 and "T2" in g.exploratory[0] and "偏空" in g.exploratory[0]
    assert all("偏空" not in f for f in _decision(g))
    for inst in ("wti", "qqq", "spy", None):                # 适用范围外不借用
        assert build_verdict(None, None, None, None, None, instrument=inst, flow_direction="偏空").exploratory == []
    assert not cl.decision_allowed("flow.direction.gold_silver", "direction")


def test_a_t1_claim_without_adapter_fails_loudly(monkeypatch):
    fake = cl.Claim("x.t1", "p", "prediction", "s", "T1", "", ("direction",), prereg_ref="r",
                    evidence_refs=("wall_space_vote",))
    monkeypatch.setitem(cl.CLAIMS, "x.t1", fake)
    with pytest.raises(NotImplementedError):
        build_verdict()


def test_verdict_card_render_has_no_legacy_bypass():
    from undertow.report.html import render_verdict_section
    v = build_verdict(_O("偏空", "偏空"), _FA(), _sig("看跌"), _fib("down", "0.236"), _rr("优", 3.0),
                      instrument="silver", flow_direction="偏空")
    html = render_verdict_section(v, "白银")
    assert "证据门控" in html and NO_T1 in html and "探索观察" in html
    for banned in ("做空？", "现价追？", "可空", "跟空", "已校准的中期趋势"):
        assert banned not in html, banned
