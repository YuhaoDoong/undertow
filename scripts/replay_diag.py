"""exploratory-replay-diagnostics-v1：逐根回放标注的探索诊断（Codex 042 replay_diagnostics.md；配置 data/backtest/replay-diag-v1/config.json）。

  python3 scripts/replay_diag.py --run-id run1

引擎修订 r2-047（Codex 047：删除按实际已见边界、止损按最新有效事件、入场根跳空、延迟参照可审计）见配置 engine_revision。
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
from collections import Counter, defaultdict
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
            r["deletes_seq"] = tgt["seq"] if tgt else None
            r["cls"] = "delete_rewind" if rewind else "delete_frontier"
            if tgt:                                            # 删除链：回看光标 as_of 与实际知识边界 seen_max 分开存（047 §3）
                tgt["deleted_at_as_of"] = e["as_of_i"]
                tgt["deleted_seen_max"] = e["seen_max"]
                tgt["deleted_by_seq"] = n
                tgt["deleted_logged_at"] = e.get("logged_at")
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
            x = {"algo": name, "user_delay": r["as_of_i"] - r["i"], "matched": False, "label_deleted_later": r.get("deleted_seen_max") is not None}
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


def _deleted_before(r: dict, kb: int, seq: int | None = None) -> bool:
    """对象 r 在知识边界 kb（及该边界内序号 seq 之前）是否已被删除。删除按实际已见边界 seen_max 计，不按回看光标。"""
    k = r.get("deleted_seen_max")
    if k is None:
        return False
    return k < kb or (k == kb and (seq is None or r["deleted_by_seq"] < seq))


def trades(rows: list, bars: list, atr: list) -> dict:
    """因果交易队列（配置 trades + engine_revision r2-047）。回看补入场另算两种反事实。

    时序：每条操作的知识边界 = 它的 seen_max（实际推进到的根）；决定在边界后的下一根开盘执行。
    - 删除的知识边界 = 删除操作的 seen_max。删除边界 == 决定根（未推进就删）→ 执行前取消；更晚 → 决定已可执行，
      删除只进最终解释层 final_layer，不撤销因果交易（回看到旧光标删除也一样）。
    - 止损按事件维护：bar i 的有效止损 = 决定根 < i、不早于本笔入场决定、在 i 之前未被删除（删除边界 ≥ i 仍有效）的止损中
      (决定根, seq) 最新的一条。新增/更新/删除最早从下一根生效；最新那条删掉后回落到仍有效的上一条。
    - 同一根内顺序（事先写定）：① 开盘执行上一根决定的用户离场 → ② 开盘执行上一根决定的入场（同根多条按 seq）→
      ③ 开盘已越过有效止损：入场根记 filled_through_stop、其后各根记 gap_stop，都按开盘价离场 →
      ④ 盘中最低（空头最高）触及止损按止损价离场。入场根与其后各根同样处理，不按未触达的价格出场。
    无费、无仓位/数量的价格路径描述，不是订单回测。"""
    last = len(bars) - 1
    dec = sorted([r for r in rows if r["action"] == "add" and r["cls"] == "frontier_decision"], key=lambda r: (r["as_of_i"], r["seq"]))
    stops = [r for r in dec if r["type"] == "止损"]
    pos, out, flags = None, [], Counter()
    by_exec = defaultdict(list)
    for r in dec:
        k = r.get("deleted_seen_max")
        if k is not None and k <= r["as_of_i"]:                # 未推进就删 = 执行前取消（止损同样从未生效）
            flags["cancelled_before_execution"] += 1
            continue
        if r["type"] not in ENTRY and r["type"] != "离场":
            continue
        if k is not None:
            flags["decision_deleted_after_executable"] += 1    # 只进最终解释层
            if r.get("deleted_at_as_of") == r["as_of_i"]:
                flags["rewind_delete_kept_executable"] += 1    # r1 会误判成执行前取消的那类（047 §3）
        if r["as_of_i"] + 1 > last:
            if r["type"] in ENTRY:
                flags["no_next_open"] += 1
            else:
                by_exec[last + 1].append(r)
            continue
        by_exec[r["as_of_i"] + 1].append(r)

    def active_stop(i: int):
        c = [x for x in stops if pos["decided_at"] <= x["as_of_i"] < i and not _deleted_before(x, i - 1)]   # 边界 i−1 内的删除从 i 起生效
        return max(c, key=lambda x: (x["as_of_i"], x["seq"])) if c else None

    def close(px, i, why):
        nonlocal pos
        s = 1 if pos["dir"] == "long" else -1
        A = atr[pos["entry_i"] - 1]
        out.append({**pos, "exit_i": i, "exit_px": px, "exit_reason": why, "atr_units": s * (px - pos["entry_px"]) / A if A else None,
                    "bars_held": i - pos["entry_i"] + 1})
        pos = None

    for i in range(last + 1):
        for r in by_exec.get(i, []):
            if r["type"] == "离场":
                if pos is None:
                    flags["exit_without_position"] += 1
                else:
                    close(bars[i][1], i, "user_exit_next_open")
            elif pos is not None:
                flags["repeat_entry"] += 1
            else:
                pos = {"dir": ENTRY[r["type"]], "decided_at": r["as_of_i"], "decided_seq": r["seq"], "entry_i": i,
                       "entry_px": bars[i][1], "label_i": r["i"], "stop": None, "stop_seq": None, "stop_path": [], "stop_deleted_in_position": False,
                       "final_layer": {"entry_deleted_after_executable": r.get("deleted_seen_max") is not None,
                                       "delete_seen_max": r.get("deleted_seen_max"), "delete_as_of": r.get("deleted_at_as_of"),
                                       "delete_rewind": r.get("deleted_rewind")}}
        if pos is None:
            continue
        a = active_stop(i)
        px, sq = (a["price"], a["seq"]) if a else (None, None)
        if sq != pos["stop_seq"]:
            prev = next((x for x in stops if x["seq"] == pos["stop_seq"]), None)
            if prev is not None and _deleted_before(prev, i - 1):
                pos["stop_deleted_in_position"] = True
            pos["stop_path"].append({"from_i": i, "stop": px, "stop_seq": sq})
            pos["stop"], pos["stop_seq"] = px, sq
        if px is None:
            continue
        s = 1 if pos["dir"] == "long" else -1
        o, h, lo = bars[i][1], bars[i][2], bars[i][3]
        if s * o <= s * px:
            close(o, i, "filled_through_stop" if i == pos["entry_i"] else "gap_stop")
        elif s * (lo if s > 0 else h) <= s * px:
            close(px, i, "stop")
    for r in by_exec.get(last + 1, []):                       # 最后一根才决定的离场：没有下一根开盘
        if pos is None:
            flags["exit_without_position"] += 1
        else:
            close(bars[last][4], last, "admin_end")
    if pos is not None:
        close(bars[last][4], last, "admin_end")
    for t in out:
        t["deleted_later"] = t["final_layer"]["entry_deleted_after_executable"]
        t["stop_ever_set"] = any(x["stop"] is not None for x in t["stop_path"])
        t["stop_changes"] = len(t["stop_path"])
        if not t["stop_ever_set"]:
            flags["missing_stop"] += 1
    back = []
    for r in sorted([r for r in rows if r["action"] == "add"], key=lambda r: (r["as_of_i"], r["seq"])):
        if r["cls"] == "entry_backfilled":
            s = 1 if ENTRY[r["type"]] == "long" else -1
            A = atr[r["i"]] or 1
            di = r["seen_max"] + 1
            back.append({"dir": ENTRY[r["type"]], "label_i": r["i"], "discovered_at": r["seen_max"],
                         "cf_label_price_to_end_atr": s * (bars[last][4] - r["price"]) / A,
                         "cf_discovery_next_open_to_end_atr": (s * (bars[last][4] - bars[di][1]) / A) if di <= last else None})
    return {"trades": out, "flags": dict(flags), "backfilled": back}


DELAY_RULE = ("事前最近同侧候选参照（启发式，不是用户声明的入场理由）：决定时仍有效（未删或删除在决定之后）的同侧用户点"
              "（做多 HL/LL、做空 LH/HH，推进前背景点或最远处标点），取极值根最晚者，同根取标注较晚者；没有则 None")


def _ref(r: dict | None, decided_at: int, decided_seq: int) -> dict | None:
    if r is None:
        return None
    return {"seq": r["seq"], "id": r["id"], "type": r["type"], "point_i": r["i"], "label_asof": r["as_of_i"],
            "label_cls": r["cls"], "deleted_before_decision": _deleted_before(r, decided_at, decided_seq),
            "deleted_later": r.get("deleted_seen_max") is not None and not _deleted_before(r, decided_at, decided_seq)}


def delays(rows: list, tr: list, alg: dict) -> list:
    """每笔因果入场的延迟分解（047 B）：参照点 → 用户标出 → 入场决定 → 执行。参照见 DELAY_RULE：启发式候选，
    主诊断只用决定时仍有效的点；把决定前已删的点也算进来时选中的点另栏列出（首次认知），不替代主诊断。
    算法列：离参照点 ≤5 根的同类算法点【最终】确认根 − 用户标出根（可正可负，不以 conf ≤ 标注时为条件），另记标注时是否已确认。
    分量分布不可相加（各分量中位之和 ≠ 总延迟中位）。"""
    out = []
    for t in tr:
        want = ("HL", "LL") if t["dir"] == "long" else ("LH", "HH")
        ds = t.get("decided_seq", 10 ** 9)
        cands = [r for r in rows if r["action"] == "add" and r["type"] in want and r["cls"] in ("frontier_pivot_label", "background_only")
                 and (r["as_of_i"] < t["decided_at"] or (r["as_of_i"] == t["decided_at"] and r["seq"] < ds))]
        alive = [r for r in cands if not _deleted_before(r, t["decided_at"], ds)]
        key = lambda r: (r["i"], r["as_of_i"], r["seq"])
        p = max(alive, key=key, default=None)
        p_all = max(cands, key=key, default=None)
        x = {"dir": t["dir"], "seg": t.get("seg"), "decided_at": t["decided_at"], "entry_i": t["entry_i"], "selection_rule": DELAY_RULE,
             "ref": _ref(p, t["decided_at"], ds), "n_alive_candidates": len(alive), "n_candidates_incl_deleted": len(cands),
             "ref_incl_deleted": _ref(p_all, t["decided_at"], ds) if p_all is not p else None,
             "ref_changes_if_deleted_included": p_all is not p}
        if p is None:
            out.append(x); continue
        x["same_bar_labels"] = sum(1 for q in alive if q["i"] == p["i"])
        x.update(extreme_to_label=p["as_of_i"] - p["i"], label_to_decision=t["decided_at"] - p["as_of_i"],
                 decision_to_exec=t["entry_i"] - t["decided_at"], extreme_to_exec=t["entry_i"] - p["i"])
        k = PIVOT_KIND[p["type"]]
        for name in ("bar", "two", "atr", "zigzag_2atr"):
            near = min((q for q in alg[name] if q[0] == k and abs(q[1] - p["i"]) <= MATCH_WIN), key=lambda q: (abs(q[1] - p["i"]), q[2]), default=None)
            x[f"{name}_final_conf_minus_user_label"] = (near[2] - p["as_of_i"]) if near else None
            x[f"{name}_confirmed_by_label_time"] = (near[2] <= p["as_of_i"]) if near else None
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
    return {"n": len(xs), "median": round(st.median(xs), 3), "mean": round(st.mean(xs), 3), "p10": round(q[len(q) // 10], 3),
            "p25": round(q[len(q) // 4], 3), "p75": round(q[(3 * len(q)) // 4], 3), "p90": round(q[(9 * len(q)) // 10], 3),
            "min": round(q[0], 3), "max": round(q[-1], 3)}


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
            dl = delays(rows, [{**x, "seg": seg["id"]} for x in t["trades"]], alg)
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
            pv["algo_match"][name] = {"user_labels": len(mm), "matched_as_of": len(ok), "unmatched": len(mm) - len(ok),
                                      "match_rate": round(len(ok) / len(mm), 3) if mm else None,
                                      "labels_deleted_later": sum(1 for x in mm if x["label_deleted_later"]),
                                      "idx_diff": _dist([x["idx_diff"] for x in ok]), "price_diff_atr": _dist([x["price_diff_atr"] for x in ok]),
                                      "algo_conf_minus_user_label_matched": _dist([x["conf_minus_user"] for x in ok]),
                                      "nearest_algo_conf_minus_user_label_all": _dist([x.get("nearest_conf_minus_user") for x in mm]),
                                      "no_same_kind_pivot_within_5": sum(1 for x in mm if "nearest_conf_minus_user" not in x),
                                      "note": ("matched 组以 conf ≤ 用户标注根为条件，确认差天然 ≤ 0，只描述已及时匹配者；总体看 nearest_all 与 unmatched。"
                                               "只按 H/L 与 ±5 根相邻匹配，不检验 HH/HL/LH/LL 分类、突破或方向，不是交易胜率")}
        pv["trades"] = {"n": len(alltr), "flags": dict(flags), "by_dir": dict(Counter(t["dir"] for t in alltr)),
                        "exit_reasons": dict(Counter(t["exit_reason"] for t in alltr)),
                        "atr_units": _dist([t["atr_units"] for t in alltr]), "bars_held": _dist([t["bars_held"] for t in alltr]),
                        "entry_icon_deleted_after_executable": sum(1 for t in alltr if t["deleted_later"]),
                        "stop_changed_in_position": sum(1 for t in alltr if t["stop_changes"] > 1),
                        "stop_deleted_in_position": sum(1 for t in alltr if t["stop_deleted_in_position"]),
                        "note": "价格路径描述：无仓位/数量、未扣费；不推定真实账户持仓"}
        pv["engine_r2_hits"] = {                               # 047 影响筛查：本 run 里实际走到新路径的次数
            "rewind_delete_kept_executable": flags.get("rewind_delete_kept_executable", 0),
            "decision_deleted_after_executable": flags.get("decision_deleted_after_executable", 0),
            "trades_stop_changed_in_position": sum(1 for t in alltr if t["stop_changes"] > 1),
            "trades_stop_deleted_in_position": sum(1 for t in alltr if t["stop_deleted_in_position"]),
            "trades_entry_bar_filled_through_stop": sum(1 for t in alltr if t["exit_reason"] == "filled_through_stop"),
            "delays_ref_changes_if_deleted_included": sum(1 for x in alld if x["ref_changes_if_deleted_included"]),
            "delays_ref_none": sum(1 for x in alld if x["ref"] is None)}
        pv["backfilled_entries"] = {"n": len(back), "cf_label_price_to_end_atr": _dist([b["cf_label_price_to_end_atr"] for b in back]),
                                    "cf_discovery_next_open_to_end_atr": _dist([b["cf_discovery_next_open_to_end_atr"] for b in back]),
                                    "note": "只作反事实，不进因果交易收益；不对真人认知作作废判词"}
        pv["delay_decomposition"] = {
            "n": len(alld), "selection_rule": DELAY_RULE,
            "ref_none": sum(1 for x in alld if x["ref"] is None),
            "ref_changes_if_deleted_included": sum(1 for x in alld if x["ref_changes_if_deleted_included"]),
            "ref_deleted_later": sum(1 for x in alld if x["ref"] and x["ref"]["deleted_later"]),
            "multiple_alive_labels_same_bar": sum(1 for x in alld if (x.get("same_bar_labels") or 0) > 1),
            **{k: _dist([x.get(k) for x in alld]) for k in ("extreme_to_label", "label_to_decision", "decision_to_exec", "extreme_to_exec")},
            **{f"{n}_final_conf_minus_user_label": _dist([x.get(f"{n}_final_conf_minus_user_label") for x in alld]) for n in ("bar", "two", "atr", "zigzag_2atr")},
            **{f"{n}_no_point_within_5": sum(1 for x in alld if x["ref"] and x.get(f"{n}_final_conf_minus_user_label") is None) for n in ("bar", "zigzag_2atr")},
            "note": ("参照是启发式候选，不是用户声明的入场点；分量分布不可相加；中位 0 不排除决策等待（看 mean/max）；"
                     "不能据此排除突破识别/回踩等待/执行等其它环节")}
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
                   "config_sha256": sha(CFG), "engine_revision": cfg.get("engine_revision", {}).get("id", "r1"), "code_sha256": {p: sha(ROOT / p) for p in ("scripts/replay_diag.py", "undertow/analyze/price_structure.py")},
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
