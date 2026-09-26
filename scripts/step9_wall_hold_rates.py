"""W04：重审「价格经常收在墙内」—— 同一批事前固定的墙，三种结果、三种墙定义分开统计。

用户观察：「多次收盘停留在墙内、短期不大幅反向就获利」。Codex 004 蓝图 H1：观察可能是真的，
但墙通常在虚值一侧，「离得远本来就不容易破」与「墙挡住了」要分开。

═══ 预登记（写于看结果之前，2026-09-26）═══
- 墙定义：local_max（≤14 天、±5% 内该侧累计 OI 最大）/ nearest（带内最近且 OI≥3000）/
  structural（pick_sell_wall，缓冲≥3%，即 v3 模块实际选墙）。三者分表，不混算一个胜率。
- 事前固定：T 日盘前快照（captured_at → decision_session，盘中剔除）+ 决策价 C[T−1] 定墙，此后不重画。
- 结果窗：k=1/2/3 个交易日（T..T+k−1 收盘），以及「实际到期窗」= 该快照里 T 起 2~4 DTE 的最近到期。
- 三种结果：endpoint_hold（窗末收盘在墙内）/ close_hold（窗内所有收盘都在墙内）/
  intraday_hold（窗内所有 high/low 都没碰到墙；OHLC 齐全才算）。
- 基准：同距离任意价位（同一缓冲 b、同一 k）在快照同期（Ein）与 20 年（E20）的守住率。
- 最小有用效果：**墙的破墙率 ≤ 同距离随机价位的 70%（O/E ≤ 0.70）**。
  判定：O/E 的 95% 区间上界 < 1 → 支持「墙有增量」；下界 > 0.70 → 排除实用增量；否则未决。
- 期望破墙数（不重叠子样本上 ΣF）< 3 时不判定：观测 0 次也无法与随机区分（bootstrap 会退化成 [0,0]）。
- 区间：不重叠子样本（每 k 天取 1；到期窗按不重叠区间取），对 (破墙, 期望) 行做 bootstrap（固定种子 20000 次）。
  局限：期望 F 当常数，未传播基准自身的估计误差；单一样本期；金银同日相关不合并。
用法：python3 scripts/step9_wall_hold_rates.py [--emit --output PATH]
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step5_wall_hold as s5                                          # noqa: E402
from undertow.analyze.gamma import local_wall, pick_sell_wall          # noqa: E402
from undertow.analyze.stretch import _atr_series                       # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource            # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload        # noqa: E402
from undertow.collect.store import SnapshotStore                       # noqa: E402
from undertow.core.config import load_config                           # noqa: E402

KEYS = ("gold", "silver", "wti", "qqq")
DEFS = ("local_max", "nearest", "structural")
KS = (1, 2, 3)
MIN_USEFUL = 0.70
B_ITERS, SEED = 20000, 20260926


def pick(snap, T, spot, kind, how):
    if how == "structural":
        w = pick_sell_wall(snap, T, spot, kind, min_buf=0.03)
        return None if w is None else {"strike": w["strike"], "oi": w.get("oi"), "oi_by_expiry": None}
    return local_wall(snap, T, spot, kind, mode="max" if how == "local_max" else "nearest")


def crosses(kind, strike, px):
    return px < strike if kind == "P" else px > strike


def base_hold(c, lo_idx, hi_idx, b, k, kind):
    """同距离任意价位在 [lo_idx, hi_idx) 期间的 close_hold 与 endpoint_hold 比例。"""
    ch = eh = n = 0
    for i in range(max(lo_idx, 1), min(hi_idx, len(c) - k)):
        win = c[i + 1:i + k + 1]
        lvl = c[i] * (1 - b) if kind == "P" else c[i] * (1 + b)
        n += 1
        ch += not any(crosses(kind, lvl, x) for x in win)
        eh += not crosses(kind, lvl, win[-1])
    return (ch / n, eh / n) if n else (float("nan"), float("nan"))


def boot_ratio(rows, key_obs, key_exp):
    """O/E 的 bootstrap 95% 区间（行 = 不重叠观测）。"""
    if len(rows) < 5:
        return None, None
    rnd = random.Random(SEED); vals = []
    for _ in range(B_ITERS):
        s = [rows[rnd.randrange(len(rows))] for _ in rows]
        e = sum(r[key_exp] for r in s)
        if e > 0:
            vals.append(sum(r[key_obs] for r in s) / e)
    if not vals:                       # 期望恒为 0：没有可比的随机破墙
        return None, None
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


MIN_EXPECTED = 3.0


def verdict(lo, hi, ratio, expected):
    if expected < MIN_EXPECTED:
        return f"期望破墙仅{expected:.1f}次·无从区分"
    if lo is None:
        return "样本不足"
    if hi < 1:
        return "支持墙有增量"
    if lo > MIN_USEFUL:
        return "排除实用增量"
    return "未决"


def run(key, cfg, store, src):
    inst = cfg.get(key); ser = src.fetch_series(inst)
    tdays, c, h, l = list(ser.dates), list(ser.closes), list(ser.highs), list(ser.lows)
    idx = {d: i for i, d in enumerate(tdays)}
    atr = _atr_series(h, l, c, 14)
    by_T, _ = s5.load_days(store, key, inst.options.symbol, tdays)
    Ts = sorted(t for t in by_T if t in idx and idx[t] > 0)
    lo_idx, hi_idx = idx[Ts[0]] - 1, idx[Ts[-1]]
    out = []
    for T in Ts:
        snap = snapshot_from_payload(by_T[T][1], key, inst.options.symbol)
        iT = idx[T]; spot = c[iT - 1]
        exps = sorted({x.expiry for x in snap.contracts if 2 <= (x.expiry - T).days <= 4})
        for kind in ("P", "C"):
            for how in DEFS:
                w = pick(snap, T, spot, kind, how)
                if not w:
                    continue
                K = w["strike"]; b = abs(K / spot - 1)
                oibe = w.get("oi_by_expiry") or {}
                windows = [(f"k{k}", k) for k in KS]
                if exps and exps[0] in idx:
                    windows.append(("expiry", idx[exps[0]] - iT + 1))
                for wname, k in windows:
                    if k < 1 or iT + k - 1 >= len(c):
                        continue
                    cw = c[iT:iT + k]; hw = h[iT:iT + k]; lw = l[iT:iT + k]
                    ohlc = all(x is not None for x in hw + lw)
                    ch_in, eh_in = base_hold(c, lo_idx, hi_idx, b, k, kind)
                    ch_20, eh_20 = base_hold(c, 250, len(c), b, k, kind)
                    pre = sum(v for d_, v in oibe.items() if date.fromisoformat(d_) < (exps[0] if exps else T)) if oibe else None
                    out.append({
                        "T": T.isoformat(), "iT": iT, "kind": kind, "def": how, "window": wname, "k": k,
                        "strike": K, "buf_pct": b * 100, "buf_atr": (abs(K - spot) / atr[iT - 1]) if atr[iT - 1] else None,
                        "oi": w.get("oi"), "oi_share_expiring_before_target": (pre / w["oi"]) if (pre is not None and w.get("oi")) else None,
                        "endpoint_breach": crosses(kind, K, cw[-1]),
                        "close_breach": any(crosses(kind, K, x) for x in cw),
                        "intraday_breach": (any(x < K for x in lw) if kind == "P" else any(x > K for x in hw)) if ohlc else None,
                        "F_close_in": 1 - ch_in, "F_end_in": 1 - eh_in, "F_close_20": 1 - ch_20, "F_end_20": 1 - eh_20})
    return {"symbol": inst.options.symbol, "span": f"{Ts[0]}→{Ts[-1]}", "n_days": len(Ts), "rows": out}


def summarize(rows):
    res = {}
    for how in DEFS:
        for kind in ("P", "C"):
            for wname in [f"k{k}" for k in KS] + ["expiry"]:
                rr = sorted([r for r in rows if r["def"] == how and r["kind"] == kind and r["window"] == wname], key=lambda r: r["iT"])
                if not rr:
                    continue
                # 不重叠：顺序取，下一行起点须晚于上一行窗末
                nov, last_end = [], -1
                for r in rr:
                    if r["iT"] > last_end:
                        nov.append(r); last_end = r["iT"] + r["k"] - 1
                n = len(rr)
                item = {"n": n, "n_nov": len(nov), "buf_pct_med": st.median(r["buf_pct"] for r in rr),
                        "buf_atr_med": st.median(r["buf_atr"] for r in rr if r["buf_atr"] is not None),
                        "endpoint_hold": 1 - sum(r["endpoint_breach"] for r in rr) / n,
                        "close_hold": 1 - sum(r["close_breach"] for r in rr) / n,
                        "intraday_hold": (1 - sum(r["intraday_breach"] for r in rr if r["intraday_breach"] is not None)
                                          / max(1, sum(r["intraday_breach"] is not None for r in rr))),
                        "random_close_hold_in": 1 - st.mean(r["F_close_in"] for r in rr),
                        "random_close_hold_20": 1 - st.mean(r["F_close_20"] for r in rr),
                        "random_end_hold_in": 1 - st.mean(r["F_end_in"] for r in rr)}
                o = sum(r["close_breach"] for r in nov); e = sum(r["F_close_in"] for r in nov)
                item["OE_close_in"] = o / e if e else None      # e=0 → 不可估计
                lo, hi = boot_ratio([{"o": r["close_breach"], "e": r["F_close_in"]} for r in nov], "o", "e")
                item["OE_ci"] = (lo, hi); item["expected_breaches_nov"] = e; item["observed_breaches_nov"] = o
                item["verdict"] = verdict(lo, hi, item["OE_close_in"], e)
                sh = [r["oi_share_expiring_before_target"] for r in rr if r["oi_share_expiring_before_target"] is not None]
                item["oi_share_before_target_med"] = st.median(sh) if sh else None
                res[f"{how}|{kind}|{wname}"] = item
    return res


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", action="store_true")
    ap.add_argument("--output", type=Path, default=ROOT / "data/history/wall_spread/wall_hold_rates.json")
    a = ap.parse_args()
    cfg = load_config(); store = SnapshotStore(); src = CboeHistorySource()
    emit = {"schema": 1, "asof": date.today().isoformat(), "min_useful_OE": MIN_USEFUL,
            "bootstrap": {"iters": B_ITERS, "seed": SEED}, "instruments": {}}
    print(f"墙守住率（事前固定墙，不重画）。O/E = 期间收盘破墙 ÷ 同期同距离随机价位期望；最小有用效果 O/E≤{MIN_USEFUL}")
    for key in KEYS:
        r = run(key, cfg, store, src); s = summarize(r["rows"])
        emit["instruments"][key] = {"symbol": r["symbol"], "span": r["span"], "n_days": r["n_days"], "summary": s}
        print(f"\n{'═'*118}\n{key} ({r['symbol']}) {r['span']} 可交易日 {r['n_days']}\n{'═'*118}")
        print(f"  {'墙·侧·窗':22s}{'n':>4s}{'缓冲%':>6s}{'ATR':>5s} │{'窗末收在墙内':>7s}{'随机':>5s} │{'期间收盘未破':>7s}{'随机同期':>6s}{'20年':>5s} │{'盘中未碰':>5s} │"
              f"{'O/E':>5s}{'95%区间':>13s}{'期望/观测':>9s}  判定")
        for kk, v in s.items():
            ci = v["OE_ci"]; cis = f"[{ci[0]:.2f},{ci[1]:.2f}]" if ci[0] is not None else "—"
            oe = f"{v['OE_close_in']:.2f}" if v["OE_close_in"] is not None else "—"
            print(f"  {kk:22s}{v['n']:4d}{v['buf_pct_med']:6.2f}{v['buf_atr_med']:5.2f} │{v['endpoint_hold']:7.0%}{v['random_end_hold_in']:6.0%} │"
                  f"{v['close_hold']:7.0%}{v['random_close_hold_in']:7.0%}{v['random_close_hold_20']:6.0%} │{v['intraday_hold']:6.0%} │"
                  f"{oe:>5s}{cis:>13s}{v['expected_breaches_nov']:5.1f}/{v['observed_breaches_nov']:<3d}  {v['verdict']}")
    if a.emit:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(emit, ensure_ascii=False, indent=1, default=str), "utf-8")
        print(f"\n已落盘 {a.output}")


if __name__ == "__main__":
    main()
