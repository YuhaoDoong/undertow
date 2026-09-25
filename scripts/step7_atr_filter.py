"""第七步：ATR 扩张预警的时效、召回率，以及加过滤后的破墙率。

用户 2026-09-25 三问：
  1. 期权墙落后一天，ATR 扩张日能当天提示吗？能提前多少？
     → ATR 与墙是【同一时点】的数据：都在 T−1 收盘定，T 开盘前可读。没有额外滞后。
       "提前多少"= 信号日 i 之后 k 天内的破墙都算它预警到的，本脚本按 k=1..4 给召回率。
  2. 加上 ATR 过滤，破墙率能到多少？
     → 三个母体分别算：20 年基准（2ATR / 5%）、近墙检验（step5 行）、SLV 81 笔真实回测。
  3. 墙是最近的还是最大的？→ step5 --mode nearest 与 max 对比，本脚本汇总。
用法：python3 scripts/step7_atr_filter.py
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step4_filters as s4                                       # noqa: E402
import step5_wall_hold as s5                                     # noqa: E402
from undertow.analyze.stretch import _atr_series                 # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource      # noqa: E402
from undertow.collect.store import SnapshotStore                 # noqa: E402
from undertow.core.config import load_config                     # noqa: E402

THR = (1.2, 1.3)


def atr_ratio(ser):
    atr = _atr_series(ser.highs, ser.lows, ser.closes, 14)
    return s4._ratio_series(atr, 5), atr


def main():
    cfg = load_config(); src = CboeHistorySource(); store = SnapshotStore()
    keys = [k for k, v in cfg.instruments.items() if v.price is not None]
    print("A. 信号 = 决策日 ATR14 / 5 日前 ATR14 ≥ 阈值（T−1 收盘可算，与墙同一时点）。母体：20 年日线，k=3，缓冲 2ATR / 5%")
    print("   精度 = 信号日后破墙率；召回 = 破墙事件里前一日有信号的比例；过滤后 = 去掉信号日后的破墙率；剔除 = 信号日占比")
    print(f"{'品种':7s}{'阈值':>5s}{'剔除':>6s} | {'2ATR↓ 全→滤':>14s}{'召回':>6s} | {'2ATR↑ 全→滤':>14s}{'召回':>6s} | {'5%↓ 全→滤':>13s}{'召回':>6s} | {'5%↑ 全→滤':>13s}{'召回':>6s}")
    agg = {}
    for key in keys:
        ser = src.fetch_series(cfg.get(key)); rows = s4.build(ser)
        for thr in THR:
            sig = [r["vals"]["ATR扩张比"] >= thr for r in rows]
            line = f"{key:7s}{thr:5.1f}{sum(sig)/len(rows):6.1%} |"
            for buf in ("2ATR", "5%"):
                for side in ("down", "up"):
                    key_o = (side, 3, buf)
                    allr = sum(r["out"][key_o] for r in rows) / len(rows)
                    keep = [r for r, s_ in zip(rows, sig) if not s_]
                    filt = sum(r["out"][key_o] for r in keep) / len(keep)
                    breaches = [s_ for r, s_ in zip(rows, sig) if r["out"][key_o]]
                    recall = sum(breaches) / len(breaches) if breaches else float("nan")
                    line += f"{allr:6.1%}→{filt:5.1%}{recall:6.0%} |"
                    agg.setdefault((thr, buf, side), []).append((allr, filt, recall))
            print(line)
    print("\n   15 品种中位：")
    for (thr, buf, side), v in agg.items():
        print(f"   阈值{thr} {buf}{'↓' if side=='down' else '↑'}: 全 {st.median(x[0] for x in v):.1%} → 滤后 {st.median(x[1] for x in v):.1%}  召回 {st.median(x[2] for x in v):.0%}")

    print("\n\nB. 预警时效：破墙窗 [i+1, i+k] 之前，信号出现在 i（前一日）/ i−1 / i−2 的比例（k=3, 2ATR, 阈值 1.3，15 品种中位）")
    lead = {0: [], 1: [], 2: []}
    for key in keys:
        ser = src.fetch_series(cfg.get(key)); rows = s4.build(ser)
        by_i = {r["i"]: r for r in rows}
        for side in ("down", "up"):
            ev = [r for r in rows if r["out"][(side, 3, "2ATR")]]
            for lag in (0, 1, 2):
                hit = [by_i[r["i"] - lag]["vals"]["ATR扩张比"] >= 1.3 for r in ev if (r["i"] - lag) in by_i]
                lead[lag].append(sum(hit) / len(hit) if hit else float("nan"))
    for lag in (0, 1, 2):
        v = [x for x in lead[lag] if x == x]
        print(f"   信号在 i−{lag}：{st.median(v):.0%} 的破墙事件前有过预警")

    print("\n\nC. 近墙检验（step5 行）加过滤：墙定义 max=带内最大 OI / nearest=带内最近；k=2；过滤 = 决策日 ATR 扩张比 <1.3")
    for mode in ("max", "nearest"):
        print(f"  ── {mode} ──")
        for key in ("gold", "silver", "wti", "qqq"):
            inst = cfg.get(key); res = s5.run(key, inst, store, src, mode=mode)
            if not res:
                continue
            ser = src.fetch_series(inst); ratio, _ = atr_ratio(ser); idx = {d: i for i, d in enumerate(ser.dates)}
            for kind in ("P", "C"):
                rr = [r for r in res["rows"] if r["kind"] == kind and r["k"] == 2]
                sig = [(ratio[idx[r["T"]] - 1] or 0) >= 1.3 for r in rr]
                keep = [r for r, s_ in zip(rr, sig) if not s_]
                o_all = sum(r["breach"] for r in rr) / len(rr); e_all = sum(r["Fin"] for r in rr) / len(rr)
                o_f = sum(r["breach"] for r in keep) / len(keep) if keep else float("nan")
                print(f"     {key:6s} {'put ' if kind=='P' else 'call'} n={len(rr):3d} 缓冲中位 {st.median(r['buf'] for r in rr)*100:.2f}%  "
                      f"破墙 {o_all:.0%}（同期随机价位期望 {e_all:.0%}）→ 过滤后 {o_f:.0%}  剔除 {sum(sig)} 天")

    print("\n\nD. SLV 真实回测母体（墙上·DTE2~4·宽2~3·权利金≥$6.40，81 笔）：决策日 ATR 扩张比")
    ser = src.fetch_series(cfg.get("silver")); ratio, _ = atr_ratio(ser); idx = {d.isoformat(): i for i, d in enumerate(ser.dates)}
    grid = [json.loads(l) for l in open(ROOT / "data/backtest/step2_grid.jsonl")]
    sel = [r for r in grid if r["off"] == 0 and 2 <= r["dte"] <= 4 and r["width_n"] in (2, 3) and r["credit"] >= 6.40]
    for r in sel:
        i = idx.get(r["open"]); r["_x"] = ratio[i - 1] if i else None
    los = [r for r in sel if r["pnl"] < 0]
    print(f"   亏损 {len(los)} 笔（8/3、8/4 开仓）决策日 ATR 扩张比：{sorted(set(round(r['_x'],2) for r in los))}  → 过滤 <1.3 {'不会' if all(r['_x'] < 1.3 for r in los) else '会'}剔除它们")
    flagged = [r for r in sel if r["_x"] is not None and r["_x"] >= 1.3]
    print(f"   全部 81 笔里被 ≥1.3 标记的：{len(flagged)} 笔，其 PnL 合计 {sum(r['pnl'] for r in flagged):+.1f}（这些是过滤器会让你错过的）")


if __name__ == "__main__":
    main()
