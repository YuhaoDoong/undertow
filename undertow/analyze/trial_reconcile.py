"""实盘试行：券商资金流水 → 逻辑交易的自动对账（只读提案；纯函数，无 I/O）。

为什么不用成交记录（2026-10-15 实测长桥只读接口）：
- `order executions [--history]` 与 `order detail` 对期权单只给标的代码（SLV.US），组合单给的是两腿净价 ——
  认不出是哪张合约、哪条腿。
- `cash-flow` 每行带合约（新行在 symbol，6–7 月旧行 symbol 为空、合约只在 description「OP/US/GLD260701C376000」），
  带符号金额（买 = 负、卖 = 正，已 ×100），但【没有张数】；费用另起 Option Buy/Sell Fee 行，
  在结算日才入账（9/25 的成交，费用 9/28 才出现）。

所以：钱以资金流水为准；仓位是否已平以当前持仓为准；分组靠「同一时刻成交」把组合单各腿连起来。
凡是本模块不能确定的情形一律标为不确定（ambiguous），不登记、不推进覆盖时点 —— 未知不当 0 笔，也不当没亏。

规则（写定，不因结果调整）：
1. 分组：试行窗口内的期权成交行，按「同一合约」与「同一成交时刻（秒）」做并查集 → 一组 = 一笔候选逻辑交易。
2. 状态：组内任一合约仍在当前持仓 → open（部分平仓不计入连续亏损，不登记）。
   都不在持仓：每张合约都有买也有卖 → closed；只有单边且已过到期日 → closed（视为到期作废）；
   只有单边、未到期却不在持仓 → ambiguous（持仓读数、行权指派或流水缺失，不能猜）。
3. 不确定：同标的同到期出现多组且时间重叠（可能分腿或分批进出）、同一合约落在多组、组内有试行到账之前的成交、
   窗口内出现未知流水类型、标的股票成交（可能是行权/指派）。
4. 费用：组内每个（合约, 买卖方向）都必须有一条同合约同方向、时间不早于该方向最后一笔成交的费用行，才算费用已入账；
   否则 fees_pending，不登记（结果会变）。费率为 0 的情况会一直 pending —— 走人工 `trial trade` 登记。
5. 含费净结果 = 组内成交金额 + 组内费用金额（费用行按合约归组，且只取不早于该组首笔成交的行）。
6. closed_at = 组内最后一笔成交时刻；含到期作废的腿时取 max(最后成交, 到期日 16:00 美东)。
7. 覆盖时点可推进到 now 的条件：两类读数都成功、窗口内无 unknown/ambiguous、每个 closed 组费用已入账、
   覆盖时点之后的每个券商成交时刻都能在资金流水里找到 ±2 秒内的期权成交行（流水晚于成交入账时不推进）；
   0 价且带期权合约代码的成交是到期处理（实测到期次日凌晨出现、流水无对应行），单列为到期事件，不要求流水。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

SCHEMA = "trial-reconcile-v1"
SOURCE = "券商资金流水自动对账（trial-reconcile-v1）"
TIME_TOL_S = 2

_OCC = re.compile(r"([A-Za-z]+)(\d{6})([CP])(\d+)")
OPTION_TXN = {"Option Buy Transaction": "buy", "Option Sell Transaction": "sell"}
OPTION_FEE = {"Option Buy Fee": "buy", "Option Sell Fee": "sell"}
EXTERNAL = {"Cash Deposit", "Cash Withdrawal"}
# 已知、与单笔逻辑交易无关的行：只列出，不归入任何交易（融资利息等是账户成本，进权益不进单笔结果）
KNOWN_OTHER = {"Currency Conversion (Credit)", "Currency Conversion (Debit)", "Reward Redemption",
               "Cash Plus Subscription", "Cash Plus Redemption", "Debit Interest",
               "Corporate Action Funds Debit", "Corporate Action Funds Credit"}


def _ts(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def contract_of(row: dict) -> str | None:
    """合约代码（不带 .US）。symbol 优先，旧行从 description 里取；都没有 → None。"""
    for field in ("symbol", "description"):
        v = str(row.get(field) or "")
        m = _OCC.search(v.split("/")[-1].split(".")[0]) if v else None
        if m and len(m.group(2)) == 6:
            return "".join(m.groups())
    return None


def parse_contract(code: str) -> tuple[str, date, str, float]:
    m = _OCC.fullmatch(code)
    root, ymd, cp, k = m.groups()
    return root.upper(), date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), cp, int(k) / 1000.0


def _expiry_close_utc(d: date) -> datetime:
    """到期日 16:00 美东 → UTC（夏令时 3 月第二个周日至 11 月第一个周日，按日期判定，不依赖系统时区库）。"""
    def nth_sunday(y, m, n):
        first = date(y, m, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    dst = nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)
    return datetime(d.year, d.month, d.day, 20 if dst else 21, tzinfo=timezone.utc)


def _amount(row) -> float | None:
    try:
        x = float(row.get("balance"))
    except (TypeError, ValueError):
        return None
    return x


def reconcile(*, cash_flow: list[dict], positions: list[str], executions: list[dict], coverage_through: str,
              trial_start: str, now: str, known_trade_ids: set | None = None) -> dict:
    """→ 对账提案。cash_flow：试行到账起至 now 的原样资金流水；positions：当前持仓合约代码（含 .US 也可）；
    executions：覆盖时点之后的原样成交（只用 time）。known_trade_ids：状态里已登记的交易 id（幂等显示用）。"""
    start, cov, t_now = _ts(trial_start), _ts(coverage_through), _ts(now)
    problems: list[str] = []
    if start is None or cov is None or t_now is None:
        return {"schema": SCHEMA, "status": "incomplete", "groups": [], "records": [], "external": [], "other": [],
                "problems": ["试行起点/覆盖时点/当前时刻缺失或不带时区"], "can_advance_coverage": False}
    held = {str(p).split(".")[0].upper() for p in positions}
    txns, fees, external, other, unknown = [], [], [], [], []
    for r in cash_flow:
        name, t, amt = r.get("flow_name"), _ts(r.get("time")), _amount(r)
        if t is None or amt is None:
            problems.append(f"流水行时间或金额不可解析：{name!r}")
            continue
        if name in OPTION_TXN or name in OPTION_FEE:
            c = contract_of(r)
            if c is None:
                problems.append(f"期权流水认不出合约：{name} @ {r.get('time')}")
                continue
            rec = {"contract": c, "side": (OPTION_TXN.get(name) or OPTION_FEE.get(name)), "t": t, "amount": amt}
            (txns if name in OPTION_TXN else fees).append(rec)
        elif name in EXTERNAL:
            external.append({"flow_name": name, "currency": r.get("currency"), "amount": amt, "time": r.get("time")})
        elif name in KNOWN_OTHER:
            other.append({"flow_name": name, "currency": r.get("currency"), "amount": amt, "time": r.get("time")})
        else:
            unknown.append({"flow_name": name, "time": r.get("time"), "symbol": r.get("symbol")})
    if unknown:
        problems.append(f"窗口内有 {len(unknown)} 行未知流水类型（如 {unknown[0]['flow_name']!r}，可能是行权/指派/股票成交）")

    # 1. 并查集：同合约、同成交秒
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_sec: dict[datetime, list[str]] = {}
    for r in txns:
        find(r["contract"])
        by_sec.setdefault(r["t"].replace(microsecond=0), []).append(r["contract"])
    for cs in by_sec.values():
        for c in cs[1:]:
            parent[find(c)] = find(cs[0])
    comp: dict[str, list[str]] = {}
    for c in parent:
        comp.setdefault(find(c), []).append(c)

    groups = []
    for root, cs in comp.items():
        cs = sorted(cs)
        gt = [r for r in txns if r["contract"] in cs]
        t0, t1 = min(r["t"] for r in gt), max(r["t"] for r in gt)
        parsed = {c: parse_contract(c) for c in cs}
        und = sorted({p[0] for p in parsed.values()})
        exps = sorted({p[1] for p in parsed.values()})
        g = {"contracts": cs, "underlying": "/".join(und), "expiries": [str(e) for e in exps],
             "first_txn": t0.isoformat(), "last_txn": t1.isoformat(), "flags": [], "state": None,
             "net_pnl_usd": None, "fees_settled": None, "closed_at": None, "trade_id": None}
        if t0 < start:
            g["flags"].append("组内有试行到账之前的成交")
        expired_legs = []
        if any(c in held for c in cs):
            g["state"] = "open"
        else:
            g["state"] = "closed"
            for c in cs:
                sides = {r["side"] for r in gt if r["contract"] == c}
                if sides != {"buy", "sell"}:
                    if _expiry_close_utc(parsed[c][1]) <= t_now:
                        expired_legs.append(c)
                    else:
                        g["flags"].append(f"{c} 只有单边成交、未到期却不在持仓（持仓读数/指派/流水缺失）")
        # 费用：每个（合约, 方向）需一条不早于该方向最后成交的费用行
        pend = []
        for c in cs:
            for side in ("buy", "sell"):
                last = max((r["t"] for r in gt if r["contract"] == c and r["side"] == side), default=None)
                if last is None:
                    continue
                if not any(f["contract"] == c and f["side"] == side and f["t"] >= last for f in fees):
                    pend.append(f"{c} {side}")
        g["fees_settled"] = not pend
        g["fees_pending"] = pend
        gf = [f for f in fees if f["contract"] in cs and f["t"] >= t0]
        g["gross_usd"] = round(sum(r["amount"] for r in gt), 2)
        g["fees_usd"] = round(sum(f["amount"] for f in gf), 2)
        if g["state"] == "closed":
            g["net_pnl_usd"] = round(g["gross_usd"] + g["fees_usd"], 2)
            end = max([t1] + [_expiry_close_utc(parsed[c][1]) for c in expired_legs])
            g["closed_at"] = end.isoformat()
            g["expired_legs"] = expired_legs
        g["trade_id"] = f"lb|{g['underlying']}|{','.join(g['expiries'])}|{','.join(cs)}|{t0.strftime('%Y%m%dT%H%M%SZ')}"
        groups.append(g)

    # 3. 跨组不确定：同一合约多组（并查集下不可能）→ 跳过；同标的同到期时间重叠的多组 → 可能分腿/分批
    for i, a in enumerate(groups):
        for b in groups[i + 1:]:
            if a["underlying"] == b["underlying"] and set(a["expiries"]) & set(b["expiries"]):
                if _ts(a["first_txn"]) <= _ts(b["last_txn"]) and _ts(b["first_txn"]) <= _ts(a["last_txn"]):
                    for g in (a, b):
                        g["flags"].append("同标的同到期另有时间重叠的成交组（可能分腿建仓或分批进出）：需人工确认")
    for g in groups:
        if g["flags"]:
            g["state"] = "ambiguous"

    known = known_trade_ids or set()
    after_cov = [g for g in groups if _ts(g["last_txn"]) > cov or (g["closed_at"] and _ts(g["closed_at"]) > cov)]
    records = []
    for g in after_cov:
        if g["state"] == "closed" and g["fees_settled"]:
            records.append({"trade_id": g["trade_id"], "net_pnl_usd": g["net_pnl_usd"], "closed_at": g["closed_at"],
                            "complete": True, "already_recorded": g["trade_id"] in known})
    # 成交时刻与资金流水对上（±TIME_TOL_S 秒；实测同一笔可差 1 秒）：流水尚未入账的成交 → 不推进。
    # 0 价、带期权合约代码的成交 = 到期处理（实测在到期次日凌晨出现，流水里没有对应行）：不要求流水，记为到期事件。
    txn_secs = sorted(by_sec)
    unmatched, expiry_events = [], []
    for e in executions:
        t = _ts(e.get("time"))
        if t is None:
            problems.append("券商成交行时间不可解析")
            continue
        if t <= cov:
            continue
        c = contract_of({"symbol": e.get("symbol")})
        try:
            px = float(e.get("price"))
        except (TypeError, ValueError):
            px = None
        if c is not None and px == 0:
            expiry_events.append({"contract": c, "time": e.get("time")})
        elif not any(abs((t - s).total_seconds()) <= TIME_TOL_S for s in txn_secs):
            unmatched.append(e.get("time"))
    if unmatched:
        problems.append(f"覆盖时点之后有 {len(unmatched)} 个成交时刻在资金流水里找不到（流水未入账？）：{unmatched[:3]}")
    blockers = list(problems)
    blockers += [f"{g['trade_id']}：{'；'.join(g['flags'])}" for g in after_cov if g["state"] == "ambiguous"]
    blockers += [f"{g['trade_id']}：费用未入账（{', '.join(g['fees_pending'])}）" for g in after_cov
                 if g["state"] == "closed" and not g["fees_settled"]]
    ok = not blockers
    return {"schema": SCHEMA, "status": "complete" if ok else "incomplete", "groups": groups, "records": records,
            "external": [x for x in external if _ts(x["time"]) > start], "other": other, "unknown": unknown,
            "problems": problems, "blockers": blockers, "expiry_events": expiry_events, "can_advance_coverage": ok}
