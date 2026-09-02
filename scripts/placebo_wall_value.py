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

**结论：无法区分。卖在墙上并不比同距离的随便一档更好。**
收益来自距离与方向，不来自墙。这与 call 侧对照一致
（put 墙实际破得比 IV 隐含少 17.7%，call 墙多破 18.3%，对称相反 = 趋势签名）。

⚠️ 这不是说墙不存在，是说【对卖方价差选腿而言】墙没有额外信息。
   要推翻本结论，需要在跌市样本上重跑，或找到墙起作用的其它条件。
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
    snaps = {}
    for f in sorted(os.listdir(f"data/snapshots/options/{sym}")):
        if not f.endswith(".json.gz"):
            continue
        try:
            fd = datetime.strptime(f[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        sess = st.decision_session("options", sym, fd, tdays)
        if sess is None or sess in snaps:
            continue
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
    rng = random.Random(20260902)
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
                # 安慰剂：同等【距现价百分比】但不是墙的行权价
                target = wk
                cands = [x for x in ks if x != wk and x < spot
                         and abs(abs(x / spot - 1) - abs(target / spot - 1)) < 0.015]
                if not cands:
                    continue
                pk = rng.choice(cands)
                pfar = [x for x in ks if x < pk]
                if not pfar:
                    continue
                pbk = min(pfar, key=lambda x: abs(x - (pk - wa)))
                tp = trade(legs, exp, pk, pbk, closes, spot, kind)
                if tp is None:
                    continue
                tw["d"] = tp["d"] = d
                tw["sym"] = tp["sym"] = sym
                allw.append(tw); allp.append(tp)
                break

    def summ(rows, lab):
        n = len(rows)
        nb = sum(1 for r in rows if not r["broke"])
        tot = sum(r["pnl"] for r in rows)
        live = defaultdict(float)
        for r in rows:
            live[r["d"].toordinal()] += r["occ"]
        peak = max(live.values()) if live else 1
        print(f"  {lab:<10} n={n:<4} 未破墙 {nb/n:>5.0%}  均缓冲 {statistics.mean(r['buf'] for r in rows):>4.1f}%"
              f"  均权利金 ${statistics.mean(r['credit'] for r in rows):>5.1f}"
              f"  总损益 {tot:>+7.0f}  收益率 {tot/peak:>+6.1%}")
        return tot / peak

    print(f"安慰剂对照：卖【结构墙】vs 卖【同等距离的非墙行权价】")
    print(f"品种 SLV/GLD/QQQ/USO　put 侧　宽度 3%　到期 4~11 天\n")
    a = summ(allw, "结构墙")
    b = summ(allp, "非墙同距")
    print(f"\n  差 {a-b:+.1%}")
    # 配对置换检验（同一笔的两个版本，按日期簇）
    byday = defaultdict(list)
    for w, p in zip(allw, allp):
        byday[w["d"]].append((w["pnl"], p["pnl"]))
    days = list(byday)
    obs = sum(w - p for dd in days for w, p in byday[dd])
    rng2 = random.Random(11)
    ge = 0
    N = 20000
    for _ in range(N):
        tot = 0.0
        for dd in days:
            flip = rng2.random() < 0.5      # 整簇翻转标签
            for w, p in byday[dd]:
                tot += (p - w) if flip else (w - p)
        if tot >= obs:
            ge += 1
    print(f"  配对差合计 {obs:+.0f}　{len(days)} 个日期簇符号置换 p={ge/N:.3f}")
    print(f"  → {'墙位有额外价值' if ge/N < 0.05 else '【无法区分】：卖在墙上并不比同距离的随便一档更好'}")


if __name__ == "__main__":
    main()
