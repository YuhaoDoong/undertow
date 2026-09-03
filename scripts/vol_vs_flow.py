"""波动率面 vs 资金流：冲突时谁更准（用户 2026-09-03 提出）

用户的假设：波动率面看的是【昨天】的 IV 反应（事后追认），资金流看的是
收盘后的持仓变化（事前布局）。两者冲突时资金流应更接近"接下来"。
2026-09-02 GLD 就是一例：资金流看涨 20.1×、波动率面「下行动能获确认」，
结果 +2.5%，资金流对。本脚本把这个问题在全部历史上跑一遍。

口径：
· 可交易日 D 由 captured_at 推导；每对相邻可交易日跑一次 analyze_flow
· 资金流方向 = tradeable_info(side, ratio)；强信号 = detect_strong_signal
· 波动率面方向 = 把 _vol_verdict 的文本映射成 偏多/偏空/中性
· 收益基准 = C[D−1]（决策价）；D+0 = C[D]/C[D−1]−1；D+1 = C[D+1]/C[D−1]−1
· 命中 = 收益符号与方向一致
"""
import pathlib, sys, statistics
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze.flow import analyze_flow, detect_strong_signal, tradeable_info  # noqa
from undertow.cli import snapshot_from_payload                                       # noqa
from undertow.collect.longbridge_kline import fetch_bars                             # noqa
from undertow.collect.store import SnapshotStore                                     # noqa

INST = {"GLD": "gold", "SLV": "silver", "USO": "wti", "QQQ": "qqq"}
BULL = ("涨势获期权端确认", "偏斜收敛 → 下行担忧减退", "跌势未获期权端追认")
BEAR = ("下行动能获确认", "保护需求上升", "put 偏斜走陡", "后续动力存疑")


def vol_dir(verdict: str) -> str:
    if any(k in verdict for k in BULL):
        return "偏多"
    if any(k in verdict for k in BEAR):
        return "偏空"
    return "中性"


def load(sym):
    closes = {str(b["ts"])[:10]: b["close"] for b in fetch_bars(f"{sym}.US", period="day", count=260)}
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


def build():
    rows = []
    for sym, inst in INST.items():
        snaps, closes, dates = load(sym)
        ss = sorted(snaps)
        for i in range(1, len(ss)):
            p, d = ss[i - 1], ss[i]
            k = d.isoformat()
            prior = [x for x in dates if x < k]
            if not prior or k not in closes:
                continue
            base = closes[prior[-1]]
            nxt = [x for x in dates if x > k]
            r0 = (closes[k] / base - 1) * 100
            r1 = (closes[nxt[0]] / base - 1) * 100 if nxt else None
            try:
                fa = analyze_flow(snaps[p], snaps[d], today=d,
                                  prev_date=p.isoformat(), curr_date=k)
            except Exception:
                continue
            ti = tradeable_info(fa)
            sg = detect_strong_signal(fa)
            v = fa.vol
            vd = vol_dir(v.verdict) if (v and v.prev) else "无"
            rows.append(dict(sym=sym, date=k,
                             flow=ti.get("side") or "-", ratio=ti.get("ratio") or 0.0,
                             decidable=bool(ti.get("decidable")),
                             sig=(sg.level + sg.direction) if sg else "",
                             sig_dir=sg.direction if sg else "", sig_lv=sg.level if sg else "",
                             vol=vd, verdict=(v.verdict if v else ""), r0=r0, r1=r1))
    return rows


def hit(direction, r):
    if r is None or direction not in ("看涨", "看跌", "偏多", "偏空"):
        return None
    up = direction in ("看涨", "偏多")
    return (r > 0) if up else (r < 0)


def stat(rows, key_dir, lab):
    for h, hl in ((0, "D+0"), (1, "D+1")):
        v = [(hit(r[key_dir], r["r0" if h == 0 else "r1"]),
              (r["r0" if h == 0 else "r1"]) * (1 if r[key_dir] in ("看涨", "偏多") else -1))
             for r in rows if hit(r[key_dir], r["r0" if h == 0 else "r1"]) is not None]
        if len(v) < 3:
            print(f"    {lab} {hl}: n={len(v)} 样本不足")
            continue
        w = sum(1 for x, _ in v if x)
        print(f"    {lab} {hl}: {w}/{len(v)} = {w/len(v):.0%}  有向均值 {statistics.mean(m for _, m in v):+.2f}%")


def main():
    rows = build()
    both = [r for r in rows if r["flow"] in ("看涨", "看跌") and r["vol"] in ("偏多", "偏空")]
    agree = [r for r in both if (r["flow"] == "看涨") == (r["vol"] == "偏多")]
    conflict = [r for r in both if (r["flow"] == "看涨") != (r["vol"] == "偏多")]
    print(f"全部快照对 {len(rows)}　资金流有方向 {sum(1 for r in rows if r['flow'] in ('看涨','看跌'))}"
          f"　波动率面有方向 {sum(1 for r in rows if r['vol'] in ('偏多','偏空'))}"
          f"　两者都有方向 {len(both)}\n")
    print(f"═══ 一致（{len(agree)} 对）═══")
    stat(agree, "flow", "共同方向")
    print(f"\n═══ 冲突（{len(conflict)} 对）—— 谁对？═══")
    stat(conflict, "flow", "资金流")
    stat(conflict, "vol", "波动率面")
    print("\n  冲突明细：")
    for r in sorted(conflict, key=lambda x: x["date"]):
        f0 = hit(r["flow"], r["r0"]); f1 = hit(r["flow"], r["r1"])
        print(f"    {r['date']} {r['sym']:<4} 资金流{r['flow']}{r['ratio']:.1f}× vs 波面{r['vol']}"
              f"  D+0 {r['r0']:+.2f}%{'✓' if f0 else '✗'}  D+1 {(f'{r[chr(114)+chr(49)]:+.2f}%' if r['r1'] is not None else '-')}{'✓' if f1 else ('✗' if f1 is not None else '')}"
              f"  ← 资金流{'对' if f0 else '错'}")

    print(f"\n═══ 强信号按方向拆开（用户：强/极强看涨准确率其实很高）═══")
    for lv in ("极强", "强"):
        for dr in ("看涨", "看跌"):
            g = [r for r in rows if r["sig_lv"] == lv and r["sig_dir"] == dr]
            if not g:
                continue
            print(f"  {lv}{dr}（{len(g)} 次）")
            stat(g, "sig_dir", "   ")

    print(f"\n═══ 资金流单独（按比值分档，不看波动率面）═══")
    for lo, hi in ((2, 5), (5, 10), (10, 20), (20, 999)):
        g = [r for r in rows if r["flow"] in ("看涨", "看跌") and lo <= r["ratio"] < hi]
        if len(g) < 3:
            continue
        print(f"  比值 {lo}~{hi if hi < 999 else '∞'}×（{len(g)} 次）")
        stat(g, "flow", "   ")


if __name__ == "__main__":
    main()
