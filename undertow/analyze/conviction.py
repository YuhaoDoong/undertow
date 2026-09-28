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

#: 冻结（Codex 022）：常量与 conviction-dev-20260928 完全相同，只改版本号与状态 → 新目录记录；
#: 开发期目录 direction_ledger/conviction-dev-20260928/ 原样保留、不回填。正式样本起点见 direction_stats.FAMILY_D_START。
RULE = {"version": "conviction-h1-v1-20260928", "s_skew_pp": 0.50, "near_pct": 0.05, "f_ratio": 2.0,
        "v_atm_pp": 0.30, "status": "frozen（方向台账族 D；积累期 T3，不参与决策）"}


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


H3_RULE = {"min_run": 10, "cooldown": 10, "merge_window": 5, "miss_reset": 2,
           "note": "符号严格按正负（不设死区，避免看过 9/17 的 +0.05 后再定门槛）；未校准、开发期"}


def flip_states(rows: list[dict], *, rule: dict | None = None) -> dict:
    """H3 偏斜翻号的逐日状态（Codex 018 #8：换月、连续日、缺失、零、翼间冲突、冷却全部写死）。

    rows：按交易日升序 [{session, skew25_pp, skew10_pp, expiry}]；缺一天就传 {session, missing: True}。
    每翼独立：
      · 值为 None / missing → 缺失；连续缺失 ≥ miss_reset 天 → 持续计数清零（未知不能算持续）。单日缺失只暂停计数。
      · 值恰为 0 → zero，不改变持续计数。
      · 与上一个非缺失记录的到期不同 = 换月日：当天若变号，记 roll_switch（结构切换），重置持续段，不计翻号。
      · 变号且此前同号持续 ≥ min_run 个有值交易日、且距本翼上次事件 > cooldown → flip 事件；
        翻成 put 贵（由负转正）记看跌 −1，翻成 call 贵记看涨 +1。持续不足 → change_short_run。
    两翼合并（前缀不变，Codex 019-04）：首翼事件当天发布、不可撤改；窗口内另一翼后来同向 → 追加 confirmations 注释；
    后来反向 → 当天另记 conflict_after。同一天两翼反向 → 当天 conflict。
    「此前同号持续」= 允许单日缺失与零值暂停的 ≥ min_run 个有值观察（不是严格连续交易日）。
    返回 {states: {session: {wing: 状态}}, wing_events: [...], events: [...]}。"""
    rule = rule or H3_RULE
    states: dict = {r["session"]: {} for r in rows}
    wing_events = []
    for wing in ("skew25_pp", "skew10_pp"):
        run_sign, run_len, miss, prev_exp, last_ev = None, 0, 0, None, None
        for i, r in enumerate(rows):
            v = None if r.get("missing") else r.get(wing)
            if v is None:
                miss += 1
                if miss >= rule["miss_reset"]:
                    run_sign, run_len = None, 0
                states[r["session"]][wing] = "missing"
                continue
            miss = 0
            roll = prev_exp is not None and r.get("expiry") != prev_exp
            prev_exp = r.get("expiry")
            sg = (v > 0) - (v < 0)
            if sg == 0:
                states[r["session"]][wing] = "zero"
                continue
            if run_sign is None:
                run_sign, run_len = sg, 1
                states[r["session"]][wing] = "start"
                continue
            if sg == run_sign:
                run_len += 1
                states[r["session"]][wing] = "hold_roll" if roll else "hold"
                continue
            if roll:
                st = "roll_switch"
            elif run_len >= rule["min_run"] and (last_ev is None or i - last_ev > rule["cooldown"]):
                st = "flip"
                last_ev = i
                wing_events.append({"session": r["session"], "i": i, "wing": wing, "direction": -1 if sg > 0 else 1,
                                    "prior_run": run_len})
            else:
                st = "change_short_run"
            states[r["session"]][wing] = st
            run_sign, run_len = sg, 1
    wing_events.sort(key=lambda e: (e["i"], e["wing"]))
    # 两翼合并（Codex 019-04：已发布事件不得被未来改写）——
    #   · 当天两翼同时翻号：同向合并为一个事件；反向当天即判 conflict（两翼数据同时可得，不涉及未来）。
    #   · 首翼事件当天发布、此后不可撤改；merge_window 内另一翼后来同向翻号 → 只在原事件上追加 confirmations 注释；
    #     后来反向翻号 → 在【它发生的那天】另记一条 conflict_after（引用原事件），原事件仍是 event。
    events: list = []
    for w in wing_events:
        same_day = next((e for e in events if e["i"] == w["i"] and w["wing"] not in e["wings"]), None)
        if same_day is not None:
            same_day["wings"].append(w["wing"])
            if same_day["direction"] != w["direction"]:
                same_day["status"] = "conflict"
            continue
        prior = next((e for e in reversed(events) if e["status"] == "event" and w["wing"] not in e["wings"]
                      and 0 < w["i"] - e["i"] <= rule["merge_window"]), None)
        if prior is not None and prior["direction"] == w["direction"]:
            prior.setdefault("confirmations", []).append({"session": w["session"], "wing": w["wing"]})
        elif prior is not None:
            events.append({"session": w["session"], "i": w["i"], "wing": w["wing"], "direction": w["direction"],
                           "prior_run": w["prior_run"], "wings": [w["wing"]], "status": "conflict_after",
                           "refers_to": prior["session"]})
        else:
            events.append({**w, "wings": [w["wing"]], "status": "event"})
    return {"rule": rule, "states": states, "wing_events": wing_events, "events": events}
