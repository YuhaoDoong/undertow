"""结构计算器（Codex 015 提交二；016 修正报价质量与费用口径）。

旧的 credit_spread.assess_credit_spread / condor.assess_condor 把三类东西混在一起：
  ① 预测性门槛：方向取自近端研判、IV−RV ≥ 2 才「有溢价」、波动率栏「偏卖方」才「适配」、适配度评分 —— 都未检验；
  ② 构造规则：选到期（theta 甜区 OI 最厚）、选卖腿（|Δ| 区间 + OI 门槛）、选保护翼 —— 这是一条【研究性的选腿启发式】，
     不是用户给定的行权价，也未经盈利验证；「按此规则找不到候选」只说明这条规则找不到，不是市场上没有合法结构；
  ③ 计算：权利金、最大亏损、盈亏平衡、缓冲、费用。
这里只保留 ②③，①由 analyze/claims.py 登记为 T3。

两种情景分开给，各自有状态（Codex 016 F16-01）：
- 模型情景：各腿 IV 反算的 BS 理论价。可算 = 所需 IV/行权价有限且净收落在 (0, 宽度) 内。
- 报价情景：卖腿 bid − 买腿 ask（CBOE 延迟快照，约为前一交易日收盘，不是现在可成交的价）。
  逐腿检查：缺值（0 或空）→ missing；非有限 → non_finite；bid > ask → crossed；净收 ≤ 0 或 ≥ 宽度 → out_of_bounds。
  任一不满足 → 报价情景整体「未知」并给原因，不输出由坏报价推出的风险数字，也不裁剪成貌似合理的数。
费用口径（Codex 016 F16-03）：
- 费用按张计，每条腿开平各一次，取影子账同一定义（shadow.CONFIG["fee_round_trip"] 为两腿往返）；只读不改冻结配置。
- 最大亏损 = 到期静态值（宽度 − 净收）×100 + 费用；不是提前指派、提前平仓情景下的实际亏损上界。
- 盈亏平衡 = 含费：代入同一静态到期收益公式净损益为 0；另给税费前数值，明确标注。含费净收 ≤ 0 → 无盈亏平衡（不存在）。
- 缓冲与所示盈亏平衡同源（含费）。
非交易指令；做不做、做哪个方向，由你决定。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

from undertow.analyze import blackscholes as bs
from undertow.analyze.condor import _ANNUALIZE_DAYS, _pick_expiry, _pick_sell_leg, _pick_wings
from undertow.analyze.credit_spread import _pick_sell_credit, _wing_credit
from undertow.analyze.shadow import CONFIG as _SHADOW_CONFIG

FEE_PER_LEG_ROUND_TRIP = _SHADOW_CONFIG["fee_round_trip"] / 2      # 单一费用定义：影子账两腿往返 ÷ 2
RULE = ("选腿规则（研究性启发式，未经盈利验证）：到期取 theta 甜区内 OI 最厚者；卖腿取 |Δ| 在区间内且 OI 达标者；"
        "保护腿按目标宽度选。不是你给定的行权价；「找不到」只指此规则找不到。")
NOTES = ("结构计算，非推荐：不含方向判断与适配评分（相关预测主张未通过验证，见 undertow claims）。",
         RULE,
         "模型 = 各腿 IV 反算的 BS 理论价；报价 = 卖腿 bid − 买腿 ask，来自 CBOE 延迟快照（约为前一交易日收盘），不是现在可成交的价格。",
         "最大亏损为到期静态值（含费），不是提前指派或提前平仓情景下的实际亏损上界；盈亏平衡与缓冲均为含费口径。")


@dataclass(frozen=True)
class CalcLeg:
    action: str      # 卖出 / 买入
    kind: str        # C / P
    strike: float
    delta: float
    iv_pp: float
    bs_price: float | None
    bid: float | None
    ask: float | None
    quote_issue: str = ""     # "" / missing / non_finite / crossed


@dataclass(frozen=True)
class Scenario:
    """一种计价情景（模型或报价）。status != "ok" 时数值字段全为 None。"""
    status: str                         # ok / missing / non_finite / crossed / out_of_bounds / no_breakeven
    reason: str = ""
    credit: float | None = None         # 每股，税费前
    max_loss: float | None = None       # 每组合美元，含费，到期静态
    breakevens: list[float] = field(default_factory=list)          # 含费
    breakevens_pre_fee: list[float] = field(default_factory=list)  # 税费前（仅供对照）
    buffer_pct: list[float] = field(default_factory=list)          # 现价到含费盈亏平衡的距离 %


@dataclass(frozen=True)
class StructureCalc:
    name: str
    computable: bool                    # 按选腿规则搭得出结构（与计价是否有效分开）
    reason: str = ""
    expiry: date | None = None
    dte: int | None = None
    spot: float | None = None
    legs: list[CalcLeg] = field(default_factory=list)
    width: float | None = None
    fee: float = 0.0                    # 每组合美元
    model: Scenario | None = None
    quote: Scenario | None = None
    iv_minus_rv: float | None = None    # 观测：只展示
    notes: tuple = NOTES


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _leg(action, c, T, spot) -> CalcLeg:
    bid, ask = c.bid, c.ask
    if not (_finite(bid) and _finite(ask)):
        issue, bid, ask = "non_finite", None, None
    elif bid <= 0 and ask <= 0:
        issue, bid, ask = "missing", None, None
    elif bid < 0 or ask <= 0:
        issue, bid, ask = "missing", (bid if bid > 0 else None), (ask if ask > 0 else None)
    elif bid > ask:
        issue = "crossed"
    else:
        issue = ""
    px = None
    if _finite(c.iv) and c.iv > 0 and _finite(c.strike) and _finite(spot) and T > 0:
        v = bs.price(spot, c.strike, T, c.iv, kind=c.kind)
        px = v if _finite(v) else None
    return CalcLeg(action, c.kind, c.strike, c.delta, (c.iv * 100) if _finite(c.iv) else float("nan"),
                   px, bid, ask, issue)


_ISSUE_CN = {"missing": "有腿缺报价", "non_finite": "有腿报价非有限值", "crossed": "有腿报价倒挂（bid > ask）"}


def _scenario(credit, width, fee, be_fn, *, status_if_bad="out_of_bounds") -> Scenario:
    """credit：每股税费前净收。be_fn(net_credit_per_share) → 盈亏平衡价列表。"""
    if credit is None or not _finite(credit):
        return Scenario("missing", "净收无法计算")
    if not (0 < credit < width):
        return Scenario(status_if_bad, f"净收 {credit:.2f} 不在 (0, 宽度 {width:g}) 内：结构界限冲突，不给风险数字")
    net = credit - fee / 100.0
    pre = be_fn(credit)
    if net <= 0:
        return Scenario("no_breakeven", f"含费净收 {net:.2f} ≤ 0：到期无盈利价位，无盈亏平衡",
                        credit=credit, max_loss=(width - credit) * 100 + fee, breakevens_pre_fee=pre)
    return Scenario("ok", credit=credit, max_loss=(width - credit) * 100 + fee, breakevens=be_fn(net),
                    breakevens_pre_fee=pre)          # buffer_pct 由调用方按危险方向计算（与含费盈亏平衡同源）


def _quote_scenario(sells, buys, width, fee, be_fn) -> Scenario:
    bad = [l for l in sells + buys if l.quote_issue]
    if bad:
        kinds = sorted({l.quote_issue for l in bad})
        return Scenario(kinds[0] if len(kinds) == 1 else "missing",
                        "；".join(f"{_ISSUE_CN[k]}（{','.join(f'{l.strike:g}{l.kind}' for l in bad if l.quote_issue == k)}）"
                                 for k in kinds))
    credit = sum(l.bid for l in sells) - sum(l.ask for l in buys)
    return _scenario(credit, width, fee, be_fn)


def _signed_buffers(spot, bes, sides):
    """sides: 每个盈亏平衡对应的「危险方向」 ("down" 表示价格跌破即亏)。缓冲 = 现价到平衡点的距离 %，已越过为负。"""
    out = []
    for b, sd in zip(bes, sides):
        out.append(100.0 * ((spot - b) if sd == "down" else (b - spot)) / spot)
    return out


def credit_spread_calc(snap, today: date, side: str, *, iv_minus_rv=None) -> StructureCalc:
    """单侧信用价差：side="P" 卖 put 价差（牛市看跌价差），"C" 卖 call 价差（熊市看涨价差）。方向由用户决定。"""
    name = "卖 put 价差（牛市看跌价差）" if side == "P" else "卖 call 价差（熊市看涨价差）"
    spot = snap.spot
    expiry, dte, _ = _pick_expiry(snap, today)
    if expiry is None:
        return StructureCalc(name, False, "按选腿规则找不到满足流动性/到期条件的合约", spot=spot, iv_minus_rv=iv_minus_rv)
    cands = [c for c in snap.contracts if c.expiry == expiry and c.open_interest > 0 and c.kind == side]
    sell = _pick_sell_credit(cands, spot, side)
    if sell is None:
        return StructureCalc(name, False, f"按选腿规则在 {expiry} 到期找不到 |Δ| 在区间内且 OI 达标的卖腿", expiry=expiry,
                             dte=dte, spot=spot, iv_minus_rv=iv_minus_rv)
    buy = _wing_credit(cands, sell, side, spot)
    if buy is None:
        return StructureCalc(name, False, "按选腿规则在卖腿外侧找不到 OI 达标的保护腿", expiry=expiry, dte=dte,
                             spot=spot, iv_minus_rv=iv_minus_rv)
    T = dte / _ANNUALIZE_DAYS
    ls, lb = _leg("卖出", sell, T, spot), _leg("买入", buy, T, spot)
    width = abs(buy.strike - sell.strike)
    fee = 2 * FEE_PER_LEG_ROUND_TRIP
    sd = "up" if side == "C" else "down"
    be_fn = (lambda c: [sell.strike + c]) if side == "C" else (lambda c: [sell.strike - c])

    def fix(sc):
        return sc if sc.status != "ok" else replace(sc, buffer_pct=_signed_buffers(spot, sc.breakevens, [sd]))
    model = fix(_scenario(None if (ls.bs_price is None or lb.bs_price is None) else ls.bs_price - lb.bs_price,
                          width, fee, be_fn))
    quote = fix(_quote_scenario([ls], [lb], width, fee, be_fn))
    return StructureCalc(name, True, expiry=expiry, dte=dte, spot=spot, legs=[ls, lb], width=width, fee=fee,
                         model=model, quote=quote, iv_minus_rv=iv_minus_rv)


def condor_calc(snap, today: date, *, iv_minus_rv=None) -> StructureCalc:
    """铁鹰：按「现价上下 OI 最大的卖腿 + 对称保护翼」的选腿规则搭一个结构并计算。不判断是否「适用」。"""
    name = "铁鹰（双侧卖价差）"
    spot = snap.spot
    expiry, dte, _ = _pick_expiry(snap, today)
    if expiry is None:
        return StructureCalc(name, False, "按选腿规则找不到满足流动性/到期条件的合约", spot=spot, iv_minus_rv=iv_minus_rv)
    ex = [c for c in snap.contracts if c.expiry == expiry and c.open_interest > 0]
    puts, calls = [c for c in ex if c.kind == "P"], [c for c in ex if c.kind == "C"]
    sp, sc = _pick_sell_leg(puts, spot, "P"), _pick_sell_leg(calls, spot, "C")
    if sp is None or sc is None:
        return StructureCalc(name, False, "按选腿规则找不到两侧卖腿（|Δ| 区间 + OI 门槛）", expiry=expiry, dte=dte,
                             spot=spot, iv_minus_rv=iv_minus_rv)
    bp, bc = _pick_wings(puts, calls, sp, sc)
    if bp is None or bc is None:
        return StructureCalc(name, False, "按选腿规则在卖腿外侧找不到 OI 达标的保护翼", expiry=expiry, dte=dte,
                             spot=spot, iv_minus_rv=iv_minus_rv)
    T = dte / _ANNUALIZE_DAYS
    legs = [_leg("卖出", sp, T, spot), _leg("买入", bp, T, spot), _leg("卖出", sc, T, spot), _leg("买入", bc, T, spot)]
    width = max(sp.strike - bp.strike, bc.strike - sc.strike)
    fee = 4 * FEE_PER_LEG_ROUND_TRIP
    be_fn = lambda c: [sp.strike - c, sc.strike + c]

    def fix(s_):
        return s_ if s_.status != "ok" else replace(s_, buffer_pct=_signed_buffers(spot, s_.breakevens, ["down", "up"]))
    bsp = [l.bs_price for l in legs]
    model = fix(_scenario(None if any(v is None for v in bsp) else bsp[0] + bsp[2] - bsp[1] - bsp[3],
                          width, fee, be_fn))
    quote = fix(_quote_scenario([legs[0], legs[2]], [legs[1], legs[3]], width, fee, be_fn))
    return StructureCalc(name, True, expiry=expiry, dte=dte, spot=spot, legs=legs, width=width, fee=fee,
                         model=model, quote=quote, iv_minus_rv=iv_minus_rv)


def expiry_pnl(calc: StructureCalc, price: float, credit: float) -> float:
    """到期静态净损益（每组合美元，含费）：用来验证「含费盈亏平衡代回为 0」。credit 为每股税费前净收。"""
    intrinsic = 0.0
    for l in calc.legs:
        v = max(0.0, (l.strike - price) if l.kind == "P" else (price - l.strike))
        intrinsic += v if l.action == "卖出" else -v
    return (credit - intrinsic) * 100 - calc.fee
