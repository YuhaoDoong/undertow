"""逐根回放 + 筹码标注页（replay_v2；用户 2026-10-14：「你要不再生成一批，前面k线多放点。我看看能不能把道氏结构和……筹码理论结合起来。你给我一个筹码标注工具」）。

  python3 scripts/structure_replay_chip_page.py --out data/backtest/structure-v0/replay_v2

与 replay_v1 的区别：
  背景 300 根（v1 为 100）；日线 ETF 加 TLT、IWM，每个标的最多 3 段；
  多带成交量，页面右侧画筹码分布（只用当前及之前的 K 线；两种口径：K 线堆叠 = 触及该价位箱的根数，即外部作者二看图的做法；成交量 = 按 [low, high] 均摊）；
  可标密集区 / 真空区（两次点击定上下沿）、支撑位 / 压力位（水平线）、突破起点。
抽样仍为四种情景各 3 段、固定种子；窗口与 labeling_v3、review_v1、replay_v1 以及彼此都不重叠。
日期隐去（相对根数）、价格按起点收盘 = 100 换算，成交量按背景段中位数 = 1 换算；真实日期只在 manifest。未来 K 线在页面数据里，靠约定不翻源码。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from scripts.structure_label_page_v2 import scenario, SCEN  # noqa: E402
from scripts.structure_replay_page import overlaps, seen_windows  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

SEED = 20261018
LEFT, RIGHT, PER, PER_SYM = 300, 150, 3, 3
SYMS = ("GLD", "SLV", "SPY", "QQQ", "USO", "TLT", "IWM")
V1 = ROOT / "data/backtest/structure-v0/replay_v1/manifest.json"


def bars_with_volume(sym: str) -> list:
    """(date, o, h, l, c, v)；与 pe.parse 同样剔除 OHLC 缺失行，成交量缺失记 None（不折 0）。"""
    r = pe.fetch(sym)[0]["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(r["timestamp"]):
        row = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if any(x is None for x in row):
            continue
        out.append((datetime.fromtimestamp(ts, timezone.utc).date(), *row, q["volume"][i]))
    assert [b[:5] for b in out] == pe.parse(pe.fetch(sym)[0])[0], sym
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    rng = random.Random(SEED)
    seen = seen_windows()
    for s in json.loads(V1.read_text("utf-8"))["samples"]:          # replay_v1 用的是同一套日线的下标
        seen.setdefault(s["symbol"], []).append(("v3", s["start_index"] - 99, s["start_index"] + 150))
    series, pools = {}, {k: [] for k in SCEN}
    for sym in SYMS:
        bars = bars_with_volume(sym)
        series[sym] = bars
        atr = ps.wilder_atr([b[:5] for b in bars])
        zz = ps.zigzag([b[:5] for b in bars], atr)
        for i in range(LEFT + 100, len(bars) - RIGHT, 5):
            if bars[i + RIGHT][0] > pe.DESIGN_END:                 # 整个窗口在设计集内
                continue
            sc = scenario([b[:5] for b in bars], atr, zz, i)
            if sc:
                pools[sc].append((sym, i))
    taken, samples, per_sym = [], [], Counter()
    for sc in SCEN:
        cand = pools[sc][:]
        rng.shuffle(cand)
        got = 0
        for sym, i in cand:
            lo, hi = i - LEFT + 1, i + RIGHT
            if per_sym[sym] >= PER_SYM or overlaps(sym, series[sym], lo, hi, seen, taken):
                continue
            vols = [series[sym][k][5] for k in range(lo, i + 1)]
            if any(v is None for v in vols) or statistics.median(vols) <= 0:
                continue
            taken.append((sym, lo, hi))
            per_sym[sym] += 1
            samples.append({"symbol": sym, "start_index": i, "start_date": str(series[sym][i][0]), "scenario": sc,
                            "window": [str(series[sym][lo][0]), str(series[sym][hi][0])]})
            got += 1
            if got == PER:
                break
    rng.shuffle(samples)
    data = []
    for k, s in enumerate(samples, 1):
        s["id"] = k
        bars, i0 = series[s["symbol"]], s["start_index"]
        f = 100.0 / bars[i0][4]
        vm = statistics.median(bars[j][5] for j in range(i0 - LEFT + 1, i0 + 1))
        s["price_scale"], s["volume_scale"] = f, 1.0 / vm
        data.append({"id": k, "symbol": s["symbol"], "left": LEFT,
                     "bars": [[j - i0, *(round(v * f, 3) for v in bars[j][1:5]), None if bars[j][5] is None else round(bars[j][5] / vm, 3)]
                              for j in range(i0 - LEFT + 1, i0 + RIGHT + 1)]})
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"seed": SEED, "left": LEFT, "right": RIGHT, "symbols": SYMS, "per_symbol_cap": PER_SYM,
                                                    "scenarios": SCEN, "design_end": "2015-12-31（整个窗口）",
                                                    "non_overlap_with": ["labeling_v3 日线窗口", "review_v1 交易图窗", "replay_v1 窗口", "彼此"],
                                                    "samples": samples}, ensure_ascii=False, indent=1), "utf-8")
    page = (ROOT / "scripts/structure_replay_chip_page.html").read_text("utf-8").replace("__DATA__", json.dumps(data, ensure_ascii=False))
    (out / "index.html").write_text(page, "utf-8")
    print(f"{len(samples)} 段 → {(out / 'index.html').relative_to(ROOT)}；{dict(Counter(s['scenario'] for s in samples))}；{dict(per_sym)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
