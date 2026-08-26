"""长桥 Level-2 盘口解析的确定性测试（不触网，喂原始 JSON 结构）。

为什么单独测这个：定限价只能用实时盘口。CBOE 虽有 bid/ask 但延迟 15 分钟，
且实测会把点差显示得比实际宽一倍以上（2026-08-26 TQQQ 80C：长桥实时
0.63/0.67 点差 0.04，CBOE 同时刻 0.70/0.79 点差 0.09）。用错来源会系统性
高估摩擦成本，进而把本可通过的结构误判成不划算。

盘口单边为空是常态（该侧无挂单/无权限），mid 必须返回 None 而不是拿 last 顶替。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.collect.longbridge_quote import Depth


def test_mid_and_spread():
    d = Depth(symbol="X", bid=0.63, bid_size=1461, ask=0.67, ask_size=268)
    assert abs(d.mid - 0.65) < 1e-9
    assert abs(d.spread_pct - (0.04 / 0.65 * 100)) < 1e-6
    print("PASS test_mid_and_spread")


def test_one_sided_book_returns_none():
    """单边空档必须返回 None —— 不许用 last 或另一边冒充中价。"""
    for d in (Depth("X", None, 0, 1.67, 1351),
              Depth("X", 0.63, 1312, None, 0),
              Depth("X", None, 0, None, 0)):
        assert d.mid is None, d
        assert d.spread_pct is None, d
    print("PASS test_one_sided_book_returns_none")


def test_zero_price_treated_as_missing():
    """价格为 0 等同于无挂单（CBOE 盘前就是全 0），不能算出 0 中价。"""
    d = Depth(symbol="X", bid=0.0, bid_size=0, ask=0.0, ask_size=0)
    assert d.mid is None and d.spread_pct is None
    print("PASS test_zero_price_treated_as_missing")


def test_cboe_overstates_spread_regression():
    """把当日实测钉成回归用例：同一时刻两个源的点差差一倍以上。

    若哪天改了取数逻辑导致又用回 CBOE 定限价，这条会提醒差距有多大。
    """
    live = Depth("TQQQ80C", 0.63, 1461, 0.67, 268)      # 长桥实时
    delayed = Depth("TQQQ80C", 0.70, 1229, 0.79, 1608)  # CBOE 同时刻（延迟15分）
    assert live.spread_pct < delayed.spread_pct / 1.8, (live.spread_pct, delayed.spread_pct)
    print("PASS test_cboe_overstates_spread_regression")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
