"""Supertrend 作为**过滤器**而非独立信号 —— 用户 2026-09-04 提出。

用户问：「是否可以把超级趋势跟踪结合我们的模型系统，趋势作为辅助。」

这个用法与「进方向投票」是两件事，先例是 vol_surface_as_filter：
波动率面单独看没有独立预测力，但作为资金流的过滤器有价值
（金银上同向 87% vs 反向 71%）。所以检验方式照搬那次：
**看我们自己的信号在「趋势同向 / 反向」两组的准确率差异。**

口径（与 vol_vs_flow.py 一致，便于对照）：
· 可交易日 D 由 captured_at 推导，每对相邻可交易日跑一次 analyze_flow
· 资金流方向 = tradeable_info(side, ratio)
· **Supertrend 方向取 D−1 收盘的值** —— 决策发生在 D 日盘前，
  那时能看到的最后一根完整日线是 D−1。取 D 当根就是未来函数。
· 收益基准 C[D−1]；D+0 = C[D]/C[D−1]−1；D+1 = C[D+1]/C[D−1]−1
· **按品种拆开报告**，不做跨品种汇总（教训见 validation.py）
"""
import pathlib, sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze.flow import analyze_flow, tradeable_info, detect_strong_signal  # noqa
from undertow.analyze.ta import supertrend as ST                                      # noqa
from undertow.cli import snapshot_from_payload                                        # noqa
from undertow.collect.longbridge_kline import fetch_bars                              # noqa
from undertow.collect.store import SnapshotStore                                      # noqa

INST = {"GLD": "gold", "SLV": "silver", "USO": "wti", "QQQ": "qqq"}


def st_by_date(sym):
    """每个交易日收盘时的 Supertrend 方向。"""
    b = fetch_bars(f"{sym}.US", period="day", count=400)
    h = [x["high"] for x in b]; l = [x["low"] for x in b]; c = [x["close"] for x in b]
    _, _, tr = ST.supertrend(h, l, c)
    return {str(b[i]["ts"])[:10]: tr[i] for i in range(len(b))}


def load(sym):
    closes = {str(b["ts"])[:10]: b["close"]
              for b in fetch_bars(f"{sym}.US", period="day", count=400)}
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
    for sym in INST:
        snaps, closes, dates = load(sym)
        stmap = st_by_date(sym)
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
            # ⚠️ 取 D−1 的 Supertrend，不是 D 当根
            trend = stmap.get(prior[-1])
            rows.append(dict(sym=sym, date=k, flow=ti.get("side") or "-",
                             ratio=ti.get("ratio") or 0.0,
                             sig_dir=sg.direction if sg else "",
                             sig_lv=sg.level if sg else "",
                             trend=trend, r0=r0, r1=r1))
    return rows


def hit(direction, r):
    if r is None or direction not in ("看涨", "看跌"):
        return None
    return (r > 0) if direction == "看涨" else (r < 0)


def main():
    rows = build()
    print(f"共 {len(rows)} 对相邻快照\n")
    for sym in INST:
        rs = [r for r in rows if r["sym"] == sym and r["flow"] in ("看涨", "看跌")
              and r["trend"] in (1, -1)]
        if len(rs) < 10:
            print(f"══ {sym} ══  n={len(rs)} 样本不足\n")
            continue
        print(f"══ {sym} ══  资金流有方向且趋势有值的样本 n={len(rs)}")
        for hz, lab in ((0, "D+0"), (1, "D+1")):
            key = "r0" if hz == 0 else "r1"
            same = [r for r in rs
                    if (r["flow"] == "看涨") == (r["trend"] == 1)]
            diff = [r for r in rs
                    if (r["flow"] == "看涨") != (r["trend"] == 1)]
            line = f"  {lab}  "
            for grp, gl in ((rs, "全部"), (same, "趋势同向"), (diff, "趋势反向")):
                v = [hit(r["flow"], r[key]) for r in grp]
                v = [x for x in v if x is not None]
                line += (f"{gl} {sum(v)}/{len(v)}={sum(v)/len(v)*100:>3.0f}%   "
                         if v else f"{gl} n/a   ")
            print(line)
        print()


if __name__ == "__main__":
    main()
