"""方向次要分析（预登记 dir-analysis-v1；Codex 009/010 设计，用户 2026-09-26 论点）。纯函数，无 I/O。

用户原话（节选）：「期权墙可以提高方向性的准确率，而方向性可以提高卖方价差的胜率……卖方价差又可以反过来
提高方向性交易的胜率，因为只要不要出现大幅度反方向即可盈利。我觉得这三者都是要结合在一起的。」
预登记全文：docs/prereg/2026-09-26_direction_v1.md（本文件的哈希冻结在其中）。

不改 v5 任何东西：选腿、入场、退出、主终点、样本准入全部沿用 shadow-v5-20260926。
这里只在 v5 已每天记录两侧的数据上，加两项预登记的配对比较与若干描述：

  记 A=墙选腿、B1=距决策价 ≥1.0×ATR14 的首个挂牌虚值档（同美元宽度），a=按方向信号的顺向侧、o=逆向侧；
  Y=v5 主终点（到期前最后交易日收盘窗退出政策）的 损益/事前最大风险，区间值按 v5 界限规则。
  H-dir       Y(B1,a) − Y(B1,o)       同品种同日配对：固定距离选腿下，方向选边有没有价值
  H-wall|dir  Y(A,a)  − Y(B1,a)       同品种同日配对：顺向交易里，墙选腿有没有额外价值
  描述性       交互 [Y(A,a)−Y(B1,a)] − [Y(A,o)−Y(B1,o)]；固定卖 put / 固定卖 call；方向频率；
              直接做方向的命中率（入场窗→退出窗的标的变动）；尾部（最差 10%）；完整性分母。

方向信号：decision.flow.call_direction（持仓流，dir-map-v1：偏多→P、偏空→C，其余无方向），
只用这一个。其余标签（综合研判、拉伸度、趋势、墙不对称、gamma 代理）只作探索，不参与判定。
"""
from __future__ import annotations

import statistics as st
from datetime import date

from undertow.analyze import shadow as sh

ANALYSIS = {
    "version": "dir-analysis-v1-20260926",
    "base_config_version": "shadow-v5-20260926",
    "signal": "decision.flow.call_direction",
    "mapping": {"偏多": "P", "偏空": "C"},           # 其余（中性/空/None）= 无方向，只进分母
    "eligible_from": "2026-09-28",
    "formal_date": "2026-12-31",
    "pool": "etf",                                    # 七个主池品种分别检验，不合并
    "hypotheses": ["H-dir", "H-wall|dir"],
    "family_size": 14,                                # 2 假设 × 7 品种（独立的次要家族，不是全项目 FWER）
    "alpha_one_sided": 0.05 / 14,
    "economic_delta": 0.03,                           # 未校准：≈ 多付一次往返费用（$3.20）相对典型最大风险的量级
    "min_pair_dates": 20,                             # 与 v5 块 bootstrap 下限一致（5 日块 × 4）
    "min_pairable_rate": 0.5,                         # 可配对机会 / 有方向的机会；低于它 = 证据不足（工程/数据）
    "estimate": "bounds",                             # v5 区间传播：[A下界−B上界, A上界−B下界]
    "tail_quantile": 0.10,
}


def aligned_sides(row: dict):
    """(顺向侧, 逆向侧) 或 None（无方向）。"""
    d = ((row.get("decision") or {}).get("flow") or {}).get("call_direction")
    a = ANALYSIS["mapping"].get(d)
    return None if a is None else (a, "C" if a == "P" else "P")


def _v(row, side, rule, basis):
    return sh._vals((row.get("outcome") or {}).get(f"{side}-{rule}"), basis, "bounds")


def _diff(x, y):
    """区间差 [x_lo−y_hi, x_hi−y_lo]；任一下界未知 → None（无界）。"""
    if x[0] is None or y[0] is None:
        return None
    return (x[0] - y[1], x[1] - y[0])


def _rows_for(rows, inst, *, as_of: date | None):
    ident = sh.formal_identity(as_of)
    out = [r for r in rows if r.get("instrument") == inst and r.get("outcome")
           and r["session"] >= ANALYSIS["eligible_from"]]
    if ident == "formal":
        f = ANALYSIS["formal_date"]
        out = [r for r in out if r["session"] <= f and all(l["expiry"] <= f for l in r["legs"]
                                                           if l.get("status") == "candidate")]
    return ident, sorted(out, key=lambda r: r["session"])


def _paired(rows, fn):
    lo_by, hi_by, unb, n = {}, {}, 0, 0
    for r in rows:
        d = fn(r)
        if d is None:
            continue
        if d == "unbounded":
            unb += 1; continue
        n += 1
        lo_by.setdefault(r["session"], []).append(d[0]); hi_by.setdefault(r["session"], []).append(d[1])
    return lo_by, hi_by, unb, n


def _judge(ci: dict, n_dates: int, pairable_rate) -> str:
    if pairable_rate is not None and pairable_rate < ANALYSIS["min_pairable_rate"]:
        return "证据不足（可配对率低于冻结门槛）"
    if ci["status"] == "residual_unknown":
        return "未决（残腿处置未知）"
    if n_dates < ANALYSIS["min_pair_dates"] or ci["status"] in ("empty", "insufficient"):
        return "证据不足（配对日期不足）"
    if ci["status"] != "ok":
        return {"degenerate": "退化（不判）", "residual_unknown": "未决（残腿处置未知）"}.get(ci["status"], ci["status"])
    if ci["lo"] > ANALYSIS["economic_delta"]:
        return "支持（校正后下界超过经济门槛）"
    if ci["lo"] > 0:
        return "统计为正、未达经济门槛"
    if ci["hi"] <= 0:
        return "不支持"
    return "未决"


def instrument_report(rows: list[dict], inst: str, *, basis: str = sh.CONFIG["primary_basis"],
                      as_of: date | None = None) -> dict:
    ident, rs = _rows_for(rows, inst, as_of=as_of)
    a_ = ANALYSIS["alpha_one_sided"]
    with_dir = [r for r in rs if aligned_sides(r)]
    cover = {"rows": len(rs), "with_direction": len(with_dir), "no_direction": len(rs) - len(with_dir),
             "direction_bull": sum(aligned_sides(r)[0] == "P" for r in with_dir),
             "direction_bear": sum(aligned_sides(r)[0] == "C" for r in with_dir)}

    def hdir(r):
        s = aligned_sides(r)
        if not s:
            return None
        x, y = _v(r, s[0], sh.CONFIG["primary_b"], basis), _v(r, s[1], sh.CONFIG["primary_b"], basis)
        if x is None or y is None:
            return None
        return _diff(x, y) or "unbounded"

    def hwall(r):
        s = aligned_sides(r)
        if not s:
            return None
        x, y = _v(r, s[0], "A", basis), _v(r, s[0], sh.CONFIG["primary_b"], basis)
        if x is None or y is None:
            return None
        return _diff(x, y) or "unbounded"

    def inter(r):
        s = aligned_sides(r)
        if not s:
            return None
        v = [_v(r, s[0], "A", basis), _v(r, s[0], sh.CONFIG["primary_b"], basis),
             _v(r, s[1], "A", basis), _v(r, s[1], sh.CONFIG["primary_b"], basis)]
        if any(x is None for x in v):
            return None
        d1, d2 = _diff(v[0], v[1]), _diff(v[2], v[3])
        return _diff(d1, d2) if (d1 and d2) else "unbounded"

    out = {"instrument": inst, "identity": ident, "analysis_version": ANALYSIS["version"], "coverage": cover}
    for name, fn in (("H-dir", hdir), ("H-wall|dir", hwall)):
        lo_by, hi_by, unb, n = _paired(rs, fn)
        ci = sh.interval_ci(lo_by, hi_by, unb, q_lo=a_, q_hi=1 - a_)
        rate = (n + unb) / cover["with_direction"] if cover["with_direction"] else None
        v = _judge(ci, len(lo_by), rate)
        out[name] = {"n_pairs": n, "n_unbounded": unb, "n_dates": len(lo_by), "pairable_rate": rate,
                     "ci_one_sided_alpha": a_, "ci": ci, "verdict": v if ident == "formal" else f"探索·{v}"}
    lo_by, hi_by, unb, n = _paired(rs, inter)
    out["interaction"] = {"n_pairs": n, "ci": sh.interval_ci(lo_by, hi_by, unb), "identity": "描述性"}

    # 固定侧基准与顺/逆两侧（B1、A）的点值均值；区间值只计数
    def side_stats(pick):
        vals, interval = [], 0
        for r in rs:
            sd = pick(r)
            if sd is None:
                continue
            x = _v(r, sd[0], sd[1], basis)
            if x is None:
                continue
            if x[0] is None or x[0] != x[1]:
                interval += 1; continue
            vals.append(x[0])
        tail = sorted(vals)[:max(1, int(len(vals) * ANALYSIS["tail_quantile"]))] if vals else []
        return {"n_point": len(vals), "n_interval_or_unbounded": interval,
                "mean": st.fmean(vals) if vals else None, "worst_decile_mean": st.fmean(tail) if tail else None,
                "win_rate": (sum(v > 0 for v in vals) / len(vals)) if vals else None}
    b1 = sh.CONFIG["primary_b"]
    out["descriptive"] = {
        "B1_aligned": side_stats(lambda r: (aligned_sides(r)[0], b1) if aligned_sides(r) else None),
        "B1_counter": side_stats(lambda r: (aligned_sides(r)[1], b1) if aligned_sides(r) else None),
        "A_aligned": side_stats(lambda r: (aligned_sides(r)[0], "A") if aligned_sides(r) else None),
        "A_counter": side_stats(lambda r: (aligned_sides(r)[1], "A") if aligned_sides(r) else None),
        "B1_always_put": side_stats(lambda r: ("P", b1)),
        "B1_always_call": side_stats(lambda r: ("C", b1)),
    }
    out["direct_direction"] = direct_direction(rs)
    return out


def direct_direction(rows: list[dict]) -> dict:
    """直接做方向（描述性）：入场窗 → 到期前一交易日收盘窗的标的变动符号是否与方向一致。
    标的价取两窗第一次尝试记录的 underlying（长桥实时）；缺失不计。与价差收益不可直接比（单位不同）。"""
    from undertow.core import market_calendar as mc
    hits = n = 0
    for r in rows:
        s = aligned_sides(r)
        if not s:
            continue
        exp = next((l["expiry"] for l in r["legs"] if l.get("status") == "candidate"), None)
        x = mc.prev_trading_day(date.fromisoformat(exp)) if exp else None
        p0 = _underlying(r, f"{r['session']}|open"); p1 = _underlying(r, f"{x.isoformat()}|close") if x else None
        if p0 is None or p1 is None or p1 == p0:
            continue
        n += 1
        hits += (p1 > p0) == (s[0] == "P")
    return {"n": n, "hit_rate": hits / n if n else None,
            "note": "符号命中率；与价差的 损益/风险 单位不同，不可直接比较"}


def _underlying(row, wkey):
    for a in ((row.get("windows") or {}).get(wkey) or {}).get("attempts", []):
        u = a.get("underlying") or {}
        v = u.get("freshest") if isinstance(u, dict) else None
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None
