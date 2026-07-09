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

from dataclasses import dataclass, field, replace
from datetime import date

from undertow.core.models import OptionsSnapshot, OptionContract

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
# —— 价差结构识别（保守，宁缺勿滥，避免把相邻买卖方误配成价差）——
SPREAD_MAX_WIDTH_FRAC = 0.08  # 两腿行权价间距 ≤ 短腿×此（垂直价差两腿应相近）
SPREAD_MIN_SIZE = 2 * MIN_DOI  # 两腿较小者需 ≥ 此（双腿都明显高于噪音才算）
SPREAD_TOP_N = 4             # 最多上报的价差数（按规模取前 N，其余视为噪音）
# —— 速读用结构性异动（墙体滚动/墙上增减/最大建仓，门槛取"结构级"远高于噪音）——
ROLL_MAX_GAP_FRAC = 0.04     # 滚动两腿行权价间距 ≤ 旧腿×此（相邻才算"搬墙"）
ROLL_BALANCE = 0.4           # 两腿规模较小者 ≥ 较大者×此，一减一增才认定为同一笔搬仓
MOVE_MIN_DOI = 1_000         # 进速读的单点 |ΔOI| 门槛
# —— 波动率面（ATM IV / 25Δ·10Δ 偏斜，作者口径的"买方确认"检查）——
VOL_MIN_DAYS = 10            # 到期太近 IV 噪音大，不用
VOL_MAX_DAYS = 90
VOL_TARGET_DAYS = 30         # 优先取最接近 30 天的到期做"标尺"
VOL_MIN_QUOTES = 4           # 单侧（C/P 各自）至少几个有效 IV 报价才可信
VOL_MAX_ABS_DELTA = 0.85     # 深 ITM 的 IV 不可靠，剔除
VOL_MIN_ABS_DELTA = 0.02     # 太深 OTM 报价稀疏，也剔除
D_ATM_SIG = 0.3              # |ΔATM IV| 超过此(pp)才算有信息
D_SKEW_SIG = 0.5             # |Δskew| 超过此(pp)才算明显收敛/走陡
D_SPOT_SIG = 0.5             # |Δspot%| 超过此才算"明显涨/跌"


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
    weight: float = 0.0    # 该腿的方向权重（用于压力聚合 / 价差扣减）
    spread_note: str = ""  # 若属某价差结构的腿，标注（如"熊市看涨价差·保护腿"）


@dataclass(frozen=True)
class Spread:
    """检测到的疑似垂直价差结构（同 C/P、相邻行权价、卖一腿 + 买一腿）。"""
    kind: str
    name: str             # 熊市看涨价差(Bear Call) 等
    short_strike: float   # 卖出腿（决定方向）
    long_strike: float    # 买入腿（封顶/保护腿）
    size: int
    net_bias: str         # bearish / bullish（看短腿）
    detail: str


@dataclass(frozen=True)
class VolRead:
    """某一天、某个到期的波动率面读数（单位 pp，即百分数点）。"""
    expiry: date
    days_out: int
    atm_iv_pp: float       # ATM 隐含波动率
    skew25_pp: float       # 25Δ put IV − 25Δ call IV（越大 = put 越贵 = 下行担忧越重）
    skew10_pp: float       # 10Δ 同理（更极端的尾部保护）


@dataclass(frozen=True)
class VolSurface:
    """两日波动率面对比 + 作者口径判读。

    来自作者 7/2 黄金分析的方法：大涨若无买方在期权端追价（ATM IV 反被压、
    put-call skew 不明显收敛），说明上涨是空头回补而非新多进场，动力存疑。
    诚实边界：事件日（非农/CPI/FOMC 兑现后）IV 回落含事件溢价释放的机械成分，判读要打折。
    """
    curr: VolRead
    prev: VolRead | None
    d_spot_pct: float      # 现价两日变化 %
    verdict: str           # 中文判读

    @property
    def d_atm_pp(self) -> float:
        return (self.curr.atm_iv_pp - self.prev.atm_iv_pp) if self.prev else 0.0

    @property
    def d_skew25_pp(self) -> float:
        return (self.curr.skew25_pp - self.prev.skew25_pp) if self.prev else 0.0

    @property
    def d_skew10_pp(self) -> float:
        return (self.curr.skew10_pp - self.prev.skew10_pp) if self.prev else 0.0


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
    downside_pressure: float = 0.0  # 买卖方判定加权的下行压力（已扣价差保护腿）
    upside_pressure: float = 0.0
    flow_tilt: str = "—"
    spreads: list[Spread] = field(default_factory=list)  # 检测到的疑似价差结构
    call_wall: float | None = None
    put_wall: float | None = None
    vol: VolSurface | None = None  # 波动率面：ATM IV / skew 日变化（买方确认检查）


def structural_moves(fa: "FlowAnalysis", *, conv=None, top_n: int = 2) -> list[str]:
    """从两日 ΔOI 里挑最结构性的动作，拼成速读用短句（确定性拼句，无新判断）。

    优先级：墙体滚动（同类相邻行权价一减一增 = 防线/压制位平移，如 360P→355P）
    > 墙上大额增减 > 最大单点建仓。conv 把 ETF 行权价换算成展示口径（商品价）。
    """
    if not fa.changes:
        return []
    cv = conv or (lambda v: v)
    fmt = (lambda v: f"{cv(v):,.0f}") if cv(fa.spot) >= 500 else (lambda v: f"{cv(v):,.1f}")
    moves: list[str] = []
    used: set[tuple[float, str]] = set()

    # 1) 墙体滚动：一减一增、行权价相邻、规模相当（changes 已按 |ΔOI| 降序）
    big = [c for c in fa.changes if abs(c.d_oi) >= MOVE_MIN_DOI]
    for dn in (c for c in big if c.d_oi < 0):
        if (dn.strike, dn.kind) in used:
            continue
        for up in (c for c in big if c.d_oi > 0 and c.kind == dn.kind
                   and (c.strike, c.kind) not in used):
            gap = abs(up.strike - dn.strike)
            if not (0 < gap <= dn.strike * ROLL_MAX_GAP_FRAC):
                continue
            lo, hi = sorted((abs(dn.d_oi), up.d_oi))
            if lo < hi * ROLL_BALANCE:
                continue
            # 双腿各带买卖方判定入句（作者口径：谁在撤、谁在进），结尾给净方向
            def _leg(c: FlowChange) -> str:
                j = c.judgment
                if j == "噪音" or "减仓" in j:      # 无 IV 方向信息时只描述 OI 动作
                    j = "增仓" if c.d_oi > 0 else "减仓"
                wall = f"（{c.on_wall}）" if c.on_wall else ""
                return f"{fmt(c.strike)}{wall} {j}（{c.d_oi:+,} 手）"

            # 净方向只看有 IV 信息的腿（中性/减仓腿不投票）；同向即明说资本方向
            sides = {b for b in (dn.bias, up.bias) if b != "neutral"}
            if sides == {"bearish"}:
                concl = "资本更看跌"
            elif sides == {"bullish"}:
                concl = "资本更看多"
            else:  # 两腿判定对立或全无信息：退回仓位平移的位置学描述
                buyer = "买方" in up.judgment
                if dn.kind == "P":
                    concl = (("看跌目标下探" if buyer else "支撑防线后撤")
                             if up.strike < dn.strike else
                             ("看跌/保护重心上移" if buyer else "承接位上移"))
                else:
                    concl = (("买方目标下移" if buyer else "压制位下压")
                             if up.strike < dn.strike else
                             ("上方目标上移" if buyer else "压制位上移"))
            side = "put 端" if dn.kind == "P" else "call 端"
            moves.append(f"{side} {_leg(dn)}、{_leg(up)}——{concl}")
            used.update({(dn.strike, dn.kind), (up.strike, up.kind)})
            break

    # 2) 墙上大额增减
    for c in fa.changes:
        if (c.strike, c.kind) in used or not c.on_wall or abs(c.d_oi) < MOVE_MIN_DOI:
            continue
        note = ("增厚，" + ("天花板更结实" if c.on_wall == "call墙" else "承接更结实")
                ) if c.d_oi > 0 else ("被削，" + ("压制松动" if c.on_wall == "call墙"
                                                  else "承接减弱"))
        moves.append(f"{c.on_wall} {fmt(c.strike)} {note}（ΔOI {c.d_oi:+,} 手）")
        used.add((c.strike, c.kind))

    # 3) 最大单点建仓（带买卖方判定），补足条数
    for c in fa.changes:
        if len(moves) >= top_n:
            break
        if (c.strike, c.kind) in used or abs(c.d_oi) < MOVE_MIN_DOI:
            continue
        act = "新增" if c.d_oi > 0 else "减仓"
        moves.append(f"{fmt(c.strike)} {'put' if c.kind == 'P' else 'call'} "
                     f"{act} {abs(c.d_oi):,} 手（{c.judgment}）")
        used.add((c.strike, c.kind))

    return moves[:top_n]


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


def detect_spreads(changes: list[FlowChange]) -> list[Spread]:
    """检测疑似垂直价差：同 C/P、卖一腿 + 买一腿、量级相当、行权价相邻。

    复刻作者 6/25 WTI 的识破——表面"上方大量买 Call"实为 Bear Call Spread 的保护腿，
    净头寸看短腿（卖 70C）的压制。垂直价差方向由"卖低买高/卖高买低"判定：
      Call: 卖低买高=熊市看涨(净空)；卖高买低=牛市看涨(净多)
      Put : 卖高买低=牛市看跌(净多)；卖低买高=熊市看跌(净空)
    """
    out: list[Spread] = []
    used: set = set()
    for kind in ("C", "P"):
        building = [c for c in changes if c.kind == kind and c.d_oi > 0]
        sellers = [c for c in building if "卖方" in c.judgment]
        buyers = [c for c in building if "买方" in c.judgment]
        for s in sorted(sellers, key=lambda x: -x.d_oi):
            if (s.strike, kind) in used:
                continue
            max_width = SPREAD_MAX_WIDTH_FRAC * s.strike
            cands = [b for b in buyers if (b.strike, kind) not in used
                     and 1e-6 < abs(b.strike - s.strike) <= max_width  # 两腿相近才算垂直价差
                     and 0.4 <= (b.d_oi / s.d_oi) <= 2.5]              # 量级相当
            if not cands:
                continue
            b = min(cands, key=lambda x: abs(x.strike - s.strike))
            size = min(s.d_oi, b.d_oi)
            if size < SPREAD_MIN_SIZE:   # 双腿都得明显高于噪音
                continue
            if kind == "C":
                name, net = ("熊市看涨价差(Bear Call)", "bearish") if s.strike < b.strike \
                    else ("牛市看涨价差(Bull Call)", "bullish")
            else:
                name, net = ("牛市看跌价差(Bull Put)", "bullish") if s.strike > b.strike \
                    else ("熊市看跌价差(Bear Put)", "bearish")
            used.add((s.strike, kind)); used.add((b.strike, kind))
            dir_cn = "看空" if net == "bearish" else "看多"
            out.append(Spread(
                kind=kind, name=name, short_strike=s.strike, long_strike=b.strike,
                size=size, net_bias=net,
                detail=f"卖 {s.strike:.0f}{kind} + 买 {b.strike:.0f}{kind}（各约 {size:,}）"
                       f" → {name}，净{dir_cn}（与净向相反的腿为封顶/保护，不计方向）",
            ))
    out.sort(key=lambda sp: -sp.size)
    return out[:SPREAD_TOP_N]   # 只报规模最大的几笔，宁缺勿滥


def _interp(pts: list[tuple[float, float]], x: float) -> float | None:
    """按 x 升序线性插值；x 超界取最近端点。pts 需 ≥2 个。"""
    if len(pts) < 2:
        return None
    pts = sorted(pts)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y0
    return None


def read_vol(snap: OptionsSnapshot, *, today: date,
             expiry: date | None = None) -> VolRead | None:
    """从单张快照读某个到期的 ATM IV 与 25Δ/10Δ 偏斜。

    到期选择：[VOL_MIN_DAYS, VOL_MAX_DAYS] 内、C/P 两侧有效报价都够数的到期里，
    取最接近 VOL_TARGET_DAYS 的那个（约一个月，作者表里的"主力月份"口径）。
    传入 expiry 则强制用它（对比昨日时保证同一到期，才可比）。
    """
    if snap.spot <= 0:
        return None
    by_exp: dict[date, dict[str, list]] = {}
    for c in snap.contracts:
        d = (c.expiry - today).days
        if not (VOL_MIN_DAYS <= d <= VOL_MAX_DAYS):
            continue
        if c.iv <= 0 or not (VOL_MIN_ABS_DELTA <= abs(c.delta) <= VOL_MAX_ABS_DELTA):
            continue
        by_exp.setdefault(c.expiry, {"C": [], "P": []})[c.kind].append(c)
    if expiry is not None:
        cands = [expiry] if expiry in by_exp else []
    else:
        cands = [e for e, s in by_exp.items()
                 if len(s["C"]) >= VOL_MIN_QUOTES and len(s["P"]) >= VOL_MIN_QUOTES]
        cands.sort(key=lambda e: abs((e - today).days - VOL_TARGET_DAYS))
    if not cands:
        return None
    exp = cands[0]
    calls, puts = by_exp[exp]["C"], by_exp[exp]["P"]
    if len(calls) < 2 or len(puts) < 2:
        return None
    # ATM：C/P 各按行权价插值到现价，再取两侧均值（剔单侧报价噪音）
    atm_c = _interp([(c.strike, c.iv) for c in calls], snap.spot)
    atm_p = _interp([(c.strike, c.iv) for c in puts], snap.spot)
    atm = [v for v in (atm_c, atm_p) if v is not None]
    if not atm:
        return None
    # 偏斜：按 |Delta| 插值（比按行权价稳，自动适配现价移动）
    civ = [(abs(c.delta), c.iv) for c in calls]
    piv = [(abs(c.delta), c.iv) for c in puts]
    c25, p25 = _interp(civ, 0.25), _interp(piv, 0.25)
    c10, p10 = _interp(civ, 0.10), _interp(piv, 0.10)
    if None in (c25, p25, c10, p10):
        return None
    return VolRead(
        expiry=exp, days_out=(exp - today).days,
        atm_iv_pp=round(100.0 * sum(atm) / len(atm), 2),
        skew25_pp=round(100.0 * (p25 - c25), 2),
        skew10_pp=round(100.0 * (p10 - c10), 2),
    )


def _vol_verdict(d_spot_pct: float, d_atm: float, d_skew25: float) -> str:
    """作者口径：价格变动 × ATM IV 变动 × 偏斜收敛与否 → 期权端是否"确认"价格。"""
    if d_spot_pct >= D_SPOT_SIG:                    # 明显上涨
        if d_atm <= -D_ATM_SIG:
            base = "价涨而 ATM IV 被压 → 没有买方追价抢筹（涨势更像空头回补）"
            if d_skew25 <= -D_SKEW_SIG:
                return base + "；但偏斜明显收敛，下行担忧同步减退 → 信号中性"
            return base + "；且偏斜未明显收敛 → 期权端未确认涨势，后续动力存疑"
        if d_atm >= D_ATM_SIG:
            return "价涨且 ATM IV 抬升 → 买方追价，涨势获期权端确认"
        return "价涨、ATM IV 大体持平 → 期权端未表态"
    if d_spot_pct <= -D_SPOT_SIG:                   # 明显下跌
        if d_atm >= D_ATM_SIG:
            if d_skew25 >= D_SKEW_SIG:
                return "价跌且 ATM IV / put 偏斜齐升 → 恐慌买保护，下行动能获确认"
            return "价跌且 ATM IV 抬升 → 保护需求上升"
        if d_atm <= -D_ATM_SIG:
            return "价跌而 ATM IV 回落 → 恐慌有限，跌势未获期权端追认"
        return "价跌、ATM IV 大体持平 → 期权端未表态"
    # 价格大体持平：只看偏斜
    if d_skew25 >= D_SKEW_SIG:
        return "价平但 put 偏斜走陡 → 下行担忧升温"
    if d_skew25 <= -D_SKEW_SIG:
        return "价平且偏斜收敛 → 下行担忧减退"
    return "价平、波动率面无方向信息"


def vol_surface(prev: OptionsSnapshot | None, curr: OptionsSnapshot, *,
                today: date) -> VolSurface | None:
    """两日波动率面对比。prev 缺失时只给当日水平（明日起点亮判读）。"""
    cr = read_vol(curr, today=today)
    if cr is None:
        return None
    pr = read_vol(prev, today=today, expiry=cr.expiry) if prev is not None else None
    if pr is None:
        return VolSurface(curr=cr, prev=None, d_spot_pct=0.0,
                          verdict="仅当日水平（无可比昨日同到期快照），明日起可出日变化判读")
    d_spot = 100.0 * (curr.spot / prev.spot - 1.0) if prev.spot > 0 else 0.0
    verdict = _vol_verdict(d_spot, cr.atm_iv_pp - pr.atm_iv_pp, cr.skew25_pp - pr.skew25_pp)
    return VolSurface(curr=cr, prev=pr, d_spot_pct=round(d_spot, 2), verdict=verdict)


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
    spreads: list[Spread] = []
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
                bias=bias, judgment=judgment, on_wall=on_wall, note=note, weight=w,
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

        # —— 价差结构识别：扣除"封顶/保护腿"的方向压力，避免把价差误读为方向 ——
        spreads = detect_spreads(changes)
        if spreads:
            labels: dict[tuple, str] = {}
            for sp in spreads:
                for c in changes:
                    if c.kind != sp.kind:
                        continue
                    is_short = abs(c.strike - sp.short_strike) < 1e-6
                    is_long = abs(c.strike - sp.long_strike) < 1e-6
                    if not (is_short or is_long):
                        continue
                    labels[(c.strike, c.kind)] = sp.name + ("·短腿(方向)" if is_short else "·长腿(保护)")
                    # 与净方向相反的腿 = 封顶腿，扣其方向压力
                    if c.bias != sp.net_bias and c.bias in ("bearish", "bullish"):
                        p = abs(c.d_oi) * c.weight
                        if c.bias == "bearish":
                            downside -= p
                        else:
                            upside -= p
            downside, upside = max(0.0, downside), max(0.0, upside)
            changes = [replace(c, spread_note=labels.get((c.strike, c.kind), "")) for c in changes]

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
        spreads=spreads,
        call_wall=call_wall,
        put_wall=put_wall,
        vol=vol_surface(prev, curr, today=today),
    )
