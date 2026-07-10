"""策略情景参数化：方向随 bias、位点随结构、缓冲随 ATR、否决票如实呈现。"""
from datetime import date, timedelta

from undertow.analyze.flow import VolRead, VolSurface
from undertow.analyze.outlook import KeyLevel, Outlook
from undertow.analyze.strategy import (build_strategy, compute_atr,
                                       ATR_BASELINE_PCT)
from undertow.core.models import PriceSeries


def _outlook(spot, levels, *, bias="偏空", conf="高",
             regime="负Gamma：做市商净空伽马"):
    return Outlook(
        instrument="silver", display_name="白银", asof="test",
        spot=spot / 1.093, commodity_spot=spot, proxy_symbol="SLV",
        bias=bias, bias_score=-5.4, confidence=conf, regime=regime,
        key_levels=levels,
    )


def _lvl(label, comm, kind):
    return KeyLevel(label, comm / 1.093, comm, kind, "")


SILVER_LEVELS = [
    _lvl("看涨墙 / 阻力", 65.5, "resistance"),
    _lvl("近到期 call pin", 62.2, "pin"),
    _lvl("资金流活跃 看涨56.0", 61.1, "flow"),
    _lvl("零伽马翻转", 60.6, "flip"),
    _lvl("看跌墙 / 支撑", 54.6, "support"),
]


def _series(closes, spread=0.6):
    n = len(closes)
    d0 = date(2026, 6, 1)
    return PriceSeries(symbol="SI=F",
                       dates=[d0 + timedelta(days=i) for i in range(n)],
                       closes=closes,
                       highs=[c + spread for c in closes],
                       lows=[c - spread for c in closes])


def test_atr_true_range_and_close_fallback():
    closes = [60.0 + 0.1 * i for i in range(20)]
    atr, note = compute_atr(_series(closes, spread=0.6))
    assert atr is not None and "真实波幅" in note
    assert 1.1 < atr < 1.4          # 高低差 1.2 主导
    s2 = PriceSeries("X", _series(closes).dates, closes)   # 无高低价
    atr2, note2 = compute_atr(s2)
    assert abs(atr2 - 0.1) < 1e-9 and "近似" in note2
    assert compute_atr(None)[0] is None


def test_short_plan_scenarios_and_vetoes():
    o = _outlook(61.3, SILVER_LEVELS, regime="正Gamma：做市商净多伽马")
    plan = build_strategy(o, series=_series([58 + 0.2 * i for i in range(20)]))
    assert plan.direction == "做空"
    fade = next(s for s in plan.scenarios if s.key == "fade")
    trig = next(s for s in plan.scenarios if s.key == "trigger")
    # 墙前区上沿 = call 墙；失效在墙上方；目标在区间下方且按 R:R 配对
    assert abs(fade.entry_hi - 65.5) < 1e-9
    assert fade.invalidation > 65.5
    assert fade.targets and all(t < fade.entry_lo for t, _ in fade.targets)
    assert len(fade.rr) == len(fade.targets) and all(r > 0 for r in fade.rr)
    # 现价 61.3 > 零伽马 60.6 → 顺结构情景未触发；正Gamma+价在翻转上方 = 至少 2 票否决
    assert trig.status == "未触发"
    assert len(plan.vetoes) >= 2 and "无有效做空信号" in plan.verdict


def test_long_plan_mirror():
    o = _outlook(56.0, SILVER_LEVELS, bias="偏多",
                 regime="正Gamma：做市商净多伽马")
    plan = build_strategy(o, series=_series([56 + 0.05 * i for i in range(20)]))
    assert plan.direction == "做多"
    fade = next(s for s in plan.scenarios if s.key == "fade")
    assert abs(fade.entry_lo - 54.6) < 1e-9        # put 墙前承接
    assert fade.invalidation < 54.6
    assert all(t > fade.entry_hi for t, _ in fade.targets)
    trig = next(s for s in plan.scenarios if s.key == "trigger")
    assert trig.status == "未触发"                  # 56.0 < 零伽马 60.6


def test_neutral_gives_no_levels():
    o = _outlook(61.3, SILVER_LEVELS, bias="中性")
    plan = build_strategy(o)
    assert plan.direction == "观望"
    assert not plan.scenarios and "不出点位" in plan.verdict


def test_triggered_breakdown_and_sizing_scale():
    # 现价已破零伽马、负Gamma 环境 → 顺结构情景"结构条件已满足"，否决票为 0
    o = _outlook(59.8, SILVER_LEVELS)
    closes = [66 - 0.5 * i for i in range(20)]     # 日均波幅大 → 仓位缩放提示
    plan = build_strategy(o, series=_series(closes, spread=2.4))
    trig = next(s for s in plan.scenarios if s.key == "trigger")
    assert trig.status == "结构条件已满足"
    assert not plan.vetoes
    assert plan.atr_pct > ATR_BASELINE_PCT and "缩至基准" in plan.sizing_note
    # 目标包含 put 墙一侧的位点
    assert any(abs(t - 54.6) < 1e-9 for t, _ in trig.targets)


def _vol(verdict, *, skew25_prev=2.0, skew25_curr=1.8):
    rd = lambda s25: VolRead(expiry=date(2026, 8, 7), days_out=31,
                             atm_iv_pp=22.0, skew25_pp=s25, skew10_pp=3.5)
    return VolSurface(curr=rd(skew25_curr), prev=rd(skew25_prev),
                      d_spot_pct=1.0, verdict=verdict)


def test_negated_buyer_verdict_is_not_a_veto():
    # "没有买方追价"（否定句）不得被子串匹配误判成"买方获确认"否决票
    o = _outlook(61.3, SILVER_LEVELS)   # 负Gamma、现价>零伽马 → 本就有 1 票
    neg = _vol("价涨而 ATM IV 被压 → 没有买方追价抢筹（涨势更像空头回补）；且偏斜未明显收敛 → 期权端未确认涨势")
    plan = build_strategy(o, vol=neg)
    assert not any("获期权端确认" in v for v in plan.vetoes)
    pos = _vol("价涨且 ATM IV 抬升 → 买方追价，涨势获期权端确认")
    plan2 = build_strategy(o, vol=pos)
    assert any("获期权端确认" in v for v in plan2.vetoes)


def test_missing_structure_degrades():
    o = _outlook(61.3, [])
    plan = build_strategy(o)
    assert plan.direction == "做空" and not plan.scenarios
    assert "结构缺失" in plan.verdict


def test_exit_plan_template():
    o = _outlook(59.8, SILVER_LEVELS)          # 已破零伽马的做空情景
    plan = build_strategy(o, series=_series([66 - 0.5 * i for i in range(20)]))
    trig = next(s for s in plan.scenarios if s.key == "trigger")
    ep = trig.exit_plan
    assert "止损 = 失效线" in ep and "收盘站上" in ep and "日收盘口径" in ep
    assert "单位风险" in ep and "%" in ep
    assert "止盈" in ep and "R:R≈" in ep
    assert "保本" in ep
    # 做多镜像用"收盘跌破"
    o2 = _outlook(56.0, SILVER_LEVELS, bias="偏多", regime="正Gamma：做市商净多伽马")
    plan2 = build_strategy(o2, series=_series([56 + 0.05 * i for i in range(20)]))
    assert all("收盘跌破" in s.exit_plan for s in plan2.scenarios if s.exit_plan)
