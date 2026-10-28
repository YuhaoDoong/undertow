"""预算权益 R（Codex 065 WP2；用户 2026-10-28 确认启用）。纯函数：选日初基准、算外部资金调整、给出 R 快照。

    A(t)        = E0 + F(t)                       E0 = 上一已收市交易日的日终观察（或显式 bootstrap）
    R_budget(t) = max(0, min(A(t), E_now(t)))      F = E0 观察之后、截至 t 已登记的外部净资金（入金正、出金负）

含义：盘中浮盈进入 E_now，但不抬高当天预算上限（下一日初由新日终观察确认）；亏损立即缩额，回升可恢复到 A；
不锁定日内最低点。期权开平现金只是在现金与持仓之间换形，不进 F；入金不解除暂停、不重置起始资本。
R ≤ 0（E_now ≤ 0）→ 禁止新增风险（固定美元下限不覆盖）；E_now/E0 未知 → incomplete（不当 0）。

口径 broker-net-assets-usd-v1：E 取长桥 `assets --currency USD` 的 net_assets（整个账户折算为 USD，含 SGD 现金；
持仓按 last 估值，未独立核验 bid/ask 与费用）。E0 与 E_now 必须同一口径；以后升 net-liquidation-usd-v2 要重定基准、不混接。

日初基准的选择（美股交易日历，ET）：
- L = 截至 t 最近一个【已收市】交易日。合格基准 = market_session == L 的日终观察（收市后采集，下一次开盘前）。
- 收市后到下一次开盘前，若 L 的日终观察还没采到，允许沿用 L 前一交易日的日终观察（这段时间期权不交易），标 pending_eod。
- 显式 bootstrap（首次部署/漏采补救，必须带原因）：采集时刻晚于 L 收市即可作基准，标 origin=bootstrap。
- 都没有 → incomplete（不拿更旧的观察冒充、不伪造历史收盘）。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from undertow.core import market_calendar as mc

R_BASIS_VERSION = "broker-net-assets-usd-v1"
SCHEMA = 1
ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
ACCOUNT_SCOPE = "longbridge-live-account-all-currencies-as-USD"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _ts(s) -> datetime | None:
    if isinstance(s, datetime):
        return s if s.tzinfo else None
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def close_dt(d: date) -> datetime | None:
    ct = mc.close_time(d)
    if ct is None:
        return None
    hh, mm = map(int, ct.split(":"))
    return datetime.combine(d, time(hh, mm), ET)


def open_dt(d: date) -> datetime | None:
    return datetime.combine(d, OPEN, ET) if mc.is_trading_day(d) else None


def last_closed_session(t: datetime) -> date | None:
    """截至 t 最近一个已收市的交易日；日历覆盖不到 → None。"""
    d = t.astimezone(ET).date()
    if mc.is_trading_day(d) is None:
        return None
    if mc.is_trading_day(d) and t >= close_dt(d):
        return d
    return mc.prev_trading_day(d)


def eod_session_for_capture(t: datetime) -> tuple[date | None, str]:
    """在 t 采集的观察能否算某个交易日的日终观察 → (交易日, 说明)。只有收市后到下一次开盘前的观察才算。"""
    L = last_closed_session(t)
    if L is None:
        return None, "日历未覆盖"
    nxt = mc.next_trading_day(L)
    if nxt is None:
        return None, "日历未覆盖下一交易日"
    if t < open_dt(nxt):
        return L, f"{L} 收市后、{nxt} 开盘前"
    return None, f"现在是 {nxt} 盘中/盘后：不是 {L} 的日终观察（盘中只能显式 bootstrap）"


def select_anchor(anchors: list[dict], t: datetime) -> tuple[dict | None, str, str]:
    """→ (基准, 状态, 说明)。状态：ok / pending_eod / bootstrap / missing / calendar_unknown。只看采集时刻 ≤ t 的观察。"""
    L = last_closed_session(t)
    if L is None:
        return None, "calendar_unknown", "日历未覆盖当前时刻 → 不可判定"
    seen = [a for a in anchors if _ts(a.get("captured_at")) is not None and _ts(a["captured_at"]) <= t
            and a.get("R_basis_version") == R_BASIS_VERSION and _num(a.get("E"))]
    eod = sorted((a for a in seen if a.get("origin") == "eod"), key=lambda a: _ts(a["captured_at"]))
    hit = [a for a in eod if a.get("market_session") == L.isoformat()]
    if hit:
        return hit[0], "ok", f"{L} 日终观察（首份）"
    boots = sorted((a for a in seen if a.get("origin") == "bootstrap" and _ts(a["captured_at"]) >= close_dt(L)),
                   key=lambda a: _ts(a["captured_at"]))
    nxt = mc.next_trading_day(L)
    after_close = nxt is not None and t < open_dt(nxt) and t >= close_dt(L)
    prev = mc.prev_trading_day(L)
    if after_close and prev is not None:
        hp = [a for a in eod if a.get("market_session") == prev.isoformat()]
        if hp:
            return hp[0], "pending_eod", f"{L} 收市后日终观察尚未采集，沿用 {prev} 日终（下一次开盘前期权不交易）"
    if boots:
        return boots[-1], "bootstrap", f"显式 bootstrap（{boots[-1].get('note') or '未注明原因'}）"
    return None, "missing", f"缺 {L} 的日终观察（也没有之后的 bootstrap）→ 跑 `undertow trial r-anchor`"


def flows_between(flows: list[dict], after: datetime, through: datetime) -> tuple[float, int]:
    """已登记的外部净资金（trial 状态 external_flows）：E0 观察之后、截至 t（含）。→ (净额, 条数)。"""
    sel = [f for f in flows if _ts(f.get("at")) is not None and after < _ts(f["at"]) <= through and _num(f.get("amount"))]
    return round(sum(f["amount"] for f in sel), 2), len(sel)


def budget_r(E0, F, E_now) -> dict:
    """A = E0 + F；R = max(0, min(A, E_now))。任一未知 → R None（incomplete）；E_now ≤ 0 → R = 0、禁止新增。"""
    if not (_num(E0) and _num(F) and _num(E_now)):
        return {"A": None, "R_budget": None, "status": "incomplete", "binding": None,
                "note": "E0/F/E_now 有未知 → R 不可判定（不是 0，也不是通过）"}
    A = round(E0 + F, 2)
    R = max(0.0, min(A, E_now))
    if E_now <= 0 or R <= 0:
        return {"A": A, "R_budget": 0.0, "status": "no_new_risk", "binding": "E_now",
                "note": f"当前权益 {E_now:,.2f} ≤ 0 或预算为 0 → 禁止新增风险（固定下限不覆盖）"}
    binding = "A（日初基准 + 已登记外部资金）" if A <= E_now else "E_now（当前权益）"
    return {"A": A, "R_budget": round(R, 2), "status": "ok", "binding": binding, "note": ""}


def snapshot(*, anchors: list[dict], flows: list[dict], E_now, captured_at: datetime, policy_version: str,
             broker_external_rows: int | None = None) -> dict:
    """一次只读预算用的 R 快照（同一次扫描只用这一份；snapshot_id = 内容散列）。"""
    a, ast, anote = select_anchor(anchors, captured_at)
    snap = {"schema": SCHEMA, "policy_version": policy_version, "R_basis_version": R_BASIS_VERSION,
            "account_scope_id": ACCOUNT_SCOPE, "base_currency": "USD",
            "captured_at": captured_at.isoformat(), "valuation_asof": captured_at.isoformat(),
            "valuation_note": "券商只给请求时点 net_assets（持仓按 last），不是独立核验的可平仓净值",
            "anchor_status": ast, "anchor_note": anote, "origin": None if a is None else a.get("origin"),
            "E0_reference_id": None if a is None else a.get("id"), "E0": None if a is None else a.get("E"),
            "E0_market_session": None if a is None else a.get("market_session"),
            "external_flows_through": captured_at.isoformat(), "net_external_F": None, "external_flow_count": None,
            "E_now": E_now if _num(E_now) else None, "missing_fields": [], "assumptions": [], "quality_state": None}
    if a is None:
        snap["missing_fields"].append("E0")
    if not _num(E_now):
        snap["missing_fields"].append("E_now")
    if a is not None:
        F, n = flows_between(flows, _ts(a["captured_at"]), captured_at)
        snap.update(net_external_F=F, external_flow_count=n)
    r = budget_r(snap["E0"], snap["net_external_F"] if a is not None else None, snap["E_now"])
    snap.update(anchor_adjusted_A=r["A"], R_budget=r["R_budget"], status=r["status"], binding=r["binding"])
    if r["note"]:
        snap["assumptions"].append(r["note"])
    snap["assumptions"].append("F 只计 trial 状态里已登记的外部资金；未登记的入金不提额（min 保守），未登记的出金已体现在 E_now")
    if broker_external_rows is None:
        snap["assumptions"].append("券商出入金流水未核对（只影响说明：未登记入金不会抬高 R）")
    elif snap["external_flow_count"] is not None and broker_external_rows != snap["external_flow_count"]:
        snap["assumptions"].append(f"券商流水在基准之后有 {broker_external_rows} 条出入金，已登记 {snap['external_flow_count']} 条 →"
                                   " 请用 trial fund 补登（未登记入金不提额）")
    snap["quality_state"] = ("incomplete" if snap["status"] == "incomplete" else
                             {"ok": "current", "pending_eod": "expected_carry", "bootstrap": "bootstrap"}.get(ast, ast))
    blob = json.dumps({k: snap[k] for k in ("policy_version", "R_basis_version", "captured_at", "E0_reference_id",
                                            "E0", "net_external_F", "E_now", "R_budget")}, sort_keys=True)
    snap["snapshot_id"] = "r-" + hashlib.sha256(blob.encode()).hexdigest()[:16]
    return snap


def anchor_record(*, E, captured_at: datetime, origin: str, market_session: str | None, note: str = "") -> dict:
    """日初基准观察（私有落盘一行）。origin：eod（收市后观察）/ bootstrap（显式，须带原因）。"""
    if origin not in ("eod", "bootstrap"):
        raise ValueError(f"未知 origin {origin!r}")
    if not _num(E):
        raise ValueError("E 必须是有限数（未知不能当基准）")
    if origin == "bootstrap" and not note.strip():
        raise ValueError("bootstrap 必须写明原因")
    rid = f"eod|{market_session}" if origin == "eod" else f"bootstrap|{captured_at.isoformat()}"
    return {"id": rid, "schema": SCHEMA, "origin": origin, "market_session": market_session, "E": round(float(E), 2),
            "currency": "USD", "R_basis_version": R_BASIS_VERSION, "account_scope_id": ACCOUNT_SCOPE,
            "captured_at": captured_at.isoformat(), "valuation_note": "请求时点 net_assets（持仓按 last）", "note": note}
