"""买方（借记）价差「100% 含费」提示规则的版本分派（纯分析，Codex 064 T1）。

为什么单独成模块：同一条模拟仓记录原先在两个入口用不同规则——模拟仓入口（scripts/paper_trades.advisory_view）尊重记录挂的
advisory_rule / advisory_effective_at，统一巡检（risk_watch.assess_spread）却对所有借记价差套当前 v2，于是已挂 v1 的仓位、
或 v2 尚未生效的仓位被巡检提前提醒。分派逻辑下沉到这里，两个入口共用；analyze 层不得反向 import scripts。

规则：
- v1 `debit-roi100-review-v1`（用户 2026-10-14、Codex 045）：含费盈利 ≥ 100% × 含费风险成本（L = 100·借记 + 往返费）。只给已挂 v1 的
  模拟仓复现，公式冻结。
- v2 `debit-roi100-review-v2-20261023`（用户 2026-10-23、Codex 063-B）：公式唯一来源 spread_econ.debit_roi_review。
- 没挂规则 → None（没有提示规则，不是「没事」也不是错误）；未知规则或结构不适用 → config_error（调用方显式上报）。
- 评估时刻早于生效时刻 → before_effective，不触发（回放/重建也不补造提示）。
- 报价超出同到期垂直价差的模型范围（可平仓净价 > 宽度）→ quote_out_of_range，不认证止盈（两版都适用；只是报价体检，不改公式）。
只提示、从不平仓。"""
from __future__ import annotations

import math
from datetime import datetime

from undertow.analyze import spread_econ as se

V1 = "debit-roi100-review-v1"
V2 = se.DEBIT_ROI_REVIEW["version"]
ADVISORY_RULES = {V1: {"roi_frac": 1.0, "structures": ("debit",), "type": "advisory", "status": "uncalibrated"},
                  V2: {"roi_frac": 1.0, "structures": ("debit",), "type": "advisory",
                       "status": "正式提醒政策（收益效果未校准）", "formula": "v2"}}
ADVISORY_CURRENT = {"debit": V2}


def _finite(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def _parse(x) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(x))
        return t if t.tzinfo else None
    except (TypeError, ValueError):
        return None


def advisory_triggered(pnl_usd, risk_cost_usd) -> bool:
    """提示条件的唯一判定（Codex 046 WP2）：含费盈利 P ≥ 含费风险成本 L，按分为单位比较，不先把 ROI 舍入再比。"""
    return _finite(pnl_usd, risk_cost_usd) and risk_cost_usd > 0 and round(pnl_usd, 2) >= round(risk_cost_usd, 2)


def effective_at(p: dict, rule: str) -> datetime | None:
    """规则生效时刻：记录字段优先；早于该字段的纠正从同一 exit_rule_corrected 事件取。"""
    return _parse(p.get("advisory_effective_at") or next(
        (x.get("effective_at") for x in reversed(p.get("events") or []) if x.get("action") == "exit_rule_corrected"
         and x.get("advisory_rule") == rule), None))


def quote_out_of_range(p: dict, val) -> bool:
    """平仓价值（借记为负：−可平仓净价）超出同到期垂直价差的模型范围：可平仓净价 > 宽度。缺失/0/负净价不算越界（分开处理）。"""
    if not _finite(val, p.get("k_sell"), p.get("k_buy")):
        return False
    return round(-val, 4) > round(abs(p["k_sell"] - p["k_buy"]), 4)


def advisory_view(p: dict, val, assume, at: datetime | None = None) -> dict | None:
    """→ {rule, roi, risk_cost_usd, max_roi, target_unreachable, triggered, before_effective, ...}；没挂提示规则 → None。
    val = 平仓价值（与 close_value 同号，借记为负）；估值缺失或有估值假设（某腿缺可成交价按 0 计）→ roi None、不触发。"""
    r = p.get("advisory_rule")
    if not r:
        return None
    cfg = ADVISORY_RULES.get(r)
    if not cfg or p.get("structure", "credit") not in cfg["structures"]:
        return {"rule": r, "config_error": f"提示规则 {r} 未知或不适用于 {p.get('structure', 'credit')} 结构", "triggered": False}
    e = se.vertical(side=p["side"], k_sell=p["k_sell"], k_buy=p["k_buy"], entry_credit=p["entry_credit"], qty=p["qty"],
                    fee_round_trip=p["fee_round_trip"])
    L, M = e.get("entry_net_payoff_max_loss"), e.get("entry_net_max_gain")
    eff = effective_at(p, r)
    out = {"rule": r, "roi": None, "risk_cost_usd": L, "max_roi": None, "target_unreachable": None, "triggered": False,
           "roi_frac": cfg["roi_frac"], "status": cfg["status"], "effective_at": p.get("advisory_effective_at"),
           "before_effective": bool(at is not None and eff is not None and at < eff)}
    if cfg.get("formula") == "v2":
        return {**out, **_v2(p, val, assume, out["before_effective"])}
    if not (_finite(L, M) and L > 0):
        return out
    out["max_roi"] = round(M / L, 4)
    out["target_unreachable"] = M / L < cfg["roi_frac"]
    if _finite(val) and assume is None:
        if quote_out_of_range(p, val):
            return {**out, "quote_status": "quote_out_of_range",
                    "why": "可平仓净价超过价差宽度（超出模型范围）：报价异常待复核，不认证止盈"}
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        out["roi"] = round(pnl / L, 4)
        out["pnl_usd"] = pnl
        out["triggered"] = advisory_triggered(pnl, cfg["roi_frac"] * L) and not out["before_effective"]
    return out


def _v2(p: dict, val, assume, before_effective: bool) -> dict:
    """v2：模拟仓的入场/平仓费就是往返费模型各一半（模拟成交本来按它记账）。估值有假设 → 当作报价缺失、不触发。"""
    fe = p.get("fee_round_trip")
    half = fe / 2 * p["qty"] if _finite(fe) and fe >= 0 else None
    ec = p.get("entry_credit")
    r = se.debit_roi_review(side=p["side"], k_sell=p["k_sell"], k_buy=p["k_buy"], cost=-ec if _finite(ec) else None,
                            qty=p["qty"], entry_fees=half, exit_fees=half,
                            value_exec=-val if _finite(val) and assume is None else None,
                            entry_fee_source="paper_model", exit_fee_source="paper_model")
    out = {"risk_cost_usd": r["invest_usd"], "max_roi": r["max_roi"], "target_unreachable": r["unreachable"],
           "roi": r["roi"], "pnl_usd": r["profit_usd"], "triggered": bool(r["triggered"]) and not before_effective,
           "review": r}
    if r["status"] == "quote_out_of_range":
        out.update(quote_status="quote_out_of_range", why=r["why"])
    return out
