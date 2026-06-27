"""CBOE 波动率指数数据源（GVZ 黄金 / OVX 原油 / VXSLV 白银，仅标准库）。

GVZ=基于 GLD 期权的隐含波动率指数（黄金版"VIX"）；OVX=原油；VXSLV=白银。
作用：情绪/不确定性温度计。高位=避险/恐慌、区间放大、谨慎；低位=自满、可能积蓄变盘。
非方向指标，作【背景/置信度调节】，与持仓·期权微观结构共振时参考。

接口：cdn.cboe.com/api/global/us_indices/daily_prices/{SYM}_History.csv
CSV：GVZ/OVX 为 `DATE,VALUE`，VXSLV 为 `DATE,OPEN,HIGH,LOW,CLOSE`——统一取最后一列为收。
日期格式 MM/DD/YYYY。
"""
from __future__ import annotations

from datetime import datetime, date

from undertow.collect.cache import FileCache
from undertow.collect.base import DataSourceError, http_get_text

CBOE_VOL_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"


class CboeVolSource:
    name = "cboe_vol"
    CACHE_TTL = 12 * 3600

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def fetch_series(self, sym: str, *, use_cache: bool = True) -> list[tuple[date, float]]:
        cache_key = f"cboevol_{sym}"
        text = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
        if text is None:
            text = http_get_text(CBOE_VOL_URL.format(sym=sym),
                                 headers={"User-Agent": "Mozilla/5.0"})
            self.cache.set(cache_key, text)
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> list[tuple[date, float]]:
        out: list[tuple[date, float]] = []
        for line in text.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            try:
                d = datetime.strptime(parts[0].strip(), "%m/%d/%Y").date()
                out.append((d, float(parts[-1])))  # 最后一列=收盘（兼容 2 列与 OHLC）
            except ValueError:
                continue
        if not out:
            raise DataSourceError("CBOE 波动率指数解析为空")
        return out
