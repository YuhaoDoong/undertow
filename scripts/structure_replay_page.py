"""逐根回放标注页（试点 12 段；Codex 040/041 工作包；用户 2026-10-14：「做逐根回放标注吧」）。

  python3 scripts/structure_replay_page.py --out data/backtest/structure-v0/replay_v1

抽样：日线 ETF（GLD、SLV、SPY、QQQ、USO，≤2015），起点处按当时可知的情景分层（下跌延续 / 下跌后反弹 / 上涨延续 / 上涨后回落），各 3 段，
固定种子；片段窗口 [起点−100, 起点+150] 与 labeling_v3（日线图窗）、review_v1（复核交易图窗）以及彼此都不重叠，避免用户凭记忆认出后续。
页面先显示起点前 100 根，用户逐根推进；每个标记记下「标注时所在的根」（as_of）。删除是追加记录，不抹历史。
页面隐去日期（横轴为相对根数，0 = 起点）、价格按起点收盘 = 100 换算，免得凭年份或价位认出 2000、2008 这类行情；真实日期只在 manifest。
未来的 K 线数据在页面里（浏览器端隐藏），请不要翻看页面源码 —— 这是靠约定，不是技术隔离。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from scripts.structure_label_page_v2 import scenario, SCEN  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

SEED = 20261017
LEFT, RIGHT, PER = 100, 150, 3
SYMS = ("GLD", "SLV", "SPY", "QQQ", "USO")


def seen_windows() -> dict:
    """已给用户看过的日线图窗（按标的）：labeling_v3 日线 250 根窗口、review_v1 每笔交易的完整图窗。"""
    out: dict = {}
    m3 = json.loads((ROOT / "data/backtest/structure-v0/labeling_v3/manifest.json").read_text("utf-8"))
    for s in m3["samples"]:
        if s["tf"] == "1d":
            out.setdefault(s["symbol"], []).append(("v3", s["prefix_index"] - 249, s["prefix_index"]))
    rv = ROOT / "data/backtest/structure-v0/review_v1/index.html"
    if rv.exists():
        txt = rv.read_text("utf-8")
        data = json.loads(txt[txt.index("const DATA = ") + 13:txt.index(";\nconst KEY")])
        for d in data:
            out.setdefault(d["symbol"], []).append(("review", d["bars"][0][0], d["bars"][-1][0]))
    return out


def overlaps(sym, bars, lo, hi, seen, taken) -> bool:
    d_lo, d_hi = str(bars[lo][0]), str(bars[hi][0])
    for tag, a, b in seen.get(sym, []):
        if tag == "v3":
            if not (hi < a or lo > b):
                return True
        elif not (d_hi < a or d_lo > b):
            return True
    return any(not (hi < a or lo > b) for s, a, b in taken if s == sym)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    rng = random.Random(SEED)
    seen = seen_windows()
    series, pools = {}, {k: [] for k in SCEN}
    for sym in SYMS:
        bars = pe.parse(pe.fetch(sym)[0])[0]
        series[sym] = bars
        atr = ps.wilder_atr(bars)
        zz = ps.zigzag(bars, atr)
        for i in range(max(LEFT, 400), len(bars) - RIGHT, 5):
            sc = scenario(bars, atr, zz, i)
            if sc:
                pools[sc].append((sym, i))
    taken, samples = [], []
    for sc in SCEN:
        cand = pools[sc][:]
        rng.shuffle(cand)
        got = 0
        for sym, i in cand:
            lo, hi = i - LEFT + 1, i + RIGHT
            if overlaps(sym, series[sym], lo, hi, seen, taken):
                continue
            taken.append((sym, lo, hi))
            samples.append({"symbol": sym, "start_index": i, "start_date": str(series[sym][i][0]), "scenario": sc,
                            "window": [str(series[sym][lo][0]), str(series[sym][hi][0])]})
            got += 1
            if got == PER:
                break
    rng.shuffle(samples)
    for k, s in enumerate(samples, 1):
        s["id"] = k
    data = []
    for s in samples:                                            # 隐去日期（相对根数，0 = 起点）、价格按起点收盘 = 100：免得用户凭年份/价位认出行情
        bars, i0 = series[s["symbol"]], s["start_index"]
        f = 100.0 / bars[i0][4]
        data.append({"id": s["id"], "symbol": s["symbol"], "left": LEFT,
                     "bars": [[k - i0, *(round(v * f, 3) for v in bars[k][1:5])] for k in range(i0 - LEFT + 1, i0 + RIGHT + 1)]})
        s["price_scale"] = f
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"seed": SEED, "left": LEFT, "right": RIGHT, "symbols": SYMS, "scenarios": SCEN,
                                                    "non_overlap_with": ["labeling_v3 日线窗口", "review_v1 交易图窗", "彼此"],
                                                    "samples": samples}, ensure_ascii=False, indent=1), "utf-8")
    page = (ROOT / "scripts/structure_replay_page.html").read_text("utf-8").replace("__DATA__", json.dumps(data, ensure_ascii=False))
    (out / "index.html").write_text(page, "utf-8")
    from collections import Counter
    print(f"{len(samples)} 段 → {(out / 'index.html').relative_to(ROOT)}；{dict(Counter(s['scenario'] for s in samples))}；{dict(Counter(s['symbol'] for s in samples))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
