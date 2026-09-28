"""偏度方向读数（预登记 skew-reading-v1；用户 2026-09-28 要求「给出类似[外部作者]的方向性判断」）。纯函数，无 I/O。

外部作者的方法核心：看期权偏度（Put−Call Skew）与固定 Delta 上 call / put 各自的 IV 变化，判断期权市场在
「防守化」（put 相对 call 变贵：下行保险需求上升）还是「进攻化」。本模块用我们自己的 ETF 期权快照
（GLD / SLV，CBOE 延迟报价，约为前一交易日收盘）复现同类读数，并【事前冻结】分档规则，写进方向判断台账，
事后按价格计分。注意：他看 COMEX 期货期权，我们看 ETF 期权，口径不同，数值不可直接互比。

⚠️ 这是未经验证的预测主张（claims.py：`skew_reading.v1`，T3）。台账攒样本、按预登记计分之前，
读数只作「未验证的方向读数」展示，不进入任何决策路径。规则一经首个前瞻样本记录即冻结；要改规则 = 新版本。

规则（RULE，全部未校准，是预登记的设计选择，不是市场规律）：
- 到期：两份快照共有、C/P 两侧有效 IV 报价各 ≥ VOL_MIN_QUOTES、DTE 在 [25, 75] 内，取最接近 45 天者。
  （他看 11 月合约，约 45–60 天；固定到期才能逐日可比。）
- 偏度 skewX = IV_put(XΔ) − IV_call(XΔ)，单位 pp；正 = put 更贵。
- 固定 Delta 阶梯：40/30/25/20/15/10Δ 上 call、put 各自 IV 日变化（structure_read.build_ladder）。
- 防守化：Δskew25 ≥ +0.25pp，且 6 档里至少 4 档「put 变化 − call 变化」≥ +0.10pp。
- 进攻化：Δskew25 ≤ −0.25pp，且至少 4 档「put 变化 − call 变化」≤ −0.10pp。
- 其余：中性；缺数据：数据不足（不折成中性）。
"""
from __future__ import annotations

from datetime import date

from undertow.analyze.flow import VOL_MIN_QUOTES
from undertow.analyze.structure_read import DELTA_LADDER, _iv_at_delta, build_ladder

RULE = {
    "version": "skew-reading-v1-20260928",
    "instruments": ("gold", "silver"),
    "target_dte": 45, "dte_range": (25, 75),
    "d_skew25_min_pp": 0.25, "rung_min_pp": 0.10, "rungs_needed": 4,
    "ladder": DELTA_LADDER,
    "labels": ("防守化", "中性", "进攻化", "数据不足"),
}


def _ok_expiries(contracts):
    by: dict = {}
    for c in contracts:
        if c.iv <= 0 or not (0.02 <= abs(c.delta) <= 0.85):
            continue
        by.setdefault(c.expiry, {"C": 0, "P": 0})[c.kind] += 1
    return {e for e, n in by.items() if n["C"] >= VOL_MIN_QUOTES and n["P"] >= VOL_MIN_QUOTES}


def pick_expiry(prev_contracts, curr_contracts, asof: date) -> date | None:
    lo, hi = RULE["dte_range"]
    common = [e for e in _ok_expiries(prev_contracts) & _ok_expiries(curr_contracts) if lo <= (e - asof).days <= hi]
    if not common:
        return None
    return min(common, key=lambda e: (abs((e - asof).days - RULE["target_dte"]), e))


def _skew(cs, d):
    p, c = _iv_at_delta(cs, "P", d), _iv_at_delta(cs, "C", d)
    return None if (p is None or c is None) else round((p - c) * 100, 3)


def read(prev_contracts, curr_contracts, *, asof: date) -> dict:
    """两份相邻快照（同一品种）→ 特征与读数。asof = 当前快照所含报价的交易日（用于算 DTE）。"""
    exp = pick_expiry(prev_contracts, curr_contracts, asof)
    if exp is None:
        return {"rule_version": RULE["version"], "reading": "数据不足", "reason": "无满足条件的共同到期"}
    pc = [c for c in prev_contracts if c.expiry == exp]
    cc = [c for c in curr_contracts if c.expiry == exp]
    feats = {"expiry": exp.isoformat(), "dte": (exp - asof).days}
    for name, cs in (("prev", pc), ("curr", cc)):
        atm = _iv_at_delta(cs, "C", 0.5)
        feats[f"atm_iv_{name}_pp"] = None if atm is None else round(atm * 100, 3)
        feats[f"skew25_{name}_pp"] = _skew(cs, 0.25)
        feats[f"skew10_{name}_pp"] = _skew(cs, 0.10)
    ladder = build_ladder(prev_contracts, curr_contracts, expiry=exp)
    feats["ladder"] = [{"delta": r.delta, "d_call_pp": r.d_call_pp, "d_put_pp": r.d_put_pp, "d_skew_pp": r.d_skew_pp}
                       for r in ladder]
    s0, s1 = feats["skew25_prev_pp"], feats["skew25_curr_pp"]
    if s0 is None or s1 is None or any(r.d_skew_pp is None for r in ladder):
        return {"rule_version": RULE["version"], "reading": "数据不足", "reason": "偏度或阶梯有缺失", "features": feats}
    d25 = round(s1 - s0, 3)
    feats["d_skew25_pp"] = d25
    up = sum(r.d_skew_pp >= RULE["rung_min_pp"] for r in ladder)
    dn = sum(r.d_skew_pp <= -RULE["rung_min_pp"] for r in ladder)
    feats["rungs_put_richer"], feats["rungs_call_richer"] = up, dn
    if d25 >= RULE["d_skew25_min_pp"] and up >= RULE["rungs_needed"]:
        reading = "防守化"
    elif d25 <= -RULE["d_skew25_min_pp"] and dn >= RULE["rungs_needed"]:
        reading = "进攻化"
    else:
        reading = "中性"
    return {"rule_version": RULE["version"], "reading": reading, "features": feats}


def forward_returns(bars: list[tuple[date, float, float]], session: date, horizons=(1, 5, 10), *,
                    closed_through: date | None = None) -> dict:
    """bars: [(交易日, 开盘, 收盘)]。基准 = session 当日开盘（信号开盘前可得、最早可执行价）；
    h 日结果 = 按【交易日历】从 session 起第 h 个交易日（含 session 当日）的收盘。

    Codex 017 D17-03：旧版按数组下标取第 h 根 bar —— 缺一天行情就把后一天当成终点（缺 9/29 时 h=2 取到 9/30）。
    现在先用日历定终点，再找该日价格：
      · 终点晚于 closed_through（尚未收市、或调用方未确认已收市）→ status=immature，结果 None；
      · 终点已收市但该日无价格 → status=missing_price，结果 None（不顺延）；
      · 日历不覆盖 → status=calendar_unknown。
    closed_through=None 时按 bars 里最后一根的日期（旧行为；调用方应显式传入已收市的最后交易日）。"""
    from undertow.core import market_calendar as mc
    by = {b[0]: b for b in bars}
    last = closed_through if closed_through is not None else (max(by) if by else None)
    out: dict = {"base_date": session.isoformat(), "closed_through": last.isoformat() if last else None}
    b0 = by.get(session)
    if last is None or session > last:
        out.update({"base_open": None, "base_status": "immature"})
    elif b0 is None or not b0[1]:
        out.update({"base_open": None, "base_status": "missing_price"})
    else:
        out.update({"base_open": b0[1], "base_status": "ok"})
    far = max(horizons)
    days = [session]
    while len(days) < far:
        nx = mc.next_trading_day(days[-1])
        if nx is None:
            break
        days.append(nx)
    for h in horizons:
        end = days[h - 1] if h <= len(days) else None
        out[f"end_{h}d"] = end.isoformat() if end else None
        if end is None:
            st = "calendar_unknown"
        elif last is None or end > last:
            st = "immature"
        elif end not in by or not by[end][2]:
            st = "missing_price"
        elif out["base_open"] is None:
            st = out["base_status"]
        else:
            st = "ok"
        out[f"status_{h}d"] = st
        out[f"ret_{h}d"] = round(by[end][2] / out["base_open"] - 1, 6) if st == "ok" else None
    return out
