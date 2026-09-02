"""安慰剂对照：卖在墙上 vs 卖在同等距离的非墙位置（2026-09-02）。

这是墙位卖方价差策略的**存亡检验**。策略的全部前提是「墙难破，所以卖在墙上
胜率高」。前半句对，但要证明后半句，必须排除「胜率高只是因为卖得够远 +
样本期在涨」这个平凡解释。

做法：对每一笔"卖在结构墙上"的交易，在同一到期、同一价差宽度下，
另找一个【距现价百分比相同（±1.5pp）但不是墙】的行权价，构成配对样本。
两笔的距离、宽度、到期、标的、方向全部相同，唯一差别就是"是不是墙"。

实测结果（SLV/GLD/QQQ/USO，put 侧，宽度 3%，到期 4~11 天）：

    结构墙    n=79  未破墙 100%  缓冲 5.2%  权利金 $52.8  收益率 +110.0%
    非墙同距  n=79  未破墙 100%  缓冲 5.0%  权利金 $50.2  收益率  +98.4%
    差 +11.6%　37 个日期簇符号置换 p=0.267

⚠️ **这个检验能回答什么、不能回答什么**（2026-09-02 自查 + codex 复核）：

两组都是 0/79 破墙。未破墙时 pnl ≡ credit − fee，所以配对差里**只剩权利金差**
（$2.6/笔 × 79 ≈ $205，实测 +202，完全吻合）。因此：

  能回答：墙上收到的权利金，是否高于同距离的非墙位置。→ 看不出差别。
  不能回答：墙上是否更难破。→ **观测到的比较信息为零**（0 事件对 0 事件，
            McNemar 型配对率差的有效事件数是 0），而这才是策略的核心主张。

所以本脚本【不能】用来说"证伪了墙位价值"。要检验核心主张必须有破墙样本，
即需要跌市数据。在那之前，这个策略与"卖远虚值 put 价差"无法区分。
"""
import os, pathlib, random, statistics, sys
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.analyze import wall_spread as ws
from undertow.analyze.gamma import structural_walls
from undertow.cli import snapshot_from_payload
from undertow.collect.longbridge_kline import fetch_bars
from undertow.collect.store import SnapshotStore

INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "USO": "wti"}


def load(sym):
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=400)}
    dates = sorted(closes)
    tdays = [datetime.strptime(x, "%Y-%m-%d").date() for x in dates]
    st = SnapshotStore()
    snaps, cand = {}, {}
    for f in sorted(os.listdir(f"data/snapshots/options/{sym}")):
        if not f.endswith(".json.gz"):
            continue
        try:
            fd = datetime.strptime(f[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        sess = st.decision_session("options", sym, fd, tdays)
        if sess is None:
            continue
        # codex 2026-09-02 P0：同一决策日多份快照要取 captured_at 最新的，
        # 不是文件名最早的（周六/周日/周一盘前都映射到周一）。
        ca = st.captured_at("options", sym, fd) or 0.0
        prev = cand.get(sess)
        if prev is None or ca > prev[0]:
            cand[sess] = (ca, fd)
    for sess, (_, fd) in cand.items():
        pay = st.load("options", sym, fd)
        if pay is None:
            continue
        try:
            snaps[sess] = snapshot_from_payload(pay, INST[sym], sym)
        except Exception:
            pass
    return snaps, closes, dates


def trade(legs, exp, sk, bk, closes, spot, kind):
    """给定卖腿/买腿，算这笔的损益。返回 None 表示不可成交。"""
    if sk not in legs[exp] or bk not in legs[exp]:
        return None
    credit = ws._fill(legs[exp][sk], legs[exp][bk])
    width = abs(bk - sk) * 100
    if credit <= ws.FEE_PER_LEG * 4 * ws.MIN_CREDIT_MULT or width <= credit:
        return None
    se = closes.get(exp.isoformat())
    if se is None:
        return None
    itr = (max(0.0, min(se - sk, width / 100)) if kind == "C"
           else max(0.0, min(sk - se, width / 100))) * 100
    return dict(pnl=credit - itr - ws.FEE_PER_LEG * 4, occ=width - credit,
                credit=credit, sell=sk, broke=(se < sk) if kind == "P" else (se > sk),
                buf=abs(sk / spot - 1) * 100)


def main():
    kind, wp, dte = "P", 0.03, (4, 11)
    allw, allp = [], []
    for sym in ("SLV", "GLD", "QQQ", "USO"):
        snaps, closes, dates = load(sym)
        for d in sorted(snaps):
            snap = snaps[d]
            prior = [x for x in dates if x < d.isoformat()]
            if not prior:
                continue
            spot = closes[prior[-1]]
            obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
            w = structural_walls(snap, obs, spot, kind, top_n=1)
            if not w:
                continue
            wk = w[0]["strike"]
            legs = defaultdict(dict)
            for c in snap.contracts:
                if c.kind == kind and c.bid is not None and c.ask:
                    if dte[0] <= (c.expiry - d).days <= dte[1]:
                        legs[c.expiry][c.strike] = c
            for exp in sorted(legs):
                ks = sorted(legs[exp])
                if wk not in ks:
                    continue
                wa = spot * wp
                far = [x for x in ks if x < wk]
                if not far:
                    continue
                bk = min(far, key=lambda x: abs(x - (wk - wa)))
                tw = trade(legs, exp, wk, bk, closes, spot, kind)
                if tw is None:
                    continue
                # ── 安慰剂腿（codex 2026-09-02 P0-2 / P1-2 修正）──
                # 旧实现的三个问题：① rng.choice 随机取一个 → 结论依赖 seed，
                # 且抽中不可成交的候选会整对丢弃，改变入样资格；
                # ② 只排除了 wk，没排除其它同样是结构墙的档位；
                # ③ 买腿各自取最近档，实际价差宽度不保证相同。
                # 现在：排除全部结构墙 → 只留【实际宽度与主腿相同】的候选
                # → 在其中取距离差最小的（确定性），平手取行权价较高的。
                all_walls = {w2["strike"] for w2 in
                             structural_walls(snap, obs, spot, kind, top_n=99)}
                tgt_dist = abs(wk / spot - 1)
                real_w = abs(bk - wk)
                pool = []
                for x in ks:
                    if x in all_walls or x >= spot:
                        continue
                    if abs(abs(x / spot - 1) - tgt_dist) >= 0.015:
                        continue
                    xb = x - real_w                      # 强制同宽度
                    if xb not in legs[exp]:
                        continue
                    t2 = trade(legs, exp, x, xb, closes, spot, kind)
                    if t2 is None:
                        continue
                    t2["dist_gap"] = abs(abs(x / spot - 1) - tgt_dist)
                    pool.append(t2)
                if not pool:
                    continue
                pool.sort(key=lambda t: (t["dist_gap"], -t["sell"]))
                tp = pool[0]
                tw["n_ctrl"] = len(pool)
                tw["dist_gap"] = tp["dist_gap"]
                # 敏感性：全部候选取均值
                tw["ctrl_mean_pnl"] = statistics.mean(t["pnl"] for t in pool)
                tw["d"] = tp["d"] = d
                tw["sym"] = tp["sym"] = sym
                allw.append(tw); allp.append(tp)
                break

    def summ(rows, lab):
        n = len(rows)
        nb = sum(1 for r in rows if not r["broke"])
        print(f"  {lab:<10} n={n:<4} 未破墙 {nb/n:>5.0%}  均缓冲 "
              f"{statistics.mean(r['buf'] for r in rows):>4.1f}%"
              f"  均权利金 ${statistics.mean(r['credit'] for r in rows):>5.1f}"
              f"  均占用 ${statistics.mean(r['occ'] for r in rows):>6.1f}"
              f"  均损益 ${statistics.mean(r['pnl'] for r in rows):>+6.1f}")

    print("安慰剂对照：卖【结构墙】vs 卖【同距离、同宽度、非墙】")
    print("品种 SLV/GLD/QQQ/USO　put 侧　宽度 3%　到期 4~11 天")
    print("配对：排除全部结构墙 → 只留实际宽度相同的候选 → 取距离差最小者（确定性）\n")
    summ(allw, "结构墙")
    summ(allp, "非墙同距")

    # ⚠️ codex 2026-09-02 P0-3：展示的效应量与被检验的统计量必须是同一个。
    # 旧版展示「各自 总PnL/各自峰值占用」之差(+11.6%)，检验却用原始美元和，
    # 两者不是一回事，把 p 标在 +11.6% 后面是错的。
    # 现在统一为【配对 ROI 差】：每对用共同资本 max(occ_w, occ_p)。
    pairs = [((w["pnl"] - p_["pnl"]) / max(w["occ"], p_["occ"]), w["d"])
             for w, p_ in zip(allw, allp) if max(w["occ"], p_["occ"]) > 0]
    obs = statistics.mean(v for v, _ in pairs)
    print(f"\n  配对 ROI 差（共同资本 max(occ_w, occ_p)）：均 {obs:+.2%}")
    print(f"  逐对距离差 中位 {statistics.median(w['dist_gap'] for w in allw):.4f}"
          f"　可用对照档数 中位 {statistics.median(w['n_ctrl'] for w in allw):.0f}")

    byday = defaultdict(list)
    for v, d in pairs:
        byday[d].append(v)
    days = list(byday)
    rng2 = random.Random(11)
    N = 20000
    ge = 0
    for _ in range(N):
        acc, cnt = 0.0, 0
        for dd in days:                      # 整簇翻转符号
            sgn = 1.0 if rng2.random() < 0.5 else -1.0
            for v in byday[dd]:
                acc += sgn * v
                cnt += 1
        if abs(acc / cnt) >= abs(obs):       # 双侧
            ge += 1
    print(f"  {len(days)} 个日期簇符号置换（**双侧**）p={ge/N:.3f}")

    # 敏感性：对照取全部候选均值
    pairs2 = [((w["pnl"] - w["ctrl_mean_pnl"]) / max(w["occ"], p_["occ"]), w["d"])
              for w, p_ in zip(allw, allp) if max(w["occ"], p_["occ"]) > 0]
    print(f"  敏感性（对照取全部候选均值）：配对 ROI 差均 "
          f"{statistics.mean(v for v, _ in pairs2):+.2%}")

    nb_w = sum(1 for r in allw if r["broke"])
    nb_p = sum(1 for r in allp if r["broke"])
    print(f"\n  破墙笔数：结构墙 {nb_w}/{len(allw)}　非墙 {nb_p}/{len(allp)}")
    if nb_w == 0 and nb_p == 0:
        print("  ⛔ 两组均【零破墙事件】——配对率差的有效信息量为 0。")
        print("     本次比较只能说明权利金维度看不出差别；")
        print("     『墙上是否更难破』这个核心主张 **无法检验**，需要跌市样本。")
    else:
        print("  （出现破墙事件，可对率差做 McNemar 检验）")


if __name__ == "__main__":
    main()
