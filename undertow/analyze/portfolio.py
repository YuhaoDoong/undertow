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
class SpreadStruct:
    underlying: str
    expiry: date
    kind: str                # C / P
    label: str               # 牛市看跌价差 / 熊市看涨价差 / 垂直价差
    short_strike: float
    long_strike: float
    width: float
    qty: int
    net_credit: float        # 每份净权金（>0=收，<0=付）
    max_profit: float        # 组合最大盈（美元）
    max_loss: float          # 组合最大亏（美元）
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
    spreads: list[SpreadStruct] = field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True)
class PortfolioReview:
    ok: bool
    asof: date
    groups: list[UnderlyingGroup]
    unmapped: list[PositionReview] = field(default_factory=list)
    headline: str = ""
    note: str = ""


def _detect_spreads(legs: list[PositionReview]) -> list[SpreadStruct]:
    """同标的同到期同类型里，一空一多相邻行权 → 垂直价差。"""
    out: list[SpreadStruct] = []
    by = {}
    for lg in legs:
        if lg.kind not in ("C", "P") or lg.strike is None or lg.expiry is None:
            continue
        by.setdefault((lg.underlying, lg.expiry, lg.kind), []).append(lg)
    for (und, exp, kind), grp in by.items():
        shorts = [x for x in grp if x.qty < 0]
        longs = [x for x in grp if x.qty > 0]
        for s in shorts:
            for l in longs:
                qty = int(min(abs(s.qty), abs(l.qty)))
                if qty <= 0:
                    continue
                width = abs(s.strike - l.strike)
                if width <= 0:
                    continue
                net_credit = s.cost_price - l.cost_price   # 卖收 − 买付
                if kind == "P":
                    label = "牛市看跌价差(收权金)" if s.strike > l.strike else "熊市看跌价差(付权金)"
                else:
                    label = "熊市看涨价差(收权金)" if s.strike < l.strike else "牛市看涨价差(付权金)"
                credit_pos = net_credit > 0
                max_profit = (net_credit if credit_pos else (width + net_credit)) * CONTRACT_MULT * qty
                max_loss = ((width - net_credit) if credit_pos else (-net_credit)) * CONTRACT_MULT * qty
                out.append(SpreadStruct(
                    underlying=und, expiry=exp, kind=kind, label=label,
                    short_strike=s.strike, long_strike=l.strike, width=width, qty=qty,
                    net_credit=net_credit, max_profit=max_profit, max_loss=max_loss,
                    note=f"卖{s.strike:g}/买{l.strike:g}，宽{width:g}，净{'收' if credit_pos else '付'}权金 {abs(net_credit):.2f}"))
    return out


def _reclassify_spread_legs(legs: list[PositionReview],
                            spreads: list[SpreadStruct]) -> list[PositionReview]:
    """价差成员腿不再单独判顺/逆势——方向由价差整体承载（保护腿≠逆势押注）。

    只改 align → 「价差腿(结构)」并去掉误报的「方向与综合研判相反」旗标；
    被行权/到期等真实风险旗标保留。
    """
    members: set[tuple] = set()
    for s in spreads:
        members.add((s.underlying, s.expiry, s.kind, s.short_strike))
        members.add((s.underlying, s.expiry, s.kind, s.long_strike))
    out: list[PositionReview] = []
    for lg in legs:
        key = (lg.underlying, lg.expiry, lg.kind, lg.strike)
        if key in members and lg.align in ("顺势", "逆势"):
            out.append(replace(
                lg, align="价差腿(结构)",
                flags=[f for f in lg.flags if "相反" not in f],
                comment=lg.comment.replace("（顺势）", "（价差腿）").replace("（逆势）", "（价差腿）")))
        else:
            out.append(lg)
    return out


def _group_summary(und: str, legs: list[PositionReview], spreads: list[SpreadStruct],
                   ctx: InstrumentContext) -> str:
    aligns = [lg.align for lg in legs if lg.align in ("顺势", "逆势")]
    n_ok = aligns.count("顺势")
    n_bad = aligns.count("逆势")
    risk = [f for lg in legs for f in lg.flags if "被行权" in f or "已过期" in f]
    parts = [f"综合研判 {ctx.bias}（近{ctx.near_bias}/中{ctx.mid_bias}）"]
    if ctx.verdict_head:
        parts.append(f"决策：{ctx.verdict_head}")
    if spreads:
        parts.append("；".join(s.label + s.note for s in spreads))
    if n_ok or n_bad:
        parts.append(f"仓位方向：{n_ok} 顺势 / {n_bad} 逆势")
    if risk:
        parts.append("⚠ " + "；".join(dict.fromkeys(risk)))
    return "。".join(parts)


def review_portfolio(positions, contexts: dict, asof: date) -> PortfolioReview:
    """核心入口。

    positions：list（需有 symbol/name/quantity/cost_price 字段）。
    contexts：{ETF根: InstrumentContext}，如 {'SLV': ctx_silver}。
    asof：评价基准日（注入，纯函数可测）。
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
            spreads = _detect_spreads(legs)
            legs = _reclassify_spread_legs(legs, spreads)
            deltas = [lg.pos_delta for lg in legs if lg.pos_delta is not None]
            pnls = [lg.pnl for lg in legs if lg.pnl is not None]
            groups.append(UnderlyingGroup(
                underlying=und, display_name=ctx.display_name,
                net_delta=sum(deltas) if deltas else None,
                total_pnl=sum(pnls) if pnls else None,
                bias=ctx.bias, verdict_head=ctx.verdict_head,
                legs=legs, spreads=spreads,
                summary=_group_summary(und, legs, spreads, ctx)))

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
