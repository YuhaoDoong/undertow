"""资金流「相对 IV 推断」与「分钟方向代理」的一致性（协议 v0 + v1 附录 docs/prereg/2026-09-30_flow_side_check_v1_addendum.md）。

  python3 scripts/flow_side_check.py 2026-09-29 [gold silver]

不是真实买卖方验证：分钟收盘价 × 整分钟量只是代理。交易日 X：用认证到 X 与 X+1 的两份快照，经冻结的 analyze_flow + grade_leg
重建 F 层的腿（到期日取自 fa.changes，不改冻结代码）；与 X 当天逐分钟成交的四种代理分类逐腿对比，
主表 = quote_past（只用严格过去的报价）。F 腿中不在采集计划里的按原因分类并报告权重。
结果写 data/history/flow_side/v1/<X>_<品种>.json（原子写 + 回读、输入哈希）；输入不全 → 不覆盖已有结果，退出码 3（pending）。
只读行情文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data/history/flow_side/v1"
NEAR = 0.05
VERSION = "flow-side-proxy-v1-20260930"


def _abs(p: Path) -> Path:
    return p if p.is_absolute() else ROOT / p


def _rel(p: Path) -> str:
    return str(_abs(p).relative_to(ROOT))


def _sha(p: Path) -> str | None:
    p = _abs(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _atomic_json(path: Path, obj: dict) -> None:
    raw = json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    if Path(name).read_text("utf-8") != raw:
        Path(name).unlink(missing_ok=True)
        raise RuntimeError(f"{path} 回读校验失败，未替换")
    os.replace(name, path)


def run(inst_key: str, X: date) -> dict:
    from undertow.analyze import flow_side as fs
    from undertow.analyze.expiry_type import classify
    from undertow.analyze.flow import analyze_flow
    from undertow.analyze.structure_read import grade_leg
    from undertow.collect import longbridge_bars as lbb
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    from undertow.core import market_calendar as mc
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    from undertow.shadow_cli import FLOWSIDE_BAND, FLOWSIDE_MAX_DTE, FLOWSIDE_TYPES, flowside_plan
    cfg, s = load_config(), SnapshotStore()
    root = cfg.instruments[inst_key].options.symbol
    idx = session_index(s, root)
    X1 = mc.next_trading_day(X)
    base = {"version": VERSION, "instrument": inst_key, "root": root, "day": X.isoformat(),
            "protocol": ["docs/prereg/2026-09-29_flow_side_check_v0.md", "docs/prereg/2026-09-30_flow_side_check_v1_addendum.md"]}
    ipath = lbb.path_of(root, X, lbb.INTRADAY_DIR)
    if X not in idx or X1 not in idx:
        return {**base, "status": "pending", "why": f"需要认证到 {X} 与 {X1} 的两份快照"}
    if not _abs(ipath).exists():
        return {**base, "status": "pending", "why": f"{X} 逐分钟文件不存在"}
    p_prev, p_curr = s.path_of("options", root, idx[X]), s.path_of("options", root, idx[X1])
    prev = snapshot_from_payload(s.load("options", root, idx[X]), inst_key, root)
    curr = snapshot_from_payload(s.load("options", root, idx[X1]), inst_key, root)
    fa = analyze_flow(prev, curr, today=X, prev_date="prev", curr_date="curr")
    spot = fa.spot
    intra = lbb.load_day(ipath) or {"contracts": {}}
    sp = ROOT / "data/history/shadow_samples" / X.isoformat() / f"{inst_key}.jsonl"
    quotes: dict = {}
    if sp.exists():
        for line in sp.read_text("utf-8").splitlines():
            r = json.loads(line)
            t0, t1 = datetime.fromisoformat(r["started_at"]), datetime.fromisoformat(r["ended_at"])
            for sym, q in (r.get("quotes") or {}).items():
                if not q.get("error"):
                    quotes.setdefault(sym, []).append((t0, t1, q.get("bid"), q.get("ask")))
    _, plan_syms, plan_spot = flowside_plan(inst_key, X)
    plan_set = set(plan_syms)
    listed_prev = {lbb.option_symbol(root, c.expiry.isoformat(), c.kind, c.strike) for c in prev.contracts}
    legs = []
    for c in fa.changes:
        l = grade_leg(c)
        if not l.counts or abs(l.strike / spot - 1) > NEAR or l.purity is None:
            continue
        sym = lbb.option_symbol(root, c.expiry.isoformat(), c.kind, c.strike)
        if sym in plan_set:
            why_out = None
        elif sym not in listed_prev:
            why_out = "new_listing（采集依据的快照里没有）"
        elif classify(c.expiry)["type"] not in FLOWSIDE_TYPES:
            why_out = "D_expiry（日度到期不在当日采集计划：9/30 前只抓 Q/M/W，之后抓 10 天内的 D）"
        elif (c.expiry - X).days > FLOWSIDE_MAX_DTE:
            why_out = "beyond_45d"
        elif plan_spot and abs(c.strike / plan_spot - 1) > FLOWSIDE_BAND:
            why_out = "outside_band_at_capture（按采集时现价在 ±5% 外）"
        else:
            why_out = "other"
        rows = [(datetime.fromisoformat(x[0].replace("Z", "+00:00")), x[1], x[2])
                for x in ((intra["contracts"].get(sym) or {}).get("rows") or [])]
        modes = {m: fs.classify_v1(rows, quotes.get(sym, []), m) for m in fs.MODES} if rows else {}
        lab = {}
        for m, cl in modes.items():
            sh = fs.minute_sign_volume_share(cl)
            tot = cl["buy"] + cl["sell"] + cl["unclassified"]
            lab[m] = {"share": None if sh is None else round(sh, 4), "label": fs.trade_label(sh),
                      "classified_frac": round((cl["buy"] + cl["sell"]) / tot, 4) if tot else None,
                      "volume": tot, "quote_age_min_s_median": (sorted(cl["quote_age_min_s"])[len(cl["quote_age_min_s"]) // 2]
                                                                 if cl["quote_age_min_s"] else None)}
        legs.append({"symbol": sym, "expiry": c.expiry.isoformat(), "kind": c.kind, "strike": c.strike, "d_oi": c.d_oi,
                     "delta": round(l.delta, 4), "delta_adj_pp": round(l.delta_adj_pp, 4), "purity": l.purity,
                     "weight": abs(l.d_oi * l.delta), "inferred": fs.inferred_label(l.delta_adj_pp),
                     "in_capture_plan": why_out is None, "not_in_plan_reason": why_out, "has_data": bool(rows),
                     "proxy": lab})
    summary = {}
    for m in fs.MODES:
        rs = [{"inferred": l["inferred"], "traded": (l["proxy"].get(m) or {}).get("label", "unknown"),
               "weight": l["weight"], "has_data": l["has_data"]} for l in legs if l["in_capture_plan"]]
        summary[m] = {**fs.agreement(rs), **fs.confusion(rs)}
    out_plan = {}
    for l in legs:
        if not l["in_capture_plan"]:
            k = l["not_in_plan_reason"].split("（")[0]
            o = out_plan.setdefault(k, {"n": 0, "weight": 0.0})
            o["n"] += 1; o["weight"] += l["weight"]
    w_all = sum(l["weight"] for l in legs)
    return {**base, "status": "ok", "spot": spot, "computed_at": datetime.now(timezone.utc).isoformat(),
            "inputs": {"snapshot_prev": {"path": _rel(p_prev), "sha256": _sha(p_prev)},
                       "snapshot_curr": {"path": _rel(p_curr), "sha256": _sha(p_curr)},
                       "intraday": {"path": _rel(ipath), "sha256": _sha(ipath)},
                       "samples": {"path": _rel(sp), "sha256": _sha(sp)},
                       "capture_plan_spot": plan_spot, "capture_plan_n": len(plan_syms)},
            "f_legs_total": len(legs), "f_weight_total": w_all,
            "f_legs_in_plan": sum(l["in_capture_plan"] for l in legs),
            "f_weight_in_plan_frac": round(sum(l["weight"] for l in legs if l["in_capture_plan"]) / w_all, 4) if w_all else None,
            "not_in_plan": out_plan, "summary_by_mode": summary, "main_mode": "quote_past", "legs": legs}


def main():
    X = date.fromisoformat(sys.argv[1])
    insts = sys.argv[2:] or ["gold", "silver"]
    rc = 0
    for k in insts:
        r = run(k, X)
        path = OUT / f"{X.isoformat()}_{k}.json"
        if r["status"] != "ok":
            print(f"{k} {X}：pending（{r['why']}）——不写结果、不覆盖已有文件")
            rc = max(rc, 3); continue
        _atomic_json(path, r)
        sm = r["summary_by_mode"]
        print(f"{k} {X}：F 腿 {r['f_legs_total']}，在采集子集内 {r['f_legs_in_plan']}（F 权重 {r['f_weight_in_plan_frac']}）；"
              f"子集外 {r['not_in_plan']}")
        for m in ("quote_past", "mixed", "tick_only", "quote_any_window"):
            a = sm[m]
            print(f"   {m:17s} 已分类 {a['n_labelled']}，一致 {a['n_agree']} → {a['agree_rate']}（加权 {a['agree_rate_weighted']}）；"
                  f"代理买方基准 {a['base_rate_traded_buy']}，推断买方基准 {a['base_rate_inferred_buy']}"
                  + ("  ← 主表" if m == "quote_past" else ("  （含之后的报价，只作敏感性）" if m == "quote_any_window" else "")))
    print("（代理一致性，不是真实买卖方验证；子集结果不外推整个 F 层；进度点不是结论门槛）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
