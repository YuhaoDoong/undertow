"""ps-trigger-causal-v1：修复 Codex 042 P1（limit 前视、候选提前占位）后，按 ps-trigger-v1 / v2 / ps-mirror-v1 的原固定配置重跑，
并与旧 run 逐笔对照。配置 data/backtest/ps-trigger-causal-v1/config.json（运行前提交；参数从配置读取并断言）。

  python3 scripts/ps_trigger_causal.py --run-id run1

旧 run 原文件保留不改（标 timing_invalid / limit invalid_execution_model）。描述性，无 p 值，未扣费，≤2015 设计集。
run-id 目录存在即拒绝。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics as st
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from scripts import ps_trigger as v1  # noqa: E402
from scripts import ps_trigger_v2 as v2  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

BASE = ROOT / "data/backtest/ps-trigger-causal-v1"
CFG = BASE / "config.json"
HOLD = {"stop": "extreme", "target": False, "struct_exit": False, "breakeven": False, "hold": 250}
OLD = {"v1": ROOT / "data/backtest/ps-trigger-v1/run1/trades.jsonl", "v2": ROOT / "data/backtest/ps-trigger-v2/run1/trades.jsonl",
       "mirror": ROOT / "data/backtest/ps-mirror-v1/run1_mirror/trades.jsonl"}
CODE = ("undertow/analyze/price_structure.py", "scripts/ps_trigger_causal.py", "scripts/ps_trigger.py", "scripts/ps_trigger_v2.py",
        "scripts/ps_explore.py")


def load_cfg() -> dict:
    c = json.loads(CFG.read_text("utf-8"))
    # 参数从配置读，并与旧脚本常量逐项断言（Codex 042 §5：不能只记哈希、参数仍硬编码）
    assert tuple(c["methods"]) == v1.METHODS, "methods 与 ps-trigger-v1 不一致"
    assert tuple(c["tolerances_v1"]) == v1.TOLS, "tolerances 与 ps-trigger-v1 不一致"
    assert c["tolerance_v2"] == v2.TOL
    assert {k: tuple(v) for k, v in c["groups"].items()} == v1.GROUPS
    assert c["hold_admin_cap"] == HOLD["hold"] == ps.POLICIES["U6"]["hold"]
    assert c["wait"] == ps.WAIT_MAX
    return c


def _stop(ep, atr, ei):
    s = ps._sign(ep.direction)
    return ep.extreme.price - s * ps.STOP_BUF * atr[ei - 1]


def schedule(cands: list, occupy) -> list:
    """cands：[(ep, fe)]，fe = fast_entry_causal 结果。按实际入场根顺序处理（同根按突破先后）；只有入场成功才占位。
    occupy(ep, fe) → 退出结果 dict（status entered 时含 exit_i）。返回逐候选行（含未入场与被占用）。"""
    out = []
    for ep, fe in cands:
        if fe["status"] != "entered":
            out.append({"ep": ep, "fe": fe, "sched": fe["status"], "res": None})
    busy_until, by = -1, None
    for ep, fe in sorted([c for c in cands if c[1]["status"] == "entered"], key=lambda c: (c[1]["entry_i"], c[0].break_at)):
        if fe["entry_i"] <= busy_until:
            out.append({"ep": ep, "fe": fe, "sched": "busy_skipped", "res": None, "blocked_by_break": by})
            continue
        res = occupy(ep, fe)
        out.append({"ep": ep, "fe": fe, "sched": res["status"], "res": res})
        if res["status"] in ("entered", "filled_through_stop"):
            busy_until, by = res["exit_i"], ep.break_at
    return sorted(out, key=lambda r: r["ep"].break_at)


def _through_stop(ep, fe, stop):
    """limit 在开盘已越过止损时成交：当根按开盘离场，0 ATR（不当作没成交）。"""
    return {"status": "filled_through_stop", "entry_i": fe["entry_i"], "entry": fe["entry"], "stop": stop, "exit_i": fe["entry_i"],
            "exit_price": fe["entry"], "exit_reason": "filled_through_stop", "days_held": 1, "signals": {}, "audit": {}}


def exit_fast(bars, atr, loc, tol, last_i, method):
    def f(ep, fe):
        ei, epx, s = fe["entry_i"], fe["entry"], ps._sign(ep.direction)
        stop = _stop(ep, atr, ei)
        if s * (epx - stop) <= 0:
            return _through_stop(ep, fe, stop) if method == "limit" else {"status": "open_beyond_stop"}
        if ei + HOLD["hold"] - 1 > last_i:
            return {"status": "boundary_excluded"}
        return ps._fast_exit(bars, atr, loc, ei, epx, stop, ep.direction, tol, hold=HOLD["hold"])
    return f


def exit_slow(bars, atr, piv, last_i, method):
    def f(ep, fe):
        ei, epx, s = fe["entry_i"], fe["entry"], ps._sign(ep.direction)
        stop = _stop(ep, atr, ei)
        if s * (epx - stop) <= 0:
            return _through_stop(ep, fe, stop) if method == "limit" else {"status": "open_beyond_stop"}
        r = ps.simulate_policy(bars, piv, ei, stop, ep.direction, ps.POLICIES["U6"], last_i=last_i, entry_price=epx)
        if r["status"] == "entered":
            r["entry_i"] = ei
        return r
    return f


def _atr_units(res, ep, atr):
    if not res or res["status"] not in ("entered", "filled_through_stop"):
        return None
    s = ps._sign(ep.direction)
    return s * (res["exit_price"] - res["entry"]) / atr[res["entry_i"] - 1]


def _hold(bars, piv, ep, fe, atr, last_i):
    ei, epx = fe["entry_i"], fe["entry"]
    h = ps.simulate_policy(bars, piv, ei, _stop(ep, atr, ei), ep.direction, HOLD, last_i=last_i, entry_price=epx)
    return ps._sign(ep.direction) * (h["exit_price"] - epx) / atr[ei - 1] if h["status"] == "entered" else None


def _row(sym, si, bars, atr, loc, r, method, tol, *, extra=None):
    ep, fe, res = r["ep"], r["fe"], r["res"]
    s = ps._sign(ep.direction)
    row = {"symbol": sym, "seg": si, "direction": ep.direction, "break": str(bars[ep.break_at][0]), "break_i": ep.break_at,
           "method": method, "tol": tol, "status": "entered" if r["sched"] in ("entered", "filled_through_stop") else r["sched"],
           "sched": r["sched"], "entry_audit": fe.get("audit")}
    if r.get("blocked_by_break") is not None:
        row["blocked_by_break"] = str(bars[r["blocked_by_break"]][0])
    if fe["status"] == "entered":
        row["planned_entry_date"] = str(bars[fe["entry_i"]][0])
    if row["status"] == "entered":
        ei = res["entry_i"]
        A = atr[ei - 1]
        lows = [p for p in loc if p.kind == ("L" if s > 0 else "H") and p.idx > ep.break_at and p.conf < ei]
        sw = lows[-1] if lows else None
        row.update(entry_date=str(bars[ei][0]), exit_date=str(bars[res["exit_i"]][0]), exit_reason=res["exit_reason"],
                   entry=res["entry"], stop=res.get("stop"), atr_at_entry=A, atr_units=_atr_units(res, ep, atr), days=res["days_held"],
                   entry_from_low_atr=(s * (res["entry"] - sw.price) / A) if sw else None,
                   entry_bars_after_low=(ei - sw.idx) if sw else None,
                   exit_signals=res.get("signals") or res.get("audit") or {})
    if extra:
        row.update(extra)
    return row


def run_symbol(sym, ref_dates, *, mirror=False):
    bars_all = pe.parse(pe.fetch(sym)[0])[0]
    if mirror:
        bars_all = v2.mirror(bars_all)
    assert bars_all[-1][0] <= pe.DESIGN_END
    segs, _ = pe.segments(bars_all, [b[0] for b in bars_all] if sym in pe.FUTURES else ref_dates)
    rows = {"v1": [], "v2": []}
    for si, bars in enumerate(segs):
        if len(bars) < ps.WARMUP + 5:
            continue
        atr = ps.wilder_atr(bars)
        piv, eps = ps.detect(bars, atr)
        last_i = len(bars) - 1
        A_eps = sorted([e for e in eps if e.kind == "A"], key=lambda e: e.break_at)
        for m in v1.METHODS:
            loc = ps.local_swings(bars, atr, "bar" if m == "limit" else m)
            for d in ("long", "short"):
                eps_d = [e for e in A_eps if e.direction == d]
                tols = v1.TOLS if not mirror else ()
                for tol in tols:                                                   # v1：快离场占位
                    c = [(e, ps.fast_entry_causal(bars, atr, e, loc, m, tol, last_i=last_i)) for e in eps_d]
                    for r in schedule(c, exit_fast(bars, atr, loc, tol, last_i, m)):
                        x = _row(sym, si, bars, atr, loc, r, m, tol)
                        if x["status"] == "entered":
                            x["hold_atr"] = _hold(bars, piv, r["ep"], r["fe"], atr, last_i)
                        rows["v1"].append(x)
                tol = v2.TOL                                                       # v2 / 镜像：慢离场占位，快离场与只拿着同入场配对
                c = [(e, ps.fast_entry_causal(bars, atr, e, loc, m, tol, last_i=last_i)) for e in eps_d]
                fx = exit_fast(bars, atr, loc, tol, last_i, m)
                for r in schedule(c, exit_slow(bars, atr, piv, last_i, m)):
                    x = _row(sym, si, bars, atr, loc, r, m, tol)
                    if x["status"] == "entered":
                        x["hold_atr"] = _hold(bars, piv, r["ep"], r["fe"], atr, last_i)
                        fres = fx(r["ep"], r["fe"])
                        x["fast_atr"] = _atr_units(fres, r["ep"], atr)
                        x["fast_exit_date"] = str(bars[fres["exit_i"]][0]) if fres.get("exit_i") is not None else None
                    rows["v2"].append(x)
    return rows


def compare(new: list, old_path: Path, keyf) -> dict:
    """旧新逐笔对照：旧 run 只落了未被占用的候选（被占用的 continue 掉了），所以旧缺行 = 旧版被提前占位或未评估。"""
    old = {}
    for line in old_path.read_text("utf-8").splitlines():
        r = json.loads(line)
        old[keyf(r)] = r
    nw = {keyf(r): r for r in new}
    cats: dict = {}
    deltas = []
    by_method: dict = {}
    for k in set(old) | set(nw):
        o, n = old.get(k), nw.get(k)
        os_ = o["status"] if o else "absent_in_old（旧版提前占位，未评估）"
        ns_ = n["status"] if n else "absent_in_new"
        key = f"{'entered' if os_ == 'entered' else 'not:' + os_} → {'entered' if ns_ == 'entered' else 'not:' + (n or {}).get('sched', ns_)}"
        cats[key] = cats.get(key, 0) + 1
        m = (n or o)["method"]
        bm = by_method.setdefault(m, {"old_entered": 0, "new_entered": 0, "both": 0, "old_mean": [], "new_mean": [], "pair_delta": []})
        bm["old_entered"] += os_ == "entered"; bm["new_entered"] += ns_ == "entered"
        if os_ == "entered":
            bm["old_mean"].append(o["atr_units"])
        if ns_ == "entered":
            bm["new_mean"].append(n["atr_units"])
        if os_ == "entered" and ns_ == "entered":
            bm["both"] += 1
            dd = n["atr_units"] - o["atr_units"]
            bm["pair_delta"].append(dd)
            if abs(dd) > 1e-9:
                deltas.append({"key": list(k), "old": round(o["atr_units"], 4), "new": round(n["atr_units"], 4),
                               "old_entry": o.get("entry_date"), "new_entry": n.get("entry_date")})
    for m, bm in by_method.items():
        for f in ("old_mean", "new_mean", "pair_delta"):
            xs = bm[f]
            bm[f] = round(st.mean(xs), 4) if xs else None
        bm["pair_changed"] = sum(1 for d in deltas if d["key"][3] == m)
    return {"transitions": dict(sorted(cats.items(), key=lambda kv: -kv[1])), "by_method": by_method,
            "changed_pairs_sample": sorted(deltas, key=lambda d: -abs(d["new"] - d["old"]))[:30], "n_changed_pairs": len(deltas)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    cfg = load_cfg()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    ref_dates = [b[0] for b in pe.parse(pe.fetch("SPY")[0])[0]]
    rows = {"v1": [], "v2": [], "mirror": []}
    syms = [s for g in v1.GROUPS.values() for s in g]
    for s in syms:
        r = run_symbol(s, ref_dates)
        rows["v1"] += r["v1"]; rows["v2"] += r["v2"]
        rows["mirror"] += run_symbol(s, ref_dates, mirror=True)["v2"]
    out.mkdir(parents=True)
    inputs = json.loads(pe.INPUT_SHA.read_text("utf-8"))
    meta = {"run_id": a.run_id, "version": cfg["version"], "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
            "config_sha256": hashlib.sha256(CFG.read_bytes()).hexdigest(),
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in CODE},
            "input_sha256": {s: inputs.get(s) for s in syms},
            "n_rows": {k: len(v) for k, v in rows.items()}}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    cmp_ = {}
    keys = {"v1": lambda r: (r["symbol"], r["direction"], r["break"], r["method"], r["tol"]),
            "v2": lambda r: (r["symbol"], r["direction"], r["break"], r["method"]),
            "mirror": lambda r: (r["symbol"], r["direction"], r["break"], r["method"])}
    for k, rs in rows.items():
        with open(out / f"trades_{k}.jsonl", "w", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        cmp_[k] = compare(rs, OLD[k], keys[k])
    summ = {"v1": v1.summarize(rows["v1"]), "v2": v2.summarize(rows["v2"]), "mirror": v2.summarize(rows["mirror"])}
    for k, rs in rows.items():
        summ[k + "_sched"] = {}
        for r in rs:
            kk = f"{r['method']}|{r['tol']}"
            d = summ[k + "_sched"].setdefault(kk, {})
            d[r["sched"]] = d.get(r["sched"], 0) + 1
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), "utf-8")
    (out / "compare_old.json").write_text(json.dumps(cmp_, ensure_ascii=False, indent=1), "utf-8")
    m1 = {**meta, "run_id": f"{a.run_id}（因果修复，v1 原配置）"}
    m2 = {**meta, "run_id": f"{a.run_id}（因果修复，v2 原配置）"}
    head = ("> **ps-trigger-causal-v1**：修复 limit 前视与候选提前占位后、按原固定配置重跑（Codex 042）。旧 run1 标 timing_invalid，"
            "其 limit 另标 invalid_execution_model。逐笔对照见 compare_old.json；调度状态计数见 summary.json 的 *_sched。\n\n")
    (out / "report_v1.md").write_text(head + v1.report(summ["v1"], m1), "utf-8")
    (out / "report_v2.md").write_text(head + v2.report(summ["v2"], m2), "utf-8")
    (out / "report_mirror.md").write_text(head + v2.report(summ["mirror"], {**m2, "run_id": f"{a.run_id}（镜像）"}), "utf-8")
    print(json.dumps(meta["n_rows"]), "→", out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
