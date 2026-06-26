"""CBOE 历史日线数据源 —— 免费、免 key、与期权同一 host。

来源: https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{SYM}.json
返回全历史日 OHLCV。我们用 GLD/SLV/USO 作商品价格代理（收益对齐用，跟踪误差小；
USO 因展期损耗对 WTI 有偏差，已在 config/报告中标注）。
"""
from __future__ import annotations

from datetime import datetime

from ..config import Instrument
from ..cache import FileCache
from ..models import PriceSeries
from .base import DataSourceError, http_get_json

CBOE_HIST_URL = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{symbol}.json"


class CboeHistorySource:
    name = "cboe_history"
    CACHE_TTL = 12 * 3600  # 日线一天更新一次，缓存半天

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def fetch_series(self, instrument: Instrument, *, use_cache: bool = True) -> PriceSeries:
        if instrument.price is None:
            raise DataSourceError(f"{instrument.key} 未配置 price 数据源")
        sym = instrument.price.symbol
        cache_key = f"cboehist_{sym}"

        payload = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
        if payload is None:
            payload = http_get_json(CBOE_HIST_URL.format(symbol=sym))
            self.cache.set(cache_key, payload)

        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise DataSourceError(f"CBOE 历史无 data 列表（{sym}）")

        pairs = []
        for r in rows:
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
                c = float(r["close"])
            except (KeyError, ValueError, TypeError):
                continue
            pairs.append((d, c))
        pairs.sort(key=lambda x: x[0])
        return PriceSeries(symbol=sym, dates=[d for d, _ in pairs], closes=[c for _, c in pairs])
