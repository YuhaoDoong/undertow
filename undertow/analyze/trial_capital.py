"""实盘试行的资本与暂停状态（Codex 043 WP3；Codex 044 包 B/C 修订）。纯函数；持久化在 undertow/collect/trial_store.py。

口径（Codex 043 推荐，见 risk_policy.POLICY_B_META）：
- 计划试行资本（约 $500）与实际到账是两个状态：funded_capital 为 None 时试行尚未开始。
- R = 当前权益。现在取长桥 net_assets（券商口径），【未】独立核验保守 bid/ask 与全部费用；估值不完整 → 不可判定，
  冻结新增（未知不是没有亏损）。
- 起始资本亏损（暂停判据）= 1 − (当前权益 − 外部净现金流) / 起始到账资本。
  Codex 044 P1-4：旧式 1 − 权益/(到账 + 入金) 让「先亏 300、再入金 300、首次核查」显示 37.5% 而不暂停，
  剔除入金后其实仍亏 60%。入金/转出只从权益里剔除，不扩大分母；累计投入亏损率另列，只报告、不判定。
- 峰值回撤另算，只报告、不判定（阈值未决定）。
- 连续亏损按【完整】逻辑持仓的含费净实现结果计；部分平仓不提前计胜负；同一 campaign 的分批照样各算一笔。
  Codex 044 P1-3：结果没有来源时空字典不能当「0 笔」。trade_coverage.through = 「截至此刻所有已平逻辑交易都已登记」
  的时间点（到账时为起点；之后由用户核对后登记）。没有 coverage、或券商在 coverage 之后还有成交 → 连续亏损【不可判定】，冻结新增。
- 暂停 latch：亏损比例 ≥ 50% 或连续 4 笔亏损 → 锁定；重启、刷新、入金都不解除，只有用户明确决定（resume，记原话）才解除。
  resume 沿用原起始基准：若仍在暂停线以下，下一次核查会立即重锁（不清零亏损）。新风险阶段（新基准）需用户另行明确决定，
  尚未实现，不在这里隐含重置。
  暂停只冻结新增；已有仓位按原计划管理，不自动平仓、撤单、换档。
一次亏满 $300（R=$500）时触发时已亏 60%；两次止损 $200 已亏 80%——50% 不是保本线。
所有时间戳都是带时区的 ISO 8601，按时间比较（不按字符串）。
"""
from __future__ import annotations

import math
from datetime import datetime

SCHEMA = 2
EQUITY_BASIS = "长桥 net_assets（券商口径；未独立核验保守 bid/ask 与全部费用）"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def ts(s) -> datetime | None:
    """带时区的 ISO 时间 → datetime；不带时区/格式不对 → None（调用方据此拒绝）。"""
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def _need_ts(s, what: str) -> None:
    if ts(s) is None:
        raise ValueError(f"{what} 必须是带时区的 ISO 时间：{s!r}")


def new_state(*, trial_id: str, planned_capital: float, policy_version: str, created_at: str) -> dict:
    _need_ts(created_at, "created_at")
    return {"schema": SCHEMA, "rev": 0, "trial_id": trial_id, "policy_version": policy_version, "created_at": created_at,
            "planned_capital": float(planned_capital), "funded_capital": None, "funded_at": None,
            "external_flows": [], "equity_history": [], "closed_trades": {},
            "trade_coverage": {"through": None, "verified_at": None, "source": None, "note": ""},
            "pause_latched": False, "pause_reason": None, "paused_at": None, "resume_log": [], "counter_reset_at": None}


def record_funding(state: dict, *, amount: float, at: str, note: str = "") -> dict:
    """首次到账 → funded_capital（必须为正），并把交易结果覆盖起点设为到账时刻（试行开始前没有试行交易）。
    之后的到账/转出记为外部现金流（正 = 入金、负 = 转出）：从权益里剔除，不洗掉亏损，也不解除暂停。"""
    _need_ts(at, "到账时间")
    if not (_num(amount) and amount != 0):
        raise ValueError("金额必须为非零有限数")
    s = dict(state)
    if s["funded_capital"] is None:
        if amount <= 0:
            raise ValueError("首次到账必须为正")
        s.update(funded_capital=float(amount), funded_at=at,
                 trade_coverage={"through": at, "verified_at": at, "source": "到账起点（试行开始，尚无试行交易）", "note": note})
    else:
        s["external_flows"] = s["external_flows"] + [{"at": at, "amount": float(amount), "note": note}]
    return s


def record_trade(state: dict, *, trade_id: str, net_pnl_usd, closed_at: str, complete: bool,
                 source: str = "", note: str = "", revise: bool = False) -> dict:
    """一笔逻辑持仓的含费净实现结果。complete=False（部分平仓）只记录，不计入连续亏损。
    同 id：内容完全一致 → 不变（重复导入幂等）；原记录未完结 → 由新记录替换（部分平仓随后完结）；
    原记录已完结且内容不同 → 冲突，拒绝（除非 revise=True，旧值保留在 revisions 里）。"""
    _need_ts(closed_at, "closed_at")
    if not (isinstance(trade_id, str) and trade_id.strip()):
        raise ValueError("交易 id 不能为空")
    if not (net_pnl_usd is None or _num(net_pnl_usd)):
        raise ValueError("净盈亏必须是有限数或 None（未知）")
    row = {"net_pnl_usd": None if net_pnl_usd is None else float(net_pnl_usd), "closed_at": closed_at,
           "complete": bool(complete), "source": source, "note": note}
    s = dict(state)
    old = s["closed_trades"].get(trade_id)
    if old is not None:
        same = {k: old.get(k) for k in row} == row
        if same:
            return s
        if old["complete"] and not revise:
            raise ValueError(f"交易 {trade_id} 已登记为完结且内容不同：要改请明确 revise 并写原因")
        row["revisions"] = old.get("revisions", []) + [{k: old.get(k) for k in ("net_pnl_usd", "closed_at", "complete", "source", "note")}]
    s["closed_trades"] = {**s["closed_trades"], trade_id: row}
    return s


def verify_trades(state: dict, *, through: str, verified_at: str, source: str, note: str) -> dict:
    """登记「截至 through，所有已平逻辑交易都已登记」（人工核对或程序同步）。只能往后推，不能回退。"""
    _need_ts(through, "through"); _need_ts(verified_at, "verified_at")
    if not (isinstance(note, str) and note.strip()):
        raise ValueError("核对登记需要说明（谁、对照了什么）")
    cur = ts(state["trade_coverage"]["through"])
    if cur is not None and ts(through) < cur:
        raise ValueError("覆盖时间不能回退")
    s = dict(state)
    s["trade_coverage"] = {"through": through, "verified_at": verified_at, "source": source, "note": note}
    return s


def consecutive_losses(state: dict, *, latest_fill_at=..., ) -> tuple[int | None, str]:
    """→ (从最近一笔往前数连续亏损的完整交易笔数, 不可判定原因)。

    latest_fill_at：券商成交记录里 coverage 之后的最新成交时间；None = 已核查、coverage 之后无成交；
    缺省（...）= 没有核查成交记录 → 不可判定。最近的完整交易里有盈亏未知 → 不可判定。"""
    cov = ts(state["trade_coverage"]["through"])
    if cov is None:
        return None, "交易结果尚无覆盖起点（试行未到账或未登记）"
    if latest_fill_at is ...:
        return None, "未核查券商成交记录 → 连续亏损不可判定"
    if latest_fill_at is not None and ts(latest_fill_at) is not None and ts(latest_fill_at) > cov:
        return None, (f"券商在结果覆盖时点 {state['trade_coverage']['through']} 之后还有成交（最新 {latest_fill_at}）"
                      "：已平交易结果未登记/未核对 → 连续亏损不可判定（undertow trial trade / verify-trades）")
    reset = ts(state.get("counter_reset_at"))
    rows = sorted((t for t in state["closed_trades"].values() if t["complete"]
                   and (reset is None or ts(t["closed_at"]) > reset)), key=lambda t: ts(t["closed_at"]))
    n = 0
    for t in reversed(rows):
        if not _num(t["net_pnl_usd"]):
            return None, "最近的完整交易里有盈亏未知"
        if t["net_pnl_usd"] < 0:
            n += 1
        else:
            break
    return n, ""


def evaluate(state: dict, *, equity, equity_complete: bool, asof: str, policy: dict, latest_fill_at=...) -> tuple[dict, dict]:
    """计算当前资本视图，并在越线时把 latch 置上（返回新 state 与视图）。从不解除 latch。
    asof 早于已记录的最新观察 → 只计算、不写入权益历史（旧观察不覆盖新观察；越线照样锁，更保守）。"""
    _need_ts(asof, "asof")
    s = dict(state)
    flows = round(sum(f["amount"] for f in s["external_flows"]), 2)
    view = {"asof": asof, "trial_id": s["trial_id"], "policy_version": policy["version"], "funded_capital": s["funded_capital"],
            "net_external_flows": flows, "current_equity": equity, "equity_basis": EQUITY_BASIS,
            "initial_capital_loss_frac": None, "cumulative_invested_loss_frac": None, "peak_drawdown_frac": None,
            "consecutive_losses": None, "consecutive_losses_note": "", "determinable": False, "pause": None,
            "trade_coverage": s["trade_coverage"], "stale_observation": False}
    if s["funded_capital"] is None:
        view["note"] = "试行资本尚未到账：计划金额不是到账事实"
    elif not (equity_complete and _num(equity)):
        view["note"] = "估值不完整/权益未知 → 不可判定，冻结新增（未知不是没有亏损）"
    else:
        F = s["funded_capital"]
        adj = equity - flows                                         # 剔除外部现金流后的权益
        loss = 1 - adj / F
        cum = 1 - equity / (F + flows) if F + flows > 0 else None
        last = s["equity_history"][-1] if s["equity_history"] else None
        stale = last is not None and ts(asof) < ts(last["asof"])
        hist = s["equity_history"] + ([] if stale else [{"asof": asof, "equity": equity, "net_flows": flows}])
        if not stale:
            s["equity_history"] = hist[-500:]
        peak = max([h["equity"] - h["net_flows"] for h in hist] + [F])
        cl, cl_note = consecutive_losses(s, latest_fill_at=latest_fill_at)
        view.update(initial_capital_loss_frac=round(loss, 4), cumulative_invested_loss_frac=None if cum is None else round(cum, 4),
                    peak_drawdown_frac=round(1 - adj / peak, 4) if peak > 0 else None, consecutive_losses=cl,
                    consecutive_losses_note=cl_note, determinable=cl is not None, stale_observation=stale)
        trig = []
        if loss >= policy["pause"]["drawdown_frac"]:
            trig.append(f"起始资本亏损 {loss:.0%} ≥ {policy['pause']['drawdown_frac']:.0%}（已剔除外部现金流）")
        if cl is not None and cl >= policy["pause"]["consecutive_losses"]:
            trig.append(f"连续 {cl} 笔完整交易亏损")
        if trig and not s["pause_latched"]:
            s.update(pause_latched=True, pause_reason="；".join(trig), paused_at=asof)
        if cl is None:
            view["note"] = cl_note + " → 冻结新增"
    view["pause"] = {"latched": s["pause_latched"], "reason": s["pause_reason"], "paused_at": s["paused_at"]} \
        if (s["pause_latched"] or view["determinable"]) else None              # 未锁且不可判定 → 未知
    return s, view


def resume(state: dict, *, user_note: str, at: str) -> dict:
    """只有用户明确决定才解除暂停；记原话与时间；连续亏损从此刻重新计数。AI 不得自行调用（CLI 要求原话）。
    起始资本基准不变：亏损仍 ≥ 暂停线时下一次核查会立即重锁（见 relock_warning）。"""
    _need_ts(at, "resume 时间")
    if not (isinstance(user_note, str) and user_note.strip()):
        raise ValueError("解除暂停需要用户原话")
    s = dict(state)
    s["resume_log"] = s["resume_log"] + [{"at": at, "user_note": user_note, "was_reason": s["pause_reason"]}]
    s.update(pause_latched=False, pause_reason=None, paused_at=None, counter_reset_at=at)
    return s


def relock_warning(state: dict, policy: dict) -> str:
    """按最近一次权益观察，resume 之后是否会马上重锁（沿用原起始基准，不清零亏损）。"""
    if state["funded_capital"] is None or not state["equity_history"]:
        return "没有权益观察：下一次核查时按原起始基准重新判定"
    h = state["equity_history"][-1]
    loss = 1 - (h["equity"] - h["net_flows"]) / state["funded_capital"]
    if loss >= policy["pause"]["drawdown_frac"]:
        return (f"⚠️ 最近一次观察（{h['asof']}）起始资本亏损 {loss:.0%} 仍 ≥ {policy['pause']['drawdown_frac']:.0%}："
                "沿用原起始基准，下一次核查会立即重新暂停。要以新基准开始新的风险阶段，需要用户另行明确决定（尚未实现）。")
    return ""


_TOP = (("trial_id", str), ("policy_version", str), ("created_at", str), ("pause_latched", bool),
        ("external_flows", list), ("equity_history", list), ("closed_trades", dict), ("trade_coverage", dict),
        ("resume_log", list))


def validate(obj) -> str | None:
    """深校验：不合格 → 原因（调用方隔离坏文件或拒绝写入、当场告知）。Codex 044 P2-7：旧版只看少数顶层容器，
    删掉 funded_capital 仍放行、到 evaluate 才 KeyError。"""
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
        return "schema 不符"
    if not (isinstance(obj.get("rev"), int) and not isinstance(obj.get("rev"), bool) and obj["rev"] >= 0):
        return "字段 rev 缺失或类型错误"
    for k, t in _TOP:
        if not isinstance(obj.get(k), t):
            return f"字段 {k} 缺失或类型错误"
    if not obj["trial_id"].strip():
        return "trial_id 为空"
    if ts(obj["created_at"]) is None:
        return "created_at 不是带时区时间"
    if not (_num(obj.get("planned_capital")) and obj["planned_capital"] > 0):
        return "planned_capital 缺失或非正"
    if "funded_capital" not in obj or "funded_at" not in obj:
        return "缺 funded_capital / funded_at"
    fc, fa = obj["funded_capital"], obj["funded_at"]
    if fc is None:
        if fa is not None or obj["external_flows"]:
            return "未到账却有到账时间或外部现金流"
    elif not (_num(fc) and fc > 0 and ts(fa) is not None):
        return "funded_capital/funded_at 不合格"
    for f in obj["external_flows"]:
        if not (isinstance(f, dict) and ts(f.get("at")) and _num(f.get("amount")) and f["amount"] != 0):
            return "外部现金流记录不合格"
    for h in obj["equity_history"]:
        if not (isinstance(h, dict) and ts(h.get("asof")) and _num(h.get("equity")) and _num(h.get("net_flows"))):
            return "权益历史记录不合格"
    for tid, t in obj["closed_trades"].items():
        if not (isinstance(t, dict) and ts(t.get("closed_at")) and isinstance(t.get("complete"), bool)
                and (t.get("net_pnl_usd") is None or _num(t.get("net_pnl_usd")))):
            return f"交易结果 {tid} 不合格"
    cov = obj["trade_coverage"]
    if set(("through", "verified_at", "source", "note")) - set(cov):
        return "trade_coverage 缺字段"
    if cov["through"] is not None and ts(cov["through"]) is None:
        return "trade_coverage.through 不是带时区时间"
    if obj["pause_latched"] and not (isinstance(obj.get("pause_reason"), str) and ts(obj.get("paused_at"))):
        return "已暂停却缺原因或时间"
    if not obj["pause_latched"] and (obj.get("pause_reason") is not None or obj.get("paused_at") is not None):
        return "未暂停却带暂停原因/时间"
    for r in obj["resume_log"]:
        if not (isinstance(r, dict) and ts(r.get("at")) and isinstance(r.get("user_note"), str) and r["user_note"].strip()):
            return "解除记录不合格"
    if obj.get("counter_reset_at") is not None and ts(obj["counter_reset_at"]) is None:
        return "counter_reset_at 不是带时区时间"
    return None
