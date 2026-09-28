"""期权多层同向读数（conviction，开发期规则；预登记草案 docs/prereg/2026-09-28_conviction_v1.md，待 Codex 复审）。

三层（Codex 018 #7：S/V 都来自同一 IV 曲面，F 的分侧也用相对 IV —— 三层不是三份独立证据）：
- S（曲面）：同一到期的 Δskew25（put−call）≤ −0.5pp → +1（转向 call）；≥ +0.5pp → −1；其余 0；曲面缺失 → None。
- F（近价具体行权价，推断的买卖方，不是已确认的主动方）：grade_leg 判「中」、行权价在 spot ±5% 内、
  且纯净度已知（purity=None 的腿不计入，单独计数 —— Codex 018 #7）。按 |ΔOI×Δ| 分侧：
  看涨侧 = 推断买 call + 推断卖 put；看跌侧 = 推断买 put + 推断卖 call。一侧 > 另一侧 2 倍且 > 0 → ±1，否则 0；
  没有逐腿数据 → None。±5%、2 倍是未校准的一版固定规则，不再在同一历史上调。
- V（ATM IV 顺向扩张）：ΔATM ≥ +0.3pp 且数据日价格有涨跌 → 价格方向（±1）；ΔATM < +0.3pp → 0；
  ΔATM 或价格缺失 → None。数据日价格 = 两份快照 spot 之比（都在决策前已知）；0 变动 → 0。
H1（主问题）= S = F ≠ 0 且 V ∈ {0, S}；任一层 None → None（未知不等于不成立，Codex 018 #8）。
同时输出偏斜水平（skew25/skew10、到期、DTE）供 H3 翻号研究用 —— 翻号判定需要整段序列，在序列层做，不在这里。
纯计算：只吃 FlowAnalysis / StructureRead / 数值，无 I/O。
"""
from __future__ import annotations

RULE = {"version": "conviction-dev-20260928", "s_skew_pp": 0.50, "near_pct": 0.05, "f_ratio": 2.0,
        "v_atm_pp": 0.30, "status": "development（未冻结；记录不得称确认样本）"}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def layers(fa, read, spot_prev: float | None, spot_curr: float | None) -> dict:
    out: dict = {"rule_version": RULE["version"]}
    vs = getattr(fa, "vol", None) if fa is not None else None
    has_vol = bool(vs is not None and getattr(vs, "prev", None) is not None and getattr(vs, "curr", None) is not None)
    if has_vol:
        d25, datm = vs.d_skew25_pp, vs.d_atm_pp
        out.update({"d_skew25_pp": round(d25, 3), "d_atm_pp": round(datm, 3),
                    "skew25_pp": round(vs.curr.skew25_pp, 3), "skew10_pp": round(vs.curr.skew10_pp, 3),
                    "atm_iv_pp": round(vs.curr.atm_iv_pp, 3),
                    "expiry": vs.curr.expiry.isoformat() if getattr(vs.curr, "expiry", None) else None,
                    "dte": getattr(vs.curr, "days_out", None),
                    "prev_expiry": vs.prev.expiry.isoformat() if getattr(vs.prev, "expiry", None) else None})
        S = 1 if d25 <= -RULE["s_skew_pp"] else (-1 if d25 >= RULE["s_skew_pp"] else 0)
    else:
        S, datm = None, None
    # —— F ——
    legs = getattr(read, "legs", None) if (read is not None and getattr(read, "ok", False)) else None
    spot = getattr(fa, "spot", None) if fa is not None else None
    if not legs or not spot:
        F = None
        out.update({"f_bull": None, "f_bear": None, "f_legs": 0, "f_unknown_purity_legs": 0})
    else:
        bull = bear = 0.0
        n = unk = 0
        top = 0.0
        for l in legs:
            if not l.counts or abs(l.strike / spot - 1) > RULE["near_pct"]:
                continue
            if l.purity is None:
                unk += 1
                continue
            w = abs(l.d_oi * l.delta)
            n += 1
            top = max(top, w)
            if (l.kind == "C") == (l.delta_adj_pp > 0):
                bull += w
            else:
                bear += w
        tot = bull + bear
        F = (1 if bull > RULE["f_ratio"] * bear and bull > 0 else
             (-1 if bear > RULE["f_ratio"] * bull and bear > 0 else 0))
        out.update({"f_bull": round(bull, 1), "f_bear": round(bear, 1), "f_legs": n, "f_unknown_purity_legs": unk,
                    "f_top_share": round(top / tot, 3) if tot > 0 else None})
    # —— V ——
    if datm is None or not spot_prev or not spot_curr:
        V = None
        px = None
    else:
        px = spot_curr / spot_prev - 1
        V = _sign(px) if datm >= RULE["v_atm_pp"] else 0
    out.update({"data_day_ret": None if px is None else round(px, 6), "S": S, "F": F, "V": V})
    if None in (S, F, V):
        h1 = None
    else:
        h1 = S if (S != 0 and F == S and V in (0, S)) else 0
    out["H1"] = h1
    out["three_layer"] = None if h1 is None else bool(h1 != 0 and V == S)
    return out
