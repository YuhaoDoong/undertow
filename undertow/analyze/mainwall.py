"""主墙近侧卖方价差：前瞻研究账的纯计算（Codex 056 合同 mainwall_contract v0.1；参数未校准，不是实盘放行）。

用户 2026-10-17：「只要我们墙位可靠，实际上当天离哪个墙近就可以做哪边墙的卖墙价差。」
唯一主问题：在同一套「主墙最近侧」选择下，卖主墙行权（A）是否比卖同侧 1·ATR 定距行权（B）有更好的含费模拟报价收益。
这只比较「给定墙侧下的行权选择规则」，不能单独证明墙的因果支撑，也不能证明近侧方向比远侧准。

时序（全部在调用方按此顺序落盘）：
1. 盘前：新鲜 T−1 输入（collect.fresh_daily + derive_inputs）→ 交易到期 E（沿用 v5 target_expiry，2–4 自然日）
   → 每侧主墙（wall_map）。墙图冻结后不按日后价格重画。
2. 入场窗 10:00–10:20 ET：先观察并冻结标的价 Sdec，再按冻结墙选近侧（choose_side），构造 A/B 候选（candidates），
   之后才取盘口。墙已被 Sdec 越过 → 该侧不合格，不换更远一层；两侧等距 → 弃权。
3. 退出/费用/价格沿用 v5：pre_expiry_exit_policy、mid_give 0.25、每组 $3.20；payoff-floor-v1 只作未校准偏好。

本模块无 I/O、无网络；合约只要有 kind / strike / expiry / open_interest 属性即可（测试用合成对象）。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta

from undertow.analyze import shadow as sh

CONFIG = {
    "version": "mainwall-v0.1-20261017",
    "contract": "GPTcom/20261017T015157+0800_codex-gpt-6_056_mainwall_review/02_PLAN/mainwall_contract.md",
    "status": "研究约定，参数未校准；不是实盘放行（墙位卖方价差实盘停用不变）",
    "instruments": ["gold", "silver"],
    "dte": [2, 4],                                   # 交易到期 E：v5 target_expiry 同义
    "band": 0.25,                                    # 主墙价域：前收虚值侧 25%（借用 gamma.WALL_PICK_MAX_DIST，未校准）
    "oi_basis": "expiry_ge_E",                       # 主分子：到期 ≥ E 的全部期限 OI 按行权价累加
    "width": {"rule": "2x_modal_strike_step", "band": 0.05},
    "b_atr": 1.0,
    "mid_give": sh.CONFIG["mid_give"],
    "fee_spread": sh.CONFIG["fee_round_trip"],       # 每组 2 张 × 往返
    "fee_condor": 2 * sh.CONFIG["fee_round_trip"],   # 四腿铁鹰
    "entry_window": "open",                          # v5 quote 窗口（10:00–10:20 ET），时刻只由 shadow.window_bounds 给出
    "primary_basis": "pre_expiry_exit_policy",
    "payoff_floor": "payoff-floor-v1-20261014",
    "stage_check": "2026-12-31",                     # 独立阶段检查，不继承 v5 的确认资格
}


def config_hash(cfg: dict = CONFIG) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


# ── 输入 ────────────────────────────────────────────────────────────────

def derive_inputs(rec: dict, ser, T: date) -> dict:
    """fresh_daily 记录 + 同一条序列 → ATR14 与语境，并核对三者截止同一日（expected=T−1）。

    返回 rec 的副本，加 atr14 / context / asof；任何一项没截到 expected → status=incomplete（不拿旧 ATR 配新收盘）。
    """
    from undertow.analyze.spread_ledger import decision_context
    from undertow.analyze.stretch import _atr_series
    out = dict(rec)
    if rec.get("status") != "fresh" or ser is None:
        return out
    atr = _atr_series(ser.highs, ser.lows, ser.closes, 14)
    ctx = decision_context(ser.highs, ser.lows, ser.closes, ser.dates, T)
    a = atr[-1] if atr else None
    out.update(atr14=round(a, 6) if (a is not None and math.isfinite(a) and a > 0) else None, context=ctx,
               asof={"hlc": rec.get("base_date"), "atr14": ser.dates[-1].isoformat() if ser.dates else None,
                     "context": ctx.get("asof")})
    if out["atr14"] is None or len({rec.get("expected_base_date"), *out["asof"].values()}) != 1:
        out["status"] = "incomplete"
    return out


# ── 主墙 ────────────────────────────────────────────────────────────────

def _otm(kind: str, K: float, px: float) -> bool:
    return K < px if kind == "P" else K > px


def wall_map(contracts, T: date, E: date, cprev: float, kind: str, *, band: float = CONFIG["band"]) -> dict:
    """某一侧的排名主墙（每侧至多一道）。

    候选档：相对前收 cprev 虚值、且 |K/cprev − 1| ≤ band。主分子 = 到期 ≥ E 的 OI 按行权价累加（早于 E 的不算）。
    取主分子最大且 > 0 的一档；相等先取离 cprev 更近，再按行权价数值升序。不设「几倍 / 多少张才可靠」闸门 ——
    第二名、份额、期限分解都记连续值（名称是「排名主墙」，不是已验证的可靠墙）。
    返回 {"status": "ok"|"no_wall", ...}。
    """
    lo, hi = (cprev * (1 - band), cprev) if kind == "P" else (cprev, cprev * (1 + band))
    by: dict[float, dict] = {}
    for c in contracts:
        if c.kind != kind or c.expiry < T:
            continue
        K = float(c.strike)
        if not _otm(kind, K, cprev) or not (lo <= K <= hi):
            continue
        oi = c.open_interest or 0
        if oi <= 0:
            continue
        d = by.setdefault(K, {"ge_E": 0, "all_live": 0, "le14": 0, "at_E": 0, "lt_E": 0})
        d["all_live"] += oi
        if c.expiry >= E:
            d["ge_E"] += oi
        else:
            d["lt_E"] += oi
        if c.expiry == E:
            d["at_E"] += oi
        if c.expiry <= T + timedelta(days=14):
            d["le14"] += oi
    out = {"kind": kind, "band": [round(lo, 4), round(hi, 4)], "oi_basis": CONFIG["oi_basis"],
           "n_strikes": sum(1 for v in by.values() if v["ge_E"] > 0)}
    ranked = sorted(((K, v) for K, v in by.items() if v["ge_E"] > 0),
                    key=lambda kv: (-kv[1]["ge_E"], abs(kv[0] - cprev), kv[0]))
    if not ranked:
        return {**out, "status": "no_wall"}
    (K1, v1) = ranked[0]
    total = sum(v["ge_E"] for _, v in ranked)
    second = ranked[1] if len(ranked) > 1 else None
    return {**out, "status": "ok", "strike": K1, "oi": v1["ge_E"], "decomp": v1,
            "dist_pct_prev": round(abs(K1 / cprev - 1) * 100, 4),
            "second": {"strike": second[0], "oi": second[1]["ge_E"]} if second else None,
            "ratio_top2": round(v1["ge_E"] / second[1]["ge_E"], 4) if second else None,
            "share": round(v1["ge_E"] / total, 4)}


# ── 入场窗：近侧与候选 ──────────────────────────────────────────────────

def choose_side(walls: dict, sdec: float, atr: float) -> dict:
    """walls: {"P": wall_map(...), "C": ...}（盘前冻结）。返回 {"near","far","reason","sides":{k: {...}}}。

    某侧无墙 → no_wall；墙已被 Sdec 越过或恰在墙上 → already_beyond（d 必须严格 > 0，不换更远一层）。
    两侧都合格取 |K−Sdec|/ATR 更小者；相等 → 弃权 tie。只一侧合格 → 该侧。
    """
    sides = {}
    for k in ("P", "C"):
        w = walls.get(k) or {}
        if w.get("status") != "ok":
            sides[k] = {"eligible": False, "reason": "no_wall"}
        elif not _otm(k, w["strike"], sdec):
            sides[k] = {"eligible": False, "reason": "already_beyond", "strike": w["strike"]}
        else:
            sides[k] = {"eligible": True, "strike": w["strike"], "dist_atr": round(abs(w["strike"] - sdec) / atr, 6)}
    ok = [k for k in ("P", "C") if sides[k]["eligible"]]
    if not ok:
        reason = "already_beyond" if any(s["reason"] == "already_beyond" for s in sides.values()) else "no_wall"
        return {"near": None, "far": None, "reason": reason, "sides": sides}
    if len(ok) == 1:
        return {"near": ok[0], "far": None, "reason": "single_side", "sides": sides}
    dp, dc = sides["P"]["dist_atr"], sides["C"]["dist_atr"]
    if abs(dp - dc) < 1e-9:
        return {"near": None, "far": None, "reason": "tie", "sides": sides}
    near = "P" if dp < dc else "C"
    return {"near": near, "far": "C" if near == "P" else "P", "reason": "nearest", "sides": sides}


def _leg(role: str, kind: str, rule: str, E: date, width: float | None, pick, why, sdec, atr) -> dict:
    leg = {"leg_id": f"{role}-{kind}-{rule}", "role": role, "side": kind, "rule": rule, "expiry": E.isoformat(),
           "width_rule_usd": round(width * 100, 4) if width else None,
           "status": "candidate" if pick else "no_candidate", "reason": why}
    if pick:
        S, B = pick
        leg.update(sell=S, buy=B, width_usd=round(abs(S - B) * 100, 4),
                   distance_atr=round(abs(S - sdec) / atr, 6), distance_pct=round(abs(S / sdec - 1) * 100, 4))
    return leg


def candidates(strikes: dict, E: date, sdec: float, atr: float, walls: dict, widths: dict, choice: dict) -> list[dict]:
    """近侧（role=near）与远侧（role=far，次要描述）各一组 A/B。strikes / widths 按侧给出 E 的挂牌行权价与冻结宽度。

    A：卖精确墙 K、买外侧同宽（v5 _buy_leg，必须恰好挂牌，缺失不挪腿）。
    B：同侧 1·ATR 定距（v5 pick_b 以 Sdec 为现价），同宽；记实际距离残差；A=B 时保留并标 same_as_A。
    """
    out = []
    for role in ("near", "far"):
        k = choice.get(role)
        if k is None:
            continue
        ks, W = strikes.get(k) or [], widths.get(k)
        if not W:
            a = b = (None, "no_width")
        else:
            K = walls[k]["strike"]
            if K not in ks:
                a = (None, "wall_strike_not_listed_for_E")
            else:
                buy = sh._buy_leg(ks, K, k, W)
                a = ((K, buy), None) if buy is not None else (None, "no_same_width_protective")
            b = sh.pick_b(ks, sdec, atr, k, CONFIG["b_atr"], W)
        la = _leg(role, k, "A", E, W, a[0], a[1], sdec, atr)
        lb = _leg(role, k, "B", E, W, b[0], b[1], sdec, atr)
        if lb["status"] == "candidate":
            lb["distance_residual_atr"] = round(lb["distance_atr"] - CONFIG["b_atr"], 6)
        lb["same_as_A"] = (la["status"] == lb["status"] == "candidate" and (la["sell"], la["buy"]) == (lb["sell"], lb["buy"]))
        out += [la, lb]
    return out


# ── 结算派生 ────────────────────────────────────────────────────────────

def payoff_check(credit_usd: float | None, width_usd: float, fee: float = CONFIG["fee_spread"]) -> dict:
    """payoff-floor-v1：含费最大收益 ≥ 含费最大亏损 10%。只是未校准偏好；不通过 = 模型不入场（确定弃权）。"""
    from undertow.analyze.risk_policy import payoff_floor
    if credit_usd is None:
        return {"status": "incomplete"}
    return payoff_floor(credit_usd - fee, width_usd - credit_usd + fee)


def row_view_after(row: dict, frozen_at: str | None) -> dict:
    """只保留冻结 Sdec 之后开始的报价尝试（合同：先冻结 Sdec，之后才取/接受盘口）。"""
    if not frozen_at:
        return row
    v = dict(row)
    v["windows"] = {wk: {"attempts": [a for a in (w.get("attempts") or []) if sh._after(a.get("started_at"), frozen_at)]}
                    for wk, w in (row.get("windows") or {}).items()}
    return v


def leg_outcome(leg: dict, row: dict, *, bars: list, now=None) -> dict:
    """一条候选的政策结果（主终点 pre_expiry_exit_policy）：settle 原样复用 v5，再叠加收益门槛。

    policy_pnl：确定弃权（payoff_reject）= 0；缺价/未成熟/残腿未知 = None（不折零）。
    """
    if leg.get("status") != "candidate":
        return {"policy": "no_candidate", "policy_pnl": 0.0, "reason": leg.get("reason")}
    view = row_view_after(row, (row.get("entry_decision") or {}).get("frozen_at"))
    res = sh.settle_leg(leg, view, bars=bars, now=now, fee=CONFIG["fee_spread"], stops=())
    basis = CONFIG["primary_basis"]
    ent = res.get("entry") or {}
    out = {"settle": res, "entry_status": ent.get("status")}
    if ent.get("status") != "valid":
        st = res["status"].get(basis)
        out.update(policy="immature" if st == "immature" else "quote_missing", policy_pnl=None)
        return out
    pf = payoff_check(ent["credit"]["conservative"], leg["width_usd"])
    out["payoff"] = pf
    if pf["status"] != "pass":
        out.update(policy="payoff_reject" if pf["status"] == "fail" else "payoff_incomplete",
                   policy_pnl=0.0 if pf["status"] == "fail" else None)
        return out
    pnl = res["pnl"].get(basis)
    st = res["status"].get(basis)
    out.update(policy="entered_model" if pnl is not None else ("immature" if st == "immature" else "exit_unknown"),
               policy_pnl=pnl, policy_bounds=res["pnl_bounds"].get(basis), basis_status=st)
    return out


def row_category(row: dict, outs: dict) -> str:
    """机会母表的最终类别（优先级事前冻结，一行只归一类）。"""
    pre = row.get("premarket") or {}
    if pre.get("status") != "ok":
        return pre.get("status") or "input_missing"
    ed = row.get("entry_decision")
    if ed is None:
        return "sdec_missing"
    if ed.get("choice", {}).get("near") is None:
        return {"no_wall": "no_wall", "already_beyond": "already_beyond", "tie": "tie"}.get(
            ed.get("choice", {}).get("reason"), "no_wall")
    a = outs.get(f"near-{ed['choice']['near']}-A") or {}
    p = a.get("policy")
    return {"no_candidate": "missing_contract", "quote_missing": "quote_missing", "payoff_reject": "payoff_reject",
            "payoff_incomplete": "quote_missing", "immature": "immature", "exit_unknown": "exit_unknown",
            "entered_model": "pairable" if (outs.get(f"near-{ed['choice']['near']}-B") or {}).get("policy_pnl") is not None
            else "entered_model"}.get(p, "unknown")


def condor(outs: dict, legs: dict) -> dict | None:
    """次要：两侧 A 同一次尝试入场、同到期时的四腿合成（费用 $6.40）。缺一侧 → None，不补 0。"""
    pa = [o for lid, o in outs.items() if lid.endswith("-A") and o.get("entry_status") == "valid"]
    if len(pa) != 2:
        return None
    e0, e1 = (o["settle"]["entry"] for o in pa)
    if e0["at"] != e1["at"]:
        return None
    sides = [lid for lid, o in outs.items() if lid.endswith("-A") and o.get("entry_status") == "valid"]
    if len({legs[l]["expiry"] for l in sides}) != 1:
        return None
    credit = e0["credit"]["conservative"] + e1["credit"]["conservative"]
    W = max(legs[l]["width_usd"] for l in sides)
    # 每侧 pnl 已各扣 $3.20 → 两侧相加正好是四腿 $6.40；任一侧点值未知（缺价/残腿实值未知）→ 整组未知
    pnls = [o["settle"]["pnl"].get(CONFIG["primary_basis"]) for o in pa]
    return {"credit": round(credit, 4), "fee": CONFIG["fee_condor"],
            "max_risk_at_expiry": round(W - credit + CONFIG["fee_condor"], 4),
            "pnl": round(sum(pnls), 4) if all(x is not None for x in pnls) else None}
