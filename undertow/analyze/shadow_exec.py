"""S05：账户可执行账（纯计算，无 I/O）。与研究账严格分开。

研究账（data/history/shadow/，公开）保留全部机会，回答「墙方案是否有增量」。
可执行账（data/account/shadow_exec/，gitignore）回答另一件事：**本账户此刻能不能承受这一组**。
研究能生成候选 ≠ 本账户可承受一组；没有可承受的一组时，结论就是「暂不可执行，继续观察」，
不为凑整数张去提高风险阈值（Codex 008 strategy_plan §2）。

每个候选推导：一组的保守收权金、往返费用、最大亏损（宽 − 收权金 + 费）、止损情景损失、
所在簇与跨日占用（本账户现有持仓 + 此前被判可执行且未到期的主臂候选）、到期处置能力（未核实项照实列出），
再用 risk_policy 算可开组数。未知输入一律 → 0 组 + 原因，不是通过。
"""
from __future__ import annotations

from datetime import date

from undertow.analyze import risk_policy as rp
from undertow.analyze import shadow as sh

VERSION = "shadow-exec-v1-20260926"
PRIMARY_ARM = "A"            # 跨日占用只累计主臂（墙方案）；各臂互为替代，同日不叠加
DISPOSITION = {
    "short_only_permission": "未核实：组合单能否单独买回短腿取决于券商与入场方式",
    "itm_residual": "未核实：实值残腿被自动行权后的处置、费用与隔夜风险",
    "expiry_process": "未核实：长桥到期处置条款（未取得官方文档）",
}


def cluster_of(inst: str) -> str:
    return next((c for c, ks in sh.CLUSTERS.items() if inst in ks), inst)


def candidate(row: dict, leg: dict, *, net_assets, account_open_max_loss, cluster_open: dict,
              policy: dict = rp.POLICY) -> dict:
    """单个候选的可执行判定。account_open_max_loss=None 表示账户现有持仓最大亏损未知。"""
    session = row["session"]
    base = {"key": row["key"], "instrument": row["instrument"], "session": session, "leg_id": leg["leg_id"],
            "rule": leg["rule"], "side": leg["side"], "sell": leg.get("sell"), "buy": leg.get("buy"),
            "expiry": leg.get("expiry"), "cluster": cluster_of(row["instrument"]),
            "policy_version": policy["version"], "exec_version": VERSION, "disposition": DISPOSITION}
    if leg.get("status") != "candidate":
        return {**base, "verdict": "无候选", "n": 0, "notes": [leg.get("reason") or "no_candidate"]}
    ent = sh.window_leg(row, leg, f"{session}|open", "entry")
    if ent["status"] != "valid":
        return {**base, "verdict": "暂不可执行", "n": 0,
                "notes": [f"入场窗无有效报价（{ent['status']}：{','.join(ent.get('reasons', []))}）"]}
    credit = ent["credit"]["conservative"]
    W, fee = leg["width_usd"], sh.CONFIG["fee_round_trip"]
    max_loss = round(W - credit + fee, 4)
    stop_loss = max_loss                   # v5 主终点无止损：止损情景按最大亏损（保守）
    econ = {"credit": credit, "fee": fee, "width": W, "max_loss": max_loss, "stop_loss": stop_loss,
            "fee_per_credit": round(fee / credit, 4) if credit else None}
    if account_open_max_loss is None:
        return {**base, **econ, "verdict": "暂不可执行", "n": 0,
                "notes": ["账户现有持仓的最大亏损未知（含无上限或算不出的成员）→ 不判定为可执行"]}
    n, notes = rp.max_units(net_assets=net_assets, unit_max_loss=max_loss, unit_stop_loss=stop_loss,
                            cluster_open_max_loss=cluster_open.get(base["cluster"], 0.0),
                            account_open_max_loss=account_open_max_loss, policy=policy)
    return {**base, **econ, "verdict": ("可执行" if n >= 1 else "暂不可执行"), "n": n,
            "notes": notes + ["到期处置能力未核实：见 disposition"]}


def open_occupancy(prior: list[dict], today: date) -> dict:
    """此前被判可执行、未到期的主臂候选按簇累计最大亏损（假设按当时允许的组数执行，保守）。"""
    out: dict = {}
    for r in prior:
        if r.get("rule") != PRIMARY_ARM or r.get("verdict") != "可执行" or not r.get("expiry"):
            continue
        if date.fromisoformat(r["expiry"]) >= today and date.fromisoformat(r["session"]) < today:
            out[r["cluster"]] = out.get(r["cluster"], 0.0) + r["max_loss"] * r["n"]
    return out


def evaluate(rows: list[dict], *, session: str, net_assets, account_open_max_loss,
             account_cluster_open: dict | None, prior: list[dict], policy: dict = rp.POLICY) -> list[dict]:
    """某 session 的全部候选。account_cluster_open：本账户现有持仓按簇的最大亏损（None=未知）。"""
    today = date.fromisoformat(session)
    occ = open_occupancy(prior, today)
    cl = dict(account_cluster_open or {})
    for k, v in occ.items():
        cl[k] = cl.get(k, 0.0) + v
    acct = None if account_open_max_loss is None else account_open_max_loss + sum(occ.values())
    out = []
    for r in rows:
        if r["session"] != session:
            continue
        for leg in r["legs"]:
            out.append(candidate(r, leg, net_assets=net_assets, account_open_max_loss=acct, cluster_open=cl,
                                 policy=policy))
    return out
