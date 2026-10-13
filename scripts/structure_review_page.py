"""算法标注复核页：把回测（ps-exit-v4 U5，用户规则）里算法认的高低点、关键位、突破、HL₁、入场、止损、上膛 HH、LH 离场画在图上，
让用户逐项判断对错并标出自己的做法（用户 2026-10-13：「你把你目前标注的，回测的入场离场。多找几个例子，让我来二次标注，看看你标注得怎么样」）。

  python3 scripts/structure_review_page.py --out data/backtest/structure-v0/review_v1

抽样：run5_v4 中 U5 实际入场的交易，按 方向 × 出场类型（LH 离场 / 止损 / 时间到期）分层，每层 3 笔，固定种子；不按盈亏挑。
这是复核页（不是盲标）：图里含入场后的走势，用来判断算法标得对不对，不用于统计「当时能否认出」。
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
from undertow.analyze import price_structure as ps  # noqa: E402

SEED = 20261016
PER = 3
RUN = ROOT / "data/backtest/ps-exit-v1/run5_v4/trades.jsonl"


def grp(reason: str) -> str:
    return "LH 离场" if "struct" in reason else ("止损" if "stop" in reason else "时间到期")


def build(sym: str, ref_dates: list, cache: dict):
    if sym not in cache:
        d, _ = pe.fetch(sym)
        bars_all = pe.parse(d)[0]
        segs, _ = pe.segments(bars_all, [b[0] for b in bars_all] if sym in pe.FUTURES else ref_dates)
        out = []
        for bars in segs:
            atr = ps.wilder_atr(bars)
            piv, eps = ps.detect(bars, atr)
            out.append((bars, atr, piv, eps))
        cache[sym] = out
    return cache[sym]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    rows = [json.loads(x) for x in RUN.read_text("utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if r["policies"]["U5"]["status"] == "entered"]
    rng = random.Random(SEED)
    picks = []
    for d in ("long", "short"):
        for g in ("LH 离场", "止损", "时间到期"):
            pool = sorted([r for r in rows if r["direction"] == d and grp(r["policies"]["U5"]["exit_reason"]) == g], key=lambda r: r["id"])
            picks += [(r, g) for r in rng.sample(pool, min(PER, len(pool)))]
    rng.shuffle(picks)
    ref = pe.fetch("SPY")[0]
    ref_dates = [b[0] for b in pe.parse(ref)[0]]
    cache, data, manifest = {}, [], []
    for k, (r, g) in enumerate(picks, 1):
        sym, si, d, start = r["id"].rsplit("-", 3)[0], int(r["id"].rsplit("-", 3)[1]), r["direction"], int(r["id"].rsplit("-", 3)[3])
        bars, atr, piv, eps = build(sym, ref_dates, cache)[si]
        e = next(x for x in eps if x.kind == "A" and x.direction == d and x.start == start)
        s = 1 if d == "long" else -1
        sig, ei = e.signal_at, e.signal_at + 1
        stop = e.extreme.price - s * ps.STOP_BUF * atr[sig - 1]
        res = ps.simulate_policy(bars, piv, ei, stop, d, ps.POLICIES["U5"], last_i=len(bars) - 1)
        lo = max(0, min(e.key.idx, e.extreme.idx, e.regime_at or e.key.idx) - 40)
        hi = min(len(bars) - 1, res["exit_i"] + 25)
        W = lambda i: i - lo
        marks = []
        prevs = {}
        for p in piv:                                            # 全部算法高低点（收盘之字形 2ATR），按与前一个同类点比较分类
            if lo <= p.idx <= hi and p.conf <= hi:
                pr = prevs.get(p.kind)
                lab = ("HH" if (pr is None or p.price > pr) else "LH") if p.kind == "H" else ("HL" if (pr is None or p.price > pr) else "LL")
                marks.append({"g": "pivot", "i": W(p.idx), "conf": W(p.conf), "label": lab, "price": p.price, "kind": p.kind})
            prevs[p.kind] = p.price
        ev = [("关键前高" if d == "long" else "关键前低", e.key.idx, e.key.price), ("LL（止损参照）" if d == "long" else "HH（止损参照）", e.extreme.idx, e.extreme.price),
              ("突破", e.break_at, bars[e.break_at][4]), ("HL₁" if d == "long" else "LH₁", e.hl1.idx, e.hl1.price), ("入场", ei, bars[ei][1])]
        au = res.get("audit", {})
        if au.get("armed_at"):
            ev.append(("新 HH（上膛）" if d == "long" else "新 LL（上膛）", au["armed_at"]["idx"], au["armed_at"]["price"]))
        if au.get("struct_point"):
            ev.append(("LH（离场信号）" if d == "long" else "HL（离场信号）", au["struct_point"]["idx"], au["struct_point"]["price"]))
        ev.append((f"离场（{res['exit_reason']}）", res["exit_i"], res["exit_price"]))
        for name, i, px in ev:
            marks.append({"g": "event", "i": W(i), "label": name, "price": px})
        data.append({"id": k, "symbol": sym, "direction": d, "group": g, "stop": stop, "entry_i": W(ei), "exit_i": W(res["exit_i"]),
                     "result_atr": round(s * (res["exit_price"] - bars[ei][1]) / atr[ei - 1], 2),
                     "bars": [[str(b[0]), round(b[1], 4), round(b[2], 4), round(b[3], 4), round(b[4], 4)] for b in bars[lo:hi + 1]], "marks": marks})
        manifest.append({"id": k, "trade_id": r["id"], "group": g, "direction": d, "symbol": sym, "entry_date": str(bars[ei][0]),
                         "exit_date": str(bars[res["exit_i"]][0]), "exit_reason": res["exit_reason"], "result_atr": data[-1]["result_atr"]})
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"seed": SEED, "per_stratum": PER, "source": str(RUN.relative_to(ROOT)), "policy": "U5",
                                                   "trades": manifest}, ensure_ascii=False, indent=1), "utf-8")
    page = (ROOT / "scripts/structure_review_page.html").read_text("utf-8").replace("__DATA__", json.dumps(data, ensure_ascii=False))
    (out / "index.html").write_text(page, "utf-8")
    print(f"{len(data)} 笔 → {(out / 'index.html').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
