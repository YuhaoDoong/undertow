"""大白话速读：确定性拼句（现价/涨跌、阻力群、第一层支撑、多空分界、环境）。"""
from undertow.analyze.outlook import KeyLevel, Outlook, plain_summary


def _outlook(spot, levels, *, bias="偏空", conf="高", regime="负Gamma：做市商净空伽马"):
    return Outlook(
        instrument="gold", display_name="黄金", asof="test",
        spot=spot / 11.0, commodity_spot=spot, proxy_symbol="GLD",
        bias=bias, bias_score=-3.9, confidence=conf, regime=regime,
        key_levels=levels,
    )


def _lvl(label, comm, kind):
    return KeyLevel(label, comm / 11.0, comm, kind, "")


GOLD_LEVELS = [
    _lvl("看涨墙 / 阻力", 4439, "resistance"),
    _lvl("零伽马翻转", 4304, "flip"),
    _lvl("看跌墙 / 支撑", 3995, "support"),
]


def test_summary_core_sentences():
    o = _outlook(4196, GOLD_LEVELS)
    s = plain_summary(o, day_chg_pct=3.0,
                      vol_verdict="价涨而 ATM IV 被压 → …期权端未确认涨势")
    assert "现价 4,196" in s and "涨 3.0%" in s
    assert "偏空（可信度高）" in s
    assert "4,304（零伽马翻转） → 4,439（看涨墙）" in s and "阻力密集" in s
    assert "把握不大" in s                       # 无买方确认 → 突破存疑
    assert "3,995" in s and "多空分界" in s      # put 墙 = 第一层支撑 = 分界（合并句）
    assert "负伽马" in s


def test_summary_buyer_confirmed_and_positive_gamma():
    o = _outlook(4196, GOLD_LEVELS, bias="偏多", regime="正Gamma：做市商净多伽马")
    s = plain_summary(o, day_chg_pct=-1.2, vol_verdict="价涨且 ATM IV 抬升 → 买方追价，涨势获期权端确认")
    assert "跌 1.2%" in s
    assert "突破尝试值得重视" in s
    assert "正伽马" in s and "假突破" in s


def test_summary_broken_below_put_wall():
    o = _outlook(3900, GOLD_LEVELS)   # 现价已破 3,995 put 墙
    s = plain_summary(o)
    assert "破位区" in s and "3,995" in s
    assert "较上一交易日" not in s     # 无日涨跌数据则不硬凑
