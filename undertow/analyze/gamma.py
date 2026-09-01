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
# ⚠️ 2026-09-01 codex P0-5：band 方式有系统性缺陷 —— 带内【总能】找到一个最大值，
#    于是较小的局部档位也会被叫作"墙"。实测 SLV 现价 58.5 时 band=5% 选出
#    55(102,820)，而真正的 50 有 168,526 张、在 ±15% 之外够不着。
#    正确拆分见本文件末尾的 structural_walls()（全范围 + 近端到期占比门槛）
#    与 local_pin()（近价带内最大，明确不是墙）。
#    ⬜ 待迁移：analyze_gamma / persistent_walls / layered_walls 仍用 band 方式，
#       它们的输出进入方向投票、关键位表与策略目标。迁移会改变全报告口径，
#       需先确认新口径在历史上的表现，故分两步走。
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

    # 带内 call/put 墙的前若干名 (行权价, OI)，按 OI 降序；[0] 即 call_wall/put_wall。
    # 单一最大值 + ±15% 硬边界会让贴边界的大墙"闪进闪出"标题（见 SLV 60），故并列展示前三。
    call_walls_top: list[tuple[float, int]] = field(default_factory=list)
    put_walls_top: list[tuple[float, int]] = field(default_factory=list)

    strike_rows: list[StrikeRow] = field(default_factory=list)

    # 到期分层墙位（见 layered_walls 的注释）。call_wall/put_wall 主字段取【近端】，
    # 因为方向投票问的是"这周会不会破位"，跨月加总的墙答不了这个问题。
    # blended_* 保留 45 天混算值，仅供与历史报告对账，不参与任何判断。
    layers: dict = field(default_factory=dict)
    wall_basis: str = ""
    blended_call_wall: float = 0.0
    blended_call_wall_oi: int = 0
    blended_put_wall: float = 0.0
    blended_put_wall_oi: int = 0

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

    # ⚠️ 两个集合必须分开（codex review 2026-08-27）：
    #   oi_pool  —— 只要求「近月 + 未到期 + 有 OI」，**不看 IV**。
    #               OI 墙是纯持仓量统计、不含任何模型假设，把 IV 缺失的合约剔掉
    #               会让真实存在的墙凭空消失，与模块声称的「OI 墙最可靠」自相矛盾。
    #   live     —— GEX 计算需要 IV（要算 BS gamma），才额外要求 iv > 0。
    # 旧写法两者共用一个 iv>0 的集合，导致 OI 墙 / put-call 比 / 总 OI 全被 IV 过滤；
    # 且近月 IV 全缺时会放宽到全部远月，所谓「45 日墙」实际来自远月。
    win = horizon_days / 365.0
    oi_pool: list[tuple[OptionContract, float]] = []
    live: list[tuple[OptionContract, float]] = []
    for c in snap.with_oi():
        T = _yearfrac(c.expiry, today)
        if not (0 < T <= win):
            continue
        oi_pool.append((c, T))
        if c.iv > 0:
            live.append((c, T))

    # 退化情形分别处理：OI 池放宽到全部未到期；GEX 池另行放宽（并保留 iv 要求）
    if not oi_pool:
        oi_pool = [(c, _yearfrac(c.expiry, today)) for c in snap.with_oi()
                   if _yearfrac(c.expiry, today) > 0]
    if not live:
        live = [(c, T) for c, T in oi_pool if c.iv > 0]

    # ── OI 聚合：走 oi_pool，纯持仓量，不含任何模型假设 ──
    by_strike: dict[float, list[int]] = {}  # strike -> [call_oi, put_oi]
    total_call_oi = total_put_oi = 0
    for c, _T in oi_pool:
        slot = by_strike.setdefault(c.strike, [0, 0])
        if c.is_call:
            slot[0] += c.open_interest
            total_call_oi += c.open_interest
        else:
            slot[1] += c.open_interest
            total_put_oi += c.open_interest

    # ── GEX 聚合：走 live（需 IV 算 BS gamma），与 OI 池分开 ──
    gex_by_strike: dict[float, float] = {}
    for c, T in live:
        g = bs.gamma(spot, c.strike, T, c.iv)
        gex = g * c.open_interest * CONTRACT_MULTIPLIER * spot * spot * 0.01
        gex_by_strike[c.strike] = gex_by_strike.get(c.strike, 0.0) + (gex if c.is_call else -gex)

    # OI 墙：看涨墙取现价上方最大 call OI；看跌墙取现价下方最大 put OI
    # 仅在现价 ±WALL_BAND 内找——远 OTM 的累积 OI 不构成有意义的吸附/pin 位
    wall_hi = spot * (1 + WALL_BAND)
    wall_lo = spot * (1 - WALL_BAND)
    call_strikes = [(s, v[0]) for s, v in by_strike.items() if spot <= s <= wall_hi and v[0] > 0]
    put_strikes = [(s, v[1]) for s, v in by_strike.items() if wall_lo <= s <= spot and v[1] > 0]
    bl_call_wall, bl_call_oi = max(call_strikes, key=lambda x: x[1]) if call_strikes else (spot, 0)
    bl_put_wall, bl_put_oi = max(put_strikes, key=lambda x: x[1]) if put_strikes else (spot, 0)

    # ── 主墙位取【近端 ≤14天】，逐层退化 ──
    # 混算口径（bl_*）把明天到期和 45 天后到期等权加总，会造出实盘不存在的墙：
    # 2026-08-31 SLV 混算说支撑 55(-8.9%)，近端实际 60(-0.7%)。方向投票用它会读反。
    layers = layered_walls(snap, today, spot)
    _order = [layers["near"], layers["mid"]]

    def _pick(side: str):
        for L in _order:
            oi = L.call_wall_oi if side == "call" else L.put_wall_oi
            if oi > 0:
                w = L.call_wall if side == "call" else L.put_wall
                top = L.call_walls_top if side == "call" else L.put_walls_top
                return w, oi, top, L.label
        bw = bl_call_wall if side == "call" else bl_put_wall
        bo = bl_call_oi if side == "call" else bl_put_oi
        bt = (sorted(call_strikes, key=lambda x: -x[1])[:3] if side == "call"
              else sorted(put_strikes, key=lambda x: -x[1])[:3])
        return bw, bo, bt, "近/中端均无仓·退 45 天混算"

    call_wall, call_wall_oi, call_walls_top, _cb = _pick("call")
    put_wall, put_wall_oi, put_walls_top, _pb = _pick("put")
    wall_basis = _cb if _cb == _pb else f"call:{_cb}｜put:{_pb}"

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
        layers=layers,
        wall_basis=wall_basis,
        blended_call_wall=bl_call_wall,
        blended_call_wall_oi=bl_call_oi,
        blended_put_wall=bl_put_wall,
        blended_put_wall_oi=bl_put_oi,
        call_wall=call_wall,
        call_wall_oi=call_wall_oi,
        put_wall=put_wall,
        put_wall_oi=put_wall_oi,
        nearest_expiry=nearest_expiry,
        nearest_call_wall=nce,
        nearest_put_wall=npe,
        call_walls_top=call_walls_top,
        put_walls_top=put_walls_top,
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
    # ETF 行权价锚：仅当确有换算（商品价≠ETF价）时补，让读者一眼分辨"墙真动"vs"比值漂移"
    scaled = conv(curr.spot) is not None and abs((conv(curr.spot) or curr.spot) - curr.spot) > 1e-6
    def etf(v: float, dp: int = 0) -> str:
        return f"（ETF {v:.{dp}f}）" if scaled else ""

    def swall(v: float) -> str:
        """墙的写法：【先报期权行权价】，换算价放括号。

        墙就是一个行权价，是你在交易软件里能直接搜到的东西。旧写法
        「call 墙 30,059（ETF 730）」把换算后的期货点位摆在前面，
        用户得先在脑子里除以 41 才知道说的是 730C（用户 2026-08-28 反馈）。
        """
        if not scaled:
            return f"{v:g}"
        return f"{v:g}（≈{conv(v):,.0f}）"
    out: list[str] = []

    # 零伽马位移（相对现价的贴近/远离一并说明）
    if prev.zero_gamma is not None and curr.zero_gamma is not None:
        d_pct = 100.0 * (curr.zero_gamma - prev.zero_gamma) / prev.zero_gamma
        if abs(d_pct) < 0.15:
            out.append(f"零伽马 {fmt(curr.zero_gamma)}{etf(curr.zero_gamma, 1)} 基本未动")
        else:
            word = "下移" if d_pct < 0 else "上移"
            closer = (abs(curr.zero_gamma - curr.spot) < abs(prev.zero_gamma - curr.spot))
            rel = "向现价贴近，收复/跌破它的门槛变近" if closer else "远离现价"
            out.append(f"零伽马 {fmt(prev.zero_gamma)}→{fmt(curr.zero_gamma)}{etf(curr.zero_gamma, 1)}"
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
                out.append(f"{name} {swall(c_w)} 未移，厚度基本不变（OI {c_oi:,}）")
            else:
                word = "增厚" if d > 0 else "削弱"
                out.append(f"{name} {swall(c_w)} 未移但{word}（OI {d:+,} 手，{role}"
                           f"{'更结实' if d > 0 else '在松动'}）")
        else:
            word = "上移" if c_w > p_w else "下移"
            # 旧版这里还要再补一句 "ETF 700→730，"——swall 已经先报行权价，重复了
            out.append(f"{name} {swall(p_w)}→{swall(c_w)}（{word}，新墙 OI {c_oi:,}）")

    if prev.call_wall_oi > 0 and curr.call_wall_oi > 0:
        wall("C", prev.call_wall, prev.call_wall_oi, curr.call_wall, curr.call_wall_oi)
    if prev.put_wall_oi > 0 and curr.put_wall_oi > 0:
        wall("P", prev.put_wall, prev.put_wall_oi, curr.put_wall, curr.put_wall_oi)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 持续墙位 —— 排除临近到期后剩下的支撑/阻力（2026-08-29，用户追问引出）
# ═══════════════════════════════════════════════════════════════════════════
# ⚠️ 7 天是【未校准的展示门槛】，不是校准出来的分界（codex 2026-08-29 P1-7）。
# 实现是 min_dte <= d，即【含】第 7 天；文档不得再写"排除 ≤7 天"（口径矛盾）。
PERSIST_MIN_DTE = 7      # 剩余到期 >=7 天才计入；更短的下周就不在了


def persistent_walls(snap, today, *, min_dte: int = PERSIST_MIN_DTE,
                     max_dte: int = 60, band: float = 0.15) -> dict:
    """排除临近到期后的 put/call 墙。

    ⚠️ 不要读成「跌到哪一定有人接」（codex 2026-08-29 P1-7）：
    这里只是"剩余到期 >=7 天的合约里，哪个行权价堆的 OI 最多"。
    OI 大不等于必然承接 —— 大 OI 往往只是因为那是整数关口、期权链上挂得早。
    要证明支撑作用，得比「触及它之后的反弹率」vs「触及随机价位的反弹率」，
    这个我们还没做。

    **为什么必须分开报**（用户 2026-08-29：「他给了位置，我们能给吗？」）：
    2026-08-28 我们报的 put 墙是 GLD 413（≈金价 4560），可它 42,388 张里有
    **40,394 张（95%）是当天到期的** —— 当天收盘就归零，它是当日的 pin，
    不是下周的承接区。
    排除 ≤7 天后重算，第一大 put 墙是 GLD 400（≈金价 4416，OI 59,563），
    那才是真正多到期分布的承接区。数据我们一直有，只是被 0DTE 盖住了。

    ⚠️ 两种墙都有用，别互相替代：
      · 当日 pin（<7天，含 0DTE）→ 只对临近几天有意义，到期即失效
      · 持续墙（>=7天）          → 做价差组合时看这个

    ⚠️⚠️ **本函数只增加一个展示字段，没有解决 0DTE 对判定的污染**
    （codex 2026-08-29 P1-7）：原来的 ga.put_wall / call_wall 仍然进入
    Gamma 层投票、关键位表、策略目标与盈亏比闸门。也就是说，
    一道当天就消失的 0DTE 墙，**仍然可以改变方向票和策略建议**，
    只是页面旁边多了一组"持续墙"供对照。
    彻底修法是把墙分成 intraday_pin / swing_wall 两个类型，
    让所有投票与策略显式声明用哪一个 —— 那是更大的改动，尚未做。
    """
    lo, hi = snap.spot * (1 - band), snap.spot * (1 + band)
    put_agg: dict = {}
    call_agg: dict = {}
    for c in snap.contracts:
        if not c.open_interest or not (lo <= c.strike <= hi):
            continue
        d = (c.expiry - today).days
        if not (min_dte <= d <= max_dte):
            continue
        tgt = put_agg if c.kind == "P" else call_agg
        tgt[c.strike] = tgt.get(c.strike, 0) + c.open_interest

    def _top(agg, n=3):
        return [{"strike": k, "oi": v} for k, v in
                sorted(agg.items(), key=lambda x: -x[1])[:n]]
    pw = _top(put_agg)
    cw = _top(call_agg)
    return {
        "min_dte": min_dte, "max_dte": max_dte,
        "put_wall": pw[0]["strike"] if pw else None,
        "put_wall_oi": pw[0]["oi"] if pw else 0,
        "call_wall": cw[0]["strike"] if cw else None,
        "call_wall_oi": cw[0]["oi"] if cw else 0,
        "put_top": pw, "call_top": cw,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 到期分层墙位
#
# 【为什么必须分层】analyze_gamma 的 by_strike 把 horizon_days 内所有到期的 OI
# 直接累加，一张明天到期的 OI 和一张 45 天后到期的 OI 权重完全相同。后果实测：
#   2026-08-31 GLD 盘前 407.53
#     混算(45天)  put 墙 400 (29,846)
#     近端 ≤14天  put 墙 405 ( 6,994)   ← 本周真正挡价格的
#     中端 15-45  put 墙 350 (27,358)
#   400 在任何单层里都不是最大值——它是三层加总后才冒出来的"假墙"。
#   同日 SLV 更严重：混算说支撑在 55(-8.9%)，近端实际在 60(-0.7%)，
#   照混算读会以为下方有 8.9% 缓冲，实际支撑就在脚下。
#
# 分层后还能读出一个混算永远给不出的信息：**跨层一致性**。
# 同日 GLD call 墙三层都是 430 → 真共识位；put 墙三层各不相同 → 加总产物。
# ─────────────────────────────────────────────────────────────────────────────

WALL_LAYERS: tuple[tuple[str, str, int, int], ...] = (
    ("near", "近端 ≤14天",   1,   14),    # 交易层：本周进出场、破位判断
    ("mid",  "中端 15-45天", 15,  45),    # 布局层：多数持仓所在
    ("far",  "远端 >45天",   46, 3650),   # 背景层：机构长期布局，不参与本周判断
)
WALL_SAME_TOL = 0.012   # 两层墙位相对差 ≤1.2% 视为同一位置（容忍行权价档距）


@dataclass(frozen=True)
class WallLayer:
    key: str
    label: str
    lo_dte: int
    hi_dte: int
    call_wall: float
    call_wall_oi: int
    put_wall: float
    put_wall_oi: int
    call_walls_top: list[tuple[float, int]]
    put_walls_top: list[tuple[float, int]]
    total_call_oi: int
    total_put_oi: int
    n_strikes: int

    @property
    def empty(self) -> bool:
        return self.call_wall_oi == 0 and self.put_wall_oi == 0


def _layer_walls(snap, today: date, spot: float, lo: int, hi: int,
                 key: str, label: str) -> WallLayer:
    """单层内独立求墙——不跨层加总，这是与 analyze_gamma 的唯一实质差别。"""
    by_strike: dict[float, list[int]] = {}
    tc = tp = 0
    wall_hi, wall_lo = spot * (1 + WALL_BAND), spot * (1 - WALL_BAND)
    for c in snap.with_oi():
        d = (c.expiry - today).days
        if not (lo <= d <= hi):
            continue
        slot = by_strike.setdefault(c.strike, [0, 0])
        if c.is_call:
            slot[0] += c.open_interest
            tc += c.open_interest
        else:
            slot[1] += c.open_interest
            tp += c.open_interest
    cs = [(s, v[0]) for s, v in by_strike.items() if spot <= s <= wall_hi and v[0] > 0]
    ps = [(s, v[1]) for s, v in by_strike.items() if wall_lo <= s <= spot and v[1] > 0]
    cw, cwo = max(cs, key=lambda x: x[1]) if cs else (spot, 0)
    pw, pwo = max(ps, key=lambda x: x[1]) if ps else (spot, 0)
    return WallLayer(
        key=key, label=label, lo_dte=lo, hi_dte=hi,
        call_wall=cw, call_wall_oi=cwo, put_wall=pw, put_wall_oi=pwo,
        call_walls_top=sorted(cs, key=lambda x: -x[1])[:3],
        put_walls_top=sorted(ps, key=lambda x: -x[1])[:3],
        total_call_oi=tc, total_put_oi=tp, n_strikes=len(by_strike),
    )


def layered_walls(snap, today: date, spot: float) -> dict[str, WallLayer]:
    return {k: _layer_walls(snap, today, spot, lo, hi, k, lab)
            for k, lab, lo, hi in WALL_LAYERS}


def wall_agreement(layers: dict[str, WallLayer], side: str) -> tuple[bool, str]:
    """近端与中端是否指向同一位置 —— 一致 = 真共识位，不一致 = 混算会造假墙的地方。

    只判 near vs mid：这两层是交易相关的（本周进出场 + 持仓所在）。far(>45天) 是机构
    长期布局，与本周价格无因果关系，给它否决权会把真共识误报成分歧 —— 实测 2026-08-31
    GLD call 近/中端都是 430、far 在 460，若三层同判就会把 430 这个真阻力抹掉。
    far 仅作为附注呈现，不参与 agree 判定。
    """
    def _w(L):
        return (L.call_wall if side == "call" else L.put_wall,
                L.call_wall_oi if side == "call" else L.put_wall_oi)
    n_w, n_oi = _w(layers["near"])
    m_w, m_oi = _w(layers["mid"])
    f_w, f_oi = _w(layers["far"])
    far_note = f"；远端在 {f_w:g}" if f_oi > 0 else ""
    if n_oi == 0 or m_oi == 0:
        which = "近端" if n_oi == 0 else "中端"
        return False, f"{which}该侧无仓，无法判定一致性{far_note}"
    if n_w > 0 and abs(m_w - n_w) / n_w <= WALL_SAME_TOL:
        return True, f"近/中端一致于 {n_w:g} · 真共识位{far_note}"
    return False, (f"近端 {n_w:g} vs 中端 {m_w:g} 不一致 · 混算会在两者之间造出假墙"
                   f"{far_note}")


@dataclass(frozen=True)
class LadderStep:
    strike: float
    oi: int
    share: float          # 占该层该侧总 OI 的比例
    dist_pct: float       # 相对现价，支撑为负、阻力为正
    gap_after: float = 0.0  # 与下一档之间的真空跨度(%)，0=紧邻
    # 该档里【当日到期】的 OI。报告以 obs_day(=快照日前一工作日) 计时，好让 0DTE 不被
    # 当成已过期剔除（当天它们仍在交易），代价是当日到期也被算进"近端支撑"——而它们
    # 今天收盘就消失。2026-08-31 SLV 的看跌增仓 12,072 张里 7,749 张是当日到期，
    # 不拆开看会把一个当天就蒸发的结构读成持续压力。
    expiring: int = 0

    @property
    def expiring_share(self) -> float:
        return self.expiring / self.oi if self.oi else 0.0


def support_ladder(snap, today: date, spot: float, *, side: str = "put",
                   max_dte: int = 14, min_dte: int = 1,
                   min_share: float = 0.03, gap_pct: float = 2.0,
                   expiring_on: date | None = None) -> list[LadderStep]:
    """近端支撑/阻力阶梯 + 真空区。

    回答的是"价格往下(上)走，一路上有没有东西挡"，混算墙位给不出这个 ——
    2026-08-31 GLD 近端支撑 407/405/404/402/400/396 六档间距 0.2~0.6%，
    但 396→370 之间是 6.3% 真空：守住 396 很厚，破了 396 比白银滑得还快。
    只有逐档列出来才看得见这种"厚一段、然后断崖"的结构。

    min_share: 低于此占比的档位视为挡不住，不计入阶梯（但计入真空跨度）。
    gap_pct:   相邻两档间距超过此值即标注为真空区。
    """
    want_call = side == "call"
    agg: dict[float, int] = {}
    exp: dict[float, int] = {}
    for c in snap.with_oi():
        if c.is_call != want_call:
            continue
        d = (c.expiry - today).days
        if not (min_dte <= d <= max_dte):
            continue
        if want_call and c.strike <= spot:
            continue
        if not want_call and c.strike >= spot:
            continue
        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
        if expiring_on is not None and c.expiry == expiring_on:
            exp[c.strike] = exp.get(c.strike, 0) + c.open_interest
    total = sum(agg.values())
    if total <= 0:
        return []
    # 支撑从高到低（离现价由近及远）；阻力从低到高
    ordered = sorted(agg.items(), key=lambda x: -x[0] if not want_call else x[0])
    keep = [(k, v) for k, v in ordered if v / total >= min_share]
    out: list[LadderStep] = []
    for i, (k, v) in enumerate(keep):
        gap = 0.0
        if i + 1 < len(keep):
            nxt = keep[i + 1][0]
            g = abs(nxt - k) / k * 100
            if g >= gap_pct:
                gap = g
        out.append(LadderStep(strike=k, oi=v, share=v / total,
                              dist_pct=(k / spot - 1) * 100, gap_after=gap,
                              expiring=exp.get(k, 0)))
    return out


def ladder_bands(snap, today: date, spot: float, *, max_dte: int = 14) -> dict:
    """近端下方支撑按跌幅分区的密度 —— 用来一眼看出"支撑堆在脚下还是堆在深渊"。

    2026-08-31 实测：GLD 0~-5% 占下方 70%，SLV 只占 30%、44% 堆在 -10% 以下。
    这就是"黄金阶梯式支撑、白银支撑稀疏"的量化形式。
    """
    agg: dict[float, int] = {}
    for c in snap.with_oi():
        if c.is_call or c.strike >= spot:
            continue
        if not (1 <= (c.expiry - today).days <= max_dte):
            continue
        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
    total = sum(agg.values())
    if total <= 0:
        return {"total": 0, "bands": []}
    bands = []
    for lo, hi, lab in ((0, 5, "0~-5%"), (5, 10, "-5~-10%"), (10, 100, "-10% 以下")):
        v = sum(x for k, x in agg.items() if lo <= (1 - k / spot) * 100 < hi)
        bands.append({"label": lab, "oi": v, "share": v / total})
    return {"total": total, "bands": bands, "max_dte": max_dte}


# ═══════════════════════════════════════════════════════════════════════════
# 结构主墙 vs 局部 pin —— codex 2026-09-01 P0-5
# ═══════════════════════════════════════════════════════════════════════════
# 【问题】本模块此前所有"墙"都是「在现价 ±band 内取 OI 最大」。band 内【总能】
# 找到一个最大值，那不是墙，只是范围内碰巧最大的行权价。实测 2026-09-01：
#     SLV 现价 58.5，band=5% 选出 55(102,820)，而真正的 50 有 168,526 张
#     —— 差 1.6 倍，且 50 连 ±15% 都在范围外。
# 【但不限范围也错】：全范围最大会选中 SLV call 100（距现价 +71%）这类
#     LEAPS/彩票堆积，那里一周到期的报价是 0.00。
#
# 【判据不是距离，是近端到期占比】（2026-09-01 实测）：
#     GLD put 330  ≤30天占 2%、136天单一到期占 57%  → 长期对冲堆积
#     GLD put 400  ≤30天占 28%、到期分布均匀        → 真正的多到期承接区
#     SLV put  30  >180天占 62%                     → 尾部保险
# 长期堆积的 OI 集中在个别远月；真承接区在各到期上都有分布。
#
# 【两个概念必须分开命名，不得混用】：
#     structural_walls() 全范围 + 近端占比门槛 → 客观描述 OI 堆在哪
#     local_pin()        近价带内最大          → 短期钉住效应，【不是】墙
# 策略层要"卖在墙上"时用前者，但还需自行检查权利金是否够（墙可能很远）。

NEAR_DTE = 30              # "近端"的定义
NEAR_SHARE_MIN = 0.15      # 近端 OI 占比门槛：低于此视为长期堆积，不算结构墙
STRUCT_MIN_SHARE = 0.03    # 该档位至少要占同侧总 OI 的 3%，滤掉零碎


def structural_walls(snap, today: date, spot: float, kind: str, *,
                     top_n: int = 3, near_dte: int = NEAR_DTE,
                     near_share_min: float = NEAR_SHARE_MIN,
                     min_share: float = STRUCT_MIN_SHARE) -> list[dict]:
    """结构主墙：全行权价范围内的 OI 堆积，滤掉长期对冲/尾部保险堆积。

    只看虚值一侧（put 取 ≤spot，call 取 ≥spot）—— 实值侧的 OI 不构成支撑阻力。
    返回按 OI 降序的前 top_n，每项含 strike/oi/share/near_share/dist_pct。
    **不保证权利金可观**：结构墙可能距现价很远（SLV 50 距 −14.5%），
    策略层必须自己检查报价，不得假设"墙上一定收得到钱"。
    """
    agg: dict[float, int] = {}
    near: dict[float, int] = {}
    for c in snap.contracts:
        if c.kind != kind or not c.open_interest:
            continue
        if kind == "P" and c.strike > spot:
            continue
        if kind == "C" and c.strike < spot:
            continue
        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
        if (c.expiry - today).days <= near_dte:
            near[c.strike] = near.get(c.strike, 0) + c.open_interest
    total = sum(agg.values())
    if total <= 0:
        return []
    out = []
    for k, v in agg.items():
        share = v / total
        ns = near.get(k, 0) / v if v else 0.0
        if share < min_share or ns < near_share_min:
            continue
        out.append({"strike": k, "oi": v, "share": share, "near_share": ns,
                    "dist_pct": (k / spot - 1) * 100 if spot else 0.0})
    out.sort(key=lambda x: -x["oi"])
    return out[:top_n]


def local_pin(snap, today: date, spot: float, kind: str, *,
              band: float = 0.05, max_dte: int = NEAR_DTE) -> dict | None:
    """局部 pin：现价 ±band 内 OI 最大的行权价。**这不是墙。**

    band 内总能找到一个最大值，所以它永远有输出 —— 这正是它不能当墙用的原因。
    它描述的是「短期内价格附近哪一档挂单最多」，可用于判断当日钉住倾向，
    不可用于判断支撑强度，更不能反转 structural_walls() 的结论。
    """
    agg: dict[float, int] = {}
    for c in snap.contracts:
        if c.kind != kind or not c.open_interest:
            continue
        if (c.expiry - today).days > max_dte:
            continue
        if kind == "P" and not (spot * (1 - band) <= c.strike <= spot):
            continue
        if kind == "C" and not (spot <= c.strike <= spot * (1 + band)):
            continue
        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
    if not agg:
        return None
    k = max(agg, key=agg.get)
    tot = sum(agg.values())
    return {"strike": k, "oi": agg[k], "share": agg[k] / tot if tot else 0.0,
            "dist_pct": (k / spot - 1) * 100 if spot else 0.0, "band": band}
