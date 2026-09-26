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
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics as st
from datetime import date

CONFIG = {
    "version": "shadow-v1-20260926",
    "dte": [2, 4], "width_n": 2,
    "wall": {"def": "local_max", "band": 0.05, "hi_dte": 14},
    "b_rules": {"B1": 1.0, "B2": 2.0}, "primary_b": "B1",
    "sides": ["P", "C"],
    "primary_basis": "hold_quote_conservative",
    "fee_round_trip": 3.20,
    "mid_give": 0.25,
}
RULES = ("A", "B1", "B2")


def config_hash(cfg: dict = CONFIG) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


# ── 选腿 ────────────────────────────────────────────────────────────────

def target_expiry(snap, T: date, dte=(2, 4)):
    exps = sorted({c.expiry for c in snap.contracts if dte[0] <= (c.expiry - T).days <= dte[1]})
    return exps[0] if exps else None


def listed_strikes(snap, kind: str, expiry: date) -> list[float]:
    return sorted({c.strike for c in snap.contracts if c.kind == kind and c.expiry == expiry})


def _buy_leg(strikes, sell, kind, width_n):
    i = strikes.index(sell)
    j = i - width_n if kind == "P" else i + width_n
    return strikes[j] if 0 <= j < len(strikes) else None


def _otm(kind, K, spot):
    return K < spot if kind == "P" else K > spot


def pick_a(wall: dict | None, strikes, spot, kind, width_n):
    if not wall:
        return None, "no_wall"
    K = wall["strike"]
    if K not in strikes:
        return None, "wall_strike_not_listed_for_target_expiry"
    if not _otm(kind, K, spot):
        return None, "wall_not_otm"
    B = _buy_leg(strikes, K, kind, width_n)
    return ((K, B), None) if B is not None else (None, "no_protective_strike")


def pick_b(strikes, spot, atr, kind, mult, width_n):
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
    B = _buy_leg(strikes, K, kind, width_n)
    return ((K, B), None) if B is not None else (None, "no_protective_strike")


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
        picks = {"A": pick_a(wall, strikes, spot, kind, cfg["width_n"]) if exp else (None, "no_target_expiry")}
        for r, m in cfg["b_rules"].items():
            picks[r] = pick_b(strikes, spot, atr, kind, m, cfg["width_n"]) if exp else (None, "no_target_expiry")
        for rule in RULES:
            pk, why = picks[rule]
            leg = {"leg_id": f"{kind}-{rule}", "side": kind, "rule": rule, "expiry": exp.isoformat() if exp else None,
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
                     "target_expiry": exp.isoformat() if exp else None, **labels},
        "legs": legs,
        # —— 以下为事后字段 ——
        "entry": None, "monitor": [], "exits": {}, "outcome": None,
    }


def frozen_part(row: dict) -> dict:
    """事前冻结部分：除 entry/monitor/exits/outcome/identity.status 与 recorded_at 以外的一切。"""
    out = {k: v for k, v in row.items()
           if k not in ("entry", "monitor", "exits", "outcome", "recorded_at", "settled_at")}
    idt = dict(out.get("identity") or {})
    idt.pop("status", None); idt.pop("certified_at", None)
    out["identity"] = idt
    return out


# ── 报价 ────────────────────────────────────────────────────────────────

def price_legs(row: dict, depth: dict, *, observed_at: str, phase: str, give: float = CONFIG["mid_give"]) -> dict:
    """depth: {(kind, strike): {"bid","ask","bid_size","ask_size","error"}}。返回每条候选腿的入场情景。"""
    out = {"observed_at": observed_at, "phase": phase,
           "executable": phase == "rth", "legs": {}}
    for l in row["legs"]:
        if l["status"] != "candidate":
            continue
        s, b = depth.get((l["side"], l["sell"])), depth.get((l["side"], l["buy"]))
        ok = s and b and not s.get("error") and not b.get("error")
        cr = _credit(s["bid"], s["ask"], b["bid"], b["ask"], give) if ok else None
        out["legs"][l["leg_id"]] = {
            "sell": {k: s.get(k) for k in ("bid", "ask", "bid_size", "ask_size", "error")} if s else None,
            "buy": {k: b.get(k) for k in ("bid", "ask", "bid_size", "ask_size", "error")} if b else None,
            "credit": cr, "valid": bool(cr and cr["conservative"] > 0 and cr["conservative"] < l["width_usd"]),
        }
    return out


def exit_cost(sell_q: dict, buy_q: dict, width_usd: float):
    """平仓成本（买回卖腿吃 ask、卖出买腿吃 bid），每张美元；越界 → None（不裁剪成好看的数）。"""
    try:
        c = (sell_q["ask"] - buy_q["bid"]) * 100
    except (TypeError, KeyError):
        return None
    return round(c, 4) if 0 <= c <= width_usd else None


# ── 结算 ────────────────────────────────────────────────────────────────

def _beyond(kind, K, px):
    return px < K if kind == "P" else px > K


def settle_leg(leg: dict, *, session: date, bars: list, entry_leg: dict | None, exit_info: dict | None,
               fee: float = CONFIG["fee_round_trip"]) -> dict | None:
    """bars: [(date, high, low, close)] 覆盖 session..expiry；缺到期 bar 返回 None（未成熟不结算）。"""
    exp = date.fromisoformat(leg["expiry"])
    win = [b for b in bars if session <= b[0] <= exp]
    if not win or win[-1][0] != exp:
        return None
    k, S, W = leg["side"], leg["sell"], leg["width_usd"]
    settle = win[-1][3]
    intrinsic = min(W, max(0.0, ((S - settle) if k == "P" else (settle - S)) * 100))
    ohlc = all(h is not None and lo is not None for _, h, lo, _c in win)
    trig = next((b[0] for b in win[:-1] if _beyond(k, S, b[3])), None)
    res = {"expiry_close": settle, "endpoint_breach": _beyond(k, S, settle),
           "any_close_breach": any(_beyond(k, S, b[3]) for b in win),
           "intraday_breach": (any((lo < S) if k == "P" else (h > S) for _, h, lo, _c in win) if ohlc else None),
           "trigger_date": trig.isoformat() if trig else None, "pnl": {}, "max_risk": {}}

    def put(basis, credit, cost):
        if credit is None:
            res["pnl"][basis] = None; res["max_risk"][basis] = None; return
        res["pnl"][basis] = round(credit - cost - fee, 4)
        res["max_risk"][basis] = round(W - credit + fee, 4)

    sc = (leg.get("snapshot_credit") or {}).get("mid_give")
    put("snapshot_model", sc, intrinsic)
    ec = entry_leg["credit"] if (entry_leg and entry_leg.get("valid")) else None
    put("hold_quote_conservative", ec["conservative"] if ec else None, intrinsic)
    put("hold_quote_mid_give", ec["mid_give"] if ec else None, intrinsic)
    # 退出规则：收盘越过卖腿 → 下一盘中窗口平仓；缺退出报价 = 未知，不偷换成持有到期
    if ec is None:
        put("exit_rule_quote_conservative", None, 0)
    elif trig is None:
        put("exit_rule_quote_conservative", ec["conservative"], intrinsic)
    else:
        cost = (exit_info or {}).get("cost_conservative")
        if cost is None:
            res["pnl"]["exit_rule_quote_conservative"] = None
            res["max_risk"]["exit_rule_quote_conservative"] = None
            res["exit_status"] = "triggered_unpriced"
        else:
            put("exit_rule_quote_conservative", ec["conservative"], cost)
            res["exit_status"] = "exited_at_quote"
    return res


# ── 统计（W06）──────────────────────────────────────────────────────────

def _norm(o: dict, basis: str):
    p, r = o["pnl"].get(basis), o["max_risk"].get(basis)
    return p / r if (p is not None and r) else None


def date_block_bootstrap(groups: dict, iters: int = 20000, seed: int = 20260926):
    """groups: {date: [值…]}。按日期整块重采样，返回 (均值, 下界, 上界)；不足 5 个日期返回 None 区间。"""
    ds = sorted(groups)
    allv = [v for d in ds for v in groups[d]]
    if not allv:
        return None, None, None
    m = st.mean(allv)
    if len(ds) < 5:
        return m, None, None
    rnd = random.Random(seed); vals = []
    for _ in range(iters):
        s = [v for _ in ds for v in groups[ds[rnd.randrange(len(ds))]]]
        vals.append(st.mean(s))
    vals.sort()
    return m, vals[int(0.025 * iters)], vals[int(0.975 * iters) - 1]


def zero_event_upper(n: int, alpha: float = 0.05) -> float | None:
    """n 次独立观测零事件时事件率的单侧 (1−alpha) 精确上界。"""
    return 1 - alpha ** (1 / n) if n > 0 else None


def judge(lo, hi, *, min_useful: float = 0.0) -> str:
    if lo is None:
        return "样本不足"
    if lo > min_useful:
        return "支持"
    if hi <= 0:
        return "不支持"
    return "未决"


def paired_summary(rows: list[dict], basis: str = CONFIG["primary_basis"],
                   b_rule: str = CONFIG["primary_b"], *, mode: str = "prospective") -> dict:
    """A 绝对收益与 A−B 配对差（按日期分块）。只收指定 mode 的已结算行。"""
    a_by, d_by, cover = {}, {}, {"opportunities": 0, "a_priced": 0, "pairs": 0, "identical_pairs": 0}
    for r in rows:
        if (r.get("identity") or {}).get("mode") != mode or not r.get("outcome"):
            continue
        for side in CONFIG["sides"]:
            cover["opportunities"] += 1
            oa = r["outcome"].get(f"{side}-A"); ob = r["outcome"].get(f"{side}-{b_rule}")
            na = _norm(oa, basis) if oa else None
            if na is None:
                continue
            cover["a_priced"] += 1
            a_by.setdefault(r["session"], []).append(na)
            nb = _norm(ob, basis) if ob else None
            if nb is None:
                continue
            cover["pairs"] += 1
            leg_b = next((l for l in r["legs"] if l["leg_id"] == f"{side}-{b_rule}"), {})
            cover["identical_pairs"] += bool(leg_b.get("same_as_A"))
            d_by.setdefault(r["session"], []).append(na - nb)
    am, alo, ahi = date_block_bootstrap(a_by)
    dm, dlo, dhi = date_block_bootstrap(d_by)
    return {"basis": basis, "b_rule": b_rule, "mode": mode, "coverage": cover,
            "n_dates_A": len(a_by), "n_dates_pairs": len(d_by),
            "A_mean_norm": am, "A_ci": [alo, ahi], "A_verdict": judge(alo, ahi),
            "AminusB_mean_norm": dm, "AminusB_ci": [dlo, dhi], "AminusB_verdict": judge(dlo, dhi),
            "note": "区间按日期整块 bootstrap；同日多品种/两侧不当独立；50 笔只是数据复核节点，不是放行线。"}
