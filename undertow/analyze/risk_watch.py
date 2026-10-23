"""盘中持仓风险巡检（risk-watch-v1）—— 纯确定性，无 I/O（持仓、报价、计划、账户状态由调用方喂入）。

用户 2026-10-15：「以后每天需要评估一下持仓风险（实盘和模拟仓都要）。每天盘中定时检查风险，有需要提示的再通知我，
正常情况不用提示。这样我可以减少看盘，防止我的情绪化交易。」

所以这个模块只回答一件事：**此刻有没有需要人动手或知道的事**。正常情况输出为空列表——不报「一切正常」。

输入统一成 SpreadView（实盘与模拟仓同一口径）：
- 估值沿用模拟仓的平仓口径：value = 卖腿 ask − 买腿 bid（每股，带符号；贷记为正、借记为负），
  含费盈亏 = −spread_econ.loss_at_value(econ, value)。实盘与模拟仓用同一个经济量函数（spread_econ 唯一来源）。
- 估值缺失（任一所需一侧缺价）→ value None，不折零，不据此触发止盈/止损；连续缺失 ≥ N 次才报「盘口缺失」
  （开盘头几分钟单边空档是常态，首次缺失不报）。
- 买腿缺 bid 时模拟仓按 0 计并给出估值假设（close_value 的 assume）；有假设的估值只用于「更坏」方向的告警（止损/失效），
  不用于止盈（止盈靠一个卖不出去的估值会误导）。

规则（全部阈值在 RISK_WATCH_META，未校准；只是「该提醒人看一眼」的工程开关，不是交易信号）：
1. 止盈到达：借记价差按 spread_econ.debit_roi_review（debit-roi100-review-v2-20261023，用户 2026-10-23、Codex 063-B 正式提醒政策）：
   净利润（可平仓对手价全平收入 − 预计平仓费 − 实际入场付出）≥ 实际入场付出（权利金 + 实际入场费）→ 提醒讨论，从不平仓。
   入场费优先取计划里核定的 entry_fees_usd，否则按往返费模型的一半（标「估计」）；100% 不可达时不报、也不换别的阈值。
   同标的还有其它在场腿时，两腿成本不代表整轮成本：子结构达标也只报「整轮 ROI 未核实」请讨论（Codex 063 T3）。
   或私有计划给的止盈价差值到达。贷记价差 含费盈利 ≥ POLICY_B 止盈比例 × 含费最大收益。
   旧 v1（分母 = 含往返费理论最大亏损）只留给已挂 v1 的模拟仓复现。
2. 计划失效位：私有计划的 invalidation（参照价可以是商品价如布伦特，也可以是标的）——盘中越过只报 warn「若收盘仍在外侧即失效」，
   临收盘窗口（当日收市前 close_warn_minutes 分钟起；收市时刻取 market_calendar，半日市 13:00）仍在外侧报 urgent，
   文案是「临收盘预警、尚未收盘确认」—— 15:30 不是收盘，巡检也不在收盘后确认。收市时刻未知 → 不升级并写明。
   参照价拿不到时只用计划里【显式写明】的标的等价位（underlying_equiv）兜底并标来源；都没有 = 本规则 missing（不是没事）。
3. 卖腿：贷记价差卖腿被触及/进入价内 → urgent，距卖腿 ≤ short_near_frac → warn。卖腿价内 ≠ 已亏损（Codex 052：
   合成净收 1、卖腿 100、标的 99.9、平仓成本 0.2 时含费仍浮盈）：提示分列当前含费估值、到期盈亏平衡与指派风险；
   到期 ≤ expiry_near_tdays 个交易日且卖腿近价或价内（借记、贷记都算）→ warn：钉价/指派风险。
   分腿平仓顺序（用户 2026-10-15 纠正：长桥组合单可以分腿平）：**先买回卖腿，再卖出买腿**，否则会留下裸卖。
4. 计划减损线：借记价差持有价值（−value）≤ 计划 stop_value → urgent；贷记价差平仓成本（value）≥ 止损线 → urgent，止损线取私有计划 stop_value（实盘），没有则取模拟仓自带的。
   借记价差「理论到期最大亏损 = 已付权利金」不等于没有处置风险：短腿仍可能被提前指派，产生股票头寸与资金占用（券商处置未核实）。
5. 复核日：计划 review_date 到达 → info（当天一次）。
6. 账户：任一币种现金为负 → warn；试行暂停已锁 → urgent；试行未建立/连续亏损不可判定/账本 incomplete → info；账本超 POLICY_B → urgent。

覆盖（Codex 052 I2）：assess_spread 同时给出逐规则覆盖状态 checked / missing / not_applicable（missing 带缺的输入）。
缺标的价、缺参照价且无显式等价位、缺成本/费用、日历未覆盖 = 核心缺口 → 调用方把本轮记为不完整（不是「无需提醒」）；
连续 ≥ quote_missing_runs 轮仍缺 → 「巡检不完整」提醒。盘口缺失沿用自身的连续计数去噪（达阈值才算核心缺口）。
成本来源分开记：plan（用户核定，私有计划）/ broker_estimate（券商成本价推算，部分减仓后会被改写，未核定）/ paper_fill（模拟成交）；
估算来源的止盈提醒明说「估算、未核定」。成本符号由行权价结构决定（借记付、贷记收），不再一律当付出。费用 0 合法；非有限/负数拒绝。

去重与送达（Codex 052 I1）：巡检与投递分离。select_new 只决定「本轮要不要把它【写进私有发件箱】」——同 key 同一美东日
只入箱一次、严重度升级再入箱一次；入箱之后的投递由调用方按发件箱 pending / ack 做（未确认的每轮都重新给出，
与下一轮条件是否仍然存在无关）。本模块只给纯函数：alert_id（稳定身份）、enqueued_map（当日已入箱的最高严重度）。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field

from datetime import datetime

from undertow.analyze import debit_advisory as da
from undertow.analyze import risk_policy as rp
from undertow.analyze import spread_econ as se

RISK_WATCH_META = {
    "version": "risk-watch-v1-20261015",
    "status": "未校准：全部是提醒人看一眼的工程开关，不是交易信号；改动请同步测试",
    "debit_take_profit_rule": se.DEBIT_ROI_REVIEW["version"],   # 唯一公式 spread_econ.debit_roi_review（Codex 063-B）
    "credit_take_profit_frac": rp.POLICY_B["take_profit_gain_frac"],   # 唯一来源 risk_policy（POLICY_B 90%）
    "short_near_frac": 0.01,               # 标的距卖腿 ≤ 1% 算「近价」（未校准）
    "expiry_near_tdays": 2,                # 距到期 ≤ 2 个交易日（含到期日当天 = 0）
    "quote_missing_runs": 2,               # 连续 ≥ 2 次巡检盘口缺失才报
    "close_warn_minutes": 30,              # 临收盘预警窗：当日收市（market_calendar.close_time）前 30 分钟起（未校准）
}
SEVERITY_RANK = {"info": 1, "warn": 2, "urgent": 3}
LEG_ORDER_NOTE = "分腿平仓须先买回卖腿、再卖出买腿（否则留下裸卖）；也可整组一起平"
PLAN_SCHEMA = "position-plans-v1"
INVALIDATION_OPS = ("<", ">")


@dataclass(frozen=True)
class RiskItem:
    key: str                  # 稳定身份：规则|仓位 key（同 key 同日只报一次）
    severity: str             # info / warn / urgent
    scope: str                # live / paper / account
    title: str                # 一行人读结论（通知标题）
    detail: str = ""
    numbers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SpreadView:
    """一组垂直价差的此刻快照。entry_credit 每股带符号（贷记 > 0，借记 < 0 = −付出）。"""
    key: str
    scope: str                # live / paper
    label: str
    underlying: str           # 期权根，如 USO
    expiry: str               # YYYY-MM-DD
    side: str                 # C / P
    k_sell: float
    k_buy: float
    entry_credit: float | None   # None = 成本缺失（不折零）
    qty: int
    fee_round_trip: float | None   # 每组往返费用（美元）；0 合法，None/非有限/负数 = 不合法
    value: float | None = None              # 平仓价值（每股，卖腿 ask − 买腿 bid）；None = 估值缺失
    value_assumption: str | None = None     # 估值假设（如买腿 bid 缺失按 0 计）
    spot: float | None = None               # 标的现价
    tdays_to_expiry: int | None = None      # 距到期交易日数（到期当天 = 0）；None = 日历未覆盖
    stop_value: float | None = None         # 模拟仓自带止损线（平仓价值口径，与 value 同号）
    missing_streak: int = 0                 # 连续估值缺失次数（含本次）
    cost_source: str = "unknown"            # plan（用户核定）/ broker_estimate（券商成本推算，未核定）/ paper_fill
    unit_note: str | None = None            # 同标的还有其它在场腿/价差（整体风险单元）→ 两腿成本不代表整轮成本
    advisory_spec: dict | None = None       # 模拟仓记录（挂的 advisory_rule / 生效时刻 / 事件）；借记提示按它分派（Codex 064 T1）


def econ(v: SpreadView) -> dict:
    return se.vertical(side=v.side, k_sell=v.k_sell, k_buy=v.k_buy, entry_credit=v.entry_credit, qty=v.qty,
                       fee_round_trip=v.fee_round_trip)


def _fin(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def pnl_usd(v: SpreadView, e: dict | None = None) -> float | None:
    """含费估值盈亏（整笔）；估值缺失 → None。"""
    if v.value is None:
        return None
    e = e or econ(v)
    loss = se.loss_at_value(e, v.value)
    return None if loss is None else round(-loss, 2)


def debit_review(v: SpreadView, plan: dict | None) -> dict:
    """借记价差的 100% 含费提醒（spread_econ.debit_roi_review 唯一公式）。入场费：计划核定的 entry_fees_usd（实际）优先，
    否则按往返费模型的一半（估计）；平仓费恒为模型估计。估值有假设（某腿缺价按 0）→ 当作报价缺失，不触发。"""
    plan = plan or {}
    fe = v.fee_round_trip if _fin(v.fee_round_trip) and v.fee_round_trip >= 0 else None
    fi_plan, qe = plan.get("entry_fees_usd"), plan.get("qty_entered")
    qe = qe if isinstance(qe, int) and not isinstance(qe, bool) and qe > 0 else None
    scope_note = None
    if qe is None:          # 不默认为当前组数（Codex 064 T2）：不知道入场后有没有减仓，就不说整轮
        scope_note = "入场组数未登记（plan-set --qty-entered），可能有过部分减仓：只算当前两腿子结构，整轮 ROI 未核实"
    elif qe != v.qty:
        scope_note = (f"入场 {qe} 组、现 {v.qty} 组：数量有变化，只算剩余 {v.qty} 组子结构"
                      f"（已平部分的真实现金未计入），整轮 ROI 未核实")
    if _fin(fi_plan) and fi_plan >= 0:
        if qe is not None and qe != v.qty:   # 原整笔入场费不整笔套给剩余组：按组数比例分摊，是估计
            fi, fi_src = round(float(fi_plan) * v.qty / qe, 2), "allocated"
        else:
            fi, fi_src = float(fi_plan), "actual"
    else:
        fi, fi_src = (None if fe is None else fe / 2 * v.qty), "model"
    cost = -v.entry_credit if _fin(v.entry_credit) and v.entry_credit < 0 else None
    vx = -v.value if v.value is not None and v.value_assumption is None else None
    r = se.debit_roi_review(side=v.side, k_sell=v.k_sell, k_buy=v.k_buy, cost=cost, qty=v.qty, entry_fees=fi,
                            exit_fees=None if fe is None else fe / 2 * v.qty, value_exec=vx,
                            entry_fee_source=fi_src, exit_fee_source="model")
    return {**r, "unit_note": v.unit_note, "scope_note": scope_note}


FEE_SOURCE_TEXT = {"actual": "", "allocated": "（入场费按组数比例分摊，估计）", "model": "（入场费为估计）",
                   "paper_model": "（模拟仓按往返费模型）"}


def debit_review_title(v: SpreadView, r: dict, est: str = "") -> str:
    fee = FEE_SOURCE_TEXT.get(r["entry_fee_source"], "（入场费来源未知）")
    if r.get("unit_note") or r.get("scope_note"):
        return (f"{v.label}：两腿子结构含费净利润 ${r['profit_usd']:,.0f} ≥ 实际付出 ${r['invest_usd']:,.0f}（≥100%）"
                f"——{'同标的还有其它腿' if r.get('unit_note') else '组数未核'}，整轮 ROI 未核实，请讨论{fee}"
                f"{'（成本估算）' if est else ''}")
    return (f"{v.label}：含费净利润 ${r['profit_usd']:,.0f} ≥ 实际入场付出 ${r['invest_usd']:,.0f}"
            f"（ROI {r['roi']:.0%}，≥100%），提醒讨论是否止盈{fee}{'（成本估算）' if est else ''}")


def debit_review_body(v: SpreadView, r: dict, est: str = "") -> str:
    sc = f"拿到期满额再多 ${r['remaining_gross_usd']:,.0f}；归零回吐 ${r['giveback_gross_usd']:,.0f}"
    if r.get("hold_close_breakeven") is not None:
        sc += f"；到期标的 {r['hold_close_breakeven']:g} 时与现在平相同"
    return (f"{r['rule']}：用户 2026-10-23 的提醒政策（收益效果未校准），只提醒、平不平由你决定。"
            f"按可平仓对手价 {r['value_exec']:.2f}/股（触发净价 {r['trigger_value']:.3f}；"
            f"按假设最小报价单位 0.01 向上取整的参考价 {r['trigger_quote']:.2f}，券商报价单位未核实）。"
            f"情景（无费、到期口径，是幅度不是概率）：{sc}。平仓费为模型估计。"
            f"{('同标的整体：' + r['unit_note'] + '。') if r.get('unit_note') else ''}"
            f"{(r['scope_note'] + '。') if r.get('scope_note') else ''}{est}{LEG_ORDER_NOTE}")


def _paper_debit_advisory(v: SpreadView, at: datetime | None, rules: dict, nums: dict, out: list, est: str) -> None:
    """模拟仓借记价差：按记录挂的提示规则与生效时刻分派（debit_advisory，与模拟仓入口同一函数；Codex 064 T1）。
    没挂规则 → 不适用；未知/不适用的规则 → missing(advisory_rule)；未生效 → 不适用、不提醒；越界报价 → 复核提醒、不认证止盈。"""
    a = da.advisory_view(v.advisory_spec, v.value, v.value_assumption, at) if v.advisory_spec is not None else None
    nums["paper_advisory"] = a
    if a is None:
        rules["take_profit"] = {"status": NA, "why": "模拟仓未挂提示规则"}
        return
    if a.get("config_error"):
        rules["take_profit"] = {"status": MISSING, "input": "advisory_rule", "why": a["config_error"]}
        return
    if a.get("quote_status") == "quote_out_of_range":
        rules["take_profit"] = {"status": MISSING, "input": "quote_quality", "why": a["why"]}
        out.append(RiskItem(f"quote_anomaly|{v.key}", "warn", v.scope, f"{v.label}：可平仓报价超出价差范围，待复核",
                            a["why"] + "；止盈提示本轮未判定（不是没到线）", nums))
        return
    if a["before_effective"]:
        rules["take_profit"] = {"status": NA, "why": f"提示规则 {a['rule']} 尚未生效（{a.get('effective_at')}）"}
        return
    if not a["triggered"]:
        return
    if a.get("review"):                                  # v2：与实盘同一文案
        r = {**a["review"], "unit_note": None, "scope_note": None}
        out.append(RiskItem(f"take_profit|{v.key}", "warn", v.scope, debit_review_title(v, r, est),
                            debit_review_body(v, r, est), nums))
    else:                                                # v1：历史规则原口径（分母 = 含费风险成本）
        out.append(RiskItem(f"take_profit|{v.key}", "warn", v.scope,
                            f"{v.label}：含费盈利 ${a['pnl_usd']:,.0f} ≥ 含费风险成本 ${a['risk_cost_usd']:,.0f}"
                            f"（{a['rule']}，{a['roi']:.0%}），考虑止盈（只提示）",
                            f"{a['rule']}（未校准，只提示、从不平仓）。{LEG_ORDER_NOTE}", nums))


# —— 私有持仓计划（data/account/position_plans.json）——

def validate_plans(obj) -> str | None:
    """计划文件 schema 校验；None = 合格。不合格的计划文件整体不用（调用方报告，不静默当作没计划）。"""
    if not isinstance(obj, dict) or obj.get("schema") != PLAN_SCHEMA or not isinstance(obj.get("plans"), dict):
        return f"计划文件 schema 不是 {PLAN_SCHEMA} 或缺 plans"
    for k, p in obj["plans"].items():
        if not isinstance(p, dict):
            return f"{k}：计划不是对象"
        for f in ("take_profit_value", "stop_value", "cost_per_share", "fee_round_trip", "entry_fees_usd"):
            if p.get(f) is not None and not (_fin(p[f]) and p[f] >= 0):
                return f"{k}：{f} 必须是非负有限数"
        qe = p.get("qty_entered")
        if qe is not None and not (isinstance(qe, int) and not isinstance(qe, bool) and qe > 0):
            return f"{k}：qty_entered 必须是正整数（按成交核定的入场组数）"
        if p.get("cost_side") is not None and p["cost_side"] not in ("debit", "credit"):
            return f"{k}：cost_side 只能是 debit / credit"
        inv = p.get("invalidation")
        if inv is not None:
            if not isinstance(inv, dict) or inv.get("op") not in INVALIDATION_OPS or not _fin(inv.get("level")) \
                    or not isinstance(inv.get("ref"), str) or not inv["ref"].strip():
                return f"{k}：invalidation 需要 ref（参照代码）、op（< 或 >）、level（有限数）"
            if inv.get("basis", "close") not in ("close", "intraday"):
                return f"{k}：invalidation.basis 只能是 close / intraday"
            if inv.get("underlying_equiv") is not None and not _fin(inv["underlying_equiv"]):
                return f"{k}：invalidation.underlying_equiv 必须是有限数"
        rd = p.get("review_date")
        if rd is not None and not (isinstance(rd, str) and len(rd) == 10 and rd[4] == "-" and rd[7] == "-"):
            return f"{k}：review_date 必须是 YYYY-MM-DD"
    return None


def _crossed(op: str, price: float, level: float) -> bool:
    return price < level if op == "<" else price > level


# —— 逐仓位规则 ——

CHECKED, MISSING, NA = "checked", "missing", "not_applicable"
CORE_INPUTS = ("spot", "ref", "cost", "fee", "econ", "calendar", "plan")   # 缺了 = 本轮不完整（quote 另按连续计数）
GAP_ALERT_INPUTS = ("spot", "ref", "calendar")
COST_SOURCE_TEXT = {"plan": "计划登记成本（用户核定）", "broker_estimate": "券商成本价推算（部分减仓后会被改写，未核定）",
                    "paper_fill": "模拟成交价", "unknown": "来源未知"}


def _minus(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    t = max(h * 60 + m - minutes, 0)
    return f"{t // 60:02d}:{t % 60:02d}"


def evaluate_spread(v: SpreadView, plan: dict | None, *, today: str, now_hm: str, ref_prices: dict | None = None,
                    close_hm: str | None = None, meta: dict = RISK_WATCH_META) -> list[RiskItem]:
    """一组价差此刻需要提醒的事；正常 → []。覆盖状态见 assess_spread。"""
    return assess_spread(v, plan, today=today, now_hm=now_hm, ref_prices=ref_prices, close_hm=close_hm, meta=meta)[0]


def assess_spread(v: SpreadView, plan: dict | None, *, today: str, now_hm: str, ref_prices: dict | None = None,
                  close_hm: str | None = None, meta: dict = RISK_WATCH_META,
                  plan_problem: str | None = None, at: datetime | None = None) -> tuple[list[RiskItem], dict]:
    """→ (提醒, 覆盖)。today = 美东日期，now_hm = 美东 HH:MM，close_hm = 当日收市 HH:MM（market_calendar；None = 未知）。
    覆盖 = {"key","scope","label","rules": {规则: {"status", "input"?, "why"?, "source"?}}}。
    plan_problem：计划文件存在但读不了/不合格（Codex 054 R1）。此时计划规则（计划止损、失效位、复核日）记 missing(input="plan")
    ——不知道计划里写了什么，不能当成「没有计划所以不适用」；不依赖计划的规则照常检查。计划文件不存在 = 没有计划（合法），不传。"""
    out: list[RiskItem] = []
    rules: dict = {}
    plan = plan or {}
    pmiss = None if plan_problem is None else {"status": MISSING, "input": "plan", "why": f"计划文件不合格，计划规则未决（{plan_problem}）"}
    nums = {"value": v.value, "spot": v.spot, "entry_credit": v.entry_credit, "qty": v.qty, "cost_source": v.cost_source}
    debit = se.is_debit(v.side, v.k_sell, v.k_buy)
    cost_ok, fee_ok = _fin(v.entry_credit), _fin(v.fee_round_trip) and v.fee_round_trip >= 0
    rules["cost"] = {"status": CHECKED, "source": v.cost_source} if cost_ok else \
        {"status": MISSING, "input": "cost", "why": "成本缺失（计划没登记、券商成本价也不可用）"}
    rules["fee"] = {"status": CHECKED} if fee_ok else \
        {"status": MISSING, "input": "fee", "why": "往返费用缺失或不合法（须有限且 ≥0）"}
    e = econ(v) if cost_ok and fee_ok else None
    econ_ok = bool(e and e.get("structure") and e.get("entry_net_payoff_max_loss") is not None)
    if cost_ok and fee_ok and not econ_ok:
        rules["cost"] = {"status": MISSING, "input": "econ", "why": f"成本与结构不符：{(e or {}).get('error')}"}
    if not econ_ok:
        why = "；".join(r["why"] for r in (rules["cost"], rules["fee"]) if r["status"] == MISSING)
        out.append(RiskItem(f"econ_invalid|{v.key}", "warn", v.scope, f"{v.label}：经济量算不出（{why}）",
                            "本仓位的止盈/止损巡检未执行（不是没事）；卖腿、失效位、到期仍照常检查", nums))
    L = e.get("entry_net_payoff_max_loss") if econ_ok else None
    M = e.get("entry_net_max_gain") if econ_ok else None
    be = e.get("breakeven") if econ_ok else None
    pnl = pnl_usd(v, e) if econ_ok else None
    nums.update(pnl_usd=pnl, max_loss_usd=L, max_gain_usd=M, breakeven=be)
    est = "（成本按券商成本价估算，未核定；以现金流水为准）" if v.cost_source == "broker_estimate" else ""

    # 盘口缺失：连续 N 次才报
    if v.value is None and v.missing_streak >= meta["quote_missing_runs"]:
        out.append(RiskItem(f"quote_missing|{v.key}", "warn", v.scope,
                            f"{v.label}：连续 {v.missing_streak} 次拿不到可平仓报价",
                            "估值未知不等于没事：止盈/止损巡检暂停，请在券商端看一眼盘口", nums))
    qmiss = {"status": MISSING, "input": "quote", "why": "可平仓估值缺失", "streak": v.missing_streak}

    clean = v.value is not None and v.value_assumption is None      # 止盈只认没有估值假设的报价
    # 1. 止盈
    tpv = plan.get("take_profit_value")
    if not econ_ok and not (_fin(tpv) and debit):
        rules["take_profit"] = {"status": MISSING, "input": "econ", "why": "经济量算不出"}
    elif v.value is None:
        rules["take_profit"] = qmiss
    elif not clean:
        rules["take_profit"] = {"status": MISSING, "input": "quote_assumption", "why": f"估值含假设（{v.value_assumption}），不用于止盈"}
    else:
        rules["take_profit"] = {"status": CHECKED, "source": v.cost_source}
    if clean and pnl is not None:
        if debit and v.scope == "paper":
            _paper_debit_advisory(v, at, rules, nums, out, est)
        elif debit:
            r = debit_review(v, plan)
            nums["debit_roi_review"] = r
            if r["status"] == "quote_out_of_range":
                rules["take_profit"] = {"status": MISSING, "input": "quote_quality", "why": r["why"]}
                out.append(RiskItem(f"quote_anomaly|{v.key}", "warn", v.scope, f"{v.label}：可平仓报价超出价差范围，待复核",
                                    r["why"] + "；止盈提醒本轮未判定（不是没到线）", nums))
            if r["triggered"]:
                out.append(RiskItem(f"take_profit|{v.key}", "warn", v.scope, debit_review_title(v, r, est),
                                    debit_review_body(v, r, est), nums))
        elif not debit and _fin(M) and M > 0 and round(pnl, 2) >= round(meta["credit_take_profit_frac"] * M, 2):
            out.append(RiskItem(f"take_profit|{v.key}", "warn", v.scope,
                                f"{v.label}：已拿到最大收益的 {pnl / M:.0%}（≥{meta['credit_take_profit_frac']:.0%} 止盈线）"
                                f"{'（估算）' if est else ''}",
                                f"POLICY_B 止盈 {meta['credit_take_profit_frac']:.0%}。{est}{LEG_ORDER_NOTE}", nums))
    if clean and _fin(tpv) and debit and -v.value >= tpv and not any(i.key == f"take_profit|{v.key}" for i in out):
        out.append(RiskItem(f"take_profit|{v.key}", "warn", v.scope,
                            f"{v.label}：价差可平仓价值 {-v.value:.2f} ≥ 计划止盈 {tpv:.2f}",
                            f"私有计划止盈。{LEG_ORDER_NOTE}", nums))

    # 4. 减损/止损线（估值有假设也照报：假设只会让估值更差，报出来让人去看）
    sv = plan.get("stop_value")
    # 贷记价差：私有计划的止损线优先（实盘），否则用模拟仓自带的。旧版只认模拟仓自带，实盘贷记写了 plan stop 也静默不报
    csv = sv if (not debit and _fin(sv)) else v.stop_value
    stop_applies = (debit and _fin(sv)) or (not debit and _fin(csv))
    rules["stop"] = (pmiss or {"status": NA}) if not stop_applies else (qmiss if v.value is None else {"status": CHECKED})
    if v.value is not None and _fin(sv) and debit and -v.value <= sv:
        out.append(RiskItem(f"stop|{v.key}", "urgent", v.scope,
                            f"{v.label}：可平仓价值 {-v.value:.2f} ≤ 计划减损线 {sv:.2f}",
                            "按计划考虑减损离场。整组持有到期的理论最大亏损 = 已付权利金" +
                            (f"（含费 ${L:,.0f}）" if _fin(L) else "") +
                            "；但理论限损不等于没有处置风险：短腿仍可能被提前指派，产生股票头寸与资金占用（券商处置未核实）" +
                            (f"；估值假设：{v.value_assumption}" if v.value_assumption else ""), nums))
    if v.value is not None and _fin(csv) and not debit and v.value >= csv:
        out.append(RiskItem(f"stop|{v.key}", "urgent", v.scope,
                            f"{v.label}：平仓成本 {v.value:.2f} ≥ 止损线 {csv:.2f}",
                            ("模拟仓止损规则会在下一个盯市时点按其体检执行" if v.scope == "paper" else
                             "按私有计划止损：本工具只提醒、从不下单。" + LEG_ORDER_NOTE) +
                            (f"；估值假设：{v.value_assumption}" if v.value_assumption else ""), nums))

    # 2. 计划失效位
    inv = plan.get("invalidation")
    if not isinstance(inv, dict):
        rules["invalidation"] = pmiss or {"status": NA}
    else:
        px = (ref_prices or {}).get(inv["ref"])
        level, ref, basis_note = inv["level"], inv["ref"], ""
        if _fin(px):
            rules["invalidation"] = {"status": CHECKED, "source": "ref"}
        elif _fin(inv.get("underlying_equiv")) and _fin(v.spot):
            px, level, ref = v.spot, inv["underlying_equiv"], v.underlying
            basis_note = f"（{inv['ref']} 取不到，按计划写明的标的等价位 {level:g} 判断）"
            rules["invalidation"] = {"status": CHECKED, "source": "underlying_equiv"}
        else:
            px = None
            rules["invalidation"] = {"status": MISSING, "input": "ref",
                                     "why": f"参照价 {inv['ref']} 取不到，且" +
                                            ("计划没有显式的标的等价位" if not _fin(inv.get("underlying_equiv")) else "标的价也缺失")}
        if _fin(px) and _crossed(inv["op"], px, level):
            intraday = inv.get("basis", "close") == "intraday"
            late = intraday or (close_hm is not None and now_hm >= _minus(close_hm, meta["close_warn_minutes"]))
            nums.update(ref=ref, ref_price=px, level=level, close_hm=close_hm)
            if intraday:
                tail = ""
            elif late:
                tail = f"（临收盘预警：今日 {close_hm} 收市，尚未收盘确认）"
            else:
                tail = "（盘中，收盘仍在外侧即失效）" + ("" if close_hm else "；今日收市时刻未知，不按临收盘升级")
            out.append(RiskItem(f"invalidation|{v.key}", "urgent" if late else "warn", v.scope,
                                f"{v.label}：{ref} {px:g} {'跌破' if inv['op'] == '<' else '突破'}失效位 {level:g}{tail}",
                                (inv.get("note") or "看涨/看跌前提被打破，按计划考虑离场") + basis_note, nums))

    # 3. 卖腿与到期
    ks = v.k_sell
    itm = near = None
    if _fin(v.spot):
        itm = v.spot >= ks if v.side == "C" else v.spot <= ks
        dist = abs(v.spot - ks) / ks if ks else None
        near = dist is not None and dist <= meta["short_near_frac"]
        nums.update(short_strike=ks, short_dist_frac=None if dist is None else round(dist, 4))
    spot_miss = {"status": MISSING, "input": "spot", "why": "标的价缺失"}
    rules["short_leg"] = {"status": NA} if debit else ({"status": CHECKED} if itm is not None else spot_miss)
    if not debit and itm:
        pnl_txt = f"按当前平仓价含费估值 {pnl:+,.2f} 美元" if pnl is not None else "当前含费估值未知"
        out.append(RiskItem(f"short_itm|{v.key}", "urgent", v.scope,
                            f"{v.label}：标的 {v.spot:g} 已越过卖腿 {ks:g}（卖腿价内，不等于已亏损）",
                            f"{pnl_txt}{est}；到期盈亏平衡 {v.underlying} " + (f"{be:g}" if _fin(be) else "未知") +
                            "。价内越深，到期亏损与提前指派风险越大；按计划止损或减仓。" + LEG_ORDER_NOTE, nums))
    elif not debit and near:
        out.append(RiskItem(f"short_near|{v.key}", "warn", v.scope,
                            f"{v.label}：标的 {v.spot:g} 距卖腿 {ks:g} 仅 {dist:.1%}", LEG_ORDER_NOTE, nums))
    if v.tdays_to_expiry is None:
        rules["expiry"] = {"status": MISSING, "input": "calendar", "why": "交易日历未覆盖到期日"}
    elif v.tdays_to_expiry > meta["expiry_near_tdays"]:
        rules["expiry"] = {"status": CHECKED}
    else:
        rules["expiry"] = {"status": CHECKED} if itm is not None else spot_miss
        if itm or near:
            out.append(RiskItem(f"expiry_pin|{v.key}", "warn", v.scope,
                                f"{v.label}：{v.tdays_to_expiry} 个交易日后到期，卖腿 {ks:g} {'价内' if itm else '近价'}"
                                f"（标的 {v.spot:g}）",
                                ("借记价差卖腿价内 = 接近最大收益，不是亏损；风险在到期钉价/指派处置：决定到期前整组平仓兑现还是持有到结算。"
                                 if debit and itm else "到期钉价/指派风险：决定到期前平仓还是持有到结算。") + LEG_ORDER_NOTE, nums))

    # 5. 复核日
    rd = plan.get("review_date")
    rules["review"] = {"status": CHECKED} if isinstance(rd, str) else (pmiss or {"status": NA})
    if isinstance(rd, str) and rd <= today:
        out.append(RiskItem(f"review|{v.key}|{rd}", "info", v.scope, f"{v.label}：到计划复核日 {rd}",
                            plan.get("review_note") or "按计划重新评估是否继续持有", nums))
    return out, {"key": v.key, "scope": v.scope, "label": v.label, "rules": rules}


def core_gaps(cov: dict, *, quote_streak: int = 0, inputs: tuple = CORE_INPUTS, meta: dict = RISK_WATCH_META) -> list[str]:
    """覆盖里的核心缺口（→ 本轮不完整）：inputs 的 missing；盘口缺失只在连续达阈值时算。→ ["规则：原因"]。
    「巡检不完整」提醒只用 inputs=GAP_ALERT_INPUTS（成本/费用已有 econ_invalid、盘口已有 quote_missing 提醒，不重复）。"""
    out = []
    for rule, r in cov["rules"].items():
        if r["status"] != MISSING:
            continue
        if r.get("input") in inputs or (r.get("input") == "quote" and quote_streak >= meta["quote_missing_runs"]):
            out.append(f"{rule}：{r.get('why', '')}")
    return sorted(set(out))


def coverage_item(v: SpreadView, gaps: list[str], streak: int) -> RiskItem:
    """连续 N 轮仍有核心缺口 → 「巡检不完整」提醒（缺什么、哪些规则没检查）。"""
    return RiskItem(f"coverage_incomplete|{v.key}", "warn", v.scope,
                    f"{v.label}：巡检不完整（连续 {streak} 轮），{len(gaps)} 项未检查",
                    "；".join(gaps)[:300] + "。未检查不等于没事：请在券商端看一眼，或补齐计划参照/等价位/成本", {"gaps": gaps})


# —— 账户层 ——

def evaluate_account(*, cash_by_ccy: dict | None, trial: dict | None, book: dict | None) -> list[RiskItem]:
    """cash_by_ccy：{币种: 可用现金}；trial：{"state": "absent|ok|error", "paused": bool|None, "consecutive_unknown": bool,
    "note": str}；book：{"overall": pass|fail|incomplete, "failed": [检查名]}。任一为 None = 没取到（报 info，不当作没事）。"""
    out: list[RiskItem] = []
    if cash_by_ccy is None:
        out.append(RiskItem("account_unknown|cash", "info", "account", "账户现金未读到：现金类检查没做",
                            "不是通过；下一轮会重试"))
    else:
        for ccy, amt in sorted(cash_by_ccy.items()):
            if _fin(amt) and amt < 0:
                out.append(RiskItem(f"cash_negative|{ccy}", "warn", "account", f"{ccy} 现金为负：{amt:,.2f}",
                                    "相当于借入该币种；可在券商 App 用其它币种换汇补正（是否计息以券商规则为准，未核实）",
                                    {"currency": ccy, "amount": amt}))
    if trial is None:
        out.append(RiskItem("trial_unknown", "info", "account", "试行状态未读到", "暂停/连续亏损判据未检查（不是通过）"))
    elif trial.get("state") == "absent":
        out.append(RiskItem("trial_absent", "info", "account", "实盘试行未建立（undertow trial init / fund）",
                            "暂停 latch 与连续亏损计数都还没有生效"))
    elif trial.get("state") == "error":
        out.append(RiskItem("trial_error", "urgent", "account", "试行状态文件异常：新增冻结", trial.get("note") or ""))
    else:
        if trial.get("paused"):
            out.append(RiskItem("trial_paused", "urgent", "account", "实盘试行已暂停（latch）：不开新仓",
                                trial.get("note") or "只有用户明确决定才解除（undertow trial resume）"))
        elif trial.get("paused") is None:
            out.append(RiskItem("trial_pause_unknown", "info", "account", "试行暂停状态不可判定：冻结新增",
                                trial.get("note") or ""))
        if trial.get("consecutive_unknown"):
            out.append(RiskItem("trial_consecutive_unknown", "info", "account",
                                "连续亏损不可判定：有成交未登记/未核对（undertow trial trade / verify-trades）",
                                trial.get("note") or ""))
    if book is None:
        out.append(RiskItem("book_unknown", "info", "account", "POLICY_B 账本未算出", "限额检查没做（不是通过）"))
    elif book.get("overall") == "fail":
        out.append(RiskItem("book_fail", "urgent", "account", "现有持仓超出 POLICY_B 限额",
                            "；".join(book.get("failed") or [])[:200]))
    elif book.get("overall") == "incomplete":
        out.append(RiskItem("book_incomplete", "info", "account", "POLICY_B 账本不完整（有成员金额未知）",
                            "；".join(book.get("failed") or [])[:200]))
    return out


# —— 去重与送达 ——

STATE_SCHEMA = "risk-watch-state-v2"


def new_state(today: str) -> dict:
    """巡检状态只剩连续缺失计数。v1 的 sent（已送达）挪进只追加的发件箱/确认账（Codex 052 I1）：
    v1 由 run 与 deliver 各自整文件覆盖，run 拿着旧快照写回会抹掉 deliver 刚写的 ack。"""
    return {"schema": STATE_SCHEMA, "date": today, "missing_streak": {}}


def roll_state(state: dict | None, today: str) -> dict:
    """跨日：连续缺失计数保留（跨日也是「连续」）。v1 状态迁移：只取 missing_streak，sent 丢弃
    （上线当天已送达过的告警会再入箱一次 —— 宁可重复一次，不漏）。"""
    if not isinstance(state, dict) or state.get("schema") not in ("risk-watch-state-v1", STATE_SCHEMA):
        return new_state(today)
    s = new_state(today)
    s["missing_streak"] = dict(state.get("missing_streak") or {})
    return s


def bump_missing(state: dict, key: str, missing: bool) -> int:
    """更新连续缺失计数并返回本次后的值（缺失 +1，拿到估值清零）。"""
    n = (state["missing_streak"].get(key, 0) + 1) if missing else 0
    if n:
        state["missing_streak"][key] = n
    else:
        state["missing_streak"].pop(key, None)
    return n


def alert_id(today: str, item: RiskItem) -> str:
    """发件箱身份：美东日 × key × 严重度。同日同 key 同严重度只有一个身份（重复巡检不重复入箱），升级是新身份。"""
    return f"{today}|{item.key}|{item.severity}"


def enqueued_map(rows: list[dict], today: str) -> dict:
    """发件箱行 → 当日每个 key 已入箱的最高严重度（只看 today：次日同 key 可再入箱一次）。"""
    out: dict = {}
    for r in rows:
        if r.get("date") != today:
            continue
        if SEVERITY_RANK.get(r.get("severity"), 0) > SEVERITY_RANK.get(out.get(r.get("key")), 0):
            out[r["key"]] = r["severity"]
    return out


def select_new(items: list[RiskItem], enqueued: dict) -> list[RiskItem]:
    """本轮需要【入箱】的：当日该 key 没入箱过，或严重度高于已入箱的。同一轮里同 key 只取最高严重度一条。"""
    best: dict = {}
    for i in items:
        if SEVERITY_RANK[i.severity] > SEVERITY_RANK.get(enqueued.get(i.key), 0) and \
                SEVERITY_RANK[i.severity] > SEVERITY_RANK.get(best[i.key].severity if i.key in best else None, 0):
            best[i.key] = i
    return [i for i in items if best.get(i.key) is i]


def digest(ids: list[str]) -> str:
    """批次指纹（只含 alert_id = 日期|key|严重度，不含仓位数字）：兜底行可对账，重复送达可识别。"""
    return hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()[:12]


def as_dicts(items: list[RiskItem]) -> list[dict]:
    return [asdict(i) for i in items]
