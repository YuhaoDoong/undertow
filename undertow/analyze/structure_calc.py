"""结构计算器（Codex 015 提交二）：给定结构，只算确定性的数 —— 不判断「适配/不开枪」，不替你选方向。

旧的 credit_spread.assess_credit_spread / condor.assess_condor 把三类东西混在一起：
  ① 预测性门槛：方向取自近端研判（未检验）、IV−RV ≥ 2 才「有溢价」（未检验）、波动率栏「偏卖方」才「适配」（未检验）、
     适配度评分与「⭐正是铁鹰适用场景」（未检验）；
  ② 构造规则：选到期（theta 甜区）、选卖腿（|Δ| 区间 + OI 门槛）、选保护翼 —— 这是「怎么搭一个结构」，不是预测；
  ③ 确定性计算：权利金、最大亏损、盈亏平衡、缓冲、费用。
这里只保留 ②③。①由 analyze/claims.py 登记为 T3，不再影响输出。旧函数保留在原模块供研究对照。

输出口径（写进每个结果的 notes）：
- 权利金给两种：BS 理论中值（各腿 IV 反算）与报价保守价（卖腿 bid − 买腿 ask）。报价缺一边 → 保守价「未知」，不折零。
  报价来自 CBOE 延迟快照（盘前 = 前一交易日收盘附近），不是现在可成交的价格。
- 最大亏损 = 到期静态值（宽度 − 净收 + 费用），**不是**所有提前被指派/提前平仓情景下的实际亏损上界；
  残腿与到期处置的未知项按影子账规则另论。
- 费用按张计（AGENTS.md）：每条腿开平各一次，用影子账同一口径。
非交易指令；做不做、做哪个方向，由你决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from undertow.analyze import blackscholes as bs
from undertow.analyze.condor import _ANNUALIZE_DAYS, _pick_expiry, _pick_sell_leg, _pick_wings
from undertow.analyze.credit_spread import _pick_sell_credit, _wing_credit

FEE_PER_LEG_ROUND_TRIP = 1.60      # 与影子账 fee_round_trip（两腿 $3.20）同一口径
NOTES = ("结构计算，非推荐：不含方向判断与适配评分（相关预测主张未通过验证，见 undertow claims）。",
         "BS 理论中值由各腿 IV 反算；保守价 = 卖腿 bid − 买腿 ask，来自 CBOE 延迟快照（约为前一交易日收盘），不是现在可成交的价格。",
         "最大亏损为到期静态值，不是提前指派或提前平仓情景下的实际亏损上界。")


@dataclass(frozen=True)
class CalcLeg:
    action: str      # 卖出 / 买入
    kind: str        # C / P
    strike: float
    delta: float
    iv_pp: float
    bs_price: float
    bid: float | None
    ask: float | None


@dataclass(frozen=True)
class StructureCalc:
    name: str
    computable: bool
    reason: str = ""                  # 不可计算时的具体原因（可行性，不是预测）
    expiry: date | None = None
    dte: int | None = None
    spot: float | None = None
    legs: list[CalcLeg] = field(default_factory=list)
    credit_bs: float | None = None          # 每股
    credit_conservative: float | None = None  # 每股；报价缺失 → None
    width: float | None = None
    fee: float = 0.0                        # 每组合美元
    max_loss_bs: float | None = None        # 每组合美元（静态到期）
    max_loss_conservative: float | None = None
    breakevens: list[float] = field(default_factory=list)
    buffer_pct: list[float] = field(default_factory=list)   # 现价到各盈亏平衡的距离 %
    iv_minus_rv: float | None = None        # 观测：只展示
    notes: tuple = NOTES


def _leg(action, c, T, spot) -> CalcLeg:
    return CalcLeg(action, c.kind, c.strike, c.delta, c.iv * 100, bs.price(spot, c.strike, T, c.iv, kind=c.kind),
                   c.bid if c.bid > 0 else None, c.ask if c.ask > 0 else None)


def _conservative(sells, buys):
    if any(l.bid is None for l in sells) or any(l.ask is None for l in buys):
        return None
    return sum(l.bid for l in sells) - sum(l.ask for l in buys)


def credit_spread_calc(snap, today: date, side: str, *, iv_minus_rv=None) -> StructureCalc:
    """单侧信用价差：side="P" 卖 put 价差（牛市看跌价差），"C" 卖 call 价差（熊市看涨价差）。方向由用户决定。"""
    name = "卖 put 价差（牛市看跌价差）" if side == "P" else "卖 call 价差（熊市看涨价差）"
    spot = snap.spot
    expiry, dte, _ = _pick_expiry(snap, today)
    if expiry is None:
        return StructureCalc(name, False, "期权链无满足流动性/到期条件的合约", spot=spot, iv_minus_rv=iv_minus_rv)
    cands = [c for c in snap.contracts if c.expiry == expiry and c.open_interest > 0 and c.kind == side]
    sell = _pick_sell_credit(cands, spot, side)
    if sell is None:
        return StructureCalc(name, False, f"{expiry} 到期无 |Δ| 在区间内且 OI 达标的卖腿", expiry=expiry, dte=dte,
                             spot=spot, iv_minus_rv=iv_minus_rv)
    buy = _wing_credit(cands, sell, side, spot)
    if buy is None:
        return StructureCalc(name, False, "卖腿外侧无 OI 达标的保护腿，无法封顶亏损", expiry=expiry, dte=dte,
                             spot=spot, iv_minus_rv=iv_minus_rv)
    T = dte / _ANNUALIZE_DAYS
    ls, lb = _leg("卖出", sell, T, spot), _leg("买入", buy, T, spot)
    width = abs(buy.strike - sell.strike)
    fee = 2 * FEE_PER_LEG_ROUND_TRIP
    cb = ls.bs_price - lb.bs_price
    cc = _conservative([ls], [lb])
    be = sell.strike + cb if side == "C" else sell.strike - cb
    buf = 100.0 * ((be - spot) / spot if side == "C" else (spot - be) / spot)
    return StructureCalc(name, True, expiry=expiry, dte=dte, spot=spot, legs=[ls, lb], credit_bs=cb,
                         credit_conservative=cc, width=width, fee=fee,
                         max_loss_bs=(width - cb) * 100 + fee,
                         max_loss_conservative=None if cc is None else (width - cc) * 100 + fee,
                         breakevens=[be], buffer_pct=[buf], iv_minus_rv=iv_minus_rv)


def condor_calc(snap, today: date, *, iv_minus_rv=None) -> StructureCalc:
    """铁鹰：按「现价上下 OI 最大的卖腿 + 对称保护翼」的构造规则搭一个结构并计算。不判断是否「适用」。"""
    name = "铁鹰（双侧卖价差）"
    spot = snap.spot
    expiry, dte, _ = _pick_expiry(snap, today)
    if expiry is None:
        return StructureCalc(name, False, "期权链无满足流动性/到期条件的合约", spot=spot, iv_minus_rv=iv_minus_rv)
    ex = [c for c in snap.contracts if c.expiry == expiry and c.open_interest > 0]
    puts, calls = [c for c in ex if c.kind == "P"], [c for c in ex if c.kind == "C"]
    sp, sc = _pick_sell_leg(puts, spot, "P"), _pick_sell_leg(calls, spot, "C")
    if sp is None or sc is None:
        return StructureCalc(name, False, "按此构造规则找不到两侧卖腿（|Δ| 区间 + OI 门槛）", expiry=expiry, dte=dte,
                             spot=spot, iv_minus_rv=iv_minus_rv)
    bp, bc = _pick_wings(puts, calls, sp, sc)
    if bp is None or bc is None:
        return StructureCalc(name, False, "卖腿外侧无 OI 达标的保护翼", expiry=expiry, dte=dte, spot=spot,
                             iv_minus_rv=iv_minus_rv)
    T = dte / _ANNUALIZE_DAYS
    legs = [_leg("卖出", sp, T, spot), _leg("买入", bp, T, spot), _leg("卖出", sc, T, spot), _leg("买入", bc, T, spot)]
    width = max(sp.strike - bp.strike, bc.strike - sc.strike)
    fee = 4 * FEE_PER_LEG_ROUND_TRIP
    cb = legs[0].bs_price + legs[2].bs_price - legs[1].bs_price - legs[3].bs_price
    cc = _conservative([legs[0], legs[2]], [legs[1], legs[3]])
    lo, hi = sp.strike - cb, sc.strike + cb
    return StructureCalc(name, True, expiry=expiry, dte=dte, spot=spot, legs=legs, credit_bs=cb,
                         credit_conservative=cc, width=width, fee=fee,
                         max_loss_bs=(width - cb) * 100 + fee,
                         max_loss_conservative=None if cc is None else (width - cc) * 100 + fee,
                         breakevens=[lo, hi], buffer_pct=[100.0 * (spot - lo) / spot, 100.0 * (hi - spot) / spot],
                         iv_minus_rv=iv_minus_rv)
