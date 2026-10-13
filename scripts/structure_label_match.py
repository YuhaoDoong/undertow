"""用户盲图标注 vs 候选认点方式（label-match-v1；规则 data/backtest/structure-v0/label_match_config.json 先于查看标注提交）。

  python3 scripts/structure_label_match.py --out data/backtest/structure-v0/label_match_v1

只比认点是否像人眼，不涉及收益。标注原件在私有 data/soul/structure_labels/（含个人判断，不入库）；输出只含聚合指标与逐图计数。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from scripts import structure_label_page_v2 as lp  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

CFG = ROOT / "data/backtest/structure-v0/label_match_config.json"
H_CLS, L_CLS = ("HH", "LH", "关键前高"), ("HL", "LL", "关键前低")


def zigzag_hl(bars, atr, m):
    """影线之字形：高点用 high、低点用 low；反转判据用 ATR_{t−1}；逐根顺序与收盘版相同（更新候选 → 检查反转）。"""
    out, d, cand = [], None, None
    for t in range(ps.WARMUP, len(bars)):
        a = atr[t - 1]
        if a is None:
            continue
        h, l = bars[t][2], bars[t][3]
        if d is None:
            if cand is None:
                hi = lo = t; cand = 0
                continue
            if h > bars[hi][2]: hi = t
            if l < bars[lo][3]: lo = t
            cH, cL = l <= bars[hi][2] - m * a, h >= bars[lo][3] + m * a
            if cH and (not cL or hi < lo):
                out.append(ps.Pivot("H", hi, t, bars[hi][2])); d = "down"; cand = min(range(hi + 1, t + 1), key=lambda i: (bars[i][3], i))
            elif cL:
                out.append(ps.Pivot("L", lo, t, bars[lo][3])); d = "up"; cand = max(range(lo + 1, t + 1), key=lambda i: (bars[i][2], -i))
            continue
        if d == "down":
            if l < bars[cand][3]:
                cand = t
            elif h >= bars[cand][3] + m * a:
                out.append(ps.Pivot("L", cand, t, bars[cand][3])); d = "up"
                lo_i = cand; cand = max(range(lo_i + 1, t + 1), key=lambda i: (bars[i][2], -i))
        else:
            if h > bars[cand][2]:
                cand = t
            elif l <= bars[cand][2] - m * a:
                out.append(ps.Pivot("H", cand, t, bars[cand][2])); d = "down"
                hi_i = cand; cand = min(range(hi_i + 1, t + 1), key=lambda i: (bars[i][3], i))
    return out


def candidates(pre):
    atr = ps.wilder_atr(pre)
    def frac(k):
        return [ps.Pivot(p.kind, p.idx, p.conf, p.price) for p in ps.alternating(ps.fractal_pivots(pre, k))]
    return atr, {
        "frac_k2": frac(2), "frac_k3": frac(3), "frac_k5": frac(5),
        "zz_close_1atr": ps.zigzag(pre, atr, 1.0), "zz_close_2atr": ps.zigzag(pre, atr, 2.0),
        "zz_hl_1.5atr": zigzag_hl(pre, atr, 1.5),
    }


def match(user, algo, atr, max_bar, max_atr):
    """同类一对一贪心：所有 (用户点, 算法点) 候选对按 |Δbar| 升序配对。返回 [(u, a)]。"""
    pairs = []
    for ui, u in enumerate(user):
        for ai, a in enumerate(algo):
            if u["cls"] != a.kind:
                continue
            db = abs(u["idx"] - a.idx)
            A = atr[u["idx"]] or atr[u["idx"] - 1]
            if db <= max_bar and A and abs(u["price"] - a.price) <= max_atr * A:
                pairs.append((db, ui, ai))
    used_u, used_a, out = set(), set(), []
    for db, ui, ai in sorted(pairs):
        if ui in used_u or ai in used_a:
            continue
        used_u.add(ui); used_a.add(ai); out.append((ui, ai))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    cfg = json.loads(CFG.read_text("utf-8"))
    lf = ROOT / "data/soul/structure_labels/2026-10-13_labeling_v3.json"
    if hashlib.sha256(lf.read_bytes()).hexdigest() != cfg["labels_sha256"]:
        raise SystemExit("标注文件哈希与配置不符")
    labels = json.loads(lf.read_text("utf-8"))["labels"]
    man = json.loads((ROOT / "data/backtest/structure-v0/labeling_v3/manifest.json").read_text("utf-8"))
    series = {}
    for s in man["samples"]:
        key = s.get("series", s["symbol"])
        if key in series:
            continue
        if s["symbol"] == "PAXG":
            series[key] = lp.paxg_4h()[0]
        elif s["tf"] == "4h":
            series[key] = lp.yahoo_4h(s["symbol"])[0]
        else:
            series[key] = [(str(b[0]), *b[1:]) for b in pe.parse(pe.fetch(s["symbol"])[0])[0]]
    mb, ma = cfg["matching"]["max_bar_diff"], cfg["matching"]["max_price_diff_atr"]
    per_chart, agg = [], {}
    for s in man["samples"]:
        lab = labels.get(str(s["id"]), {})
        bars = series[s.get("series", s["symbol"])]
        i_end, W = s["prefix_index"], man["window"][s["tf"]]
        pre = bars[:i_end + 1]
        off = i_end - W + 1
        user, unsure = [], []
        for mk in lab.get("marks", []):
            cls = "H" if mk["type"] in H_CLS else ("L" if mk["type"] in L_CLS else None)
            if cls is None:
                continue
            u = {"cls": cls, "idx": off + mk["i"], "price": mk["price"], "type": mk["type"]}
            (unsure if mk.get("uncertain") else user).append(u)
        atr, cands = candidates(pre)
        blind = lab.get("trend") == "看不出" and lab.get("stage") == "看不出"
        row = {"id": s["id"], "set": "selection" if s["id"] <= 45 else "holdout", "symbol": s["symbol"], "tf": s["tf"],
               "user_points": len(user), "user_unsure": len(unsure), "user_blind": blind}
        for cid, piv in cands.items():
            vis = [p for p in piv if p.idx >= off and p.conf <= i_end]
            m = match(user, vis, atr, mb, ma)
            mu = match(unsure, vis, atr, mb, ma)
            cls_ok = cls_n = 0
            for ui, ai in m:
                u, p = user[ui], vis[ai]
                prev = [q for q in vis[:ai] if q.kind == p.kind]
                if u["type"] in ("HH", "LH", "HL", "LL") and prev:
                    algo_t = ("HH" if p.price > prev[-1].price else "LH") if p.kind == "H" else ("HL" if p.price > prev[-1].price else "LL")
                    cls_n += 1; cls_ok += algo_t == u["type"]
            lag = [p.conf - p.idx for p in vis]
            row[cid] = {"algo_points": len(vis), "matched": len(m), "unsure_hit": len(mu), "cls_ok": cls_ok, "cls_n": cls_n,
                        "lag_median": sorted(lag)[len(lag) // 2] if lag else None}
            g = agg.setdefault((row["set"], cid), {"user": 0, "algo": 0, "matched": 0, "unsure": 0, "unsure_hit": 0, "cls_ok": 0, "cls_n": 0, "lags": [],
                                                    "blind_algo_points": 0})
            if blind:
                g["blind_algo_points"] += len(vis)
            else:
                g["user"] += len(user); g["algo"] += len(vis); g["matched"] += len(m)
            g["unsure"] += len(unsure); g["unsure_hit"] += len(mu); g["cls_ok"] += cls_ok; g["cls_n"] += cls_n; g["lags"] += lag
        per_chart.append(row)
    res = {}
    for (st, cid), g in agg.items():
        rec = g["matched"] / g["user"] if g["user"] else None
        prec = g["matched"] / g["algo"] if g["algo"] else None
        f1 = 2 * rec * prec / (rec + prec) if rec and prec else None
        lags = sorted(g["lags"])
        res.setdefault(st, {})[cid] = {"recall": _r(rec), "precision": _r(prec), "f1": _r(f1), "user_points": g["user"], "algo_points": g["algo"],
                                       "matched": g["matched"], "unsure_hit": f"{g['unsure_hit']}/{g['unsure']}",
                                       "class_agree": f"{g['cls_ok']}/{g['cls_n']}", "lag_median_bars": lags[len(lags) // 2] if lags else None,
                                       "algo_points_on_blind_charts": g["blind_algo_points"]}
    sel = res["selection"]
    best = max(sel, key=lambda c: (sel[c]["f1"] or 0))
    close = [c for c in sel if (sel[best]["f1"] or 0) - (sel[c]["f1"] or 0) < 0.03]
    choice = min(close, key=lambda c: sel[c]["lag_median_bars"] or 99)
    summary = {"config_version": cfg["version"], "labels_sha256": cfg["labels_sha256"], "selection": sel, "chosen": choice,
               "chosen_rule": cfg["selection_rule"], "holdout_of_chosen": res.get("holdout", {}).get(choice),
               "holdout_all_reported_after_choice": res.get("holdout")}
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    (out / "per_chart.json").write_text(json.dumps(per_chart, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps({"chosen": choice, "selection": sel, "holdout_of_chosen": summary["holdout_of_chosen"]}, ensure_ascii=False, indent=1))
    return 0


def _r(x):
    return None if x is None else round(x, 4)


if __name__ == "__main__":
    sys.exit(main())
