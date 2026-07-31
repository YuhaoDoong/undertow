"""铁鹰（Iron Condor）策略子模块：区间震荡 + 偏卖方环境下的规则化结构映射。

定位（与 strategy.py 同级的一个【独立策略子模块】，由 strategy_hub 统筹）：
  * 只在"波动率偏卖方 + 现价被上下两道墙夹住（区间结构）"时才适配——铁鹰是
    净卖波动率（short vega/gamma），卖的是"横盘 + IV 回落"，怕单边大行情。
  * 选腿规则（确定性）：
      卖腿 = 现价上/下方、|delta|∈[SELL_DELTA_MIN, SELL_DELTA_MAX]、OI 最大的墙。
             （同时用 delta 和墙 OI 两个维度：排除 delta 太低的远虚"天花板墙"，
               只在贴价的显著墙上卖 → 收权利金且有墙的吸附力。）
      买翼 = 卖腿外侧、|delta| 最接近 WING_DELTA_TARGET 的行权价（保护尾部）。
  * 权利金/盈亏用各腿 iv 反算 BS 理论中值（CBOE 快照无 bid/ask）；实盘按 bid/ask
    穿价成交会略低于理论净收。
  * 适配度打分：卖腿 delta 分档（A型 1σ外 / B型 贴墙 / 激进）、净收/翼宽比、
    skew 摩擦（买翼是否比卖身贵）、现价居中度。

诚实边界：这是【策略模块的确定性结构输出与体检】，给人/顾问做参考框架，不是交易
指令、更不替使用者拍板；行权价均为 ETF 口径（与用户实际下单口径一致）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from undertow.analyze import blackscholes as bs
from undertow.core.models import OptionsSnapshot, OptionContract

# —— 到期选择：theta 甜区（距今交易日历日）——
SWEET_DTE_LO = 14       # 太近 gamma 风险陡增、权利金薄
SWEET_DTE_HI = 45       # 太远 vega 暴露长、theta 慢、资金占用久
FALLBACK_DTE_MIN = 7    # 甜区无到期时的兜底下限

# —— 选腿 delta 区间 ——
SELL_DELTA_MIN = 0.16   # 卖腿 |delta| 下限（低于此 = 远虚天花板，当买翼不当卖腿）
SELL_DELTA_MAX = 0.42   # 卖腿 |delta| 上限（高于此 = 太贴价、几乎必被触及）
WING_DELTA_TARGET = 0.10  # 买翼目标 |delta|（保护尾部、成本低）
MIN_LEG_OI = 500        # 每条腿最低 OI（流动性门槛）

# —— 适配度分档 ——
TYPE_A_DELTA = 0.22     # 卖腿均 |delta| ≤ 此 → 1σ外 A 型
TYPE_B_DELTA = 0.40     # ≤ 此 → 贴墙 B 型；> 此 → 激进
CREDIT_RATIO_MIN = 1.0 / 3.0  # 净收 / 翼宽 的健康下限
SKEW_COST_SIG = 3.0     # 买翼 IV − 卖身 IV 超过此(pp) → skew 摩擦显著
_ANNUALIZE_DAYS = 365.0


@dataclass(frozen=True)
class CondorLeg:
    action: str            # 卖出 / 买入
    kind: str              # C / P
    strike: float
    delta: float           # 快照 delta（原符号）
    iv_pp: float           # 隐含波动率（pp）
    bs_price: float         # 每股 BS 理论价


@dataclass(frozen=True)
class CondorPlan:
    """铁鹰结构映射 + 盈亏结构 + 适配度体检。金额单位：每股为权利金，$ 为每 1 组合(×100)。"""
    applicable: bool
    stance: str                             # 来自 volregime 的波动率倾向
    headline: str
    expiry: date | None = None
    dte: int | None = None
    spot: float | None = None
    legs: list[CondorLeg] = field(default_factory=list)  # [卖P, 买P翼, 卖C, 买C翼]
    net_credit: float | None = None          # 每股（理论中值）
    width_put: float | None = None
    width_call: float | None = None
    max_profit: float | None = None          # $ / 组合
    max_loss: float | None = None            # $ / 组合
    rr: float | None = None                  # 盈亏比 = 最大盈利 / 最大亏损
    be_lo: float | None = None               # 下盈亏平衡
    be_hi: float | None = None               # 上盈亏平衡
    centering: float | None = None           # 0=完美居中，→1=贴某侧墙
    condor_type: str = ""                    # 1σ外A型 / 贴墙B型 / 激进
    fit_score: int = 0                       # 0~100
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


def _fmt_k(spot: float):
    return (lambda v: f"{v:,.0f}") if spot >= 500 else (lambda v: f"{v:,.1f}")


def _pick_expiry(snap: OptionsSnapshot, today: date) -> tuple[date | None, int, bool]:
    """选到期：theta 甜区内总 OI 最大；无则兜底取 ≥FALLBACK_DTE_MIN 的最厚。返回 (expiry, dte, in_sweet)。"""
    oi_by_exp: dict[date, int] = {}
    dte_by_exp: dict[date, int] = {}
    for c in snap.contracts:
        if c.open_interest <= 0:
            continue
        oi_by_exp[c.expiry] = oi_by_exp.get(c.expiry, 0) + c.open_interest
        dte_by_exp[c.expiry] = (c.expiry - today).days
    if not oi_by_exp:
        return None, 0, False
    sweet = [(e, o) for e, o in oi_by_exp.items() if SWEET_DTE_LO <= dte_by_exp[e] <= SWEET_DTE_HI]
    if sweet:
        e = max(sweet, key=lambda t: t[1])[0]
        return e, dte_by_exp[e], True
    fb = [(e, o) for e, o in oi_by_exp.items() if dte_by_exp[e] >= FALLBACK_DTE_MIN]
    if not fb:
        return None, 0, False
    e = max(fb, key=lambda t: t[1])[0]
    return e, dte_by_exp[e], False


def _pick_sell_leg(cands: list[OptionContract], spot: float, side: str) -> OptionContract | None:
    """卖腿：现价上(call)/下(put)方、|delta|∈[MIN,MAX]、OI 最大的墙。"""
    pool = []
    for c in cands:
        if c.open_interest < MIN_LEG_OI:
            continue
        if side == "C" and c.strike <= spot:
            continue
        if side == "P" and c.strike >= spot:
            continue
        if SELL_DELTA_MIN <= abs(c.delta) <= SELL_DELTA_MAX:
            pool.append(c)
    if not pool:
        return None
    return max(pool, key=lambda c: c.open_interest)


def _wing_pool(cands: list[OptionContract], sell_strike: float, side: str) -> list[OptionContract]:
    """卖腿外侧、比卖腿更虚、流动性达标的候选翼。"""
    pool = []
    for c in cands:
        if c.open_interest < MIN_LEG_OI:
            continue
        if side == "C" and c.strike <= sell_strike:
            continue
        if side == "P" and c.strike >= sell_strike:
            continue
        if abs(c.delta) >= SELL_DELTA_MIN:  # 翼要比卖腿更虚
            continue
        pool.append(c)
    return pool


def _pick_wings(puts, calls, sell_put, sell_call):
    """买双翼：先各自按 |delta|≈WING_DELTA_TARGET 初选，再取较窄侧为【统一翼宽】
    使两侧对称（对称铁鹰风险更可控、最大亏损更小），最后落到最接近该宽度的实际行权价。"""
    pp, cp = _wing_pool(puts, sell_put.strike, "P"), _wing_pool(calls, sell_call.strike, "C")
    if not pp or not cp:
        return None, None
    bp0 = min(pp, key=lambda c: abs(abs(c.delta) - WING_DELTA_TARGET))
    bc0 = min(cp, key=lambda c: abs(abs(c.delta) - WING_DELTA_TARGET))
    width = min(sell_put.strike - bp0.strike, bc0.strike - sell_call.strike)  # 较窄侧 → 对称
    buy_put = min(pp, key=lambda c: abs(c.strike - (sell_put.strike - width)))
    buy_call = min(cp, key=lambda c: abs(c.strike - (sell_call.strike + width)))
    return buy_put, buy_call


def _not_applicable(stance: str, headline: str, reasons: list[str]) -> CondorPlan:
    return CondorPlan(applicable=False, stance=stance, headline=headline, reasons=reasons,
                      caveats=["铁鹰是净卖波动率结构，只在区间震荡 + 期权偏贵时占优；"
                               "以上为策略模块的确定性判断，非交易指令。"])


def assess_condor(*, snap: OptionsSnapshot, vr, today: date, fa=None) -> CondorPlan:
    """组合波动率倾向 + 期权链墙位 → 铁鹰结构映射与适配度。

    snap: 当前期权链快照（OptionsSnapshot，ETF 口径）。
    vr:   volregime.VolRegime（需 stance=="偏卖方" 才适配）。
    fa:   flow.FlowAnalysis | None（可选，用 skew / 方向做微调提示）。
    """
    stance = getattr(vr, "stance", "数据不足")
    spot = snap.spot

    # —— 门槛 1：波动率必须偏卖方（铁鹰卖 vega，需期权偏贵）——
    if stance != "偏卖方":
        return _not_applicable(
            stance,
            f"波动率环境为「{stance}」，铁鹰（净卖方结构）暂不适配——卖波动率需要期权相对偏贵。",
            [f"前置条件未满足：波动率栏为「{stance}」，非「偏卖方」。铁鹰在此环境无正期望优势。"])

    # —— 门槛 2：选到期（theta 甜区）——
    expiry, dte, in_sweet = _pick_expiry(snap, today)
    if expiry is None:
        return _not_applicable(stance, "无足够流动性的到期可构造铁鹰。",
                               ["期权链无满足流动性/到期条件的合约。"])
    exp_contracts = [c for c in snap.contracts if c.expiry == expiry and c.open_interest > 0]
    calls = [c for c in exp_contracts if c.kind == "C"]
    puts = [c for c in exp_contracts if c.kind == "P"]

    # —— 门槛 3：现价被上下两道墙夹住（区间结构）——
    sell_put = _pick_sell_leg(puts, spot, "P")
    sell_call = _pick_sell_leg(calls, spot, "C")
    if sell_put is None or sell_call is None:
        miss = []
        if sell_put is None:
            miss.append("现价下方无符合条件的 put 墙")
        if sell_call is None:
            miss.append("现价上方无符合条件的 call 墙")
        return _not_applicable(
            stance, "现价未被上下两道墙夹住（缺少区间结构），铁鹰不适配。",
            ["；".join(miss) + "——可能是单边趋势市，宜用单边价差而非铁鹰。"])

    buy_put, buy_call = _pick_wings(puts, calls, sell_put, sell_call)
    if buy_put is None or buy_call is None:
        return _not_applicable(
            stance, "卖腿外侧无合适保护翼可挂，无法构造有限风险铁鹰。",
            ["卖腿更外侧缺少满足流动性的保护翼行权价。"])

    # —— 权利金：各腿 iv 反算 BS 理论价 ——
    T = dte / _ANNUALIZE_DAYS

    def _bs(c: OptionContract) -> float:
        return bs.price(spot, c.strike, T, c.iv, kind=c.kind)

    p_sp, p_bp = _bs(sell_put), _bs(buy_put)
    p_sc, p_bc = _bs(sell_call), _bs(buy_call)
    net_credit = (p_sp + p_sc) - (p_bp + p_bc)

    width_put = sell_put.strike - buy_put.strike
    width_call = buy_call.strike - sell_call.strike
    max_width = max(width_put, width_call)
    max_profit = net_credit * 100.0
    max_loss = (max_width - net_credit) * 100.0
    rr = net_credit / (max_width - net_credit) if (max_width - net_credit) > 0 else None
    be_lo = sell_put.strike - net_credit
    be_hi = sell_call.strike + net_credit

    legs = [
        CondorLeg("卖出", "P", sell_put.strike, sell_put.delta, sell_put.iv * 100, p_sp),
        CondorLeg("买入", "P", buy_put.strike, buy_put.delta, buy_put.iv * 100, p_bp),
        CondorLeg("卖出", "C", sell_call.strike, sell_call.delta, sell_call.iv * 100, p_sc),
        CondorLeg("买入", "C", buy_call.strike, buy_call.delta, buy_call.iv * 100, p_bc),
    ]

    # —— 适配度体检 ——
    fmt = _fmt_k(spot)
    reasons: list[str] = []
    caveats: list[str] = []
    score = 100

    reasons.append(f"波动率栏「偏卖方」✓ + 现价 {fmt(spot)} 被 {fmt(sell_put.strike)}P / "
                   f"{fmt(sell_call.strike)}C 双墙夹住 → 区间结构成立")

    avg_sell_delta = (abs(sell_put.delta) + abs(sell_call.delta)) / 2.0
    if avg_sell_delta <= TYPE_A_DELTA:
        condor_type = "1σ外A型"
        reasons.append(f"卖腿均 |delta| {avg_sell_delta:.2f}（≤{TYPE_A_DELTA}）→ 1σ外 A 型：区间宽、触及概率低、盈亏比好")
    elif avg_sell_delta <= TYPE_B_DELTA:
        condor_type = "贴墙B型"
        score -= 5
        reasons.append(f"卖腿均 |delta| {avg_sell_delta:.2f} → 贴墙 B 型：卖在厚墙上收更多权利金，"
                       f"赌墙的吸附力，代价是缓冲较薄（需墙足够厚才成立）")
    else:
        condor_type = "激进"
        score -= 20
        reasons.append(f"卖腿均 |delta| {avg_sell_delta:.2f}（>{TYPE_B_DELTA}）→ 激进：卖腿过贴价，"
                       f"极易被触及，盈亏比差")

    # 居中度
    mid = (sell_put.strike + sell_call.strike) / 2.0
    half = (sell_call.strike - sell_put.strike) / 2.0
    centering = abs(spot - mid) / half if half > 0 else None
    if centering is not None:
        near = "下墙" if spot < mid else "上墙"
        if centering > 0.6:
            score -= 15
            caveats.append(f"现价明显偏向{near}（居中度 {centering:.2f}）——{near}侧先受威胁，"
                           f"可考虑整体{'下' if near=='下墙' else '上'}移半档让现价更居中")
        elif centering > 0.35:
            score -= 8
            caveats.append(f"现价略偏{near}（居中度 {centering:.2f}），该侧缓冲较薄")
        else:
            reasons.append(f"现价接近区间中点（居中度 {centering:.2f}）→ 两侧较均衡")

    # 净收 / 翼宽
    credit_ratio = net_credit / max_width if max_width > 0 else 0.0
    if credit_ratio >= CREDIT_RATIO_MIN:
        reasons.append(f"理论净收 {net_credit:.2f}/翼宽 {fmt(max_width)} = {credit_ratio*100:.0f}%（≥1/3）→ 权利金对风险的补偿达标")
    else:
        score -= 15
        caveats.append(f"理论净收仅为翼宽的 {credit_ratio*100:.0f}%（<1/3）——权利金补偿偏薄，可收窄翼或外移卖腿")

    # 翼宽对称性（行权价稀疏时一侧翼会被迫拉宽，放大最大亏损）
    lo_w, hi_w = min(width_put, width_call), max(width_put, width_call)
    if lo_w > 0 and hi_w / lo_w > 1.5:
        score -= 10
        caveats.append(f"两侧翼宽不对称（put {fmt(width_put)} / call {fmt(width_call)}）——"
                       f"该品种行权价稀疏，较宽侧把最大亏损放大、盈亏比受损，难构造干净对称铁鹰")

    # skew 摩擦：买翼是否比卖身贵
    sell_iv = (sell_put.iv + sell_call.iv) / 2.0 * 100
    wing_iv = (buy_put.iv + buy_call.iv) / 2.0 * 100
    skew_cost = wing_iv - sell_iv
    if skew_cost >= SKEW_COST_SIG:
        score -= 10
        caveats.append(f"买翼 IV {wing_iv:.0f} 明显高于卖身 IV {sell_iv:.0f}（+{skew_cost:.0f}pp skew 摩擦）"
                       f"——买贵卖便宜，尾部保护偏贵，吃掉部分边际")
    else:
        reasons.append(f"买翼/卖身 IV 差 {skew_cost:+.0f}pp → skew 摩擦不显著")

    # 方向微调（来自 flow 当日方向盘）
    tilt = getattr(fa, "flow_tilt", "") if fa is not None else ""
    if tilt and not tilt.startswith("—"):
        if "空" in tilt:
            caveats.append("当日资金方向偏空——铁鹰宜整体下移半档（put 卖腿离现价远些）对冲下行威胁")
        elif "多" in tilt:
            caveats.append("当日资金方向偏多——铁鹰宜整体上移半档（call 卖腿离现价远些）对冲上行威胁")

    if not in_sweet:
        caveats.append(f"注意：所选到期距今 {dte} 天，不在 theta 甜区({SWEET_DTE_LO}~{SWEET_DTE_HI}天)——"
                       f"甜区内无足够流动性的到期，已退取最厚到期")

    caveats.append("以上为策略模块的确定性结构输出与体检，非交易指令；权利金为各腿 iv 反算的 BS "
                   "理论中值，实盘按 bid/ask 穿价会略低。是否开仓、用哪型、何时平由使用者判断。")

    score = max(0, min(100, score))
    if score >= 75:
        icon, quality = "✅", "适配"
    elif score >= 55:
        icon, quality = "⚠️", "勉强适配"
    else:
        icon, quality = "⚠️", "结构偏弱"
    headline = (f"{icon} 铁鹰{quality}（{condor_type}）：卖 {fmt(sell_put.strike)}P/{fmt(sell_call.strike)}C、"
                f"买 {fmt(buy_put.strike)}P/{fmt(buy_call.strike)}C，理论净收 ${max_profit:.0f}"
                f"，最大亏损 ${max_loss:.0f}"
                + (f"，盈亏比 {rr:.2f}:1" if rr else "")
                + f"（到期 {expiry.isoformat()}，{dte}天）")

    return CondorPlan(
        applicable=True, stance=stance, headline=headline,
        expiry=expiry, dte=dte, spot=spot, legs=legs,
        net_credit=net_credit, width_put=width_put, width_call=width_call,
        max_profit=max_profit, max_loss=max_loss, rr=rr, be_lo=be_lo, be_hi=be_hi,
        centering=centering, condor_type=condor_type, fit_score=score,
        reasons=reasons, caveats=caveats,
    )
