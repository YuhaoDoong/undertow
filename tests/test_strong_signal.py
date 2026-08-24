"""近端资金流强信号检测器的确定性测试（函数式，不依赖 pytest）。

复盘背景：8/19 黄金期权"主翼 call 买盘一边倒 + 上行压力压倒下行"，综合投票却因慢因子
对冲成"分歧/中性"而埋没，次日兑现直线上涨。detect_strong_signal 就是把这种一边倒的
领先信号独立拎出来置顶。测试锚定：该触发时触发、涨后防守型 put 买盘不误报看跌、
清淡/均衡日不触发、与综合背离时打背离标。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.flow import (
    FlowAnalysis, FlowChange, VolRead, VolSurface, detect_strong_signal,
    STRONG_MIN_NET_DOI)


def _call(strike, d_oi, bias, weight, delta=0.30):
    return FlowChange(
        expiry=date(2026, 10, 16), strike=strike, kind="C", prev_oi=1000,
        curr_oi=1000 + d_oi, d_oi=d_oi, delta=delta, prev_iv=0.20, curr_iv=0.21,
        d_iv_pp=1.0, adj_iv_pp=0.6, curr_volume=d_oi, moneyness=0.03,
        bias=bias, judgment="买方" if bias == "bullish" else "卖方压制",
        on_wall="", note="", weight=weight)


def _put(strike, d_oi, bias, weight, delta=-0.30):
    return FlowChange(
        expiry=date(2026, 10, 16), strike=strike, kind="P", prev_oi=1000,
        curr_oi=1000 + d_oi, d_oi=d_oi, delta=delta, prev_iv=0.20, curr_iv=0.21,
        d_iv_pp=1.0, adj_iv_pp=0.6, curr_volume=d_oi, moneyness=-0.03,
        bias=bias, judgment="买方保护" if bias == "bearish" else "卖方做支撑",
        on_wall="", note="", weight=weight)


def _vs(d_spot_pct, d_atm_pp=0.0, d_skew25_pp=0.0):
    # skew25 = put IV − call IV；d_skew25 = curr − prev。给 prev 反号即得目标日变化。
    curr = VolRead(date(2026, 10, 16), 56, 23.0, 1.0, 2.0)
    prev = VolRead(date(2026, 10, 16), 57, 23.0 - d_atm_pp, 1.0 - d_skew25_pp, 2.0)
    return VolSurface(curr=curr, prev=prev, d_spot_pct=d_spot_pct, verdict="")


def _fa(changes, *, up, dn, net_call=40000, net_put=2000, vs=None):
    return FlowAnalysis(
        instrument="gold", proxy_symbol="GLD", spot=400.0, horizon_days=60,
        curr_date="2026-08-19", curr_asof="2026-08-19", prev_date="2026-08-18",
        changes=changes, net_call_doi=net_call, net_put_doi=net_put,
        downside_pressure=dn, upside_pressure=up, vol=vs)


def test_bullish_fires():
    """主翼 call 买盘一边倒 + 上行压力压倒下行 → ⚡强看涨。"""
    changes = [_call(445, 4906, "bullish", 1.0), _call(425, 4856, "bullish", 1.0),
               _call(455, 1521, "bullish", 1.0), _call(435, 968, "bullish", 1.0)]
    fa = _fa(changes, up=40609, dn=6782)
    ss = detect_strong_signal(fa, outlook_bias="分歧(双向)")
    assert ss is not None and ss.direction == "看涨", ss
    assert ss.level == "强", ss.level          # 无波动率面追认 → 强（非极强）
    assert ss.pressure_ratio >= 3.0, ss.pressure_ratio
    assert ss.diverges is True, "综合=分歧应判背离"
    print(f"PASS test_bullish_fires ({ss.level}{ss.direction} 压力{ss.pressure_ratio}× 主翼{ss.wing_ratio}×)")


def test_bullish_extreme_with_vol_confirm():
    """价涨 + ATM IV 抬升（买方追价）→ 升级为极强。"""
    changes = [_call(445, 4906, "bullish", 1.0), _call(425, 4856, "bullish", 1.0),
               _call(455, 1521, "bullish", 1.0)]
    fa = _fa(changes, up=40609, dn=6782, vs=_vs(d_spot_pct=+2.0, d_atm_pp=+1.0))
    ss = detect_strong_signal(fa, outlook_bias="偏多")
    assert ss is not None and ss.level == "极强", ss
    assert ss.vol_confirms is True
    assert ss.diverges is False, "综合=偏多 与看涨同向，不背离"
    print("PASS test_bullish_extreme_with_vol_confirm")


def test_skew_flip_upgrades_to_extreme():
    """无 ATM 追认，但 25Δ skew 向 call 大幅倾斜（call 变贵）→ 升级极强（核心信号）。"""
    changes = [_call(445, 4906, "bullish", 1.0), _call(425, 4856, "bullish", 1.0),
               _call(455, 1521, "bullish", 1.0)]
    # 价微跌、ATM 不追认，但 skew25 下降 1.2pp（put 相对 call 变便宜＝抢 call）
    fa = _fa(changes, up=40609, dn=6782, vs=_vs(d_spot_pct=-0.3, d_atm_pp=0.0, d_skew25_pp=-1.2))
    ss = detect_strong_signal(fa, outlook_bias="分歧(双向)")
    assert ss is not None and ss.direction == "看涨", ss
    assert ss.level == "极强" and ss.vol_confirms, ss
    assert any("skew 向 call 倾斜" in r for r in ss.reasons), ss.reasons
    print("PASS test_skew_flip_upgrades_to_extreme")


def test_contra_gate_suppresses_defensive_puts():
    """价格大涨那天的 put 保护买盘（下行压力高）≠ 看跌，价格背离闸门抑制。"""
    changes = [_put(390, 8000, "bearish", 1.0), _put(385, 6000, "bearish", 1.0),
               _put(395, 4000, "bearish", 1.0)]
    fa = _fa(changes, up=11701, dn=168355, net_put=30354,
             vs=_vs(d_spot_pct=+2.68, d_atm_pp=+2.78))   # 当日价大涨 → 与看跌背离
    ss = detect_strong_signal(fa, outlook_bias="分歧(双向)")
    assert ss is None, f"大涨日的防守 put 不应报强看跌，得到 {ss}"
    print("PASS test_contra_gate_suppresses_defensive_puts")


def test_bearish_fires_on_down_day():
    """价跌日主翼 put 买保护/ call 压制一边倒 → ⚡强看跌（价格不背离）。"""
    changes = [_put(390, 8000, "bearish", 1.0), _put(385, 6000, "bearish", 1.0),
               _put(395, 4000, "bearish", 1.0)]
    fa = _fa(changes, up=11701, dn=168355, net_put=30354,
             vs=_vs(d_spot_pct=-1.8, d_atm_pp=+1.5))
    ss = detect_strong_signal(fa, outlook_bias="偏空")
    assert ss is not None and ss.direction == "看跌", ss
    assert ss.level == "极强" and ss.vol_confirms, ss   # 跌 + IV 升 = 买保护追认
    print("PASS test_bearish_fires_on_down_day")


def test_balanced_no_signal():
    """买卖两边势均力敌 → 不触发（宁缺勿滥）。"""
    changes = [_call(445, 3000, "bullish", 1.0), _call(430, 3000, "bearish", 1.0)]
    fa = _fa(changes, up=20000, dn=18000)
    assert detect_strong_signal(fa) is None
    print("PASS test_balanced_no_signal")


def test_thin_day_no_signal():
    """净建仓规模不达门槛（清淡日）→ 不触发。"""
    changes = [_call(445, 500, "bullish", 1.0)]
    fa = _fa(changes, up=5000, dn=200, net_call=STRONG_MIN_NET_DOI - 1)
    assert detect_strong_signal(fa) is None
    print("PASS test_thin_day_no_signal")


def test_no_prev_no_signal():
    """仅一份快照（无日对日 diff）→ 不触发。"""
    fa = FlowAnalysis(instrument="gold", proxy_symbol="GLD", spot=400.0,
                      horizon_days=60, curr_date="2026-08-19", curr_asof="x",
                      prev_date=None, upside_pressure=99999, downside_pressure=0)
    assert detect_strong_signal(fa) is None
    print("PASS test_no_prev_no_signal")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
