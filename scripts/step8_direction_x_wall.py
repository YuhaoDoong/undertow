"""第八步：卖哪一侧？—— 增仓层方向 × 近墙破墙。

用户 2026-09-25：「卖的方向是否可以根据技术分析指标判断出来？只要方向对了，就不会破墙。」
仓库里方向层的证据：技术面子模块（RSI/MACD/KDJ 等，validation.ta_indicators_direction）
60 个组合配对检验无一显著；第四步再证它们是波动率代理。**唯一有单品种证据的方向层是
增仓层（signal_ledger.call_direction）**：金 72%、银 71%（n≈30，二项 p<0.05，未达 MIN_N=50）。

检验：把增仓层当日方向（偏多 → 卖 put 侧为顺；偏空 → 卖 call 侧为顺）与第五步近墙
破墙结果配对。顺方向侧的破墙率是否低于逆方向侧、低于同期随机价位期望？
统计：同日两侧配对后，每 k 个交易日取一组；精确 McNemar 双侧检验。
方向台账按其原始快照 captured_at 重映射到决策日；无法核实的行明确剔除。
用法：python3 scripts/step8_direction_x_wall.py [--emit]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step5_wall_hold as s5                                     # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource      # noqa: E402
from undertow.collect.store import SnapshotStore                 # noqa: E402
from undertow.core.config import load_config                     # noqa: E402
from undertow.core.clock import decision_session                 # noqa: E402

KEYS = ("gold", "silver", "wti", "qqq")


def align_ledger(ledger, store, symbol, trading_days):
    """用构造方向的两份快照核实可用时刻，不把台账文件日当决策日。

    同一决策日多份方向只取最晚一份。缺少原快照、captured_at 无效、
    当前快照盘中抓取或前份快照晚于当前快照，都记录原因并剔除。
    """
    aligned, audit = {}, {"accepted": [], "dropped": []}
    metadata = {}

    def captured(day):
        if day not in metadata:
            raw = store.path_of("options", symbol, date.fromisoformat(day)).read_bytes()
            value = json.loads(gzip.decompress(raw)).get("captured_at")
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("缺少有效 captured_at")
            metadata[day] = value
        return metadata[day]

    for day, row in sorted(ledger.items()):
        try:
            ca = captured(day)
            prev_day = row.get("prev_date")
            if not prev_day:
                raise ValueError("缺少方向输入的 prev_date")
            prev_ca = captured(prev_day)
            if prev_ca >= ca:
                raise ValueError("前份快照未早于当前快照")
            T = decision_session(ca, trading_days)
            if T is None:
                raise ValueError("盘中抓取或价格交易日尚未覆盖决策日")
        except (OSError, ValueError, TypeError, EOFError) as exc:
            audit["dropped"].append({"source_date": day, "reason": str(exc)})
            continue
        session = T.isoformat()
        item = {**row, "source_date": day, "decision_date": session,
                "captured_at": ca, "prev_captured_at": prev_ca}
        previous = aligned.get(session)
        if previous is None or ca > previous["captured_at"]:
            if previous is not None:
                audit["dropped"].append({"source_date": previous["source_date"],
                                         "reason": "同一决策日已有更新快照"})
            aligned[session] = item
        else:
            audit["dropped"].append({"source_date": day, "reason": "同一决策日已有更新快照"})
    audit["accepted"] = [{k: row[k] for k in
                          ("source_date", "decision_date", "captured_at", "prev_captured_at")}
                         for _, row in sorted(aligned.items())]
    return aligned, audit


def pair(res, ledger, k):
    """返回两侧候选；ledger 必须已由 align_ledger 映射至实际决策日。"""
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
    """同日完整配对、统一抽样相位，率与精确 McNemar p 均来自同一组日期。

    p 条件于不一致配对数；没有不一致配对时 p=1，没有可比日期时 p=None。
    时间窗不重叠仍不能消除所有行情依赖，p 不是跨行情稳定性的证明。
    """
    if k < 1:
        raise ValueError("k 必须为正")
    by_a, by_g = {r["iT"]: r for r in al}, {r["iT"]: r for r in ag}
    if len(by_a) != len(al) or len(by_g) != len(ag):
        raise ValueError("同一侧同一决策日重复，不能当成独立配对")
    all_days = sorted(set(by_a) | set(by_g))
    common = sorted(set(by_a) & set(by_g))
    dates = [i for i in common if (i - all_days[0]) % k == 0] if all_days else []
    a, g = [by_a[i] for i in dates], [by_g[i] for i in dates]
    ka, kg = sum(r["breach"] for r in a), sum(r["breach"] for r in g)
    a_only = sum(x["breach"] and not y["breach"] for x, y in zip(a, g))
    g_only = sum(y["breach"] and not x["breach"] for x, y in zip(a, g))
    discordant = a_only + g_only
    p = (min(1.0, 2 * sum(math.comb(discordant, j) for j in range(min(a_only, g_only) + 1))
             / 2 ** discordant) if a else None)
    rate = lambda x: (sum(r["breach"] for r in x) / len(x)) if x else float("nan")
    expc = lambda x: (sum(r["Fin"] for r in x) / len(x)) if x else float("nan")
    return {"n_al": len(a), "r_al": rate(a), "e_al": expc(a), "n_ag": len(g), "r_ag": rate(g),
            "e_ag": expc(g), "n_nov": (len(a), len(g)), "p": p, "method": "exact_mcnemar",
            "events_al": ka, "events_ag": kg, "al_only": a_only, "ag_only": g_only,
            "difference": rate(a) - rate(g), "sample_indices": dates,
            "n_pairs_all": len(common), "unpaired_al": len(by_a) - len(common),
            "unpaired_ag": len(by_g) - len(common),
            "raw": {"sample": "overlapping_unpaired", "n_al": len(al), "r_al": rate(al),
                    "e_al": expc(al), "n_ag": len(ag), "r_ag": rate(ag), "e_ag": expc(ag)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--output", type=Path, help="配合 --emit 指定新产物路径，保留原研究结果")
    args = ap.parse_args()
    if args.output is not None and not args.emit:
        ap.error("--output 需要 --emit")
    cfg = load_config(); src = CboeHistorySource(); store = SnapshotStore()
    emit = {"schema": 2, "asof": date.today().isoformat(), "results": {}, "alignment": {}}
    print("顺方向侧 = 增仓层偏多时卖 put 墙 / 偏空时卖 call 墙；逆方向侧反之。随机 = 同期同距离任意价位的期望破墙率。")
    print("展示率与 p 同源：同日两侧完整配对、每 k 个交易日取一组，精确 McNemar 双侧检验。单品种不合并。\n")
    for key in KEYS:
        ledger = {r["date"]: r for r in json.load(open(ROOT / "data/history/signals" / f"{key}.json"))}
        ser = src.fetch_series(cfg.get(key))
        ledger, alignment = align_ledger(ledger, store, cfg.get(key).options.symbol, list(ser.dates))
        emit["alignment"][key] = alignment
        print(f"{key} 方向时序：可核实 {len(ledger)} 个决策日；剔除 {len(alignment['dropped'])} 行")
        for row in alignment["dropped"]:
            print(f"  剔除 {row['source_date']}：{row['reason']}")
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
                      f"配对不重叠 {s['n_nov']}  p={s['p'] if s['p'] is not None else '未知'}  "
                      f"缺侧剔除 {s['unpaired_al']}/{s['unpaired_ag']}")
        print()
    if args.emit:
        out = args.output or ROOT / "data/history/wall_spread/direction_x_wall_v2.json"
        out.write_text(json.dumps(emit, ensure_ascii=False, indent=1), "utf-8"); print(f"已落盘 {out}")


if __name__ == "__main__":
    main()
