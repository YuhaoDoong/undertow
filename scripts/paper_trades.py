"""模拟仓自动执行（用户 2026-09-29：「今天开盘后，一个是模拟仓开仓，一个是继续记录数据验证。自动定时进行」）。

  python3 scripts/paper_trades.py tick          # session 钩子每 5 分钟调用：到点才做事，没到点什么都不写
  python3 scripts/paper_trades.py status

只读行情、只写私有 data/soul/journal.json（execution=模拟 且带 paper 规格的事前判断）；**从不下单**（AGENTS 第一条）。
状态机（规则取自各条 paper 规格，事前写死）：
- planned → 入场窗口（ET，默认 10:00–10:20）内取两腿实时盘口，保守价 权利金 = 卖腿 bid − 买腿 ask；
  权利金/宽度 ≥ min_credit_ratio → entered；低于 → skipped(credit_low)；窗口结束仍取不到有效报价 → skipped(no_quote)。
- entered → 每个交易日的盯市时点（默认 09:40、16:35 ET，各一次）：平仓价值 = 卖腿 ask − 买腿 bid；
  ≥ stop_mult × 权利金 → closed_stop（按该价值平仓记账）。
- entered → 到期日 ET 16:20 后按标的收盘价的到期内在价值结算 → settled。
盈亏 = (权利金 − 平仓/到期价值) × 100 × 张数 − 每组往返费用。每一步都记原始报价与时刻（events）。
"""
from __future__ import annotations

import fcntl
import json
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
ET = ZoneInfo("America/New_York")


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def _depth(symbols):
    from undertow.collect.longbridge_quote import fetch_depth
    d = fetch_depth(symbols)
    return {s: {"bid": getattr(d.get(s), "bid", None), "ask": getattr(d.get(s), "ask", None),
                "error": getattr(d.get(s), "error", None)} for s in symbols}


def _close_price(underlying: str):
    from undertow.collect.longbridge_quote import fetch_stock_quotes
    q = fetch_stock_quotes([underlying]).get(underlying)
    return getattr(q, "last", None) if q else None


def step(p: dict, now: datetime, *, depth=_depth, close_price=_close_price) -> str | None:
    """推进一步；返回动作名（None = 没到点）。纯逻辑 + 注入的取数函数，便于测试。"""
    et = now.astimezone(ET)
    today = et.date()
    width = abs(p["k_sell"] - p["k_buy"])
    st = p["state"]
    ev = p.setdefault("events", [])
    if st == "planned":
        lo, hi = (_hm(x) for x in p["entry_window_et"])
        if today != date.fromisoformat(p["entry_date"]) or et.time() < lo:
            return None
        if et.time() >= hi:
            p["state"] = "skipped"; p["skip_reason"] = "no_quote（窗口内未取得有效报价）"
            ev.append({"at": now.isoformat(), "action": "skip", "why": p["skip_reason"]})
            return "skip"
        q = depth([p["sell"], p["buy"]])
        sb, ba = q[p["sell"]]["bid"], q[p["buy"]]["ask"]
        ev.append({"at": now.isoformat(), "action": "entry_quote", "quotes": q})
        if sb is None or ba is None:
            return "retry"
        credit = round(sb - ba, 4)
        if credit / width < p["min_credit_ratio"]:
            p["state"] = "skipped"; p["skip_reason"] = f"credit_low（{credit:.2f}/{width:g} < {p['min_credit_ratio']:.0%}）"
            ev.append({"at": now.isoformat(), "action": "skip", "why": p["skip_reason"]})
            return "skip"
        fee = p["fee_round_trip"] / 100
        p.update(state="entered", entered_at=now.isoformat(), entry_credit=credit,
                 max_loss_usd=round((width - credit) * 100 * p["qty"] + p["fee_round_trip"], 2),
                 breakeven=round(p["k_sell"] - credit + fee if p["side"] == "P" else p["k_sell"] + credit - fee, 4),
                 stop_value=round(p["stop_mult"] * credit, 4))
        ev.append({"at": now.isoformat(), "action": "enter", "credit": credit})
        return "enter"
    if st != "entered":
        return None
    exp = date.fromisoformat(p["expiry"])
    if today == exp and et.time() >= time(16, 20):
        s = close_price(p["underlying"])
        if s is None:
            return "retry"
        if p["side"] == "P":
            val = max(0.0, p["k_sell"] - s) - max(0.0, p["k_buy"] - s)
        else:
            val = max(0.0, s - p["k_sell"]) - max(0.0, s - p["k_buy"])
        pnl = round((p["entry_credit"] - val) * 100 * p["qty"] - p["fee_round_trip"], 2)
        p.update(state="settled", settled_at=now.isoformat(), settle_underlying=s, settle_value=round(val, 4), pnl_usd=pnl)
        ev.append({"at": now.isoformat(), "action": "settle", "underlying_close": s, "value": val, "pnl": pnl})
        return "settle"
    done = {e.get("slot") for e in ev if e.get("action") == "mark"}
    for slot in p["mark_slots_et"]:
        key = f"{today.isoformat()} {slot}"
        t0 = _hm(slot)
        if key in done or et.time() < t0 or (et.hour * 60 + et.minute) > t0.hour * 60 + t0.minute + 15:
            continue
        if today > exp:
            continue
        q = depth([p["sell"], p["buy"]])
        sa, bb = q[p["sell"]]["ask"], q[p["buy"]]["bid"]
        if sa is None or bb is None:
            ev.append({"at": now.isoformat(), "action": "mark_retry", "slot": key, "quotes": q})
            return "retry"
        val = round(sa - bb, 4)
        ev.append({"at": now.isoformat(), "action": "mark", "slot": key, "quotes": q, "value": val})
        if val >= p["stop_value"]:
            pnl = round((p["entry_credit"] - val) * 100 * p["qty"] - p["fee_round_trip"], 2)
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
    elif p["state"] == "skipped":
        t["outcome"] = "未执行"
        t["review"] = (t.get("review") or "") + f"【模拟仓未入场】{p.get('skip_reason')}"


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
                a = step(p, now)
            except Exception as e:                      # 取数失败：留痕并在下次唤醒重试
                p.setdefault("events", []).append({"at": now.isoformat(), "action": "error", "error": f"{type(e).__name__}: {e}"[:200]})
                a = "error"
            if a:
                changed = True
                if a in ("enter", "skip", "stop", "settle"):
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
            print(t["id"], p["state"], {k: p.get(k) for k in ("entry_credit", "breakeven", "stop_value", "pnl_usd", "skip_reason")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
