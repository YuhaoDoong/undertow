"""FRED 宏观数据源 —— 美联储经济数据库（免 key 的 fredgraph.csv，仅标准库）。

为什么加它：黄金/白银的【基本面驱动】是宏观——尤其 **10 年期实际利率(TIPS)** 和
**美元指数**：实际利率越低、美元越弱，持有无息黄金的机会成本越小→越利多。
这是和"持仓/期权"完全不同的维度（另一个项目 /Users/yhdong/Gold 的思路），
把它作为"宏观背景"并入综合研判，能交叉验证微观结构信号。

合法/可达：FRED 官方 `fredgraph.csv?id=SERIES` 免 key、返回全历史 CSV，本机实测可达。
解析：CSV 头 `observation_date,SERIES`，缺失值标记为 "."（跳过）。
"""
from __future__ import annotations

from datetime import date

from undertow.collect.cache import FileCache
from undertow.collect.base import DataSourceError, http_get_text

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class FredMacroSource:
    name = "fred"
    CACHE_TTL = 12 * 3600  # 宏观日频，12h 缓存够用

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def fetch_series(self, series_id: str, *, use_cache: bool = True) -> list[tuple[date, float]]:
        """取某 FRED 序列的 (日期, 值) 升序列表，跳过缺失。"""
        cache_key = f"fred_{series_id}"
        text = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
        if text is None:
            # FRED 拒绝非浏览器 UA，需带 Mozilla UA
            text = http_get_text(FRED_CSV, params={"id": series_id},
                                 headers={"User-Agent": "Mozilla/5.0"})
            self.cache.set(cache_key, text)
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> list[tuple[date, float]]:
        out: list[tuple[date, float]] = []
        lines = text.strip().splitlines()
        for line in lines[1:]:  # 跳过表头
            parts = line.split(",")
            if len(parts) < 2:
                continue
            ds, vs = parts[0].strip(), parts[1].strip()
            if vs in (".", ""):  # FRED 缺失标记
                continue
            try:
                out.append((date.fromisoformat(ds), float(vs)))
            except ValueError:
                continue
        if not out:
            raise DataSourceError("FRED 序列解析为空")
        return out
