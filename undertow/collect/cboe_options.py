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

from undertow.core.config import Instrument
from undertow.collect.cache import FileCache
from undertow.core.models import OptionContract, OptionsSnapshot
from undertow.collect.base import DataSourceError, http_get_json

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
            bid=_to_float(o.get("bid")),
            ask=_to_float(o.get("ask")),
            bid_size=_to_int(o.get("bid_size")),
            ask_size=_to_int(o.get("ask_size")),
        ))

    return OptionsSnapshot(
        instrument=instrument_key,
        proxy_symbol=sym,
        spot=spot,
        asof=asof,
        contracts=contracts,
    )


def chain_fingerprint(snap: OptionsSnapshot) -> str:
    """期权**持仓结构**指纹（每行权价 expiry/kind/strike/OI），用于识别"无新持仓数据"。

    只认 OI，**不含现价与 volume**——这是刻意的：
    - 休市日（周末/节假日）：OI 不变 → 指纹相同 → 跳过，不把重复快照落成新的一天。
    - 交易日 ET 凌晨 OCC 尚未发布隔夜结算 OI 时：CBOE 延迟报价里 OI 仍是上一交易日
      的结算值，但现价/volume 已刷新成当日值。若指纹含现价/volume 就会误判为"新数据"
      而落盘一份 OI 未结算的残缺快照（现价新、OI 旧），使 flow 层日对日 diff 退化成
      全 0（ΔOI≡0）。只认 OI 后，这种"OI 未结算"状态会被正确识别为无新数据→跳过，
      交给定时任务的后续重试点在 OCC 发布后再抓（见 scripts/daily_update.sh）。
    我们的情报核心是持仓量（OI），现价另由期货源实时提供、volume 日内累积本就多变，
    二者都不该参与"是否有新持仓"的判定。
    """
    import hashlib
    # ⚠️ 只取 OI>0 的行。交易所每天都会新挂一批 OI=0 的行权价/到期，
    # 若把它们计入指纹，"持仓完全没变"也会因为多了几百条空合约而哈希不同 →
    # 放行落盘一份 OI 未结算的残缺快照，正是本函数声称要防的那种。
    # 2026-08-27 实测：SPY 新增 388 条、IWM 新增 236 条，全部 OI=0，
    # 而两者已有合约的总 |ΔOI| 恰为 0 —— 指纹却判定为"新数据"。
    rows = sorted(
        (c.expiry.isoformat(), c.kind, round(c.strike, 4), c.open_interest)
        for c in snap.contracts if (c.open_interest or 0) > 0
    )
    h = hashlib.md5()
    h.update(repr(rows).encode("utf-8"))
    return h.hexdigest()


def oi_change_total(prev: OptionsSnapshot, curr: OptionsSnapshot) -> int:
    """两份快照之间【已建仓合约】的 OI 变动总量 Σ|ΔOI|（按 到期/类型/行权价 对齐）。

    比 chain_fingerprint 更严格，用来判定"OCC 隔夜结算是否已落地"。
    指纹是【单快照】函数，判不了两类情形：
      1. 交易所新挂 OI=0 的行权价（已在指纹里排除）；
      2. **合约到期消失** —— 存活合约的 OI 一张没动，但 OI>0 的行集合变了，
         指纹照样不同 → 放行一份 OI 未结算的残缺快照。
    2026-08-27 实测：GLD 在指纹修好之后仍因到期滚出而被放行，Σ|ΔOI| 恰为 0。

    返回 0 表示【没有任何已建仓合约的持仓发生变化】＝ OI 尚未结算，不应落盘。
    """
    pm: dict = {}
    for c in prev.contracts:
        pm[(c.expiry, c.kind, round(c.strike, 4))] = c.open_interest or 0
    total = 0
    seen = set()
    for c in curr.contracts:
        k = (c.expiry, c.kind, round(c.strike, 4))
        seen.add(k)
        total += abs((c.open_interest or 0) - pm.get(k, 0))
    # 消失的合约（到期滚出）不计入：它们的"归零"不是持仓变化，是合约不存在了
    return total


def materially_same(a: OptionsSnapshot, b: OptionsSnapshot) -> bool:
    """两份快照的【物质内容】是否完全一致 —— 用于同日重复抓取的去重。

    2026-09-25 实测 GLD 2026-09-22 在 git 里的 4 个版本（ET 04:00/05:00/06:00/06:45
    四个时点各落盘一次）：

        Σ|ΔOI| = 0 · Σ|Δvolume| = 0 · 合约数差 = 0 · 源 ts 全为 2026-09-21T15:59:59
        唯一差别：current_price 398.38 → 395.89（延迟报价随盘前走）

    也就是说后三份**没有任何物质增量**，却在 git 里各占一个 417KB 的 blob。
    全库因此多出 71MB（快照工作区 117MB vs git 历史 188MB）。

    「物质」的定义只含三样 —— 它们是分析层真正吃进去的东西：
      · 合约集合（新挂/到期滚出都算变化）
      · 每个合约的 OI（OCC 隔夜结算，日内恒定）
      · 每个合约的 volume（盘前抓取时已是上一交易日的最终值，同样恒定）

    **spot 刻意不计入。** 它是"抓取时刻的延迟报价，介于 D−1 收盘与 D 盘前之间，
    不精确等于任一收盘"（见 flow.py 时序约定）—— 四个时点的 spot 没有哪个更对，
    拿它当差异判据只会让去重永远不生效。

    ⚠️ 只用于【同一天】的重复抓取。跨日去重必须继续用 oi_change_total()：
    那里要回答的是"OCC 结算到了没有"，是另一个问题。
    """
    if len(a.contracts) != len(b.contracts):
        return False
    key = lambda c: (c.expiry, c.kind, round(c.strike, 4))
    am = {key(c): (c.open_interest or 0, c.volume or 0) for c in a.contracts}
    if len(am) != len(a.contracts):        # 键冲突 → 判不了，宁可当作不同
        return False
    for c in b.contracts:
        if am.get(key(c)) != (c.open_interest or 0, c.volume or 0):
            return False
    return True


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
