"""持仓 × 信号冲突告警 —— 回归测试。

全部用例都锚定 2026-08-28 那次失职：黄金亮 ⚡极强看跌（53.5×），
用户持白银多头，金银相关 0.89 —— 没提信号、没质疑白银的相反结论、没预警持仓。
次日 SLV -4.38%。
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.position_alert import (  # noqa: E402
    parse_symbol, leg_bias, position_bias, check_conflicts, render, unparsed)


@dataclass
class SS:
    direction: str
    level: str
    pressure_ratio: float
    reasons: list


# 长桥真实返回的格式：带 .US 交易所后缀
REAL = {"SLV260918C70000.US": 1, "SLV260918C73000.US": -1,
        "TQQQ260918C76000.US": 1, "TQQQ260918C80000.US": -1}


def test_parses_exchange_suffix():
    """真实代码带 .US —— 早先正则以 $ 收尾，四条腿一条都解析不出来，
    持仓被当成不存在，告警静默消失。"""
    assert parse_symbol("SLV260918C70000.US") == ("SLV", "C")
    assert parse_symbol("SLV260918C70000") == ("SLV", "C")
    assert unparsed(REAL) == [], "真实持仓必须全部可解析"
    print("PASS test_parses_exchange_suffix")


def test_unparsed_is_reported_not_swallowed():
    """解析不了必须能被报出来 —— 静默吞掉等于没有告警。"""
    assert unparsed({"NOT_AN_OPTION": 1}) == ["NOT_AN_OPTION"]
    print("PASS test_unparsed_is_reported_not_swallowed")


def test_spread_direction_from_strikes():
    """价差两腿张数抵消，方向由【买的腿在哪一侧】唯一决定 —— 这是定义不是推测。"""
    assert leg_bias("SLV260918C70000.US", 1) == 1      # 买 call
    assert leg_bias("SLV260918C73000.US", -1) == -1    # 卖 call
    # 买低卖高的 call 价差 = 牛市看涨
    assert position_bias(REAL) == {"SLV": 1, "TQQQ": 1}
    # 买高卖低的 call 价差 = 熊市看涨（净看跌）
    bear = {"AAA260918C80000": 1, "AAA260918C76000": -1}
    assert position_bias(bear)["AAA"] == -1
    # 买高卖低的 put 价差 = 熊市看跌
    bear_put = {"BBB260918P80000": 1, "BBB260918P76000": -1}
    assert position_bias(bear_put)["BBB"] == -1
    print("PASS test_spread_direction_from_strikes")


def test_gold_signal_wakes_silver_position():
    """8/28 原样重演：黄金的极强看跌必须惊动白银的持仓。"""
    signals = {"GLD": SS("看跌", "极强", 53.5, ["看跌加权增仓 112,789 ≫ 看涨 563"])}
    cf = check_conflicts(REAL, signals)
    assert cf, "金银相关 0.89，黄金极强看跌必须对白银多头告警"
    c = cf[0]
    assert c.underlying == "SLV" and c.holding == "看涨"
    assert c.source == "GLD" and c.cross is True and c.corr == 0.89
    assert "SLV" in c.headline() and "GLD" in c.headline()
    assert "不是平仓指令" in render(cf), "必须声明未经校准，不是指令"
    print("PASS test_gold_signal_wakes_silver_position")


def test_same_direction_does_not_alert():
    """信号与持仓同向不该告警 —— 告警滥发等于没有告警。"""
    assert check_conflicts(REAL, {"GLD": SS("看涨", "强", 5.0, [])}) == []
    assert check_conflicts(REAL, {"SLV": SS("看涨", "强", 5.0, [])}) == []
    print("PASS test_same_direction_does_not_alert")


def test_same_instrument_ranks_before_cross():
    """同品种信号比交叉信号更直接，必须排在前面。"""
    cf = check_conflicts(REAL, {"GLD": SS("看跌", "极强", 53.5, []),
                                "SLV": SS("看跌", "强", 4.0, [])})
    assert cf[0].source == "SLV" and cf[0].cross is False
    print("PASS test_same_instrument_ranks_before_cross")


def test_family_consistency_flags_gold_silver_split():
    """金银同向、QQQ/TQQQ 同向 —— 结论不一致必须被摆出来（用户 2026-08-29 要求）。

    8/28 原样重演：黄金 ⚡极强看跌、白银近端中性，两者相关 0.89，
    当时没有任何地方指出这个矛盾。次日 GLD -3.24%、SLV -4.38%。
    """
    from undertow.analyze.family import check

    views = {
        "gold": {"near": "偏空(弱)", "signal_dir": "看跌", "signal_level": "极强"},
        "silver": {"near": "中性", "signal_dir": "", "signal_level": ""},
        "qqq": {"near": "偏空(弱)", "signal_dir": "", "signal_level": ""},
        "spy": {"near": "偏多(弱)", "signal_dir": "", "signal_level": ""},
        "tqqq": {"near": "偏空(弱)", "signal_dir": "", "signal_level": ""},
    }
    notes = check(views)
    pairs = {(n.a, n.b): n for n in notes}
    assert ("gold", "silver") in pairs, "黄金强信号落单必须惊动白银"
    assert pairs[("gold", "silver")].kind == "强信号落单"
    assert ("qqq", "spy") in pairs, "QQQ 与 SPY 近端相反必须报"
    assert pairs[("qqq", "spy")].kind == "近端方向相反"
    # QQQ 与 TQQQ 同向 → 不该报
    assert ("qqq", "tqqq") not in pairs, "同向不得误报，滥发等于没有告警"
    # 相关性越高、等级越强的排前面
    assert notes[0].severity >= notes[-1].severity
    print("PASS test_family_consistency_flags_gold_silver_split")


def test_family_same_direction_is_silent():
    """同族同向时必须完全沉默 —— 每天都报的提示等于没有提示。"""
    from undertow.analyze.family import check
    assert check({"gold": {"near": "偏多"}, "silver": {"near": "偏多(弱)"}}) == []
    assert check({"qqq": {"near": "偏空"}, "tqqq": {"near": "偏空(弱)"}}) == []
    print("PASS test_family_same_direction_is_silent")
