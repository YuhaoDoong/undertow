"""墙位卖方价差参数扫描 —— 黄金/白银分开，逐维度测

用户 2026-08-31：「到期日，口径，价差等等，这些参数都需要测出来。
白银和黄金分开，不同品种可能还是不太一样。」

墙的定义由参数 wall_cap（到期上限天数）决定，墙位【每天重算但只在跨档时换】：
  · 计算 ≤wall_cap 天到期的累计 OI，取现价上/下方最大者为墙
  · 只有当墙位连续 confirm 天都指向新位置时才换墙（滤掉一日抖动）
  · 换墙当天平掉旧墙位的全部未到期仓位（用当日盘口）
结算：到期日收盘越过卖腿 = 破墙。成交：组合单中价往不利方向让 25%。
"""
import argparse, itertools, json, os, pathlib, statistics, sys
from collections import defaultdict
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.cli import snapshot_from_payload, _prev_weekday      # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars           # noqa: E402
from undertow.collect.store import SnapshotStore                   # noqa: E402

FEE = 0.80
INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "TQQQ": "tqqq",
        "SPY": "spy", "IWM": "iwm", "TLT": "tlt"}


def load(sym):
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=300)}
    st = SnapshotStore()
    snaps = {}
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
            snaps[d] = snapshot_from_payload(st.load("options", sym, d), INST[sym], sym)
        except Exception:
            pass
    return snaps, closes


def raw_wall(snap, obs, kind, cap, band=0.15):
    # band 决定往现价多远找墙。2026-08-31 实测这一维被我漏了整整一天：
    # 硬编码 15% 会让 GLD 8/31 选出 350（距现价 -14%），那里权利金只有 $2、
    # 连 $3.2 手续费都不够；band≤10% 才会选 400（-2%）。白银不受影响
    # （60 在任何 band 下都被选中），所以之前 SLV 的结果没问题、GLD 被系统性低估。
    """≤cap 天到期的累计 OI 最大行权价"""
    spot = snap.spot
    agg = defaultdict(int)
    for c in snap.contracts:
        if c.kind != kind:
            continue
        if kind == "C" and not (spot <= c.strike <= spot * (1 + band)):
            continue
        if kind == "P" and not (spot * (1 - band) <= c.strike <= spot):
            continue
        if 1 <= (c.expiry - obs).days <= cap:
            agg[c.strike] += c.open_interest
    return max(agg, key=agg.get) if agg else None


def locked_walls(snaps, kind, cap, confirm, band=0.15):
    """墙位序列：只有连续 confirm 天指向同一新位置才换墙"""
    ds = sorted(snaps)
    raw = {d: raw_wall(snaps[d], _prev_weekday(d), kind, cap, band) for d in ds}
    out = {}
    cur = None
    streak = 0
    cand = None
    for d in ds:
        r = raw[d]
        if r is None:
            out[d] = cur
            continue
        if cur is None:
            cur = r
        elif r != cur:
            if r == cand:
                streak += 1
            else:
                cand, streak = r, 1
            if streak >= confirm:
                cur, cand, streak = r, None, 0
        else:
            cand, streak = None, 0
        out[d] = cur
    return out


def fill(sc, bc, closing=False, give=0.25):
    sb, sa = sc.bid or 0, sc.ask or 0
    bb, ba = bc.bid or 0, bc.ask or 0
    mid = ((sb + sa) / 2 - (bb + ba) / 2) * 100
    worst = ((sa - bb) if closing else (sb - ba)) * 100
    return mid + (worst - mid) * give


MIN_CREDIT_MULT = 3.0   # 权利金至少要是手续费的几倍，否则这笔没有意义


def run(snaps, closes, kind, cap, confirm, width, dte_lo, dte_hi, offset=0.0,
        width_is_pct=False, band=0.15, min_credit_mult=MIN_CREDIT_MULT):
    """width_is_pct=True 时 width 是【占现价的百分比】。

    用户 2026-08-31：「价差肯定要根据品种来啊，QQQ 和 TQQQ 都不一样」。
    用绝对美元当宽度会让不同价位的品种测的根本不是同一个东西：$2 宽度
    对 SLV(60) 是 3.3%、对 QQQ(716) 只有 0.28% —— 后者权利金薄到扣完
    手续费只剩 $2，于是 QQQ 看起来「策略无效」，其实是宽度设错了。
    """
    walls = locked_walls(snaps, kind, cap, confirm, band)
    ds = sorted(snaps)
    switches = {ds[i] for i in range(1, len(ds)) if walls[ds[i]] != walls[ds[i - 1]]}
    out = []
    for d in ds:
        w = walls.get(d)
        if w is None:
            continue
        spot = snaps[d].spot
        k = w * (1 + offset) if kind == "C" else w * (1 - offset)
        legs = defaultdict(dict)
        for c in snaps[d].contracts:
            if c.kind != kind or c.bid is None or not c.ask:
                continue
            if not (dte_lo <= (c.expiry - d).days <= dte_hi):
                continue
            legs[c.expiry][c.strike] = c
        pick = None
        for exp in sorted(legs):
            ks = sorted(legs[exp])
            cand = [x for x in ks if (x > spot if kind == "C" else x < spot)]
            if not cand:
                continue
            s = min(cand, key=lambda x: abs(x - k))
            w_abs = spot * width if width_is_pct else width
            tgt = s + w_abs if kind == "C" else s - w_abs
            far = [x for x in ks if (x > s if kind == "C" else x < s)]
            if not far:
                continue
            b = min(far, key=lambda x: abs(x - tgt))
            pick = (exp, legs[exp][s], legs[exp][b], s, b)
            break
        if not pick:
            continue
        exp, sc, bc, sk, bk = pick
        credit = fill(sc, bc)
        # 权利金连手续费几倍都不到的笔没有交易意义，且会系统性拖低统计
        if credit <= FEE * 4 * min_credit_mult:
            continue
        realw = abs(bk - sk) * 100
        if realw <= credit:
            continue
        occ = realw - credit
        ex = next((s for s in sorted(switches) if d < s < exp), None)
        pnl = None
        broke = False
        if ex and ex in snaps:
            s2 = b2 = None
            for c in snaps[ex].contracts:
                if c.expiry != exp or c.kind != kind or c.bid is None or not c.ask:
                    continue
                if abs(c.strike - sk) < 1e-6:
                    s2 = c
                elif abs(c.strike - bk) < 1e-6:
                    b2 = c
            if s2 and b2:
                pnl = credit - fill(s2, b2, closing=True) - FEE * 4
        if pnl is None:
            se = closes.get(exp.isoformat())
            if se is None:
                continue
            itr = (max(0.0, min(se - sk, realw / 100)) if kind == "C"
                   else max(0.0, min(sk - se, realw / 100))) * 100
            pnl = credit - itr - FEE * 4
            broke = (se > sk) if kind == "C" else (se < sk)
        out.append(dict(d=d, pnl=pnl, occ=occ, credit=credit, broke=broke,
                        roi=pnl / occ, hold=((ex or exp) - d).days))
    return out, len(switches)


def stat(rows, span):
    if len(rows) < 8:
        return None
    live = defaultdict(float)
    for r in rows:
        for k in range(r["hold"] + 1):
            live[r["d"].toordinal() + k] += r["occ"]
    peak = max(live.values()) if live else 1
    tot = sum(r["pnl"] for r in rows)
    return dict(n=len(rows), unbroken=sum(1 for r in rows if not r["broke"]) / len(rows),
                win=sum(1 for r in rows if r["pnl"] > 0) / len(rows), total=tot,
                roi=statistics.mean(r["roi"] for r in rows), peak=peak,
                annual=tot / peak * 365 / span, occ=statistics.median(r["occ"] for r in rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV,GLD")
    ap.add_argument("--dim", default="cap",
                    choices=["cap", "confirm", "width", "dte", "offset"])
    a = ap.parse_args()
    for sym in a.symbol.split(","):
        snaps, closes = load(sym)
        ds = sorted(snaps)
        span = (ds[-1] - ds[0]).days or 1
        print(f"\n{'='*104}\n{sym}　{ds[0]} ~ {ds[-1]}（{span}天 / {len(ds)}个快照）"
              f"　扫描维度：{a.dim}\n{'='*104}")
        base = dict(cap=45, confirm=2, width=3.0, dte=(4, 11), offset=0.0)
        grid = {"cap": [14, 30, 45, 90, 9999], "confirm": [1, 2, 3],
                "width": [1.0, 2.0, 3.0, 5.0],
                "dte": [(1, 4), (4, 11), (12, 25), (26, 45)],
                "offset": [0.0, 0.01, 0.02, 0.03]}[a.dim]
        print(f"{'参数':<12}{'侧':<8}{'笔数':>5}{'换墙':>5}{'未破墙':>7}{'净盈利':>7}"
              f"{'总损益':>9}{'单笔ROI':>9}{'占用中位':>9}{'峰值':>8}{'年化':>8}")
        print("-" * 104)
        for v in grid:
            p = dict(base)
            p[a.dim] = v
            lab = f"{v}" if not isinstance(v, tuple) else f"{v[0]}~{v[1]}天"
            legs = {}
            for kind, kl in (("P", "卖put"), ("C", "卖call")):
                rows, nsw = run(snaps, closes, kind, p["cap"], p["confirm"],
                                p["width"], p["dte"][0], p["dte"][1], p["offset"])
                legs[kind] = rows
                s = stat(rows, span)
                if s:
                    print(f"{lab:<12}{kl:<8}{s['n']:>5}{nsw:>5}{s['unbroken']:>6.0%}"
                          f"{s['win']:>7.0%}{s['total']:>+9.0f}{s['roi']*100:>+8.1f}%"
                          f"{s['occ']:>9.0f}{s['peak']:>8.0f}{s['annual']*100:>+7.0f}%")
            byd = defaultdict(dict)
            for k in ("P", "C"):
                for r in legs.get(k, []):
                    byd[r["d"]][k] = r
            iron = [dict(d=d, pnl=v2["P"]["pnl"] + v2["C"]["pnl"],
                         occ=max(v2["P"]["occ"], v2["C"]["occ"]),
                         roi=(v2["P"]["pnl"] + v2["C"]["pnl"]) / max(v2["P"]["occ"], v2["C"]["occ"]),
                         broke=v2["P"]["broke"] or v2["C"]["broke"],
                         hold=max(v2["P"]["hold"], v2["C"]["hold"]))
                    for d, v2 in sorted(byd.items()) if "P" in v2 and "C" in v2]
            s = stat(iron, span)
            if s:
                print(f"{'':<12}{'铁鹰':<8}{s['n']:>5}{'':>5}{s['unbroken']:>6.0%}"
                      f"{s['win']:>7.0%}{s['total']:>+9.0f}{s['roi']*100:>+8.1f}%"
                      f"{s['occ']:>9.0f}{s['peak']:>8.0f}{s['annual']*100:>+7.0f}%")
            print()


if __name__ == "__main__":
    main()
