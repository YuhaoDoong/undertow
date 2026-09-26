"""前瞻配对影子账（W05/W06，Codex 004 蓝图 H1~H3）—— 纯计算，无 I/O。

主实验只有一个比较：**墙选腿 A** vs **不看 OI、只按事前风险距离选腿 B**，同日同侧同到期同宽度。
每天每品种每侧产生一个事前冻结的「机会」，无候选也记原因；两腿报价必须晚于信号、盘中抓取；
结算分三种突破（窗末 / 期间收盘 / 盘中）与多种损益口径，互不代替。只读，从不下单。

═══ 预登记配置（2026-09-26 冻结；改动 = 新版本号，旧账不回写）═══
- 到期：T 起 2~4 DTE 的最近到期（沿用 v3，研究配置，不是最优证据）
- 宽度：卖腿再往虚值方向 2 个挂牌档（同时记美元宽度）
- A：gamma.local_wall(mode="max", band=5%, ≤14 天) —— W04 里用户实际在卖的那类墙
- B1（主对照）：离决策价 ≥ 1.0×ATR14 的第一个挂牌档。1.0 取自 W04 局部最大墙的历史距离中位（0.7~1.8 ATR），
  使 A/B 平均距离可比；A/B 逐日距离仍不同，比较解释为「规则整体」而非「同距离墙因果」。
- B2（次要）：2.0×ATR（Codex 决策登记的研究默认值）
- 两侧 P/C 都记；增仓方向、ATR 扩张、事件只作标签，**不过滤**
- 主终点：hold_quote_conservative（盘中两腿可成交侧报价入场、持有到期）÷ 最大风险；
  次要：exit_rule_quote_conservative（收盘越过卖腿 → 下一盘中窗口可成交侧报价平仓）、snapshot_model
- 费用：wall_spread.FEE_PER_TRADE 往返预算（4 个合约边），不重复计

═══ v2（2026-09-26，首个前瞻样本之前；v1 仅有回放行，不丢任何前瞻数据）═══
- B3 = 卖腿 |Δ| 最接近 0.20 的虚值档（快照 delta，业界常用；IV 高时自动放远）。
  理由：若 A 与 B1/B2 无差，Δ 规则是「策略该改成什么」的最自然候选，现在并行记录省一轮。
- 标签：卖腿 |Δ|、期限结构（目标到期 ATM IV − 约 30 天到期 ATM IV，>0 为倒挂）。
- 盘口两个时点：开盘窗（ET 10:00，入场 + 持仓标记）与收盘窗（ET 15:30，持仓标记）。
  由此新增次要口径 stop1x / stop2x：标记时点上保守平仓成本 ≥ (1+m)×入场权利金即按该报价平仓
  （离散监控，一天两次；不是连续止损）。
- 分侧统计与「只做增仓方向一侧」子集作为次要预登记分析。

═══ v3（2026-09-26，Codex 005 审查 R01/R02/R04/R05/R06 与 S00；仍在首个前瞻样本之前）═══
- 同美元宽度（R04）：宽度 = 2 × 目标到期、现价 ±5% 内最常见的挂牌间隔（不看 OI）；所有臂按同一美元宽度找保护腿，
  找不到记 no_same_width_protective（不可配对），不再「各自往外数两档」（v2 回放实测 8% 配对宽度不同）。
- 原始观测与派生分离：quote 只追加原始盘口尝试（每窗口可重试），入场/标记/退出/止损全部由 settle 从原始记录推出。
- 报价质量（R02）：卖腿 bid>0 且 bid_size>0、买腿 ask>0 且 ask_size>0、ask≥bid；每腿取窗口内「第一次通过质量检查」
  的尝试，不择优；源报价时间长桥 depth 不提供 → 记 unknown。
- 止损四态（R01）：未运行 / 运行缺价 / 有效未触发 / 有效触发；首次触发前任一应有窗口未知 → 该口径 None（path_unknown）。
- 平仓成本保留原值（R05）：超过宽度不置空，标 exceeds_width。
- 主终点改名（R06）：quote_entry_expiry_intrinsic = 报价入场—到期内在价值「研究收益」（不等于 ETF 实际到期处置）；
  新增 pre_expiry_exit_policy = 到期前最后一个交易日 ET 15:30 窗口按两腿保守报价整体平仓（小账户实际会执行的出场）。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics as st
from datetime import date, datetime
from zoneinfo import ZoneInfo

from undertow.core import market_calendar as mc

POOLS = {"etf": ["gold", "silver", "wti", "qqq", "tlt", "spy", "iwm"],
         "leveraged_etf": ["tqqq"],
         "single_stock": ["googl", "tsla", "nvda", "intc", "amd", "msft", "aapl"]}

CONFIG = {
    "version": "shadow-v5-20260926",
    # ── S00 实验身份（机器可读；改任一项 = 新版本）──
    "instruments": [k for pool in POOLS.values() for k in pool],
    # Codex 006：ETF / 杠杆 ETF / 个股不因样本多就混成一种策略证据 —— 分池冻结、分池报告，从不合并
    "pools": POOLS, "primary_pool": "etf",
    "aggregation": "池内每个 (品种, 侧, 入场日) 观测等权；不同池分开报告，不合并成一个结论",
    "primary_comparison": "A vs B1",
    # Codex 006 决定 1 + 007：唯一主终点 = 到期前最后交易日收盘窗的【退出政策】（拟执行，非「已证明更优」）
    "primary_endpoint": "pre_expiry_exit_policy",
    "primary_endpoint_definition": (
        "到期前最后交易日收盘窗：两腿报价都合格且长腿 bid>0 → 两腿整体平仓（点值）；"
        "长腿报价有效但 bid=0 → 只买回短腿、长腿持有至到期：到期虚值 → 残腿现金流 0（点值，依 OCC 按例外行权规则），"
        "到期实值 → 处置未知（损益只有上界，无下界）。点值与区间分开保存，不把残值零情景冒充已平仓收益。"),
    "short_only_policy": {
        "trigger": "长腿报价有效（无错误、ask>0、ask_size>0）且 bid==0",
        "residual": "长腿 1 张持有至到期，不再下任何指令",
        "otm_at_expiry": "模型处置情景：现金流 0（OCC exercise-by-exception 只自动行权实值 ≥$0.01 的期权，"
                         "https://www.optionseducation.org/referencelibrary/faq/options-exercise）；"
                         "假设无相反指令、券商按此处置，实际未核实（actual_confirmed=None）",
        "itm_at_expiry": "未知：会被自动行权成标的仓位，券商处置、费用与隔夜风险未核实 → 损益下界 None，上界 = 内在值",
        "permission": "未核实：组合单是否允许单独买回短腿取决于券商与入场方式（AGENTS.md 组合单 vs 分腿）",
        "fees": "仍按 $3.20 往返预算"},
    "estimates": {"primary": "bounds（点值或 [下界, 上界]；任一配对无界 → A−B 判定为未决）",
                  "conditional": "both_legs_only（两臂都为双腿整体退出的条件样本，不外推全部机会）",
                  "scenario": "residual0（残腿按 0 估值的情景，不是已实现收益）"},
    "secondary_endpoints": ["quote_entry_expiry_intrinsic", "close_beyond_next_open_exit",
                            "stop1x_twice_daily", "stop2x_twice_daily", "snapshot_model"],
    "quote": {"timezone": "America/New_York",
              # 决定 3：收盘窗由预存日历的【标的核心收市】倒推 30~15 分钟：正常 15:30–15:45，13:00 收市日 12:30–12:45
              "windows": {"open": {"start": "10:00", "end": "10:20"},
                          "close": {"rule": "core_close_minus", "minutes_before": [30, 15]}},
              "entry_window": "open",
              "selection": "first_valid_attempt_per_leg",
              "quality": {
                  "entry": "sell: 无错误, bid>0, bid_size>0, ask>=bid; buy: 无错误, ask>0, ask_size>0, ask>=bid",
                  "exit": ("sell: 无错误, ask>0, ask_size>0, ask>=bid; buy: 无错误, bid 与 ask 均为数, ask>=bid; "
                           "buy bid>0 须 bid_size>0 → both_legs；buy bid==0 须 ask>0 且 ask_size>0 → short_only；"
                           "保护腿报错/缺失/缺 ask/零量 → 该窗口缺失，不计任何收入")},
              "source_timestamp": "unknown (longbridge depth 不提供)"},
    "calendar": {"version": mc.VERSION, "hash": mc.calendar_hash(),
                 "source": mc.SOURCE["url"], "read_at": mc.SOURCE["read_at"]},
    "calendar_policy": ("交易日与收市时刻只来自预存交易所日历（core.market_calendar），不从日线倒推；"
                        "日历覆盖外 → calendar_unknown；交易日缺日线 → bars_incomplete"),
    "maturity_policy": "各终点独立成熟：提前退出在其退出窗结束后即可结算，不等到期；到期类终点等到期日日线",
    "fee_policy": "每张每腿 $0.80，4 个合约边往返预算 $3.20；持有到期也保守计同一预算；真实费率待成交记录核实",
    "missing_policy": "未知一律 None，不折零；缺价不偷换为持有到期",
    "stats": {"version": "stats-v2", "block": "circular_moving_block_over_calendar_trading_days",
              "block_days": 5, "sensitivity_block_days": 10, "iters": 20000, "seed": 20260926,
              # 计算保护，不是统计充分性证明（Codex C04）：有样本的日期数 // 块长 ≥ 4
              "min_full_blocks": 4,
              "degenerate": "样本值全等或区间零宽 → degenerate，不判定",
              "non_overlap_sensitivity": "同品种同侧：入场日须晚于上一笔被采纳入场的到期日"},
    "formal_test": {"date": "2026-12-31",
                    "sample": "入场日与全部候选腿到期日均 ≤ 该日",
                    "identity": "该日之后的运行才是正式判定；此前一切区间与判定标 exploratory",
                    "freeze": ("首次正式运行把结果、纳入行的数据 sha、config/code sha、生成时刻写入 formal/ 并永久保留；"
                               "之后数据迟到或更正产生的差异只追加为修订，不改首份")},
    # S02：机会分母事前冻结（Codex 005 R07 / 007）
    "prospective_start": "2026-09-28",
    "denominator": {"unit": "池 × 品种 × 侧 × 日历交易日（prospective_start 起）",
                    "categories": ["not_generated", "no_candidate", "entry_missing", "immature",
                                   "unknown", "priced_A_only", "pairable"]},
    "metrics": ["net_usd", "pnl_per_width", "pnl_per_max_risk", "credit_per_width", "fee_per_credit"],
    "dte": [2, 4], "width": {"rule": "2x_modal_strike_step", "band": 0.05},
    "wall": {"def": "local_max", "band": 0.05, "hi_dte": 14},
    "b_rules": {"B1": {"atr": 1.0}, "B2": {"atr": 2.0}, "B3": {"delta": 0.20}}, "primary_b": "B1",
    "stops": [1.0, 2.0],
    "term_structure": {"atm_band": 0.02, "far_dte": [20, 45]},
    "sides": ["P", "C"],
    "primary_basis": "pre_expiry_exit_policy",
    "fee_round_trip": 3.20,
    "mid_give": 0.25,
}
RULES = ("A", "B1", "B2", "B3")


def config_hash(cfg: dict = CONFIG) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


# ── 选腿 ────────────────────────────────────────────────────────────────

def target_expiry(snap, T: date, dte=(2, 4)):
    exps = sorted({c.expiry for c in snap.contracts if dte[0] <= (c.expiry - T).days <= dte[1]})
    return exps[0] if exps else None


def listed_strikes(snap, kind: str, expiry: date) -> list[float]:
    return sorted({c.strike for c in snap.contracts if c.kind == kind and c.expiry == expiry})


def spread_width(strikes, spot, band=0.05):
    """事前、不看 OI 的美元宽度：2 × 现价 ±band 内最常见的挂牌间隔（×100 为每张美元）。"""
    near = [k for k in strikes if abs(k / spot - 1) <= band]
    steps = [round(b - a, 4) for a, b in zip(near, near[1:]) if b > a]
    if not steps:
        return None
    mode = max(set(steps), key=lambda x: (steps.count(x), -x))    # 并列取较小间隔
    return round(2 * mode, 4)


def _buy_leg(strikes, sell, kind, width):
    """同美元宽度的保护腿：sell ∓ width 必须恰好挂牌，否则 None（不可配对）。"""
    target = round(sell - width if kind == "P" else sell + width, 4)
    return next((k for k in strikes if abs(k - target) < 1e-6), None)


def _otm(kind, K, spot):
    return K < spot if kind == "P" else K > spot


def pick_a(wall: dict | None, strikes, spot, kind, width):
    if not wall:
        return None, "no_wall"
    K = wall["strike"]
    if K not in strikes:
        return None, "wall_strike_not_listed_for_target_expiry"
    if not _otm(kind, K, spot):
        return None, "wall_not_otm"
    B = _buy_leg(strikes, K, kind, width)
    return ((K, B), None) if B is not None else (None, "no_same_width_protective")


def pick_b(strikes, spot, atr, kind, mult, width):
    if not atr or atr <= 0:
        return None, "atr_unavailable"
    d = mult * atr
    if kind == "P":
        c = [k for k in strikes if k <= spot - d]
        K = max(c) if c else None
    else:
        c = [k for k in strikes if k >= spot + d]
        K = min(c) if c else None
    if K is None:
        return None, "no_strike_beyond_distance"
    B = _buy_leg(strikes, K, kind, width)
    return ((K, B), None) if B is not None else (None, "no_same_width_protective")


def pick_delta(snap, expiry, kind, spot, target, width):
    """|Δ| 最接近 target 的虚值挂牌档（用快照给的 delta）；并列取更虚值的一档（更保守）。"""
    strikes = listed_strikes(snap, kind, expiry)
    c = [x for x in snap.contracts if x.kind == kind and x.expiry == expiry and _otm(kind, x.strike, spot)
         and x.delta is not None and math.isfinite(x.delta) and x.delta != 0]
    if not c:
        return None, "no_delta"
    best = min(c, key=lambda x: (abs(abs(x.delta) - target), x.strike if kind == "P" else -x.strike))
    B = _buy_leg(strikes, best.strike, kind, width)
    return ((best.strike, B), None) if B is not None else (None, "no_same_width_protective")


def term_structure(snap, T: date, spot: float, target_exp, cfg: dict = None) -> dict:
    """目标到期 ATM IV − 约 30 天到期 ATM IV（pp）。>0 = 倒挂（近月比远月贵，恐慌特征）。"""
    cfg = cfg or CONFIG["term_structure"]
    def atm_iv(exp):
        v = [x.iv for x in snap.contracts if x.expiry == exp and abs(x.strike / spot - 1) <= cfg["atm_band"]
             and x.iv is not None and math.isfinite(x.iv) and x.iv > 0]
        return st.mean(v) * 100 if v else None
    lo, hi = cfg["far_dte"]
    fars = sorted({x.expiry for x in snap.contracts if lo <= (x.expiry - T).days <= hi},
                  key=lambda e: abs((e - T).days - 30))
    near = atm_iv(target_exp) if target_exp else None
    far = atm_iv(fars[0]) if fars else None
    slope = round(near - far, 4) if (near is not None and far is not None) else None
    return {"iv_target_pp": round(near, 4) if near is not None else None,
            "iv_30d_pp": round(far, 4) if far is not None else None,
            "far_expiry": fars[0].isoformat() if fars else None,
            "slope_pp": slope, "inverted": (slope > 0) if slope is not None else None}


def _snap_quote(snap, kind, expiry, strike):
    for c in snap.contracts:
        if c.kind == kind and c.expiry == expiry and c.strike == strike:
            return c
    return None


def _credit(sell_bid, sell_ask, buy_bid, buy_ask, give):
    """(conservative, mid, mid_give)，每张美元。任一价缺失或倒挂 → None。"""
    vals = (sell_bid, sell_ask, buy_bid, buy_ask)
    if any(v is None or not math.isfinite(v) or v < 0 for v in vals) or sell_bid > sell_ask or buy_bid > buy_ask:
        return None
    cons = (sell_bid - buy_ask) * 100
    mid = ((sell_bid + sell_ask) / 2 - (buy_bid + buy_ask) / 2) * 100
    return {"conservative": round(cons, 4), "mid": round(mid, 4), "mid_give": round(mid + give * (cons - mid), 4)}


def build_opportunity(*, inst: str, sym: str, snap, session: date, spot: float, atr: float | None,
                      wall_fn, labels: dict, identity: dict, cfg: dict = CONFIG) -> dict:
    """一个 (品种, 决策日) 的机会行。wall_fn(kind) -> local_wall 结果（调用方注入，保持本模块纯）。"""
    exp = target_expiry(snap, session, tuple(cfg["dte"]))
    legs = []
    for kind in cfg["sides"]:
        strikes = listed_strikes(snap, kind, exp) if exp else []
        wall = wall_fn(kind) if exp else None
        width = spread_width(strikes, spot, cfg["width"]["band"]) if exp else None
        miss = "no_target_expiry" if not exp else ("no_width" if width is None else None)
        picks = {"A": (None, miss) if miss else pick_a(wall, strikes, spot, kind, width)}
        for r, spec in cfg["b_rules"].items():
            if miss:
                picks[r] = (None, miss)
            elif "atr" in spec:
                picks[r] = pick_b(strikes, spot, atr, kind, spec["atr"], width)
            else:
                picks[r] = pick_delta(snap, exp, kind, spot, spec["delta"], width)
        for rule in RULES:
            pk, why = picks[rule]
            leg = {"leg_id": f"{kind}-{rule}", "side": kind, "rule": rule, "expiry": exp.isoformat() if exp else None,
                   "width_rule_usd": round(width * 100, 4) if (exp and width) else None,
                   "status": "candidate" if pk else "no_candidate", "reason": why}
            if pk:
                S, B = pk
                sq, bq = _snap_quote(snap, kind, exp, S), _snap_quote(snap, kind, exp, B)
                leg.update({
                    "sell": S, "buy": B, "width_usd": round(abs(S - B) * 100, 4),
                    "distance_pct": round(abs(S / spot - 1) * 100, 4),
                    "distance_atr": round(abs(S - spot) / atr, 4) if atr else None,
                    "snapshot_credit": (_credit(sq.bid, sq.ask, bq.bid, bq.ask, cfg["mid_give"])
                                        if sq and bq else None),
                    "sell_delta": (round(abs(sq.delta), 4) if sq is not None and sq.delta is not None
                                   and math.isfinite(sq.delta) else None),
                })
                if rule == "A":
                    leg["wall"] = {k: wall.get(k) for k in ("strike", "oi", "buf_pct", "n_exp", "oi_by_expiry")}
            legs.append(leg)
        a = next(l for l in legs if l["leg_id"] == f"{kind}-A")
        for l in legs:
            if l["side"] == kind and l["rule"] != "A":
                l["same_as_A"] = (a["status"] == l["status"] == "candidate"
                                  and (a["sell"], a["buy"]) == (l["sell"], l["buy"]))
    return {
        "key": f"{session.isoformat()}|{inst}", "schema": 1, "instrument": inst, "symbol": sym,
        "session": session.isoformat(), "config_version": cfg["version"], "config_hash": config_hash(cfg),
        "identity": identity,
        "decision": {"base_close": round(spot, 4), "atr14": round(atr, 6) if atr else None,
                     "target_expiry": exp.isoformat() if exp else None,
                     "term_structure": term_structure(snap, session, spot, exp), **labels},
        "legs": legs,
        # —— 以下为事后字段：只有原始观测 windows；入场/标记/退出/止损由 settle 推出 ——
        "windows": {}, "outcome": None,
    }


def frozen_part(row: dict) -> dict:
    """事前冻结部分：除 entry/monitor/exits/outcome/identity.status 与 recorded_at 以外的一切。"""
    out = {k: v for k, v in row.items()
           if k not in ("windows", "entry", "marks", "monitor", "exits", "outcome", "recorded_at", "settled_at",
                        "rederived_at")}
    idt = dict(out.get("identity") or {})
    idt.pop("status", None); idt.pop("certified_at", None)
    out["identity"] = idt
    return out


# ── 报价（v3：原始观测 → 派生）───────────────────────────────────────────
# 行里只存原始观测 row["windows"][f"{date}|{open|close}"] = {"attempts": [attempt, ...]}，
# attempt = {"started_at","ended_at","phase","underlying","quotes": {"P|57.0": {bid,ask,bid_size,ask_size,error}}}。
# 入场、标记、退出、止损全部由下面的纯函数在 settle 时推出：以后修派生逻辑不动原始数据。

def qkey(side: str, strike: float) -> str:
    return f"{side}|{float(strike):g}"


def _num(x):
    return x if (isinstance(x, (int, float)) and math.isfinite(x)) else None


def entry_quality(sq: dict | None, bq: dict | None) -> str | None:
    """开仓质量（卖卖腿吃 bid、买买腿吃 ask）。通过返回 None，否则返回原因。"""
    if not sq or not bq:
        return "leg_missing"
    if sq.get("error") or bq.get("error"):
        return "leg_error"
    sb, sa, sbs = _num(sq.get("bid")), _num(sq.get("ask")), sq.get("bid_size") or 0
    bb, ba, bas = _num(bq.get("bid")), _num(bq.get("ask")), bq.get("ask_size") or 0
    if sb is None or sa is None or ba is None or bb is None:
        return "price_missing"
    if sb <= 0 or ba <= 0 or sa < sb or ba < bb:
        return "price_invalid"
    if sbs <= 0 or bas <= 0:
        return "zero_size"
    return None


def exit_quality(sq: dict | None, bq: dict | None) -> str | None:
    """平仓质量（买回卖腿吃 ask、卖出买腿吃 bid）。通过返回 None，否则返回原因。

    Codex 006 C01：原先只查卖腿，保护腿报错且零挂单量时它的 bid 仍被当作卖出收入扣掉，
    保护腿整个缺失时又按 0 放行 —— 缺失被折成了「已完整平仓」。现在保护腿报错/缺失/零量
    一律使该窗口缺失；只有保护腿报价有效且 bid==0 时，才按「只买回短腿、长腿残值 0」计。
    """
    if not sq:
        return "sell_leg_missing"
    if sq.get("error"):
        return "sell_leg_error"
    sa, sb, sas = _num(sq.get("ask")), _num(sq.get("bid")), sq.get("ask_size") or 0
    if sa is None or sa <= 0 or (sb is not None and sa < sb):
        return "price_invalid"
    if sas <= 0:
        return "zero_size"
    if not bq:
        return "protective_leg_missing"
    if bq.get("error"):
        return "protective_leg_error"
    bb, ba = _num(bq.get("bid")), _num(bq.get("ask"))
    if bb is None:
        return "protective_price_missing"
    if ba is None:
        # Codex 007：bid=0、ask=None、双侧零量曾被当作「有效零买价」—— 那只是缺报价
        return "protective_ask_missing"
    if bb < 0 or ba < bb:
        return "protective_price_invalid"
    if bb > 0 and (bq.get("bid_size") or 0) <= 0:
        return "protective_zero_size"
    if bb == 0 and (ba <= 0 or (bq.get("ask_size") or 0) <= 0):
        return "protective_zero_bid_unverified"
    return None


def exit_mode(bq: dict) -> str:
    return "both_legs" if _num(bq.get("bid")) > 0 else "short_only"


def residual_leg(leg: dict, expiry_bar) -> dict:
    """只买回短腿后，残余长腿（1 张）到期的现金流身份（每张美元）。expiry_bar=(date,h,l,close) 或 None。

    到期虚值 → 现金流 0，但这是【模型处置情景】：依据 OCC 按例外行权规则（虚值不自动行权），
    假设没有相反行权指令、券商按该规则处置；长桥的实际到期处置条款未核实（Codex 008 v5 复核）。
    所以点值带 disposition_basis / model_assumption / actual_confirmed=None，不等于已确认的实际现金流。
    恰在行权价或实值 → 会/可能被行权成标的仓位，处置未知：下界 None，上界 = 内在值。
    """
    if expiry_bar is None:
        return {"status": "immature"}
    K, px = leg["buy"], expiry_bar[3]
    otm = px > K if leg["side"] == "P" else px < K
    intr = round(max(0.0, (K - px) if leg["side"] == "P" else (px - K)) * 100, 4)
    ident = {"disposition_basis": "model: OCC exercise-by-exception",
             "model_assumption": "无相反行权指令；券商按 OCC 规则处置；按到期日收盘价判虚实值（盘后变动未计）",
             "actual_confirmed": None}                    # None = 未用实际成交/交割记录核对
    if otm:
        return {"status": "expired_otm", "cash_lo": 0.0, "cash_hi": 0.0, **ident}
    return {"status": "itm_disposition_unknown", "cash_lo": None, "cash_hi": intr, **ident}


def exit_cost_raw(sq: dict, bq: dict) -> float:
    """平仓成本原值（每张美元），**不裁剪**：分腿吃价可能超过宽度（R05）。调用前须过 exit_quality。"""
    return round((sq["ask"] - bq["bid"]) * 100, 4)


def _credit(sell_bid, sell_ask, buy_bid, buy_ask, give):
    """(conservative, mid, mid_give)，每张美元。任一价缺失或倒挂 → None。"""
    vals = (sell_bid, sell_ask, buy_bid, buy_ask)
    if any(v is None or not math.isfinite(v) or v < 0 for v in vals) or sell_bid > sell_ask or buy_bid > buy_ask:
        return None
    cons = (sell_bid - buy_ask) * 100
    mid = ((sell_bid + sell_ask) / 2 - (buy_bid + buy_ask) / 2) * 100
    return {"conservative": round(cons, 4), "mid": round(mid, 4), "mid_give": round(mid + give * (cons - mid), 4)}


# ── 窗口（v4：由预存日历给出，不从日线倒推）──────────────────────────────

_TZ = ZoneInfo(CONFIG["quote"]["timezone"])


def _m(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def window_bounds(day: date, wname: str) -> tuple[int, int] | None:
    """某交易日某窗口的 [起, 止] ET 分钟（含止分钟）。非交易日或日历未知 → None。"""
    ct = mc.close_time(day)
    if ct is None:
        return None
    q = CONFIG["quote"]["windows"][wname]
    if wname == "open":
        return _m(q["start"]), _m(q["end"])
    before_lo, before_hi = q["minutes_before"]
    return _m(ct) - before_lo, _m(ct) - before_hi


def windows_inside_option_hours(root: str, day: date) -> bool | None:
    """两个观察窗是否都在该根代码期权的交易时段内（9:30 之后、期权收市之前）。未认证/非交易日 → None。
    产品时段来自 core.option_products（主池已认证，扩展池 None）。"""
    from undertow.core import option_products as op
    ct = mc.close_time(day)
    if ct is None:
        return None
    close_m = op.option_close_minutes(root, early=(ct != mc.REGULAR_CLOSE))
    if close_m is None:
        return None
    return all(bd is not None and 9 * 60 + 30 <= bd[0] and bd[1] < close_m
               for bd in (window_bounds(day, "open"), window_bounds(day, "close")))


def window_end(wkey: str) -> datetime | None:
    day, wname = wkey.split("|")
    bd = window_bounds(date.fromisoformat(day), wname)
    if bd is None:
        return None
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, bd[1] // 60, bd[1] % 60, 59, tzinfo=_TZ)


def _passed(wkey: str, now: datetime | None) -> bool:
    """窗口是否已结束。now=None 表示「一切已发生」（事后复算/测试）。"""
    if now is None:
        return True
    end = window_end(wkey)
    return end is not None and now > end


def _day_closed(d: date, now: datetime | None) -> bool:
    if now is None:
        return True
    ct = mc.close_time(d)
    if ct is None:
        return False
    return now > datetime(d.year, d.month, d.day, _m(ct) // 60, _m(ct) % 60, tzinfo=_TZ)


def in_window(wkey: str, started_at: str, ended_at: str) -> bool:
    """尝试是否在预登记窗口内开始【并】结束（ET、同一日期、含结束分钟）。时间戳不可解析 → False。

    晚到的尝试（抓取拖过窗口末）不计：否则「10:20 窗口」会悄悄变成「任何时候」。
    """
    day, wname = wkey.split("|")
    bd = window_bounds(date.fromisoformat(day), wname)
    if bd is None:
        return False
    try:
        t0 = datetime.fromisoformat(started_at).astimezone(_TZ)
        t1 = datetime.fromisoformat(ended_at).astimezone(_TZ)
    except (TypeError, ValueError):
        return False
    return all(t.date().isoformat() == day and bd[0] <= t.hour * 60 + t.minute <= bd[1] for t in (t0, t1))


def window_leg(row: dict, leg: dict, wkey: str, purpose: str) -> dict:
    """某窗口对某条腿的状态：no_window / not_run / missing（附原因）/ valid（附价格）。取第一次通过质量的尝试。"""
    day, wname = wkey.split("|")
    if window_bounds(date.fromisoformat(day), wname) is None:
        return {"status": "no_window"}
    w = (row.get("windows") or {}).get(wkey)
    if not w or not w.get("attempts"):
        return {"status": "not_run"}
    reasons = []
    for a in w["attempts"]:
        if a.get("phase") != "rth":
            reasons.append("off_hours"); continue
        if not in_window(wkey, a.get("started_at"), a.get("ended_at")):
            reasons.append("outside_window"); continue
        sq = a["quotes"].get(qkey(leg["side"], leg["sell"]))
        bq = a["quotes"].get(qkey(leg["side"], leg["buy"]))
        bad = entry_quality(sq, bq) if purpose == "entry" else exit_quality(sq, bq)
        if bad:
            reasons.append(bad); continue
        if purpose == "entry":
            cr = _credit(sq["bid"], sq["ask"], bq["bid"], bq["ask"], CONFIG["mid_give"])
            if cr is None or not (0 < cr["conservative"] < leg["width_usd"]):
                reasons.append("credit_outside_0_width"); continue
            return {"status": "valid", "credit": cr, "at": a["started_at"], "ended_at": a["ended_at"]}
        cost = exit_cost_raw(sq, bq)
        return {"status": "valid", "cost": cost, "exceeds_width": cost > leg["width_usd"],
                "mode": exit_mode(bq), "at": a["started_at"], "ended_at": a["ended_at"]}
    return {"status": "missing", "reasons": reasons}


def expected_mark_windows(session: date, expiry: date) -> list[str] | None:
    """入场（session 开盘窗）之后、到期收盘为止应有的标记窗口，按时间顺序；日历未知 → None。

    Codex 006 C02：只看日历，不看有没有日线 —— 缺一根日线不能让那天的应有窗口消失。
    """
    days = mc.trading_days(session, expiry)
    if days is None:
        return None
    out = [f"{session.isoformat()}|close"]
    for d in days:
        if d > session:
            out += [f"{d.isoformat()}|open", f"{d.isoformat()}|close"]
    return out


# ── 结算 ────────────────────────────────────────────────────────────────

def _beyond(kind, K, px):
    return px < K if kind == "P" else px > K


def _after(later_iso: str, earlier_iso: str) -> bool:
    try:
        return datetime.fromisoformat(later_iso) > datetime.fromisoformat(earlier_iso)
    except (TypeError, ValueError):
        return False


BASES = ("snapshot_model", "quote_entry_expiry_intrinsic", "pre_expiry_exit_policy",
         "close_beyond_next_open_exit") + tuple(f"stop{m:g}x_twice_daily" for m in CONFIG["stops"])


def settle_leg(leg: dict, row: dict, *, bars: list, now: datetime | None = None,
               fee: float = CONFIG["fee_round_trip"], stops=tuple(CONFIG["stops"])) -> dict:
    """bars: [(date, high, low, close)] 已完成日线；now: 结算时刻（None=事后复算，视一切已发生）。

    各终点独立判断成熟（status="immature"），全部成熟且日线齐全时 complete=True。
    交易日与窗口来自预存日历；日线只用来取价，缺失记 bars_incomplete，不当作休市。
    """
    session = date.fromisoformat(row["session"])
    exp = date.fromisoformat(leg["expiry"])
    k, S, W = leg["side"], leg["sell"], leg["width_usd"]
    res = {"calendar": mc.VERSION, "width_usd": W, "pnl": {}, "pnl_bounds": {}, "pnl_residual0": {},
           "max_risk": {}, "credit": {}, "status": {}, "exit_mode": {}, "residual": {}, "complete": False}

    def put(basis, credit, cost, status="ok", resid=None):
        # 有价却标 ok 才可信：缺权利金 / 权利金越出 (0, W) 必须在状态上显式可见，不能混进「正常」
        if status in ("ok", "stale_snapshot_quote"):
            if credit is None:
                status = "credit_missing"
            elif not (0 < credit < W):
                status = "credit_outside_0_width"
            elif cost is None:
                status = "cost_missing"
        res["status"][basis] = status
        if credit is None or cost is None or not (0 < credit < W) or status == "immature":
            res["pnl"][basis] = None; res["max_risk"][basis] = None; res["credit"][basis] = credit
            res["pnl_bounds"][basis] = [None, None]; res["pnl_residual0"][basis] = None
            return
        base = round(credit - cost - fee, 4)
        res["max_risk"][basis] = round(W - credit + fee, 4)
        res["credit"][basis] = credit
        res["pnl_residual0"][basis] = base                  # 残腿按 0 的情景（非已实现）
        if resid is None:
            res["pnl"][basis] = base; res["pnl_bounds"][basis] = [base, base]
            return
        res["residual"][basis] = {"side": k, "strike": leg["buy"], "qty": 1, **resid}
        lo = None if resid["cash_lo"] is None else round(base + resid["cash_lo"], 4)
        hi = round(base + resid["cash_hi"], 4)
        res["pnl_bounds"][basis] = [lo, hi]
        res["pnl"][basis] = lo if lo == hi else None       # 只有残腿现金流确定时才有点值

    days = mc.trading_days(session, exp)
    if days is None or session not in days or exp not in days:
        for basis in BASES:
            put(basis, None, None, "calendar_unknown")
        return res

    bar_by = {b[0]: b for b in bars}
    eb = bar_by.get(exp)
    missing_bars = [d for d in days if d not in bar_by and _day_closed(d, now)]
    intrinsic = (min(W, max(0.0, ((S - eb[3]) if k == "P" else (eb[3] - S)) * 100)) if eb else None)
    res["expiry_close"] = eb[3] if eb else None
    res["bars_missing"] = [d.isoformat() for d in missing_bars]
    full = eb is not None and not missing_bars
    win = [bar_by[d] for d in days] if full else None
    ohlc = full and all(h is not None and lo is not None for _, h, lo, _c in win)
    res["endpoint_breach"] = _beyond(k, S, eb[3]) if eb else None
    res["any_close_breach"] = any(_beyond(k, S, b[3]) for b in win) if full else None
    res["intraday_breach"] = (any((lo < S) if k == "P" else (h > S) for _, h, lo, _c in win) if ohlc else None)

    put("snapshot_model", (leg.get("snapshot_credit") or {}).get("mid_give"), intrinsic,
        "stale_snapshot_quote" if eb else "immature")

    ekey = f"{session.isoformat()}|open"
    ent = window_leg(row, leg, ekey, "entry")
    res["entry"] = ent
    ec = ent["credit"]["conservative"] if ent["status"] == "valid" else None
    tag = "ok" if ec is not None else ("immature" if not _passed(ekey, now) else f"entry_{ent['status']}")

    put("quote_entry_expiry_intrinsic", ec, intrinsic, tag if ec is None else ("ok" if eb else "immature"))

    def settle_exit(basis, x, label):
        """x=有效出场报价。双腿 → 点值；只买回短腿 → 残腿按到期结果定点值或区间（Codex 007）。"""
        res["exit_mode"][basis] = x["mode"]
        if x["mode"] == "both_legs":
            return put(basis, ec, x["cost"], label)
        rl = residual_leg(leg, eb)
        if rl["status"] == "immature":
            return put(basis, ec, None, "immature")
        tag = "short_only_expired_otm" if rl["status"] == "expired_otm" else "short_only_itm_unknown"
        put(basis, ec, x["cost"], f"{label}|{tag}", resid=rl)

    def exit_at(basis, wk):
        """在 wk 窗口退出：须严格晚于实际入场（C03）。"""
        if not _passed(wk, now):
            return put(basis, ec, None, "immature")
        w = window_leg(row, leg, wk, "exit")
        if w["status"] == "valid" and not _after(w["at"], ent["ended_at"]):
            return put(basis, ec, None, "exit_not_after_entry")
        if w["status"] == "valid":
            return settle_exit(basis, w, "ok")
        put(basis, ec, None, f"exit_{w['status']}")

    # 主终点：到期前最后一个交易日收盘窗的退出政策。入场日本身可以是这一天（周五入场、周一到期）。
    X = mc.prev_trading_day(exp)
    if ec is None:
        put("pre_expiry_exit_policy", None, None, tag)
    elif X is None or X < session:
        put("pre_expiry_exit_policy", ec, None, "no_exit_day")
    else:
        exit_at("pre_expiry_exit_policy", f"{X.isoformat()}|close")

    # 收盘越过卖腿 → 下一交易日开盘窗平仓；首次越线前任一交易日缺日线 → 未知（不当作没越线）
    basis = "close_beyond_next_open_exit"
    if ec is None:
        put(basis, None, None, tag)
    else:
        trig, state = None, None
        for d in days[:-1]:
            b = bar_by.get(d)
            if b is None:
                state = "bars_incomplete" if _day_closed(d, now) else "immature"
                break
            if _beyond(k, S, b[3]):
                trig = d; break
        res["trigger_date"] = trig.isoformat() if trig else None
        if trig is not None:
            exit_at(basis, f"{mc.next_trading_day(trig).isoformat()}|open")
        elif state:
            put(basis, ec, None, state)
        else:
            put(basis, ec, intrinsic, "ok" if eb else "immature")

    # 离散止损（一天两次）：四态；首次触发前任何未知 → 结果未知（R01）
    marks = expected_mark_windows(session, exp)
    seq = [(wk, window_leg(row, leg, wk, "exit")) for wk in marks if _passed(wk, now)]
    pending = len(marks) - len(seq)
    res["marks"] = {"expected": len(marks),
                    "valid": sum(x["status"] == "valid" for _, x in seq),
                    "not_run": sum(x["status"] == "not_run" for _, x in seq),
                    "missing": sum(x["status"] == "missing" for _, x in seq),
                    "pending": pending}
    for mult in stops:
        basis = f"stop{mult:g}x_twice_daily"
        if ec is None:
            put(basis, None, None, tag); continue
        unknown, hit = False, None
        for wk, x in seq:
            if x["status"] != "valid":
                unknown = True; continue
            if x["cost"] >= (1 + mult) * ec:
                hit = (wk, x); break
        if hit and not unknown:
            settle_exit(basis, hit[1], f"stopped@{hit[0]}")
        elif hit and unknown:
            put(basis, ec, None, "path_unknown_before_trigger")
        elif unknown:
            put(basis, ec, None, "path_unknown")
        elif pending or eb is None:
            put(basis, ec, None, "immature")
        else:
            put(basis, ec, intrinsic, "held_all_marks_valid")

    res["complete"] = full and all(v != "immature" for v in res["status"].values())
    return res


# ── 统计（W06）──────────────────────────────────────────────────────────

def _norm(o: dict, basis: str, which: str = "point"):
    """which: point / lo / hi / residual0。缺失或不可得 → None。"""
    r = o["max_risk"].get(basis)
    if which == "point":
        p = o["pnl"].get(basis)
    elif which == "residual0":
        p = (o.get("pnl_residual0") or {}).get(basis)
    else:
        b = (o.get("pnl_bounds") or {}).get(basis) or [None, None]
        p = b[0] if which == "lo" else b[1]
    return p / r if (p is not None and r) else None


def block_bootstrap(groups: dict, *, block_days: int = CONFIG["stats"]["block_days"],
                    iters: int = CONFIG["stats"]["iters"], seed: int = CONFIG["stats"]["seed"],
                    calendar_days: list | None = None, q_lo: float = 0.025, q_hi: float = 0.975) -> dict:
    """groups: {"YYYY-MM-DD": [值…]}。按【日历交易日】做循环移动块 bootstrap（Politis–Romano）。

    - 块沿真实交易日历滑动：无机会/技术缺失的交易日照样占位（贡献 0 个值），不被压缩掉，
      也不补假收益（Codex 006 C04）。calendar_days 缺省时由预存日历给出首末样本日之间的交易日。
    - 有样本的日期数 // 块长 < min_full_blocks → insufficient（计算保护，不是充分性证明）。
    - 样本值全等或区间零宽 → degenerate：非参数 bootstrap 在常数样本上必然退化，不据此判定。
    返回 {"mean","lo","hi","status","n_dates","n_calendar_days","block_days"}。
    """
    out = {"mean": None, "lo": None, "hi": None, "status": "empty", "n_dates": 0,
           "n_calendar_days": None, "block_days": block_days}
    ds = sorted(d for d in groups if groups[d])
    allv = [v for d in ds for v in groups[d]]
    if not allv:
        return out
    out["mean"] = st.mean(allv); out["n_dates"] = len(ds)
    span = calendar_days if calendar_days is not None else mc.trading_days(
        date.fromisoformat(ds[0]), date.fromisoformat(ds[-1]))
    if span is None:
        out["status"] = "calendar_unknown"; return out
    span = [d.isoformat() if isinstance(d, date) else d for d in span]
    out["n_calendar_days"] = len(span)
    if set(ds) - set(span):
        out["status"] = "date_not_trading_day"; return out
    b = max(1, block_days)
    if len(ds) // b < CONFIG["stats"]["min_full_blocks"]:
        out["status"] = "insufficient"; return out
    if max(allv) - min(allv) <= 1e-12:
        out["status"] = "degenerate"; return out
    N = len(span); k = -(-N // b)
    rnd = random.Random(seed); vals = []
    for _ in range(iters):
        picked = []
        for _ in range(k):
            s0 = rnd.randrange(N)
            picked.extend(span[(s0 + j) % N] for j in range(b))
        s_ = [v for d in picked[:N] for v in groups.get(d, ())]
        if s_:
            vals.append(st.mean(s_))
    if len(vals) < iters * 0.95:
        out["status"] = "insufficient"; return out
    vals.sort(); n = len(vals)
    out["lo"], out["hi"] = vals[int(q_lo * n)], vals[min(n - 1, max(0, int(q_hi * n) - 1))]
    out["q"] = [q_lo, q_hi]
    out["status"] = "degenerate" if out["hi"] - out["lo"] <= 1e-12 else "ok"
    return out


def zero_event_upper(n: int, alpha: float = 0.05) -> float | None:
    """n 次独立观测零事件时事件率的单侧 (1−alpha) 精确上界。"""
    return 1 - alpha ** (1 / n) if n > 0 else None


_NOT_JUDGED = {"empty": "样本不足", "insufficient": "样本不足", "degenerate": "退化（不判）",
               "calendar_unknown": "日历未知", "date_not_trading_day": "日期不在交易日历",
               "residual_unknown": "未决（残腿处置未知）"}


def judge(ci: dict, *, min_useful: float = 0.0) -> str:
    """只对 status=ok 的区间下判定；其余状态原样说出来，不折成「未决」或「支持」。"""
    if ci["status"] != "ok":
        return _NOT_JUDGED.get(ci["status"], ci["status"])
    if ci["lo"] > min_useful:
        return "支持"
    if ci["hi"] <= 0:
        return "不支持"
    return "未决"


def flow_aligned_side(row: dict):
    d = ((row.get("decision") or {}).get("flow") or {}).get("call_direction")
    return {"偏多": "P", "偏空": "C"}.get(d)


def status_breakdown(rows: list[dict], basis: str) -> dict:
    """某终点下所有候选腿的状态计数（用户可见，S01 验收：每种状态进报告，不只写日志）。

    未结算的腿记「unsettled」；另汇总入场缺失原因与持仓标记覆盖（应有/有效/未运行/缺失）。
    """
    from collections import Counter
    st_, reasons = Counter(), Counter()
    marks = {"expected": 0, "valid": 0, "not_run": 0, "missing": 0, "pending": 0}
    modes = Counter()
    model_pts = 0     # 残腿到期虚值、按模型处置情景给点值且未经实际记录确认的腿
    for r in rows:
        for l in r.get("legs", []):
            if l.get("status") != "candidate":
                continue
            o = (r.get("outcome") or {}).get(l["leg_id"])
            if not o:
                st_["unsettled"] += 1
                continue
            st_[o["status"].get(basis, "absent")] += 1
            ent = o.get("entry") or {}
            for why in ent.get("reasons", []) if ent.get("status") == "missing" else []:
                reasons[why] += 1
            for k in marks:
                marks[k] += (o.get("marks") or {}).get(k, 0)
            if (o.get("exit_mode") or {}).get(basis):
                modes[o["exit_mode"][basis]] += 1
            rs = (o.get("residual") or {}).get(basis) or {}
            if rs.get("status") == "expired_otm" and rs.get("actual_confirmed") is not True:
                model_pts += 1
    return {"basis": basis, "status": dict(st_.most_common()), "entry_missing_reasons": dict(reasons.most_common()),
            "marks": marks, "exit_modes": dict(modes.most_common()),
            "model_disposition_points": model_pts}


def formal_identity(as_of: date | None) -> str:
    return "formal" if (as_of is not None and as_of > date.fromisoformat(CONFIG["formal_test"]["date"])) else "exploratory"


def _non_overlap(rows: list[dict], side: str) -> list[dict]:
    """同品种同侧：入场日须晚于上一笔被采纳入场的 A 腿到期日（不重叠持有期）。"""
    keep, last = [], {}
    for r in sorted(rows, key=lambda x: x["session"]):
        a = next((l for l in r["legs"] if l["leg_id"] == f"{side}-A" and l.get("status") == "candidate"), None)
        if a is None:
            continue
        prev = last.get(r["instrument"])
        if prev is not None and r["session"] <= prev:
            continue
        last[r["instrument"]] = a["expiry"]
        keep.append(r)
    return keep


def interval_ci(lo_by: dict, hi_by: dict, unbounded: int = 0, *,
                block_days: int = CONFIG["stats"]["block_days"], iters: int = CONFIG["stats"]["iters"],
                q_lo: float = 0.025, q_hi: float = 0.975) -> dict:
    """区间值样本的块 bootstrap：下界序列取其区间下沿、上界序列取其区间上沿（Codex 007）。

    点值样本（下界 = 上界）两条序列相同、同一种子 → 退化为普通区间。unbounded>0（有观测只有单侧界）→
    status=residual_unknown，不判定：下界相减不是差值的下界，缺一侧就给不出有证据的界限。
    """
    L = block_bootstrap(lo_by, block_days=block_days, iters=iters, q_lo=q_lo, q_hi=q_hi)
    H = block_bootstrap(hi_by, block_days=block_days, iters=iters, q_lo=q_lo, q_hi=q_hi)
    out = {"mean": L["mean"] if L["mean"] == H["mean"] else None, "mean_bounds": [L["mean"], H["mean"]],
           "lo": L["lo"], "hi": H["hi"], "n_dates": L["n_dates"], "n_calendar_days": L["n_calendar_days"],
           "block_days": block_days, "unbounded": unbounded}
    if unbounded:
        out["status"] = "residual_unknown"
    elif L["status"] != "ok":
        out["status"] = L["status"]
    elif H["status"] != "ok":
        out["status"] = H["status"]
    else:
        out["status"] = "degenerate" if out["hi"] - out["lo"] <= 1e-12 else "ok"
    return out


def _vals(o: dict | None, basis: str, estimate: str):
    """某条腿在某终点下的归一化值 (下界, 上界)；未定价 → None；条件样本外 → "excluded"。下界可为 None（无界）。"""
    if not o:
        return None
    if estimate == "bounds":
        hi = _norm(o, basis, "hi")
        return None if hi is None else (_norm(o, basis, "lo"), hi)
    if estimate == "both_legs_only":
        if (o.get("exit_mode") or {}).get(basis, "both_legs") != "both_legs":
            return "excluded"
        v = _norm(o, basis, "point")
        return None if v is None else (v, v)
    if estimate == "residual0":
        v = _norm(o, basis, "residual0")
        return None if v is None else (v, v)
    raise ValueError(estimate)


def _eligible(rows, *, mode, pool, as_of):
    ident = formal_identity(as_of)
    fdate = CONFIG["formal_test"]["date"]
    base = [r for r in rows if (r.get("identity") or {}).get("mode") == mode
            and (pool is None or r.get("instrument") in CONFIG["pools"][pool])]
    if ident == "formal":
        base = [r for r in base if r["session"] <= fdate
                and all(l["expiry"] <= fdate for l in r["legs"] if l.get("status") == "candidate")]
    return ident, base


def paired_summary(rows: list[dict], basis: str = CONFIG["primary_basis"],
                   b_rule: str = CONFIG["primary_b"], *, mode: str = "prospective",
                   sides=None, flow_aligned_only: bool = False, pool: str | None = CONFIG["primary_pool"],
                   non_overlap: bool = False, as_of: date | None = None, estimate: str = "bounds") -> dict:
    """A 全体 / A 配对 / B 配对 三个均值与 A−B 配对差（日历块 bootstrap，区间值按界限传播）。

    estimate：bounds（主）/ both_legs_only（条件样本）/ residual0（情景）。
    pool=None 仅供单元测试；报告永远分池。as_of 晚于正式检验日 → formal，只收检验日前入场且到期的行。
    """
    ident, base = _eligible(rows, mode=mode, pool=pool, as_of=as_of)
    base = [r for r in base if r.get("outcome")]
    S = {k: {} for k in ("aL", "aH", "paL", "paH", "pbL", "pbH", "dL", "dH")}
    cover = {"opportunities": 0, "a_priced": 0, "a_unbounded": 0, "pairs": 0, "pairs_unbounded": 0,
             "identical_pairs": 0, "excluded_conditional": 0}
    add = lambda key, d, v: S[key].setdefault(d, []).append(v)
    for side in (sides or CONFIG["sides"]):
        use = _non_overlap(base, side) if non_overlap else base
        for r in use:
            if flow_aligned_only and flow_aligned_side(r) != side:
                continue
            cover["opportunities"] += 1
            d = r["session"]
            va = _vals(r["outcome"].get(f"{side}-A"), basis, estimate)
            vb = _vals(r["outcome"].get(f"{side}-{b_rule}"), basis, estimate)
            if va == "excluded" or vb == "excluded":
                cover["excluded_conditional"] += 1
            if va is None or va == "excluded":
                continue
            cover["a_priced"] += 1
            if va[0] is None:
                cover["a_unbounded"] += 1
            else:
                add("aL", d, va[0]); add("aH", d, va[1])
            if vb is None or vb == "excluded":
                continue
            cover["pairs"] += 1
            leg_b = next((l for l in r["legs"] if l["leg_id"] == f"{side}-{b_rule}"), {})
            cover["identical_pairs"] += bool(leg_b.get("same_as_A"))
            if va[0] is None or vb[0] is None:
                cover["pairs_unbounded"] += 1
                continue
            add("paL", d, va[0]); add("paH", d, va[1]); add("pbL", d, vb[0]); add("pbH", d, vb[1])
            add("dL", d, va[0] - vb[1]); add("dH", d, va[1] - vb[0])
    sens = CONFIG["stats"]["sensitivity_block_days"]
    A = interval_ci(S["aL"], S["aH"], cover["a_unbounded"])
    PA = interval_ci(S["paL"], S["paH"], cover["pairs_unbounded"])
    PB = interval_ci(S["pbL"], S["pbH"], cover["pairs_unbounded"])
    D = interval_ci(S["dL"], S["dH"], cover["pairs_unbounded"])
    A10 = interval_ci(S["aL"], S["aH"], cover["a_unbounded"], block_days=sens)
    D10 = interval_ci(S["dL"], S["dH"], cover["pairs_unbounded"], block_days=sens)
    tagv = (lambda v: v) if ident == "formal" else (lambda v: f"探索·{v}")
    return {"basis": basis, "b_rule": b_rule, "mode": mode, "pool": pool, "identity": ident, "estimate": estimate,
            "sides": list(sides or CONFIG["sides"]), "flow_aligned_only": flow_aligned_only,
            "non_overlap": non_overlap, "coverage": cover,
            "n_dates_A": len(S["aL"]), "n_dates_pairs": len(S["dL"]),
            "A": A, "A_verdict": tagv(judge(A)),
            "A_paired": PA, "B_paired": PB,
            "AminusB": D, "AminusB_verdict": tagv(judge(D)),
            "stats_version": CONFIG["stats"]["version"],
            "sensitivity": {"block_days": sens, "A": A10, "A_verdict": tagv(judge(A10)),
                            "AminusB": D10, "AminusB_verdict": tagv(judge(D10))},
            "note": ("区间为按日历交易日的循环移动块 bootstrap（主 5 日、敏感性 10 日）；区间值观测按 "
                     "[A下界−B上界, A上界−B下界] 传播；同日多品种/两侧不当独立；"
                     f"{CONFIG['formal_test']['date']} 之前的一切判定都是探索，不是放行依据。")}


# ── S02：机会分母与描述性指标（Codex 005 R07 / 007）──────────────────────────

def opportunity_ledger(rows: list[dict], *, basis: str = CONFIG["primary_basis"], b_rule: str = CONFIG["primary_b"],
                       pool: str = CONFIG["primary_pool"], mode: str = "prospective",
                       start: date | None = None, end: date | None = None,
                       high_vol: set | None = None) -> dict:
    """按「池 × 品种 × 侧 × 日历交易日」生成分母，每个格子落入且只落入一个类别。

    not_generated：该交易日没有机会行（capture 未跑/失败）；no_candidate：有行但 A 无候选腿；
    entry_missing：入场窗无有效报价；immature：终点未成熟或未结算；unknown：其余拿不到点值/上界
    （退出缺价、止损路径未知等）；priced_A_only：A 有值、B 无；pairable：A、B 都有值（区间亦算）。
    high_vol：{(品种, "YYYY-MM-DD")} 大波动交易日集合（由调用方按各品种 |日收益| 70 分位给出），
    用来看缺失是否集中在大波动日。纯函数：不取数。
    """
    cats = CONFIG["denominator"]["categories"]
    insts = CONFIG["pools"][pool]
    mine = [r for r in rows if (r.get("identity") or {}).get("mode") == mode and r.get("instrument") in insts]
    if start is None:
        start = (date.fromisoformat(CONFIG["prospective_start"]) if mode == "prospective"
                 else (min(date.fromisoformat(r["session"]) for r in mine) if mine else None))
    if end is None:
        end = max((date.fromisoformat(r["session"]) for r in mine), default=start)
    out = {"basis": basis, "b_rule": b_rule, "pool": pool, "mode": mode,
           "start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
           "totals": dict.fromkeys(cats, 0), "by_instrument": {}, "no_candidate_reasons": {},
           "missing_by_vol": {"high": {"cells": 0, "missing": 0}, "other": {"cells": 0, "missing": 0}}}
    days = mc.trading_days(start, end) if start and end and start <= end else []
    if days is None:
        out["status"] = "calendar_unknown"
        return out
    by = {(r["instrument"], r["session"]): r for r in mine}
    for inst in insts:
        ci = out["by_instrument"].setdefault(inst, dict.fromkeys(cats, 0))
        for d in days:
            r = by.get((inst, d.isoformat()))
            for side in CONFIG["sides"]:
                if r is None:
                    cat = "not_generated"
                else:
                    la = next((l for l in r["legs"] if l["leg_id"] == f"{side}-A"), None)
                    if la is None or la.get("status") != "candidate":
                        cat = "no_candidate"
                        why = (la or {}).get("reason") or "absent"
                        out["no_candidate_reasons"][why] = out["no_candidate_reasons"].get(why, 0) + 1
                    else:
                        oa = (r.get("outcome") or {}).get(f"{side}-A")
                        stt = (oa or {}).get("status", {}).get(basis) if oa else None
                        va = _vals(oa, basis, "bounds")
                        if oa is None or stt == "immature":
                            cat = "immature"
                        elif str(stt).startswith("entry_"):
                            cat = "entry_missing"
                        elif va is None:
                            cat = "unknown"
                        else:
                            vb = _vals((r.get("outcome") or {}).get(f"{side}-{b_rule}"), basis, "bounds")
                            cat = "pairable" if vb is not None else "priced_A_only"
                ci[cat] += 1; out["totals"][cat] += 1
                if high_vol is not None and cat != "not_generated":
                    key = "high" if (inst, d.isoformat()) in high_vol else "other"
                    out["missing_by_vol"][key]["cells"] += 1
                    out["missing_by_vol"][key]["missing"] += cat in ("entry_missing", "unknown")
    n_pair = out["totals"]["pairable"]
    for inst, c in out["by_instrument"].items():
        n = sum(c.values())
        c["cells"] = n
        c["pairable_rate"] = round(c["pairable"] / n, 4) if n else None
        c["weight_in_pool"] = round(c["pairable"] / n_pair, 4) if n_pair else None
    out["cells"] = sum(out["totals"].values())
    if high_vol is None:
        out["missing_by_vol"] = None
    out["status"] = "ok"
    return out


def metrics_table(rows: list[dict], *, basis: str = CONFIG["primary_basis"], pool: str = CONFIG["primary_pool"],
                  mode: str = "prospective", as_of: date | None = None, sides=None) -> dict:
    """每条规则的描述性指标均值（只取有点值的腿；区间值与未知单独计数）。不是组合收益，不做推断。

    net_usd = 每张净损益；pnl_per_width = 净损益 / 宽度；pnl_per_max_risk = 净损益 / 事前最大风险；
    credit_per_width = 入场保守收权金 / 宽度；fee_per_credit = 往返费用预算 / 收权金。
    """
    _, base = _eligible(rows, mode=mode, pool=pool, as_of=as_of)
    fee = CONFIG["fee_round_trip"]
    out = {}
    for rule in RULES:
        acc = {m: [] for m in CONFIG["metrics"]}
        n_interval = n_unknown = 0
        for r in base:
            for side in (sides or CONFIG["sides"]):
                o = (r.get("outcome") or {}).get(f"{side}-{rule}")
                if not o:
                    continue
                cr, W, mr = o["credit"].get(basis), o["width_usd"], o["max_risk"].get(basis)
                if cr is None or mr is None:
                    n_unknown += 1; continue
                pnl = o["pnl"].get(basis)
                if pnl is None:
                    n_interval += 1; continue
                acc["net_usd"].append(pnl); acc["pnl_per_width"].append(pnl / W)
                acc["pnl_per_max_risk"].append(pnl / mr); acc["credit_per_width"].append(cr / W)
                acc["fee_per_credit"].append(fee / cr)
        out[rule] = {"n_point": len(acc["net_usd"]), "n_interval_only": n_interval, "n_unpriced": n_unknown,
                     **{m: (round(st.mean(v), 4) if v else None) for m, v in acc.items()}}
    return out


# ── P5 风险预算（草案，未接入任何流程；实盘试点 G4 前由用户与 Codex 定稿）────────
CLUSTERS = {"贵金属": ("gold", "silver"), "股指": ("qqq", "tqqq", "spy", "iwm"),
            "能源": ("wti",), "利率": ("tlt",),
            "科技股": ("googl", "tsla", "nvda", "intc", "amd", "msft", "aapl")}


def contracts_allowed(max_loss_per_contract: float, equity: float, *, hard_frac: float | None = None,
                      cluster_used: float = 0.0, cluster_frac: float | None = None) -> int:
    """最大亏损一层的可开张数 —— 委托 analyze.risk_policy（唯一来源，Codex 008 G07）。

    hard_frac / cluster_frac 仅供测试覆盖；缺省读政策。止损情景按最大亏损计（保守）。"""
    from undertow.analyze import risk_policy as rp
    pol = dict(rp.POLICY)
    if hard_frac is not None:
        pol["max_loss_frac"] = hard_frac
        pol["max_stop_risk_frac"] = max(pol["max_stop_risk_frac"], hard_frac)
    else:
        pol["max_stop_risk_frac"] = pol["max_loss_frac"]      # 本函数只管最大亏损一层
    if cluster_frac is not None:
        pol["cluster_max_loss_frac"] = cluster_frac
    n, _ = rp.max_units(net_assets=equity, unit_max_loss=max_loss_per_contract,
                        cluster_open_max_loss=cluster_used, policy=pol)
    return n