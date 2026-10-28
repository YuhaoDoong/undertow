"""同标的风险单元的现金归属（Codex 063 N1、064 §5）—— 纯函数，无 I/O。

旧口径（`risk_aggregate.apply_cash_ledger`，已删除）逐账本行按「行内各腿合约代码」取全部流水：
  1. 只认【仍在场】合约 → 换腿时已离场腿的真实现金被丢掉。2026-10-23 SLV 换腿：卖出 52P 收入 +700 不在任何一行，
     risk-unit 把同一单元的最大亏损报成 $456（真实到期最坏约 $22），单笔/金银合计误判 fail，巡检推假告警。
  2. 两行共用中间行权价时同一条流水被两行各认领一次（Codex 063 反例：真实 −200 被算成 +400，蝶式风险 200 → 0）。
  3. 「含同合约更早轮次」把独立已了结交易的现金当成本轮抵扣；缺 balance 当 0；90 天窗口不证明覆盖建仓起点。

本模块的口径：
- **事件**：每条流水只分配一次（按行对象计，不按 (代码, 时刻, 金额) 去重——源没有事件 id，同一秒同代码同金额的两条
  是两笔真实分笔成交，如 10/23 卖 52P 两条各 +350；若券商重复推送同一条，本模块无法识别，只计数报告）。
- **campaign 关联**：从在场合约出发，经「同一时刻成交」（同一秒 = 同一组合单的代理）传递闭包，把换腿时离场的旧腿
  并入；与在场合约没有同秒成交的同标的交易（例如另开另平的 10/26 那组）不并入，它们是独立交易。
- **起点**：私有计划登记了 `campaign_start` → 已核起点；没登记 → 取所有【可行起点】里现金最少的那个（保守界）：
  可行 = 起点及之后的关联成交覆盖每个在场合约。真实起点必在候选里，所以取最小值不会低估风险。
- **费用**：费用次日入账。一条费用行覆盖同合约「上一条费用行之后到它之前」的成交；覆盖的成交全在起点前 → 不计；
  跨起点 → 全额计入（保守）。成交之后还没有费用行 → 按模型估计计入（标估计）。
- **窗口**：在场合约在窗口内没有任何成交、登记起点早于窗口、最早关联成交落在窗口首日 → 不能证明覆盖 → incomplete。

三把尺子（Codex 064 §5，不混用、不相加）：
  1. campaign 累计：本轮全部实际现金 + 剩余腿到期 payoff − 未来费用（本模块给现金，`net_risk.unit_risk` 给风险）。
  2. 新阶段入场：私有计划登记 `stage`（起点 + 沿用腿的内部估值区间）→ 新阶段现金 = 起点后新腿的真实现金 + 沿用腿按
     内部价的虚拟划转；旧阶段参考 = 本轮现金 − 新阶段现金，两者之和恒等于真实总账。虚拟划转不产生费用、不写进现金。
     政策 B 预算取区间里使新阶段风险最大的端点（现金最少），不是「按 ask 对两边都保守」。
  3. 从现在起：`net_risk.hold_risk`（需要实时报价，在命令层算）。
政策 B 用哪一把：登记了新阶段 → 2 的保守端（新交易从成立时点算，不吃旧阶段利润，Codex 063 N1 第 4 条）；否则 → 1。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

VERSION = "campaign-cash-v1-20261028"
SOURCE = "Codex 063 N1（现金事件唯一分配、来源质量）+ 064 §5（换腿三把尺子），2026-10-23 SLV 换腿实例"
MULT = 100
FEE_PER_CONTRACT = 0.80          # 与 healthcheck / spread_econ 同一模型费率（只用于未入账费用的估计）
_OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+)(?:\.[A-Z]{1,4})?$")
_STOCK = re.compile(r"^([A-Z]+)(?:\.[A-Z]{1,4})?$")


def parse_occ(sym: str):
    """期权代码 → (root, 'YYYY-MM-DD', 'C'|'P', 行权价)；不是期权代码 → None。"""
    m = _OCC.match((sym or "").upper())
    if not m:
        return None
    root, yy, mm, dd, cp, k = m.groups()
    return root, f"20{yy}-{mm}-{dd}", cp, int(k) / 1000.0


def symbol_root(sym: str) -> str | None:
    """流水代码所属标的根：期权取根，正股（如 SLV.US）取代码本身；都认不出 → None。"""
    o = parse_occ(sym)
    if o:
        return o[0]
    m = _STOCK.match((sym or "").upper())
    return m.group(1) if m else None


def plan_key(root: str, expiry: str, side: str, k_sell: float, k_buy: float) -> str:
    """私有持仓计划的键（riskwatch 同一格式）：live|ROOT|到期|C/P|卖腿/买腿。"""
    return f"live|{root}|{expiry}|{side}|{k_sell:g}/{k_buy:g}"


def plan_key_for_legs(root: str, legs: list[dict]) -> str | None:
    """账本行的腿 → 计划键；只认同到期、同类型、一多一空、张数相等的垂直价差，其它 → None（没有计划可对）。"""
    if len(legs) != 2 or any(lg.get("kind") not in ("C", "P") for lg in legs):
        return None
    a, b = legs
    if a["kind"] != b["kind"] or a.get("expiry") != b.get("expiry") or a["qty"] != -b["qty"] or a["qty"] == 0:
        return None
    sell, buy = (a, b) if a["qty"] < 0 else (b, a)
    return plan_key(root, sell["expiry"], sell["kind"], float(sell["strike"]), float(buy["strike"]))


def _ts(s):
    """ISO 时刻 → aware datetime；不合法 → None。无时区的不接受（不默认添时区）。"""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _amount(r):
    try:
        v = float(r.get("balance"))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _is_fee(r) -> bool:
    return "fee" in str(r.get("flow_name") or "").lower()


def _parse_rows(unit: str, cash_rows: list[dict]):
    """本单元的流水 → (成交行, 费用行, 问题)。每行带 idx（原始位置）、sym、t、amt。不是本单元的行不看。"""
    tx, fees, probs = [], [], []
    for i, r in enumerate(cash_rows or []):
        sym = str(r.get("symbol") or "").upper()
        if symbol_root(sym) != unit:
            continue
        t, amt = _ts(r.get("time")), _amount(r)
        if t is None or amt is None:
            probs.append(f"{sym}：流水{'时刻' if t is None else '金额'}缺失或无法解析（不当 0）")
            continue
        row = {"idx": i, "sym": sym, "t": t, "amt": amt}
        (fees if _is_fee(r) else tx).append(row)
    return tx, fees, probs


def _closure(held: set[str], tx: list[dict], start) -> tuple[set[str], list[dict]]:
    """从在场合约出发，经同一时刻（同秒）成交传递闭包；只看 start 及之后的成交。→ (关联合约, 关联成交)。"""
    events: dict = {}
    for r in tx:
        if start is None or r["t"] >= start:
            events.setdefault(r["t"], []).append(r)
    syms = set(held)
    while True:
        grow = {r["sym"] for ev in events.values() if any(x["sym"] in syms for x in ev) for r in ev}
        if grow <= syms:
            break
        syms |= grow
    used = [r for ev in events.values() if any(x["sym"] in syms for x in ev) for r in ev]
    return syms, sorted(used, key=lambda r: (r["t"], r["idx"]))


def _fee_coverage(fees: list[dict], tx: list[dict]) -> dict:
    """每条费用行覆盖的成交（同合约、上一条费用行之后到本条之前）。→ {费用 idx: [成交行]}。"""
    out = {}
    by_sym: dict = {}
    for f in fees:
        by_sym.setdefault(f["sym"], []).append(f)
    for sym, fs in by_sym.items():
        fs = sorted(fs, key=lambda f: (f["t"], f["idx"]))
        txs = sorted((r for r in tx if r["sym"] == sym), key=lambda r: r["t"])
        prev = None
        for f in fs:
            out[f["idx"]] = [r for r in txs if (prev is None or r["t"] > prev) and r["t"] <= f["t"]]
            prev = f["t"]
    return out


def _cash_from(start, held: set[str], tx, fees, cover, est_qty: int):
    """给定起点：关联成交 + 归属费用 + 未入账费用估计。→ dict 或 None（不可行：有在场合约在起点后没有成交）。"""
    syms, used = _closure(held, tx, start)
    if not held <= {r["sym"] for r in used}:
        return None
    used_idx = {r["idx"] for r in used}
    fee_rows, notes, straddle = [], [], False
    for f in fees:
        if f["sym"] not in syms:
            continue
        cov = cover.get(f["idx"], [])
        if not cov:
            if start is None or f["t"] >= start:
                notes.append(f"{f['sym']} {f['t']:%m-%d} 费用行找不到对应成交（窗口外或源异常），未计入")
            continue
        if all(r["idx"] in used_idx for r in cov):
            fee_rows.append(f)
        elif any(r["idx"] in used_idx for r in cov):
            fee_rows.append(f)                           # 跨起点：全额计入（费用为负，计入更保守）
            straddle = True
    if straddle:
        notes.append("有费用行同时覆盖起点前后的成交：全额计入（保守）")
    covered = {r["idx"] for f in fee_rows for r in cover.get(f["idx"], [])}
    pending = [r for r in used if r["idx"] not in covered]
    est = round(len(pending) * est_qty * FEE_PER_CONTRACT, 2)
    cash = round(sum(r["amt"] for r in used) + sum(f["amt"] for f in fee_rows) - est, 2)
    return {"start": start, "syms": syms, "tx": used, "fees": fee_rows, "pending": pending, "fee_estimate": est,
            "cash": cash, "notes": notes}


def _stage_view(stage: dict | None, row: dict, alloc: dict, tx, fees, cover, est_qty: int, start) -> dict | None:
    """新阶段入场现金（尺子 2）。stage = {"start": ISO, "inherited": [{symbol, qty, ref, low, high, basis?}]}。
    → {"cash": {"low","ref","high"}, "policy_cash", "real_cash", "old_stage": {...}, "problems": [...]}；没登记 → None。"""
    if not stage:
        return None
    probs = []
    st = _ts(stage.get("start"))
    if st is None:
        probs.append("新阶段起点不是带时区的 ISO 时刻")
    elif start is not None and st < start:
        probs.append("新阶段起点早于本轮起点")
    held = {(lg.get("symbol") or "").upper(): lg for lg in row["legs"]}
    inh = stage.get("inherited") or []
    if not inh:
        probs.append("新阶段没有登记沿用腿（没有沿用腿就不是换腿阶段，按本轮累计即可）")
    virt = {"low": 0.0, "ref": 0.0, "high": 0.0}
    for x in inh:
        sym = str(x.get("symbol") or "").upper()
        lg = held.get(sym)
        q = x.get("qty")
        ps = [x.get(k) for k in ("low", "ref", "high")]
        if lg is None or not isinstance(q, int) or isinstance(q, bool) or q != lg.get("qty"):
            probs.append(f"沿用腿 {sym} 与在场持仓不符（代码或带符号张数）")
            continue
        if not all(isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) and p > 0 for p in ps) \
                or not ps[0] <= ps[1] <= ps[2]:
            probs.append(f"沿用腿 {sym} 的内部估值须为 0 < low ≤ ref ≤ high")
            continue
        for k, p in zip(("low", "ref", "high"), ps):
            virt[k] += -q * MULT * p                       # 沿用空头（q<0）= 新阶段按内部价「卖出」→ 正现金
    if probs:
        return {"problems": probs}
    inh_syms = {str(x["symbol"]).upper() for x in inh}
    new_syms = set(held) - inh_syms
    real_tx = [r for r in tx if r["sym"] in new_syms and r["t"] >= st]
    real_idx = {r["idx"] for r in real_tx}
    real_fees = [f for f in fees if f["sym"] in new_syms and any(r["idx"] in real_idx for r in cover.get(f["idx"], []))]
    covered = {r["idx"] for f in real_fees for r in cover.get(f["idx"], [])}
    est = round(sum(1 for r in real_tx if r["idx"] not in covered) * est_qty * FEE_PER_CONTRACT, 2)
    real = round(sum(r["amt"] for r in real_tx) + sum(f["amt"] for f in real_fees) - est, 2)
    cash = {k: round(real + v, 2) for k, v in virt.items()}
    row_cash = alloc.get(row["id"])
    return {"start": stage.get("start"), "real_cash": real, "virtual": {k: round(v, 2) for k, v in virt.items()},
            "cash": cash, "policy_cash": min(cash.values()), "policy_endpoint": min(cash, key=cash.get),
            "old_stage": None if row_cash is None else {k: round(row_cash - v, 2) for k, v in cash.items()},
            "inherited": inh, "fee_estimate": est, "problems": []}


def unit_cash(unit: str, rows: list[dict], cash_rows: list[dict], *, window_start: str,
              anchors: dict | None = None, stages: dict | None = None) -> dict:
    """一个风险单元的现金归属。rows：同一标的的在场账本行（各带 id、legs[symbol, qty]）。
    anchors：{行 id: campaign_start ISO}（私有计划登记）；stages：{行 id: stage}。
    → {"quality": verified|bound|incomplete, "campaign_cash", "by_row", "policy_cash_by_row", "stages", ...}。"""
    anchors, stages = anchors or {}, stages or {}
    out = {"version": VERSION, "unit": unit, "window_start": window_start, "quality": "incomplete",
           "campaign_cash": None, "by_row": {}, "policy_cash_by_row": {}, "start": None, "start_basis": None,
           "linked": [], "exited": [], "independent_rows": 0, "duplicate_keys": 0, "fee_estimate": 0.0,
           "pending_fee_rows": 0, "stages": {}, "problems": [], "notes": []}
    w0 = _ts(window_start)
    if w0 is None:
        out["problems"].append("窗口起点不是带时区的 ISO 时刻")
        return out
    tx, fees, probs = _parse_rows(unit, cash_rows)
    out["problems"] += probs
    tx = [r for r in tx if r["t"] >= w0]                 # 窗口以外的行不用（调用方按窗口取流水；这里再守一次）
    fees = [f for f in fees if f["t"] >= w0]
    rows = sorted(rows, key=lambda r: r["id"])
    held_by_row = {}
    for r in rows:
        syms = {(lg.get("symbol") or "").upper() for lg in r.get("legs") or []}
        if not r.get("legs") or "" in syms:
            out["problems"].append(f"{r['id']}：腿缺合约代码，无法对流水")
        held_by_row[r["id"]] = syms
    held = set().union(*held_by_row.values()) if held_by_row else set()
    seen_tx = {r["sym"] for r in tx}
    for s in sorted(held - seen_tx):
        out["problems"].append(f"{s}：窗口内（{window_start[:10]} 起）没有任何成交流水 → 建仓在窗口外或源缺行，不能证明覆盖")
    keys = [(r["sym"], r["t"], r["amt"]) for r in tx]
    out["duplicate_keys"] = len(keys) - len(set(keys))
    if out["duplicate_keys"]:
        out["notes"].append(f"{out['duplicate_keys']} 条成交与另一条代码/时刻/金额完全相同：按多笔真实分笔成交计"
                            "（源无事件 id，若是券商重复推送无法识别）")
    est_qty = max((abs(int(lg["qty"])) for r in rows for lg in r.get("legs") or []), default=1)
    cover = _fee_coverage(fees, tx)

    # 起点：每个在场行各自定（有登记 → 已核；没登记 → 可行起点里现金最少者），再取并集（每条流水只算一次）
    results, bases = [], []
    for r in rows:
        h = held_by_row[r["id"]]
        a = anchors.get(r["id"])
        if a is not None:
            at = _ts(a)
            if at is None:
                out["problems"].append(f"{r['id']}：登记的本轮起点不是带时区的 ISO 时刻")
                continue
            if at < w0:
                out["problems"].append(f"{r['id']}：登记起点 {a} 早于流水窗口起点 → 窗口没覆盖整轮")
                continue
            res = _cash_from(at, h, tx, fees, cover, est_qty)
            if res is None:
                out["problems"].append(f"{r['id']}：登记起点 {a} 之后缺在场合约的成交（起点登记晚了）")
                continue
            results.append(res); bases.append("anchor")
            continue
        feas = {}                                       # 有效起点 = 该候选下第一条关联成交的时刻（同一闭包只留一份）
        for c in sorted({t["t"] for t in tx}):
            x = _cash_from(c, h, tx, fees, cover, est_qty)
            if x is not None:
                x["start"] = x["tx"][0]["t"]
                feas.setdefault(x["start"], x)
        if not feas:
            out["problems"].append(f"{r['id']}：找不到覆盖全部在场合约的起点")
            continue
        widest = feas[min(feas)]
        if widest["start"].date() == w0.date():
            out["problems"].append(f"{r['id']}：最早关联成交落在窗口首日 {window_start[:10]}，可能被截断 → 放宽窗口或登记起点")
            continue
        worst = min(feas.values(), key=lambda x: (x["cash"], x["start"]))
        worst["inferred_start"] = widest["start"]
        results.append(worst); bases.append("bound")
    if out["problems"]:
        return out

    used, fee_used, notes = {}, {}, []
    for res in results:
        for x in res["tx"]:
            used[x["idx"]] = x
        for f in res["fees"]:
            fee_used[f["idx"]] = f
        notes += res["notes"]
    est = round(sum(res["fee_estimate"] for res in results), 2)
    # 分配：每条流水只给一行 —— 合约在场 → 按 id 排第一的持有行；已离场 → 按 id 排第一的、闭包含它的行
    by_row = {r["id"]: 0.0 for r in rows}
    owner_syms = {r["id"]: set() for r in rows}
    for res, r in zip(results, rows):
        owner_syms[r["id"]] |= res["syms"]
    for x in list(used.values()) + list(fee_used.values()):
        rid = next((r["id"] for r in rows if x["sym"] in held_by_row[r["id"]]), None) or \
            next(r["id"] for r in rows if x["sym"] in owner_syms[r["id"]])
        by_row[rid] += x["amt"]
    for res, r in zip(results, rows):
        by_row[r["id"]] -= res["fee_estimate"]
    by_row = {k: round(v, 2) for k, v in by_row.items()}
    total = round(sum(x["amt"] for x in used.values()) + sum(f["amt"] for f in fee_used.values()) - est, 2)
    if abs(sum(by_row.values()) - total) > 0.005:                        # 现金守恒：分配之和 = 唯一计数总额
        out["problems"].append(f"现金守恒失败：分配合计 {sum(by_row.values()):.2f} ≠ 总额 {total:.2f}")
        return out
    linked = set().union(*(res["syms"] for res in results)) if results else set()
    starts = [res["start"] for res in results]
    out.update(campaign_cash=total, by_row=by_row, linked=sorted(linked), exited=sorted(linked - held),
               start=min(starts).isoformat() if starts else None,
               start_basis="anchor" if all(b == "anchor" for b in bases) else "bound",
               quality="verified" if all(b == "anchor" for b in bases) else "bound",
               independent_rows=sum(1 for x in tx if x["idx"] not in used),
               fee_estimate=est, pending_fee_rows=sum(len(res["pending"]) for res in results), notes=notes)
    out["inferred_starts"] = {r["id"]: res["inferred_start"].isoformat() for res, r in zip(results, rows)
                              if "inferred_start" in res}
    if est:
        out["notes"].append(f"{out['pending_fee_rows']} 笔成交的费用尚未入账：按模型 ${FEE_PER_CONTRACT}/张 × {est_qty} 张估计"
                            f" ${est:.2f}（入账后自动改用实际）")
    policy = dict(by_row)
    for r in rows:
        sv = _stage_view(stages.get(r["id"]), r, by_row, tx, fees, cover, est_qty,
                         next(res["start"] for res, rr in zip(results, rows) if rr["id"] == r["id"]))
        if sv is None:
            continue
        out["stages"][r["id"]] = sv
        if sv["problems"]:
            out["problems"] += [f"{r['id']} 新阶段：{p}" for p in sv["problems"]]
            out["quality"] = "incomplete"
            continue
        policy[r["id"]] = sv["policy_cash"]
    out["policy_cash_by_row"] = policy
    return out


def basis_text(res: dict, rid: str) -> str:
    """账本行 cash_basis 的人读口径（同一单元各行共用同一份归属结果）。"""
    q = {"verified": "本轮起点已登记", "bound": "本轮起点未登记 → 取可行起点里现金最少者（保守界）",
         "incomplete": "不完整"}[res["quality"]]
    ex = f"；含已离场腿 {'、'.join(res['exited'])}" if res["exited"] else ""
    st = res["stages"].get(rid)
    stage = (f"；政策预算用新阶段入场现金（内部估值 {st['policy_endpoint']} 端，不吃旧阶段利润）"
             if st and not st.get("problems") else "")
    return f"现金流水按本轮唯一分配（{VERSION}；{q}{ex}{stage}）"
