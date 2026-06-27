"""期权资金流 / 持仓异动分析（确定性计算，无 I/O）。

动机（来自文章作者对 WTI 6/22 的整理）:
  作者抓的不是「静态的墙」(那是 gamma.py 的活)，而是【墙的增量 + 买卖方性质】：
  逐行权价看 ΔOI、当前OI、精确Delta、以及【Delta 修正后的相对 IV 变化】，
  据此判断每个行权价是【买方建仓】还是【卖方建仓/撤退】：
    · OI 增 + IV 升 = 买方在抬价买入（看跌买保护 / 看涨买突破）
    · OI 增 + IV 降 = 卖方在写权收钱（写 put 做支撑 / 写 call 做压制）
  作者据此判断 WTI「80 上方极强卖方压制、65 下方大量买方保护、put 越来越贵→下行风险更大」，
  结果 6/24 原油如其所料走弱。

  关键洞见：延迟数据没有逐笔成交，但【IV 变化的方向】可作买/卖方的代理——
  买方抬价→IV 升，卖方供给→IV 降。这就是不用 tick 数据也能分买卖方的窍门。

本模块两件事:
  1) scan_unusual(snap)        —— 单张快照即可：按 volume/OI 找"今日异常活跃"。
  2) analyze_flow(prev, curr)  —— 两日快照 diff：逐 (到期,行权价,C/P) 求 ΔOI / ΔIV，
     做【Delta 修正】(剔除现价移动沿偏斜的机械 IV 变化)，再按 OI 增减 × 修正IV 方向
     判定买方/卖方，复刻作者那张表。需 ≥2 天落盘快照（CBOE 无期权历史，自攒）。

诚实标注:
  * 「Delta 修正后相对 IV 变化」是对作者方法的【原理化近似】(剔除 skew×Δspot 的机械项)，
    不是其精确公式；买卖方判定在边界行可能与人工的酌情判断不同。
  * 仍是 ETF 代理（USO≠WTI，行权价/IV 仅定性）、样本短，只作预警不作预言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models import OptionsSnapshot, OptionContract

DEFAULT_HORIZON_DAYS = 60      # 只看近月
NEAR_MONEY_BAND = 0.15        # 只看现价 ±15% 内的行权价
MIN_DOI = 50                  # |ΔOI| 低于此视为噪音（异动表）
TOP_N = 15                    # 单快照异动榜上限
TABLE_N = 14                  # 买卖方表 put/call 各自上限
UNUSUAL_MIN_VOLUME = 50
UNUSUAL_MIN_VOL_OI = 0.5
# —— 买卖方判定阈值（pp，Delta 修正后）——
IV_NOISE = 0.08              # |修正IV| 低于此 = 噪音
IV_MILD = 0.28              # 区分"轻微" vs 正常强度
IV_STRONG = 1.0            # call 卖方"极强压制"门槛
MAX_ABS_DELTA = 0.90       # |delta| 超过此=深 ITM，IV 不可靠，剔除
REL_MIN_STRIKES = 8        # 行权价数 ≥ 此才做"相对化"(减中位 ΔIV，剔全局 vol 平移)


@dataclass(frozen=True)
class UnusualContract:
    expiry: date
    strike: float
    kind: str
    open_interest: int
    volume: int
    iv: float
    delta: float
    vol_oi_ratio: float
    moneyness: float
    note: str


@dataclass(frozen=True)
class FlowChange:
    """两日 diff 里一个 (到期,行权价,C/P) 的持仓异动 + 买卖方判定。"""
    expiry: date
    strike: float
    kind: str
    prev_oi: int
    curr_oi: int
    d_oi: int
    delta: float           # 精确 Delta（CBOE 给）
    prev_iv: float
    curr_iv: float
    d_iv_pp: float         # 原始 ΔIV ×100 (pp)
    adj_iv_pp: float       # Delta 修正后的相对 ΔIV (pp)
    curr_volume: int
    moneyness: float
    bias: str              # bearish / bullish / neutral（粗方向，供聚合）
    judgment: str          # 买方保护 / 卖方做支撑 / 卖方压制 / 卖方撤退 …（细判，作者口径）
    on_wall: str           # put墙 / call墙 / ""
    note: str


@dataclass(frozen=True)
class FlowAnalysis:
    instrument: str
    proxy_symbol: str
    spot: float
    horizon_days: int
    curr_date: str
    curr_asof: str
    prev_date: str | None
    # 单快照异动
    unusual: list[UnusualContract] = field(default_factory=list)
    total_call_volume: int = 0
    total_put_volume: int = 0
    # 两日 diff
    changes: list[FlowChange] = field(default_factory=list)
    net_call_doi: int = 0          # 近月增建 call OI 净增（kind 求和，向后兼容）
    net_put_doi: int = 0           # 近月增建 put OI 净增
    downside_pressure: float = 0.0  # 买卖方判定加权的下行压力
    upside_pressure: float = 0.0
    flow_tilt: str = "—"
    call_wall: float | None = None
    put_wall: float | None = None


def _yearfrac(expiry: date, today: date) -> float:
    return (expiry - today).days / 365.0


def _live(snap: OptionsSnapshot, today: date, horizon_days: int) -> list[OptionContract]:
    if snap.spot <= 0:
        return []
    lo, hi = snap.spot * (1 - NEAR_MONEY_BAND), snap.spot * (1 + NEAR_MONEY_BAND)
    out = []
    for c in snap.contracts:
        T = _yearfrac(c.expiry, today)
        if 0 < T <= horizon_days / 365.0 and lo <= c.strike <= hi:
            out.append(c)
    return out


def _lin_slope(pts: list[tuple[float, float]]) -> float:
    """最小二乘斜率 dY/dX（用于估期权偏斜 ∂IV/∂K）。"""
    n = len(pts)
    if n < 2:
        return 0.0
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den <= 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in pts)
    return num / den


def scan_unusual(snap: OptionsSnapshot, *, today: date,
                 horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[UnusualContract]:
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
            open_interest=c.open_interest, volume=c.volume, iv=c.iv, delta=c.delta,
            vol_oi_ratio=ratio, moneyness=(c.strike / spot - 1.0) if spot else 0.0,
            note=f"{kind_cn}·{fresh}",
        ))
    out.sort(key=lambda u: (u.expiry, -u.volume))
    return out[:TOP_N]


def _judge(kind: str, d_oi: int, adj_pp: float, prev_known: bool) -> tuple[str, str, float]:
    """按 持仓C/P × OI增减 × Delta修正IV方向 判定买卖方。
    返回 (粗方向 bearish/bullish/neutral, 细判中文, 聚合权重系数 0~1)。

    复刻作者口径：IV 升=买方抬价、IV 降=卖方供给；OI 增=建仓、OI 减=平仓/撤退。
    """
    if not prev_known:  # 无昨日 IV：只能按 OI 方向定性
        if kind == "P":
            return ("bearish", "买方保护(新建)", 1.0) if d_oi > 0 else ("neutral", "看跌减仓", 0.0)
        return ("bullish", "买方(新建)", 1.0) if d_oi > 0 else ("neutral", "看涨减仓", 0.0)

    a = adj_pp
    if abs(a) < IV_NOISE:
        return "neutral", "噪音", 0.0
    up_oi = d_oi > 0
    strong = abs(a) >= IV_STRONG
    mild = abs(a) < IV_MILD
    if kind == "P":
        if a > 0:   # 买方抬 IV → 下方保护买盘（看跌）
            if up_oi:
                return "bearish", ("买方轻微保护" if mild else "买方保护"), (0.6 if mild else 1.0)
            return "bearish", "卖方撤退", 0.5          # OI 降 + IV 升：支撑卖方退场，偏空
        else:       # 卖方压 IV → 写 put 做支撑（看多）
            if up_oi:
                return "bullish", "卖方做支撑", 1.0
            return "bullish", "买方了结", 0.5
    else:  # CALL
        if a < 0:   # 卖方压 IV → 上方压制（看空）
            if up_oi:
                lvl = "极强卖方压制" if strong else ("轻微卖方压制" if mild else "卖方压制")
                return "bearish", lvl, (1.3 if strong else (0.6 if mild else 1.0))
            return "bearish", "买方了结", 0.5
        else:       # 买方抬 IV → 上方突破买盘（看多）
            if up_oi:
                return "bullish", ("轻微买方" if mild else "买方"), (0.6 if mild else 1.0)
            return "bullish", "卖方撤退", 0.5


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
    live = _live(curr, today, horizon_days)

    unusual = scan_unusual(curr, today=today, horizon_days=horizon_days)
    tcv = sum(c.volume for c in live if c.kind == "C")
    tpv = sum(c.volume for c in live if c.kind == "P")

    changes: list[FlowChange] = []
    net_call = net_put = 0
    downside = upside = 0.0
    tilt = "—（仅一份快照，明天起可出 ΔOI/ΔIV 与买卖方判定）"

    if prev is not None:
        d_spot = spot - prev.spot
        prev_live = _live(prev, today, horizon_days)
        # 当前链的偏斜 ∂IV/∂K（put / call 各一条），用于 Delta 修正
        put_slope = _lin_slope([(c.strike, c.iv) for c in live if c.kind == "P" and c.iv > 0])
        call_slope = _lin_slope([(c.strike, c.iv) for c in live if c.kind == "C" and c.iv > 0])

        def _agg(contracts):
            """按 (行权价,C/P) 合并多个到期：OI 求和，IV/Delta 按 OI 加权。"""
            m: dict[tuple, dict] = {}
            for c in contracts:
                k = (c.strike, c.kind)
                a = m.setdefault(k, {"oi": 0, "ivw": 0.0, "dlw": 0.0, "vol": 0, "exp": c.expiry})
                a["oi"] += c.open_interest
                if c.iv > 0:
                    a["ivw"] += c.iv * c.open_interest
                a["dlw"] += c.delta * c.open_interest
                a["vol"] += c.volume
                if c.expiry < a["exp"]:
                    a["exp"] = c.expiry
            return m

        cagg, pagg = _agg(live), _agg(prev_live)
        # 第一遍：每个行权价的 Delta 修正 ΔIV（尚未相对化）
        rows = []
        for (strike, kind), a in cagg.items():
            coi = a["oi"]
            if coi <= 0:
                continue
            cdelta = a["dlw"] / coi
            if abs(cdelta) > MAX_ABS_DELTA:   # 深 ITM，IV 不可靠
                continue
            civ = a["ivw"] / coi if a["ivw"] > 0 else 0.0
            p = pagg.get((strike, kind))
            poi = p["oi"] if p else 0
            piv = (p["ivw"] / p["oi"]) if (p and p["oi"] > 0 and p["ivw"] > 0) else 0.0
            d_oi = coi - poi
            if abs(d_oi) < MIN_DOI:
                continue
            prev_known = bool(p and piv > 0 and civ > 0)
            d_iv = (civ - piv) if prev_known else 0.0
            slope = put_slope if kind == "P" else call_slope
            corrected = (d_iv - slope * d_spot) if prev_known else 0.0
            rows.append({"strike": strike, "kind": kind, "coi": coi, "poi": poi, "d_oi": d_oi,
                         "delta": cdelta, "civ": civ, "piv": piv, "d_iv": d_iv,
                         "corrected": corrected, "known": prev_known, "vol": a["vol"], "exp": a["exp"]})

        # "相对化"基准：行权价足够多时减去中位修正 ΔIV，剔除全市场 vol 平移；少则不减
        cv = sorted(r["corrected"] for r in rows if r["known"])
        ref = cv[len(cv) // 2] if len(cv) >= REL_MIN_STRIKES else 0.0

        for r in rows:
            adj_iv_pp = ((r["corrected"] - ref) * 100.0) if r["known"] else 0.0
            d_iv_pp = r["d_iv"] * 100.0
            bias, judgment, w = _judge(r["kind"], r["d_oi"], adj_iv_pp, r["known"])
            on_wall = ""
            if call_wall is not None and abs(r["strike"] - call_wall) < 1e-6:
                on_wall = "call墙"
            elif put_wall is not None and abs(r["strike"] - put_wall) < 1e-6:
                on_wall = "put墙"
            note = judgment if r["poi"] > 0 else "【昨日无此行】" + judgment
            changes.append(FlowChange(
                expiry=r["exp"], strike=r["strike"], kind=r["kind"],
                prev_oi=r["poi"], curr_oi=r["coi"], d_oi=r["d_oi"], delta=r["delta"],
                prev_iv=r["piv"], curr_iv=r["civ"], d_iv_pp=d_iv_pp, adj_iv_pp=adj_iv_pp,
                curr_volume=r["vol"], moneyness=(r["strike"] / spot - 1.0) if spot else 0.0,
                bias=bias, judgment=judgment, on_wall=on_wall, note=note,
            ))
            if r["d_oi"] > 0:  # kind 求和（向后兼容字段）
                if r["kind"] == "C":
                    net_call += r["d_oi"]
                else:
                    net_put += r["d_oi"]
            mag = abs(r["d_oi"]) * w
            if bias == "bearish":
                downside += mag
            elif bias == "bullish":
                upside += mag

        changes.sort(key=lambda x: -abs(x.d_oi))

        if downside or upside:
            if downside > upside * 1.3:
                tilt = f"偏空（下行压力 {downside:,.0f} > 上行 {upside:,.0f}；put 买保护/call 卖压制占优）"
            elif upside > downside * 1.3:
                tilt = f"偏多（上行 {upside:,.0f} > 下行 {downside:,.0f}；call 买盘/put 卖方做支撑占优）"
            else:
                tilt = f"分歧（下行 {downside:,.0f} ≈ 上行 {upside:,.0f}）"

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
        downside_pressure=round(downside, 1),
        upside_pressure=round(upside, 1),
        flow_tilt=tilt,
        call_wall=call_wall,
        put_wall=put_wall,
    )
