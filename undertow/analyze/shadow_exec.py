"""S05：账户风险预算账（纯计算，无 I/O）。与研究账严格分开。

研究账（data/history/shadow/，公开）保留全部机会，回答「墙/方向方案是否有增量」。
本账（data/account/shadow_exec/，gitignore）只回答：**按现行风控政策，这一组的理论风险预算过不过**。

Codex 009 N03 更正：旧版把结论写成「可执行 n 组」，但①三项到期处置/权限都未核实，②没有传入券商实际保证金与
购买力，③同日多个候选各自用同一份剩余额度（它们是互斥备选，不能相加），④跨日占用累计的是此前「被判可执行」的
假设仓位（不是实际选入）、按到期而非 v5 提前退出日释放、还可能与真实持仓重复。所以现在分成三个状态：
  budget_status    pass / fail / unknown —— 仅指 risk_policy 的理论风险预算；
  broker_status    unverified —— 实际保证金、单腿退出权限、到期处置均未核实（见 DISPOSITION）；
  selection_status independent_alternative —— 每个候选单独对照同一份剩余额度，互为备选，n 不可跨候选相加。
额度只扣本账户【真实】持仓；此前候选的假设占用单独列出（hypothetical_prior_occupancy），不参与 n 的计算。
"""
from __future__ import annotations

from datetime import date

from undertow.analyze import risk_policy as rp
from undertow.analyze import shadow as sh

VERSION = "shadow-exec-v2-20260926"
PRIMARY_ARM = "A"
DISPOSITION = {
    "short_only_permission": "未核实：组合单能否单独买回短腿取决于券商与入场方式",
    "itm_residual": "长桥条款：实值≥0.01 自动行权成正股；资金不足可能被强平 —— 小账户处置成本未知",
    "vertical_margin": "未核实：长桥组合保证金只列 covered call/put，垂直价差实际占用未知",
    "expiry_day_margin": "长桥条款：到期日 ET 14:00 起提高保证金要求",
}
SELECTION = "independent_alternative"


def cluster_of(inst: str) -> str:
    return next((c for c, ks in sh.CLUSTERS.items() if inst in ks), inst)


def candidate(row: dict, leg: dict, *, net_assets, account_open_max_loss, cluster_open: dict,
              policy: dict = rp.POLICY) -> dict:
    """单个候选的理论风险预算。account_open_max_loss=None 表示账户现有持仓最大亏损未知。"""
    session = row["session"]
    base = {"key": row["key"], "instrument": row["instrument"], "session": session, "leg_id": leg["leg_id"],
            "rule": leg["rule"], "side": leg["side"], "sell": leg.get("sell"), "buy": leg.get("buy"),
            "expiry": leg.get("expiry"), "cluster": cluster_of(row["instrument"]),
            "policy_version": policy["version"], "exec_version": VERSION, "disposition": DISPOSITION,
            "broker_status": "unverified", "selection_status": SELECTION}
    if leg.get("status") != "candidate":
        return {**base, "budget_status": "no_candidate", "n": 0, "notes": [leg.get("reason") or "no_candidate"]}
    ent = sh.window_leg(row, leg, f"{session}|open", "entry")
    if ent["status"] != "valid":
        return {**base, "budget_status": "unknown", "n": 0,
                "notes": [f"入场窗无有效报价（{ent['status']}：{','.join(ent.get('reasons', []))}）"]}
    credit = ent["credit"]["conservative"]
    W, fee = leg["width_usd"], sh.CONFIG["fee_round_trip"]
    max_loss = round(W - credit + fee, 4)
    stop_loss = max_loss                   # v5 主终点无止损：止损情景按最大亏损（保守）
    econ = {"credit": credit, "fee": fee, "width": W, "max_loss": max_loss, "stop_loss": stop_loss,
            "fee_per_credit": round(fee / credit, 4) if credit else None}
    if account_open_max_loss is None:
        return {**base, **econ, "budget_status": "unknown", "n": 0,
                "notes": ["账户现有持仓的最大亏损未知（含无上限或算不出的成员）→ 预算无法判定"]}
    n, notes = rp.max_units(net_assets=net_assets, unit_max_loss=max_loss, unit_stop_loss=stop_loss,
                            cluster_open_max_loss=cluster_open.get(base["cluster"], 0.0),
                            account_open_max_loss=account_open_max_loss, policy=policy)
    status = "pass" if n >= 1 else ("unknown" if any("未知" in x for x in notes[:1]) else "fail")
    return {**base, **econ, "budget_status": status, "n": n,
            "notes": notes + ["n 为理论预算上限，互斥备选，不可跨候选相加；券商执行性未核实"]}


def hypothetical_prior_occupancy(prior: list[dict], today: date) -> dict:
    """【假设】此前每个预算通过的主臂候选都按 1 组开了：按簇累计、到 v5 退出日（到期前一交易日）释放。
    只作参考，不扣额度 —— 这些不是实际选入的仓位，且可能与真实持仓重复。"""
    from undertow.core import market_calendar as mc
    out: dict = {}
    for r in prior:
        if r.get("rule") != PRIMARY_ARM or r.get("budget_status") != "pass" or not r.get("expiry"):
            continue
        exit_day = mc.prev_trading_day(date.fromisoformat(r["expiry"])) or date.fromisoformat(r["expiry"])
        if date.fromisoformat(r["session"]) < today <= exit_day:
            out[r["cluster"]] = out.get(r["cluster"], 0.0) + r["max_loss"]
    return out


def evaluate(rows: list[dict], *, session: str, net_assets, account_open_max_loss,
             account_cluster_open: dict | None, prior: list[dict], policy: dict = rp.POLICY) -> list[dict]:
    """某 session 的全部候选；额度只扣本账户真实持仓（account_cluster_open / account_open_max_loss）。"""
    today = date.fromisoformat(session)
    hyp = hypothetical_prior_occupancy(prior, today)
    cl = dict(account_cluster_open or {})
    out = []
    for r in rows:
        if r["session"] != session:
            continue
        for leg in r["legs"]:
            c = candidate(r, leg, net_assets=net_assets, account_open_max_loss=account_open_max_loss,
                          cluster_open=cl, policy=policy)
            c["hypothetical_prior_occupancy"] = hyp.get(c["cluster"], 0.0)
            out.append(c)
    return out
