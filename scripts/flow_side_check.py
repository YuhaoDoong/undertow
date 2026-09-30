"""资金流买卖方推断验证（协议 docs/prereg/2026-09-29_flow_side_check_v0.md；探索，描述性）。

  python3 scripts/flow_side_check.py 2026-09-29 [gold silver]

交易日 X：用认证到 X 与 X+1 的两份快照（X+1 那份含 X 的结算，所以 X+1 开盘前才可算），经冻结的 analyze_flow + grade_leg
重建 F 层的腿（到期日取自 fa.changes，不改冻结代码）；与 X 当天的逐分钟成交主动方（报价规则优先、tick 规则兜底）逐腿对比。
结果写 data/history/flow_side/<X>_<品种>.json（逐腿原始字段 + 汇总），按日累积，不挑日子。只读。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data/history/flow_side"
NEAR = 0.05


def run(inst_key: str, X: date) -> dict:
    from undertow.analyze import flow_side as fs
    from undertow.analyze.flow import analyze_flow
    from undertow.analyze.structure_read import grade_leg
    from undertow.collect import longbridge_bars as lbb
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    from undertow.core import market_calendar as mc
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    cfg, s = load_config(), SnapshotStore()
    root = cfg.instruments[inst_key].options.symbol
    idx = session_index(s, root)
    X1 = mc.next_trading_day(X)
    if X not in idx or X1 not in idx:
        return {"instrument": inst_key, "day": X.isoformat(), "status": "snapshot_missing",
                "why": f"需要认证到 {X} 与 {X1} 的两份快照"}
    prev = snapshot_from_payload(s.load("options", root, idx[X]), inst_key, root)
    curr = snapshot_from_payload(s.load("options", root, idx[X1]), inst_key, root)
    fa = analyze_flow(prev, curr, today=X, prev_date="prev", curr_date="curr")
    spot = fa.spot
    intra = lbb.load_day(lbb.path_of(root, X, lbb.INTRADAY_DIR)) or {"contracts": {}}
    quotes: dict = {}
    sp = ROOT / "data/history/shadow_samples" / X.isoformat() / f"{inst_key}.jsonl"
    if sp.exists():
        for line in sp.read_text("utf-8").splitlines():
            r = json.loads(line)
            t = datetime.fromisoformat(r["started_at"])
            for sym, q in (r.get("quotes") or {}).items():
                quotes.setdefault(sym, []).append((t, q.get("bid"), q.get("ask")))
    legs = []
    for c in fa.changes:
        l = grade_leg(c)
        if not l.counts or abs(l.strike / spot - 1) > NEAR or l.purity is None:
            continue
        sym = lbb.option_symbol(root, c.expiry.isoformat(), c.kind, c.strike)
        rows = [(datetime.fromisoformat(x[0].replace("Z", "+00:00")), x[1], x[2])
                for x in ((intra["contracts"].get(sym) or {}).get("rows") or [])]
        cl = fs.classify_minutes(rows, quotes.get(sym, [])) if rows else None
        share = fs.buy_share(cl) if cl else None
        legs.append({"symbol": sym, "expiry": c.expiry.isoformat(), "kind": c.kind, "strike": c.strike, "d_oi": c.d_oi,
                     "delta": round(l.delta, 4), "delta_adj_pp": round(l.delta_adj_pp, 4), "purity": l.purity,
                     "weight": abs(l.d_oi * l.delta), "inferred": fs.inferred_label(l.delta_adj_pp),
                     "has_data": bool(rows), "classified": cl, "buy_share": None if share is None else round(share, 4),
                     "traded": fs.trade_label(share) if rows else "unknown"})
    return {"instrument": inst_key, "root": root, "day": X.isoformat(), "status": "ok", "spot": spot,
            "protocol": "docs/prereg/2026-09-29_flow_side_check_v0.md", "computed_at": datetime.now(timezone.utc).isoformat(),
            "summary": fs.agreement(legs), "legs": legs}


def main():
    X = date.fromisoformat(sys.argv[1])
    insts = sys.argv[2:] or ["gold", "silver"]
    OUT.mkdir(parents=True, exist_ok=True)
    for k in insts:
        r = run(k, X)
        (OUT / f"{X.isoformat()}_{k}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), "utf-8")
        if r["status"] != "ok":
            print(f"{k} {X}：{r['status']}（{r['why']}）"); continue
        sm = r["summary"]
        cov = sum(1 for l in r["legs"] if l["has_data"])
        print(f"{k} {X}：F 腿 {sm['n_legs']}（有逐分钟 {cov}，覆盖 F 权重 {sm['weight_covered']}）；已分类 {sm['n_labelled']}，"
              f"一致 {sm['n_agree']} → 一致率 {sm['agree_rate']}（加权 {sm['agree_rate_weighted']}）；mixed {sm['n_mixed']}、unknown {sm['n_unknown']}")
    print("（描述性；协议要求 ≥10 个交易日、已分类 ≥200 之前不下结论）")


if __name__ == "__main__":
    main()
