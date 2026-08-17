"""综合研判层 outlook 单元测试（合成对象，纯计算）。

验证：方向投票 sign/权重、bias 映射、关键位汇总、情景排序、拥挤反指 caveat。
运行: python tests/test_outlook.py  或  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.analyze.positioning import PositioningAnalysis
from undertow.analyze.signals import Signal
from undertow.analyze.gamma import GammaAnalysis
from undertow.analyze.flow import FlowAnalysis
from undertow.analyze.outlook import build_outlook, _cot_votes, _bias


def _ga(**kw) -> GammaAnalysis:
    base = dict(
        instrument="t", proxy_symbol="T", spot=100.0, asof="2026-06-26",
        horizon_days=45, multiplier=10.0, proxy_quality="good",
        total_call_oi=1000, total_put_oi=1000, put_call_ratio=1.0,
        net_gex=-5e6, gex_regime="负Gamma",
        zero_gamma=98.0, call_wall=110.0, call_wall_oi=5000,
        put_wall=95.0, put_wall_oi=6000,
        nearest_expiry=None, nearest_call_wall=None, nearest_put_wall=None,
    )
    base.update(kw)
    return GammaAnalysis(**base)


def _fa(**kw) -> FlowAnalysis:
    base = dict(instrument="t", proxy_symbol="T", spot=100.0, horizon_days=45,
                curr_date="2026-06-26", curr_asof="2026-06-26", prev_date=None,
                total_call_volume=100, total_put_volume=160)  # put/call 1.6 → 偏空(弱权)
    base.update(kw)
    return FlowAnalysis(**base)


def _pos() -> PositioningAnalysis:
    return PositioningAnalysis(instrument="t", market_name="T", report_date=date(2026, 6, 23),
                              prev_date=None, open_interest=100000, open_interest_change=0,
                              lookback_used=156, categories={})


def test_bias_mapping():
    assert _bias(2.5, 2.5, 0.0)[0] == "偏多"
    assert _bias(-2.5, 0.0, 2.5)[0] == "偏空"
    assert _bias(0.3, 0.3, 0.0)[0] == "中性"
    assert _bias(0.0, 2.0, 2.0)[0] == "分歧(双向)"
    assert _bias(3.5, 3.5, 0.0)[1] == "高"   # 置信度随幅度


def test_cot_votes_sign_and_weight():
    sigs = [Signal(code="MM_CROWDED_LONG", title="多头拥挤", direction="risk-down",
                   detail="x。y", strength="强")]
    v = _cot_votes(sigs)[0]
    assert v.sign == -1 and v.direction == "看空"
    assert v.weight == 2.0  # 强(2.0) × 可信度(1.0)


def test_build_outlook_bearish_synthesis():
    sigs = [
        Signal("MM_CROWDED_LONG", "投机资金多头拥挤", "risk-down", "拥挤反指", "中"),
        Signal("SMART_DIVERGE_BEAR", "聪明钱背离", "bearish", "防守", "中"),
    ]
    o = build_outlook(_pos(), sigs, _ga(), _fa(), display_name="测试金")
    # 综合偏空
    assert o.bias.startswith("偏空")
    assert o.bias_score < 0
    # 关键位含三类
    labels = " ".join(k.label for k in o.key_levels)
    assert "看涨墙" in labels and "看跌墙" in labels and "零伽马" in labels
    # 商品价换算（multiplier=10 → 110*10=1100）
    cw = next(k for k in o.key_levels if "看涨墙" in k.label)
    assert abs(cw.commodity_level - 1100.0) < 1e-6
    # 情景：与偏空一致者排首位
    assert "向下" in o.scenarios[0].name
    # 拥挤反指的 caveat 在
    assert any("拥挤反指" in c for c in o.caveats)


def test_horizon_split_near_bear_mid_bull():
    """双周期分层：中期(COT)看多 + 近端(Gamma墙位近顶 + 资金流 put 偏多)看空 → 近空中多。
    复刻银 8/15：综合折中成偏多，但近端结构其实偏空，horizon_split 应点亮。"""
    sigs = [Signal("SMART_ACCUM_BULL", "聪明钱吸筹", "bullish", "OR逆势增多", "强")]
    ga = _ga(call_wall=102.0, put_wall=85.0)   # 上行空间 2% < 下行 15% → gamma 看空
    o = build_outlook(_pos(), sigs, ga, _fa(), display_name="测试银")  # _fa put/call 1.6 → flow 看空
    assert o.mid_bias.startswith("偏多"), o.mid_bias
    assert o.near_bias.startswith("偏空"), o.near_bias
    assert o.horizon_split is True
    # 同向时不点亮
    o2 = build_outlook(_pos(), sigs, _ga(), _fa(total_put_volume=100, total_call_volume=160),
                       display_name="测试")
    assert o2.horizon_split is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
