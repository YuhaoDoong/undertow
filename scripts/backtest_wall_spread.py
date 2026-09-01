"""墙位卖方价差回测 —— 唯一出口（2026-09-01 定版）

替代 backtest_{credit_wall,sell_put_wall,true_wall,wall_lock,wall_v3}.py 等
散落脚本。那些脚本各自定义 spot / 破墙 / 平仓口径，互相矛盾。

═══ 口径（改这里等于推翻结论，改前先读 wall_spread.py 文件头）═══
1. 决策价 = C[D−1]（快照 D 在 D 开盘前可读，D−1 收盘是决策时已知的最后价格）
   ⛔ 禁用 snapshot.spot —— 46 个快照里 34 个的 spot 不是文件名当天的价
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
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                    # noqa: E402
from undertow.cli import snapshot_from_payload                    # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars          # noqa: E402
from undertow.collect.store import SnapshotStore                  # noqa: E402

INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "TQQQ": "tqqq",
        "SPY": "spy", "IWM": "iwm", "TLT": "tlt", "USO": "wti"}


def load(sym):
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=400)}
    st = SnapshotStore()
    snaps = {}
    for f in sorted(os.listdir(f"data/snapshots/options/{sym}")):
        if not f.endswith(".json.gz"):
            continue
        try:
            d = datetime.strptime(f[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        try:
            snaps[d] = snapshot_from_payload(st.load("options", sym, d),
                                             INST[sym], sym)
        except Exception:
            pass
    return snaps, closes, sorted(closes)


def decision_price(d, closes, dates):
    """口径 1：D−1 收盘。找不到就跳过这天，不猜。"""
    k = d.isoformat()
    prior = [x for x in dates if x < k]
    return closes[prior[-1]] if prior else None


def wall_of(snap, spot, kind, obs, cap, band):
    """band=None → 真墙（不限范围）。"""
    agg = defaultdict(int)
    for c in snap.contracts:
        if c.kind != kind:
            continue
        if kind == "P" and c.strike > spot:
            continue
        if kind == "C" and c.strike < spot:
            continue
        if band is not None:
            if kind == "P" and c.strike < spot * (1 - band):
                continue
            if kind == "C" and c.strike > spot * (1 + band):
                continue
        if not (1 <= (c.expiry - obs).days <= cap):
            continue
        agg[c.strike] += c.open_interest
    if not agg:
        return None, 0
    k = max(agg, key=agg.get)
    return k, agg[k]


def run(snaps, closes, dates, kind, *, band, width_pct, dte, cap=9999,
        min_credit_mult=ws.MIN_CREDIT_MULT):
    out, skip = [], defaultdict(int)
    for d in sorted(snaps):
        snap = snaps[d]
        spot = decision_price(d, closes, dates)
        if spot is None:
            skip["无D-1收盘"] += 1
            continue
        prior = [x for x in dates if x < d.isoformat()]
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        wk, woi = wall_of(snap, spot, kind, obs, cap, band)
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
    print(f"  {label:<12}{len(rows):>4}笔 未破墙{nb/len(rows):>5.0%} "
          f"均缓冲{statistics.mean(r['buffer'] for r in rows):>5.1f}% "
          f"均权利金${statistics.mean(r['credit'] for r in rows):>6.1f} "
          f"总损益{tot:>+7.0f} 峰值占用${peak:>6.0f} "
          f"年化{tot/peak*365/span*100:>+6.0f}% "
          f"赔率{(f'{b:.2f}' if b != float('inf') else '∞'):>5}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    for sym in a.symbol.split(","):
        snaps, closes, dates = load(sym)
        ds = sorted(snaps)
        print(f"\n{'='*118}\n{sym}　{ds[0]}~{ds[-1]}　{len(ds)} 个快照"
              f"　决策价=C[D−1]（禁用 snapshot.spot）\n{'='*118}")
        for kind, kl in (("P", "卖put"), ("C", "卖call")):
            for bl, band in (("band5%", 0.05), ("真墙", None)):
                for wp in (0.02, 0.03, 0.05):
                    for dte, dl in (((4, 11), "4~11d"), ((12, 25), "12~25d")):
                        rows, skip = run(snaps, closes, dates, kind,
                                         band=band, width_pct=wp, dte=dte)
                        report(sym, rows, skip, f"{kl} {bl} {wp*100:.0f}% {dl}")
        if a.detail:
            rows, _ = run(snaps, closes, dates, "P", band=None,
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
