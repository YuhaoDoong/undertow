"""方向次要分析（预登记 dir-analysis-v1.2；Codex 009/010/011/012 设计，用户 2026-09-26 论点）。纯函数，无 I/O。

用户原话（节选）：「期权墙可以提高方向性的准确率，而方向性可以提高卖方价差的胜率……卖方价差又可以反过来
提高方向性交易的胜率，因为只要不要出现大幅度反方向即可盈利。我觉得这三者都是要结合在一起的。」
预登记：docs/prereg/2026-09-26_direction_v1.2.md（取代 v1.1，v1/v1.1 原件保留）。本文件与依赖函数的指纹冻结在其 JSON。

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

v1.2（Codex 012 R01–R04 + 描述性界限版）：
  R01 两份来源快照的抓取时刻都须已知且早于 recorded_at，available_at 须等于二者较晚者；
  R03 准入顺序 session → 信号来源 → 方向，来源不合格的中性行记 identity_fail，不混进弃权；
  R04 先看退出日截止、后读结果（as_of ≤ 退出日一律 immature）；描述性部分同样截止；
  R02 冻结的条件配对率保留；另列全日历覆盖（原始分子分母）与结论适用范围；有工程缺失时推广判「未决」。
      不新增闸门、不改门槛；当日采集窗口（09:30 ET）未结束的缺行记 collection_pending，不算漏采。
"""
from __future__ import annotations

import statistics as st
from datetime import date, datetime
from zoneinfo import ZoneInfo

from undertow.analyze import shadow as sh
from undertow.core import market_calendar as mc

ET = ZoneInfo("America/New_York")

ANALYSIS = {
    "version": "dir-analysis-v1.2-20260926",
    "supersedes": "dir-analysis-v1.1-20260926",
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
CATEGORIES = ("collection_pending", "not_generated", "identity_fail", "no_direction", "no_candidate", "beyond_formal_date",
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


def _session_identity(row: dict) -> str:
    idt = row.get("identity") or {}
    if idt.get("mode") != "prospective" or idt.get("status") != "certified":
        return "session 非前瞻或未认证"
    rec = _aware(row.get("recorded_at"))
    s = date.fromisoformat(row["session"])
    if rec is None or rec >= datetime(s.year, s.month, s.day, 9, 30, tzinfo=ET):
        return "recorded_at 缺失/无时区/不早于当日 09:30 ET"
    return ""


def _source_identity(row: dict) -> str:
    """信号来源身份（v1.2 R01）：两份来源快照的抓取时刻都已知、带时区、早于 recorded_at，
    available_at 恰为二者较晚者；快照/台账哈希、映射版本、台账方向算法指纹齐全且符合冻结值。"""
    f = (row.get("decision") or {}).get("flow")
    if not f:
        return "信号来源缺失（无台账行）"
    rec = _aware(row.get("recorded_at"))
    src = f.get("source_captured_at") or {}
    times = {}
    for name in ("current", "previous"):
        t = _aware(src.get(name))
        if t is None:
            return f"来源快照 {name} 的抓取时刻缺失或无时区"
        if not t < rec:
            return f"来源快照 {name} 的抓取时刻不早于 recorded_at"
        times[name] = t
    av = _aware(f.get("available_at"))
    if av is None or av != max(times.values()):
        return "available_at 缺失、无时区或不等于两份来源抓取时刻的较晚者"
    for k in ("snapshot_sha", "prev_snapshot_sha", "ledger_row_sha"):
        if not f.get(k):
            return f"{k} 缺失"
    if f.get("mapping") != ANALYSIS["mapping_version"]:
        return "映射版本不符"
    if f.get("ledger_code_sha") != ANALYSIS["call_code_sha"]:
        return "台账方向算法指纹缺失或与冻结值不符"
    return ""


def eligibility(row: dict) -> tuple[str, str]:
    """("ok"|"identity_fail"|"no_direction", 原因)。

    顺序（v1.2 R03）：session 身份 → 信号来源身份 → 方向取值。无方向（no_direction）只在来源身份合格时成立，
    是预定义的弃权日；来源不合格的中性行记 identity_fail，不能混进弃权。"""
    why = _session_identity(row)
    if why:
        return "identity_fail", f"session：{why}"
    why = _source_identity(row)
    if why:
        return "identity_fail", f"来源：{why}"
    if aligned_sides(row) is None:
        return "no_direction", "无方向（来源合格）"
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
    # v1.2 R04：先看截止日、后读结果。日级口径：退出日（到期前最后交易日）收盘窗之后才可能有结果，
    # 所以 as_of ≤ 退出日 一律 immature —— 数据文件里预填了之后的结果也不能穿透截止日。
    exit_day = mc.prev_trading_day(date.fromisoformat(max(l["expiry"] for l in legs)))
    if exit_day is None or as_of <= exit_day:
        return "immature", "" if exit_day else "日历未覆盖退出日", None
    out = row.get("outcome") or {}
    os_ = [out.get(f"{s}-{r}") for s, r in need]
    sts = [((o or {}).get("status") or {}).get(basis) for o in os_]
    if any(o is None for o in os_) or "immature" in sts:
        return "matured_missing_result", "退出日已过但无结算结果", None
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


def _collection_closed(d: date, now: datetime | None) -> bool:
    """当日盘前采集窗口是否已结束：准入要求 recorded_at < 09:30 ET，所以 09:30 ET 之后不可能再有合格行。
    now=None（不知道运行时刻）→ 视为未结束，不把「还没到点」当成漏采。"""
    return now is not None and now >= datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)


def opportunity_table(rows: list[dict], inst: str, hypothesis: str, *, basis: str, as_of: date,
                      now: datetime | None = None) -> dict:
    """日历交易日（eligible_from … as_of，正式时截到检验日）× 品种，每格恰好一类。行不预先过滤。
    as_of 当天没有行且采集窗口未结束 → collection_pending（不算漏采）。"""
    ident, days = _window(as_of)
    if days is None:
        return {"status": "calendar_unknown", "table": None, "reasons": {}, "days": None, "items": [],
                "pairable_rate": None, "pairable_denominator": list(PAIRABLE_DENOM), "coverage": None}
    by = {r["session"]: r for r in rows if r.get("instrument") == inst}
    table, reasons, items = dict.fromkeys(CATEGORIES, 0), {}, []
    for d in days:
        r = by.get(d.isoformat())
        if r is None and d == as_of and not _collection_closed(d, now):
            cat, why, vals = "collection_pending", "", None
        else:
            cat, why, vals = classify(r, hypothesis, basis=basis, as_of=as_of, formal=ident == "formal")
        table[cat] += 1
        if why and cat in ("identity_fail", "matured_missing_result", "immature"):
            reasons[why] = reasons.get(why, 0) + 1
        items.append({"session": d.isoformat(), "category": cat, "vals": vals})
    denom = sum(table[c] for c in PAIRABLE_DENOM)
    return {"status": "ok", "table": table, "reasons": reasons, "days": len(days), "items": items,
            "pairable_rate": (table["complete"] + table["unbounded"]) / denom if denom else None,
            "pairable_denominator": list(PAIRABLE_DENOM), "coverage": coverage(table)}


def _frac(num, den):
    return {"num": num, "den": den, "rate": num / den if den else None}


def coverage(t: dict) -> dict:
    """v1.2 R02：与冻结的条件配对率并列的全日历覆盖（描述，不是闸门）。每级给原始分子分母。"""
    due = sum(t.values()) - t["collection_pending"]
    generated = due - t["not_generated"]
    identity_ok = generated - t["identity_fail"]
    directed = identity_ok - t["no_direction"]
    candidate = directed - t["no_candidate"] - t["beyond_formal_date"]
    matured = sum(t[c] for c in PAIRABLE_DENOM)
    paired = t["complete"] + t["unbounded"]
    engineering_gaps = t["not_generated"] + t["identity_fail"] + t["matured_missing_result"]
    return {"collection": _frac(generated, due),                    # 应采集日里生成了机会行
            "identity_auditable": _frac(identity_ok, generated),    # 生成的行里来源身份可审计
            "direction_present": _frac(directed, identity_ok),      # 合格行里有方向（无方向=预定弃权）
            "candidate_present": _frac(candidate, directed),
            "matured_result_complete": _frac(matured - t["matured_missing_result"], matured),
            "end_to_end_paired": _frac(paired, due),                # 应采集日里最终可配对
            "engineering_gaps": engineering_gaps,
            "integrity": ("empty" if due == 0 else "complete" if engineering_gaps == 0 else "incomplete")}


def scope(cov: dict | None) -> dict:
    """结论适用范围：检验结论只对「身份合格、有方向、有候选、已成熟且可配对」的条件样本成立。
    有工程缺失（漏采/身份不合格/成熟缺结果）时，缺失机制可能与行情相关，推广到全日历另判「未决」。"""
    cond = "条件结论：仅限身份合格、有方向、有候选、已成熟且可配对的样本"
    if cov is None:
        return {"conditional": cond, "generalization": "未决（日历未知）"}
    if cov["integrity"] == "empty":
        return {"conditional": cond, "generalization": "未决（尚无应采集日）"}
    if cov["integrity"] == "complete":
        return {"conditional": cond,
                "generalization": "覆盖完整：无漏采、无身份失败、无成熟缺结果；无方向日为预定义弃权"}
    return {"conditional": cond,
            "generalization": f"未决：工程缺失 {cov['engineering_gaps']} 格，缺失敏感性分析未做，"
                              "条件结论不能代表全日历、更不等于可执行策略已验证"}


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
                      as_of: date, now: datetime | None = None) -> dict:
    ident = sh.formal_identity(as_of)
    out = {"instrument": inst, "identity": ident, "analysis_version": ANALYSIS["version"],
           "note": "门槛（20 个日期、0.5 可配对率、δ=0.03）是研究设计选择，不保证足够的统计功效；"
                   "「未证实」不等于「已排除」，只有上界 < δ 才算排除实用增量。"}
    for h in ANALYSIS["hypotheses"]:
        ot = opportunity_table(rows, inst, h, basis=basis, as_of=as_of, now=now)
        ci, n, unb, nd = _ci(ot["items"])
        v = judge(ci, nd, ot["pairable_rate"])
        if (ot["coverage"] or {}).get("integrity") != "complete" and v.startswith(("支持", "统计为正", "不支持", "排除")):
            v += "［条件样本内；全日历推广未决］"           # R02：范围随判定一起出现，不能单独转述成整体结论
        out[h] = {"opportunities": {k: ot[k] for k in ("status", "table", "reasons", "days", "pairable_rate",
                                                         "pairable_denominator", "coverage")},
                  "scope": scope(ot["coverage"]),
                  "n_pairs": n, "n_unbounded": unb, "n_dates": nd, "ci_one_sided_alpha": ANALYSIS["alpha_one_sided"],
                  "ci": ci, "verdict": v if ident == "formal" else f"探索·{v}"}
    ok_rows = _eligible_rows(rows, inst, as_of)
    out["interaction"] = interaction(ok_rows, basis)
    out["common_sample"] = common_sample(ok_rows, basis)
    out["common_bounds"] = common_bounds(ok_rows, basis)
    out["all_opportunities"] = all_opportunities(ok_rows, basis)
    out["direct_direction"] = direct_direction(ok_rows)
    out["diagnostics"] = diagnostics(ok_rows)
    return out


def _matured(row, as_of) -> bool:
    exps = [l["expiry"] for l in row.get("legs", []) if l.get("status") == "candidate"]
    if not exps:
        return False
    x = mc.prev_trading_day(date.fromisoformat(max(exps)))
    return x is not None and as_of > x


def _eligible_rows(rows, inst, as_of):
    """描述性部分用：窗口内、准入通过（有方向）、全部候选腿已过退出日（R04 截止）的行。正式时再按检验日截断。"""
    ident, days = _window(as_of)
    keep = {d.isoformat() for d in (days or [])}
    out = [r for r in rows if r.get("instrument") == inst and r["session"] in keep and eligibility(r)[0] == "ok"
           and _matured(r, as_of)]
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


def _env(pairs, k):
    """[(L, U)] 的确定性计价包络。L 可为 None（下界未知）：保守侧即为未知，不以 0 补齐。"""
    n = len(pairs)
    Ls, Us = [p[0] for p in pairs], [p[1] for p in pairs]
    known = all(x is not None for x in Ls)
    worst = (lambda xs: st.fmean(sorted(xs)[:k])) if n else None
    return {"n": n, "n_lower_unknown": sum(x is None for x in Ls),
            "mean": [st.fmean(Ls) if (n and known) else None, st.fmean(Us) if n else None],
            "win_rate": [sum(x is not None and x > 0 for x in Ls) / n if n else None,
                         sum(x > 0 for x in Us) / n if n else None],
            "worst_k_mean": [worst(Ls) if (n and known) else None, worst(Us) if n else None], "k": k if n else 0}


def common_bounds(rows, basis) -> dict:
    """v1.2（Codex 012 第 5 项）：共同【可界定】键上的绝对收益包络 + 相对效应的正确区间传播。描述性，不参与判定。

    键 = 该规则 P、C 两侧都有上界（未定价的另计 n_unpriced，不伪造数值）。
    绝对：均值、胜率、最差 k 均值各给 [按下界, 按上界]；任一下界未知 → 保守侧未知。
    相对：逐日 [L_x − U_y, U_x − L_y] 再取均值；任一端未知 → 该端未知。两个上界相减既不是上界也不是下界。
    这是确定性计价界，不是统计置信区间；也不覆盖连上界都缺失的日子。主检验遇无界即未决的规则不变。"""
    out = {"identity": "描述性·共同可界定样本的计价包络（非置信区间）"}
    for rule in (sh.CONFIG["primary_b"], "A"):
        keys, n_unpriced = [], 0
        ser = {"aligned": [], "counter": [], "always_put": [], "always_call": []}
        for r in rows:
            vp, vc = _v(r, "P", rule, basis), _v(r, "C", rule, basis)
            if vp is None or vc is None:
                n_unpriced += 1; continue
            a, o = aligned_sides(r)
            val = {"P": vp, "C": vc}
            keys.append(r["session"])
            ser["aligned"].append(val[a]); ser["counter"].append(val[o])
            ser["always_put"].append(vp); ser["always_call"].append(vc)
        k = max(1, int(len(keys) * ANALYSIS["tail_quantile"])) if keys else 0
        rel = {}
        for name, (x, y) in {"aligned_minus_counter": ("aligned", "counter"),
                             "aligned_minus_always_put": ("aligned", "always_put"),
                             "aligned_minus_always_call": ("aligned", "always_call")}.items():
            d = [((None if (X[0] is None) else X[0] - Y[1]), (None if Y[0] is None else X[1] - Y[0]))
                 for X, Y in zip(ser[x], ser[y])]
            lo_known = all(p[0] is not None for p in d)
            hi_known = all(p[1] is not None for p in d)
            rel[name] = {"n": len(d), "mean": [st.fmean(p[0] for p in d) if (d and lo_known) else None,
                                               st.fmean(p[1] for p in d) if (d and hi_known) else None]}
        out[rule] = {"keys": keys, "n_keys": len(keys), "n_unpriced": n_unpriced,
                     "absolute": {s_: _env(v, k) for s_, v in ser.items()}, "relative": rel}
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
            # v1.2：window_leg 的直接被调函数（Codex 012：守卫只证明列出的对象未变，不是完整传递闭包）
            "shadow.entry_quality": h(sh.entry_quality), "shadow.exit_quality": h(sh.exit_quality),
            "shadow._credit": h(sh._credit), "shadow.exit_cost_raw": h(sh.exit_cost_raw),
            "shadow.exit_mode": h(sh.exit_mode), "shadow.in_window": h(sh.in_window),
            "shadow.window_bounds": h(sh.window_bounds), "shadow.qkey": h(sh.qkey),
            "shadow.config_hash": sh.config_hash()}
