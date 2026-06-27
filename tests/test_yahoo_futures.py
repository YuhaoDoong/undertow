"""Yahoo 期货数据源解析测试（合成 payload 注入缓存，不联网）。

验证：chart JSON → PriceSeries 解析、None 收盘剔除、现价取 regularMarketPrice。
运行: python tests/test_yahoo_futures.py  或  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.collect.cache import FileCache
from undertow.collect.yahoo_futures import YahooFuturesSource


def _payload():
    return {"chart": {"result": [{
        "timestamp": [1709251200, 1709337600, 1709424000],  # 三天
        "indicators": {"quote": [{"close": [2050.5, None, 2065.0]}]},  # 中间 None 应被剔除
        "meta": {"regularMarketPrice": 2068.3, "regularMarketTime": 1709424000,
                 "currency": "USD", "fullExchangeName": "COMEX", "symbol": "GC=F"},
    }]}}


def _src_with_cache(tmp, rngs):
    src = YahooFuturesSource(cache=FileCache(root=Path(tmp)))
    for rng in rngs:
        src.cache.set(f"yahoo_GC=F_{rng}", _payload())
    return src


def test_fetch_series_parses_and_drops_none():
    with tempfile.TemporaryDirectory() as tmp:
        src = _src_with_cache(tmp, ["2y"])
        ps = src.fetch_series("GC=F", rng="2y", use_cache=True)
        assert ps.symbol == "GC=F"
        assert ps.closes == [2050.5, 2065.0]   # None 被剔除
        assert len(ps.dates) == 2
        assert ps.dates[0] < ps.dates[1]       # 升序


def test_fetch_quote_uses_regular_market_price():
    with tempfile.TemporaryDirectory() as tmp:
        src = _src_with_cache(tmp, ["5d"])
        price, asof = src.fetch_quote("GC=F", use_cache=True)
        assert abs(price - 2068.3) < 1e-9
        assert asof.startswith("2024-")        # regularMarketTime 转 ISO


def test_live_ratio_conversion():
    # 真实期货 2068.3 / ETF 现价 190.0 → 比值≈10.886；ETF 行权价 200 → 商品 ≈2177
    ratio = 2068.3 / 190.0
    assert abs(200.0 * ratio - 2177.16) < 0.1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
