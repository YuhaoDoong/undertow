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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
