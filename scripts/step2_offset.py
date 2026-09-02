"""第 2.2 / 2.3 步：卖腿相对墙位偏移　（墙内 / 墙上 / 墙外）

用户 2026-09-02 的编号：
  2.1 卖在墙上（scripts/step2_spread.py）
  2.2 卖在墙内（offset > 0，朝现价方向）
  2.3 卖在墙外（offset < 0，远离现价）

口径与 2.1 完全一致：卖腿由第一步的墙位偏移得到，买腿再往外 width_n 档，
全部持有到期，成交按中价让 25% 点差，手续费 $3.20/笔。
「破」的判定改为**破卖腿**（不是破墙）——卖腿才是真正的风险边界。
"""
import argparse
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                  # noqa: E402
from undertow.analyze.gamma import pick_sell_wall               # noqa: E402
import importlib.util                                            # noqa: E402
_s2 = importlib.util.spec_from_file_location(
    "s2", str(pathlib.Path(__file__).parent / "step2_spread.py"))
_m = importlib.util.module_from_spec(_s2)
sys.argv = [sys.argv[0]]
_s2.loader.exec_module(_m)


def build(sym, kind, offsets, width_n=3, max_dte=45):
    snaps, closes, dates = _m.load(sym)
    out = []
    for sess in sorted(snaps):
        snap = snaps[sess]
        prior = [x for x in dates if x < sess.isoformat()]
        if not prior:
            continue
        spot = closes[prior[-1]]
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        r = pick_sell_wall(snap, obs, spot, kind)
        if r is None:
            continue
        W = r["strike"]
        legs = defaultdict(dict)
        for c in snap.contracts:
            if c.kind == kind and c.bid is not None and c.ask:
                legs[c.expiry][c.strike] = c
        for exp in sorted(legs):
            dte = (exp - sess).days
            if not (1 <= dte <= max_dte):
                continue
            ks = sorted(legs[exp])
            if W not in ks:
                continue
            se = closes.get(exp.isoformat())
            if se is None:
                continue
            i = ks.index(W)
            for off in offsets:
                # offset>0 朝现价（墙内）；<0 远离现价（墙外）
                si = i + off if kind == "P" else i - off
                if si < 0 or si >= len(ks):
                    continue
                S = ks[si]
                if kind == "P" and S >= spot:
                    continue                      # 不卖实值
                if kind == "C" and S <= spot:
                    continue
                bi = si - width_n if kind == "P" else si + width_n
                if bi < 0 or bi >= len(ks):
                    continue
                B = ks[bi]
                credit = ws._fill(legs[exp][S], legs[exp][B])
                width = abs(B - S) * 100
                if width <= 0:
                    continue
                itr = (max(0.0, min(S - se, width / 100)) if kind == "P"
                       else max(0.0, min(se - S, width / 100))) * 100
                out.append(dict(
                    d=sess, dte=dte, off=off, S=S, W=W, spot=spot, se=se,
                    buf=abs(S / spot - 1) * 100, credit=credit, width=width,
                    pnl=credit - itr - 3.2, occ=width - credit, itr=itr,
                    broke=(se < S) if kind == "P" else (se > S)))
    return out


def label(off):
    return "墙上" if off == 0 else (f"墙内{off}档" if off > 0 else f"墙外{-off}档")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--dtes", default="2,4,7,9,14")
    ap.add_argument("--width", type=int, default=3)
    a = ap.parse_args()
    offs = [-3, -2, -1, 0, 1, 2, 3]
    dtes = [int(x) for x in a.dtes.split(",")]
    for kind, kl in (("P", "put"), ("C", "call")):
        rows = build(a.symbol, kind, offs, a.width)
        by = defaultdict(list)
        for r in rows:
            by[(r["dte"], r["off"])].append(r)
        print(f"\n{'='*88}\n{a.symbol} {kl} 侧　宽度固定 {a.width} 档　"
              f"判定=破卖腿（持有到期）\n{'='*88}")
        print(f"{'DTE':>4}{'位置':>9}{'笔':>4}{'均缓冲':>8}{'权利金$':>9}"
              f"{'净日均$':>9}{'破卖腿':>8}{'均破深$':>9}{'总损益$':>9}")
        for dte in dtes:
            for off in offs:
                g = by.get((dte, off), [])
                if len(g) < 3:
                    continue
                cr = statistics.mean(x["credit"] for x in g)
                bad = [x for x in g if x["broke"]]
                deep = statistics.mean(x["itr"] for x in bad) if bad else 0.0
                print(f"{dte:>4}{label(off):>9}{len(g):>4}"
                      f"{statistics.mean(x['buf'] for x in g):>7.1f}%{cr:>9.1f}"
                      f"{(cr-3.2)/dte:>9.2f}{len(bad)/len(g):>7.1%}"
                      f"{deep:>9.0f}{sum(x['pnl'] for x in g):>+9.0f}")
            print()


if __name__ == "__main__":
    main()
