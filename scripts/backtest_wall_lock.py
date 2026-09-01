"""锁定墙位卖方价差回测 —— 用户 2026-08-31 描述的策略，可复现

规则（用户原话整理）：
  · 卖腿锁定在【跨到期累计 OI 最大】的墙上，不每天重算
  · 每天开一张，选一周到期（4~11 天）
  · 墙被突破/出现极强反向信号才换墙，换墙当天平掉旧墙位的全部未到期仓位
  · 持有到期按【到期日收盘价】结算（盘中触及不算破墙）
  · 价差组合单按【中价往保守方向让 25%】成交（不是两边吃满点差）

换墙时点（SLV 样本期，由墙位实际迁移确定）：
  call: 55 →(8/4 极强看涨)→ 60 →(8/20 突破)→ 63
  put : 50 →(8/5)→ 55 →(8/20)→ 60

⚠️ 样本期 SLV +14.8%、GLD +10.5%，是单边上涨行情。
   put 侧零破墙有相当部分来自方向，不能外推到跌市。
"""
# ⚠️ 2026-09-01：本脚本用 snapshot.spot 当开仓现价，而 46 个快照里 34 个的
#    spot 不是文件名当天的价（见 memory/snapshot-date-alignment-p0）。
#    结论仍保留（它记录的是"策略不通过验证"这个负面结果，方向不受影响），
#    但数值不可引用。新回测走 scripts/backtest_wall_spread.py（决策价=C[D−1]）。

import argparse, json, math, os, pathlib, statistics, sys
from collections import defaultdict
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.cli import snapshot_from_payload                    # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars          # noqa: E402
from undertow.collect.store import SnapshotStore                  # noqa: E402

FEE_PER_LEG = 0.80
WALLS = {
    "SLV": {"C": [(None, 55.0), (date(2026, 8, 4), 60.0), (date(2026, 8, 20), 63.0)],
            "P": [(None, 50.0), (date(2026, 8, 5), 55.0), (date(2026, 8, 20), 60.0)]},
}


def wall_on(sym, d, kind):
    k = None
    for sd, v in WALLS[sym][kind]:
        if sd is None or d >= sd:
            k = v
    return k


def switch_days(sym, kind):
    return {sd for sd, _ in WALLS[sym][kind] if sd}


def fill(sc, bc, mode, closing=False):
    """成交价。closing=True 时方向相反（买回卖腿、卖出买腿）。"""
    sb, sa = sc.bid or 0, sc.ask or 0
    bb, ba = bc.bid or 0, bc.ask or 0
    mid = ((sb + sa) / 2 - (bb + ba) / 2) * 100
    worst = ((sa - bb) if closing else (sb - ba)) * 100
    if mode == "mid":
        return mid
    if mode == "worst":
        return worst
    return mid + (worst - mid) * 0.25          # mid25：中价往不利方向让 25%


def run(sym, kind, width, mode, dte_lo, dte_hi, snaps, closes):
    ds = sorted(snaps)
    sw = switch_days(sym, kind)
    out = []
    for d in ds:
        k = wall_on(sym, d, kind)
        if k is None:
            continue
        bk = k + width if kind == "C" else k - width
        cand = {}
        for c in snaps[d].contracts:
            if c.kind != kind or c.bid is None or not c.ask:
                continue
            dte = (c.expiry - d).days
            if not (dte_lo <= dte <= dte_hi):
                continue
            if abs(c.strike - k) < 1e-6:
                cand.setdefault(c.expiry, {})["s"] = c
            elif abs(c.strike - bk) < 1e-6:
                cand.setdefault(c.expiry, {})["b"] = c
        pair = None
        for exp in sorted(cand):
            if "s" in cand[exp] and "b" in cand[exp]:
                pair = (exp, cand[exp]["s"], cand[exp]["b"])
                break
        if not pair:
            continue
        exp, sc, bc = pair
        credit = fill(sc, bc, mode)
        if credit <= 0:
            continue
        occ = width * 100 - credit
        ex = next((s for s in sorted(sw) if d < s < exp), None)
        pnl = None
        broke = False
        how = "到期"
        if ex and ex in snaps:
            s2 = b2 = None
            for c in snaps[ex].contracts:
                if c.expiry != exp or c.kind != kind or c.bid is None or not c.ask:
                    continue
                if abs(c.strike - k) < 1e-6:
                    s2 = c
                elif abs(c.strike - bk) < 1e-6:
                    b2 = c
            if s2 and b2:
                pnl = credit - fill(s2, b2, mode, closing=True) - FEE_PER_LEG * 4
                how = "换墙平仓"
        if pnl is None:
            se = closes.get(exp.isoformat())
            if se is None:
                continue
            itr = (max(0.0, min(se - k, width)) if kind == "C"
                   else max(0.0, min(k - se, width))) * 100
            pnl = credit - itr - FEE_PER_LEG * 4
            broke = (se > k) if kind == "C" else (se < k)
        out.append(dict(open=d, exp=exp, kind=kind, sell=k, buy=bk,
                        credit=round(credit, 2), occ=round(occ, 2),
                        pnl=round(pnl, 2), roi=pnl / occ if occ else 0,
                        broke=broke, how=how, hold=(ex or exp) - d))
    return out


def summarize(rows, label, span_days):
    if not rows:
        return None
    n = len(rows)
    nb = sum(1 for r in rows if not r["broke"])
    wp = sum(1 for r in rows if r["pnl"] > 0)
    tot = sum(r["pnl"] for r in rows)
    occ = statistics.median(r["occ"] for r in rows)
    hold = statistics.mean(r["hold"].days for r in rows)
    # 峰值占用：每天开一张、持有到平仓，同时在手的最大张数 × 单笔占用
    live = defaultdict(float)
    for r in rows:
        d = r["open"]
        while d <= (r["open"] + r["hold"]):
            live[d] += r["occ"]
            d = date.fromordinal(d.toordinal() + 1)
    peak = max(live.values()) if live else occ
    roi_per = statistics.mean(r["roi"] for r in rows)
    ann = tot / peak * 365 / span_days if peak and span_days else 0
    return dict(label=label, n=n, unbroken=nb / n, winrate=wp / n, total=tot,
                per=tot / n, occ=occ, peak=peak, roi=roi_per, hold=hold, annual=ann)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--mode", default="mid25", choices=["mid", "mid25", "worst"])
    ap.add_argument("--dte", default="4,11")
    a = ap.parse_args()
    sym = a.symbol
    lo, hi = (int(x) for x in a.dte.split(","))
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=300)}
    st = SnapshotStore()
    snaps = {}
    inst = {"SLV": "silver", "GLD": "gold"}[sym]
    for f in sorted(os.listdir(f"data/snapshots/options/{sym}")):
        if not f.endswith(".json.gz"):
            continue
        try:
            d = datetime.strptime(f[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d.isoformat() not in closes:
            continue
        try:
            snaps[d] = snapshot_from_payload(st.load("options", sym, d), inst, sym)
        except Exception:
            pass
    ds = sorted(snaps)
    span = (ds[-1] - ds[0]).days or 1
    print(f"{sym}　{ds[0]} ~ {ds[-1]}（{span} 天，{len(ds)} 个快照日）　"
          f"成交假设={a.mode}　到期 {lo}~{hi} 天\n")
    hdr = (f"{'策略':<18}{'宽度':>5}{'笔数':>5}{'未破墙':>7}{'净盈利':>7}{'总损益':>9}"
           f"{'平均/笔':>8}{'单笔占用':>9}{'峰值占用':>9}{'单笔ROI':>8}{'年化':>8}")
    print(hdr)
    print("-" * 100)
    store = {}
    for width in (1.0, 3.0, 5.0):
        legs = {}
        for kind, klab in (("P", "卖 put 价差"), ("C", "卖 call 价差")):
            rows = run(sym, kind, width, a.mode, lo, hi, snaps, closes)
            legs[kind] = rows
            s = summarize(rows, klab, span)
            if s:
                store[(klab, width)] = s
                print(f"{klab:<18}${width:>4.0f}{s['n']:>5}{s['unbroken']:>6.0%}"
                      f"{s['winrate']:>7.0%}{s['total']:>+9.0f}{s['per']:>+8.1f}"
                      f"{s['occ']:>9.0f}{s['peak']:>9.0f}{s['roi']*100:>+7.1f}%"
                      f"{s['annual']*100:>+7.0f}%")
        # 铁鹰：同日两侧都开，占用取单边（券商按较大一侧计）
        byd = defaultdict(dict)
        for k in ("P", "C"):
            for r in legs.get(k, []):
                byd[r["open"]][k] = r
        iron = [dict(open=d, pnl=v["P"]["pnl"] + v["C"]["pnl"],
                     occ=max(v["P"]["occ"], v["C"]["occ"]),
                     roi=(v["P"]["pnl"] + v["C"]["pnl"]) / max(v["P"]["occ"], v["C"]["occ"]),
                     broke=v["P"]["broke"] or v["C"]["broke"],
                     hold=max(v["P"]["hold"], v["C"]["hold"]))
                for d, v in sorted(byd.items()) if "P" in v and "C" in v]
        s = summarize(iron, "铁鹰（双边）", span)
        if s:
            store[("铁鹰（双边）", width)] = s
            print(f"{'铁鹰（双边）':<18}${width:>4.0f}{s['n']:>5}{s['unbroken']:>6.0%}"
                  f"{s['winrate']:>7.0%}{s['total']:>+9.0f}{s['per']:>+8.1f}"
                  f"{s['occ']:>9.0f}{s['peak']:>9.0f}{s['roi']*100:>+7.1f}%"
                  f"{s['annual']*100:>+7.0f}%")
        print()
    print("说明：")
    print("  未破墙 = 到期日收盘未越过卖腿（盘中触及不算）")
    print("  净盈利 = 扣手续费后损益为正的比例")
    print("  峰值占用 = 每天开一张、滚动持有下同时在手的最大保证金")
    print("  年化 = 总损益 ÷ 峰值占用 × 365 ÷ 样本天数")
    print(f"\n⚠️ 样本期 {sym} 单边上涨，put 侧零破墙有相当部分来自方向，不能外推到跌市。")


if __name__ == "__main__":
    main()
