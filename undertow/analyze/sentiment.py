"""情绪面板 v0 的纯计算（Codex 066 合同 sentiment-panel-v0-20261028）。

只吃已解析好的数值/字典，不做 I/O、不联网。取数与落盘在 `undertow/sentiment_cli.py`。

定位：**描述层**（claims 角色=背景）。三组分量分列——波动价格、期权不对称/张数结构、期货分类持仓——
不合成「恐慌分数」，不给方向，不加研判置信度，不做开仓过滤。分位只是该变量在自身历史里的位置，
不是反弹概率。

四条容易写错的地方（066 审查逐条点名）：
1. **固定 Delta 必须被报价严格包围。** 旧 `structure_read._interp` 越界取端点；这里复用
   `flow._iv_at_delta_strict`（越界返回 None），并另外记录两包围点的 Delta 间距。
2. **恒定 30 日 = 两个包围到期按总方差插值**，不取最近到期、不外推。最近到期只进审计字段，
   不进主值也不进主值的分位历史。
3. **缺数据不折零。** P/C 合计里任何一张合约缺字段，整项返回 None 并报缺失数；分母 0 单独报状态。
4. **分位 prior-only。** 只用当前点之前、决策时已可得的观察；并列取中位秩；没有历史给 None。
"""
from __future__ import annotations

import math
from datetime import date

from undertow.analyze.flow import _iv_at_delta_strict

RULE_VERSION = "sentiment-panel-v0-20261028"

# 质量状态枚举（合同 §1）。缺失、未到发布时间、源停更必须可区分。
CURRENT = "current"                  # 预期的最新一期
EXPECTED_CARRY = "expected_carry"    # 正常沿用（如 COT 周内）
PENDING = "pending"                  # 预期的一期还没发布（不是失败）
STALE = "stale"                      # 落后超过正常发布节奏
MISSING = "missing"
INVALID = "invalid"
RECONSTRUCTED = "reconstructed"      # 用今天下载的版本重建历史（含回算段）
UNKNOWN_VINTAGE = "unknown_vintage"  # 不知道当时公开的是不是这个值
QUALITY_STATES = (CURRENT, EXPECTED_CARRY, PENDING, STALE, MISSING, INVALID,
                  RECONSTRUCTED, UNKNOWN_VINTAGE)

TARGET_DELTA = 0.25
ATM_DELTA = 0.50
TARGET_DAYS = 30
MIN_DTE = 14           # 工程设计：避开极近月；不是校准结果（合同 §3.4）
PC_MAX_DTE = 45        # 主描述窗 0–45D；全链只作对照（合同 §4）
VOL_LOOKBACK = 252     # 日频：前 252 个有效观察
COT_LOOKBACK = 156     # COT：前 156 个独立报告期
PERCENTILE_METHOD = "prior_only_midrank"

# VXSLV 方法版本（Cboe 官方方法说明：2025-06-16 重启，停算期间历史按重启方法回算；
# FRED 旧序列止于 2022-02-11）。回算段不是当时公开的值。
VXSLV_FRED_LAST = date(2022, 2, 11)
VXSLV_RESTART = date(2025, 6, 16)
VOL_METHOD_NOTES = {
    "VXSLV": {
        "methodology_vintage": "cboe-vxslv-restart-2025-06-16",
        "reconstructed_from": VXSLV_FRED_LAST, "reconstructed_until": VXSLV_RESTART,
        "source_doc": "https://cdn.cboe.com/api/global/us_indices/governance/"
                      "Volatility_Index_Methodology_Selected_Broad_Based_Index_Equity_and_ETF_Volatility_Indices.pdf",
    },
}


def vol_point_vintage(sym: str, d: date) -> str:
    """某个波动指数在日期 d 的值属于哪种版本：current / reconstructed / unknown_vintage。

    今天下载的历史，除 VXSLV 回算段明确为 reconstructed 外，其余都无法证明与当时公开值一致
    → 历史点标 unknown_vintage；只有前瞻抓到的最新点由调用方标 current。
    """
    note = VOL_METHOD_NOTES.get(sym)
    if note and note["reconstructed_from"] < d < note["reconstructed_until"]:
        return RECONSTRUCTED
    return UNKNOWN_VINTAGE


# ─────────────────────────── 分位 ───────────────────────────

def prior_midrank(prior: list[float], current: float | None) -> float | None:
    """(#历史<当前 + 0.5·#历史=当前) / n_prior。prior 必须已排除当前点；空 → None。"""
    if current is None or not _fin(current):
        return None
    vals = [v for v in prior if _fin(v)]
    if not vals:
        return None
    lt = sum(1 for v in vals if v < current)
    eq = sum(1 for v in vals if v == current)
    return (lt + 0.5 * eq) / len(vals)


def rank_block(prior: list[tuple[date, float]], current: float | None, lookback: int) -> dict:
    """在「当前点之前」的有序观察里取最近 lookback 个算中位秩，并给出窗口元数据。"""
    window = [(d, v) for d, v in prior if _fin(v)][-lookback:] if lookback > 0 else []
    pct = prior_midrank([v for _, v in window], current)
    return {
        "percentile": pct, "percentile_method": PERCENTILE_METHOD,
        "lookback_target": lookback, "n_prior": len(window),
        "window_start": window[0][0].isoformat() if window else None,
        "window_end": window[-1][0].isoformat() if window else None,
        "partial_window": len(window) < lookback,
    }


# ─────────────────────────── 期权：报价合法性与严格 Delta ───────────────────────────

def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def quote_ok(c: dict) -> tuple[bool, str]:
    """主插值可用的报价：IV>0、Delta 有限且符号与 C/P 一致、ask≥bid>0。返回 (是否可用, 原因)。"""
    kind, iv, dl, bid, ask = c.get("kind"), c.get("iv"), c.get("delta"), c.get("bid"), c.get("ask")
    if kind not in ("C", "P"):
        return False, "kind"
    if not (_fin(iv) and iv > 0):
        return False, "iv"
    if not _fin(dl):
        return False, "delta"
    if kind == "C" and not (0 < dl < 1):
        return False, "delta_sign"
    if kind == "P" and not (-1 < dl < 0):
        return False, "delta_sign"
    if not (_fin(bid) and _fin(ask) and bid > 0 and ask >= bid):
        return False, "bid_ask"
    return True, ""


def side_at_delta(contracts: list[dict], kind: str, target_abs: float) -> dict | None:
    """单个到期、单侧，在 |Delta|=target_abs 处严格包围插值 IV。

    contracts 已是同一到期的报价。只用 quote_ok 的合约。恰好命中目标时直接取值；
    否则必须有一个点在目标两侧，越界返回 None——**不取端点、不外推**。
    返回 {iv, lo_delta, hi_delta, delta_gap, exact, lo_spread, hi_spread}。
    """
    pts, spreads = [], {}
    for c in contracts:
        if c.get("kind") != kind:
            continue
        ok, _ = quote_ok(c)
        if not ok:
            continue
        x = abs(c["delta"])
        pts.append((x, c["iv"]))
        mid = (c["bid"] + c["ask"]) / 2
        spreads.setdefault(x, (c["ask"] - c["bid"]) / mid if mid > 0 else None)
    exact = [iv for x, iv in pts if x == target_abs]
    if exact:
        return {"iv": sum(exact) / len(exact), "lo_delta": target_abs, "hi_delta": target_abs,
                "delta_gap": 0.0, "exact": True,
                "lo_spread": spreads.get(target_abs), "hi_spread": spreads.get(target_abs)}
    below = [p for p in pts if p[0] < target_abs]
    above = [p for p in pts if p[0] > target_abs]
    if not below or not above:
        return None
    lo = max(below, key=lambda p: p[0])
    hi = min(above, key=lambda p: p[0])
    iv = _iv_at_delta_strict([lo, hi], target_abs)
    if iv is None:
        return None
    return {"iv": iv, "lo_delta": lo[0], "hi_delta": hi[0], "delta_gap": hi[0] - lo[0],
            "exact": False, "lo_spread": spreads.get(lo[0]), "hi_spread": spreads.get(hi[0])}


def total_variance_interp(t1: float, s1: float, t2: float, s2: float, t: float) -> float | None:
    """两到期之间按总方差插到 t（单位一致即可）。必须 t1<t<t2，或 t 恰等于某一端。"""
    if not all(_fin(v) for v in (t1, s1, t2, s2, t)) or s1 <= 0 or s2 <= 0 or t <= 0:
        return None
    if t == t1:
        return s1
    if t == t2:
        return s2
    if not (t1 < t < t2):
        return None
    lam = (t2 - t) / (t2 - t1)
    w = lam * s1 * s1 * t1 + (1 - lam) * s2 * s2 * t2
    return math.sqrt(w / t) if w > 0 else None


def _const_maturity(by_exp: dict[date, dict], valuation: date, side_key: str) -> dict:
    """在「该侧已有严格值」的到期里取 30 日两侧最近的一对，按总方差插值；无包围 → None。"""
    pts = sorted((((e - valuation).days, e, v[side_key]) for e, v in by_exp.items()
                  if v.get(side_key) is not None and (e - valuation).days >= MIN_DTE),
                 key=lambda t: t[0])
    exact = [p for p in pts if p[0] == TARGET_DAYS]
    if exact:
        _, e, s = exact[0]
        return {"iv": s["iv"], "expiries": [e.isoformat()], "dte": [TARGET_DAYS]}
    lo = [p for p in pts if p[0] < TARGET_DAYS]
    hi = [p for p in pts if p[0] > TARGET_DAYS]
    if not lo or not hi:
        return {"iv": None, "expiries": [], "dte": [],
                "missing_reason": "no_expiry_bracket_30d"}
    (t1, e1, s1), (t2, e2, s2) = lo[-1], hi[0]
    iv = total_variance_interp(t1, s1["iv"], t2, s2["iv"], TARGET_DAYS)
    return {"iv": iv, "expiries": [e1.isoformat(), e2.isoformat()], "dte": [t1, t2],
            "delta_gaps": [s1["delta_gap"], s2["delta_gap"]],
            "missing_reason": None if iv is not None else "total_variance_invalid"}


def skew_features(contracts: list[dict], valuation: date | None) -> dict:
    """期权不对称：恒30 主值 + 最近到期审计列。contracts 每项含 expiry(date)/kind/iv/delta/bid/ask。

    valuation = 报价估值日（时间基础由调用方声明）；None → 全部主值 None。
    IV 单位按源（小数）；skew 输出 pp = 100·(IV_put25 − IV_call25)。
    """
    out = {"skew25_30d_pp": None, "iv_put25_30d": None, "iv_call25_30d": None,
           "atm_put50_30d": None, "atm_call50_30d": None, "atm_mid50_30d": None,
           "nearest_expiry": None, "nearest_dte": None, "nearest_skew25_pp": None,
           "detail": {}, "invalid_quotes": 0, "missing_reason": None}
    if valuation is None:
        out["missing_reason"] = "valuation_date_unknown"
        return out
    groups: dict[date, list[dict]] = {}
    bad = 0
    for c in contracts:
        if not quote_ok(c)[0]:
            bad += 1
        groups.setdefault(c["expiry"], []).append(c)
    out["invalid_quotes"] = bad
    by_exp: dict[date, dict] = {}
    for e, cs in groups.items():
        if (e - valuation).days < MIN_DTE:
            continue
        by_exp[e] = {"p25": side_at_delta(cs, "P", TARGET_DELTA), "c25": side_at_delta(cs, "C", TARGET_DELTA),
                     "p50": side_at_delta(cs, "P", ATM_DELTA), "c50": side_at_delta(cs, "C", ATM_DELTA)}
    det = {k: _const_maturity(by_exp, valuation, k) for k in ("p25", "c25", "p50", "c50")}
    out["detail"] = det
    p25, c25 = det["p25"]["iv"], det["c25"]["iv"]
    out["iv_put25_30d"], out["iv_call25_30d"] = p25, c25
    if p25 is not None and c25 is not None:
        out["skew25_30d_pp"] = 100.0 * (p25 - c25)
    else:
        out["missing_reason"] = det["p25"].get("missing_reason") or det["c25"].get("missing_reason") \
            or "delta_bracket_missing"
    p50, c50 = det["p50"]["iv"], det["c50"]["iv"]
    out["atm_put50_30d"], out["atm_call50_30d"] = p50, c50
    if p50 is not None and c50 is not None:
        out["atm_mid50_30d"] = (p50 + c50) / 2
    # 审计：最近到期（≥MIN_DTE、最接近 30 天）的严格 25Δ，不进主值、不进主值分位
    if by_exp:
        ne = min(by_exp, key=lambda e: (abs((e - valuation).days - TARGET_DAYS), e))
        out["nearest_expiry"], out["nearest_dte"] = ne.isoformat(), (ne - valuation).days
        a, b = by_exp[ne]["p25"], by_exp[ne]["c25"]
        if a and b:
            out["nearest_skew25_pp"] = 100.0 * (a["iv"] - b["iv"])
    return out


# ─────────────────────────── 期权：P/C 张数 ───────────────────────────

def _ratio_block(rows: list[dict], field: str) -> dict:
    put = call = 0.0
    n = missing = 0
    for c in rows:
        v = c.get(field)
        n += 1
        if not (_fin(v) and v >= 0):
            missing += 1
            continue
        if c["kind"] == "P":
            put += v
        else:
            call += v
    blk = {"numerator_put": put, "denominator_call": call, "n_contracts": n, "n_missing": missing,
           "value": None, "state": "ok"}
    if n == 0:
        blk.update(numerator_put=None, denominator_call=None, state="no_contracts")
    elif missing:
        blk.update(numerator_put=None, denominator_call=None, state="incomplete_missing_fields")
    elif call == 0:
        blk["state"] = "zero_denominator"
    else:
        blk["value"] = put / call
    return blk


def pc_features(contracts: list[dict], valuation: date | None, *, max_dte: int = PC_MAX_DTE) -> dict:
    """源链覆盖范围内的 P/C 成交比与持仓比：0–max_dte 主窗 + 全链对照。

    这是张数统计，没有买卖/开平分类，不能读作资金净流向。覆盖字段写明链里最早到期，
    用来判断是否含估值日当天到期的合约——缺覆盖证明时它就只是「源链 P/C」，不是全市场统计。
    """
    if valuation is None:
        return {"window": None, "full_chain": None, "missing_reason": "valuation_date_unknown",
                "coverage": None}
    cs = [c for c in contracts if c.get("kind") in ("C", "P") and isinstance(c.get("expiry"), date)]
    win = [c for c in cs if 0 <= (c["expiry"] - valuation).days <= max_dte]
    exps = sorted({c["expiry"] for c in cs})
    return {
        "name": "source_chain_pc",
        "window": {"max_dte": max_dte, "volume": _ratio_block(win, "volume"),
                   "open_interest": _ratio_block(win, "open_interest")},
        "full_chain": {"volume": _ratio_block(cs, "volume"),
                       "open_interest": _ratio_block(cs, "open_interest")},
        "coverage": {"min_expiry": exps[0].isoformat() if exps else None,
                     "max_expiry": exps[-1].isoformat() if exps else None,
                     "n_expiries": len(exps),
                     "includes_valuation_day_expiry": valuation in exps,
                     "has_expired_before_valuation": bool(exps) and exps[0] < valuation},
        "missing_reason": None,
    }


# ─────────────────────────── COT ───────────────────────────

def cot_features(reports: list[dict], lookback: int = COT_LOOKBACK) -> dict | None:
    """reports：按 report_date 升序的同一系列，每项含 report_date/long/short/open_interest（缺失=None）。

    当前 = 最后一项；分位只用之前的报告期（不同报告期各算一个观察，沿用不虚增 n）。
    主描述量 net/OI；净张数分位作审计；毛多毛空与周变化原样保留。
    """
    if not reports:
        return None
    cur = reports[-1]
    net = _net(cur)
    oi = cur.get("open_interest")
    ratio = net / oi if (net is not None and _fin(oi) and oi > 0) else None
    prior = reports[:-1]
    prior_ratio = [(r["report_date"], _net(r) / r["open_interest"]) for r in prior
                   if _net(r) is not None and _fin(r.get("open_interest")) and r["open_interest"] > 0]
    prior_net = [(r["report_date"], _net(r)) for r in prior if _net(r) is not None]
    prev = reports[-2] if len(reports) >= 2 else None
    return {
        "report_date": cur["report_date"].isoformat(),
        "long": cur.get("long"), "short": cur.get("short"), "spread": cur.get("spread"),
        "open_interest": oi, "net": net, "net_over_oi": ratio,
        "change_long": cur.get("change_long"), "change_short": cur.get("change_short"),
        "prev_report_date": prev["report_date"].isoformat() if prev else None,
        "net_change": (net - _net(prev)) if (prev and net is not None and _net(prev) is not None) else None,
        "rank_net_over_oi": rank_block(prior_ratio, ratio, lookback),
        "rank_net_audit": rank_block(prior_net, net, lookback),
    }


def _net(r: dict | None):
    if not r:
        return None
    lo, sh = r.get("long"), r.get("short")
    return lo - sh if (_fin(lo) and _fin(sh)) else None


# ─────────────────────────── 波动指数 ───────────────────────────

def vol_features(series: list[tuple[date, float]], decision_date: date, sym: str,
                 lookback: int = VOL_LOOKBACK) -> dict:
    """取严格早于 decision_date 的最新一个值（决策日当天收盘的值在决策时还不存在），
    并对它之前的 lookback 个观察算中位秩。窗口内含回算段时如实标出。"""
    pts = [(d, v) for d, v in series if d < decision_date and _fin(v)]
    if not pts:
        return {"value": None, "event_date": None, "missing_reason": "no_value_before_decision",
                **rank_block([], None, lookback), "window_contains_reconstructed": False}
    d, v = pts[-1]
    rb = rank_block(pts[:-1], v, lookback)
    note = VOL_METHOD_NOTES.get(sym)
    contaminated = False
    if note and rb["window_start"]:
        ws, we = date.fromisoformat(rb["window_start"]), date.fromisoformat(rb["window_end"])
        contaminated = ws < note["reconstructed_until"] and we > note["reconstructed_from"]
    return {"value": v, "event_date": d.isoformat(), "missing_reason": None, **rb,
            "window_contains_reconstructed": contaminated,
            "point_vintage": vol_point_vintage(sym, d)}


def vol_quality(event_date: date | None, decision_date: date, trading_days: list[date] | None) -> str:
    """日频指数的新鲜度：最新值应是决策日之前最后一个交易日。
    差 1 个交易日 → pending（发布等待，不是失败）；更多 → stale。
    交易日历未覆盖 → unknown_vintage（不猜哪天该有数）。"""
    if event_date is None:
        return MISSING
    if not trading_days:
        return UNKNOWN_VINTAGE
    before = [t for t in trading_days if t < decision_date]
    if not before:
        return UNKNOWN_VINTAGE
    expected = before[-1]
    if event_date >= expected:
        return CURRENT
    gap = sum(1 for t in before if t > event_date)
    return PENDING if gap == 1 else STALE
