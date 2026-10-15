"""ps-entry-pair-v1：同一突破机会下，旧 U6 慢入场 vs 固定快入场的成对对照（Codex 047 D；配置 data/backtest/ps-entry-pair-v1/config.json）。

  python3 scripts/ps_entry_pair.py --run-id run1

两边同一出场（U6 慢离场）；按机会成对，不按任一变体的占位删机会。收益统一用突破根 ATR（两边共用、突破时已知）。
描述性，无 p 值，无费用模型；≤2015 设计集。run-id 目录存在即拒绝。
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
from scripts import ps_trigger_causal as pc  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

BASE = ROOT / "data/backtest/ps-entry-pair-v1"
CFG = BASE / "config.json"
CODE = ("undertow/analyze/price_structure.py", "scripts/ps_entry_pair.py", "scripts/ps_trigger_causal.py", "scripts/ps_explore.py")
DONE = ("entered", "filled_through_stop")


def load_cfg() -> dict:
    c = json.loads(CFG.read_text("utf-8"))
    assert tuple(c["methods"]) == pc.v1.METHODS and c["tolerance"] == pc.v2.TOL
    assert {k: tuple(v) for k, v in c["groups"].items()} == pc.v1.GROUPS
    return c


def slow_trade(bars, atr, piv, ep, last_i) -> dict:
    """旧 U6 入场：2ATR 之字形确认 HL₁（episode.status=signal）→ 下一根开盘；止损按 ps-exit-v1 原定义（ATR[signal_at−1]）。"""
    if ep.status != "signal":
        return {"status": ep.status}
    sig = ep.signal_at
    ei = sig + 1
    if ei > last_i:
        return {"status": "no_entry_bar"}
    s = ps._sign(ep.direction)
    stop = ep.extreme.price - s * ps.STOP_BUF * atr[sig - 1]
    r = ps.simulate_policy(bars, piv, ei, stop, ep.direction, ps.POLICIES["U6"], last_i=last_i)
    if r["status"] == "entered":
        r["entry_i"] = ei
    r["decided_at"] = sig
    return r


def path(bars, ep, entry_i, entry, exit_i, A0) -> dict:
    """持有期 [entry_i, exit_i] 内最大不利/有利（突破根 ATR）。"""
    s = ps._sign(ep.direction)
    hi = max(b[2] for b in bars[entry_i:exit_i + 1])
    lo = min(b[3] for b in bars[entry_i:exit_i + 1])
    fav, adv = (hi - entry, lo - entry) if s > 0 else (entry - lo, entry - hi)
    return {"mfe_atr0": fav / A0, "mae_atr0": adv / A0}


def leg(bars, atr, ep, res, A0) -> dict:
    if not res or res.get("status") not in DONE:
        return {"status": (res or {}).get("status")}
    s = ps._sign(ep.direction)
    ei = res["entry_i"]
    return {"status": res["status"], "entry_i": ei, "entry_date": str(bars[ei][0]), "entry": res["entry"], "exit_i": res["exit_i"],
            "exit_date": str(bars[res["exit_i"]][0]), "exit_reason": res["exit_reason"], "stop": res.get("stop"),
            "pnl_atr0": s * (res["exit_price"] - res["entry"]) / A0, "pnl_own_atr": s * (res["exit_price"] - res["entry"]) / atr[ei - 1],
            "days": res.get("days_held"), **path(bars, ep, ei, res["entry"], res["exit_i"], A0)}


def _hold(bars, piv, ep, entry_i, entry, stop, A0, last_i):
    h = ps.simulate_policy(bars, piv, entry_i, stop, ep.direction, pc.HOLD, last_i=last_i, entry_price=entry)
    return ps._sign(ep.direction) * (h["exit_price"] - entry) / A0 if h["status"] == "entered" else None


def own_schedule(items: list, key: str) -> set:
    """各变体按自身时间流占位（同方向同时一笔、entry_i > 上一笔 exit_i）：返回会被占用跳过的机会序号（只作说明）。"""
    skipped, busy = set(), {"long": -1, "short": -1}
    for n, it in sorted([(n, it) for n, it in enumerate(items) if it[key]["status"] in DONE],
                        key=lambda x: (x[1][key]["entry_i"], x[1]["break_i"])):
        L = it[key]
        if L["entry_i"] <= busy[it["direction"]]:
            skipped.add(n)
        else:
            busy[it["direction"]] = L["exit_i"]
    return skipped


def run_symbol(sym, ref_dates) -> list:
    bars_all = pe.parse(pe.fetch(sym)[0])[0]
    assert bars_all[-1][0] <= pe.DESIGN_END
    segs, _ = pe.segments(bars_all, [b[0] for b in bars_all] if sym in pe.FUTURES else ref_dates)
    rows = []
    for si, bars in enumerate(segs):
        if len(bars) < ps.WARMUP + 5:
            continue
        atr = ps.wilder_atr(bars)
        piv, eps = ps.detect(bars, atr)
        last_i = len(bars) - 1
        A_eps = sorted([e for e in eps if e.kind == "A"], key=lambda e: e.break_at)
        slow = {id(e): slow_trade(bars, atr, piv, e, last_i) for e in A_eps}
        for m in pc.v1.METHODS:
            loc = ps.local_swings(bars, atr, "bar" if m == "limit" else m)
            ex = pc.exit_slow(bars, atr, piv, last_i, m)
            items = []
            for e in A_eps:
                A0 = atr[e.break_at]
                if A0 is None:
                    continue
                fe = ps.fast_entry_causal(bars, atr, e, loc, m, pc.v2.TOL, last_i=last_i)
                fres = ex(e, fe) if fe["status"] == "entered" else {"status": fe["status"]}
                if fres.get("status") in DONE:
                    fres.setdefault("entry_i", fe["entry_i"])
                F, S = leg(bars, atr, e, fres, A0), leg(bars, atr, e, slow[id(e)], A0)
                row = {"symbol": sym, "seg": si, "direction": e.direction, "break": str(bars[e.break_at][0]), "break_i": e.break_at,
                       "method": m, "tol": pc.v2.TOL, "atr0": A0, "fast": F, "slow": S, "fast_entry_audit": fe.get("audit"),
                       "slow_decided_at": slow[id(e)].get("decided_at")}
                f_in, s_in = F["status"] in DONE, S["status"] in DONE
                row["category"] = {(True, True): "both", (True, False): "fast_only", (False, True): "slow_only"}.get((f_in, s_in), "neither")
                if f_in:
                    F["hold_atr0"] = _hold(bars, piv, e, F["entry_i"], F["entry"], F["stop"], A0, last_i) if F["status"] == "entered" else 0.0
                if s_in:
                    S["hold_atr0"] = _hold(bars, piv, e, S["entry_i"], S["entry"], S["stop"], A0, last_i)
                if f_in and s_in:
                    s_ = ps._sign(e.direction)
                    row["pair"] = {"lead_bars": S["entry_i"] - F["entry_i"], "entry_px_adv_atr0": s_ * (S["entry"] - F["entry"]) / A0,
                                   "pnl_diff_atr0": F["pnl_atr0"] - S["pnl_atr0"]}
                items.append(row)
            for key in ("fast", "slow"):
                for n in own_schedule(items, key):
                    items[n][f"{key}_own_sched_skipped"] = True
            rows += items
    return rows


def _d(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    q = sorted(xs)
    r = lambda v: round(v, 4)
    return {"n": len(xs), "mean": r(st.mean(xs)), "median": r(st.median(xs)), "p10": r(q[len(q) // 10]), "p90": r(q[(9 * len(q)) // 10]),
            "min": r(q[0]), "max": r(q[-1]), "share_pos": r(sum(1 for x in xs if x > 0) / len(xs))}


def summarize(rows: list, groups: dict) -> dict:
    out = {}
    gof = {s: g for g, ss in groups.items() for s in ss}
    for m in pc.v1.METHODS:
        for g in ("全部", *groups):
            rs = [r for r in rows if r["method"] == m and (g == "全部" or gof[r["symbol"]] == g)]
            cat = {}
            for c in ("both", "fast_only", "slow_only", "neither"):
                cr = [r for r in rs if r["category"] == c]
                x = {"n": len(cr)}
                if c in ("both", "fast_only"):
                    x.update(fast_pnl_atr0=_d([r["fast"]["pnl_atr0"] for r in cr]), fast_mae_atr0=_d([r["fast"]["mae_atr0"] for r in cr]),
                             fast_hold_atr0=_d([r["fast"].get("hold_atr0") for r in cr]),
                             fast_exit_reasons=_count(r["fast"]["exit_reason"] for r in cr))
                if c in ("both", "slow_only"):
                    x.update(slow_pnl_atr0=_d([r["slow"]["pnl_atr0"] for r in cr]), slow_mae_atr0=_d([r["slow"]["mae_atr0"] for r in cr]),
                             slow_hold_atr0=_d([r["slow"].get("hold_atr0") for r in cr]))
                if c == "both":
                    x.update(lead_bars=_d([r["pair"]["lead_bars"] for r in cr]), entry_px_adv_atr0=_d([r["pair"]["entry_px_adv_atr0"] for r in cr]),
                             pnl_diff_atr0=_d([r["pair"]["pnl_diff_atr0"] for r in cr]))
                if c == "fast_only":
                    x["slow_not_entered_reason"] = _count(r["slow"]["status"] for r in cr)
                if c == "slow_only":
                    x["fast_not_entered_reason"] = _count(r["fast"]["status"] for r in cr)
                cat[c] = x
            per_opp = [r["fast"]["pnl_atr0"] if r["fast"]["status"] in DONE else 0.0 for r in rs]
            per_opp_s = [r["slow"]["pnl_atr0"] if r["slow"]["status"] in DONE else 0.0 for r in rs]
            out[f"{m}|{g}"] = {"opportunities": len(rs), "categories": cat,
                               "per_opportunity_sum_atr0": {"fast": round(sum(per_opp), 3), "slow": round(sum(per_opp_s), 3),
                                                            "note": "每机会未入场计 0、各一笔、不排占位、无费用；重叠机会不独立"},
                               "own_sched_skipped": {"fast": sum(1 for r in rs if r.get("fast_own_sched_skipped")),
                                                     "slow": sum(1 for r in rs if r.get("slow_own_sched_skipped"))}}
    return out


def _count(it) -> dict:
    d: dict = {}
    for k in it:
        d[str(k)] = d.get(str(k), 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def report(summ: dict, meta: dict) -> str:
    L = [f"# ps-entry-pair-v1 · {meta['run_id']}", "",
         "同一突破机会：旧 U6 慢入场（2ATR 确认回踩）vs 快入场（四种确认全部列出，tol 0.5），两边同一 U6 慢离场。",
         "单位 = 突破根 ATR。无费用、无 p 值、≤2015 设计集（已多次查看）；重叠机会不独立。不是可交易盈利的证明。", "",
         "| 方法 | 组 | 机会 | 两边都入 | 只快入 | 只慢入 | 都没入 | 两边都入：快−慢 均值/中位 | 提前根数中位 | 只快入：快收益 均值/中位 | 只慢入：慢收益 均值 | 按机会合计 快/慢 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in summ.items():
        m, g = k.split("|")
        c = v["categories"]
        b, fo, so = c["both"], c["fast_only"], c["slow_only"]
        pd_, fp, sp = b.get("pnl_diff_atr0", {}), fo.get("fast_pnl_atr0", {}), so.get("slow_pnl_atr0", {})
        L.append(f"| {m} | {g} | {v['opportunities']} | {b['n']} | {fo['n']} | {so['n']} | {c['neither']['n']} | "
                 f"{pd_.get('mean', '—')} / {pd_.get('median', '—')} | {b.get('lead_bars', {}).get('median', '—')} | "
                 f"{fp.get('mean', '—')} / {fp.get('median', '—')} | {sp.get('mean', '—')} | "
                 f"{v['per_opportunity_sum_atr0']['fast']} / {v['per_opportunity_sum_atr0']['slow']} |")
    L += ["", "只快入（慢没有入场）里包含慢入场因假突破失效而避开的机会：这部分的快收益就是更早入场付出的假突破代价，与两边都入的增量同屏看。",
          "MAE/MFE、未入场原因、各自占位下会跳过的机会数见 summary.json。", "",
          f"git {meta['git_head'][:8]} · config {meta['config_sha256'][:12]}"]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    cfg = load_cfg()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    ref_dates = [b[0] for b in pe.parse(pe.fetch("SPY")[0])[0]]
    syms = [s for g in pc.v1.GROUPS.values() for s in g]
    rows = []
    for s in syms:
        rows += run_symbol(s, ref_dates)
    out.mkdir(parents=True)
    inputs = json.loads(pe.INPUT_SHA.read_text("utf-8"))
    meta = {"run_id": a.run_id, "version": cfg["version"], "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
            "config_sha256": hashlib.sha256(CFG.read_bytes()).hexdigest(),
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in CODE},
            "input_sha256": {s: inputs.get(s) for s in syms}, "n_rows": len(rows)}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    with open(out / "pairs.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    summ = summarize(rows, cfg["groups"])
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report(summ, meta), "utf-8")
    print(len(rows), "→", out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
