"""第八步：卖哪一侧？—— 增仓层方向 × 近墙破墙。

用户 2026-09-25：「卖的方向是否可以根据技术分析指标判断出来？只要方向对了，就不会破墙。」
仓库里方向层的证据：技术面子模块（RSI/MACD/KDJ 等，validation.ta_indicators_direction）
60 个组合配对检验无一显著；第四步再证它们是波动率代理。**唯一有单品种证据的方向层是
增仓层（signal_ledger.call_direction）**：金 72%、银 71%（n≈30，二项 p<0.05，未达 MIN_N=50）。

检验：把增仓层当日方向（偏多 → 卖 put 侧为顺；偏空 → 卖 call 侧为顺）与第五步近墙
破墙结果配对。顺方向侧的破墙率是否低于逆方向侧、低于同期随机价位期望？
统计：不重叠子样本（每 k 天取 1）两比例 z；单品种，不合并（金银相关 0.89）。
用法：python3 scripts/step8_direction_x_wall.py [--emit]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step4_filters as s4                                       # noqa: E402
import step5_wall_hold as s5                                     # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource      # noqa: E402
from undertow.collect.store import SnapshotStore                 # noqa: E402
from undertow.core.config import load_config                     # noqa: E402

KEYS = ("gold", "silver", "wti", "qqq")


def pair(res, ledger, k):
    """返回 (顺方向行, 逆方向行)。方向取自 signal_ledger 当日 call_direction。"""
    al, ag = [], []
    for r in res["rows"]:
        if r["k"] != k:
            continue
        L = ledger.get(r["T"].isoformat())
        d = L.get("call_direction") if L else None
        if d not in ("偏多", "偏空"):
            continue
        ((al if ((d == "偏多") == (r["kind"] == "P")) else ag)).append(r)
    return al, ag


def stats(al, ag, k):
    """不重叠子样本上的两比例 z：顺 vs 逆。"""
    def nov(rows):
        if not rows:
            return []
        i0 = rows[0]["iT"]
        return [r for r in rows if (r["iT"] - i0) % k == 0]
    a, g = nov(al), nov(ag)
    ka, kg = sum(r["breach"] for r in a), sum(r["breach"] for r in g)
    z, p = s4.two_prop(ka, len(a), kg, len(g))
    rate = lambda x: (sum(r["breach"] for r in x) / len(x)) if x else float("nan")
    expc = lambda x: (sum(r["Fin"] for r in x) / len(x)) if x else float("nan")
    return {"n_al": len(al), "r_al": rate(al), "e_al": expc(al), "n_ag": len(ag), "r_ag": rate(ag),
            "e_ag": expc(ag), "n_nov": (len(a), len(g)), "z": z, "p": p}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", action="store_true"); args = ap.parse_args()
    cfg = load_config(); src = CboeHistorySource(); store = SnapshotStore()
    emit = {"schema": 1, "asof": date.today().isoformat(), "results": {}}
    print("顺方向侧 = 增仓层偏多时卖 put 墙 / 偏空时卖 call 墙；逆方向侧反之。随机 = 同期同距离任意价位的期望破墙率。")
    print("z/p：不重叠子样本两比例检验（顺 vs 逆）。单品种不合并。\n")
    for key in KEYS:
        ledger = {r["date"]: r for r in json.load(open(ROOT / "data/history/signals" / f"{key}.json"))}
        for mode in ("max", "nearest"):
            res = s5.run(key, cfg.get(key), store, src, mode=mode)
            if not res:
                continue
            for k in (2, 3):
                al, ag = pair(res, ledger, k)
                s = stats(al, ag, k)
                emit["results"][f"{key}|{mode}|k{k}"] = s
                print(f"{key:6s} 墙={mode:7s} k={k}  顺 n={s['n_al']:2d} 破墙 {s['r_al']:4.0%}（随机 {s['e_al']:4.0%}）  "
                      f"逆 n={s['n_ag']:2d} 破墙 {s['r_ag']:4.0%}（随机 {s['e_ag']:4.0%}）  "
                      f"不重叠 {s['n_nov']}  z={s['z']:+.2f} p={s['p']:.3f}")
        print()
    if args.emit:
        out = ROOT / "data/history/wall_spread/direction_x_wall.json"
        out.write_text(json.dumps(emit, ensure_ascii=False, indent=1), "utf-8"); print(f"已落盘 {out}")


if __name__ == "__main__":
    main()
