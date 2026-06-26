"""期权资金流 / 持仓异动分析（确定性计算，无 I/O）。

动机（来自文章作者 6/24 的实战判断）:
  作者抓的不是「静态的墙」(那是 gamma.py 的活)，而是【墙的增量】——
  "6/22 七月 4000 put 单日 OI +1461、IV +1.13pp（新买盘涌入）→ 极强看跌；
   6/23 又见卖方撤退 → 机构认为黄金大概率跌破 4000"。结果 6/24 黄金确实破 4000。
  这种【临近到期 + 大单 ΔOI + ΔIV 同向】的异动，是静态快照看不见的高价值信号。

本模块两件事:
  1) scan_unusual(snap)        —— 单张快照即可：按 volume / OI 比找"今日异常活跃"的
     行权价。volume ≫ OI 说明今天有大量新交易涌入（明天往往兑现为新 OI）。
     【今天就能用，不需要历史】。
  2) analyze_flow(prev, curr)  —— 两日快照 diff：逐 (到期,行权价,C/P) 求 ΔOI / ΔIV，
     按 |ΔOI| 排序，分类"看跌/看涨持仓增建 vs 减仓"，并叠加静态墙位
     （新 OI 正堆在 put 墙上 + IV 升 = 作者那种自我实现的破位预警）。
     【需要至少两天落盘的快照；CBOE 无期权历史，只能从落盘当天起往后攒】。

诚实标注（已写进报告）:
  * 延迟数据无逐笔成交，无法严格区分主动买/卖；本模块用
    「持仓方向(C/P) × OI 增减 × IV 方向」做【启发式】推断，不是成交主动性。
  * put 减仓本身歧义（获利了结偏多 / 卖方撤退偏空），仅作展示不计入净倾向。
  * 净倾向只用【增建】侧（新建 put=偏空、新建 call=偏多，方向无歧义）聚合。
  * 单标的 ETF 代理、样本短，只作预警、不作预言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models import OptionsSnapshot, OptionContract

DEFAULT_HORIZON_DAYS = 60      # 只看近月（远月异动对短期方向意义小）
NEAR_MONEY_BAND = 0.15        # 只看现价 ±15% 内的行权价
MIN_DOI = 50                  # |ΔOI| 低于此视为噪音
TOP_N = 15                    # 异动榜最多展示条数
UNUSUAL_MIN_VOLUME = 50       # 单快照异动：最低成交量门槛
UNUSUAL_MIN_VOL_OI = 0.5      # volume ≥ 0.5×OI 视为"今日活跃"


@dataclass(frozen=True)
class UnusualContract:
    """单快照里"今日异常活跃"的一个合约。"""
    expiry: date
    strike: float
    kind: str
    open_interest: int
    volume: int
    iv: float
    vol_oi_ratio: float
    moneyness: float   # strike/spot - 1
    note: str


@dataclass(frozen=True)
class FlowChange:
    """两日 diff 里一个 (到期,行权价,C/P) 的持仓异动。"""
    expiry: date
    strike: float
    kind: str
    prev_oi: int
    curr_oi: int
    d_oi: int
    prev_iv: float
    curr_iv: float
    d_iv_pp: float       # (curr-prev) × 100，单位 pp
    curr_volume: int
    moneyness: float
    bias: str            # bearish / bullish / unwind / neutral
    on_wall: str         # "put墙" / "call墙" / ""
    note: str


@dataclass(frozen=True)
class FlowAnalysis:
    instrument: str
    proxy_symbol: str
    spot: float
    horizon_days: int
    curr_date: str
    curr_asof: str
    prev_date: str | None          # None=只有一份快照，仅出单快照异动
    # —— 单快照异动 ——
    unusual: list[UnusualContract] = field(default_factory=list)
    total_call_volume: int = 0
    total_put_volume: int = 0
    # —— 两日 diff ——
    changes: list[FlowChange] = field(default_factory=list)
    net_call_doi: int = 0          # 近月增建的 call OI 净增（只计正向增建）
    net_put_doi: int = 0           # 近月增建的 put OI 净增
    flow_tilt: str = "—"
    # 墙位上下文（来自 gamma，便于叠加判断）
    call_wall: float | None = None
    put_wall: float | None = None


def _yearfrac(expiry: date, today: date) -> float:
    return (expiry - today).days / 365.0


def _live(snap: OptionsSnapshot, today: date, horizon_days: int) -> list[OptionContract]:
    """近月、未到期、在现价 ±NEAR_MONEY_BAND 内的合约。"""
    if snap.spot <= 0:
        return []
    lo, hi = snap.spot * (1 - NEAR_MONEY_BAND), snap.spot * (1 + NEAR_MONEY_BAND)
    out = []
    for c in snap.contracts:
        T = _yearfrac(c.expiry, today)
        if 0 < T <= horizon_days / 365.0 and lo <= c.strike <= hi:
            out.append(c)
    return out


def scan_unusual(snap: OptionsSnapshot, *, today: date,
                 horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[UnusualContract]:
    """单快照异动扫描：volume 大、且 volume/OI 高 = 今日新活跃（不需历史）。"""
    spot = snap.spot
    out: list[UnusualContract] = []
    for c in _live(snap, today, horizon_days):
        if c.volume < UNUSUAL_MIN_VOLUME:
            continue
        ratio = c.volume / c.open_interest if c.open_interest > 0 else float("inf")
        if ratio < UNUSUAL_MIN_VOL_OI:
            continue
        kind_cn = "看跌put" if c.kind == "P" else "看涨call"
        fresh = "量≫OI(疑全新建仓)" if ratio >= 1.0 else "量/OI偏高"
        out.append(UnusualContract(
            expiry=c.expiry, strike=c.strike, kind=c.kind,
            open_interest=c.open_interest, volume=c.volume, iv=c.iv,
            vol_oi_ratio=ratio, moneyness=(c.strike / spot - 1.0) if spot else 0.0,
            note=f"{kind_cn}·{fresh}",
        ))
    # 先按到期升序（近月优先），再按成交量降序
    out.sort(key=lambda u: (u.expiry, -u.volume))
    return out[:TOP_N]


def _classify(kind: str, d_oi: int, d_iv_pp: float) -> tuple[str, str]:
    """按 持仓方向 × OI 增减 × IV 方向 做启发式方向推断。"""
    iv_tag = "·IV升" if d_iv_pp > 0.1 else ("·IV降" if d_iv_pp < -0.1 else "")
    if d_oi > 0:  # 增建（方向无歧义）
        if kind == "P":
            return "bearish", f"看跌持仓增建{iv_tag}" + ("(恐慌升温)" if d_iv_pp > 0.1 else "")
        return "bullish", f"看涨持仓增建{iv_tag}"
    else:         # 减仓（歧义，不计入净倾向）
        if kind == "P":
            return "unwind", f"看跌持仓减仓{iv_tag}(获利了结偏多/卖方撤退偏空,歧义)"
        return "unwind", f"看涨持仓减仓{iv_tag}(平多/获利了结)"


def analyze_flow(
    prev: OptionsSnapshot | None,
    curr: OptionsSnapshot,
    *,
    today: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    call_wall: float | None = None,
    put_wall: float | None = None,
    prev_date: str | None = None,
    curr_date: str | None = None,
) -> FlowAnalysis:
    spot = curr.spot

    # —— 单快照异动（总能出）——
    unusual = scan_unusual(curr, today=today, horizon_days=horizon_days)
    tcv = sum(c.volume for c in _live(curr, today, horizon_days) if c.kind == "C")
    tpv = sum(c.volume for c in _live(curr, today, horizon_days) if c.kind == "P")

    changes: list[FlowChange] = []
    net_call = net_put = 0
    tilt = "—（仅一份快照，明天起可出日对日 ΔOI/ΔIV 异动）"

    if prev is not None:
        # 以 (到期iso, 行权价, C/P) 为键对齐两日
        prev_map: dict[tuple, OptionContract] = {
            (c.expiry.isoformat(), c.strike, c.kind): c for c in prev.contracts
        }
        for c in _live(curr, today, horizon_days):
            key = (c.expiry.isoformat(), c.strike, c.kind)
            p = prev_map.get(key)
            prev_oi = p.open_interest if p else 0
            prev_iv = p.iv if p else 0.0
            d_oi = c.open_interest - prev_oi
            if abs(d_oi) < MIN_DOI:
                continue
            d_iv_pp = (c.iv - prev_iv) * 100.0 if (p and prev_iv > 0 and c.iv > 0) else 0.0
            bias, note = _classify(c.kind, d_oi, d_iv_pp)
            if p is None and c.open_interest >= MIN_DOI:
                note = "【昨日无此行】" + note
            on_wall = ""
            if call_wall is not None and abs(c.strike - call_wall) < 1e-6:
                on_wall = "call墙"
            elif put_wall is not None and abs(c.strike - put_wall) < 1e-6:
                on_wall = "put墙"
            changes.append(FlowChange(
                expiry=c.expiry, strike=c.strike, kind=c.kind,
                prev_oi=prev_oi, curr_oi=c.open_interest, d_oi=d_oi,
                prev_iv=prev_iv, curr_iv=c.iv, d_iv_pp=d_iv_pp,
                curr_volume=c.volume,
                moneyness=(c.strike / spot - 1.0) if spot else 0.0,
                bias=bias, on_wall=on_wall, note=note,
            ))
            # 净倾向只累计【增建】侧（方向无歧义）
            if d_oi > 0:
                if c.kind == "C":
                    net_call += d_oi
                else:
                    net_put += d_oi
        # 按 |ΔOI| 降序（最大异动排前），近月本就已过滤
        changes.sort(key=lambda x: -abs(x.d_oi))
        changes = changes[:TOP_N]

        # 净倾向：增建的 put vs call 谁占优
        if net_put or net_call:
            if net_put > net_call * 1.3:
                tilt = f"偏空（近月新增看跌押注 {net_put:,} > 看涨 {net_call:,}）"
            elif net_call > net_put * 1.3:
                tilt = f"偏多（近月新增看涨押注 {net_call:,} > 看跌 {net_put:,}）"
            else:
                tilt = f"分歧（新增看涨 {net_call:,} ≈ 看跌 {net_put:,}）"

    return FlowAnalysis(
        instrument=curr.instrument,
        proxy_symbol=curr.proxy_symbol,
        spot=spot,
        horizon_days=horizon_days,
        curr_date=curr_date or "",
        curr_asof=curr.asof,
        prev_date=prev_date,
        unusual=unusual,
        total_call_volume=tcv,
        total_put_volume=tpv,
        changes=changes,
        net_call_doi=net_call,
        net_put_doi=net_put,
        flow_tilt=tilt,
        call_wall=call_wall,
        put_wall=put_wall,
    )
