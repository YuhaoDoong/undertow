"""墙位卖方价差回测 —— 唯一出口（2026-09-01 定版）

替代 backtest_{credit_wall,sell_put_wall,true_wall,wall_lock,wall_v3}.py 等
散落脚本。那些脚本各自定义 spot / 破墙 / 平仓口径，互相矛盾。

═══ 口径（改这里等于推翻结论，改前先读 wall_spread.py 文件头）═══
1. 可交易日由 captured_at 决定，【不是文件名日期】（codex 2026-09-01 P0-1）：
     盘前抓 → 当天；盘后/周末抓 → 下一交易日；盘中抓 → 剔除（无法模拟成交）
   决策价 = C[可交易日的前一交易日] 收盘
   ⛔ 禁用 snapshot.spot —— 46 个快照里 34 个的 spot 不是文件名当天的价
   ⛔ 禁用文件名日期当可交易日 —— 193 份里 21 份不是盘前抓的
2. 权利金 = 快照 D 里的 bid/ask，按组合单中价让 25% 点差
3. 破墙 = 到期日收盘越过卖腿（不是盘中触及）
4. 换墙【不】平仓，只影响新开仓选哪个行权价
5. 平仓只有一个触发：反向极强信号 且 价格已越过卖腿（wall_spread.should_exit）
6. 墙的两种口径并列输出，供对比：
     band 墙 = 现价 ±band 内累计 OI 最大（现行实现，已知缺陷）
     真墙   = 全行权价范围内累计 OI 最大（用户口径）
"""
import argparse
import os
import pathlib
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                    # noqa: E402
from undertow.analyze.gamma import local_pin, structural_walls    # noqa: E402
from undertow.cli import snapshot_from_payload                    # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars          # noqa: E402
from undertow.collect.store import SnapshotStore                  # noqa: E402

INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "TQQQ": "tqqq",
        "SPY": "spy", "IWM": "iwm", "TLT": "tlt", "USO": "wti"}


def load(sym):
    """返回 {可交易日: (快照, 文件名日)}、收盘价表、交易日列表、剔除计数。

    ⚠️ 不再裸吞异常（codex P1）：每一个被丢弃的快照都要计数并可打印，
    否则「选择性丢样本」会伪装成「样本本来就这么多」。
    """
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=400)}
    dates = sorted(closes)
    tdays = [datetime.strptime(x, "%Y-%m-%d").date() for x in dates]
    st = SnapshotStore()
    snaps, drop = {}, defaultdict(int)
    for f in sorted(os.listdir(f"data/snapshots/options/{sym}")):
        if not f.endswith(".json.gz"):
            continue
        try:
            fd = datetime.strptime(f[:10], "%Y-%m-%d").date()
        except ValueError:
            drop["文件名非日期"] += 1
            continue
        sess = st.decision_session("options", sym, fd, tdays)
        if sess is None:
            drop["盘中抓取或无captured_at"] += 1
            continue
        if sess in snaps:
            # 两份快照映射到同一可交易日（如周末抓 + 周一盘前抓）：留盘前那份
            drop["同一可交易日重复"] += 1
            continue
        payload = st.load("options", sym, fd)
        if payload is None:
            drop["快照损坏或缺失"] += 1
            continue
        try:
            snaps[sess] = (snapshot_from_payload(payload, INST[sym], sym), fd)
        except Exception as e:
            drop[f"解析失败({type(e).__name__})"] += 1
    return snaps, closes, dates, drop


def decision_price(d, closes, dates):
    """口径 1：D−1 收盘。找不到就跳过这天，不猜。"""
    k = d.isoformat()
    prior = [x for x in dates if x < k]
    return closes[prior[-1]] if prior else None


def wall_of(snap, spot, kind, obs, mode):
    """三种找墙口径并列，供对比。返回 (行权价, OI) 或 (None, 0)。

    · "structural" —— gamma.structural_walls()：全范围 + 近端到期占比 ≥15%。
      2026-09-01 定的正确口径：长期对冲堆积（GLD 330 的 ≤30 天占比仅 2%）
      被滤掉，真承接区（GLD 400 占 28%）保留。
    · "pin" —— gamma.local_pin()：近价带内最大。**它不是墙**，带内总有最大值；
      放在这里只为量化"用错口径会差多少"。
    · "band5" —— 旧实现，等价于 pin(band=0.05)，保留以复现历史结论。
    """
    if mode == "structural":
        w = structural_walls(snap, obs, spot, kind, top_n=1)
        return (w[0]["strike"], w[0]["oi"]) if w else (None, 0)
    band = 0.05 if mode == "band5" else 0.05
    p = local_pin(snap, obs, spot, kind, band=band)
    return (p["strike"], p["oi"]) if p else (None, 0)


def run(snaps, closes, dates, kind, *, mode, width_pct, dte,
        min_credit_mult=ws.MIN_CREDIT_MULT):
    out, skip = [], defaultdict(int)
    for d in sorted(snaps):
        snap, file_day = snaps[d]
        spot = decision_price(d, closes, dates)
        if spot is None:
            skip["无D-1收盘"] += 1
            continue
        prior = [x for x in dates if x < d.isoformat()]
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        wk, woi = wall_of(snap, spot, kind, obs, mode)
        if wk is None:
            skip["无墙"] += 1
            continue
        legs = defaultdict(dict)
        for c in snap.contracts:
            if c.kind != kind or c.bid is None or not c.ask:
                continue
            if dte[0] <= (c.expiry - d).days <= dte[1]:
                legs[c.expiry][c.strike] = c
        pick = None
        for exp in sorted(legs):
            ks = sorted(legs[exp])
            if wk not in ks:
                continue
            wa = spot * width_pct
            far = [x for x in ks if (x > wk if kind == "C" else x < wk)]
            if not far:
                continue
            bk = min(far, key=lambda x: abs(x - (wk + wa if kind == "C" else wk - wa)))
            pick = (exp, legs[exp][wk], legs[exp][bk], bk)
            break
        if not pick:
            skip["墙位无可用合约"] += 1
            continue
        exp, sc, bc, bk = pick
        credit = ws._fill(sc, bc)
        if credit <= ws.FEE_PER_LEG * 4 * min_credit_mult:
            skip["权利金不足"] += 1
            continue
        width = abs(bk - wk) * 100
        if width <= credit:
            skip["宽度异常"] += 1
            continue
        se = closes.get(exp.isoformat())
        if se is None:
            skip["无结算价"] += 1
            continue
        itr = (max(0.0, min(se - wk, width / 100)) if kind == "C"
               else max(0.0, min(wk - se, width / 100))) * 100
        pnl = credit - itr - ws.FEE_PER_LEG * 4
        out.append(dict(d=d, spot=spot, wall=wk, woi=woi, buy=bk, exp=exp,
                        dte=(exp - d).days, credit=credit, occ=width - credit,
                        settle=se, pnl=pnl, broke=(se > wk) if kind == "C" else (se < wk),
                        buffer=abs(wk / spot - 1) * 100))
    return out, skip


def cluster_bootstrap(rows, *, trials=5000, seed=7):
    """按【进场日期】整簇重采样，给净收益率的 95% 置信区间。

    为什么必须按簇（项目回测硬要求之一）：同一天开的多笔、以及跨品种同日的仓位，
    收益高度相关。把它们当独立样本会把置信区间算得过窄，
    再小的样本都能"显著"。
    统计量 = 总净损益 / 峰值资本占用（这才是这套策略的真实收益率，
    不是单笔 ROI 的平均 —— 后者忽略了同时持有多笔时的资金占用）。
    """
    if len(rows) < 3:
        return None
    byday = defaultdict(list)
    for r in rows:
        byday[r["d"]].append(r)
    days = list(byday)
    if len(days) < 3:
        return None

    def ratio(sample):
        if not sample:
            return 0.0
        live = defaultdict(float)
        for r in sample:
            for k in range(r["dte"] + 1):
                live[r["d"].toordinal() + k] += r["occ"]
        peak = max(live.values()) if live else 1.0
        return sum(r["pnl"] for r in sample) / peak if peak else 0.0

    rng = random.Random(seed)
    out = []
    for _ in range(trials):
        samp = [x for dd in rng.choices(days, k=len(days)) for x in byday[dd]]
        out.append(ratio(samp))
    out.sort()
    return {"obs": ratio(rows), "lo": out[int(trials * 0.025)],
            "hi": out[int(trials * 0.975)], "clusters": len(days)}


def report(sym, rows, skip, label):
    if len(rows) < 3:
        print(f"  {label:<12} 笔数 {len(rows)} 不足，跳过（{dict(skip)}）")
        return
    nb = sum(1 for r in rows if not r["broke"])
    tot = sum(r["pnl"] for r in rows)
    live = defaultdict(float)
    for r in rows:
        for k in range(r["dte"] + 1):
            live[r["d"].toordinal() + k] += r["occ"]
    peak = max(live.values()) if live else 1
    ds = sorted(r["d"] for r in rows)
    span = (ds[-1] - ds[0]).days or 1
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    loss = [r["pnl"] for r in rows if r["pnl"] <= 0]
    b = (statistics.mean(wins) / abs(statistics.mean(loss))) if loss and wins else float("inf")
    cb = cluster_bootstrap(rows)
    ci = (f" 簇{cb['clusters']:>2} CI[{cb['lo']:+.0%},{cb['hi']:+.0%}]"
          f"{'✓' if cb['lo'] > 0 else '✗'}" if cb else " 簇<3")
    print(f"  {label:<12}{len(rows):>4}笔 未破墙{nb/len(rows):>5.0%} "
          f"均缓冲{statistics.mean(r['buffer'] for r in rows):>5.1f}% "
          f"均权利金${statistics.mean(r['credit'] for r in rows):>6.1f} "
          f"总损益{tot:>+7.0f} 峰值占用${peak:>6.0f} "
          f"年化{tot/peak*365/span*100:>+6.0f}% "
          f"赔率{(f'{b:.2f}' if b != float('inf') else '∞'):>5}" + ci)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    for sym in a.symbol.split(","):
        snaps, closes, dates, drop = load(sym)
        ds = sorted(snaps)
        print(f"\n{'='*118}\n{sym}　{ds[0]}~{ds[-1]}　{len(ds)} 个快照"
              f"　可交易日按 captured_at 推导，决策价=C[前一交易日]\n{'='*118}")
        if drop:
            print(f"  剔除：{dict(drop)}")
        for kind, kl in (("P", "卖put"), ("C", "卖call")):
            for mode, ml in (("structural", "结构墙"), ("pin", "局部pin")):
                for wp in (0.02, 0.03, 0.05):
                    for dte, dl in (((4, 11), "4~11d"), ((12, 25), "12~25d")):
                        rows, skip = run(snaps, closes, dates, kind,
                                         mode=mode, width_pct=wp, dte=dte)
                        report(sym, rows, skip, f"{kl} {ml} {wp*100:.0f}% {dl}")
        if a.detail:
            rows, _ = run(snaps, closes, dates, "P", mode="structural",
                          width_pct=0.03, dte=(4, 11))
            print(f"\n  真墙 put 3% 4~11d 明细")
            for r in rows:
                print(f"    {r['d']} 决策价{r['spot']:>7.2f} 墙{r['wall']:>6g}"
                      f"({r['woi']:>7,}张) 缓冲{r['buffer']:>5.1f}% "
                      f"到期{r['exp']} 收{r['settle']:>7.2f} "
                      f"{'✗破墙' if r['broke'] else '  守住'} "
                      f"权利金{r['credit']:>6.0f} 损益{r['pnl']:>+6.0f}")


if __name__ == "__main__":
    main()
