"""技术指标层的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：单调上涨序列→多头排列+超买；单调下跌→空头排列+超卖；震荡→中性；短序列不崩。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.core.models import PriceSeries
from undertow.analyze.technicals import analyze_technicals, _rsi, _bollinger


def _series(closes):
    d0 = date(2026, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(len(closes))]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    return PriceSeries(symbol="TST", dates=dates, closes=closes, highs=highs, lows=lows)


def test_uptrend_overbought():
    """稳步上涨 60 根 → 多头排列 + 短线超买（RSI/KDJ 高、贴布林上轨）。"""
    closes = [50 + i * 0.5 for i in range(60)]        # 单调上涨
    tr = analyze_technicals(_series(closes))
    assert tr.ok and tr.trend == "多头排列", tr.trend
    assert tr.heat_score > 0 and "超买" in tr.heat, (tr.heat_score, tr.heat)
    assert tr.rsi6 is not None and tr.rsi6 >= 70, tr.rsi6
    assert tr.macd is not None and tr.macd[0] > 0, tr.macd    # DIF 在零轴上方
    print(f"PASS test_uptrend_overbought → {tr.headline}")


def test_downtrend_oversold():
    """稳步下跌 → 空头排列 + 短线超卖。"""
    closes = [90 - i * 0.5 for i in range(60)]
    tr = analyze_technicals(_series(closes))
    assert tr.trend == "空头排列", tr.trend
    assert tr.heat_score < 0 and "超卖" in tr.heat, (tr.heat_score, tr.heat)
    print(f"PASS test_downtrend_oversold → {tr.headline}")


def test_choppy_neutral():
    """横盘震荡 → 中性、不超买不超卖。"""
    closes = [60 + (1 if i % 2 else -1) for i in range(60)]
    tr = analyze_technicals(_series(closes))
    assert tr.heat == "中性", (tr.heat, tr.heat_score)
    print(f"PASS test_choppy_neutral → {tr.headline}")


def test_short_series_graceful():
    """价序不足 30 根 → ok=False，不崩。"""
    tr = analyze_technicals(_series([60 + i for i in range(10)]))
    assert not tr.ok and "不足" in tr.note, tr
    print("PASS test_short_series_graceful")


def test_rsi_bounds():
    """全涨 RSI→100、全跌 RSI→0。"""
    up = [50 + i for i in range(20)]
    down = [50 - i for i in range(20)]
    assert abs(_rsi(up, 6) - 100.0) < 1e-6
    assert abs(_rsi(down, 6) - 0.0) < 1e-6
    print("PASS test_rsi_bounds")




def test_aggregate_must_not_cross_sessions():
    """4h 聚合必须按交易日分组，绝不能对全序列盲分。

    美股 RTH 6.5 小时 → 每日 7 根 1h，**不是 4 的整数倍**。对全序列每 4 根一切，
    必然把前一日尾盘和次日开盘拼成一根。2026-08-27 实测：08-26 19:30 收 711.37
    被拼进"08-27 15:30"那根、成为其开盘 712.31，横跨隔夜跳空 ——
    这种 K 线在任何看盘软件里都不存在，指标自然对不上（RSI6 一度算出 100）。
    修复后 4H 的 MACD 从「柱 -0.66、死叉 13 根前」变成「柱 +0.23、金叉本根」——
    方向都反了，可见影响之大。
    """
    from datetime import datetime, timezone, timedelta
    from undertow.collect.longbridge_kline import aggregate

    def _b(day, hour, close):
        return {"ts": datetime(2026, 8, day, hour, 30, tzinfo=timezone.utc),
                "open": close - 1, "high": close + 1, "low": close - 2,
                "close": close, "volume": 100.0}

    # 两个交易日，每日 7 根（美股 RTH 实况）
    bars = [_b(26, 13 + i, 700 + i) for i in range(7)] + \
           [_b(27, 13 + i, 800 + i) for i in range(7)]
    agg = aggregate(bars, 4)
    for g in agg:
        pass
    # 每根聚合 K 线必须落在单一交易日内 —— 用 open/close 的量级区分两天
    for g in agg:
        assert (g["open"] < 750) == (g["close"] < 750), \
            f"聚合 K 线跨了交易日：open={g['open']} close={g['close']}"
    # 8/27 的第一根必须以当日开盘为开盘，而不是 8/26 尾盘
    d27 = [g for g in agg if g["close"] >= 800]
    assert d27 and abs(d27[0]["open"] - 799.0) < 1e-6, d27[0]
    print("PASS test_aggregate_must_not_cross_sessions")


def test_crossovers_reports_event_and_age():
    """穿越检测必须给出【事件 + 距今根数】，末值答不了"有没有金叉"。"""
    from undertow.analyze.technicals import crossovers
    n = 80
    closes = [100.0 - i * 0.5 for i in range(50)] + [75.0 + i * 1.2 for i in range(n - 50)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    cx = crossovers(highs, lows, closes)
    assert "kdj" in cx and "macd" in cx
    assert cx["macd_params"] == (12, 26, 9)
    kd = cx["kdj"]
    assert kd["state"].startswith("多头"), kd          # 尾段单边上涨 → 快线在上
    assert kd["event"] in ("金叉", "死叉", None)
    # 数据不足时返回空 dict，不猜
    assert crossovers(highs[:20], lows[:20], closes[:20]) == {}
    print("PASS test_crossovers_reports_event_and_age")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
