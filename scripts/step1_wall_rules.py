"""第一步：只比较【选墙规则】的破墙率，不涉及价差宽度、到期、权利金、损益。

用户 2026-09-02 定的三步法：
  ① 先找出破墙率最低、且接近现价的墙位置
  ② 墙位定了再测价差宽度与到期日
  ③ 最后处理换墙/提前平仓等细节

此前的回测把三件事混在一起同时变动，结果无法归因（用户："一团浆糊"）。
本脚本只做第一步。

⚠️ 破墙率必须和【缓冲距离】一起读：卖距现价 +91% 的 call 永远不破墙，
   但那是废话。有意义的比较是"同样的缓冲距离下，谁的破墙率更低"。
   为此加入【固定距离】对照组：不看 OI，只按距现价百分比选行权价。
   如果按墙选和按固定距离选的破墙率没差别，墙对选腿就没有信息。

口径：
· 可交易日由 captured_at 推导（clock.decision_session），同日多份取最新
· 决策价 = C[可交易日前一交易日] 收盘
· 破墙 = 到期日【收盘】越过该行权价
· 每个可交易日对每个到期各记一条（不去重，统计时按日期簇处理）
"""
import argparse
import os
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze.gamma import structural_walls          # noqa: E402
from undertow.cli import snapshot_from_payload               # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars      # noqa: E402
from undertow.collect.store import SnapshotStore              # noqa: E402

INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "USO": "wti"}


def load(sym):
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=260)}
    dates = sorted(closes)
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
    return snaps, closes, dates


# ── 选墙规则：输入 (快照, 决策价, 侧, 观察日)，输出行权价或 None ──────────
def r_struct(maxdist):
    def f(snap, spot, kind, obs):
        w = [x for x in structural_walls(snap, obs, spot, kind, top_n=8)
             if abs(x["dist_pct"]) <= maxdist * 100]
        return w[0]["strike"] if w else None
    f.__name__ = f"结构墙#1≤{maxdist*100:.0f}%"
    return f


def r_struct_n(maxdist, n):
    def f(snap, spot, kind, obs):
        w = [x for x in structural_walls(snap, obs, spot, kind, top_n=8)
             if abs(x["dist_pct"]) <= maxdist * 100]
        return w[n - 1]["strike"] if len(w) >= n else None
    f.__name__ = f"结构墙#{n}≤{maxdist*100:.0f}%"
    return f


def r_round5(maxdist):
    """只在 5 的倍数里选 OI 最大的（用户 2026-09-02：这些墙往往是 5 的倍数）。"""
    def f(snap, spot, kind, obs):
        agg = defaultdict(int)
        for c in snap.contracts:
            if c.kind != kind or not c.open_interest:
                continue
            if kind == "P" and c.strike > spot:
                continue
            if kind == "C" and c.strike < spot:
                continue
            if abs(c.strike / spot - 1) > maxdist:
                continue
            if abs(c.strike % 5) > 1e-9:
                continue
            agg[c.strike] += c.open_interest
        return max(agg, key=agg.get) if agg else None
    f.__name__ = f"5倍数最大≤{maxdist*100:.0f}%"
    return f


def r_nearest5(mindist):
    """距现价至少 mindist 的最近一个 5 的倍数（不看 OI）。"""
    def f(snap, spot, kind, obs):
        ks = sorted({c.strike for c in snap.contracts
                     if c.kind == kind and abs(c.strike % 5) < 1e-9})
        if kind == "P":
            c = [k for k in ks if k <= spot * (1 - mindist)]
            return max(c) if c else None
        c = [k for k in ks if k >= spot * (1 + mindist)]
        return min(c) if c else None
    f.__name__ = f"最近5倍数≥{mindist*100:.0f}%"
    return f


def r_fixed(dist):
    """对照组：不看 OI，只按距现价百分比取最接近的行权价。"""
    def f(snap, spot, kind, obs):
        tgt = spot * (1 - dist) if kind == "P" else spot * (1 + dist)
        ks = sorted({c.strike for c in snap.contracts if c.kind == kind})
        if not ks:
            return None
        return min(ks, key=lambda x: abs(x - tgt))
    f.__name__ = f"[对照]固定{dist*100:.0f}%"
    return f


def r_pick():
    """定版规则（用户 2026-09-02 第一步产物）：见 gamma.pick_sell_wall。"""
    from undertow.analyze.gamma import pick_sell_wall

    def f(snap, spot, kind, obs):
        r = pick_sell_wall(snap, obs, spot, kind)
        return r["strike"] if r else None
    f.__name__ = "★定版pick_sell_wall"
    return f


def evaluate(snaps, closes, dates, kind, rule, dte):
    rows = []
    for sess in sorted(snaps):
        snap = snaps[sess]
        prior = [x for x in dates if x < sess.isoformat()]
        if not prior:
            continue
        spot = closes[prior[-1]]
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        wk = rule(snap, spot, kind, obs)
        if wk is None:
            continue
        # ⚠️ 第一步只看【当日收盘】是否越过用 D−1 结构算出的墙。
        # 不涉及到期日/价差/持仓 —— 那些属于第二、三步（用户 2026-09-02：
        # 「第一步是确定墙，你看当日现价收盘是否破墙就行了，你算什么价差」）。
        se = closes.get(sess.isoformat())
        if se is None:
            continue
        rows.append(dict(d=sess, wall=wk, spot=spot, settle=se,
                         broke=(se > wk) if kind == "C" else (se < wk),
                         buf=abs(wk / spot - 1) * 100))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV,GLD")
    ap.add_argument("--dte", default="4,11", help="已废弃：第一步不看到期")
    a = ap.parse_args()
    lo, hi = (int(x) for x in a.dte.split(","))
    rules = [r_pick(), r_struct(0.25), r_struct(0.15),
             r_round5(0.25), r_nearest5(0.05),
             r_fixed(0.03), r_fixed(0.05), r_fixed(0.08), r_fixed(0.12)]
    for sym in a.symbol.split(","):
        snaps, closes, dates = load(sym)
        print(f"\n{'='*96}\n{sym}　到期 {lo}~{hi} 天　"
              f"{len(snaps)} 个可交易日　破墙=到期收盘越过\n{'='*96}")
        for kind, kl in (("P", "put 侧（下方支撑）"), ("C", "call 侧（上方阻力）")):
            print(f"\n  {kl}")
            print(f"    {'规则':<18}{'笔数':>5}{'破墙':>6}{'破墙率':>8}"
                  f"{'均缓冲':>8}{'墙位分布':>28}")
            for rule in rules:
                rows = evaluate(snaps, closes, dates, kind, rule, (lo, hi))
                if len(rows) < 5:
                    continue
                nb = sum(1 for r in rows if r["broke"])
                walls = sorted({r["wall"] for r in rows})
                ws = ",".join(f"{w:g}" for w in walls[:6]) + ("…" if len(walls) > 6 else "")
                print(f"    {rule.__name__:<18}{len(rows):>5}{nb:>6}{nb/len(rows):>7.0%}"
                      f"{statistics.mean(r['buf'] for r in rows):>7.1f}%{ws:>28}")


if __name__ == "__main__":
    main()
