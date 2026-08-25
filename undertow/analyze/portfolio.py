"""实盘持仓理论评价（纯确定性计算，无 I/O、无网络）。

吃两样东西：
  1) 券商持仓（collect/longbridge_account.RawPosition，或任何等价结构）；
  2) 每个标的的 undertow 研判上下文（InstrumentContext：现价 + Gamma 墙 + 近/中研判
     + 当日决策 + 期权链 delta/iv 查询）。

产出每笔持仓的评价（顺势/逆势、行权价 vs 墙、到期/被行权风险、盈亏、净 Delta），
再把同标的同到期的腿识别成价差结构，最后合成组合级总评。

**立场**：只作**波段级风险情景复盘**，非投资建议、非交易指令；方向与数字全部来自
上游确定性模块，本模块只做规则化合成。LLM 不碰算术。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

from undertow.analyze import blackscholes as bs

# 期权合约乘数（美式股票期权：1 张 = 100 股）
CONTRACT_MULT = 100
# 到期风险窗口（自然日）：DTE ≤ 此且贴近/价内 → 被行权/到期风险高
NEAR_EXPIRY_DTE = 7
# 贴价阈值：|现价−行权|/现价 ≤ 此算“贴近行权价”
NEAR_STRIKE_PCT = 0.03
# 无风险利率（与 blackscholes 默认一致，估值/兜底 delta 用）
RISK_FREE = 0.04

_OCC_RE = re.compile(r"^([A-Za-z]+)(\d{6})([CP])(\d+)(?:\.[A-Za-z]+)?$")


@dataclass(frozen=True)
class ParsedSymbol:
    underlying: str          # 标的根，如 SLV
    kind: str                # "C" / "P" / "STOCK"
    strike: float | None     # 期权行权价；股票为 None
    expiry: date | None      # 期权到期；股票为 None

    @property
    def is_option(self) -> bool:
        return self.kind in ("C", "P")


def parse_symbol(symbol: str) -> ParsedSymbol:
    """解析长桥格式代码。

    期权 `SLV260826P61000.US` → 根 SLV / 到期 2026-08-26 / P / 行权 61.0
    （⚠️ 长桥行权价=价×1000 但**不补零**，见 cheche 踩坑记录）。
    非期权（无 6 位日期+CP 结构）当作正股，只留根与 STOCK。
    """
    core = symbol.strip()
    m = _OCC_RE.match(core)
    if not m:
        root = core.split(".")[0]
        return ParsedSymbol(underlying=root.upper(), kind="STOCK", strike=None, expiry=None)
    root, ymd, cp, strike_raw = m.groups()
    try:
        exp = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return ParsedSymbol(underlying=root.upper(), kind="STOCK", strike=None, expiry=None)
    return ParsedSymbol(underlying=root.upper(), kind=cp.upper(),
                        strike=int(strike_raw) / 1000.0, expiry=exp)


# ————————————————————————————————————————————————————————— 上下文


@dataclass(frozen=True)
class InstrumentContext:
    """某标的（以 ETF 口径，如 SLV）的 undertow 研判上下文，喂给评价引擎。

    全部数字由上游确定性模块算好；本模块只读不算。`greeks(kind, strike, expiry)`
    返回 (delta_per_share, iv)——优先取期权链真实值，链上没有则回退 BS。
    """
    etf_symbol: str                     # SLV / GLD / ...
    display_name: str                   # 白银 Silver (COMEX)
    spot: float                         # ETF 现价
    call_wall: float                    # ETF 口径
    put_wall: float
    zero_gamma: float | None
    bias: str                           # 综合方向：偏多/偏空/中性/分歧...
    near_bias: str                      # 近端
    mid_bias: str                       # 中期
    verdict_head: str = ""              # 当日决策一句话总纲
    proxy_quality: str = "good"
    greeks: object = None               # callable(kind,strike,expiry)->(delta,iv)|None

    def look(self, kind: str, strike: float, expiry: date) -> tuple[float, float]:
        """取每股 delta 与 iv：链上有用链上，没有回退 BS（iv 用同标的近端 ATM 近似）。"""
        if callable(self.greeks):
            got = self.greeks(kind, strike, expiry)
            if got is not None:
                d, iv = got
                if iv and iv > 0:
                    return d, iv
        # 回退：无链数据时，用一个保守 iv 兜底（仅供方向敞口粗估，不作定价）
        iv = 0.30
        T = max((expiry - _today_ref()).days, 0) / 365.0
        return bs.delta(self.spot, strike, T, iv, kind=kind, r=RISK_FREE), iv


_TODAY: date | None = None


def _today_ref() -> date:
    """评价基准日：由 review_portfolio 注入，保证纯函数可测（不偷用系统时钟）。"""
    if _TODAY is None:
        raise RuntimeError("portfolio 评价未设置基准日；请经 review_portfolio 调用")
    return _TODAY


# ————————————————————————————————————————————————————————— 单腿评价


@dataclass(frozen=True)
class PositionReview:
    symbol: str
    name: str
    underlying: str
    kind: str                # C / P / STOCK
    side: str                # 多头 / 空头(卖出) / 正股
    qty: float
    strike: float | None
    expiry: date | None
    dte: int | None
    cost_price: float
    est_value: float | None  # 每股理论中值（BS，无 bid/ask）
    pnl: float | None        # 该腿浮动盈亏（含方向，正=盈）
    pos_delta: float | None  # 持仓 Delta（份×100×每股delta×方向）
    moneyness: str           # 价内/价外/贴价
    dist_pct: float | None   # (现价−行权)/现价
    wall_note: str           # 行权价与 Gamma 墙关系
    align: str               # 顺势 / 逆势 / 中性 / —
    flags: list[str] = field(default_factory=list)   # 风险旗标
    comment: str = ""        # 一句话评价


def _side(kind: str, qty: float) -> str:
    if kind == "STOCK":
        return "正股(多)" if qty > 0 else "正股(空)"
    return "多头" if qty > 0 else "空头(卖出)"


def _moneyness(kind: str, spot: float, strike: float) -> tuple[str, float]:
    dist = (spot - strike) / spot if spot else 0.0
    if abs(dist) <= NEAR_STRIKE_PCT:
        return "贴价", dist
    if kind == "C":
        return ("价内" if spot > strike else "价外"), dist
    return ("价内" if spot < strike else "价外"), dist


def _wall_note(kind: str, strike: float, ctx: InstrumentContext) -> str:
    """行权价与 Gamma 墙的关系——卖 put 想落在 put 墙(支撑)之上/附近，卖 call 想落在 call 墙之下。"""
    parts = []
    if ctx.put_wall > 0:
        rel = "上方" if strike > ctx.put_wall else ("附近" if abs(strike - ctx.put_wall) / ctx.put_wall <= 0.01 else "下方")
        parts.append(f"put墙 {ctx.put_wall:.1f} {rel}")
    if ctx.call_wall > 0:
        rel = "下方" if strike < ctx.call_wall else ("附近" if abs(strike - ctx.call_wall) / ctx.call_wall <= 0.01 else "上方")
        parts.append(f"call墙 {ctx.call_wall:.1f} {rel}")
    return "行权价处" + "、".join(parts) if parts else ""


def _bull(b: str) -> int:
    """把方向串折成 +1/−1/0。"""
    if "偏多" in b or "看多" in b:
        return 1
    if "偏空" in b or "看空" in b:
        return -1
    return 0


def _align(kind: str, qty: float, ctx: InstrumentContext) -> str:
    """持仓方向 vs undertow 综合研判是否顺势。

    卖 put / 买 call / 正股多 = 看多敞口；买 put / 卖 call / 正股空 = 看空敞口。
    """
    if kind == "STOCK":
        exposure = 1 if qty > 0 else -1
    elif kind == "P":
        exposure = 1 if qty < 0 else -1        # 卖 put=看多，买 put=看空
    else:  # C
        exposure = 1 if qty > 0 else -1        # 买 call=看多，卖 call=看空
    b = _bull(ctx.bias)
    if b == 0:
        return "中性(研判分歧/中性)"
    if exposure == b:
        return "顺势"
    return "逆势"


def _review_leg(pos, parsed: ParsedSymbol, ctx: InstrumentContext | None) -> PositionReview:
    side = _side(parsed.kind, pos.quantity)
    base = dict(symbol=pos.symbol, name=pos.name, underlying=parsed.underlying,
                kind=parsed.kind, side=side, qty=pos.quantity, strike=parsed.strike,
                expiry=parsed.expiry, cost_price=pos.cost_price)
    # 无上下文（该标的 undertow 无期权代理，如无映射）：只给结构、不评方向
    if ctx is None or ctx.spot <= 0:
        dte = (parsed.expiry - _today_ref()).days if parsed.expiry else None
        return PositionReview(**base, dte=dte, est_value=None, pnl=None, pos_delta=None,
                              moneyness="—", dist_pct=None, wall_note="",
                              align="—", flags=["无 undertow 期权代理，无法评方向"],
                              comment="仅列出，未接入研判上下文")
    if parsed.kind == "STOCK":
        pos_delta = pos.quantity          # 正股每股 delta=1
        return PositionReview(**base, dte=None, est_value=None,
                              pnl=(ctx.spot - pos.cost_price) * pos.quantity,
                              pos_delta=pos_delta, moneyness="—", dist_pct=None,
                              wall_note="", align=_align("STOCK", pos.quantity, ctx),
                              flags=[], comment=f"正股敞口，{_align('STOCK', pos.quantity, ctx)}于综合研判")

    # 期权腿
    dte = (parsed.expiry - _today_ref()).days
    d_share, iv = ctx.look(parsed.kind, parsed.strike, parsed.expiry)
    T = max(dte, 0) / 365.0
    est = bs.price(ctx.spot, parsed.strike, T, iv, kind=parsed.kind, r=RISK_FREE)
    pnl = (est - pos.cost_price) * CONTRACT_MULT * pos.quantity
    pos_delta = d_share * CONTRACT_MULT * pos.quantity
    money, dist = _moneyness(parsed.kind, ctx.spot, parsed.strike)
    wall = _wall_note(parsed.kind, parsed.strike, ctx)
    align = _align(parsed.kind, pos.quantity, ctx)

    flags: list[str] = []
    short = pos.quantity < 0
    if short and dte is not None and 0 <= dte <= NEAR_EXPIRY_DTE and money in ("价内", "贴价"):
        flags.append(f"临近到期({dte}天)且{money}——被行权风险高")
    if dte is not None and dte < 0:
        flags.append("已过期（数据滞后？请核对）")
    if align == "逆势":
        flags.append("方向与综合研判相反")
    if ctx.proxy_quality != "good":
        flags.append(f"{ctx.etf_symbol} 代理质量{ctx.proxy_quality}，位点仅定性")

    comment = _leg_comment(parsed, short, money, align, wall, dte)
    return PositionReview(**base, dte=dte, est_value=est, pnl=pnl, pos_delta=pos_delta,
                          moneyness=money, dist_pct=dist, wall_note=wall,
                          align=align, flags=flags, comment=comment)


def _leg_comment(parsed: ParsedSymbol, short: bool, money: str, align: str,
                 wall: str, dte: int | None) -> str:
    k = "看跌" if parsed.kind == "P" else "看涨"
    act = "卖出" if short else "买入"
    head = f"{act}{k}期权"
    if parsed.kind == "P" and short:
        # 卖 put：想收权金、不被行权 → 现价站在行权价上方最舒服
        tail = "收权金/愿低位接货" + ("；" + wall if wall else "")
        if money == "价外":
            tail += "，现价在行权价上方（暂安全）"
        elif money in ("贴价", "价内"):
            tail += f"，现价已{money}，注意被行权"
    elif parsed.kind == "C" and short:
        tail = "收权金/压制上行" + ("；" + wall if wall else "")
    elif parsed.kind == "P" and not short:
        tail = "买保护/看跌下行" + ("；" + wall if wall else "")
    else:  # 买 call
        tail = "博上行" + ("；" + wall if wall else "")
    return f"{head}：{tail}（{align}）"


# ————————————————————————————————————————————————————————— 结构 & 组合


@dataclass(frozen=True)
class AccountCapital:
    """账户资金口径，喂给评价引擎做"资金够不够接货"这类约束。"""
    buy_power: float          # 购买力（可用于新开/接货）
    net_assets: float         # 净资产
    cash_usd: float           # 可用美元现金


@dataclass(frozen=True)
class Combo:
    """一个"组合期权"——同品种下按(到期)聚合、或跨期识别出的多腿结构。

    max_loss=None 表示风险未封顶（裸卖/裸空）；capital_at_risk=占用或最坏风险资金（美元）。
    """
    underlying: str
    expiry_label: str        # "2026-08-26" / "跨期 08-26→09-18"
    label: str               # 牛市看跌价差 / 空头跨式 / 铁鹰 / 日历价差 / 单腿买call ...
    stance: str              # 保守做多 / 激进做多 / 中性收权金 / 保守做空 / 方向对冲 ...
    legs: list               # 成员 PositionReview
    qty: int
    net_credit: float | None # 每组净权金（>0=收，<0=付；None=不适用）
    max_profit: float | None
    max_loss: float | None   # None=风险未封顶
    defined_risk: bool
    capital_at_risk: float | None
    note: str


@dataclass(frozen=True)
class UnderlyingGroup:
    underlying: str
    display_name: str
    net_delta: float | None
    total_pnl: float | None
    bias: str
    verdict_head: str
    legs: list[PositionReview]
    combos: list[Combo] = field(default_factory=list)
    stance: str = ""              # 整品种策略姿态一句话
    capital_note: str = ""        # 资金分配/够不够接货
    summary: str = ""
    advice: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioReview:
    ok: bool
    asof: date
    groups: list[UnderlyingGroup]
    unmapped: list[PositionReview] = field(default_factory=list)
    headline: str = ""
    note: str = ""


# 短腿权金已收回多少比例才提示"可考虑落袋"
TAKE_PROFIT_FRAC = 0.70


@dataclass(frozen=True)
class _Vert:
    """一对垂直价差的中间量（内部用）。"""
    kind: str
    short: PositionReview
    long: PositionReview
    qty: int
    width: float
    credit: float             # 每份净权金（>0=收）
    max_profit: float
    max_loss: float
    label: str
    stance: str


def _pair_vertical(shorts, longs, kind):
    """从同类型的空/多腿各取一条配一个垂直价差；配不出返回 None。"""
    for s in shorts:
        for l in longs:
            width = abs(s.strike - l.strike)
            if width <= 0:
                continue
            qty = int(min(abs(s.qty), abs(l.qty)))
            if qty <= 0:
                continue
            credit = s.cost_price - l.cost_price
            cpos = credit > 0
            if kind == "P":
                bull = s.strike > l.strike
                label = "牛市看跌价差(收权金)" if bull else "熊市看跌价差(付权金)"
                stance = "保守做多" if bull else "保守做空"
            else:
                bear = s.strike < l.strike
                label = "熊市看涨价差(收权金)" if bear else "牛市看涨价差(付权金)"
                stance = "保守做空" if bear else "保守做多"
            max_profit = (credit if cpos else (width + credit)) * CONTRACT_MULT * qty
            max_loss = ((width - credit) if cpos else (-credit)) * CONTRACT_MULT * qty
            return _Vert(kind, s, l, qty, width, credit, max_profit, max_loss, label, stance)
    return None


def _vertical_combo(exp, v: _Vert, ctx) -> Combo:
    note = (f"卖{v.short.strike:g}/买{v.long.strike:g}，宽{v.width:g}，"
            f"净{'收' if v.credit > 0 else '付'}权金 {abs(v.credit):.2f}")
    return Combo(underlying=v.short.underlying, expiry_label=exp.isoformat(),
                 label=v.label, stance=v.stance, legs=[v.short, v.long], qty=v.qty,
                 net_credit=v.credit, max_profit=v.max_profit, max_loss=v.max_loss,
                 defined_risk=True, capital_at_risk=v.max_loss, note=note)


def _iron_combo(exp, pv: _Vert, cv: _Vert, ctx) -> Combo:
    qty = min(pv.qty, cv.qty)
    butterfly = pv.short.strike == cv.short.strike
    label = "铁蝶(收权金)" if butterfly else "铁鹰(收权金)"
    max_profit = pv.max_profit + cv.max_profit
    max_loss = max(pv.max_loss, cv.max_loss)   # 铁鹰只可能一侧被击穿
    note = (f"put 侧 卖{pv.short.strike:g}/买{pv.long.strike:g} + "
            f"call 侧 卖{cv.short.strike:g}/买{cv.long.strike:g}；赌区间震荡")
    return Combo(underlying=pv.short.underlying, expiry_label=exp.isoformat(),
                 label=label, stance="中性收权金(区间)",
                 legs=[pv.short, pv.long, cv.short, cv.long], qty=qty,
                 net_credit=pv.credit + cv.credit, max_profit=max_profit, max_loss=max_loss,
                 defined_risk=True, capital_at_risk=max_loss, note=note)


def _straddle_combos(exp, calls, puts, ctx, used) -> list[Combo]:
    """剩余未配对的 call+put：同号→跨式/宽跨式，异号→合成/风险反转。"""
    out = []
    for c in list(calls):
        for p in list(puts):
            if id(c) in used or id(p) in used:
                continue
            qty = int(min(abs(c.qty), abs(p.qty)))
            if qty <= 0:
                continue
            same_k = c.strike == p.strike
            if c.qty < 0 and p.qty < 0:            # 双卖
                label = "空头跨式(收权金)" if same_k else "空头宽跨式(收权金)"
                stance = "中性收权金(赌不动)"
                credit = c.cost_price + p.cost_price
                cap = None; mloss = None; defined = False
            elif c.qty > 0 and p.qty > 0:          # 双买
                label = "多头跨式" if same_k else "多头宽跨式"
                stance = "博大波动(方向中性)"
                credit = -(c.cost_price + p.cost_price)
                cap = (c.cost_price + p.cost_price) * CONTRACT_MULT * qty
                mloss = cap; defined = True
            else:                                   # 一买一卖不同类=合成/风险反转
                bull = (p.qty < 0 and c.qty > 0)
                label = "风险反转(合成做多)" if bull else "风险反转(合成做空)"
                stance = "激进做多" if bull else "激进做空"
                credit = (c.cost_price if c.qty < 0 else -c.cost_price) + \
                         (p.cost_price if p.qty < 0 else -p.cost_price)
                cap = None; mloss = None; defined = False
            used.add(id(c)); used.add(id(p))
            out.append(Combo(underlying=c.underlying, expiry_label=exp.isoformat(),
                             label=label, stance=stance, legs=[c, p], qty=qty,
                             net_credit=credit, max_profit=None, max_loss=mloss,
                             defined_risk=defined, capital_at_risk=cap,
                             note=f"call {c.strike:g}{'卖' if c.qty<0 else '买'} + put {p.strike:g}{'卖' if p.qty<0 else '买'}"))
    return out


def _single_combo(lg: PositionReview, ctx) -> Combo:
    short = lg.qty < 0
    prem = lg.cost_price * CONTRACT_MULT * abs(lg.qty)
    if lg.kind == "C":
        stance = "保守做空(压顶收权金)" if short else "激进做多(凸性上行)"
        label = f"单腿{'卖' if short else '买'}call {lg.strike:g}"
    else:
        stance = "保守做多(收权金愿接货)" if short else "激进做空/对冲(买跌)"
        label = f"单腿{'卖' if short else '买'}put {lg.strike:g}"
    if short:
        # 裸卖：put 接货全额 / call 上行无限——风险未封顶
        cap = (lg.strike * CONTRACT_MULT * abs(lg.qty)) if lg.kind == "P" else None
        defined = False; mloss = None
    else:
        cap = prem; defined = True; mloss = prem     # 买方最大亏=已付权金
    return Combo(underlying=lg.underlying,
                 expiry_label=lg.expiry.isoformat() if lg.expiry else "—",
                 label=label, stance=stance, legs=[lg], qty=int(abs(lg.qty)),
                 net_credit=(lg.cost_price if short else -lg.cost_price),
                 max_profit=(prem if short else None), max_loss=mloss,
                 defined_risk=defined, capital_at_risk=cap,
                 note=f"{'卖出收权金' if short else '买入付权金'} {lg.cost_price:.2f}")


def _calendar_combos(leftover, ctx, used) -> list[Combo]:
    """跨到期同类型、同/异行权：一近一远反向 → 日历(同行权)/对角(异行权)价差。

    长桥不会把这类显示成组合，靠这里自己识别。
    """
    out = []
    by_kind = {}
    for lg in leftover:
        if id(lg) in used:
            continue
        by_kind.setdefault(lg.kind, []).append(lg)
    for kind, grp in by_kind.items():
        grp = sorted(grp, key=lambda x: x.expiry)
        for i in range(len(grp)):
            for j in range(len(grp)):
                a, b = grp[i], grp[j]
                if id(a) in used or id(b) in used or a is b:
                    continue
                if a.expiry == b.expiry or (a.qty < 0) == (b.qty < 0):
                    continue
                near, far = (a, b) if a.expiry < b.expiry else (b, a)
                # 典型日历=卖近买远（收 theta）；反之为反向日历
                same_k = near.strike == far.strike
                label = ("日历价差" if same_k else "对角价差") + \
                        ("(卖近买远)" if near.qty < 0 else "(买近卖远)")
                stance = "中性偏收(theta)" if same_k else "方向+theta混合"
                used.add(id(a)); used.add(id(b))
                out.append(Combo(underlying=near.underlying,
                                 expiry_label=f"跨期 {near.expiry:%m-%d}→{far.expiry:%m-%d}",
                                 label=label, stance=stance, legs=[near, far],
                                 qty=int(min(abs(near.qty), abs(far.qty))),
                                 net_credit=None, max_profit=None, max_loss=None,
                                 defined_risk=False, capital_at_risk=None,
                                 note=f"{kind} 近{near.strike:g}({near.expiry:%m-%d})/远{far.strike:g}({far.expiry:%m-%d})"))
    return out


def _classify_underlying(legs: list[PositionReview], ctx) -> list[Combo]:
    """把一个品种的全部腿识别成组合：先按到期内组合，再跨期日历/对角，剩下按单腿。"""
    opts = [l for l in legs if l.kind in ("C", "P") and l.strike is not None and l.expiry is not None]
    used: set = set()
    combos: list[Combo] = []

    by_exp: dict = {}
    for l in opts:
        by_exp.setdefault(l.expiry, []).append(l)

    for exp in sorted(by_exp):
        grp = by_exp[exp]
        calls = [l for l in grp if l.kind == "C"]
        puts = [l for l in grp if l.kind == "P"]
        pv = _pair_vertical([x for x in puts if x.qty < 0], [x for x in puts if x.qty > 0], "P")
        cv = _pair_vertical([x for x in calls if x.qty < 0], [x for x in calls if x.qty > 0], "C")
        # 铁鹰/铁蝶：put 侧与 call 侧各一个收权金垂直价差
        if pv and cv and pv.credit > 0 and cv.credit > 0:
            for l in (pv.short, pv.long, cv.short, cv.long):
                used.add(id(l))
            combos.append(_iron_combo(exp, pv, cv, ctx))
            continue
        if pv:
            used.add(id(pv.short)); used.add(id(pv.long))
            combos.append(_vertical_combo(exp, pv, ctx))
        if cv:
            used.add(id(cv.short)); used.add(id(cv.long))
            combos.append(_vertical_combo(exp, cv, ctx))
        rem_c = [l for l in calls if id(l) not in used]
        rem_p = [l for l in puts if id(l) not in used]
        combos += _straddle_combos(exp, rem_c, rem_p, ctx, used)

    leftover = [l for l in opts if id(l) not in used]
    combos += _calendar_combos(leftover, ctx, used)
    for l in opts:
        if id(l) not in used:
            combos.append(_single_combo(l, ctx))
            used.add(id(l))
    return combos


def _reclassify_combo_legs(legs, combos):
    """多腿组合的成员腿不再单独判顺/逆势——方向由组合整体承载。"""
    multi = set()
    for c in combos:
        if len(c.legs) >= 2:
            for l in c.legs:
                multi.add(id(l))
    out = []
    for lg in legs:
        if id(lg) in multi and lg.align in ("顺势", "逆势"):
            out.append(replace(
                lg, align="组合腿(结构)",
                flags=[f for f in lg.flags if "相反" not in f],
                comment=lg.comment.replace("（顺势）", "（组合腿）").replace("（逆势）", "（组合腿）")))
        else:
            out.append(lg)
    return out


def _book_stance(combos, ctx, capital) -> tuple[str, str]:
    """整品种策略姿态 + 资金分配一句话。"""
    if not combos:
        return "", ""
    aggr = [c for c in combos if "激进" in c.stance]
    cons = [c for c in combos if "保守" in c.stance]
    neut = [c for c in combos if "中性" in c.stance or "对冲" in c.stance or "theta" in c.stance]
    long_bias = _bull(ctx.bias)
    dir_word = "偏多" if long_bias > 0 else ("偏空" if long_bias < 0 else "中性")
    layers = "；".join(f"{c.stance}（{c.label}）" for c in combos)
    stance = f"整体≈{dir_word}·{len(combos)} 层结构：{layers}"

    # 资金分配：激进(买方付权金) vs 保守(定义风险最大亏)
    def cap_of(cs):
        return sum(c.capital_at_risk for c in cs if c.capital_at_risk is not None)
    ca, cc = cap_of(aggr), cap_of(cons)
    parts = []
    if ca or cc:
        na = capital.net_assets if capital else 0
        seg = []
        if ca:
            seg.append(f"激进 ${ca:,.0f}" + (f"（≈净资产{ca/na*100:.0f}%）" if na else ""))
        if cc:
            seg.append(f"保守 ${cc:,.0f}" + (f"（≈净资产{cc/na*100:.0f}%）" if na else ""))
        parts.append("资金投入/风险：" + " · ".join(seg))
        if ca and cc and 0.5 <= ca / cc <= 2.0:
            parts.append("两层大致半仓激进、半仓保守")
    # 资金够不够接货
    if capital is not None:
        naked_puts = [c for c in combos if "单腿卖put" in c.label]
        need = sum((c.capital_at_risk or 0) for c in naked_puts)
        if need > 0 and capital.buy_power < need:
            parts.append(f"⚠ 购买力 ${capital.buy_power:,.0f} < 裸卖 put 接货全额 ${need:,.0f}，"
                         f"资金不足接货——这些腿只能到期前平仓/展期，不能走接货路径")
    return stance, "。".join(parts)


def _group_advice(combos, legs, ctx, capital) -> list[str]:
    """规则化建议（💡权衡/参考口径，**非投资指令**）。数字全确定性算出，含资金约束。"""
    adv: list[str] = []
    spot = ctx.spot
    bp = capital.buy_power if capital else None

    for c in combos:
        # 收权金垂直价差：盈亏平衡 + 封顶 + 资金真相
        if c.label.startswith("牛市看跌价差") and c.net_credit and c.net_credit > 0:
            sshort = max(l.strike for l in c.legs)
            slong = min(l.strike for l in c.legs)
            be = sshort - c.net_credit
            room = (spot - be) / spot * 100 if spot else 0
            adv.append(
                f"【{c.label}】盈亏平衡≈{be:.2f}；现价 {spot:.2f} 在其{'上方' if spot > be else '下方'}"
                f" {abs(room):.1f}%。跌破 {be:.2f} 才转亏，**真正现金风险=价差最大亏 ${c.max_loss:,.0f}**"
                f"（已被买{slong:g}腿封顶），不是全额接货。时间站你这边，可持有到期让权金归零。")
        elif c.label.startswith("熊市看涨价差") and c.net_credit and c.net_credit > 0:
            sshort = min(l.strike for l in c.legs)
            be = sshort + c.net_credit
            adv.append(f"【{c.label}】盈亏平衡≈{be:.2f}；站上 {be:.2f} 转亏，最大亏 ${c.max_loss:,.0f} 封顶。")
        elif c.label.startswith("单腿买call"):
            lg = c.legs[0]
            adv.append(f"【{c.label}】激进做多、凸性上行；最大亏=已付权金 ${c.capital_at_risk:,.0f}"
                       f"（占净资产不小，属博弹性的那半仓）。看涨墙 {ctx.call_wall:.1f} 是上行目标参考。")
        elif c.label == "铁鹰(收权金)" or c.label == "铁蝶(收权金)":
            adv.append(f"【{c.label}】赌区间震荡收权金，两侧风险均被买腿封顶（最大亏 ${c.max_loss:,.0f}）；"
                       f"现价越靠中间越舒服，逼近任一短腿要考虑调整。")

    # 卖出腿的临近到期处理（资金约束是关键）
    def _in_defined(lg):
        # 按合约代码匹配（重分类后 leg 对象已变，不能用对象相等）
        return any(c.defined_risk and any(m.symbol == lg.symbol for m in c.legs) for c in combos)

    for lg in legs:
        if lg.kind not in ("C", "P") or lg.strike is None or lg.dte is None or lg.qty >= 0:
            continue
        in_defined = _in_defined(lg)
        captured = (lg.cost_price - lg.est_value) / lg.cost_price if lg.cost_price and lg.est_value is not None else 0
        if any("被行权" in f for f in lg.flags):
            if lg.kind == "P":
                assign = lg.strike * CONTRACT_MULT * abs(lg.qty)
                if bp is not None and bp < assign:
                    if in_defined:
                        adv.append(
                            f"⚠【{lg.name}】剩 {lg.dte} 天且{lg.moneyness}。你**购买力仅 ${bp:,.0f}、"
                            f"远不够接货 ${assign:,.0f}**——虽有保护腿把最终亏损封顶，但若到期被指派仍需短暂"
                            f"垫付全额现金，账户扛不住。**务必到期前平仓或展期(roll)，别让它到期指派**。")
                    else:
                        adv.append(
                            f"⚠【{lg.name}】裸卖、剩 {lg.dte} 天且{lg.moneyness}，接货需 ${assign:,.0f} 但"
                            f"购买力仅 ${bp:,.0f}——**不能接货**，只能到期前平仓/展期，或纪律止损。")
                else:
                    adv.append(
                        f"⚠【{lg.name}】剩 {lg.dte} 天且{lg.moneyness}：①愿接货→持有等行权"
                        f"（约 ${assign:,.0f}，接后转卖 covered call 继续车轮）；②不愿→平仓/向下向后展期收新权金；③止损。")
            else:
                adv.append(f"⚠【{lg.name}】卖 call 剩 {lg.dte} 天且{lg.moneyness}，被叫走概率上升；"
                           f"持正股可接受，否则平仓/向上展期。")
        elif lg.moneyness == "价外" and captured >= TAKE_PROFIT_FRAC:
            adv.append(f"【{lg.name}】已收回约 {captured*100:.0f}% 权金且仍价外，可提前平仓落袋、免尾部风险。")

    # 组合级顺逆
    b = _bull(ctx.bias)
    if b != 0:
        dir_legs = [lg for lg in legs if lg.align in ("顺势", "逆势")]
        if dir_legs and all(lg.align == "顺势" for lg in dir_legs):
            adv.append(f"整体方向与综合研判（{ctx.bias}）一致，可按纪律持有；加仓/新开先过盈亏比闸门（别追、等回调）。")
    return adv


def _group_summary(und, legs, combos, ctx) -> str:
    risk = [f for lg in legs for f in lg.flags if "被行权" in f or "已过期" in f]
    parts = [f"综合研判 {ctx.bias}（近{ctx.near_bias}/中{ctx.mid_bias}）"]
    if ctx.verdict_head:
        parts.append(f"决策：{ctx.verdict_head}")
    if combos:
        parts.append("组合：" + "；".join(f"{c.label}" for c in combos))
    if risk:
        parts.append("⚠ " + "；".join(dict.fromkeys(risk)))
    return "。".join(parts)


def review_portfolio(positions, contexts: dict, asof: date,
                     capital: AccountCapital | None = None) -> PortfolioReview:
    """核心入口。

    positions：list（需有 symbol/name/quantity/cost_price 字段）。
    contexts：{ETF根: InstrumentContext}，如 {'SLV': ctx_silver}。
    asof：评价基准日（注入，纯函数可测）。
    capital：账户资金口径（可选）；给了才做"够不够接货"的资金约束建议。
    """
    global _TODAY
    _TODAY = asof
    try:
        reviews: list[tuple[ParsedSymbol, PositionReview]] = []
        for pos in positions:
            parsed = parse_symbol(pos.symbol)
            ctx = contexts.get(parsed.underlying)
            reviews.append((parsed, _review_leg(pos, parsed, ctx)))

        groups: list[UnderlyingGroup] = []
        unmapped: list[PositionReview] = []
        by_und: dict[str, list[PositionReview]] = {}
        for parsed, rv in reviews:
            if parsed.underlying in contexts:
                by_und.setdefault(parsed.underlying, []).append(rv)
            else:
                unmapped.append(rv)

        for und, legs in by_und.items():
            ctx = contexts[und]
            combos = _classify_underlying(legs, ctx)
            legs = _reclassify_combo_legs(legs, combos)
            deltas = [lg.pos_delta for lg in legs if lg.pos_delta is not None]
            pnls = [lg.pnl for lg in legs if lg.pnl is not None]
            stance, cap_note = _book_stance(combos, ctx, capital)
            groups.append(UnderlyingGroup(
                underlying=und, display_name=ctx.display_name,
                net_delta=sum(deltas) if deltas else None,
                total_pnl=sum(pnls) if pnls else None,
                bias=ctx.bias, verdict_head=ctx.verdict_head,
                legs=legs, combos=combos, stance=stance, capital_note=cap_note,
                summary=_group_summary(und, legs, combos, ctx),
                advice=_group_advice(combos, legs, ctx, capital)))

        headline = _portfolio_headline(groups, unmapped)
        return PortfolioReview(ok=True, asof=asof, groups=groups, unmapped=unmapped,
                               headline=headline)
    finally:
        _TODAY = None


def _portfolio_headline(groups: list[UnderlyingGroup], unmapped: list[PositionReview]) -> str:
    n_legs = sum(len(g.legs) for g in groups) + len(unmapped)
    if not groups and not unmapped:
        return "当前无持仓"
    tags = []
    for g in groups:
        d = "" if g.net_delta is None else f"净Δ{g.net_delta:+.0f}"
        tags.append(f"{g.display_name} {d}")
    risk = sum(1 for g in groups for lg in g.legs for f in lg.flags if "被行权" in f)
    head = f"{n_legs} 笔持仓 · " + " / ".join(tags)
    if risk:
        head += f" · ⚠ {risk} 笔临近到期被行权风险"
    return head
