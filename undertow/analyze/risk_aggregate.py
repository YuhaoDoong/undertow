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


def _total(values: list[tuple[str, float | None]]) -> dict:
    known = [v for _, v in values if v is not None]
    missing = [name for name, v in values if v is None]
    return {"value": (sum(known) if not missing else None), "known_part": sum(known) if known else 0.0,
            "complete": not missing, "missing": missing}


def aggregate(review, capital, *, asof, policy: dict = rp.POLICY) -> dict:
    net = getattr(capital, "net_assets", None) if capital is not None else None
    items = []
    for g in getattr(review, "groups", []):
        for c in g.combos:
            status, ml = _classify(c)
            from undertow.analyze.healthcheck import stop_risk
            sr = stop_risk(c) if status == "known" else None
            items.append({"group": g.underlying, "label": c.label, "status": status, "max_loss": ml,
                          "stop_loss": sr, "occupancy": c.capital_at_risk})
    unmapped = [getattr(lg, "name", "?") for lg in getattr(review, "unmapped", [])]

    def name(it):
        return f"{it['group']}·{it['label']}"
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

    # 单笔限额：每项 pass / fail / incomplete
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
            "items": items, "unmapped": unmapped, "totals": totals, "greeks": greeks,
            "checks": checks, "overall": overall,
            "account_limit": ("未设定：全账户合计只报告、不判定" if policy.get("account_max_loss_frac") is None
                              else f"≤{policy['account_max_loss_frac']:.0%}")}


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
