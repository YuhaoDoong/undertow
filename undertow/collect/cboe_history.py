"""CBOE 历史日线数据源 —— 免费、免 key、与期权同一 host。

来源: https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{SYM}.json
返回全历史日 OHLCV。我们用 GLD/SLV/USO 作商品价格代理（收益对齐用，跟踪误差小；
USO 因展期损耗对 WTI 有偏差，已在 config/报告中标注）。
"""
from __future__ import annotations

from datetime import datetime

from undertow.core.config import Instrument
from undertow.collect.cache import FileCache
from undertow.core.models import PriceSeries
from undertow.collect.base import DataSourceError, http_get_json

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

        # 同时取 high/low：台账的 MFE/MAE（盘中最有利/最不利偏移）需要它们。
        # 2026-09-23 用户问「盘中跌下去又被拉回来的，统计里算失败吗」——是的，
        # 旧口径只吃 closes。实测 67 个开火信号：收盘口径 70.1%、盘中极值口径 79.1%，
        # 其中 6 个（9%）是「盘中到过、收盘回吐」被判失败。
        # ⚠️ high/low 缺失时整行跳过而不是折成 close —— 折值会让 MAE 系统性偏小，
        #    而 MAE 正是卖方结构判断「有没有被盘中击穿」的唯一依据（低估风险比高估危险）。
        rowsc = []
        for r in rows:
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
                c = float(r["close"])
                h = float(r["high"])
                lo = float(r["low"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (lo <= c <= h):      # 脏行：极值与收盘矛盾，宁可丢弃不可污染
                continue
            rowsc.append((d, c, h, lo))
        rowsc.sort(key=lambda x: x[0])
        return PriceSeries(symbol=sym,
                           dates=[x[0] for x in rowsc],
                           closes=[x[1] for x in rowsc],
                           highs=[x[2] for x in rowsc],
                           lows=[x[3] for x in rowsc])
