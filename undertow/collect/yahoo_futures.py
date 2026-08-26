"""真实商品期货价数据源 —— Yahoo Finance chart JSON 接口（仅标准库 urllib）。

为什么需要它：期权链是 ETF（GLD/SLV/USO）的，位点本是 ETF 价；要落到
**真实商品价**（COMEX 黄金/白银期货、NYMEX WTI），靠 ETF×固定乘数会随基金费率漂移，
USO 更是与 WTI 价格量级都不一致。正解是用【当日实时比值】= 真实期货价 / ETF 价
来换算所有位点——本源负责提供那个真实期货价（现价 + 历史日线）。

合法性：用的是 Yahoo 公开 chart 接口（yfinance 库底层也是打这个），非 CME 那种
明令禁止抓取的源；调用频率低（每日几次）。符号：黄金 GC=F、白银 SI=F、WTI CL=F。

保持项目"纯标准库"：不引 yfinance/pandas，直接 urllib + json 解析。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from undertow.core.config import Instrument
from undertow.collect.cache import FileCache
from undertow.core.models import PriceSeries
from undertow.collect.base import DataSourceError, http_get_json

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooFuturesSource:
    name = "yahoo_futures"
    CACHE_TTL = 6 * 3600  # 期货价 6h 缓存够"人工监控"用

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def _fetch_payload(self, symbol: str, rng: str, use_cache: bool) -> dict:
        cache_key = f"yahoo_{symbol}_{rng}"
        payload = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
        if payload is None:
            # ⚠️ range=max 会被 Yahoo 自动降频成【月线】（SPY 实测只回 404 根）。
            # 要拿全历史日线，必须给显式 period1/period2 区间。
            if rng == "max":
                params = {"period1": 0, "period2": int(time.time()), "interval": "1d"}
            else:
                params = {"range": rng, "interval": "1d"}
            payload = http_get_json(
                YAHOO_URL.format(symbol=symbol),
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            self.cache.set(cache_key, payload)
        return payload

    @staticmethod
    def _result(payload: dict) -> dict:
        try:
            res = payload["chart"]["result"][0]
        except (KeyError, IndexError, TypeError):
            err = (payload.get("chart") or {}).get("error")
            raise DataSourceError(f"Yahoo 返回无 chart 数据: {err}")
        return res

    def fetch_series(self, symbol: str, *, rng: str = "2y", use_cache: bool = True) -> PriceSeries:
        """真实期货日线（升序）。"""
        res = self._result(self._fetch_payload(symbol, rng, use_cache))
        ts = res.get("timestamp") or []
        quote = (res.get("indicators", {}).get("quote") or [{}])[0]
        closes_raw = quote.get("close") or []
        highs_raw = quote.get("high") or []
        lows_raw = quote.get("low") or []
        dates, closes, highs, lows = [], [], [], []
        for i, (t, c) in enumerate(zip(ts, closes_raw)):
            if c is None:
                continue
            dates.append(datetime.fromtimestamp(t, tz=timezone.utc).date())
            closes.append(float(c))
            h = highs_raw[i] if i < len(highs_raw) else None
            l = lows_raw[i] if i < len(lows_raw) else None
            # 个别 bar 缺高低价时用收盘顶替，保证与 closes 等长的不变式
            highs.append(float(h) if h is not None else float(c))
            lows.append(float(l) if l is not None else float(c))
        if not closes:
            raise DataSourceError(f"Yahoo {symbol} 无有效收盘价")
        return PriceSeries(symbol=symbol, dates=dates, closes=closes,
                           highs=highs, lows=lows)

    def fetch_quote(self, symbol: str, *, use_cache: bool = True) -> tuple[float, str]:
        """最新真实期货价 + 时间戳。优先 regularMarketPrice，回退最后一根日线收盘。"""
        res = self._result(self._fetch_payload(symbol, "5d", use_cache))
        meta = res.get("meta", {})
        price = meta.get("regularMarketPrice")
        ts = meta.get("regularMarketTime")
        if price is None:
            ser = self.fetch_series(symbol, rng="5d", use_cache=use_cache)
            price, asof = ser.closes[-1], ser.dates[-1].isoformat()
            return float(price), asof
        asof = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "")
        return float(price), asof

    def fetch_for(self, instrument: Instrument, *, rng: str = "2y",
                  use_cache: bool = True) -> tuple[PriceSeries, float, str]:
        """便捷：按 instrument.commodity.symbol 取 (历史日线, 现价, 时间戳)。"""
        if instrument.commodity is None:
            raise DataSourceError(f"{instrument.key} 未配置 commodity（真实期货）数据源")
        sym = instrument.commodity.symbol
        series = self.fetch_series(sym, rng=rng, use_cache=use_cache)
        price, asof = self.fetch_quote(sym, use_cache=use_cache)
        return series, price, asof
