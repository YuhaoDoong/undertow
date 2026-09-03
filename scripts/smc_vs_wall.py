"""SMC 结构区 × 期权墙 的交叉验证（用户 2026-09-03 提出）

问题：K 线技术面（SMC 供需区）与期权墙如果重合，可信度是否更高？

设计：
· 对每个可交易日 D：用 **D 之前**的日线算 SMC 区（无前视，pivot 需右侧确认）
· 用 D 的快照按第一步定版规则选墙（gamma.pick_sell_wall）
· 判断墙的行权价是否落在同向 SMC 区里（put 墙 ↔ 需求区，call 墙 ↔ 供给区）
· 对比：重合组 vs 不重合组的破墙率（当日收盘口径，与第一步一致）
· 对照组：同一天、同样距现价百分比的**非墙行权价**，落在 SMC 区的比例
  —— 若墙与非墙的重合率没差别，说明"重合"只是区域覆盖面广，不是信息
"""
import pathlib, sys, statistics
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze.gamma import pick_sell_wall, structural_walls   # noqa
from undertow.analyze.smc import build_zones, confluence              # noqa
from undertow.cli import snapshot_from_payload                        # noqa
from undertow.collect.longbridge_kline import fetch_bars              # noqa
from undertow.collect.store import SnapshotStore                      # noqa

INST = {"GLD": "gold", "SLV": "silver"}
TOL = 0.005          # 墙落在区间 ±0.5% 内算重合


def load(sym):
    bars = fetch_bars(f"{sym}.US", period="day", count=300)
    px = {str(b["ts"])[:10]: b for b in bars}
    dates = sorted(px)
    tdays = [datetime.strptime(x, "%Y-%m-%d").date() for x in dates]
    st = SnapshotStore()
    cand = {}
    for fd in st.dates("options", sym):
        sess = st.decision_session("options", sym, fd, tdays)
        if sess is None:
            continue
        ca = st.captured_at("options", sym, fd) or 0.0
        if sess not in cand or ca > cand[sess][0]:
            cand[sess] = (ca, fd)
    snaps = {}
    for sess, (_, fd) in cand.items():
        pay = st.load("options", sym, fd)
        if pay is None:
            continue
        try:
            snaps[sess] = snapshot_from_payload(pay, INST[sym], sym)
        except Exception:
            pass
    return snaps, px, dates


def run(sym, tol=TOL):
    snaps, px, dates = load(sym)
    rows = []
    for sess in sorted(snaps):
        k = sess.isoformat()
        prior = [d for d in dates if d < k]
        if len(prior) < 80 or k not in px:
            continue
        spot = px[prior[-1]]["close"]           # 决策价 = D−1 收盘
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        # SMC 只用 D 之前的 K 线
        w = prior[-120:]
        o = [px[d]["open"] for d in w]; h = [px[d]["high"] for d in w]
        lo = [px[d]["low"] for d in w]; c = [px[d]["close"] for d in w]
        zones = build_zones(o, h, lo, c)
        close_today = px[k]["close"]
        for kind, want in (("P", "需求"), ("C", "供给")):
            r = pick_sell_wall(snaps[sess], obs, spot, kind)
            if r is None:
                continue
            W = r["strike"]
            zs = [z for z in confluence(W, zones, tol=tol) if z.kind == want]
            broke = (close_today < W) if kind == "P" else (close_today > W)
            # 对照：同一天同距离的非墙行权价
            allk = sorted({x.strike for x in snaps[sess].contracts if x.kind == kind})
            tgt = abs(W / spot - 1)
            ctrl = [x for x in allk if x != W and abs(abs(x / spot - 1) - tgt) < 0.015
                    and ((x < spot) if kind == "P" else (x > spot))]
            ctrl_hit = sum(1 for x in ctrl
                           if any(z.kind == want for z in confluence(x, zones, tol=tol)))
            rows.append(dict(sym=sym, date=k, kind=kind, wall=W, spot=spot,
                             buf=abs(W / spot - 1) * 100, broke=broke,
                             n_zone=len(zs), zone_src=",".join(sorted({z.source for z in zs})),
                             n_ctrl=len(ctrl), ctrl_hit=ctrl_hit,
                             n_zones_total=len([z for z in zones if z.kind == want])))
    return rows


def main():
    tol = float(sys.argv[1]) if len(sys.argv) > 1 else TOL
    rows = []
    for sym in INST:
        rows += run(sym, tol)
    print(f"容差 ±{tol*100:.1f}%　样本 {len(rows)} 个（品种×可交易日×侧）\n")
    print("═══ ① 墙落在同向 SMC 区里的比例 vs 同距离非墙行权价 ═══")
    for kind, kl in (("P", "put 墙/需求区"), ("C", "call墙/供给区")):
        g = [r for r in rows if r["kind"] == kind]
        if not g:
            continue
        wall_hit = sum(1 for r in g if r["n_zone"] > 0) / len(g)
        cs = [r for r in g if r["n_ctrl"] > 0]
        ctrl_rate = (sum(r["ctrl_hit"] for r in cs) / sum(r["n_ctrl"] for r in cs)) if cs else 0
        print(f"  {kl}: 墙重合 {wall_hit:.0%}（{sum(1 for r in g if r['n_zone']>0)}/{len(g)}）"
              f"　同距离非墙 {ctrl_rate:.0%}"
              f"　差 {wall_hit-ctrl_rate:+.0f}pp" if False else
              f"  {kl}: 墙重合 {wall_hit:.0%}（{sum(1 for r in g if r['n_zone']>0)}/{len(g)}）"
              f"　同距离非墙 {ctrl_rate:.0%}　差 {(wall_hit-ctrl_rate)*100:+.0f}pp")
    print("\n═══ ② 关键：重合 vs 不重合 的破墙率（当日收盘）═══")
    for kind, kl in (("P", "put "), ("C", "call")):
        for lab, sel in (("重合", lambda r: r["n_zone"] > 0), ("不重合", lambda r: r["n_zone"] == 0)):
            g = [r for r in rows if r["kind"] == kind and sel(r)]
            if len(g) < 3:
                print(f"  {kl} {lab:<4} n={len(g)} 样本不足")
                continue
            b = sum(1 for r in g if r["broke"])
            print(f"  {kl} {lab:<4} n={len(g):>3}  破墙 {b}/{len(g)} = {b/len(g):>5.1%}"
                  f"  均缓冲 {statistics.mean(r['buf'] for r in g):>4.1f}%")
    print("\n═══ ③ 按结构类型拆（哪类结构最能佐证）═══")
    by = defaultdict(list)
    for r in rows:
        for s in (r["zone_src"].split(",") if r["zone_src"] else ["无"]):
            by[s].append(r)
    for s in sorted(by, key=lambda x: -len(by[x])):
        g = by[s]
        if len(g) < 5:
            continue
        b = sum(1 for r in g if r["broke"])
        print(f"  {s:<12} n={len(g):>3}  破墙 {b/len(g):>5.1%}  均缓冲 {statistics.mean(r['buf'] for r in g):>4.1f}%")


if __name__ == "__main__":
    main()
