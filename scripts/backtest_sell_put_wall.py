"""只卖【下方 put 墙】的价差 —— 用户 2026-08-31 说的那个策略。

之前的 backtest_credit_wall 是「顺信号卖逆向侧」：看涨→卖put、看跌→卖call。
逐笔账本一看就露馅：白银 7~8 月从 52 涨到 62.72，四笔「看跌→卖call」全被打穿
（-91~-99），而五笔「看涨→卖put」全赢（+3~+24）。亏损全来自方向信号，
不是价差结构。用户说的从来是「卖墙的 put」——只做下方一侧。

本脚本只测这个：不管方向信号，只在 put 墙下方卖看跌价差。
可选按方向信号过滤（--require-bull 只在看涨信号时做）。
限定 GLD/SLV（用户：其他先别管）。

口径与 backtest_credit_wall 一致：
  · 每信号只选一笔（规则可配）
  · DTE 按执行日算
  · 只用到期日当天收盘结算，缺失即丢弃
  · 按日期簇聚合，置换检验净收益 > 0
"""
# ⚠️ 2026-09-01：本脚本用 snapshot.spot 当开仓现价，而 46 个快照里 34 个的
#    spot 不是文件名当天的价（见 memory/snapshot-date-alignment-p0）。
#    结论仍保留（它记录的是"策略不通过验证"这个负面结果，方向不受影响），
#    但数值不可引用。新回测走 scripts/backtest_wall_spread.py（决策价=C[D−1]）。

import argparse, json, os, pathlib, random, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze.flow import analyze_flow, tradeable_info      # noqa: E402
from undertow.analyze.gamma import layered_walls                    # noqa: E402
from undertow.cli import snapshot_from_payload, _prev_weekday       # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars            # noqa: E402
from undertow.collect.store import SnapshotStore                    # noqa: E402

INST = {"GLD": "gold", "SLV": "silver"}
FEE = 0.80
OUT = pathlib.Path("data/backtest")


def closes(sym):
    try:
        return {str(b["ts"])[:10]: b["close"]
                for b in fetch_bars(f"{sym}.US", period="day", count=300)}
    except Exception:
        return {}


def legs(snap, exp):
    return sorted([c for c in snap.contracts if c.expiry == exp and c.kind == "P"
                   and c.bid and c.ask and c.bid > 0], key=lambda x: x.strike)


def run(offset, width, dte_lo, dte_hi, rule, min_share, require_bull, min_ratio):
    st = SnapshotStore()
    px = {s: closes(s) for s in INST}
    trades, skip = [], defaultdict(int)
    for sym, inst in INST.items():
        dirp = f"data/snapshots/options/{sym}"
        if not os.path.isdir(dirp):
            continue
        ds = []
        for f in sorted(os.listdir(dirp)):
            if f.endswith(".json.gz"):
                try:
                    ds.append(datetime.strptime(f[:10], "%Y-%m-%d").date())
                except ValueError:
                    pass
        for i in range(1, len(ds)):
            d, p = ds[i], ds[i - 1]
            obs = _prev_weekday(d)
            try:
                cur = snapshot_from_payload(st.load("options", sym, d), inst, sym)
                prv = snapshot_from_payload(st.load("options", sym, p), inst, sym)
                fa = analyze_flow(prv, cur, today=obs, prev_date=p.isoformat(),
                                  curr_date=d.isoformat(), horizon_days=45)
            except Exception:
                skip["快照/flow失败"] += 1
                continue
            ti = tradeable_info(fa)
            if min_ratio is not None and ti.get("ratio", 0) < min_ratio:
                skip["未过压力比"] += 1
                continue
            if require_bull and ti.get("side") != "看涨":
                skip["非看涨信号"] += 1
                continue
            spot = cur.spot
            L = layered_walls(cur, obs, spot)["near"]
            wk, woi, tot = L.put_wall, L.put_wall_oi, L.total_put_oi
            if woi <= 0 or tot <= 0 or woi / tot < min_share:
                skip["墙不够厚"] += 1
                continue
            cands = []
            for exp in sorted({c.expiry for c in cur.contracts
                               if dte_lo <= (c.expiry - d).days <= dte_hi}):
                se = px.get(sym, {}).get(exp.isoformat())
                if se is None:
                    skip["到期日无收盘价"] += 1
                    continue
                ls = legs(cur, exp)
                pool = [c for c in ls if c.strike < spot * 0.995]
                if not pool:
                    continue
                tgt = min(wk * (1 - offset), spot * 0.995)
                sell = min(pool, key=lambda c: abs(c.strike - tgt))
                lower = [c for c in ls if c.strike < sell.strike]
                if not lower:
                    continue
                buy = min(lower, key=lambda c: abs(c.strike - sell.strike * (1 - width)))
                cr = (sell.bid - buy.ask) * 100
                w = (sell.strike - buy.strike) * 100
                if cr <= 0 or w <= cr:
                    continue
                occ = w - cr
                intr = max(0.0, min(sell.strike - se, w / 100)) * 100
                cands.append(dict(
                    sym=sym, signal_date=d.isoformat(), side=ti.get("side"),
                    ratio=round(ti.get("ratio", 0), 2), wall=wk,
                    wall_share=round(woi / tot, 3),
                    sell=sell.strike, buy=buy.strike, expiry=exp.isoformat(),
                    dte=(exp - d).days, credit=round(cr, 2), occupancy=round(occ, 2),
                    spot_entry=round(spot, 4), spot_settle=round(se, 4),
                    pnl=round(cr - intr - FEE * 4, 2),
                    roi=round((cr - intr - FEE * 4) / occ, 4),
                    broke=bool(se < sell.strike)))
            if not cands:
                skip["无候选"] += 1
                continue
            if rule == "first":
                pick = min(cands, key=lambda c: c["dte"])
            elif rule == "max_credit":
                pick = max(cands, key=lambda c: c["credit"])
            else:
                pick = max(cands, key=lambda c: c["roi"] * 365 / max(c["dte"], 1)
                           if False else c["credit"] / c["occupancy"])
            trades.append(pick)
    return trades, skip


def clusters(tr):
    by = defaultdict(list)
    for t in tr:
        by[t["signal_date"]].append(t["roi"])
    return {k: sum(v) / len(v) for k, v in sorted(by.items())}


def perm_p(vals, n=20000, seed=42):
    if len(vals) < 3:
        return 1.0, (0.0, 0.0)
    rng = random.Random(seed)
    obs = statistics.mean(vals)
    c = sum(1 for _ in range(n)
            if statistics.mean(v * rng.choice((1, -1)) for v in vals) >= obs)
    ms = sorted(statistics.mean(rng.choices(vals, k=len(vals))) for _ in range(n // 4))
    return (c + 1) / (n + 1), (ms[int(len(ms) * .025)], ms[int(len(ms) * .975)])


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--rule", default="max_credit")
    a.add_argument("--min-share", type=float, default=0.10)
    a.add_argument("--min-ratio", type=float, default=None)
    a.add_argument("--require-bull", action="store_true")
    ns = a.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"只卖下方 put 墙 · GLD+SLV · 规则={ns.rule} · 墙厚≥{ns.min_share:.0%}"
          + (" · 仅看涨信号" if ns.require_bull else " · 不看方向信号")
          + (f" · 压力比≥{ns.min_ratio}" if ns.min_ratio else "") + "\n")
    hdr = f"{'配置':<22}{'笔数':>4}{'簇':>4}{'胜率':>6}{'破墙':>6}{'簇均ROI':>9}{'置换p':>7}{'95%CI':>18}{'总PnL':>8}"
    print(hdr); print("-" * 96)
    best = []
    for off, ol in ((0.0, "卖墙上"), (0.02, "墙外2%"), (0.04, "墙外4%")):
        for lo, hi, dl in ((4, 14, "4~14天"), (15, 45, "15~45天")):
            tr, sk = run(off, 0.025, lo, hi, ns.rule, ns.min_share,
                         ns.require_bull, ns.min_ratio)
            if len(tr) < 6:
                continue
            cl = clusters(tr); vals = list(cl.values())
            p, (clo, chi) = perm_p(vals)
            wr = sum(1 for t in tr if t["pnl"] > 0) / len(tr)
            br = sum(1 for t in tr if t["broke"]) / len(tr)
            m = statistics.mean(vals)
            print(f"{ol + ' · ' + dl:<22}{len(tr):>4}{len(cl):>4}{wr:>5.0%}{br:>6.0%}"
                  f"{m * 100:>+8.2f}%{p:>7.3f}  [{clo * 100:+.1f}%,{chi * 100:+.1f}%]"
                  f"{sum(t['pnl'] for t in tr):>+8.0f}")
            best.append((m, ol, dl, tr, cl, p, (clo, chi), wr, br))
    if best:
        best.sort(key=lambda x: -x[0])
        m, ol, dl, tr, cl, p, ci, wr, br = best[0]
        out = OUT / "sell_put_wall_best.jsonl"
        with open(out, "w") as f:
            for t in tr:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"\n最优：{ol} · {dl}　簇均 {m*100:+.2f}%　p={p:.3f}　逐笔 → {out}")


if __name__ == "__main__":
    main()
