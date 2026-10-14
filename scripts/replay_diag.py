"""exploratory-replay-diagnostics-v1：逐根回放标注的探索诊断（Codex 042 replay_diagnostics.md；配置 data/backtest/replay-diag-v1/config.json）。

  python3 scripts/replay_diag.py --run-id run1

读私有标注（data/soul/structure_labels/）与回放页面内嵌 K 线；逐条结果只写私有 data/soul/structure_labels/diag-v1/<run>/，
另写一份不含逐点判断与备注的聚合 aggregate.json。run 目录存在即拒绝。描述性，不证明策略盈利。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics as st
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import price_structure as ps  # noqa: E402

CFG = ROOT / "data/backtest/replay-diag-v1/config.json"
LABELS = {v: ROOT / f"data/soul/structure_labels/2026-10-14_replay_{v}.json" for v in ("v1", "v2")}
PAGES = {v: ROOT / f"data/backtest/structure-v0/replay_{v}" for v in ("v1", "v2")}
OUT = ROOT / "data/soul/structure_labels/diag-v1"
PIVOT_KIND = {"HH": "H", "LH": "H", "HL": "L", "LL": "L", "关键前高": "H", "关键前低": "L"}
SWING = ("HH", "LH", "HL", "LL")
ENTRY = {"入场做多": "long", "入场做空": "short"}
MATCH_WIN = 5


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def page_data(v: str) -> list:
    h = (PAGES[v] / "index.html").read_text("utf-8")
    m = re.search(r"const DATA = (\[.*?\]);\n", h, re.S)
    return json.loads(m.group(1))


def classify(v: str, seg: dict, events: list) -> list:
    """每条操作 → 派生 id、类别、删除链。v1 没有 id：派生 id = 原件序号；删除按 type@i 匹配（与页面 live() 相同）；v2 按 target。"""
    left = seg["left"]
    out = []
    alive: dict = {}
    for n, e in enumerate(events):
        r = {"seq": n, "id": e.get("id") or f"{v}-{seg['id']}-{n}", **{k: e.get(k) for k in
             ("action", "type", "i", "as_of_i", "seen_max", "price", "price2", "uncertain", "logged_at", "view", "target")}}
        rewind = e["as_of_i"] < e["seen_max"]
        if e["action"] == "delete":
            key = e.get("target") if v == "v2" else f"{e['type']}@{e['i']}"
            tgt = alive.pop(key, None)
            r["deletes"] = tgt["id"] if tgt else None
            r["cls"] = "delete_rewind" if rewind else "delete_frontier"
            if tgt:
                tgt["deleted_at_as_of"] = e["as_of_i"]
                tgt["deleted_rewind"] = rewind
            out.append(r)
            continue
        if e["i"] > e["as_of_i"]:
            r["cls"] = "invalid_time"
        elif e["as_of_i"] == left - 1 and e["seen_max"] == left - 1:
            r["cls"] = "background_only"
        elif rewind:
            r["cls"] = "entry_backfilled" if e["type"] in ENTRY else "rewind_edit"
        elif e["type"] in ENTRY or e["type"] in ("离场", "止损"):
            r["cls"] = "frontier_decision"
        elif e["type"] in PIVOT_KIND:
            r["cls"] = "frontier_pivot_label"
        else:
            r["cls"] = "frontier_other"                       # 密集区 / 支撑位 等
        alive[e.get("id") if v == "v2" else f"{e['type']}@{e['i']}"] = r
        out.append(r)
    return out


def algo_pivots(bars: list) -> dict:
    atr = ps.wilder_atr(bars)
    piv, _ = ps.detect(bars, atr)
    out = {"zigzag_2atr": [(p.kind, p.idx, p.conf, p.price) for p in piv]}
    for m in ("bar", "two", "atr"):
        out[m] = [(p.kind, p.idx, p.conf, p.price) for p in ps.local_swings(bars, atr, m)]
    return out, atr


def match(rows: list, alg: dict, atr: list) -> list:
    """用户首次因果标的摆动点 vs 各算法：①只与 as_of 已确认的同类点一对一匹配；②离它最近的同类点最终确认根（另名）。"""
    res = []
    for name, pts in alg.items():
        used = set()
        for r in sorted([x for x in rows if x["cls"] == "frontier_pivot_label" and x["type"] in SWING], key=lambda x: (x["as_of_i"], x["seq"])):
            k = PIVOT_KIND[r["type"]]
            A = atr[r["i"]] or atr[r["as_of_i"]]
            known = [p for p in pts if p[0] == k and p[2] <= r["as_of_i"] and abs(p[1] - r["i"]) <= MATCH_WIN and (p[1], p[2]) not in used]
            near = min((p for p in pts if p[0] == k), key=lambda p: (abs(p[1] - r["i"]), p[2]), default=None)
            x = {"algo": name, "user_delay": r["as_of_i"] - r["i"], "matched": False}
            if known:
                p = min(known, key=lambda p: (abs(p[1] - r["i"]), p[2]))
                used.add((p[1], p[2]))
                x.update(matched=True, idx_diff=p[1] - r["i"], price_diff_atr=(p[3] - r["price"]) / A if A else None,
                         conf_minus_user=p[2] - r["as_of_i"])
            if near is not None and abs(near[1] - r["i"]) <= MATCH_WIN:
                x["nearest_conf_minus_user"] = near[2] - r["as_of_i"]
                x["nearest_idx_diff"] = near[1] - r["i"]
            res.append(x)
    return res


def trades(rows: list, bars: list, atr: list) -> dict:
    """因果交易队列（见配置 trades）。回看补入场另算两种反事实。"""
    last = len(bars) - 1
    seq = sorted([r for r in rows if r["action"] == "add"], key=lambda r: (r["as_of_i"], r["seq"]))
    stops = [r for r in seq if r["type"] == "止损" and r["cls"] == "frontier_decision"]
    pos, out, flags = None, [], Counter()

    def close(px, i, why):
        s = 1 if pos["dir"] == "long" else -1
        A = atr[pos["entry_i"] - 1]
        out.append({**pos, "exit_i": i, "exit_px": px, "exit_reason": why, "atr_units": s * (px - pos["entry_px"]) / A if A else None,
                    "bars_held": i - pos["entry_i"] + 1})

    def run_to(t_end):
        nonlocal pos
        if pos is None:
            return
        s = 1 if pos["dir"] == "long" else -1
        for i in range(pos["cursor"], min(t_end, last) + 1):
            st_ = pos.get("stop")
            if st_ is None:
                cand = [x for x in stops if pos["decided_at"] <= x["as_of_i"] < i]
                if cand:
                    pos["stop"] = st_ = cand[0]["price"]; pos["stop_set_at"] = cand[0]["as_of_i"]
            if st_ is not None and i > pos["entry_i"] - 1:
                o, h, lo = bars[i][1], bars[i][2], bars[i][3]
                if i > pos["entry_i"] and s * o <= s * st_:
                    close(o, i, "gap_stop"); pos = None; return
                if s * (lo if s > 0 else h) <= s * st_:
                    close(st_, i, "stop"); pos = None; return
            pos["cursor"] = i + 1

    for r in seq:
        if r["cls"] != "frontier_decision":
            continue
        run_to(r["as_of_i"])
        if r["type"] in ENTRY:
            if pos is not None:
                flags["repeat_entry"] += 1; continue
            if r["as_of_i"] + 1 > last:
                flags["no_next_open"] += 1; continue
            ei = r["as_of_i"] + 1
            pos = {"dir": ENTRY[r["type"]], "decided_at": r["as_of_i"], "entry_i": ei, "entry_px": bars[ei][1], "cursor": ei,
                   "label_i": r["i"], "deleted_later": r.get("deleted_at_as_of") is not None}
        elif r["type"] == "离场":
            if pos is None:
                flags["exit_without_position"] += 1; continue
            if r["as_of_i"] + 1 > last:
                close(bars[last][4], last, "admin_end"); pos = None; continue
            run_to(r["as_of_i"])
            if pos is not None:
                close(bars[r["as_of_i"] + 1][1], r["as_of_i"] + 1, "user_exit_next_open"); pos = None
    if pos is not None:
        run_to(last)
        if pos is not None:
            close(bars[last][4], last, "admin_end")
    for t in out:
        if t.get("stop") is None:
            flags["missing_stop"] += 1
    back = []
    for r in seq:
        if r["cls"] == "entry_backfilled":
            s = 1 if ENTRY[r["type"]] == "long" else -1
            A = atr[r["i"]] or 1
            di = r["seen_max"] + 1
            back.append({"dir": ENTRY[r["type"]], "label_i": r["i"], "discovered_at": r["seen_max"],
                         "cf_label_price_to_end_atr": s * (bars[last][4] - r["price"]) / A,
                         "cf_discovery_next_open_to_end_atr": (s * (bars[last][4] - bars[di][1]) / A) if di <= last else None})
    return {"trades": out, "flags": dict(flags), "backfilled": back}


def delays(rows: list, tr: list, alg: dict) -> list:
    """每笔因果入场的延迟分解：同侧最近用户点极值 → 用户标出 → 入场决定 → 执行；并列算法对同一点的确认根。"""
    out = []
    for t in tr:
        want = ("HL", "LL") if t["dir"] == "long" else ("LH", "HH")
        pts = [r for r in rows if r["action"] == "add" and r["type"] in want and r["as_of_i"] <= t["decided_at"]
               and r["cls"] in ("frontier_pivot_label", "background_only")]
        if not pts:
            out.append({"dir": t["dir"], "ref": "none"}); continue
        p = max(pts, key=lambda r: (r["i"], r["as_of_i"]))
        k = PIVOT_KIND[p["type"]]
        x = {"dir": t["dir"], "ref": "unique" if len([q for q in pts if q["i"] == p["i"]]) == 1 else "multiple_labels_same_bar",
             "extreme_to_label": p["as_of_i"] - p["i"], "label_to_decision": t["decided_at"] - p["as_of_i"], "decision_to_exec": 1,
             "extreme_to_exec": t["entry_i"] - p["i"]}
        for name in ("bar", "zigzag_2atr"):
            near = min((q for q in alg[name] if q[0] == k and abs(q[1] - p["i"]) <= MATCH_WIN), key=lambda q: abs(q[1] - p["i"]), default=None)
            x[f"{name}_conf_minus_user_label"] = (near[2] - p["as_of_i"]) if near else None
        out.append(x)
    return out


def zones(rows: list, bars: list, atr: list) -> list:
    out = []
    last = len(bars) - 1
    for r in rows:
        if r["action"] != "add" or r["type"] not in ("密集区", "支撑位"):
            continue
        a = r["as_of_i"]
        A = atr[a] or None
        lo, hi = sorted([r["price"], r["price2"] if r.get("price2") is not None else r["price"]])
        c = bars[a][4]
        x = {"type": r["type"], "cls": r["cls"], "width_atr": (hi - lo) / A if A else None,
             "dist_close_atr": (0.0 if lo <= c <= hi else min(abs(c - lo), abs(c - hi))) / A if A else None,
             "has_two_prices": r.get("price2") is not None, "view": r.get("view")}
        for h in (5, 10, 20):
            if a + h > last:
                x[f"touch_{h}"] = None
            else:
                x[f"touch_{h}"] = any(bars[j][3] <= hi and bars[j][2] >= lo for j in range(a + 1, a + h + 1))
        out.append(x)
    return out


def _dist(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    q = sorted(xs)
    return {"n": len(xs), "median": round(st.median(xs), 3), "mean": round(st.mean(xs), 3), "p25": round(q[len(q) // 4], 3),
            "p75": round(q[(3 * len(q)) // 4], 3), "min": round(q[0], 3), "max": round(q[-1], 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    out = OUT / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    cfg = json.loads(CFG.read_text("utf-8"))
    agg = {"version": cfg["version"], "per_version": {}}
    private = {}
    for v in ("v1", "v2"):
        lab = json.loads(LABELS[v].read_text("utf-8"))
        data = {d["id"]: d for d in page_data(v)}
        allrows, allm, alltr, alld, allz, flags, back = [], [], [], [], [], Counter(), []
        seg_tab = []
        for seg in lab["segments"]:
            sid = str(seg["id"])
            d = data[seg["id"]]
            assert d["symbol"] == seg["symbol"] and d["left"] == seg["left"]
            bars = [tuple(b[:5]) for b in d["bars"]]
            alg, atr = algo_pivots(bars)
            rows = classify(v, seg, lab["state"][sid]["events"])
            m = match(rows, alg, atr)
            t = trades(rows, bars, atr)
            dl = delays(rows, t["trades"], alg)
            z = zones(rows, bars, atr)
            for r in rows:
                r["seg"] = seg["id"]; r["symbol"] = seg["symbol"]
            allrows += rows; allm += m; alltr += [{**x, "seg": seg["id"], "symbol": seg["symbol"]} for x in t["trades"]]
            alld += dl; allz += z; flags.update(t["flags"]); back += t["backfilled"]
            c = Counter(r["cls"] for r in rows)
            seg_tab.append({"seg": seg["id"], "symbol": seg["symbol"], "ops": len(rows), "classes": dict(c),
                            "trades": len(t["trades"]), "flags": t["flags"]})
        adds = [r for r in allrows if r["action"] == "add"]
        dels = [r for r in allrows if r["action"] != "add"]
        fp = [r for r in allrows if r["cls"] == "frontier_pivot_label"]
        pv = {"segments": len(lab["segments"]), "symbols": dict(Counter(s["symbol"] for s in lab["segments"])),
              "ops": {"total": len(allrows), "add": len(adds), "delete": len(dels),
                      "rewind_ops": sum(1 for r in allrows if r["cls"] in ("rewind_edit", "entry_backfilled", "delete_rewind")),
                      "deletes_per_add": round(len(dels) / len(adds), 3) if adds else None,
                      "deleted_objects": sum(1 for r in adds if r.get("deleted_at_as_of") is not None)},
              "classes": dict(Counter(r["cls"] for r in allrows)),
              "by_type": dict(Counter(f"{r['action']}:{r['type']}" for r in allrows)),
              "uncertain": sum(1 for r in adds if r.get("uncertain")),
              "frontier_pivot_delay_bars": _dist([r["as_of_i"] - r["i"] for r in fp if r["type"] in SWING]),
              "frontier_pivot_delay_by_type": {k: _dist([r["as_of_i"] - r["i"] for r in fp if r["type"] == k]) for k in SWING + ("关键前高", "关键前低")},
              "frontier_pivot_deleted_later": sum(1 for r in fp if r.get("deleted_at_as_of") is not None),
              "algo_match": {}, "trades": {}, "delay_decomposition": {}, "zones": None, "segments": seg_tab}
        for name in ("zigzag_2atr", "bar", "two", "atr"):
            mm = [x for x in allm if x["algo"] == name]
            ok = [x for x in mm if x["matched"]]
            pv["algo_match"][name] = {"user_labels": len(mm), "matched_as_of": len(ok),
                                      "match_rate": round(len(ok) / len(mm), 3) if mm else None,
                                      "idx_diff": _dist([x["idx_diff"] for x in ok]), "price_diff_atr": _dist([x["price_diff_atr"] for x in ok]),
                                      "algo_conf_minus_user_label_matched": _dist([x["conf_minus_user"] for x in ok]),
                                      "nearest_algo_conf_minus_user_label_all": _dist([x.get("nearest_conf_minus_user") for x in mm]),
                                      "no_same_kind_pivot_within_5": sum(1 for x in mm if "nearest_conf_minus_user" not in x)}
        pv["trades"] = {"n": len(alltr), "flags": dict(flags), "by_dir": dict(Counter(t["dir"] for t in alltr)),
                        "exit_reasons": dict(Counter(t["exit_reason"] for t in alltr)),
                        "atr_units": _dist([t["atr_units"] for t in alltr]), "bars_held": _dist([t["bars_held"] for t in alltr]),
                        "entry_icon_deleted_after_executable": sum(1 for t in alltr if t["deleted_later"]),
                        "note": "价格路径描述：无仓位/数量、未扣费；不推定真实账户持仓"}
        pv["backfilled_entries"] = {"n": len(back), "cf_label_price_to_end_atr": _dist([b["cf_label_price_to_end_atr"] for b in back]),
                                    "cf_discovery_next_open_to_end_atr": _dist([b["cf_discovery_next_open_to_end_atr"] for b in back]),
                                    "note": "只作反事实，不进因果交易收益；不对真人认知作作废判词"}
        pv["delay_decomposition"] = {"n": len(alld), "ref": dict(Counter(x["ref"] for x in alld)),
                                     **{k: _dist([x.get(k) for x in alld]) for k in ("extreme_to_label", "label_to_decision", "extreme_to_exec",
                                                                                    "bar_conf_minus_user_label", "zigzag_2atr_conf_minus_user_label")}}
        if allz:
            pv["zones"] = {"n": len(allz), "by_type": dict(Counter(z["type"] for z in allz)), "classes": dict(Counter(z["cls"] for z in allz)),
                           "width_atr": _dist([z["width_atr"] for z in allz]), "dist_close_atr": _dist([z["dist_close_atr"] for z in allz]),
                           **{f"touch_{h}": {"yes": sum(1 for z in allz if z[f"touch_{h}"]), "no": sum(1 for z in allz if z[f"touch_{h}"] is False),
                                             "window_missing": sum(1 for z in allz if z[f"touch_{h}"] is None)} for h in (5, 10, 20)},
                           "profile_proxy_replay": "未做（待办）：按当时 view/chipMode/chipRange 复刻实际显示的分布"}
        agg["per_version"][v] = pv
        private[v] = {"rows": allrows, "match": allm, "trades": alltr, "delays": alld, "zones": allz, "backfilled": back}
    agg["meta"] = {"run_id": a.run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
                   "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
                   "config_sha256": sha(CFG), "code_sha256": {p: sha(ROOT / p) for p in ("scripts/replay_diag.py", "undertow/analyze/price_structure.py")},
                   "label_sha256": {v: sha(p) for v, p in LABELS.items()},
                   "page_sha256": {v: {f: sha(PAGES[v] / f) for f in ("index.html", "manifest.json")} for v in PAGES},
                   "match_window_bars": MATCH_WIN}
    out.mkdir(parents=True)
    (out / "aggregate.json").write_text(json.dumps(agg, ensure_ascii=False, indent=1), "utf-8")
    (out / "private_detail.json").write_text(json.dumps(private, ensure_ascii=False, default=str), "utf-8")
    print(f"→ {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
