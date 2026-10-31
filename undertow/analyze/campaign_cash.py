"""同标的风险单元的现金归属（Codex 063 N1、064 §5、067 P0）—— 纯函数，无 I/O。

旧口径（`risk_aggregate.apply_cash_ledger`，已删除）逐账本行按「行内各腿合约代码」取全部流水：
  1. 只认【仍在场】合约 → 换腿时已离场腿的真实现金被丢掉（合成例：卖出旧腿的收入不在任何一行），
     risk-unit 因此高估同一单元的最大亏损，单笔/类别合计误判 fail，巡检推假告警。
  2. 两行共用中间行权价时同一条流水被两行各认领一次（Codex 063 反例：真实 −200 被算成 +400，蝶式风险 200 → 0）。
  3. 「含同合约更早轮次」把独立已了结交易的现金当成本轮抵扣；缺 balance 当 0；90 天窗口不证明覆盖建仓起点。

v1（campaign-cash-v1-20261028）用「同秒成交传递闭包」认定换腿离场的旧腿，未登记起点时取「各行可行起点里现金最少者」。
Codex 067 用合成反例推翻了两点（`tests/test_campaign_cash.py` 的 067 回归）：
  a. 旧保护腿分开成交（与在场腿不同秒）→ 闭包连不上 → 它的净支出被丢掉，现金偏高、最大亏损偏低，却标 verified；
     起点登记只证明起点，不证明起点之后的相关流水都找齐了。
  b. 各行各取最少现金再取事件并集 ≠ 联合归属里的最小值：某行为凑最少把同代码早轮的净利润并入，合成例报 bound −300、真实 −600。
  根因：现金流水没有张数、没有订单号，同秒只是组合单的代理；凭它既不能证明成员齐全，也不能证明哪个起点覆盖了全部在场张数。

本模块 v2 的口径：
- **成员 = 私有登记（已人工核对）**：每个在场行必须在私有计划登记 `campaign_start`（本轮起点）；换腿离场的旧腿登记在
  `campaign_members`（在场腿自动是成员）；起点之后同标的、但不属于本轮的合约登记在 `campaign_independent`。
  一条成交属于行 r ⇔ 合约 ∈ r 的成员 且 时刻 ≥ r 的起点。
- **说不清就不完整**：任一行没登记起点，或起点之后有同标的成交既不是任何行的成员、也没登记为独立 → incomplete。
  同秒闭包只作为「候选」写进提示（帮人去登记），不再决定成员，更不升级 verified。没有「保守界」档：v1 的保守界被证伪了。
- **能力边界（源无张数）**：同一成员合约在起点之后若另有一次与本轮无关的往返，本模块识别不了 —— 这部分依赖登记时的人工核对。
  用成交（order executions，带张数与订单号）证明成员完整，留待后续版本，见 `MEMBERSHIP_ASSUMPTION`。
- **事件**：每条流水只分配一次（按行对象计，不按 (代码, 时刻, 金额) 去重——源没有事件 id，同一秒同代码同金额的两条
  是两笔真实分笔成交；若券商重复推送同一条，本模块无法识别，只计数报告）。
  多行共同认领的流水：没有新阶段时只影响展示用的逐行分配（按 id 字典序给第一行；单元合计与政策合计不随 id 改变）；
  有新阶段时逐行分配会改变政策现金 → incomplete。
- **费用**：费用次日入账。一条费用行覆盖同合约「上一条费用行之后到它之前」的成交；覆盖的成交都不是本轮成员 → 不计；
  部分是 → 全额计入（费用为负，保守）。成员成交之后还没有费用行 → 按模型估计计入（标估计）。
- **窗口**：在场合约在窗口内没有任何成交、登记起点早于窗口 → 不能证明覆盖 → incomplete。

三把尺子（Codex 064 §5，不混用、不相加）：
  1. campaign 累计：本轮全部实际现金 + 剩余腿到期 payoff − 未来费用（本模块给现金，`net_risk.unit_risk` 给风险）。
  2. 新阶段入场：私有计划登记 `stage`（起点 + 沿用腿的内部估值区间）→ 新阶段现金 = 起点后新腿的真实现金 + 沿用腿按
     内部价的虚拟划转；旧阶段参考 = 本轮现金 − 新阶段现金，两者之和恒等于真实总账。虚拟划转不产生费用、不写进现金。
     政策预算取「逐腿最差端」：虚拟划转对每条沿用腿的价格是线性的，盒子 [low, high]^n 上的最小值在各腿各自的端点取到
     —— 沿用空头取 low、沿用多头取 high（Codex 067：low/ref/high 三个同步端点在多空混合时不一定覆盖最差组合）。
     同一合约在一个阶段里只能登记一次；单元内各阶段沿用的张数合计不得超过在场张数；沿用腿须在新阶段起点前已是本轮成员。
  3. 从现在起：`net_risk.hold_risk`（需要实时报价，在命令层算）。
政策 B 用哪一把：登记了新阶段 → 2 的逐腿最差端（新交易从成立时点算，不吃旧阶段利润，Codex 063 N1 第 4 条）；否则 → 1。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

VERSION = "campaign-cash-v2-20261031"
SOURCE = "Codex 063 N1（现金事件唯一分配、来源质量）+ 064 §5（换腿三把尺子）+ 067 P0（成员须登记、阶段去重与逐腿最差端）"
MEMBERSHIP_BASIS = "registered-private"
MEMBERSHIP_ASSUMPTION = ("成员完整性依赖私有登记（campaign_start / campaign_members / campaign_independent）的人工核对；"
                         "现金流水无张数与订单号，起点之后同一成员合约若另有一次无关往返，本模块无法识别")
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


def norm_symbol(sym) -> str:
    """合约代码规范化：大写，补 .US 后缀（流水与持仓都带 .US；登记时可省略）。"""
    s = str(sym or "").strip().upper()
    return s if not s or "." in s else s + ".US"


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
        sym = norm_symbol(r.get("symbol"))
        if symbol_root(sym) != unit:
            continue
        t, amt = _ts(r.get("time")), _amount(r)
        if t is None or amt is None:
            probs.append(f"{sym}：流水{'时刻' if t is None else '金额'}缺失或无法解析（不当 0）")
            continue
        row = {"idx": i, "sym": sym, "t": t, "amt": amt}
        (fees if _is_fee(r) else tx).append(row)
    return tx, fees, probs


def _closure(held: set[str], tx: list[dict], start) -> set[str]:
    """候选提示（不决定成员）：从在场合约出发，经同一时刻（同秒）成交传递闭包；只看 start 及之后的成交。→ 关联合约。"""
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
    return syms


def _fee_coverage(fees: list[dict], tx: list[dict]) -> dict:
    """每条费用行覆盖的成交（同合约、上一条费用行之后到它之前）。→ {费用 idx: [成交行]}。"""
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


def _member_list(raw, what: str, probs: list) -> set[str]:
    """登记的合约清单 → 规范化代码集合；格式不对记问题（不猜）。"""
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) and parse_occ(norm_symbol(x)) for x in raw):
        probs.append(f"{what} 必须是期权合约代码列表")
        return set()
    return {norm_symbol(x) for x in raw}


def _stage_view(stage: dict | None, row: dict, alloc: dict, member_tx: list[dict], fees, cover, est_qty: int,
                start) -> dict | None:
    """新阶段入场现金（尺子 2）。stage = {"start": ISO, "inherited": [{symbol, qty, ref, low, high, basis?}]}。
    member_tx：本行的成员成交（起点之后、合约属于本行）。
    → {"cash": {"low","ref","high"}, "policy_cash", "real_cash", "old_stage": {...}, "problems": [...]}；没登记 → None。"""
    if not stage:
        return None
    probs = []
    st = _ts(stage.get("start"))
    if st is None:
        probs.append("新阶段起点不是带时区的 ISO 时刻")
    elif start is not None and st < start:
        probs.append("新阶段起点早于本轮起点")
    held = {norm_symbol(lg.get("symbol")): lg for lg in row["legs"]}
    inh = stage.get("inherited") or []
    if not inh:
        probs.append("新阶段没有登记沿用腿（没有沿用腿就不是换腿阶段，按本轮累计即可）")
    seen, dup = set(), set()
    virt = {"low": 0.0, "ref": 0.0, "high": 0.0}
    worst = 0.0
    for x in inh:
        sym = norm_symbol(x.get("symbol"))
        if sym in seen:
            dup.add(sym)
            continue
        seen.add(sym)
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
        if st is not None and not any(r["sym"] == sym and r["t"] < st for r in member_tx):
            probs.append(f"沿用腿 {sym} 在新阶段起点之前没有本轮成员成交（沿用腿须在该时点已存在）")
            continue
        for k, p in zip(("low", "ref", "high"), ps):
            virt[k] += -q * MULT * p                       # 沿用空头（q<0）= 新阶段按内部价「卖出」→ 正现金
        worst += min(-q * MULT * ps[0], -q * MULT * ps[2])  # 线性 → 盒子上的最小值在各腿各自的端点
    for sym in sorted(dup):
        probs.append(f"沿用腿 {sym} 在同一阶段登记了不止一次（重复沿用会重复计虚拟收入）")
    if probs:
        return {"problems": probs}
    new_syms = set(held) - seen
    early_new = sorted({r["sym"] for r in member_tx if r["sym"] in new_syms and r["t"] < st})
    if early_new:
        return {"problems": [f"新腿 {'、'.join(early_new)} 在新阶段起点之前已有本轮成交（不是新阶段的新腿，或起点登记错）"]}
    real_tx = [r for r in member_tx if r["sym"] in new_syms and r["t"] >= st]
    real_idx = {r["idx"] for r in real_tx}
    real_fees = [f for f in fees if f["sym"] in new_syms and any(r["idx"] in real_idx for r in cover.get(f["idx"], []))]
    covered = {r["idx"] for f in real_fees for r in cover.get(f["idx"], [])}
    est = round(sum(1 for r in real_tx if r["idx"] not in covered) * est_qty * FEE_PER_CONTRACT, 2)
    real = round(sum(r["amt"] for r in real_tx) + sum(f["amt"] for f in real_fees) - est, 2)
    cash = {k: round(real + v, 2) for k, v in virt.items()}
    policy = round(real + worst, 2)
    row_cash = alloc.get(row["id"])
    return {"start": stage.get("start"), "real_cash": real, "virtual": {k: round(v, 2) for k, v in virt.items()},
            "cash": cash, "policy_cash": policy, "policy_endpoint": "per-leg-worst",
            "policy_endpoint_text": "逐腿最差端（沿用空头取 low、沿用多头取 high）",
            "old_stage": None if row_cash is None else {k: round(row_cash - v, 2) for k, v in cash.items()},
            "old_stage_policy": None if row_cash is None else round(row_cash - policy, 2),
            "inherited": inh, "fee_estimate": est, "problems": []}


def unit_cash(unit: str, rows: list[dict], cash_rows: list[dict], *, window_start: str,
              anchors: dict | None = None, stages: dict | None = None, members: dict | None = None) -> dict:
    """一个风险单元的现金归属。rows：同一标的的在场账本行（各带 id、legs[symbol, qty]）。
    anchors：{行 id: campaign_start ISO}；stages：{行 id: stage}；
    members：{行 id: {"campaign_members": [已离场成员合约], "campaign_independent": [起点后不属本轮的同标的合约]}}
    （都来自私有计划，登记时人工核对）。
    → {"quality": verified|incomplete, "campaign_cash", "by_row", "policy_cash_by_row", "stages", ...}。"""
    anchors, stages, members = anchors or {}, stages or {}, members or {}
    out = {"version": VERSION, "unit": unit, "window_start": window_start, "quality": "incomplete",
           "membership_basis": MEMBERSHIP_BASIS, "assumptions": [MEMBERSHIP_ASSUMPTION],
           "campaign_cash": None, "by_row": {}, "policy_cash_by_row": {}, "start": None,
           "linked": [], "exited": [], "independent": [], "independent_rows": 0, "duplicate_keys": 0,
           "fee_estimate": 0.0, "pending_fee_rows": 0, "stages": {}, "candidates": {}, "problems": [], "notes": []}
    w0 = _ts(window_start)
    if w0 is None:
        out["problems"].append("窗口起点不是带时区的 ISO 时刻")
        return out
    tx, fees, probs = _parse_rows(unit, cash_rows)
    out["problems"] += probs
    tx = [r for r in tx if r["t"] >= w0]                 # 窗口以外的行不用（调用方按窗口取流水；这里再守一次）
    fees = [f for f in fees if f["t"] >= w0]
    rows = sorted(rows, key=lambda r: r["id"])
    held_by_row, mem_by_row, start_by_row = {}, {}, {}
    indep: set[str] = set()
    for r in rows:
        syms = {norm_symbol(lg.get("symbol")) for lg in r.get("legs") or []}
        if not r.get("legs") or "" in syms:
            out["problems"].append(f"{r['id']}：腿缺合约代码，无法对流水")
        held_by_row[r["id"]] = syms
        m = members.get(r["id"]) or {}
        mp: list = []
        extra = _member_list(m.get("campaign_members"), f"{r['id']}：campaign_members", mp)
        indep |= _member_list(m.get("campaign_independent"), f"{r['id']}：campaign_independent", mp)
        out["problems"] += mp
        mem_by_row[r["id"]] = syms | extra
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

    # 起点：必须登记（Codex 067：同秒闭包与「可行起点取最少」都不能证明成员完整或覆盖全部张数）
    for r in rows:
        a = anchors.get(r["id"])
        at = _ts(a) if a is not None else None
        h = held_by_row[r["id"]]
        if a is None:
            first = min((x["t"] for x in tx if x["sym"] in h), default=None)
            cand = sorted(_closure(h, tx, first) - h) if first else []
            out["candidates"][r["id"]] = {"earliest_held_trade": first.isoformat() if first else None,
                                          "same_second_linked": cand}
            out["problems"].append(f"{r['id']}：未登记本轮起点（campaign_start）→ 成员与起点都无法证明"
                                   + (f"；候选：在场合约最早成交 {first.isoformat()}" if first else "")
                                   + (f"，同秒关联 {'、'.join(cand)}（只作提示，须人工核对后登记）" if cand else ""))
            continue
        if at is None:
            out["problems"].append(f"{r['id']}：登记的本轮起点不是带时区的 ISO 时刻")
            continue
        if at < w0:
            out["problems"].append(f"{r['id']}：登记起点 {a} 早于流水窗口起点 → 窗口没覆盖整轮")
            continue
        missing = sorted(s for s in h if not any(x["sym"] == s and x["t"] >= at for x in tx))
        if missing:
            out["problems"].append(f"{r['id']}：登记起点 {a} 之后缺在场合约 {'、'.join(missing)} 的成交（起点登记晚了）")
            continue
        ghost = sorted(s for s in mem_by_row[r["id"]] - h if not any(x["sym"] == s and x["t"] >= at for x in tx))
        if ghost:
            out["problems"].append(f"{r['id']}：登记的离场成员 {'、'.join(ghost)} 在起点之后没有成交（登记错或窗口不全）")
            continue
        start_by_row[r["id"]] = at
    if out["problems"]:
        return out

    def owners(x):
        return [r["id"] for r in rows if x["sym"] in mem_by_row[r["id"]] and x["t"] >= start_by_row[r["id"]]]

    t0 = min(start_by_row.values()) if start_by_row else None
    member_tx, own = [], {}
    unexplained, independent = [], []
    for x in tx:
        o = owners(x)
        if o:
            member_tx.append(x); own[x["idx"]] = o
        elif t0 is not None and x["t"] >= t0:
            (independent if x["sym"] in indep else unexplained).append(x)
    if unexplained:
        syms = sorted({x["sym"] for x in unexplained})
        out["problems"].append(f"本轮起点之后有同标的成交既不是登记成员、也没登记为独立：{'、'.join(syms)}"
                               f"（{len(unexplained)} 条）→ 无法证明归属完整；核对后登记到 campaign_members 或 campaign_independent")
        return out
    shared = [x for x in member_tx if len(own[x["idx"]]) > 1]
    if shared and any(own_id in stages for x in shared for own_id in own[x["idx"]]):
        out["problems"].append("多行共同认领的流水（" + "、".join(sorted({x["sym"] for x in shared}))
                               + "）且其中有行登记了新阶段：逐行分配会改变政策现金，归属不唯一")
        return out

    member_idx = {x["idx"] for x in member_tx}
    fee_used, straddle, fee_owner = [], False, {}
    for f in fees:
        cov = cover.get(f["idx"], [])
        if not cov:
            if t0 is None or f["t"] >= t0:
                out["notes"].append(f"{f['sym']} {f['t']:%m-%d} 费用行找不到对应成交（窗口外或源异常），未计入")
            continue
        mine = [r for r in cov if r["idx"] in member_idx]
        if not mine:
            continue
        if len(mine) < len(cov):
            straddle = True                               # 跨起点 / 与独立交易合并收费：全额计入（费用为负，计入更保守）
        fee_used.append(f)
        fee_owner[f["idx"]] = sorted({o for r in mine for o in own[r["idx"]]})
    if straddle:
        out["notes"].append("有费用行同时覆盖本轮成员与非成员成交：全额计入（保守）")
    covered = {r["idx"] for f in fee_used for r in cover.get(f["idx"], [])}
    pending = [x for x in member_tx if x["idx"] not in covered]
    est = round(len(pending) * est_qty * FEE_PER_CONTRACT, 2)
    total = round(sum(x["amt"] for x in member_tx) + sum(f["amt"] for f in fee_used) - est, 2)

    # 逐行分配（展示用；单元合计与 id 无关）：多行认领 → 按 id 字典序第一行；未入账费用估计按成交归属行
    by_row = {r["id"]: 0.0 for r in rows}
    for x in member_tx:
        by_row[own[x["idx"]][0]] += x["amt"]
    for f in fee_used:
        by_row[fee_owner[f["idx"]][0]] += f["amt"]
    for x in pending:
        by_row[own[x["idx"]][0]] -= est_qty * FEE_PER_CONTRACT
    by_row = {k: round(v, 2) for k, v in by_row.items()}
    if abs(sum(by_row.values()) - total) > 0.005:                        # 现金守恒：分配之和 = 唯一计数总额
        out["problems"].append(f"现金守恒失败：分配合计 {sum(by_row.values()):.2f} ≠ 总额 {total:.2f}")
        return out
    linked = {x["sym"] for x in member_tx}
    out.update(campaign_cash=total, by_row=by_row, linked=sorted(linked), exited=sorted(linked - held),
               independent=sorted({x["sym"] for x in independent}), independent_rows=len(independent),
               start=t0.isoformat() if t0 else None, quality="verified",
               fee_estimate=est, pending_fee_rows=len(pending))
    if est:
        out["notes"].append(f"{len(pending)} 笔成交的费用尚未入账：按模型 ${FEE_PER_CONTRACT}/张 × {est_qty} 张估计"
                            f" ${est:.2f}（入账后自动改用实际）")
    # 单元内沿用张数对账（Codex 067）：沿用腿必须是本行在场腿且张数等于本行张数（_stage_view），而多行共认领的合约一旦
    # 有行登记新阶段就已 incomplete（上面）→ 每个被沿用的合约只属于一行，各阶段沿用张数合计不可能超过在场张数。
    policy = dict(by_row)
    for r in rows:
        mtx = [x for x in member_tx if r["id"] in own[x["idx"]]]
        sv = _stage_view(stages.get(r["id"]), r, by_row, mtx, fees, cover, est_qty, start_by_row[r["id"]])
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
    q = {"verified": "本轮起点与成员已登记（人工核对，源无张数）", "incomplete": "不完整"}.get(res["quality"], res["quality"])
    ex = f"；含已离场腿 {'、'.join(res['exited'])}" if res.get("exited") else ""
    st = res["stages"].get(rid)
    stage = (f"；政策预算用新阶段入场现金（{st['policy_endpoint_text']}，不吃旧阶段利润）"
             if st and not st.get("problems") else "")
    return f"现金流水按本轮唯一分配（{VERSION}；{q}{ex}{stage}）"
