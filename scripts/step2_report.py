"""读 step2_grid.jsonl 出汇总表。改口径只需改这里，不必重跑网格。"""
import argparse
import json
import pathlib
import statistics
from collections import defaultdict

G = pathlib.Path("data/backtest/step2_grid.jsonl")


def load(sym=None, side=None):
    out = []
    with G.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if sym and r["sym"] != sym:
                continue
            if side and r["side"] != side:
                continue
            out.append(r)
    return out


def lab(off):
    return "墙上" if off == 0 else (f"墙内{off}" if off > 0 else f"墙外{-off}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--side", default="P")
    ap.add_argument("--width", type=int, default=3)
    ap.add_argument("--metric", default="netday",
                    choices=["netday", "broke", "pnl", "credit", "roi"])
    a = ap.parse_args()
    rows = [r for r in load(a.symbol, a.side) if r["width_n"] == a.width]
    by = defaultdict(list)
    for r in rows:
        by[(r["dte"], r["off"])].append(r)
    offs = sorted({r["off"] for r in rows})
    name = {"netday": "净日均$（权利金−手续费）÷DTE", "broke": "破卖腿率",
            "pnl": "总损益$", "credit": "均权利金$", "roi": "均单笔ROI"}[a.metric]
    print(f"{a.symbol} {'put' if a.side=='P' else 'call'} 侧　宽度 {a.width} 档　"
          f"{name}\n")
    print(f"{'DTE':>4}" + "".join(f"{lab(o):>10}" for o in offs))
    for dte in range(1, 46):
        cells, any_ = [], False
        for o in offs:
            g = by.get((dte, o), [])
            if len(g) < 3:
                cells.append(f"{'-':>10}")
                continue
            any_ = True
            if a.metric == "netday":
                v = statistics.mean(x["credit"] - x["fee"] for x in g) / dte
                cells.append(f"{v:>10.2f}")
            elif a.metric == "broke":
                cells.append(f"{sum(1 for x in g if x['broke'])/len(g):>9.0%} ")
            elif a.metric == "pnl":
                cells.append(f"{sum(x['pnl'] for x in g):>+10.0f}")
            elif a.metric == "credit":
                cells.append(f"{statistics.mean(x['credit'] for x in g):>10.1f}")
            else:
                cells.append(f"{statistics.mean(x['pnl']/x['occ'] for x in g if x['occ']>0):>+9.1%} ")
        if any_:
            print(f"{dte:>4}" + "".join(cells))


if __name__ == "__main__":
    main()
