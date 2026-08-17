"""方向性信用价差子模块（顺方向卖方价差）：偏空→熊市看涨价差、偏多→牛市看跌价差。

与铁鹰（condor.py）的分工——都卖 vega，区别在有没有方向：
    condor.py        无方向观点 → 中性区间、双侧卖，怕单边大行情
    credit_spread.py 有明确方向 + 期权偏贵 → 顺方向【单侧】卖，买保护腿封顶最大损失

为什么需要它（2026-08-13 沪银卖 Call 价差场景、裸方向空手旁观的复盘）：
  在"明确看空 + 正Gamma + 高 IV 溢价 + 现价还没跌破零伽马"这个场景，
    · 裸方向做空（strategy.py 墙前拒绝）被【正Gamma易被磨 + 现价在零伽马上方】两票否决、一直等打回；
    · 中性铁鹰（condor.py）没利用方向、勉强适配。
  而顺方向的卖 Call 价差恰恰把这两个"否决项"翻成"入场理由"——正Gamma 横盘=时间价值稳定衰减，
  是卖方的朋友；只需价格"不大涨"而非"下跌"，不必等拒绝形态确认。

前置（对应波动率栏"买方 vs 卖方"）：
    * 方向来自 outlook（偏空/偏多；分歧/中性/观望→不适配）。
    * 期权必须相对【实际波动】偏贵 —— 用 IV−RV ≥ 阈值（vr.iv_minus_rv），
      **不用 volregime 的 stance**。因为低 IV 历史分位会把"中性"盖过高 IV−RV
      （silver 8/13：IV−RV +8.4pp 但分位仅 38%→stance 判"中性"），
      而卖方吃的是"相对实际波动的溢价"，不看历史分位——这正是被中和掉的机会。

结构复用 condor 的选到期/选卖腿/选翼/BS 权利金，只做一侧。行权价 ETF 口径（与下单一致）。
诚实边界：策略模块的确定性结构输出与体检，非交易指令；权利金为各腿 iv 反算 BS 中值，
实盘按 bid/ask 穿价略低。信用价差盈亏比天然低（靠胜率+时间价值，非靠盈亏比）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from undertow.analyze import blackscholes as bs
from undertow.analyze.condor import (
    _pick_expiry, SELL_DELTA_MIN, SELL_DELTA_MAX, MIN_LEG_OI, _ANNUALIZE_DAYS,
)
from undertow.core.models import OptionsSnapshot, OptionContract

# —— 前置：期权相对实际波动的溢价（卖方是否有正期望的核心门槛）——
CREDIT_IV_MIN = 2.0      # IV−RV ≥ 此(pp) → 期权贵于近期实际波动，卖方有溢价
CREDIT_IV_STRONG = 5.0   # ≥ 此 → 溢价充沛（强卖方环境）
# —— 保护翼：目标翼宽 ≈ 现价的此比例（封顶亏损，别太宽塌盈亏比）——
TARGET_WING_FRAC = 0.05
# —— 缓冲：现价到盈亏平衡的距离（越大越安全）——
BUFFER_THIN = 1.0        # 缓冲 < 此(%) → 太薄、易被击穿


@dataclass(frozen=True)
class SpreadLeg:
    action: str            # 卖出 / 买入
    kind: str              # C / P
    strike: float
    delta: float
    iv_pp: float
    bs_price: float


@dataclass(frozen=True)
class CreditSpreadPlan:
    """方向性信用价差结构 + 盈亏 + 适配体检。金额：每股权利金；$ 为每 1 组合(×100)。"""
    applicable: bool
    direction: str                   # 做空 / 做多 / 观望
    spread_name: str
    headline: str
    stance: str = ""
    iv_minus_rv: float | None = None
    expiry: date | None = None
    dte: int | None = None
    spot: float | None = None
    legs: list[SpreadLeg] = field(default_factory=list)   # [卖腿, 买保护腿]
    net_credit: float | None = None
    width: float | None = None
    max_profit: float | None = None
    max_loss: float | None = None
    rr: float | None = None
    breakeven: float | None = None
    buffer_pct: float | None = None
    fit_score: int = 0
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


def _fmt_k(spot: float):
    return (lambda v: f"{v:,.0f}") if spot >= 500 else (lambda v: f"{v:,.1f}")


def _pick_sell_credit(cands: list[OptionContract], spot: float, side: str) -> OptionContract | None:
    """信用价差卖腿：现价上(call)/下(put)方、|delta|∈[MIN,MAX]、OI 达标里【最贴价】
    （|delta| 最高）的一档 —— 顺方向卖方要积极收权利金，选最贴价而非 OI 最大的远虚墙
    （远虚墙权利金太薄、盈亏比塌到 0.1 以下，失去卖方意义）。"""
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
    return max(pool, key=lambda c: abs(c.delta))   # 最贴价 = |delta| 最高


def _wing_credit(cands: list[OptionContract], sell: OptionContract, side: str,
                 spot: float) -> OptionContract | None:
    """保护翼：卖腿外侧、比卖腿更虚、OI 达标里【翼宽最接近目标(现价×TARGET_WING_FRAC)】的一档。
    信用价差的翼只为封顶亏损，不必极虚——近一点、窄一点反而控制最大亏损、保住盈亏比。"""
    pool = []
    for c in cands:
        if c.open_interest < MIN_LEG_OI:
            continue
        if side == "C" and c.strike <= sell.strike:
            continue
        if side == "P" and c.strike >= sell.strike:
            continue
        if abs(c.delta) >= abs(sell.delta):   # 翼要比卖腿虚
            continue
        pool.append(c)
    if not pool:
        return None
    target = spot * TARGET_WING_FRAC
    return min(pool, key=lambda c: abs(abs(c.strike - sell.strike) - target))


def _na(direction: str, spread_name: str, headline: str, reasons: list[str],
        *, stance: str = "", iv_mr: float | None = None) -> CreditSpreadPlan:
    return CreditSpreadPlan(
        applicable=False, direction=direction, spread_name=spread_name, headline=headline,
        stance=stance, iv_minus_rv=iv_mr, reasons=reasons,
        caveats=["信用价差是顺方向卖方结构，只在【方向明确 + 期权相对实际波动偏贵】时占优；"
                 "以上为策略模块的确定性判断，非交易指令。"])


def assess_credit_spread(*, snap: OptionsSnapshot, vr, outlook, today: date,
                         fa=None) -> CreditSpreadPlan:
    """方向（outlook）+ 期权偏贵（vr.IV−RV）→ 顺方向单侧信用价差结构映射与适配度。

    snap: 当前期权链快照（ETF 口径）。vr: volregime.VolRegime。outlook: analyze.Outlook。
    """
    # 方向跟【近端】(墙位/资金流·战术周期)——信用价差是战术卖方结构，跟短线结构而非
    # 中期持仓面（作者卖沪银 Call 跟的是短线空、非其"中期看涨"）；缺省回退综合 bias。
    bias = getattr(outlook, "near_bias", "") or getattr(outlook, "bias", "") or ""
    conf = getattr(outlook, "confidence", "") or ""
    regime = getattr(outlook, "regime", "") or ""
    split_note = ("（近中分歧：中期持仓面为 "
                  f"{getattr(outlook, 'mid_bias', '')}，本结构只跟近端战术周期）"
                  if getattr(outlook, "horizon_split", False) else "")
    stance = getattr(vr, "stance", "数据不足")
    iv_mr = getattr(vr, "iv_minus_rv", None)
    spot = snap.spot
    fmt = _fmt_k(spot)

    # —— 门槛 1：近端方向必须明确（分歧/中性/观望不做单侧价差）——
    directional = ("空" in bias or "多" in bias) and "分歧" not in bias
    if not directional:
        return _na("观望", "", f"近端研判为「{bias or '中性'}」，无明确方向——单侧信用价差不适配"
                   f"（分歧/中性宜用中性铁鹰或不动）。", [f"近端方向门槛未满足：near_bias=「{bias or '中性'}」。"],
                   stance=stance, iv_mr=iv_mr)
    short_side = "空" in bias
    direction = "做空" if short_side else "做多"
    side = "C" if short_side else "P"
    spread_name = "熊市看涨价差（卖 Call 压制）" if short_side else "牛市看跌价差（卖 Put 承接）"

    # —— 门槛 2：期权相对实际波动偏贵（卖方溢价）——用 IV−RV，不用被分位中和的 stance ——
    if iv_mr is None:
        return _na(direction, spread_name, "缺 IV−RV 数据（无波动率栏），无法判卖方溢价。",
                   ["波动率栏未提供 ATM IV−RV。"], stance=stance)
    if iv_mr < CREDIT_IV_MIN:
        return _na(direction, spread_name,
                   f"期权未相对实际波动偏贵（IV−RV {iv_mr:+.1f}pp < {CREDIT_IV_MIN:.0f}）——"
                   f"卖方无溢价，别在便宜时卖。",
                   [f"卖方溢价门槛未满足：IV−RV {iv_mr:+.1f}pp。"
                    f"（注：这里看 IV−RV 而非波动率栏 stance「{stance}」——低 IV 分位会把"
                    f"高溢价盖成'中性'，但卖方吃的是相对实际波动的贵，不看历史分位。）"],
                   stance=stance, iv_mr=iv_mr)

    # —— 门槛 3：选到期（theta 甜区）+ 顺方向卖腿 + 保护翼 ——
    expiry, dte, in_sweet = _pick_expiry(snap, today)
    if expiry is None:
        return _na(direction, spread_name, "无足够流动性的到期可构造价差。",
                   ["期权链无满足流动性/到期条件的合约。"], stance=stance, iv_mr=iv_mr)
    cands = [c for c in snap.contracts if c.expiry == expiry and c.open_interest > 0 and c.kind == side]
    sell = _pick_sell_credit(cands, spot, side)
    if sell is None:
        where = "上方 call" if short_side else "下方 put"
        return _na(direction, spread_name, f"现价{where}无符合条件的卖腿墙，无法构造价差。",
                   [f"现价{where}缺 |delta|∈区间且 OI 达标的卖腿。"], stance=stance, iv_mr=iv_mr)
    buy = _wing_credit(cands, sell, side, spot)
    if buy is None:
        return _na(direction, spread_name, "卖腿外侧无合适保护翼，无法封顶最大损失。",
                   ["卖腿更外侧缺满足流动性的保护翼行权价。"], stance=stance, iv_mr=iv_mr)

    # —— 权利金 / 盈亏（BS 反算）——
    T = dte / _ANNUALIZE_DAYS

    def _bs(c):
        return bs.price(spot, c.strike, T, c.iv, kind=c.kind)

    p_sell, p_buy = _bs(sell), _bs(buy)
    net_credit = p_sell - p_buy
    width = abs(buy.strike - sell.strike)
    max_profit = net_credit * 100.0
    max_loss = (width - net_credit) * 100.0
    rr = net_credit / (width - net_credit) if (width - net_credit) > 0 else None
    breakeven = sell.strike + net_credit if short_side else sell.strike - net_credit
    buffer_pct = (100.0 * (breakeven - spot) / spot) if short_side else (100.0 * (spot - breakeven) / spot)

    legs = [
        SpreadLeg("卖出", side, sell.strike, sell.delta, sell.iv * 100, p_sell),
        SpreadLeg("买入", side, buy.strike, buy.delta, buy.iv * 100, p_buy),
    ]

    # —— 适配体检 ——
    reasons: list[str] = []
    caveats: list[str] = []
    score = 100

    reasons.append(f"方向来自近端研判「{bias}」（战术周期·墙位/资金流）→ {direction}，做{spread_name}"
                   + split_note)
    strong = iv_mr >= CREDIT_IV_STRONG
    reasons.append(f"波动率栏 IV−RV {iv_mr:+.1f}pp（期权贵于近期实际波动{'、溢价充沛' if strong else ''}）"
                   f"→ 卖方有正溢价，这是卖 vega 的入场理由")
    if not strong:
        score -= 5

    # 正Gamma 对卖方是利好（与裸方向做空相反）——本模块存在的核心理由
    if "正Gamma" in regime or "正伽马" in regime:
        reasons.append("正Gamma 环境（横盘钉住）→ 时间价值稳定衰减，对卖方是【利好】，"
                       "而非裸方向做空的否决项——只需价格不大涨/不大跌，不必等拒绝形态确认")
    elif "负Gamma" in regime or "负伽马" in regime:
        score -= 10
        caveats.append("负Gamma 环境：单边行情被对冲放大、卖腿被击穿风险上升——降规模或收窄翼")

    if "低" in conf:
        score -= 15
        caveats.append(f"方向可信度{conf}——方向本身不够硬，单侧价差承担方向风险，宜减仓或等可信度抬升")

    # 卖腿贴价度（delta 越高越贴价、越易被触及）
    sd = abs(sell.delta)
    if sd >= 0.35:
        score -= 8
        caveats.append(f"卖腿 |delta| {sd:.2f} 偏贴价——收权利金多但缓冲薄，需方向兑现得快")
    else:
        reasons.append(f"卖腿 {fmt(sell.strike)}{side} |delta| {sd:.2f}（现价{'上' if short_side else '下'}方虚值墙）"
                       f"→ 收权利金且留缓冲")

    # 缓冲
    if buffer_pct < BUFFER_THIN:
        score -= 10
        caveats.append(f"现价到盈亏平衡仅 {buffer_pct:+.1f}%——缓冲太薄，方向稍不配合即破位")
    else:
        move = "涨破" if short_side else "跌破"
        reasons.append(f"盈亏平衡 {fmt(breakeven)}、缓冲 {buffer_pct:+.1f}% → 到期只要不{move}即赢（横盘/回调都赚）")

    if rr is not None:
        reasons.append(f"盈亏比 {rr:.2f}:1（信用价差天然低——靠胜率+时间价值盈利、最大损失已封顶，非靠盈亏比）")

    if not in_sweet:
        caveats.append(f"所选到期距今 {dte} 天，不在 theta 甜区——甜区内无足够流动性，已退取最厚到期")

    caveats.append("以上为策略模块的确定性结构输出与体检，非交易指令；权利金为各腿 iv 反算 BS 中值，"
                   "实盘穿价略低。是否开仓、规模、何时平由使用者判断。")

    score = max(0, min(100, score))
    if score >= 75:
        icon, quality = "✅", "适配"
    elif score >= 55:
        icon, quality = "⚠️", "勉强适配"
    else:
        icon, quality = "⚠️", "偏弱"
    headline = (f"{icon} {spread_name} {quality}：卖 {fmt(sell.strike)}{side}/买 {fmt(buy.strike)}{side}，"
                f"理论净收 ${max_profit:.0f}、最大亏损 ${max_loss:.0f}"
                + (f"、盈亏比 {rr:.2f}:1" if rr else "")
                + f"、缓冲 {buffer_pct:+.1f}%（到期 {expiry.isoformat()}，{dte}天）")

    return CreditSpreadPlan(
        applicable=True, direction=direction, spread_name=spread_name, headline=headline,
        stance=stance, iv_minus_rv=iv_mr, expiry=expiry, dte=dte, spot=spot, legs=legs,
        net_credit=net_credit, width=width, max_profit=max_profit, max_loss=max_loss,
        rr=rr, breakeven=breakeven, buffer_pct=buffer_pct, fit_score=score,
        reasons=reasons, caveats=caveats,
    )
