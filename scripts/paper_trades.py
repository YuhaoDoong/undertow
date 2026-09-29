"""模拟仓自动执行（用户 2026-09-29：「今天开盘后，一个是模拟仓开仓，一个是继续记录数据验证。自动定时进行」）。

  python3 scripts/paper_trades.py tick          # session 钩子每 5 分钟调用：到点才做事，没到点什么都不写
  python3 scripts/paper_trades.py status

只读行情、只写私有 data/soul/（journal.json 里 execution=模拟 且带 paper 规格的事前判断 + paper_discretionary.jsonl 研究台账）；
**从不下单**（AGENTS 第一条）。规则版本 RULE_VERSION；每条 paper 规格记下它按哪一版执行。

v2（Codex 025-1/2 修订，入场前生效）：
- 规格校验：数值有限、qty 为正整数、宽度 > 0、腿方向正确（put 价差 卖腿行权价 > 买腿；call 相反）。不合格 → invalid_spec，不执行。
- 报价校验：两腿 bid/ask 有限、> 0、bid ≤ ask；权利金 = 卖腿 bid − 买腿 ask 必须 0 < 权利金 < 宽度。
  不合格 → 记下原始报价（entry_quote_invalid）并在窗口内继续取；**不截断报价制造可行成交**。
- 入场政策：窗口内【第一份】合格且权利金/宽度 ≥ 门槛的报价即入场（不看完窗口再择优）；窗口已过仍未入场 →
  missed（从未取得报价）或 skipped(no_valid_quote / credit_low)。错过入场日也终态标记，不会一直 planned。
- 盯市：只在常规交易时段内的时点（默认 09:45、15:45 ET）取盘口估值；这是**稀疏检查，不是连续止损**。
  时点落在常规时段之外 → 只记估值（mark_offhours），不执行止损。
- 结算：**理论到期记账**（按到期日常规收盘价的内在价值现金化；不模拟提前行权、指派、到期处置与实物交割）。
  收盘价取长桥日线中【日期 = 到期日】的那根，且只在到期日 16:20 ET 之后；取不到 → settlement_pending，之后每次唤醒重试；
  到期后才恢复运行也能补结（用到期日那根，不用恢复当天的价）。
- 风险单位：每笔固定 1 组（qty=1），收益与风险按每组比较；两笔金银同日不是两份独立证据。
- 结算口径 provisional（长桥日线）；CBOE 日线到后异步核对（audit_settlement），两源原值都留、不择优不平均。
- 手续费：fee_round_trip 为【每组】往返费用，整单 = 每组 × qty；最大亏损、最大收益、盈亏平衡都由 economics() 同一函数算。
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOURNAL = ROOT / "data/soul/journal.json"
LOCK = ROOT / "data/soul/journal.json.lock"
LEDGER = ROOT / "data/soul/paper_discretionary.jsonl"
ET = ZoneInfo("America/New_York")
RULE_VERSION = "paper-sim-v2-20260929"
RTH = (time(9, 30), time(16, 0))
TERMINAL = ("settled", "closed_stop", "skipped", "missed", "invalid_spec")


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def _finite(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def economics(side: str, k_sell: float, k_buy: float, credit: float, qty: int, fee_round_trip: float) -> dict:
    """价差经济量的唯一来源（Codex 025-2）。fee_round_trip = 每组往返费用；整单 = 每组 × qty。"""
    width = abs(k_sell - k_buy)
    fee_ps = fee_round_trip / 100.0                      # 每股摊到的费用（1 组 = 100 股）
    be = (k_sell - credit + fee_ps) if side == "P" else (k_sell + credit - fee_ps)
    return {"width": width, "credit": credit,
            "max_gain_usd": round((credit * 100 - fee_round_trip) * qty, 2),
            "max_loss_usd": round(((width - credit) * 100 + fee_round_trip) * qty, 2),
            "breakeven": round(be, 4), "fee_total_usd": round(fee_round_trip * qty, 2)}


def validate_spec(p: dict) -> str | None:
    if not _finite(p.get("k_sell"), p.get("k_buy"), p.get("min_credit_ratio"), p.get("stop_mult"), p.get("fee_round_trip")):
        return "数值非有限或缺失"
    if not (isinstance(p.get("qty"), int) and p["qty"] > 0):
        return "qty 须为正整数"
    if p.get("side") not in ("P", "C"):
        return "side 须为 P/C"
    if p["k_sell"] == p["k_buy"]:
        return "宽度为 0"
    if (p["side"] == "P") != (p["k_sell"] > p["k_buy"]):
        return "腿方向错误（put 价差卖腿行权价须高于买腿，call 相反）"
    if p["fee_round_trip"] < 0 or p["stop_mult"] <= 1 or not 0 <= p["min_credit_ratio"] < 1:
        return "费用/止损倍数/门槛不在合理范围"
    return None


def quote_ok(q: dict) -> bool:
    """报价可用：无显式错误、bid/ask 有限、0 < bid ≤ ask（Codex 026：带 error 的报价一律拒绝）。"""
    return q is not None and not q.get("error") and _finite(q.get("bid"), q.get("ask")) and 0 < q["bid"] <= q["ask"]


def quote_labels(q: dict, sell: str, buy: str, qty: int) -> dict:
    """研究模式按报价假设成交；另标挂单量是否够、两腿抓取时差、源时戳未知 —— 只标注，不据此称可执行。"""
    s, b = q.get(sell) or {}, q.get(buy) or {}
    sizes = (s.get("bid_size"), b.get("ask_size"))
    size_ok = None if any(x is None for x in sizes) else all(x >= qty for x in sizes)
    gap = None
    try:
        gap = abs((datetime.fromisoformat(b["fetched_at"]) - datetime.fromisoformat(s["fetched_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    return {"quote_valid": True, "size_sufficient": size_ok, "leg_fetch_gap_s": gap,
            "time_alignment": "source_ts_unknown（长桥 depth 不给源时戳）"}


def _depth(symbols):
    """逐腿取盘口。长桥 depth 接口不给源时戳，只能记逐腿抓取时刻（两腿时差由此可算）与一档挂单量。"""
    from undertow.collect.longbridge_quote import fetch_depth
    out = {}
    for s in symbols:
        d = fetch_depth([s]).get(s)
        out[s] = {"bid": getattr(d, "bid", None), "ask": getattr(d, "ask", None),
                  "bid_size": getattr(d, "bid_size", None), "ask_size": getattr(d, "ask_size", None),
                  "error": getattr(d, "error", None), "fetched_at": datetime.now(timezone.utc).isoformat(),
                  "source_ts": None}
    return out


def _session_close(underlying: str, day: date):
    """到期日常规收盘：长桥日线里日期 = day 的那根；没有 → None（settlement_pending）。
    取数根数按目标日期推算（长时间停机后恢复也能取到旧到期日，Codex 026）。结果是 provisional，CBOE 到后另行核对。"""
    from undertow.collect.longbridge_kline import fetch_bars
    count = min(1000, max(10, (datetime.now(ET).date() - day).days + 5))
    for b in fetch_bars(underlying, period="day", count=count):
        if b["ts"].astimezone(ET).date() == day and _finite(b["close"]):
            return {"close": b["close"], "source": "longbridge kline day", "bar_date": day.isoformat(),
                    "status": "provisional", "bar": {k: (v.isoformat() if k == "ts" else v) for k, v in b.items()},
                    "fetched_at": datetime.now(timezone.utc).isoformat()}
    return None


def _cboe_close(underlying: str, day: date):
    """CBOE 公开日线里日期 = day 的收盘（通常滞后 1–3 天）；没有 → None。第二来源不一定独立，也不是交易所官方真值。"""
    from undertow.collect.base import http_get_json
    from undertow.collect.cboe_history import CBOE_HIST_URL
    d = http_get_json(CBOE_HIST_URL.format(symbol=underlying.split(".")[0]))
    for r in d.get("data") or []:
        if r.get("date") == day.isoformat():
            try:
                c = float(r["close"])
            except (KeyError, TypeError, ValueError):
                return None
            return {"close": c, "source": "cboe daily", "row": r} if _finite(c) else None
    return None


def _spread_value(p: dict, s: float) -> float:
    return (max(0.0, p["k_sell"] - s) - max(0.0, p["k_buy"] - s)) if p["side"] == "P" else \
           (max(0.0, s - p["k_sell"]) - max(0.0, s - p["k_buy"]))


AUDIT_TOL = 0.01          # 两源收盘差 ≤ 1 美分算一致（ETF 报价精度 0.01）
AUDIT_RETRY_S = 3600      # 第二来源未到时最多每小时查一次


def audit_settlement(p: dict, now: datetime, *, cboe_close=_cboe_close) -> str | None:
    """已结算仓位的异步交叉核对（Codex 026-4）：追加 source_match / source_mismatch / source_unavailable，
    两源原值都保留；不改原记账、不择优、不平均。不一致且改变价内外或盈亏符号 → result_under_review。"""
    if p.get("state") != "settled":
        return None
    a = p.get("settle_audit") or {}
    if a.get("status") in ("source_match", "source_mismatch"):
        return None
    if a.get("last_try") and (now - datetime.fromisoformat(a["last_try"])).total_seconds() < AUDIT_RETRY_S:
        return None
    exp = date.fromisoformat(p["expiry"])
    c = cboe_close(p["underlying"], exp)
    a = {"last_try": now.isoformat(), "primary": p["settle"]}
    if c is None:
        a["status"] = "source_unavailable"
        first = (p.get("settle_audit") or {}).get("status") != "source_unavailable"
        p["settle_audit"] = a
        if first:
            p["events"].append({"at": now.isoformat(), "action": "settle_audit", "status": "source_unavailable"})
            return "source_unavailable"
        return None
    v2 = _spread_value(p, c["close"])
    pnl2 = round(((p["entry_credit"] - v2) * 100 - p["fee_round_trip"]) * p["qty"], 2)
    same = abs(c["close"] - p["settle"]["close"]) <= AUDIT_TOL
    flip = (v2 > 0) != (p["settle_value"] > 0) or (pnl2 >= 0) != (p["pnl_usd"] >= 0)
    a.update(status="source_match" if same else "source_mismatch", secondary=c, secondary_value=round(v2, 4),
             secondary_pnl_usd=pnl2, result_under_review=bool(not same and flip))
    p["settle_audit"] = a
    if a["result_under_review"]:
        p["result_under_review"] = True
    p["events"].append({"at": now.isoformat(), "action": "settle_audit", "status": a["status"],
                        "primary_close": p["settle"]["close"], "secondary_close": c["close"],
                        "result_under_review": a["result_under_review"]})
    return a["status"]


def step(p: dict, now: datetime, *, depth=_depth, session_close=_session_close) -> str | None:
    """推进一步；返回动作名（None = 没到点）。纯逻辑 + 注入的取数函数，便于测试。"""
    et = now.astimezone(ET)
    today = et.date()
    ev = p.setdefault("events", [])
    st = p["state"]
    if st in TERMINAL:
        return None
    bad = validate_spec(p)
    if bad:
        p["state"] = "invalid_spec"; p["invalid_reason"] = bad
        ev.append({"at": now.isoformat(), "action": "invalid_spec", "why": bad})
        return "invalid_spec"
    p.setdefault("rule_version", RULE_VERSION)
    width = abs(p["k_sell"] - p["k_buy"])
    if st == "planned":
        lo, hi = (_hm(x) for x in p["entry_window_et"])
        ed = date.fromisoformat(p["entry_date"])
        if today < ed or (today == ed and et.time() < lo):
            return None
        if today > ed or et.time() >= hi:                  # 窗口已过（含错过入场日）→ 终态
            tried = [e for e in ev if e.get("action", "").startswith("entry_quote")]
            if not tried:
                p["state"] = "missed"; p["skip_reason"] = "missed_window（窗口内未运行）"
            else:
                p["state"] = "skipped"; p["skip_reason"] = p.get("last_reject") or "no_valid_quote"
            ev.append({"at": now.isoformat(), "action": p["state"], "why": p["skip_reason"]})
            return p["state"]
        q = depth([p["sell"], p["buy"]])
        s, b = q.get(p["sell"]), q.get(p["buy"])
        if not (quote_ok(s) and quote_ok(b)):
            p["last_reject"] = "no_valid_quote（报价缺失/非有限/bid>ask）"
            ev.append({"at": now.isoformat(), "action": "entry_quote_invalid", "quotes": q, "why": p["last_reject"]})
            return "retry"
        credit = round(s["bid"] - b["ask"], 4)
        if not 0 < credit < width:
            p["last_reject"] = f"quote_anomaly（权利金 {credit} 不在 (0, 宽度 {width:g})）"
            ev.append({"at": now.isoformat(), "action": "entry_quote_invalid", "quotes": q, "why": p["last_reject"]})
            return "retry"
        if credit / width < p["min_credit_ratio"]:
            p["last_reject"] = f"credit_low（{credit:.2f}/{width:g} < {p['min_credit_ratio']:.0%}）"
            ev.append({"at": now.isoformat(), "action": "entry_quote", "quotes": q, "why": p["last_reject"]})
            return "retry"                                 # 窗口内继续取；第一份合格即入场
        econ = economics(p["side"], p["k_sell"], p["k_buy"], credit, p["qty"], p["fee_round_trip"])
        lab = quote_labels(q, p["sell"], p["buy"], p["qty"])
        p.update(state="entered", entered_at=now.isoformat(), entry_credit=credit, economics=econ,
                 stop_value=round(p["stop_mult"] * credit, 4), entry_quote_labels=lab)
        ev.append({"at": now.isoformat(), "action": "enter", "quotes": q, "credit": credit, "economics": econ,
                   "quote_labels": lab})
        return "enter"
    # —— entered ——
    exp = date.fromisoformat(p["expiry"])
    if today > exp or (today == exp and et.time() >= time(16, 20)):
        c = session_close(p["underlying"], exp)
        if c is None:
            if p.get("settlement_status") != "pending":
                p["settlement_status"] = "pending"
                ev.append({"at": now.isoformat(), "action": "settlement_pending", "why": "到期日收盘价尚不可得"})
                return "settlement_pending"
            return None
        s = c["close"]
        val = _spread_value(p, s)
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        p.update(state="settled", settled_at=now.isoformat(), settlement_status="done", settle=c,
                 settle_value=round(val, 4), pnl_usd=pnl,
                 settlement_model="理论到期记账：按到期日常规收盘价的内在价值现金化；不模拟提前行权/指派/实物交割")
        ev.append({"at": now.isoformat(), "action": "settle", "close": c, "value": val, "pnl": pnl})
        return "settle"
    done = {e.get("slot") for e in ev if e.get("action") in ("mark", "mark_offhours")}
    for slot in p["mark_slots_et"]:
        key = f"{today.isoformat()} {slot}"
        t0 = _hm(slot)
        if key in done or today > exp or et.time() < t0 or (et.hour * 60 + et.minute) > t0.hour * 60 + t0.minute + 15:
            continue
        q = depth([p["sell"], p["buy"]])
        s, b = q.get(p["sell"]), q.get(p["buy"])
        if not (quote_ok(s) and quote_ok(b)):
            ev.append({"at": now.isoformat(), "action": "mark_retry", "slot": key, "quotes": q})
            return "retry"
        val = round(s["ask"] - b["bid"], 4)
        in_rth = RTH[0] <= et.time() < RTH[1]
        ev.append({"at": now.isoformat(), "action": "mark" if in_rth else "mark_offhours", "slot": key,
                   "quotes": q, "value": val, "note": "稀疏检查（非连续止损）" + ("" if in_rth else "；非常规时段只估值、不执行")})
        if in_rth and val >= p["stop_value"]:
            pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
            p.update(state="closed_stop", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl)
            ev.append({"at": now.isoformat(), "action": "stop", "value": val, "pnl": pnl})
            return "stop"
        return "mark"
    return None


def _outcome(t: dict) -> None:
    p = t["paper"]
    if p["state"] in ("settled", "closed_stop"):
        t["trade_pnl"] = f"{p['pnl_usd']:.2f}"
        full = p["state"] == "settled" and p.get("settle_value", 1) == 0
        t["outcome"] = "对" if full else ("错" if p["pnl_usd"] < 0 else "部分")
        t["scored_at"] = datetime.now(ET).date().isoformat()
    elif p["state"] in ("skipped", "missed", "invalid_spec"):
        t["outcome"] = "未执行"
        t["review"] = (t.get("review") or "") + f"【模拟仓未入场】{p.get('skip_reason') or p.get('invalid_reason')}"


OUTCOME_ACTIONS = ("enter", "skipped", "missed", "invalid_spec", "stop", "settle")
LEDGER_ACTIONS = OUTCOME_ACTIONS + ("settle_audit",)


def event_id(thesis_id: str, e: dict) -> str:
    return f"{thesis_id}|{e['action']}|{e['at']}"


def sync_ledger(j: dict, ledger: Path | None = None) -> int:
    """paper-discretionary 研究台账 = journal 关键事件的只追加投影（Codex 025/026）。
    在 journal 原子替换【之后】调用；按稳定 event_id 去重 —— 中途崩溃后下次唤醒补齐，重复运行不重复写。
    台账某行解析失败 → 抛错（不静默跳过、不覆盖）。返回本次追加条数。"""
    ledger = ledger or LEDGER
    seen = set()
    if ledger.exists():
        for i, line in enumerate(ledger.read_text("utf-8").splitlines(), 1):
            if line.strip():
                try:
                    seen.add(json.loads(line)["event_id"])
                except (ValueError, KeyError) as e:
                    raise RuntimeError(f"{ledger.name} 第 {i} 行损坏：{e}")
    new = []
    for t in j.get("theses", []):
        p = t.get("paper")
        if t.get("execution") != "模拟" or not p:
            continue
        for e in p.get("events") or []:
            if e.get("action") not in LEDGER_ACTIONS or event_id(t["id"], e) in seen:
                continue
            new.append({"event_id": event_id(t["id"], e), "thesis_id": t["id"],
                        "rule_version": p.get("rule_version", RULE_VERSION), "action": e["action"], "at": e["at"],
                        "state_now": p["state"],
                        "spec": {k: p.get(k) for k in ("underlying", "expiry", "side", "sell", "buy", "k_sell", "k_buy",
                                                       "qty", "entry_window_et", "min_credit_ratio", "stop_mult",
                                                       "fee_round_trip", "mark_slots_et")},
                        "evidence": e,
                        "result": {k: p.get(k) for k in ("entry_credit", "economics", "pnl_usd", "settle", "skip_reason")}})
    if new:
        with open(ledger, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())
    return len(new)


def tick(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = []
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        j = json.loads(JOURNAL.read_text("utf-8"))
        changed = False
        for t in j.get("theses", []):
            p = t.get("paper")
            if t.get("execution") != "模拟" or not p:
                continue
            try:
                a = step(p, now) or audit_settlement(p, now)
            except Exception as e:                      # 取数失败：留痕并在下次唤醒重试
                p.setdefault("events", []).append({"at": now.isoformat(), "action": "error", "error": f"{type(e).__name__}: {e}"[:200]})
                a = "error"
            if a:
                changed = True
                if a in OUTCOME_ACTIONS:
                    _outcome(t)
                out.append(f"{t['id']}:{a}")
        if changed:
            body = json.dumps(j, ensure_ascii=False, indent=2)
            fd, name = tempfile.mkstemp(dir=JOURNAL.parent, prefix=".journal.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body); f.flush(); os.fsync(f.fileno())
            if json.loads(Path(name).read_text("utf-8")) != j:
                raise RuntimeError("journal 回读校验失败，未替换")
            os.replace(name, JOURNAL)
        sync_ledger(j)                                   # journal 落盘之后再投影；每次都核对补齐
        fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "tick":
        acts = tick()
        print("模拟仓：" + ("；".join(acts) if acts else "无到点动作"))
        return 1 if any(a.endswith(":error") for a in acts) else 0
    j = json.loads(JOURNAL.read_text("utf-8"))
    for t in j.get("theses", []):
        p = t.get("paper")
        if t.get("execution") == "模拟" and p:
            print(t["id"], p["state"], {k: p.get(k) for k in ("rule_version", "entry_credit", "economics", "stop_value",
                                                              "pnl_usd", "skip_reason", "settlement_status")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
