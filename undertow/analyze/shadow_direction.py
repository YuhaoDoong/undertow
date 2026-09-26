"""方向次要分析（预登记 dir-analysis-v1.1；Codex 009/010/011 设计，用户 2026-09-26 论点）。纯函数，无 I/O。

用户原话（节选）：「期权墙可以提高方向性的准确率，而方向性可以提高卖方价差的胜率……卖方价差又可以反过来
提高方向性交易的胜率，因为只要不要出现大幅度反方向即可盈利。我觉得这三者都是要结合在一起的。」
预登记：docs/prereg/2026-09-26_direction_v1.1.md（取代 v1，v1 原件保留）。本文件与依赖函数的指纹冻结在其 JSON。

不改 v5 的任何东西。在 v5 已每天记录两侧的数据上：
  记 A=墙选腿、B1=距决策价 ≥1.0×ATR14 的首个挂牌虚值档（同美元宽度），a=按方向信号的顺向侧、o=逆向侧；
  Y=v5 主终点（到期前最后交易日收盘窗退出政策）的 损益/事前最大风险，区间值按 v5 界限规则。
  H-dir       Y(B1,a) − Y(B1,o)       同品种同日配对
  H-wall|dir  Y(A,a)  − Y(B1,a)       同品种同日配对

v1.1 只修「实现与承诺不一致」（Codex 011 D01–D05），不改假设、信号、映射、门槛、α、家族、纳入起点与检验日：
  D01 准入在本模块内完成：session 已认证且 recorded_at < 09:30 ET；available_at < recorded_at（带时区比较）；
      快照/台账身份齐全；映射版本；台账写入时的方向算法指纹 = 冻结值。不符按原因计数，不在外面静默过滤。
  D02 先按「日历交易日 × 品种」建机会表再分类；成熟但缺结果的留在分母里。
  D03 动态方向与固定卖 put/call 只在共同样本键上比较，并列出键；全机会描述另列。
  D05 判定用语区分「未证实超过门槛」与「排除实用增量」；依赖函数指纹一并冻结。
另附描述性诊断 dir-diagnostics-v1（不判定、不作过滤）：开仓前/后按方向取号的标的移动（ATR 为单位）。
"""
from __future__ import annotations

import statistics as st
from datetime import date, datetime
from zoneinfo import ZoneInfo

from undertow.analyze import shadow as sh
from undertow.core import market_calendar as mc

ET = ZoneInfo("America/New_York")

ANALYSIS = {
    "version": "dir-analysis-v1.1-20260926",
    "supersedes": "dir-analysis-v1-20260926",
    "base_config_version": "shadow-v5-20260926",
    "signal": "decision.flow.call_direction",
    "mapping": {"偏多": "P", "偏空": "C"},           # 其余（中性/空/None）= 无方向，只进分母
    "mapping_version": "dir-map-v1",
    "call_code_sha": "31057c35f587532f",             # signal_ledger.call_code_sha() 冻结值（方向算法相关文件）
    "eligible_from": "2026-09-28",
    "formal_date": "2026-12-31",
    "pool": "etf",                                    # 七个主池品种分别检验，不合并
    "hypotheses": ["H-dir", "H-wall|dir"],
    "family_size": 14,                                # 2 假设 × 7 品种（独立的次要家族，不是全项目 FWER）
    "alpha_one_sided": 0.05 / 14,
    "economic_delta": 0.03,                           # 未校准：≈ 多付一次往返费用相对典型最大风险；设计选择，不保证功效
    "min_pair_dates": 20,
    "min_pairable_rate": 0.5,
    "estimate": "bounds",
    "tail_quantile": 0.10,
    "diagnostics_version": "dir-diagnostics-v1",
}

# 机会表类别（每个 日历交易日×品种 恰好一类）。可配对率的分母 = PAIRABLE_DENOM 各类之和。
CATEGORIES = ("not_generated", "identity_fail", "no_direction", "no_candidate", "beyond_formal_date",
              "immature", "matured_missing_result", "entry_missing", "unpriced", "unbounded", "complete")
PAIRABLE_DENOM = ("matured_missing_result", "entry_missing", "unpriced", "unbounded", "complete")


# ── D01：准入 ──────────────────────────────────────────────────────────

def _aware(s):
    try:
        t = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def aligned_sides(row: dict):
    """(顺向侧, 逆向侧) 或 None（无方向）。"""
    d = ((row.get("decision") or {}).get("flow") or {}).get("call_direction")
    a = ANALYSIS["mapping"].get(d)
    return None if a is None else (a, "C" if a == "P" else "P")


def eligibility(row: dict) -> tuple[str, str]:
    """("ok"|"identity_fail"|"no_direction", 原因)。session 身份先于方向：未认证的行不看方向。"""
    idt = row.get("identity") or {}
    if idt.get("mode") != "prospective" or idt.get("status") != "certified":
        return "identity_fail", "session 非前瞻或未认证"
    rec = _aware(row.get("recorded_at"))
    s = date.fromisoformat(row["session"])
    if rec is None or rec >= datetime(s.year, s.month, s.day, 9, 30, tzinfo=ET):
        return "identity_fail", "recorded_at 缺失/无时区/不早于当日 09:30 ET"
    if aligned_sides(row) is None:
        return "no_direction", "无方向"
    f = (row.get("decision") or {}).get("flow") or {}
    av = _aware(f.get("available_at"))
    if av is None:
        return "identity_fail", "available_at 缺失或无时区"
    if not av < rec:
        return "identity_fail", "available_at 不早于 recorded_at"
    for k in ("snapshot_sha", "prev_snapshot_sha", "ledger_row_sha"):
        if not f.get(k):
            return "identity_fail", f"{k} 缺失"
    if f.get("mapping") != ANALYSIS["mapping_version"]:
        return "identity_fail", "映射版本不符"
    if f.get("ledger_code_sha") != ANALYSIS["call_code_sha"]:
        return "identity_fail", "台账方向算法指纹缺失或与冻结值不符"
    return "ok", ""


# ── D02：机会表 ────────────────────────────────────────────────────────

def _leg(row, side, rule):
    return next((l for l in row.get("legs", []) if l["leg_id"] == f"{side}-{rule}"), None)


def _v(row, side, rule, basis):
    return sh._vals((row.get("outcome") or {}).get(f"{side}-{rule}"), basis, "bounds")


def _need(row, hypothesis):
    a, o = aligned_sides(row)
    b1 = sh.CONFIG["primary_b"]
    return [(a, b1), (o, b1)] if hypothesis == "H-dir" else [(a, "A"), (a, b1)]


def classify(row: dict | None, hypothesis: str, *, basis: str, as_of: date, formal: bool):
    """某 日×品种 机会在某项假设下的 (类别, 原因, 两条腿的区间值或 None)。"""
    if row is None:
        return "not_generated", "", None
    cat, why = eligibility(row)
    if cat != "ok":
        return cat, why, None
    need = _need(row, hypothesis)
    legs = [_leg(row, s, r) for s, r in need]
    if any(l is None or l.get("status") != "candidate" for l in legs):
        return "no_candidate", "", None
    if formal and any(l["expiry"] > ANALYSIS["formal_date"] for l in legs):
        return "beyond_formal_date", "", None
    out = row.get("outcome") or {}
    os_ = [out.get(f"{s}-{r}") for s, r in need]
    sts = [((o or {}).get("status") or {}).get(basis) for o in os_]
    if any(o is None for o in os_) or "immature" in sts:
        exit_day = mc.prev_trading_day(date.fromisoformat(max(l["expiry"] for l in legs)))
        if exit_day is not None and as_of > exit_day:
            return "matured_missing_result", "退出日已过但无结算结果", None
        return "immature", "", None
    if any(str(s).startswith("entry_") for s in sts):
        return "entry_missing", "", None
    vals = [_v(row, s, r, basis) for s, r in need]
    if any(v is None for v in vals):
        return "unpriced", "", None
    if any(v[0] is None for v in vals):
        return "unbounded", "", vals
    return "complete", "", vals


def _window(as_of: date):
    ident = sh.formal_identity(as_of)
    start = date.fromisoformat(ANALYSIS["eligible_from"])
    end = min(as_of, date.fromisoformat(ANALYSIS["formal_date"])) if ident == "formal" else as_of
    days = mc.trading_days(start, end) if start <= end else []
    return ident, days


def opportunity_table(rows: list[dict], inst: str, hypothesis: str, *, basis: str, as_of: date) -> dict:
    """日历交易日（eligible_from … as_of，正式时截到检验日）× 品种，每格恰好一类。行不预先过滤。"""
    ident, days = _window(as_of)
    if days is None:
        return {"status": "calendar_unknown", "table": None, "reasons": {}, "days": None, "items": [],
                "pairable_rate": None, "pairable_denominator": list(PAIRABLE_DENOM)}
    by = {r["session"]: r for r in rows if r.get("instrument") == inst}
    table, reasons, items = dict.fromkeys(CATEGORIES, 0), {}, []
    for d in days:
        cat, why, vals = classify(by.get(d.isoformat()), hypothesis, basis=basis, as_of=as_of,
                                  formal=ident == "formal")
        table[cat] += 1
        if why and cat in ("identity_fail", "matured_missing_result"):
            reasons[why] = reasons.get(why, 0) + 1
        items.append({"session": d.isoformat(), "category": cat, "vals": vals})
    denom = sum(table[c] for c in PAIRABLE_DENOM)
    return {"status": "ok", "table": table, "reasons": reasons, "days": len(days), "items": items,
            "pairable_rate": (table["complete"] + table["unbounded"]) / denom if denom else None,
            "pairable_denominator": list(PAIRABLE_DENOM)}


# ── 判定（D05 用语）────────────────────────────────────────────────────

def judge(ci: dict, n_dates: int, pairable_rate) -> str:
    d = ANALYSIS["economic_delta"]
    if pairable_rate is None:
        return "证据不足（尚无已成熟、可评估的机会）"
    if pairable_rate < ANALYSIS["min_pairable_rate"]:
        return "证据不足（可配对率低于设计门槛）"
    if ci["status"] == "residual_unknown":
        return "未决（残腿处置未知）"
    if n_dates < ANALYSIS["min_pair_dates"] or ci["status"] in ("empty", "insufficient"):
        return "证据不足（配对日期不足）"
    if ci["status"] != "ok":
        return {"degenerate": "退化（不判）"}.get(ci["status"], ci["status"])
    lo, hi = ci["lo"], ci["hi"]
    if lo > d:
        return "支持（校正后下界超过经济门槛）"
    if hi <= 0:
        return "不支持（校正后上界 ≤ 0）"
    if hi < d:
        return ("统计为正，但排除实用增量（上界 < 经济门槛）" if lo > 0
                else "排除实用增量（校正后上界 < 经济门槛）")
    if lo > 0:
        return "统计为正，尚未证实超过经济门槛"
    return "未决"


def _diff(x, y):
    """区间差 [x_lo−y_hi, x_hi−y_lo]；调用方保证下界已知。"""
    return (x[0] - y[1], x[1] - y[0])


def _ci(items):
    lo_by, hi_by, unb, n = {}, {}, 0, 0
    for it in items:
        if it["category"] == "unbounded":
            unb += 1
        elif it["category"] == "complete":
            dlo, dhi = _diff(*it["vals"])
            lo_by.setdefault(it["session"], []).append(dlo); hi_by.setdefault(it["session"], []).append(dhi)
            n += 1
    a_ = ANALYSIS["alpha_one_sided"]
    return sh.interval_ci(lo_by, hi_by, unb, q_lo=a_, q_hi=1 - a_), n, unb, len(lo_by)


def instrument_report(rows: list[dict], inst: str, *, basis: str = sh.CONFIG["primary_basis"],
                      as_of: date) -> dict:
    ident = sh.formal_identity(as_of)
    out = {"instrument": inst, "identity": ident, "analysis_version": ANALYSIS["version"],
           "note": "门槛（20 个日期、0.5 可配对率、δ=0.03）是研究设计选择，不保证足够的统计功效；"
                   "「未证实」不等于「已排除」，只有上界 < δ 才算排除实用增量。"}
    for h in ANALYSIS["hypotheses"]:
        ot = opportunity_table(rows, inst, h, basis=basis, as_of=as_of)
        ci, n, unb, nd = _ci(ot["items"])
        v = judge(ci, nd, ot["pairable_rate"])
        out[h] = {"opportunities": {k: ot[k] for k in ("status", "table", "reasons", "days", "pairable_rate",
                                                         "pairable_denominator")},
                  "n_pairs": n, "n_unbounded": unb, "n_dates": nd, "ci_one_sided_alpha": ANALYSIS["alpha_one_sided"],
                  "ci": ci, "verdict": v if ident == "formal" else f"探索·{v}"}
    ok_rows = _eligible_rows(rows, inst, as_of)
    out["interaction"] = interaction(ok_rows, basis)
    out["common_sample"] = common_sample(ok_rows, basis)
    out["all_opportunities"] = all_opportunities(ok_rows, basis)
    out["direct_direction"] = direct_direction(ok_rows)
    out["diagnostics"] = diagnostics(ok_rows)
    return out


def _eligible_rows(rows, inst, as_of):
    """描述性部分用：窗口内、准入通过（有方向）的行。正式时再按检验日截断候选到期。"""
    ident, days = _window(as_of)
    keep = {d.isoformat() for d in (days or [])}
    out = [r for r in rows if r.get("instrument") == inst and r["session"] in keep and eligibility(r)[0] == "ok"]
    if ident == "formal":
        out = [r for r in out if all(l["expiry"] <= ANALYSIS["formal_date"]
                                     for l in r.get("legs", []) if l.get("status") == "candidate")]
    return sorted(out, key=lambda r: r["session"])


# ── 描述性（不参与判定）────────────────────────────────────────────────

def interaction(rows, basis) -> dict:
    """[Y(A,a)−Y(B1,a)] − [Y(A,o)−Y(B1,o)]，区间传播；任一下界未知计无界。"""
    b1 = sh.CONFIG["primary_b"]
    lo_by, hi_by, unb, n = {}, {}, 0, 0
    for r in rows:
        a, o = aligned_sides(r)
        v = [_v(r, a, "A", basis), _v(r, a, b1, basis), _v(r, o, "A", basis), _v(r, o, b1, basis)]
        if any(x is None for x in v):
            continue
        if any(x[0] is None for x in v):
            unb += 1; continue
        d = _diff(_diff(v[0], v[1]), _diff(v[2], v[3]))
        lo_by.setdefault(r["session"], []).append(d[0]); hi_by.setdefault(r["session"], []).append(d[1]); n += 1
    return {"n_pairs": n, "n_unbounded": unb, "ci": sh.interval_ci(lo_by, hi_by, unb), "identity": "描述性"}


def _desc(xs):
    if not xs:
        return {"n": 0, "mean": None, "win_rate": None, "worst_decile_mean": None, "worst_decile_points": 0}
    k = max(1, int(len(xs) * ANALYSIS["tail_quantile"]))
    return {"n": len(xs), "mean": st.fmean(xs), "win_rate": sum(x > 0 for x in xs) / len(xs),
            "worst_decile_mean": st.fmean(sorted(xs)[:k]), "worst_decile_points": k}


def _point(row, side, rule, basis):
    v = _v(row, side, rule, basis)
    return v[0] if (v is not None and v[0] is not None and v[0] == v[1]) else None


def common_sample(rows, basis) -> dict:
    """D03：动态方向 vs 固定卖 put / 固定卖 call，只在共同键上（该规则两侧都有点值）。B1 与 A 各一组键。"""
    out = {"identity": "描述性·共同样本",
           "caveat": "只含两侧都为点值的日期，是条件样本；最差 10% 在小样本下只有少数几个点"}
    for rule in (sh.CONFIG["primary_b"], "A"):
        keys, ser = [], {"aligned": [], "counter": [], "always_put": [], "always_call": []}
        for r in rows:
            vp, vc = _point(r, "P", rule, basis), _point(r, "C", rule, basis)
            if vp is None or vc is None:
                continue
            a, o = aligned_sides(r)
            val = {"P": vp, "C": vc}
            keys.append(r["session"])
            ser["aligned"].append(val[a]); ser["counter"].append(val[o])
            ser["always_put"].append(vp); ser["always_call"].append(vc)
        out[rule] = {"keys": keys, "n_keys": len(keys), **{k: _desc(v) for k, v in ser.items()}}
    return out


def all_opportunities(rows, basis) -> dict:
    """全机会描述：每格各自的点值样本（样本不同，不可互相比较）；区间与无界只计数。"""
    b1 = sh.CONFIG["primary_b"]
    picks = {"B1_aligned": (0, b1), "B1_counter": (1, b1), "A_aligned": (0, "A"), "A_counter": (1, "A")}
    out = {"identity": "描述性·各格样本不同，不可互相比较"}
    for name, (i, rule) in picks.items():
        xs, interval = [], 0
        for r in rows:
            v = _v(r, aligned_sides(r)[i], rule, basis)
            if v is None:
                continue
            if v[0] is None or v[0] != v[1]:
                interval += 1; continue
            xs.append(v[0])
        out[name] = {**_desc(xs), "n_interval_or_unbounded": interval}
    return out


def _leg_underlying(row, leg, wkey, purpose):
    """该腿在该窗口【实际采用的那次尝试】记录的标的价；取不到 → None。"""
    w = sh.window_leg(row, leg, wkey, purpose)
    if w.get("status") != "valid":
        return None
    for a in ((row.get("windows") or {}).get(wkey) or {}).get("attempts", []):
        if a.get("started_at") == w.get("at"):
            u = a.get("underlying") or {}
            v = u.get("freshest") if isinstance(u, dict) else None
            return float(v) if isinstance(v, (int, float)) and v > 0 else None
    return None


def _entry_exit_px(row):
    """顺向 B1 腿的入场标的价与退出标的价（均对齐到该腿实际采用的报价尝试）。"""
    a, _ = aligned_sides(row)
    leg = _leg(row, a, sh.CONFIG["primary_b"])
    if leg is None or leg.get("status") != "candidate":
        return None, None
    p0 = _leg_underlying(row, leg, f"{row['session']}|open", "entry")
    x = mc.prev_trading_day(date.fromisoformat(leg["expiry"]))
    p1 = _leg_underlying(row, leg, f"{x.isoformat()}|close", "exit") if x else None
    return p0, p1


def direct_direction(rows) -> dict:
    """直接做方向：入场→到期前一交易日收盘窗的标的变动符号是否与方向一致。与价差收益单位不同，不可直接比。"""
    hits = n = 0
    for r in rows:
        p0, p1 = _entry_exit_px(r)
        if p0 is None or p1 is None or p1 == p0:
            continue
        n += 1
        hits += (p1 > p0) == (aligned_sides(r)[0] == "P")
    return {"n": n, "hit_rate": hits / n if n else None,
            "note": "符号命中率；与价差的 损益/风险 单位不同，不可直接比较"}


def diagnostics(rows) -> dict:
    """dir-diagnostics-v1。d=+1 偏多 / −1 偏空：
      pre_move_ATR  = d×(入场标的价 − 决策价)/ATR14   （决策价=T 之前最后日线收盘）
      post_move_ATR = d×(退出标的价 − 入场标的价)/ATR14
    入场/退出标的价对齐到顺向 B1 腿实际采用的报价尝试。「前收盘→信号可用」与「信号可用→入场」两段
    在信号可用时刻没有标的价，记未知 —— pre_move 是两段之和，不能全算成信号可用后错过的行情。"""
    pre, post, unknown = [], [], 0
    for r in rows:
        dec = r.get("decision") or {}
        base, atr = dec.get("base_close"), dec.get("atr14")
        p0, p1 = _entry_exit_px(r)
        if not base or not atr or p0 is None:
            unknown += 1; continue
        d = 1 if aligned_sides(r)[0] == "P" else -1
        pre.append(d * (p0 - base) / atr)
        if p1 is not None:
            post.append(d * (p1 - p0) / atr)
    f = lambda xs: {"n": len(xs), "mean": st.fmean(xs) if xs else None, "median": st.median(xs) if xs else None}
    return {"version": ANALYSIS["diagnostics_version"], "pre_move_ATR": f(pre), "post_move_ATR": f(post),
            "n_unknown": unknown, "split_close_to_available_to_entry": "未知（信号可用时刻无标的价）",
            "identity": "描述性：只看信息发生在哪一段；不据此过滤、不选阈值（任何规则需新预登记版本）"}


# ── D05：依赖指纹 ──────────────────────────────────────────────────────

def dependency_fingerprints() -> dict:
    """冻结的依赖：本模块用到的 v5 取值/统计/窗口函数、日历模块全文、v5 配置哈希。变化即需修订记录或新版本。"""
    import hashlib
    import inspect

    def h(obj):
        return hashlib.sha256(inspect.getsource(obj).encode()).hexdigest()[:16]
    return {"shadow._vals": h(sh._vals), "shadow._norm": h(sh._norm), "shadow.interval_ci": h(sh.interval_ci),
            "shadow.block_bootstrap": h(sh.block_bootstrap), "shadow.formal_identity": h(sh.formal_identity),
            "shadow.window_leg": h(sh.window_leg), "core.market_calendar": h(mc),
            "shadow.config_hash": sh.config_hash()}
