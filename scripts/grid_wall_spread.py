"""墙位卖方价差【联合网格】搜索 —— 逐维扫描会漏掉参数交互

用户 2026-08-31：「跑一遍联合网格」。
逐维扫描时其余维度固定在未优化的基准值上，找到的"最优"可能只是那个基准的局部最优。

网格：cap(5) × confirm(3) × width(4) × dte(4) = 240 组 × 3 侧
防过拟合三件套：
  · 报全网格分布（不只报最优）——最优点若远离分布主体，多半是噪声尖峰
  · 报最优点的参数邻域表现——真信号在邻域内应该稳，过拟合点会断崖
  · 报各维度边际均值——看哪个参数真的重要
"""
import itertools, os, pathlib, statistics, sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
_src = (pathlib.Path(__file__).parent / "scan_wall_spread.py").read_text("utf-8")
exec(_src.split("def main()")[0])          # 复用 load/locked_walls/fill/stat

CAPS = [14, 30, 45, 90, 9999]
CONFIRMS = [1, 2, 3]
WIDTHS = [1.0, 2.0, 3.0, 5.0]
DTES = [(1, 4), (4, 11), (12, 25), (26, 45)]


def run_cached(snaps, closes, kind, walls, switches, width, dte_lo, dte_hi):
    """与 scan_wall_spread.run 同逻辑，但墙位序列由外部传入（避免重复计算）"""
    ds = sorted(snaps)
    out = []
    for d in ds:
        w = walls.get(d)
        if w is None:
            continue
        spot = snaps[d].spot
        legs = defaultdict(dict)
        for c in snaps[d].contracts:
            if c.kind != kind or c.bid is None or not c.ask:
                continue
            if not (dte_lo <= (c.expiry - d).days <= dte_hi):
                continue
            legs[c.expiry][c.strike] = c
        pick = None
        for exp in sorted(legs):
            ks = sorted(legs[exp])
            cand = [x for x in ks if (x > spot if kind == "C" else x < spot)]
            if not cand:
                continue
            s = min(cand, key=lambda x: abs(x - w))
            tgt = s + width if kind == "C" else s - width
            far = [x for x in ks if (x > s if kind == "C" else x < s)]
            if not far:
                continue
            b = min(far, key=lambda x: abs(x - tgt))
            pick = (exp, legs[exp][s], legs[exp][b], s, b)
            break
        if not pick:
            continue
        exp, sc, bc, sk, bk = pick
        credit = fill(sc, bc)
        if credit <= 0:
            continue
        realw = abs(bk - sk) * 100
        if realw <= credit:
            continue
        occ = realw - credit
        ex = next((s for s in sorted(switches) if d < s < exp), None)
        pnl = None
        broke = False
        if ex and ex in snaps:
            s2 = b2 = None
            for c in snaps[ex].contracts:
                if c.expiry != exp or c.kind != kind or c.bid is None or not c.ask:
                    continue
                if abs(c.strike - sk) < 1e-6:
                    s2 = c
                elif abs(c.strike - bk) < 1e-6:
                    b2 = c
            if s2 and b2:
                pnl = credit - fill(s2, b2, closing=True) - FEE * 4
        if pnl is None:
            se = closes.get(exp.isoformat())
            if se is None:
                continue
            itr = (max(0.0, min(se - sk, realw / 100)) if kind == "C"
                   else max(0.0, min(sk - se, realw / 100))) * 100
            pnl = credit - itr - FEE * 4
            broke = (se > sk) if kind == "C" else (se < sk)
        out.append(dict(d=d, pnl=pnl, occ=occ, credit=credit, broke=broke,
                        roi=pnl / occ, hold=((ex or exp) - d).days))
    return out


def main():
    for sym in (sys.argv[1:] or ["SLV", "GLD"]):
        snaps, closes = load(sym)
        ds = sorted(snaps)
        span = (ds[-1] - ds[0]).days or 1
        print(f"\n{'='*112}\n{sym}　联合网格 {len(CAPS)}×{len(CONFIRMS)}×"
              f"{len(WIDTHS)}×{len(DTES)} = {len(CAPS)*len(CONFIRMS)*len(WIDTHS)*len(DTES)} 组"
              f"　样本 {ds[0]} ~ {ds[-1]}（{span}天）\n{'='*112}")
        wcache = {}
        for cap, conf, kind in itertools.product(CAPS, CONFIRMS, ("P", "C")):
            w = locked_walls(snaps, kind, cap, conf)
            sw = {ds[i] for i in range(1, len(ds)) if w[ds[i]] != w[ds[i - 1]]}
            wcache[(kind, cap, conf)] = (w, sw)
        res = []
        for cap, conf, width, dte in itertools.product(CAPS, CONFIRMS, WIDTHS, DTES):
            legs = {}
            for kind in ("P", "C"):
                w, sw = wcache[(kind, cap, conf)]
                legs[kind] = run_cached(snaps, closes, kind, w, sw, width, dte[0], dte[1])
            byd = defaultdict(dict)
            for k in ("P", "C"):
                for r in legs[k]:
                    byd[r["d"]][k] = r
            iron = [dict(d=d, pnl=v["P"]["pnl"] + v["C"]["pnl"],
                         occ=max(v["P"]["occ"], v["C"]["occ"]),
                         roi=(v["P"]["pnl"] + v["C"]["pnl"]) / max(v["P"]["occ"], v["C"]["occ"]),
                         broke=v["P"]["broke"] or v["C"]["broke"],
                         hold=max(v["P"]["hold"], v["C"]["hold"]))
                    for d, v in sorted(byd.items()) if "P" in v and "C" in v]
            for side, rows in (("put", legs["P"]), ("call", legs["C"]), ("iron", iron)):
                s = stat(rows, span)
                if s:
                    res.append(dict(cap=cap, conf=conf, width=width, dte=dte,
                                    side=side, **s))
        for side in ("put", "iron", "call"):
            sub = [r for r in res if r["side"] == side]
            if not sub:
                continue
            anns = sorted(r["annual"] for r in sub)
            pos = sum(1 for a in anns if a > 0) / len(anns)
            print(f"\n── {side}：{len(sub)} 个有效组合　年化分布："
                  f"中位 {statistics.median(anns)*100:+.0f}%　"
                  f"25/75分位 [{anns[len(anns)//4]*100:+.0f}%, {anns[3*len(anns)//4]*100:+.0f}%]　"
                  f"为正的比例 {pos:.0%}")
            top = sorted(sub, key=lambda r: -r["annual"])[:8]
            print(f"   {'cap':>6}{'conf':>5}{'width':>6}{'dte':>9}{'笔数':>5}"
                  f"{'未破墙':>7}{'净盈利':>7}{'总损益':>9}{'ROI':>8}{'占用':>7}{'年化':>8}")
            for r in top:
                dte_lab = f"{r['dte'][0]}~{r['dte'][1]}"
                print(f"   {r['cap']:>6}{r['conf']:>5}{r['width']:>6.0f}"
                      f"{dte_lab:>9}{r['n']:>5}{r['unbroken']:>6.0%}"
                      f"{r['win']:>7.0%}{r['total']:>+9.0f}{r['roi']*100:>+7.1f}%"
                      f"{r['occ']:>7.0f}{r['annual']*100:>+7.0f}%")
            b = top[0]
            print(f"   最优点邻域稳健性（改动单个参数后的年化）：")
            for dim, vals in (("cap", CAPS), ("conf", CONFIRMS),
                              ("width", WIDTHS), ("dte", DTES)):
                line = []
                for v in vals:
                    q = dict(cap=b["cap"], conf=b["conf"], width=b["width"], dte=b["dte"])
                    q[dim] = v
                    m = next((r for r in sub if all(r[k] == q[k] for k in q)), None)
                    mark = "*" if v == b[dim] else ""
                    lab = v if not isinstance(v, tuple) else f"{v[0]}~{v[1]}"
                    line.append(f"{lab}={m['annual']*100:+.0f}%{mark}" if m else f"{lab}=—")
                print(f"     {dim:<6}" + "  ".join(line))
            print(f"   各维度边际（该取值下所有组合的年化中位数）：")
            for dim, vals in (("cap", CAPS), ("conf", CONFIRMS),
                              ("width", WIDTHS), ("dte", DTES)):
                parts = []
                for v in vals:
                    g = [r["annual"] for r in sub if r[dim] == v]
                    if g:
                        lab = v if not isinstance(v, tuple) else f"{v[0]}~{v[1]}"
                        parts.append(f"{lab}={statistics.median(g)*100:+.0f}%")
                print(f"     {dim:<6}" + "  ".join(parts))


if __name__ == "__main__":
    main()
