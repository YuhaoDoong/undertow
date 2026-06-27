"""CBOE 期权数据源 —— 免费的延迟报价 JSON 接口。

来源: https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json
公开延迟数据（非 CME 那种禁止抓取的实时数据），每个行权价直接带
open_interest / gamma / delta / iv，省去自己定价的麻烦（仍会用 BS 交叉校验）。

注意：我们用 ETF 期权（GLD/SLV/USO）作为 COMEX 商品期权的【代理】——
合法、可脚本化、但不是文章读的那张 COMEX 期权表。代理质量与换算见 config。
后续若接入付费 COMEX 源，只需在此再加一个 source 实现，分析层不变。
"""
from __future__ import annotations

from datetime import date, datetime

from ..config import Instrument
from ..cache import FileCache
from ..models import OptionContract, OptionsSnapshot
from .base import DataSourceError, http_get_json

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"


def parse_occ(symbol: str) -> tuple[str, date, str, float]:
    """解析 OCC 期权代码，如 'GLD260918P00358000'。

    从右往左切，兼容任意长度的 root:
        最后 8 位 = 行权价 ×1000；前 1 位 = C/P；再前 6 位 = YYMMDD；其余 = root。
    """
    strike = int(symbol[-8:]) / 1000.0
    kind = symbol[-9]
    yymmdd = symbol[-15:-9]
    root = symbol[:-15]
    expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    return root, expiry, kind, strike


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def snapshot_from_payload(payload: dict, instrument_key: str, sym: str) -> OptionsSnapshot:
    """从 CBOE 原始 payload 还原 OptionsSnapshot（无 I/O）。

    抽成自由函数，便于「快照仓库」把落盘的历史原始 payload 也还原成同一套模型，
    供 flow 层做日对日 diff——不必再走网络。
    """
    data = payload.get("data") or {}
    if "options" not in data:
        raise DataSourceError(f"CBOE 返回无 options 字段（{sym}）")

    spot = _to_float(data.get("current_price") or data.get("close"))
    asof = payload.get("timestamp", "")

    contracts: list[OptionContract] = []
    for o in data["options"]:
        try:
            _root, expiry, kind, strike = parse_occ(o["option"])
        except (ValueError, KeyError):
            continue
        contracts.append(OptionContract(
            expiry=expiry,
            strike=strike,
            kind=kind,
            open_interest=_to_int(o.get("open_interest")),
            volume=_to_int(o.get("volume")),
            gamma=_to_float(o.get("gamma")),
            delta=_to_float(o.get("delta")),
            iv=_to_float(o.get("iv")),
        ))

    return OptionsSnapshot(
        instrument=instrument_key,
        proxy_symbol=sym,
        spot=spot,
        asof=asof,
        contracts=contracts,
    )


def chain_fingerprint(snap: OptionsSnapshot) -> str:
    """期权链内容指纹（现价 + 每行权价 OI/volume），用于识别"无新数据"。

    休市日（周末/节假日）CBOE 延迟报价仍是上一交易日的同一份数据——OI/volume/价
    全部不变。把这种重复快照落成新的一天，会让 flow 层的日对日 diff 退化成全 0
    （ΔOI≡0），悄悄抹掉买卖方信号。落盘前用指纹比对上一份：相同则跳过，不污染序列。
    """
    import hashlib
    rows = sorted(
        (c.expiry.isoformat(), c.kind, round(c.strike, 4), c.open_interest, c.volume)
        for c in snap.contracts
    )
    h = hashlib.md5()
    h.update(repr((round(snap.spot, 4), rows)).encode("utf-8"))
    return h.hexdigest()


class CboeOptionsSource:
    name = "cboe_etf"
    # 延迟报价日内会变，但对"人工监控"30 分钟缓存够用；调试可 use_cache=False
    CACHE_TTL = 30 * 60

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def fetch_raw(self, instrument: Instrument, *, use_cache: bool = True) -> dict:
        """取回 CBOE 原始 payload（全字段）。给「快照仓库」落盘用——
        我们要尽量多存原始数据，而不是只存解析后的子集。"""
        if instrument.options is None:
            raise DataSourceError(f"{instrument.key} 未配置 options 数据源")
        sym = instrument.options.symbol
        cache_key = f"cboe_{sym}"

        payload = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
        if payload is None:
            payload = http_get_json(CBOE_URL.format(symbol=sym))
            self.cache.set(cache_key, payload)
        return payload

    def fetch_snapshot(self, instrument: Instrument, *, use_cache: bool = True) -> OptionsSnapshot:
        payload = self.fetch_raw(instrument, use_cache=use_cache)
        return snapshot_from_payload(payload, instrument.key, instrument.options.symbol)
