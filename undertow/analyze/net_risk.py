"""同一标的整体风险（净额）—— 纯函数，无 I/O（Codex 061/062，用户 2026-10-19）。

用户原话：「我认为同一个品种的所有期权，应该整体计算风险，不然分开开仓的对冲，难道重复计算风险吗」
「不同到期，比如日历价差，对角价差等，应该整体计算风险暴露。如果不确定的时候，再以保守上限计算。」
「平仓的时候，如果单独部分平仓，又要重新计算风险」

旧口径：政策 B 把每个逻辑组合的到期最大亏损相加。对能相互抵消的腿（如借记看跌价差上再加同到期牛市 put 价差 =
蝶式），那是【各自最坏】之和，不是【共同价格情景】下的最坏；分开下单的对冲被重复计险、被误判超限。

风险单元 = 同一标的的全部在场腿（含指派/行权留下的正股）+ 拟变更的增量腿。损益：

    P(S) = 净现金流（已付费用已在内）− 尚未发生的费用 + Σ 净数量 × 乘数 × 内在价值(S)

结果分四种 bound_type（Codex 062 二节，不混用）：
  joint_bound         同一到期：分段线性到期损益在 S=0、各行权价与右尾斜率上精确取极值（method=terminal_payoff_exact）。
  conservative_bound  跨到期：假设【最近到期日的管理截止前整组退出，或在那时按剩余持仓重算】。最近到期的腿按内在价值；
                      更远到期的多头按美式下界（≥ 内在价值，忽略时间价值 → 保守），空头按无套利上界（call ≤ S、put ≤ K）。
                      这是有依据的保守上限，不是精确最大亏损；远月空头越多越松。
  unbounded           同一到期且右尾净空 call（斜率 < 0）→ 真实无上限。
  unresolved          输入不全（腿缺字段/数量非有限/现金未知），或跨期保守界为无穷（拆裸腿的无穷 ≠ 已证明组合无界）。

不在本模块里：保证金占用、提前指派过程、分腿成交中间态、券商处置——这些是独立维度（Codex 061 §6），
不能拿来把已确认的经济抵扣加回成各笔最坏之和。未成交的挂单保护不得作为当前持有的腿传进来。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

VERSION = "netting-v1-20261019"
SOURCE = "Codex 061（同到期净额）+ 062（跨到期整体、部分平仓重算），用户 2026-10-19 要求"
MULT = 100
# 到期日处置（Codex 061 §6）：长桥 FAQ 写明到期日 ET 14:00 起提高保证金；13:00 开始检查、13:30 前确认剩余期权全部退出。
# 30 分钟是【未校准】的操作缓冲，不是券商保证 13:30 前安全；提前收市日按 min(券商风险时点, 收市) − 30 分钟。
EXPIRY_CHECK_ET, EXPIRY_EXIT_BY_ET = "13:00", "13:30"
EXPIRY_NOTE = (f"到期日 ET {EXPIRY_CHECK_ET} 开始处置检查、{EXPIRY_EXIT_BY_ET} 前确认剩余期权全部退出"
               "（长桥：到期日 ET 14:00 起提高保证金；30 分钟缓冲未校准；提前收市日前移）。分腿时先回补短腿。")


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _exp(e):
    if isinstance(e, date):
        return e.isoformat()
    return e if isinstance(e, str) and e.strip() else None


def normalize(legs) -> tuple[list[dict], list[str]]:
    """校验并按合约合并带符号数量（同一合约先求净数量，不按历史组合标签配对）。返回 (净腿, 问题)。
    腿：{"kind": "C"/"P"/"STOCK", "strike", "expiry", "qty"(期权=张、正股=股，带符号), ["symbol"]}。"""
    probs, acc = [], defaultdict(float)
    for i, lg in enumerate(legs or []):
        if not isinstance(lg, dict):
            probs.append(f"第 {i + 1} 条腿不是对象"); continue
        k, q = lg.get("kind"), lg.get("qty")
        if not _num(q):
            probs.append(f"{lg.get('symbol') or i + 1}：数量非有限"); continue
        if k == "STOCK":
            acc[("STOCK", None, None)] += q
            continue
        if k not in ("C", "P"):
            probs.append(f"{lg.get('symbol') or i + 1}：未知种类 {k!r}"); continue
        s, e = lg.get("strike"), _exp(lg.get("expiry"))
        if not (_num(s) and s > 0) or e is None:
            probs.append(f"{lg.get('symbol') or i + 1}：缺行权价或到期"); continue
        acc[(k, float(s), e)] += q
    out = [{"kind": k, "strike": s, "expiry": e, "qty": q} for (k, s, e), q in sorted(
        acc.items(), key=lambda kv: (kv[0][2] or "", kv[0][0], kv[0][1] or 0)) if abs(q) > 1e-12]
    return out, probs


def _extrema(terms: list[tuple[str, float, float]], const: float) -> dict:
    """分段线性 f(S) = const + Σ 项，S ≥ 0。项：("C", K, w) = w·max(S−K,0)；("P", K, w) = w·max(K−S,0)；("L", 0, w) = w·S。
    极值只可能在 S=0、各拐点或右尾：右尾斜率 = Σ C 与 L 的 w。返回 min/max 及取值点（右尾无界 → None + 标记）。"""
    def f(S):
        v = const
        for t, K, w in terms:
            v += w * (max(S - K, 0.0) if t == "C" else max(K - S, 0.0) if t == "P" else S)
        return v
    pts = sorted({0.0} | {K for t, K, _ in terms if t in ("C", "P")})
    vals = [(f(S), S) for S in pts]
    slope = sum(w for t, _, w in terms if t in ("C", "L"))
    lo, lo_at = min(vals)
    hi, hi_at = max(vals)
    return {"min": None if slope < -1e-12 else lo, "min_at": None if slope < -1e-12 else lo_at,
            "max": None if slope > 1e-12 else hi, "max_at": None if slope > 1e-12 else hi_at,
            "right_slope": slope, "points": [(round(S, 4), round(v, 2)) for v, S in vals]}


def unit_risk(legs, *, net_cash, future_fees=0.0, cash_basis: str = "", label: str = "") -> dict:
    """一个风险单元（同一标的）的整体风险。net_cash：这些腿至今全部进出账净额（负 = 净投入，已付费用在内；
    候选增量按有时点的报价情景换算）；future_fees：之后平仓/处置还要付的费用（模型假设，另列）。"""
    net, probs = normalize(legs)
    out = {"version": VERSION, "label": label, "net_legs": net, "cash_basis": cash_basis or "未注明",
           "net_cash": net_cash, "future_fees": future_fees, "problems": probs, "assumptions": [],
           "method": None, "bound_type": "unresolved", "max_loss": None, "max_gain": None,
           "max_gain_unbounded": False, "worst_at": None, "best_at": None, "expiries": [],
           "option_value_nonneg": None, "per_expiry": []}
    if not _num(net_cash) or not (_num(future_fees) and future_fees >= 0):
        probs.append("净现金流或未来费用未知")
    if probs:
        return out
    const = net_cash - future_fees
    exps = sorted({lg["expiry"] for lg in net if lg["kind"] != "STOCK"})
    out["expiries"] = exps
    stock = sum(lg["qty"] for lg in net if lg["kind"] == "STOCK")
    stock_term = [("L", 0.0, stock)] if abs(stock) > 1e-12 else []
    # 各到期单独的到期损益（解释用：贡献，不与整体结果相加）
    for e in exps:
        ex = _extrema([(lg["kind"], lg["strike"], lg["qty"] * MULT) for lg in net if lg["expiry"] == e], 0.0)
        out["per_expiry"].append({"expiry": e, "option_payoff_min": ex["min"], "option_payoff_max": ex["max"]})
    if len(exps) <= 1:
        terms = [(lg["kind"], lg["strike"], lg["qty"] * MULT) for lg in net if lg["kind"] != "STOCK"] + stock_term
        opt = _extrema([t for t in terms if t[0] != "L"], 0.0)
        out["option_value_nonneg"] = (opt["min"] is not None and opt["min"] >= -1e-9) if not stock_term else False
        ex = _extrema(terms, const)
        out["method"] = "terminal_payoff_exact"
        out["assumptions"].append("同一到期：到期损益分段线性，在 S=0、各行权价与右尾斜率上精确取极值；正股按到期价计")
        if ex["min"] is None:
            out["bound_type"] = "unbounded"
            out["assumptions"].append("右尾净空 call/正股（斜率 < 0）：价格上涨亏损无上限")
        else:
            out["bound_type"], out["max_loss"], out["worst_at"] = "joint_bound", round(max(0.0, -ex["min"]), 2), ex["min_at"]
        out["max_gain"] = None if ex["max"] is None else round(ex["max"], 2)
        out["max_gain_unbounded"] = ex["max"] is None
        out["best_at"] = ex["max_at"]
        return out
    # 跨到期：最近到期日整组退出（或那时按剩余持仓重算）的保守界
    t1 = exps[0]
    near = [(lg["kind"], lg["strike"], lg["qty"] * MULT) for lg in net if lg.get("expiry") == t1] + stock_term
    far = []                                     # 每个远月到期一组：(逐腿下界项, 常数, 该组到期最小损益)
    for e in exps[1:]:
        g = [lg for lg in net if lg.get("expiry") == e]
        terms, c0 = [], 0.0
        for lg in g:
            w = lg["qty"] * MULT
            if w > 0:                            # 远月多头：美式下界 = 内在价值（时间价值不计）
                terms.append((lg["kind"], lg["strike"], w))
            elif lg["kind"] == "C":              # 远月空 call：价值 ≤ S
                terms.append(("L", 0.0, w))
            else:                                # 远月空 put：价值 ≤ K
                c0 += w * lg["strike"]
        m = _extrema([(lg["kind"], lg["strike"], lg["qty"] * MULT) for lg in g], 0.0)["min"]
        far.append((terms, c0, m))
    ex = _cross_min(near, far, const)
    out["method"] = "first_expiry_exit_bound"
    out["assumptions"] += [f"跨到期：假设最近到期日 {t1} 的管理截止前整组退出，或那时按剩余持仓重新计险",
                           "最近到期的腿按内在价值；每个远月到期组的价值取两个有效下界中较大者："
                           "逐腿（多头 ≥ 内在价值、空 call ≤ S、空 put ≤ K）与该组自身到期最小损益（无套利，按零利率、"
                           "美式提前行权时多头腿也可行权对冲）→ 保守上限，不计远月时间价值",
                           "若最近到期后仍持有远月腿，本界失效，须按剩余持仓重算"]
    if ex is None:
        out["bound_type"] = "unresolved"
        out["assumptions"].append("保守界为无穷：不等于已证明组合无上限（远月腿缺可证明的保护）")
    else:
        out["bound_type"], out["max_loss"], out["worst_at"] = "conservative_bound", round(max(0.0, -ex[0]), 2), ex[1]
    out["max_gain"] = None                       # 跨期最大收益依赖远月时间价值，未建模
    return out


def _cross_min(near, far, const):
    """f(S) = const + 近月到期损益(S) + Σ_远月组 max(逐腿下界_g(S), 组到期最小_g)，S ≥ 0 上的最小值 (值, 取值点)；
    右尾趋向 −∞ → None。拐点：0、全部行权价、逐腿下界与组常数下界的交点、右尾。"""
    def lin(terms, S):
        return sum(w * (max(S - K, 0.0) if t == "C" else max(K - S, 0.0) if t == "P" else S) for t, K, w in terms)

    def f(S):
        v = const + lin(near, S)
        for terms, c0, m in far:
            a = c0 + lin(terms, S)
            v += a if m is None else max(a, m)
        return v
    pts = {0.0} | {K for t, K, _ in near if t in ("C", "P")}
    for terms, _, _ in far:
        pts |= {K for t, K, _ in terms if t in ("C", "P")}
    base = sorted(pts)
    xs = base + [base[-1] + 1.0]
    for terms, c0, m in far:                     # 逐腿下界与组常数的交点（每段线性）
        if m is None:
            continue
        for a0, a1 in zip(xs, xs[1:]):
            d0, d1 = c0 + lin(terms, a0) - m, c0 + lin(terms, a1) - m
            if d0 * d1 < 0:
                pts.add(a0 + (a1 - a0) * d0 / (d0 - d1))
        x = xs[-1]
        sl = (c0 + lin(terms, x + 1) - m) - (c0 + lin(terms, x) - m)
        dx = c0 + lin(terms, x) - m
        if sl != 0 and -dx / sl > 0:
            pts.add(x - dx / sl)
    allp = sorted(pts)
    X = allp[-1] + 1.0
    if f(X + 1.0) - f(X) < -1e-9:
        return None
    v, S = min((f(S), S) for S in allp + [X])
    return v, round(S, 4)


def hold_risk(legs, value_now, *, fee_now: float, fee_future: float) -> dict:
    """从现在继续持有的风险（Codex 064 §5 第 3 把尺子）：max(0, Vnow − Vmin + f_future − f_now)。
    value_now：剩余腿此刻的带符号可平仓值（多头按买一、空头按卖一，资产正、负债负）；fee_now：此刻平仓费（净清算基准已扣）；
    fee_future：未来最坏情景的处置费。实现上把「此刻净清算值」当作入场现金交给 unit_risk，跨期沿用同一保守界。
    它回答「从此刻可能回吐多少」（含已进入权益的浮盈），不是本轮累计亏损，也不进政策 B 的入场预算。"""
    if not (_num(value_now) and _num(fee_now) and _num(fee_future)):
        r = unit_risk(legs, net_cash=None, future_fees=0.0, label="从现在起")
        r["problems"].append("当前可平仓值或费用未知")
        return r
    r = unit_risk(legs, net_cash=-(value_now - fee_now), future_fees=fee_future,
                  cash_basis="从现在起：此刻净清算值（买一/卖一、扣平仓费）作起点", label="从现在起")
    r["value_now"] = round(value_now, 2)
    return r


def stop_bound(r: dict) -> float | None:
    """整组止损损失的【上界】（不是止损规则本身）：同一到期、不含正股、期权部分到期损益处处 ≥ 0（借记价差、蝶式等）
    → 无套利下该组合任意时刻价值 ≥ 0，任何时点整组退出的损失 ≤ 已投入净额 + 退出费用 = max_loss（忽略买卖价差滑点）。
    其余结构没有整组止损规则 → None（显示「待补」，不伪造金额）。"""
    if r.get("bound_type") == "joint_bound" and r.get("option_value_nonneg") and _num(r.get("max_loss")):
        return r["max_loss"]
    return None


def scale(legs, k) -> list[dict]:
    return [dict(lg, qty=lg["qty"] * k) for lg in legs]


def what_if(current_legs, current_cash, delta_legs, delta_cash, *, future_fee_per_contract: float,
            cash_basis: str = "") -> dict:
    """现仓 → 拟操作后剩余组合（加腿、减仓、部分平仓都用增量腿表示；平仓 = 反向数量，delta_cash 为该操作的现金情景）。
    未成交前只是情景；实际成交后应按券商净持仓与真实流水重读（Codex 062 §4）。"""
    def fees(legs):
        n, _ = normalize(legs)
        return round(sum(abs(lg["qty"]) for lg in n if lg["kind"] != "STOCK") * future_fee_per_contract, 2)
    before = unit_risk(current_legs, net_cash=current_cash, future_fees=fees(current_legs), cash_basis=cash_basis,
                       label="操作前")
    after_legs = list(current_legs) + list(delta_legs)
    after = unit_risk(after_legs, net_cash=(current_cash + delta_cash) if _num(current_cash) and _num(delta_cash) else None,
                      future_fees=fees(after_legs), cash_basis=cash_basis + "＋候选报价情景", label="操作后")
    d = None
    if _num(before["max_loss"]) and _num(after["max_loss"]):
        d = round(after["max_loss"] - before["max_loss"], 2)
    return {"before": before, "after": after, "delta_max_loss": d,
            "note": "拟操作后只是情景：未成交保护不计入当前；成交（含部分成交）后按真实持仓重算"}
