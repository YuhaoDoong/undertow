"""实盘试行的资本与暂停状态（Codex 043 WP3）。纯函数；持久化在 undertow/collect/trial_store.py。

口径（Codex 043 推荐，见 risk_policy.POLICY_B_META）：
- 计划试行资本（约 $500）与实际到账是两个状态：funded_capital 为 None 时试行尚未开始。
- R = 当前可审计权益（含保守可估的未实现盈亏与费用）；估值不完整 → 不可判定，冻结新增（未知不是没有亏损）。
- 起始亏损比例 = 1 − 权益 / (到账资本 + 外部净入金)。入金调整分母，不洗掉亏损，也不自动解除暂停。
- 峰值回撤另算，只报告、不判定（阈值未决定）。
- 连续亏损按【完整】逻辑持仓的含费净实现结果计；部分平仓不提前计胜负；同一 campaign 的分批照样各算一笔。
- 暂停 latch：亏损比例 ≥ 50% 或连续 4 笔亏损 → 锁定；重启、刷新、入金都不解除，只有用户明确决定（resume，记原话）才解除。
  暂停只冻结新增；已有仓位按原计划管理，不自动平仓、撤单、换档。
一次亏满 $300（R=$500）时触发时已亏 60%；两次止损 $200 已亏 80%——50% 不是保本线。
"""
from __future__ import annotations

import math

SCHEMA = 1


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def new_state(*, trial_id: str, planned_capital: float, policy_version: str, created_at: str) -> dict:
    return {"schema": SCHEMA, "trial_id": trial_id, "policy_version": policy_version, "created_at": created_at,
            "planned_capital": planned_capital, "funded_capital": None, "funded_at": None,
            "external_flows": [], "equity_history": [], "closed_trades": {},
            "pause_latched": False, "pause_reason": None, "paused_at": None, "resume_log": [], "counter_reset_at": None}


def record_funding(state: dict, *, amount: float, at: str, note: str = "") -> dict:
    """首次到账 → funded_capital；之后的到账记为外部入金（调整分母，不解除暂停）。"""
    if not (_num(amount) and amount > 0):
        raise ValueError("入金金额必须为正的有限数")
    s = dict(state)
    if s["funded_capital"] is None:
        s.update(funded_capital=float(amount), funded_at=at)
    else:
        s["external_flows"] = s["external_flows"] + [{"at": at, "amount": float(amount), "note": note}]
    return s


def record_trade(state: dict, *, trade_id: str, net_pnl_usd, closed_at: str, complete: bool) -> dict:
    """一笔逻辑持仓的含费净实现结果。complete=False（部分平仓）只记录，不计入连续亏损；同 id 再次导入以最新为准（幂等）。"""
    s = dict(state)
    s["closed_trades"] = {**s["closed_trades"],
                          trade_id: {"net_pnl_usd": net_pnl_usd, "closed_at": closed_at, "complete": bool(complete)}}
    return s


def consecutive_losses(state: dict) -> int | None:
    """从最近一笔往前数连续亏损的完整交易笔数；最近的完整交易里有盈亏未知 → None（不可判定）。"""
    rows = sorted((t for t in state["closed_trades"].values() if t["complete"]
                   and (state.get("counter_reset_at") is None or t["closed_at"] > state["counter_reset_at"])),
                  key=lambda t: t["closed_at"])
    n = 0
    for t in reversed(rows):
        if not _num(t["net_pnl_usd"]):
            return None
        if t["net_pnl_usd"] < 0:
            n += 1
        else:
            break
    return n


def evaluate(state: dict, *, equity, equity_complete: bool, asof: str, policy: dict) -> dict:
    """计算当前资本视图，并在越线时把 latch 置上（返回新 state 与视图）。不在这里解除 latch。"""
    s = dict(state)
    view = {"asof": asof, "trial_id": s["trial_id"], "policy_version": policy["version"], "funded_capital": s["funded_capital"],
            "net_external_flows": round(sum(f["amount"] for f in s["external_flows"]), 2), "current_equity": equity,
            "equity_basis": "可审计权益（含保守估计的未实现盈亏与费用）", "initial_capital_loss_frac": None,
            "peak_drawdown_frac": None, "consecutive_losses": None, "determinable": False, "pause": None}
    if s["funded_capital"] is None:
        view["note"] = "试行资本尚未到账：计划金额不是到账事实"
    elif not (equity_complete and _num(equity)):
        view["note"] = "估值不完整/权益未知 → 不可判定，冻结新增（未知不是没有亏损）"
    else:
        base = s["funded_capital"] + view["net_external_flows"]
        loss = 1 - equity / base if base > 0 else None
        hist = s["equity_history"] + [{"asof": asof, "equity": equity, "basis": base}]
        s["equity_history"] = hist[-500:]
        peak = max(h["equity"] - (h["basis"] - s["funded_capital"]) for h in hist)    # 扣掉入金后的峰值权益
        adj = equity - view["net_external_flows"]
        cl = consecutive_losses(s)
        view.update(initial_capital_loss_frac=None if loss is None else round(loss, 4),
                    peak_drawdown_frac=round(1 - adj / peak, 4) if peak > 0 else None, consecutive_losses=cl,
                    determinable=loss is not None and cl is not None)
        trig = []
        if loss is not None and loss >= policy["pause"]["drawdown_frac"]:
            trig.append(f"起始资本亏损 {loss:.0%} ≥ {policy['pause']['drawdown_frac']:.0%}")
        if cl is not None and cl >= policy["pause"]["consecutive_losses"]:
            trig.append(f"连续 {cl} 笔完整交易亏损")
        if trig and not s["pause_latched"]:
            s.update(pause_latched=True, pause_reason="；".join(trig), paused_at=asof)
    view["pause"] = {"latched": s["pause_latched"], "reason": s["pause_reason"], "paused_at": s["paused_at"]} \
        if (s["pause_latched"] or view["determinable"]) else None              # 未锁且不可判定 → 未知
    return s, view


def resume(state: dict, *, user_note: str, at: str) -> dict:
    """只有用户明确决定才解除暂停；记原话与时间；连续亏损从此刻重新计数。AI 不得自行调用（CLI 要求原话）。"""
    if not (isinstance(user_note, str) and user_note.strip()):
        raise ValueError("解除暂停需要用户原话")
    s = dict(state)
    s["resume_log"] = s["resume_log"] + [{"at": at, "user_note": user_note, "was_reason": s["pause_reason"]}]
    s.update(pause_latched=False, pause_reason=None, paused_at=None, counter_reset_at=at)
    return s


def validate(obj) -> str | None:
    """结构校验：不合格 → 原因（调用方隔离坏文件、当场告知）。"""
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
        return "schema 不符"
    for k, t in (("trial_id", str), ("pause_latched", bool), ("external_flows", list), ("closed_trades", dict),
                 ("equity_history", list), ("resume_log", list)):
        if not isinstance(obj.get(k), t):
            return f"字段 {k} 缺失或类型错误"
    return None
