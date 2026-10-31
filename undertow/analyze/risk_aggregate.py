"""账户风险汇总：分口径、带完整性（Codex 008 G03）。纯函数。

旧问题：实时体检把有符号的可平仓价值相加叫「总敞口」；集中度把算不出的风险折成 0；
组合 Greeks 只加已知腿。风险有几种【不能互相替代】的量，这里分开给，每项都附完整性：

  max_loss     最大亏损（跳空硬口径）。裸卖 call = 无上限；裸卖 put 按接货全额（保守有界）；
               日历/对角等跨期结构 = 算不出。
  stop_loss    按预设止损了结的情景损失（正常行情软口径，healthcheck.stop_risk）。
  occupancy    资金占用/风险资金（capital_at_risk）。
  greeks       有符号的净 Δ/Γ/Θ（不用 abs(sum) 冒充尾部风险）；缺腿 → None。

任何一项有未知或无上限成员 → 合计为 None，同时给出已知部分与缺失清单；限额结论只有
pass / fail / incomplete 三种，incomplete 永远不是通过。
"""
from __future__ import annotations

from undertow.analyze import risk_policy as rp


def _classify(c) -> tuple[str, float | None]:
    """(状态, 最大亏损)。状态：known / unbounded / unknown。"""
    if c.max_loss is not None:
        return "known", float(c.max_loss)
    if "卖call" in c.label and len(c.legs) == 1:
        return "unbounded", None
    if "卖put" in c.label and len(c.legs) == 1 and c.capital_at_risk is not None:
        return "known", float(c.capital_at_risk)          # 接货全额：保守上界
    return "unknown", None


def _leg_key(lg) -> str | None:
    """一条腿的合约身份：优先券商合约代码，其次 种类+行权价+到期；都没有 → None（身份不可知）。方向用数量符号。"""
    q = getattr(lg, "qty", None)
    sign = "-" if isinstance(q, (int, float)) and q < 0 else "+"
    sym = getattr(lg, "symbol", None)
    if isinstance(sym, str) and sym.strip():
        return sign + sym.strip()
    k, s, e = getattr(lg, "kind", None), getattr(lg, "strike", None), getattr(lg, "expiry", None)
    if k is None or s is None or e is None:
        return None
    return f"{sign}{k}{s:g}@{e}"


def _legs_and_cash(c) -> tuple[list | None, float | None, float | None]:
    """同标的净额用的腿明细与现金（Codex 061/062）：(腿, 净现金流, 未来费用)。
    现金按券商成本价 × 带符号数量 − 开仓费（模型）；券商成本价在部分减仓后会被改写，调用方能拿到现金流水时应
    用 apply_campaign_cash 覆盖。任一腿缺种类/行权价/到期/数量/成本 → (None, None, None)，该组合退回各笔口径。"""
    from undertow.analyze.healthcheck import FEE_PER_CONTRACT, CONTRACT_MULT
    legs, cash, n = [], 0.0, 0
    for lg in getattr(c, "legs", None) or []:
        k, q, cp = getattr(lg, "kind", None), getattr(lg, "qty", None), getattr(lg, "cost_price", None)
        if not (isinstance(q, (int, float)) and isinstance(cp, (int, float))) or q == 0:
            return None, None, None
        if k == "STOCK":
            legs.append({"kind": "STOCK", "strike": None, "expiry": None, "qty": q, "symbol": getattr(lg, "symbol", None)})
            cash -= q * cp
            continue
        s, e = getattr(lg, "strike", None), getattr(lg, "expiry", None)
        if k not in ("C", "P") or s is None or e is None:
            return None, None, None
        legs.append({"kind": k, "strike": float(s), "expiry": e.isoformat() if hasattr(e, "isoformat") else e,
                     "qty": q, "symbol": getattr(lg, "symbol", None)})
        cash -= q * CONTRACT_MULT * cp
        n += abs(q)
    if not legs:
        return None, None, None
    fee = n * FEE_PER_CONTRACT
    return legs, round(cash - fee, 2), round(fee, 2)


def apply_campaign_cash(book: list[dict], cash: dict | None) -> list[str]:
    """用现金流水按本轮唯一分配覆盖账本行的净现金流（Codex 063 N1；口径见 campaign_cash 模块文档）。原地修改，返回提示。

    cash = {"rows": 原样流水, "window_start": 带时区 ISO, "plans": {计划键: 计划}}；{"error": 原因} = 流水读取失败。
    同一标的的带腿行合成一份归属；归属不完整（缺金额、窗口没覆盖、起点登记不合格）或流水读不到 → 这些行 net_cash=None，
    预算报 incomplete（不再留券商成本价口径冒充已核：成本价在部分减仓后会被改写）。
    政策 B 用的 net_cash：登记了新阶段 → 新阶段入场现金的逐腿最差端；否则 → 本轮累计。
    成员来自私有计划 campaign_start / campaign_members / campaign_independent（campaign-cash v2，Codex 067 P0）。另存 campaign_cash / cash_detail 供展示。"""
    from undertow.analyze import campaign_cash as cc
    notes = []
    units: dict = {}
    for p in book:
        if p.get("legs") is not None:
            units.setdefault(rp.unit_key(p.get("underlying")), []).append(p)
    if not units or cash is None:
        return notes
    if cash.get("error"):
        for ps in units.values():
            for p in ps:
                p.update(net_cash=None, cash_basis=f"现金流水读取失败（{cash['error']}）→ 不可核", cash_detail=None)
        return [f"现金流水读取失败（{cash['error']}）→ 同标的净额现金未知，预算 incomplete"]
    plans = cash.get("plans") or {}
    for u, ps in units.items():
        anchors, stages, members = {}, {}, {}
        for p in ps:
            plan = plans.get(cc.plan_key_for_legs(u, p["legs"]) or "") or {}
            if plan.get("campaign_start"):
                anchors[p["id"]] = plan["campaign_start"]
            if plan.get("stage"):
                stages[p["id"]] = plan["stage"]
            members[p["id"]] = {k: plan.get(k) for k in ("campaign_members", "campaign_independent")}
        res = cc.unit_cash(u, ps, cash.get("rows") or [], window_start=cash["window_start"],
                           anchors=anchors, stages=stages, members=members)
        for p in ps:
            ok = res["quality"] != "incomplete"
            p.update(net_cash=res["policy_cash_by_row"].get(p["id"]) if ok else None,
                     campaign_cash=res["by_row"].get(p["id"]) if ok else None,
                     cash_basis=cc.basis_text(res, p["id"]), cash_detail=res)
        notes += [f"{u}：{x}" for x in res["problems"] + res["notes"]]
        if res.get("candidates"):
            notes.append(f"{u}：本轮起点/成员未登记 → 不完整；人工核对流水后用 `undertow riskwatch plan-set --key <键> "
                         f"--campaign-start <ISO> [--campaign-member <离场合约>…] [--campaign-independent <合约>…]` 登记")
    return notes


def combo_id(underlying, c) -> str | None:
    """逻辑持仓的稳定身份（Codex 044 P1-1）：标的 | 到期 | 结构 | 各腿合约与方向 | 组数。
    显示名（「熊市看涨价差(收权金)」）只是策略名，不含到期/腿，旧版拿它当 id，同标签不同到期的多笔被去重成一笔。
    券商只给同一合约的汇总数量：同合约不同批次在这里本来就合成一笔（按已知汇总风险报告，不假装能区分批次）。
    任一腿身份不可知 → None（下游 incomplete，不猜）。"""
    keys = [_leg_key(lg) for lg in getattr(c, "legs", None) or []]
    if not keys or any(k is None for k in keys):
        return None
    return f"{underlying}|{c.expiry_label}|{c.label}|{','.join(sorted(keys))}|x{c.qty}"


def _total(values: list[tuple[str, float | None]]) -> dict:
    known = [v for _, v in values if v is not None]
    missing = [name for name, v in values if v is None]
    return {"value": (sum(known) if not missing else None), "known_part": sum(known) if known else 0.0,
            "complete": not missing, "missing": missing}


_NET = object()


def aggregate(review, capital, *, asof, policy: dict = rp.POLICY_B, R=_NET, cash_input: dict | None = None) -> dict:
    """policy 必须显式指定版本（Codex 043 WP2）：实盘入口传 rp.LIVE_POLICY；POLICY_B / rp.POLICY（v1）只用于复现历史记录。
    R：额度计算用的预算权益（Codex 065：调用方传 r_budget 快照的 R_budget，未知传 None → incomplete）；
    不传 = 旧口径直接用 net_assets（只供历史复现）。占比列仍按净资产显示。
    cash_input：现金流水归属输入（见 apply_campaign_cash；Codex 063 N1）。实盘入口必传；None = 旧口径券商成本价（只供复现）。"""
    net = getattr(capital, "net_assets", None) if capital is not None else None
    Rv = net if R is _NET else R
    items = []
    for g in getattr(review, "groups", []):
        for c in g.combos:
            status, ml = _classify(c)
            from undertow.analyze.healthcheck import stop_risk_detail
            d = stop_risk_detail(c) if status == "known" else None
            legs, cash, ffee = _legs_and_cash(c)
            items.append({"id": combo_id(g.underlying, c), "group": g.underlying, "label": c.label,
                          "legs": legs, "net_cash": cash, "future_fees": ffee,
                          "cash_basis": "券商成本价 + 模型费用（未用现金流水核实）" if legs is not None else None,
                          "expiry": c.expiry_label, "status": status, "max_loss": ml,
                          "stop_loss": None if d is None else d["planned_stop_loss_usd"],
                          "payoff_max_loss_with_fees": None if d is None else d["payoff_max_loss_usd"],
                          "stop_exceeds_payoff_max_loss": None if d is None else d["stop_exceeds_payoff_max_loss"],
                          "occupancy": c.capital_at_risk})
    unmapped = [getattr(lg, "name", "?") for lg in getattr(review, "unmapped", [])]

    def name(it):                                            # 显示名：带到期，便于人读；身份用 it["id"]
        return f"{it['group']}·{it['label']}·{it['expiry']}"
    totals = {
        "max_loss": _total([(name(i), i["max_loss"]) for i in items] + [(u, None) for u in unmapped]),
        "stop_loss": _total([(name(i), i["stop_loss"]) for i in items] + [(u, None) for u in unmapped]),
        "occupancy": _total([(name(i), i["occupancy"]) for i in items] + [(u, None) for u in unmapped]),
    }
    totals["max_loss"]["unbounded"] = [name(i) for i in items if i["status"] == "unbounded"]
    greeks = {g.underlying: {"delta": g.net_delta, "gamma": getattr(g, "net_gamma", None),
                             "theta": getattr(g, "net_theta", None),
                             "incomplete": getattr(g, "incomplete", {})}
              for g in getattr(review, "groups", [])}

    book = book_of(items, unmapped)
    cash_notes = apply_campaign_cash(book, cash_input)
    if policy.get("profile_id"):                             # 政策 B：整笔含费金额、类别/全场合计、大仓数量
        b = rp.evaluate_book(book, {"equity": Rv}, policy)
        disp = {i["id"]: name(i) for i in items if i["id"]}
        for c in b["checks"]:                                # 人读：单笔检查显示「标的·策略·到期」，身份仍按 id 判
            if c.get("position") in disp:
                c["check"] = c["check"].replace(c["position"], disp[c["position"]], 1)
            elif str(c.get("position", "")).startswith("unit|"):   # 同标的整体风险单元（Codex 061/062）
                c["check"] = c["check"].replace(c["position"], f"{c['position'].split('|')[1]}·同标的整体", 1)
        checks = [{"item": c["check"], "limit": f"≤ ${c['limit']:,.0f}" if isinstance(c["limit"], (int, float)) and not
                   c["check"].startswith("大仓") else (f"≤ {c['limit']} 笔" if c["limit"] is not None else "—"),
                   "status": c["status"], "value": c["value"],
                   "pct": (c["value"] / net * 100 if (isinstance(c["value"], (int, float)) and net and net > 0
                                                       and not c["check"].startswith("大仓")) else None)}
                  for c in b["checks"]]
        return {"asof": str(asof), "policy_version": policy["version"], "net_assets": net, "R_budget": Rv,
                "items": items, "unmapped": unmapped, "totals": totals, "greeks": greeks, "book": book,
                "checks": checks, "overall": b["overall"], "cash_notes": cash_notes,
                "account_limit": f"全场合计 ≤ max(60%·R, $500) = ${rp.policy_limit(policy, policy['account'], Rv):,.0f}"
                                 if Rv is not None else "预算权益 R 未知"}
    # 单笔限额（v1，历史复现用）：每项 pass / fail / incomplete
    checks = []
    for it in items:
        for key, frac_key, val in (("最大亏损", "max_loss_frac", it["max_loss"]),
                                   ("止损情景", "max_stop_risk_frac", it["stop_loss"])):
            lim = policy[frac_key]
            if net is None or val is None:
                st = "incomplete"
            elif net <= 0:
                st = "fail" if val > 0 else "pass"
            else:
                st = "pass" if val <= lim * net else "fail"
            checks.append({"item": name(it), "limit": f"{key} ≤{lim:.0%}", "status": st,
                           "value": val, "pct": (val / net * 100 if (val is not None and net and net > 0) else None)})
    if unmapped:
        checks.append({"item": "、".join(unmapped), "limit": "无法映射的腿", "status": "incomplete",
                       "value": None, "pct": None})
    if net is None:
        checks.append({"item": "账户", "limit": "净资产", "status": "incomplete", "value": None, "pct": None})
    sts = {c["status"] for c in checks}
    overall = "fail" if "fail" in sts else ("incomplete" if ("incomplete" in sts or not checks) else "pass")
    return {"asof": str(asof), "policy_version": policy["version"], "net_assets": net,
            "items": items, "unmapped": unmapped, "totals": totals, "greeks": greeks, "book": book,
            "checks": checks, "overall": overall,
            "account_limit": ("未设定：全账户合计只报告、不判定" if policy.get("account_max_loss_frac") is None
                              else f"≤{policy['account_max_loss_frac']:.0%}")}


def book_of(items: list[dict], unmapped: list[str]) -> list[dict]:
    """预算/核查用的账本（唯一构造处；shadow exec 也读这里，不再自己拼 id）。无法映射的腿各成一条、金额未知。"""
    return [{"id": i["id"], "underlying": i["group"], "stop_loss": i["stop_loss"],
             "payoff_max_loss": i["payoff_max_loss_with_fees"],
             **({"legs": i["legs"], "net_cash": i["net_cash"], "future_fees": i["future_fees"],
                 "cash_basis": i["cash_basis"]} if i.get("legs") is not None else {})} for i in items] + \
           [{"id": f"unmapped|{u}", "underlying": u, "stop_loss": None, "payoff_max_loss": None} for u in unmapped]


def render_md(agg: dict) -> str:
    f = lambda v: "—" if v is None else f"${v:,.0f}"
    word = {"pass": "✅ 通过", "fail": "🔴 超限", "incomplete": "⚠️ 未完成核查（不是通过）"}[agg["overall"]]
    L = [f"## 🧮 风险汇总（{agg['policy_version']}） —— {word}", ""]
    for k, lab in (("max_loss", "最大亏损合计（跳空口径）"), ("stop_loss", "止损情景损失合计"),
                   ("occupancy", "资金占用合计")):
        t = agg["totals"][k]
        if t["complete"]:
            L.append(f"- {lab}：{f(t['value'])}")
        else:
            extra = f"；无上限：{'、'.join(t['unbounded'])}" if t.get("unbounded") else ""
            L.append(f"- {lab}：**未知**（已知部分 {f(t['known_part'])}，缺 {len(t['missing'])} 项：{'、'.join(t['missing'][:3])}{extra}）")
    L.append(f"- 全账户上限：{agg['account_limit']}")
    bad = [c for c in agg["checks"] if c["status"] != "pass"]
    for c in bad[:8]:
        pct = f"（{c['pct']:.0f}%）" if c["pct"] is not None else ""
        L.append(f"  - [{c['status']}] {c['item']} · {c['limit']}：{f(c['value'])}{pct}")
    L.append("")
    L.append("> 净清算价值（现在全平收回/付出的现金）不是风险；这里的数才用于限额。")
    return "\n".join(L)
