"""配置加载：实例注册表与路径解析。

所有"非代码"的可调项（盯哪些品种、合约代码、回看周数）都集中在
config/instruments.json，代码只读不写。新增品种 = 改 JSON，不改代码。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# 项目根 = 本文件所在包的上一级目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "instruments.json"
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"


@dataclass(frozen=True)
class CotSpec:
    """一个品种在 COT 报告中的定位。"""

    report: str  # 报告类型，目前支持 "disaggregated_fut"
    contract_market_code: str  # CFTC 合约代码，如黄金 088691
    market_name: str  # 人类可读市场名（用于校验/展示）


@dataclass(frozen=True)
class OptionsSpec:
    """期权数据定位。当前用 ETF 期权作为商品期权的代理。"""

    source: str  # 数据源标识，目前支持 "cboe_etf"
    symbol: str  # 代理标的，如黄金用 GLD
    approx_commodity_multiplier: float | None  # ETF价 × 此乘数 ≈ 现货商品价；None=无稳定映射
    proxy_quality: str  # good / weak —— 代理质量提示
    note: str


@dataclass(frozen=True)
class PriceSpec:
    """价格序列定位（用于回测/价格对齐）。"""

    source: str  # 目前支持 "cboe_history"
    symbol: str
    quality: str
    note: str


@dataclass(frozen=True)
class Instrument:
    key: str  # 内部标识，如 "gold"
    display_name: str
    asset_class: str
    cot: CotSpec
    options: OptionsSpec | None = None
    price: PriceSpec | None = None
    commodity: PriceSpec | None = None  # 真实商品期货价（GC=F/SI=F/CL=F）


@dataclass(frozen=True)
class Config:
    instruments: dict[str, Instrument]
    defaults: dict[str, Any]

    def get(self, key: str) -> Instrument:
        try:
            return self.instruments[key]
        except KeyError:
            valid = ", ".join(sorted(self.instruments))
            raise KeyError(f"未知品种 '{key}'，可用: {valid}") from None

    @property
    def lookback_weeks(self) -> int:
        return int(self.defaults.get("lookback_weeks", 156))


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    """读取并缓存实例注册表。"""
    cfg_path = path or CONFIG_PATH
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    instruments: dict[str, Instrument] = {}
    for key, spec in raw.get("instruments", {}).items():
        cot = spec["cot"]
        opts_raw = spec.get("options")
        options = None
        if opts_raw:
            options = OptionsSpec(
                source=opts_raw["source"],
                symbol=opts_raw["symbol"],
                approx_commodity_multiplier=opts_raw.get("approx_commodity_multiplier"),
                proxy_quality=opts_raw.get("proxy_quality", "unknown"),
                note=opts_raw.get("note", ""),
            )
        def _price_spec(raw):
            if not raw:
                return None
            return PriceSpec(source=raw["source"], symbol=raw["symbol"],
                             quality=raw.get("quality", "unknown"), note=raw.get("note", ""))

        instruments[key] = Instrument(
            key=key,
            display_name=spec["display_name"],
            asset_class=spec.get("asset_class", "unknown"),
            cot=CotSpec(
                report=cot["report"],
                contract_market_code=cot["contract_market_code"],
                market_name=cot["market_name"],
            ),
            options=options,
            price=_price_spec(spec.get("price")),
            commodity=_price_spec(spec.get("commodity")),
        )
    return Config(instruments=instruments, defaults=raw.get("defaults", {}))
