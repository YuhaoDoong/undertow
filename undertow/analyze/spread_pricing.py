"""垂直价差下单定价、买卖方同期对比、ETF↔商品位点换算 —— 纯函数，无 I/O。

为什么有这个模块（用户 2026-10-15）：「有些重要的功能或者操作，如果是你手动做的，你应该写成模块代码固定下来。」
当天我三次用临时 python 手算：① 组合单净价与挂单区间；② 买方 vs 卖方（卖方按周到期滚动）对比；
③ 商品价 ↔ ETF 位点换算。手算容易口径漂移（哪边按 bid、费用扣几次、比值取哪个时刻），所以固定在这里，
CLI 入口见 undertow/desk_cli.py（spread-quote / sides）与 cli.cmd_quote（--commodity / --level）。

口径（与仓库既有约定一致，不另立）：
- 净价一律用【带符号的每股净借记】表示：正 = 付（借记价差），负 = 收（贷记价差）。三档：
    worst = 逐腿对手价参考（买腿按 ask、卖腿按 bid）；mid = 各腿中价；best = 最好情形（买腿按 bid、卖腿按 ask）。
  逐腿对手价只是各腿买一/卖一的合成参考，【不保证整单立即成交】（组合单另行撮合，且只看了一档深度）。
- 盘口合同（Codex 052 I3）：
    · 某侧缺失（None / 0）或该侧挂单量明确为 0 → 只有用到该侧的档 None（不折零、不拿 last 顶替）；
    · 盘口交叉（bid > ask）、非有限/负数/非数值、取数报错 → 整条腿不可信，三档全 None，不出价、不出挂单建议；
    · 陈旧（如 CBOE 昨收快照）→ 默认三档全 None；调用方显式 allow_stale=True 才算，结果带 stale=True，
      只作情景参考，limit_range 对陈旧价不给挂单区间。长桥 depth 不带时间戳，新鲜度无法核实，调用方须如实说明。
- 经济量（最大亏损/最大收益/盈亏平衡）一律委托 spread_econ.vertical（唯一来源），费用按其往返模型：
  healthcheck.FEE_PER_CONTRACT × 腿数 × 2 笔（$3.20/组），含费口径与模拟仓、体检一致；指派处置费用未知，不计。
- 金额单位（Codex 052 I3）：每组（unit_*）与整笔（total_*，即请求组数下的整笔）分开给，都直接取自 spread_econ
  （qty=1 与 qty=实际组数各调一次），本模块不自己乘组数。政策预算只吃【每组】金额 + 请求组数，组数只在 risk_policy 里乘一次。
- 借记价差的理论最大亏损是【按到期清算】的含费限损，不排除短腿被提前指派后的股票头寸、资金占用与追缴处置
  （券商具体处理未核实），见 ASSIGNMENT_NOTE。
- delta 只当「价内概率粗略代理」（风险中性近似），不是真实概率、更不是含费盈利概率；含费最大收益 ≤ 0 时不给代理。
- 组合单是整单一个净价挂单（长桥）；分腿出场按用户 2026-10-15 实测：可以分腿平，但必须先平卖腿再平买腿。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from undertow.analyze.healthcheck import FEE_PER_CONTRACT
from undertow.analyze import risk_policy as rp

MULT = 100

# 组合单挂单区间：起挂 = 中价；上限 = 中价向对手价走 LIMIT_CAP_FRAC。【未校准】——依据只有一次观察，
# 明细私有：data/account/broker_observations.md（Codex 052 I4 / 054 R1 从公开源码迁出）。改数值请同时更新依据。
LIMIT_CAP_FRAC = 0.6
LIMIT_META = {"status": "未校准（单次观察）", "start": "中价", "cap": f"中价 + {LIMIT_CAP_FRAC:.0%} ×（对手价 − 中价）",
              "evidence": "单次观察，未校准；明细私有：data/account/broker_observations.md"}

# 券商处置的用户实测（不是券商文档）。
BROKER_NOTES = {
    "combo_leg_exit": "用户 2026-10-15 实测：长桥组合单可分腿平仓，须先平卖腿（买回短腿）再平买腿；顺序反了会留下裸卖。",
}

# 理论限损 ≠ 没有处置风险（Codex 052 金融提示 4；OIC 指派说明、Cboe/Fidelity 牛市 call 价差说明：短腿可被提前指派，资金不足可能追缴）。
ASSIGNMENT_NOTE = ("理论最大亏损按到期清算计；短腿仍可能被提前指派，留下股票头寸、占用资金或触发追缴/强制处置，"
                   "处置费用与长桥的具体处理未核实，不在上面的金额里。")

# 商品参考价：config/instruments.json 的 commodity 是每个品种的主参考；这里只放【额外】参考（如原油同时看布伦特）。
EXTRA_COMMODITY_REFS: dict[str, list[tuple[str, str]]] = {
    "wti": [("BZ=F", "布伦特期货（ICE）")],
}
COMMODITY_LABELS = {
    "GC=F": "COMEX 黄金期货（≠ 伦敦现货 XAUUSD，有基差）",
    "SI=F": "COMEX 白银期货（≠ 伦敦现货 XAGUSD，有基差）",
    "CL=F": "NYMEX WTI 期货（USO 跟的是它）",
    "BZ=F": "布伦特期货（ICE）",
}


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _bad_number(x) -> bool:
    """非 None 却不是可用的数（非数值、bool、NaN/inf、负数）→ 数据损坏（区别于「该侧无挂单」的 None/0）。"""
    if x is None:
        return False
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return True
    return not math.isfinite(x) or x < 0


@dataclass(frozen=True)
class LegQuote:
    strike: float
    kind: str                  # "C" / "P"
    side: int                  # +1 买入 / −1 卖出
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None   # None = 来源没给量（未知）；0 = 该侧明确无挂单量
    ask_size: int | None = None
    delta: float | None = None
    error: str = ""            # 取数报错（非空 = 该腿不可信）
    stale: bool = False        # 陈旧报价（如昨收快照）

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2.0 if (_pos(self.bid) and _pos(self.ask)) else None


def leg_problem(lg: LegQuote) -> str | None:
    """整条腿不可信的原因（交叉/非有限/报错）；可用（哪怕缺一侧）→ None。陈旧另由 net_prices 的 allow_stale 处理。"""
    if lg.error:
        return f"取数报错：{lg.error}"
    if _bad_number(lg.bid) or _bad_number(lg.ask):
        return f"盘口数值非法（bid={lg.bid!r} ask={lg.ask!r}）"
    if _pos(lg.bid) and _pos(lg.ask) and lg.bid > lg.ask:
        return f"盘口交叉（bid {lg.bid:g} > ask {lg.ask:g}）"
    return None


def net_prices(legs: list[LegQuote], *, allow_stale: bool = False) -> dict:
    """整单净价三档（带符号每股净借记：正付负收）。返回 {"worst","mid","best","missing","problems","stale"}。
    missing：缺某侧（只影响用到它的档）；problems：整腿不可信（交叉/非有限/报错/未获准的陈旧）→ 三档全 None。"""
    out = {"worst": 0.0, "mid": 0.0, "best": 0.0, "missing": [], "problems": [], "stale": False}
    for lg in legs:
        name = f"{'买' if lg.side > 0 else '卖'} {lg.strike:g}{lg.kind}"
        prob = leg_problem(lg)
        if prob is None and lg.stale and not allow_stale:
            prob = "报价陈旧（未获准按陈旧价计算）"
        if prob is not None:
            out["problems"].append(f"{name} {prob}")
            continue
        out["stale"] = out["stale"] or lg.stale
        bid = lg.bid if (lg.bid_size is None or lg.bid_size > 0) else None    # 量明确为 0 → 该侧视为没有
        ask = lg.ask if (lg.ask_size is None or lg.ask_size > 0) else None
        mid = (bid + ask) / 2.0 if (_pos(bid) and _pos(ask)) else None
        worst_px = ask if lg.side > 0 else bid             # 逐腿对手价：买付 ask、卖收 bid
        best_px = bid if lg.side > 0 else ask
        for key, px, what in (("worst", worst_px, "ask" if lg.side > 0 else "bid"),
                              ("best", best_px, "bid" if lg.side > 0 else "ask"),
                              ("mid", mid, "中价")):
            if out[key] is None:
                continue
            if not _pos(px):
                out[key] = None
                out["missing"].append(f"{name} 缺 {what}")
            else:
                out[key] += lg.side * px
    if out["problems"]:
        out["worst"] = out["mid"] = out["best"] = None
    for k in ("worst", "mid", "best"):
        if out[k] is not None:
            out[k] = round(out[k], 4)
    out["missing"] = sorted(set(out["missing"]))
    return out


def vertical(legs: list[LegQuote]) -> dict:
    """识别两腿垂直价差：同类型、一买一卖、不同行权价。返回 {"kind","debit","width","long","short","name"}；不合法抛 ValueError。"""
    if len(legs) != 2:
        raise ValueError("只支持两腿垂直价差")
    a, b = legs
    if a.kind != b.kind or a.kind not in ("C", "P"):
        raise ValueError("两腿必须同为 call 或同为 put")
    if {a.side, b.side} != {1, -1}:
        raise ValueError("必须一腿买入、一腿卖出")
    if a.strike == b.strike:
        raise ValueError("两腿行权价不能相同")
    lng, sht = (a, b) if a.side > 0 else (b, a)
    debit = lng.strike < sht.strike if a.kind == "C" else lng.strike > sht.strike
    # 方向前缀按结构的多空方向（Codex 052：旧版把熊市 call 贷记叫「看涨贷记」、牛市 put 贷记叫「看跌贷记」，与括号相反）
    name = {("C", True): "看涨 call 借记（牛市 call 价差：买低卖高）",
            ("C", False): "看跌 call 贷记（熊市 call 价差：卖低买高）",
            ("P", True): "看跌 put 借记（熊市 put 价差：买高卖低）",
            ("P", False): "看涨 put 贷记（牛市 put 价差：卖高买低）"}[(a.kind, debit)]
    bias = "bull" if (a.kind == "C") == debit else "bear"
    return {"kind": a.kind, "debit": debit, "width": abs(a.strike - b.strike), "long": lng.strike,
            "short": sht.strike, "name": name, "bias": bias}


def economics(spec: dict, net: float | None, *, qty: int = 1, fee: float = FEE_PER_CONTRACT, n_legs: int = 2) -> dict | None:
    """按某个带符号净价算含费经济量 —— 委托 spread_econ.vertical（垂直价差经济量的唯一来源，Codex 043 WP1），
    这里只做符号转换（entry_credit = −net）与下限检查。费用 = 腿数 × 张 × 2 笔 × FEE_PER_CONTRACT 的往返模型。
    net 的方向必须与结构一致（借记 > 0、贷记 < 0）且绝对值在 (0, 宽度) 内，否则 None（不可计算）。

    单位（Codex 052 I3：旧版只返回整笔，却被 budget_candidate 当每组再乘一次组数）：
      unit_max_loss / unit_max_gain / unit_fee_total = 每组（spread_econ qty=1）；
      total_max_loss / total_max_gain / total_fee_total = qty 组整笔（spread_econ qty=qty）。
    两套都由 spread_econ 直接给出，这里不乘组数；故意不再提供不带单位的 max_loss/max_gain，旧读法会直接 KeyError。"""
    from undertow.analyze import spread_econ as se
    if net is None or isinstance(net, bool) or not isinstance(net, (int, float)) or not math.isfinite(net):
        return None
    if not (isinstance(qty, int) and not isinstance(qty, bool) and qty >= 1):
        return None

    def run(q):
        e = se.vertical(side=spec["kind"], k_sell=spec["short"], k_buy=spec["long"], entry_credit=-net, qty=q,
                        fee_round_trip=n_legs * 2 * fee)
        if not e.get("complete", True) or e.get("error") or e["entry_net_payoff_max_loss"] is None:
            return None
        return e
    unit, total = run(1), run(qty)
    if unit is None or total is None:
        return None
    pf = rp.payoff_floor(unit["entry_net_max_gain"], unit["entry_net_payoff_max_loss"])
    return {"net": net, "qty": qty, "breakeven": unit["breakeven"],
            "unit_max_loss": unit["entry_net_payoff_max_loss"], "unit_max_gain": unit["entry_net_max_gain"],
            "unit_fee_total": unit["fee_total_usd"],
            "total_max_loss": total["entry_net_payoff_max_loss"], "total_max_gain": total["entry_net_max_gain"],
            "total_fee_total": total["fee_total_usd"],
            "fee_profit_possible": unit["entry_net_max_gain"] > 0,
            "payoff_floor": pf, "breakeven_win_rate": pf["breakeven_win_rate"] if unit["entry_net_max_gain"] > 0 else None}


def limit_range(prices: dict) -> dict | None:
    """建议组合单挂单区间（带符号净价）：起挂 = 中价（向有利方向取整到 0.01），上限 = 中价 + LIMIT_CAP_FRAC×(对手价−中价)。
    借记时上限是最多付多少；贷记时（负数）上限对应最少收多少。中价或对手价缺失、盘口陈旧或不可信 → None。"""
    m, w = prices.get("mid"), prices.get("worst")
    if m is None or w is None or prices.get("stale") or prices.get("problems"):
        return None                                       # 陈旧/不可信盘口不给挂单建议
    start = math.floor(m * 100 + 1e-9) / 100
    cap = math.floor((m + LIMIT_CAP_FRAC * (w - m)) * 100 + 1e-9) / 100
    return {"start": round(start, 2), "cap": round(max(cap, start), 2), "meta": LIMIT_META}


def price_bound_under(spec: dict, ok, *, qty: int, step: float = 0.01) -> float | None:
    """在 (0, 宽度) 内按 step 扫描带符号净价，找规则 ok(econ) 仍通过的最不利净价：
    借记 → 最高可付；贷记 → 最少需收（以负数返回）。一个都不通过 → None。ok 由调用方给（预算/下限）。
    qty 必填（Codex 052 I3：旧版永远按 1 组构造经济量，调用方要求 ≥2 组时任何价都不通过、边界恒为 None），
    econ 按实际请求组数构造，ok 收到的 econ["qty"] 就是请求组数。"""
    W = spec["width"]
    n = int(round(W / step))
    best = None
    for i in range(1, n):
        x = round(i * step, 4)
        net = x if spec["debit"] else -x
        e = economics(spec, net, qty=qty)
        if e is not None and ok(e):
            if spec["debit"]:
                best = net                                   # 付得越多越不利：取最大的通过值
            elif best is None:
                best = net                                   # 收得越少越不利：取最小的通过收入
    return best


def budget_candidate(spec: dict, econ: dict, underlying: str, cid: str) -> dict:
    """政策预算候选（risk_policy.evaluate_budget 接口：金额是【每组】，组数另给）。
    借记/贷记价差都不设硬止损时，计划止损损失 = 每组理论最大亏损（含费）；qty_requested = econ 的组数，
    组数只由 risk_policy 乘一次（Codex 052 I3：旧版把整笔金额当每组、又请求 qty 组，两组被算成四组）。"""
    return {"id": cid, "underlying": underlying, "unit_stop_loss": econ["unit_max_loss"],
            "unit_payoff_max_loss": econ["unit_max_loss"], "qty_requested": econ["qty"]}


def itm_proxy(spec: dict, long_delta: float | None, short_delta: float | None, econ: dict | None) -> dict | None:
    """delta 粗估的【价内概率粗略代理】（≈风险中性到期价内概率；不是真实概率，更不是本账户含费盈利概率）。
    借记：短腿到期价内 ≈ |Δ短|、长腿到期价内 ≈ |Δ长| —— 毛盈亏平衡在两行权价之间，故落在 [lo, hi]；
    贷记：短腿到期价外 ≈ 1−|Δ短|、长腿到期价外 ≈ 1−|Δ长|。
    缺 delta / delta 非有限或越出 [0,1] → None。含费最大收益 ≤ 0（或经济量不可计算）→ status="no_fee_profit"、
    不给区间（Codex 052：合成净收 0.01、往返费 3.20 时任何到期价都没有含费盈利，旧版仍报 80%–90%）。"""
    def ok(d):
        return isinstance(d, (int, float)) and not isinstance(d, bool) and math.isfinite(d) and abs(d) <= 1
    if not (ok(long_delta) and ok(short_delta)):
        return None
    if econ is None or not econ.get("fee_profit_possible"):
        return {"status": "no_fee_profit", "lo": None, "hi": None,
                "text": "含费最大收益 ≤ 0（或经济量不可计算）：任何到期价都没有含费盈利，不给概率代理"}
    dl, ds = abs(long_delta), abs(short_delta)
    if spec["debit"]:
        lo, hi = min(dl, ds), max(dl, ds)
        what = f"短腿到期价内≈{ds:.0%}、长腿到期价内≈{dl:.0%}"
    else:
        lo, hi = 1 - max(dl, ds), 1 - min(dl, ds)
        what = f"短腿到期价外≈{1 - ds:.0%}、长腿到期价外≈{1 - dl:.0%}"
    return {"status": "ok", "lo": round(lo, 3), "hi": round(hi, 3),
            "text": f"价内概率粗略代理 ≈ {lo:.0%}–{hi:.0%}（delta，风险中性近似；非真实概率、非含费盈利概率）："
                    f"{what}；含费到期盈亏平衡 {econ['breakeven']:.2f}"}


def roll_plan(round_dte: int, horizon_days: int) -> int:
    """卖方按 round_dte 天一轮滚动，horizon_days 内能做几轮（向下取整，至少 0）。"""
    if round_dte <= 0 or horizon_days <= 0:
        return 0
    return horizon_days // round_dte


def realtime_ratio(commodity_px, etf_px) -> float | None:
    """当日实时比值 = 商品价 / ETF 价（AGENTS 数据口径：不用静态乘数）。比值 ≠ 每份持有量。"""
    return commodity_px / etf_px if (_pos(commodity_px) and _pos(etf_px)) else None


def convert_level(level: float, ratio: float | None, *, to_commodity: bool) -> float | None:
    """位点换算：ETF → 商品（× 比值）或商品 → ETF（÷ 比值）。比值未知 → None。"""
    if ratio is None or not _pos(level):
        return None
    return level * ratio if to_commodity else level / ratio
