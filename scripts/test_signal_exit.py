"""分离「换墙」与「平仓」—— 用户 2026-08-31 指出的设计错误

原实现把两者绑死：只有墙位确认切换（conf 天）才平仓。用户：
「出现破墙的极端信号就要立即平仓。conf 修改会导致隔天，所以亏损变大。」

数据印证：call 侧 conf 1=+181% → 2=-229%，晚一天就崩。

正确设计：
  · 换墙（决定卖哪个行权价）：可以用 conf 平滑，滤掉一日抖动
  · 平仓（保护已有仓位）：反向极强信号出现【当天】就平，不等墙确认
"""
import itertools, os, pathlib, statistics, sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
_src = (pathlib.Path(__file__).parent / "scan_wall_spread.py").read_text("utf-8")
exec(_src.split("def main()")[0])
from undertow.analyze.flow import analyze_flow, tradeable_info      # noqa: E402


def build_signals(snaps):
    ds = sorted(snaps)
    sig = {}
    for i in range(1, len(ds)):
        d, p = ds[i], ds[i - 1]
        try:
            fa = analyze_flow(snaps[p], snaps[d], today=_prev_weekday(d),
                              prev_date=p.isoformat(), curr_date=d.isoformat(),
                              horizon_days=45)
            ti = tradeable_info(fa)
            sig[d] = (ti.get("side"), ti.get("ratio", 0.0))
        except Exception:
            pass
    return sig


def run2(snaps, closes, kind, walls, switches, width, dte_lo, dte_hi,
         sig, sig_thr=None, dist_thr=None):
    """sig_thr=None 只按换墙平仓；给阈值则反向极强信号当天平仓。

    dist_thr（用户 2026-08-31 补充）：极端信号只有在【价格已经贴近卖腿】时才需要
    平仓 —— 「如果离得很远，其实也无所谓」。卖 60C 而现价 52 时出现极强看涨，
    离墙 15%，一周内涨到 60 的概率极低，平仓纯属白花点差。
    dist_thr=0.03 表示只在 |卖腿/现价-1| ≤ 3% 时才响应信号。
    """
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
            if dte_lo <= (c.expiry - d).days <= dte_hi:
                legs[c.expiry][c.strike] = c
        pick = None
        for exp in sorted(legs):
            ks = sorted(legs[exp])
            cand = [x for x in ks if (x > spot if kind == "C" else x < spot)]
            if not cand:
                continue
            s = min(cand, key=lambda x: abs(x - w))
            far = [x for x in ks if (x > s if kind == "C" else x < s)]
            if not far:
                continue
            b = min(far, key=lambda x: abs(x - (s + width if kind == "C" else s - width)))
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
        # 出场日：换墙日 或 反向极强信号日，取更早的
        ex, why = None, None
        for nd in [x for x in ds if d < x < exp]:
            if sig_thr is not None:
                sd = sig.get(nd)
                if sd and sd[1] >= sig_thr:
                    adverse = ((sd[0] == "看涨" and kind == "C")
                               or (sd[0] == "看跌" and kind == "P"))
                    if adverse:
                        near = True
                        if dist_thr is not None:
                            sp2 = snaps[nd].spot if nd in snaps else spot
                            near = abs(sk / sp2 - 1) <= dist_thr
                        if near:
                            ex, why = nd, "信号"
                            break
            if nd in switches:
                ex, why = nd, "换墙"
                break
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
            ex, why = None, "到期"
        out.append(dict(d=d, pnl=pnl, occ=occ, broke=broke, roi=pnl / occ,
                        hold=((ex or exp) - d).days, why=why))
    return out


def main():
    for sym in (sys.argv[1:] or ["SLV"]):
        snaps, closes = load(sym)
        ds = sorted(snaps)
        span = (ds[-1] - ds[0]).days or 1
        sig = build_signals(snaps)
        nsig = sum(1 for v in sig.values() if v[1] >= 10)
        print(f"\n{'='*100}\n{sym}　{ds[0]}~{ds[-1]}　"
              f"≥10× 的信号 {nsig} 天 / {len(sig)} 天\n{'='*100}")
        for cap, conf, width, dte in ((9999, 1, 2.0, (4, 11)),
                                      (9999, 3, 2.0, (4, 11))):
            wc = {k: locked_walls(snaps, k, cap, conf) for k in ("P", "C")}
            sw = {k: {ds[i] for i in range(1, len(ds)) if wc[k][ds[i]] != wc[k][ds[i-1]]}
                  for k in ("P", "C")}
            print(f"\n── cap={cap} conf={conf} width={width:g} dte={dte[0]}~{dte[1]}")
            print(f"   {'平仓规则':<20}{'侧':<7}{'笔数':>5}{'信号平仓':>9}{'换墙平仓':>9}"
                  f"{'未破墙':>7}{'总损益':>9}{'ROI':>8}{'年化':>8}")
            combos = [(None, None, "仅换墙")]
            for t in (30, 10):
                for dd in (None, 0.05, 0.03, 0.02):
                    dl = "不限距离" if dd is None else f"且距墙≤{dd:.0%}"
                    combos.append((t, dd, f"≥{t}×{dl}"))
            for thr, dthr, tl in combos:
                legs = {}
                for kind, kl in (("P", "卖put"), ("C", "卖call")):
                    rows = run2(snaps, closes, kind, wc[kind], sw[kind],
                                width, dte[0], dte[1], sig, thr, dthr)
                    legs[kind] = rows
                    s = stat(rows, span)
                    if not s:
                        continue
                    ns = sum(1 for r in rows if r["why"] == "信号")
                    nw = sum(1 for r in rows if r["why"] == "换墙")
                    print(f"   {tl:<20}{kl:<7}{s['n']:>5}{ns:>9}{nw:>9}"
                          f"{s['unbroken']:>6.0%}{s['total']:>+9.0f}"
                          f"{s['roi']*100:>+7.1f}%{s['annual']*100:>+7.0f}%")
                byd = defaultdict(dict)
                for k in ("P", "C"):
                    for r in legs[k]:
                        byd[r["d"]][k] = r
                iron = [dict(d=d, pnl=v["P"]["pnl"] + v["C"]["pnl"],
                             occ=max(v["P"]["occ"], v["C"]["occ"]),
                             roi=(v["P"]["pnl"] + v["C"]["pnl"]) / max(v["P"]["occ"], v["C"]["occ"]),
                             broke=v["P"]["broke"] or v["C"]["broke"],
                             hold=max(v["P"]["hold"], v["C"]["hold"]), why="")
                        for d, v in sorted(byd.items()) if "P" in v and "C" in v]
                s = stat(iron, span)
                if s:
                    print(f"   {'':<20}{'铁鹰':<7}{s['n']:>5}{'':>9}{'':>9}"
                          f"{s['unbroken']:>6.0%}{s['total']:>+9.0f}"
                          f"{s['roi']*100:>+7.1f}%{s['annual']*100:>+7.0f}%")


if __name__ == "__main__":
    main()
