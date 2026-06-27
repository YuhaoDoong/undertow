"""CFTC COT（持仓报告）数据源 —— Disaggregated 周报。

来源: https://publicreporting.cftc.gov （Socrata 开放数据，免费、官方）
数据集: 72hh-3qpy = Disaggregated Futures-Only（物理大宗商品用这个）
发布: 每周五 15:30 ET，数据截止当周二（即天然滞后约 3 天）。

本模块负责把 CFTC 的 194 字段映射成 models.CotReport 的语义类别。
字段名经实测核对；注意 swap 短/套利字段是双下划线（CFTC 命名不一致）。
"""
from __future__ import annotations

from datetime import date, datetime

from undertow.core.config import CotSpec, Instrument
from undertow.collect.cache import FileCache
from undertow.core.models import CategoryChange, CotReport, TraderCategory
from undertow.collect.base import DataSource, http_get_json

# Socrata 数据集端点
DATASETS = {
    "disaggregated_fut": "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
}

# 类别 -> (long字段, short字段, spread字段或None)  —— 当前持仓
_POSITION_FIELDS = {
    "managed_money": ("m_money_positions_long_all", "m_money_positions_short_all", "m_money_positions_spread"),
    "other_reportables": ("other_rept_positions_long", "other_rept_positions_short", "other_rept_positions_spread"),
    # 注意：swap 的 short/spread 是双下划线，long 是单下划线
    "swap_dealers": ("swap_positions_long_all", "swap__positions_short_all", "swap__positions_spread_all"),
    "producer_merchant": ("prod_merc_positions_long", "prod_merc_positions_short", None),
    "nonreportable": ("nonrept_positions_long_all", "nonrept_positions_short_all", None),
}

# 类别 -> (long变化, short变化, spread变化或None)  —— 周环比变化（CFTC 自带）
_CHANGE_FIELDS = {
    "managed_money": ("change_in_m_money_long_all", "change_in_m_money_short_all", "change_in_m_money_spread"),
    "other_reportables": ("change_in_other_rept_long", "change_in_other_rept_short", "change_in_other_rept_spread"),
    "swap_dealers": ("change_in_swap_long_all", "change_in_swap_short_all", "change_in_swap_spread_all"),
    "producer_merchant": ("change_in_prod_merc_long", "change_in_prod_merc_short", None),
    "nonreportable": ("change_in_nonrept_long_all", "change_in_nonrept_short_all", None),
}


def _to_int(value) -> int:
    if value in (None, "", "."):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_date(value: str) -> date:
    # 形如 "2022-08-02T00:00:00.000"
    return datetime.fromisoformat(value.split("T")[0]).date()


class CftcCotSource(DataSource):
    name = "cftc_cot"

    # COT 每周才更新，缓存 6 小时足够；调试时可传 use_cache=False
    CACHE_TTL = 6 * 3600

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def _endpoint(self, spec: CotSpec) -> str:
        if spec.report not in DATASETS:
            raise ValueError(f"未支持的报告类型: {spec.report}")
        return DATASETS[spec.report]

    def fetch_history(
        self, instrument: Instrument, *, lookback: int = 156, use_cache: bool = True
    ) -> list[CotReport]:
        """拉取该品种最近 lookback 期 COT 周报，按日期升序返回。"""
        spec = instrument.cot
        cache_key = f"cot_{spec.report}_{spec.contract_market_code}_{lookback}"

        records = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
        if records is None:
            records = self._query(spec, lookback)
            self.cache.set(cache_key, records)

        reports = [self._parse(instrument, r) for r in records]
        reports.sort(key=lambda x: x.report_date)
        return reports

    def _query(self, spec: CotSpec, lookback: int) -> list[dict]:
        params = {
            "$where": f"cftc_contract_market_code='{spec.contract_market_code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": lookback,
        }
        data = http_get_json(self._endpoint(spec), params)
        if not data:
            raise ValueError(
                f"CFTC 未返回数据，检查合约代码 {spec.contract_market_code} 是否正确"
            )
        return data

    def _parse(self, instrument: Instrument, rec: dict) -> CotReport:
        def category(name: str) -> TraderCategory:
            lf, sf, spf = _POSITION_FIELDS[name]
            return TraderCategory(
                long=_to_int(rec.get(lf)),
                short=_to_int(rec.get(sf)),
                spread=_to_int(rec.get(spf)) if spf else 0,
            )

        changes: dict[str, CategoryChange] = {}
        for name, (lf, sf, spf) in _CHANGE_FIELDS.items():
            changes[name] = CategoryChange(
                long=_to_int(rec.get(lf)),
                short=_to_int(rec.get(sf)),
                spread=_to_int(rec.get(spf)) if spf else 0,
            )

        return CotReport(
            instrument=instrument.key,
            report_date=_parse_date(rec["report_date_as_yyyy_mm_dd"]),
            market_name=rec.get("market_and_exchange_names", instrument.cot.market_name),
            open_interest=_to_int(rec.get("open_interest_all")),
            open_interest_change=_to_int(rec.get("change_in_open_interest_all")),
            managed_money=category("managed_money"),
            other_reportables=category("other_reportables"),
            swap_dealers=category("swap_dealers"),
            producer_merchant=category("producer_merchant"),
            nonreportable=category("nonreportable"),
            changes=changes,
            raw=rec,
        )
