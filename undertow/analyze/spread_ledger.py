"""卖方价差推荐台账 —— 逐日记录研报给出的候选，事后回填结果。

用户 2026-09-02：「记录每次研报里的推荐，收集数据」。

为什么要有：策略的三步规则是在 43 个可交易日的历史上定出来的，
样本期 SLV +10.6%（含末段 −7.7% 的下跌）。要判断它在真实行情里成不成立，
必须有【事前记录、事后回填】的前瞻台账 —— 回测再怎么做都是事后的。

与 signal_ledger 同构：
  record()   每天研报生成时调用，把当日候选（或"无候选"及其原因）落盘
  backfill() 事后用真实收盘价回填：到期收盘、是否破卖腿、实际损益
  summarize() 统计命中与损益

⚠️ 记录的是【推荐】，不是【成交】。用户是否下单、以什么价成交，
   要以券商成交回报为准（那属于 journal 模块）。这里只回答一个问题：
   "如果每次都照着做，结果会怎样"。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, is_dataclass
from datetime import date

DIR = pathlib.Path("data/history/wall_spread")


def _path(inst: str) -> pathlib.Path:
    return DIR / f"{inst}.jsonl"


def record(inst: str, sym: str, session: date, spot: float, verdict,
           *, root: pathlib.Path | None = None) -> pathlib.Path:
    """落盘某品种某可交易日的推荐。同日重复调用会覆盖该日记录（研报可能重跑）。

    spot 必须是决策价（C[可交易日前一交易日] 收盘），与回测口径一致。
    """
    d = root or DIR
    d.mkdir(parents=True, exist_ok=True)
    path = (d / f"{inst}.jsonl")
    row = {
        "date": session.isoformat(), "inst": inst, "sym": sym,
        "spot": round(float(spot), 4),
        "ok": bool(verdict.ok), "reason": verdict.reason,
        "params": {k: (list(v) if isinstance(v, tuple) else v)
                   for k, v in (verdict.params or {}).items()},
        "candidates": [
            {"kind": c.kind, "expiry": c.expiry.isoformat(), "dte": c.dte,
             "sell": c.sell, "buy": c.buy, "wall": c.wall, "offset": c.offset,
             "width_n": c.width_n, "wall_rule": c.wall_rule,
             "credit": round(c.credit, 2), "width": round(c.width, 2),
             "occ": round(c.occupancy, 2), "buf_pct": round(c.buffer_pct, 3),
             "net_credit": round(c.net_credit, 2),
             "breakeven_rate": round(c.breakeven_rate, 4),
             # 事后回填
             "settle": None, "broke": None, "pnl": None}
            for c in verdict.all],
    }
    rows = [r for r in load(inst, root=d) if r.get("date") != row["date"]]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                    + "\n", encoding="utf-8")
    return path


def load(inst: str, *, root: pathlib.Path | None = None) -> list[dict]:
    path = (root or DIR) / f"{inst}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def backfill(inst: str, closes: dict[str, float],
             *, root: pathlib.Path | None = None) -> tuple[int, int]:
    """用真实收盘价回填每个候选的结果。返回 (回填条数, 仍待填条数)。

    破卖腿 = 到期日收盘越过卖腿（与回测口径一致，不用盘中价）。
    损益 = 权利金 − 内在价值 − 手续费（持有到期口径；提前平仓另记）。
    """
    from undertow.analyze.wall_spread import FEE_PER_TRADE
    rows = load(inst, root=root)
    if not rows:
        return 0, 0
    filled = pending = 0
    for r in rows:
        for c in r.get("candidates", []):
            if c.get("pnl") is not None:
                continue
            se = closes.get(c["expiry"])
            if se is None:
                pending += 1
                continue
            w = c["width"] / 100.0
            itr = (max(0.0, min(c["sell"] - se, w)) if c["kind"] == "P"
                   else max(0.0, min(se - c["sell"], w))) * 100
            c["settle"] = round(se, 4)
            c["broke"] = bool(se < c["sell"]) if c["kind"] == "P" else bool(se > c["sell"])
            c["pnl"] = round(c["credit"] - itr - FEE_PER_TRADE, 2)
            filled += 1
    path = (root or DIR) / f"{inst}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
                    + "\n", encoding="utf-8")
    return filled, pending


def summarize(inst: str, *, root: pathlib.Path | None = None) -> dict:
    """统计已回填的推荐。样本不足就说不足，不给结论。"""
    rows = load(inst, root=root)
    days = len(rows)
    with_cand = sum(1 for r in rows if r.get("ok"))
    done = [c for r in rows for c in r.get("candidates", [])
            if c.get("pnl") is not None]
    if not done:
        return {"days": days, "days_with_candidate": with_cand,
                "settled": 0, "note": "尚无已回填的候选"}
    broke = sum(1 for c in done if c.get("broke"))
    for kind in ("P", "C"):
        pass
    return {
        "days": days, "days_with_candidate": with_cand,
        "coverage": round(with_cand / days, 3) if days else 0.0,
        "settled": len(done), "broke": broke,
        "break_rate": round(broke / len(done), 4),
        "total_pnl": round(sum(c["pnl"] for c in done), 2),
        "avg_pnl": round(sum(c["pnl"] for c in done) / len(done), 2),
        "put": sum(1 for c in done if c["kind"] == "P"),
        "call": sum(1 for c in done if c["kind"] == "C"),
    }
