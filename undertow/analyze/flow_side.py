"""资金流买卖方推断的验证计算（协议 docs/prereg/2026-09-29_flow_side_check_v0.md；探索，不改冻结的 F 层）。

纯计算、无 I/O：逐分钟成交的主动方分类（报价规则优先，否则 tick 规则）→ 合约当日 buy_share → 与相对 IV 推断方向对比。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

QUOTE_WINDOW = timedelta(minutes=2)
BUY_HI, BUY_LO = 0.6, 0.4          # buy_share 分档（未校准设计值）


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def classify_minutes(rows: list, quotes: list) -> dict:
    """rows：[(分钟起点 datetime, 收盘价, 成交量)]，按时间升序；quotes：[(datetime, bid, ask)]。
    返回 {"buy", "sell", "unclassified", "by_quote", "by_tick"}（成交量口径）。"""
    out = {"buy": 0.0, "sell": 0.0, "unclassified": 0.0, "by_quote": 0.0, "by_tick": 0.0}
    prev_px, prev_side = None, None
    good_q = [(t, b, a) for t, b, a in quotes if _num(b) and _num(a) and 0 < b <= a]
    for t, px, vol in rows:
        px, vol = _num(px), _num(vol)
        if not vol or vol <= 0 or px is None or px <= 0:
            continue
        side = None
        near = [q for q in good_q if abs(q[0] - t) <= QUOTE_WINDOW]
        if near:
            _, b, a = min(near, key=lambda q: abs(q[0] - t))
            mid = (b + a) / 2
            if px > mid:
                side = "buy"
            elif px < mid:
                side = "sell"
            if side:
                out["by_quote"] += vol
        if side is None and prev_px is not None:
            side = "buy" if px > prev_px else ("sell" if px < prev_px else prev_side)
            if side:
                out["by_tick"] += vol
        out[side or "unclassified"] += vol
        prev_px, prev_side = px, (side or prev_side)
    return out


def buy_share(c: dict) -> float | None:
    tot = c["buy"] + c["sell"]
    return c["buy"] / tot if tot > 0 else None


def trade_label(share: float | None) -> str:
    if share is None:
        return "unknown"
    return "buy" if share >= BUY_HI else ("sell" if share <= BUY_LO else "mixed")


def inferred_label(delta_adj_pp: float) -> str | None:
    return "buy" if delta_adj_pp > 0 else ("sell" if delta_adj_pp < 0 else None)


def agreement(rows: list) -> dict:
    """rows：{inferred, traded, weight, has_data}。一致率只在 traded ∈ {buy, sell} 且 inferred 非空的行上算。"""
    lab = [r for r in rows if r["traded"] in ("buy", "sell") and r["inferred"]]
    agree = [r for r in lab if r["inferred"] == r["traded"]]
    w_all = sum(r["weight"] for r in rows)
    w_lab = sum(r["weight"] for r in lab)
    return {"n_legs": len(rows), "n_labelled": len(lab), "n_agree": len(agree),
            "agree_rate": round(len(agree) / len(lab), 4) if lab else None,
            "agree_rate_weighted": round(sum(r["weight"] for r in agree) / w_lab, 4) if w_lab else None,
            "weight_covered": round(sum(r["weight"] for r in rows if r["has_data"]) / w_all, 4) if w_all else None,
            "n_mixed": sum(r["traded"] == "mixed" for r in rows), "n_unknown": sum(r["traded"] == "unknown" for r in rows)}
