"""期权 Gamma / OI 持仓结构分析（确定性计算）。

产出（对应文章里"关键位点/吸附点"那部分，但量化）:
  - OI 墙：看涨墙(阻力/磁吸) / 看跌墙(支撑/磁吸) —— 纯 OI，无模型假设，最稳健。
  - Put/Call OI 比 —— 情绪/偏度。
  - 做市商 GEX（伽马敞口）正负 —— 在【明确标注的假设】下判断"抑波动/易钉"还是"放大波动"。
  - 零伽马翻转位 —— 用 BS 重定价扫描求得，价格越过它，做市商对冲方向反转。

立场提醒（已在报告中标注）:
  * 这是 ETF 期权【代理】，不是 COMEX 原表；位点以 ETF 计，乘数换算商品仅近似。
  * GEX 的正负依赖"做市商净多 call、净空 put"这一【行业惯用但不确定】的假设。
  * OI 墙不需要该假设，是最可靠的部分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from undertow.core.models import OptionContract, OptionsSnapshot
from undertow.analyze import blackscholes as bs

CONTRACT_MULTIPLIER = 100  # 美式 ETF 期权每张 100 股
DEFAULT_HORIZON_DAYS = 45   # 主分析聚焦近月（近月主导 gamma）
DISPLAY_BAND = 0.10         # 展示 ±10% 现价附近的行权价
WALL_BAND = 0.15            # OI 墙只在现价 ±15% 内找（远 OTM 的累积 OI 非吸附点）


@dataclass(frozen=True)
class StrikeRow:
    strike: float
    call_oi: int
    put_oi: int
    net_gex: float  # 该行权价的做市商净 GEX（相对单位）


@dataclass(frozen=True)
class GammaAnalysis:
    instrument: str
    proxy_symbol: str
    spot: float
    asof: str
    horizon_days: int
    multiplier: float | None  # ETF×乘数≈商品价；None=无稳定映射
    proxy_quality: str

    total_call_oi: int
    total_put_oi: int
    put_call_ratio: float

    net_gex: float
    gex_regime: str
    zero_gamma: float | None  # ETF 价

    call_wall: float
    call_wall_oi: int
    put_wall: float
    put_wall_oi: int

    nearest_expiry: date | None
    nearest_call_wall: float | None
    nearest_put_wall: float | None

    strike_rows: list[StrikeRow] = field(default_factory=list)

    def to_commodity(self, etf_price: float | None) -> float | None:
        if etf_price is None or self.multiplier is None:
            return None
        return etf_price * self.multiplier


def _yearfrac(expiry: date, today: date) -> float:
    return (expiry - today).days / 365.0


def _dealer_gamma_at(contracts, S: float, today: date) -> float:
    """给定假设现价 S，做市商净伽马（含 S^2 缩放）。

    符号惯例：做市商净多 call 伽马(+)、净空 put 伽马(-)。
    """
    total = 0.0
    for c, T in contracts:
        g = bs.gamma(S, c.strike, T, c.iv)
        if g == 0.0:
            continue
        sign = 1.0 if c.is_call else -1.0
        total += sign * g * c.open_interest * CONTRACT_MULTIPLIER * S * S * 0.01
    return total


def _find_zero_gamma(contracts, spot: float, today: date) -> float | None:
    """扫描现价网格，找最接近现价的伽马翻转(零伽马)位。

    只接受【非零两侧符号翻转】的真实穿越：空链/无有效 IV/数值下溢形成的
    零平台不算根（否则空数据会返回 zero_gamma=spot 的假翻转位）；
    且整个扫描的 |GEX| 峰值必须显著大于零，否则判"无结构"返回 None。
    """
    if spot <= 0:
        return None
    lo, hi, steps = 0.70 * spot, 1.30 * spot, 240
    grid = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    gs = [_dealer_gamma_at(contracts, S, today) for S in grid]
    peak = max(abs(g) for g in gs)
    if peak <= 0:
        return None
    eps = peak * 1e-6           # 相对零阈：低于此视为"无信号平台"，不当根
    best = None
    best_dist = float("inf")
    for i in range(1, len(grid)):
        g0, g1 = gs[i - 1], gs[i]
        if abs(g0) <= eps or abs(g1) <= eps:
            continue            # 零平台/下溢段不算穿越
        if (g0 < 0) != (g1 < 0):
            cross = grid[i - 1] + (grid[i] - grid[i - 1]) * (0 - g0) / (g1 - g0)
            dist = abs(cross - spot)
            if dist < best_dist:
                best_dist, best = dist, cross
    return best


def analyze_gamma(
    snap: OptionsSnapshot,
    *,
    multiplier: float | None,
    proxy_quality: str,
    today: date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> GammaAnalysis:
    today = today or date.today()
    spot = snap.spot

    # 近月、未到期、有 OI 的合约（带年化到期时间），近月主导 gamma
    live: list[tuple[OptionContract, float]] = []
    for c in snap.with_oi():
        T = _yearfrac(c.expiry, today)
        if 0 < T <= horizon_days / 365.0 and c.iv > 0:
            live.append((c, T))

    # 退化情形：近月没数据则放宽到全部未到期
    if not live:
        for c in snap.with_oi():
            T = _yearfrac(c.expiry, today)
            if T > 0 and c.iv > 0:
                live.append((c, T))

    # 按行权价聚合 OI 与净 GEX（当前现价下）
    by_strike: dict[float, list[int]] = {}  # strike -> [call_oi, put_oi]
    gex_by_strike: dict[float, float] = {}
    total_call_oi = total_put_oi = 0
    for c, T in live:
        slot = by_strike.setdefault(c.strike, [0, 0])
        g = bs.gamma(spot, c.strike, T, c.iv)
        gex = g * c.open_interest * CONTRACT_MULTIPLIER * spot * spot * 0.01
        if c.is_call:
            slot[0] += c.open_interest
            total_call_oi += c.open_interest
            gex_by_strike[c.strike] = gex_by_strike.get(c.strike, 0.0) + gex
        else:
            slot[1] += c.open_interest
            total_put_oi += c.open_interest
            gex_by_strike[c.strike] = gex_by_strike.get(c.strike, 0.0) - gex

    # OI 墙：看涨墙取现价上方最大 call OI；看跌墙取现价下方最大 put OI
    # 仅在现价 ±WALL_BAND 内找——远 OTM 的累积 OI 不构成有意义的吸附/pin 位
    wall_hi = spot * (1 + WALL_BAND)
    wall_lo = spot * (1 - WALL_BAND)
    call_strikes = [(s, v[0]) for s, v in by_strike.items() if spot <= s <= wall_hi and v[0] > 0]
    put_strikes = [(s, v[1]) for s, v in by_strike.items() if wall_lo <= s <= spot and v[1] > 0]
    call_wall, call_wall_oi = max(call_strikes, key=lambda x: x[1]) if call_strikes else (spot, 0)
    put_wall, put_wall_oi = max(put_strikes, key=lambda x: x[1]) if put_strikes else (spot, 0)

    net_gex = sum(gex_by_strike.values())
    if net_gex > 0:
        regime = "正Gamma：做市商净多伽马 → 倾向抑制波动、价格易被钉在高伽马区"
    elif net_gex < 0:
        regime = "负Gamma：做市商净空伽马 → 对冲放大波动、易追涨杀跌"
    else:
        regime = "中性"

    zero_gamma = _find_zero_gamma(live, spot, today)

    # 最近一个有量到期的 pin 位
    expiries = snap.expiries()
    nearest_expiry = next((e for e in expiries if _yearfrac(e, today) > 0), None)
    nce = npe = None
    if nearest_expiry is not None:
        ne_calls: dict[float, int] = {}
        ne_puts: dict[float, int] = {}
        for c in snap.with_oi():
            # pin 位同样只看近价带内
            if c.expiry == nearest_expiry and wall_lo <= c.strike <= wall_hi:
                (ne_calls if c.is_call else ne_puts)[c.strike] = (
                    (ne_calls if c.is_call else ne_puts).get(c.strike, 0) + c.open_interest
                )
        if ne_calls:
            nce = max(ne_calls, key=ne_calls.get)
        if ne_puts:
            npe = max(ne_puts, key=ne_puts.get)

    # 展示窗口：现价 ±DISPLAY_BAND 的行权价
    lo, hi = spot * (1 - DISPLAY_BAND), spot * (1 + DISPLAY_BAND)
    rows = []
    for s in sorted(by_strike):
        if lo <= s <= hi:
            co, po = by_strike[s]
            rows.append(StrikeRow(strike=s, call_oi=co, put_oi=po,
                                  net_gex=gex_by_strike.get(s, 0.0)))

    return GammaAnalysis(
        instrument=snap.instrument,
        proxy_symbol=snap.proxy_symbol,
        spot=spot,
        asof=snap.asof,
        horizon_days=horizon_days,
        multiplier=multiplier,
        proxy_quality=proxy_quality,
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        put_call_ratio=(total_put_oi / total_call_oi) if total_call_oi else float("nan"),
        net_gex=net_gex,
        gex_regime=regime,
        zero_gamma=zero_gamma,
        call_wall=call_wall,
        call_wall_oi=call_wall_oi,
        put_wall=put_wall,
        put_wall_oi=put_wall_oi,
        nearest_expiry=nearest_expiry,
        nearest_call_wall=nce,
        nearest_put_wall=npe,
        strike_rows=rows,
    )


def structure_delta(prev: "GammaAnalysis", curr: "GammaAnalysis",
                    prev_surviving: dict | None = None) -> list[str]:
    """昨日→今日的结构变化短句（速读用，确定性拼句）。

    位移按 ETF 行权价内在口径判断（免换算比值的时点噪音），展示用今日商品口径；
    墙的强弱 = 同一行权价的 OI 对昨变化（墙没动看厚薄，动了报迁移）。

    prev_surviving：昨日快照中【以今日窗口衡量仍存活】的 (行权价, C/P)→OI——
    墙厚对比必须用它作基准，否则"今日到期合约滚出窗口"会伪装成墙被削弱
    （R8b 到期滚落污染；零伽马对比不受此扰，仍用各自日期锚定的完整结构）。
    """
    conv = curr.to_commodity
    c_spot = conv(curr.spot) or curr.spot
    fmt = (lambda v: f"{conv(v):,.0f}") if c_spot >= 500 else (lambda v: f"{conv(v):,.1f}")
    out: list[str] = []

    # 零伽马位移（相对现价的贴近/远离一并说明）
    if prev.zero_gamma is not None and curr.zero_gamma is not None:
        d_pct = 100.0 * (curr.zero_gamma - prev.zero_gamma) / prev.zero_gamma
        if abs(d_pct) < 0.15:
            out.append(f"零伽马 {fmt(curr.zero_gamma)} 基本未动")
        else:
            word = "下移" if d_pct < 0 else "上移"
            closer = (abs(curr.zero_gamma - curr.spot) < abs(prev.zero_gamma - curr.spot))
            rel = "向现价贴近，收复/跌破它的门槛变近" if closer else "远离现价"
            out.append(f"零伽马 {fmt(prev.zero_gamma)}→{fmt(curr.zero_gamma)}"
                       f"（{word} {abs(d_pct):.1f}%，{rel}）")

    prev_by_strike = {r.strike: r for r in prev.strike_rows}

    def wall(kind: str, p_w: float, p_oi: int, c_w: float, c_oi: int) -> None:
        name = "call 墙" if kind == "C" else "put 墙"
        role = "压制" if kind == "C" else "承接"
        if abs(p_w - c_w) < 1e-9:
            if prev_surviving is not None:
                base = prev_surviving.get((c_w, kind), 0)
            else:
                r = prev_by_strike.get(c_w)
                base = (r.call_oi if kind == "C" else r.put_oi) if r else p_oi
            d = c_oi - base
            if abs(d) < max(200, base * 0.01):
                out.append(f"{name} {fmt(c_w)} 未移，厚度基本不变（OI {c_oi:,}）")
            else:
                word = "增厚" if d > 0 else "削弱"
                out.append(f"{name} {fmt(c_w)} 未移但{word}（OI {d:+,} 手，{role}"
                           f"{'更结实' if d > 0 else '在松动'}）")
        else:
            word = "上移" if c_w > p_w else "下移"
            out.append(f"{name} {fmt(p_w)}→{fmt(c_w)}（{word}，新墙 OI {c_oi:,}）")

    if prev.call_wall_oi > 0 and curr.call_wall_oi > 0:
        wall("C", prev.call_wall, prev.call_wall_oi, curr.call_wall, curr.call_wall_oi)
    if prev.put_wall_oi > 0 and curr.put_wall_oi > 0:
        wall("P", prev.put_wall, prev.put_wall_oi, curr.put_wall, curr.put_wall_oi)
    return out
