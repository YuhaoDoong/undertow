"""第二步：在第一步定下的墙位上，测「到期日 × 价差宽度」的收益结构。

用户 2026-09-02 的三步法第二步。要点：
· 卖腿固定 = gamma.pick_sell_wall()（第一步定版规则，门槛 3%）
· 买腿 = 卖腿往虚值方向推 N 档（用期权链上实际存在的档位，不是美元数）
· 开仓依据 = D−1 的期权结构与收盘价；判定 = 到期日当天收盘
· **全部持有到期，不考虑提前平仓**（那是第三步）
· 成交按组合单中价让 25% 点差（ws._fill）；手续费 $0.80/腿 × 4 腿

到期日不能脱离价差单独定（用户原话），所以这里 DTE 步长为 1 全测。
"""
import argparse
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                  # noqa: E402
from undertow.analyze.gamma import pick_sell_wall               # noqa: E402
from undertow.cli import snapshot_from_payload                  # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars        # noqa: E402
from undertow.collect.store import SnapshotStore                # noqa: E402

INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "USO": "wti"}
FEE_LEGS = 4


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


def build(sym, kind, max_dte=45, widths=(1, 2, 3, 5)):
    """返回 rows：每条 = 一个 (开仓日, DTE, 宽度档数) 组合。"""
    snaps, closes, dates = load(sym)
    out = []
    for sess in sorted(snaps):
        snap = snaps[sess]
        prior = [x for x in dates if x < sess.isoformat()]
        if not prior:
            continue
        spot = closes[prior[-1]]
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        r = pick_sell_wall(snap, obs, spot, kind)
        if r is None:
            continue
        W = r["strike"]
        legs = defaultdict(dict)
        for c in snap.contracts:
            if c.kind == kind and c.bid is not None and c.ask:
                legs[c.expiry][c.strike] = c
        for exp in sorted(legs):
            dte = (exp - sess).days
            if not (1 <= dte <= max_dte):
                continue
            ks = sorted(legs[exp])
            if W not in ks:
                continue
            se = closes.get(exp.isoformat())
            if se is None:
                continue
            i = ks.index(W)
            for n in widths:
                j = i - n if kind == "P" else i + n
                if j < 0 or j >= len(ks):
                    continue
                B = ks[j]
                credit = ws._fill(legs[exp][W], legs[exp][B])
                width = abs(B - W) * 100
                if width <= 0:
                    continue
                fee = ws.FEE_PER_LEG * FEE_LEGS
                itr = (max(0.0, min(W - se, width / 100)) if kind == "P"
                       else max(0.0, min(se - W, width / 100))) * 100
                out.append(dict(
                    d=sess, dte=dte, n=n, W=W, B=B, exp=exp, se=se,
                    spot=spot, buf=r["buf_pct"], rule=r["rule"],
                    credit=credit, width=width, fee=fee,
                    itr=itr, pnl=credit - itr - fee,
                    occ=width - credit,
                    broke=(se < W) if kind == "P" else (se > W)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--side", default="P", choices=["P", "C"])
    ap.add_argument("--metric", default="pnl",
                    choices=["pnl", "credit", "feeshare", "roi", "ann", "n"])
    ap.add_argument("--max-dte", type=int, default=45)
    a = ap.parse_args()
    widths = (1, 2, 3, 5)
    rows = build(a.symbol, a.side, a.max_dte, widths)
    if not rows:
        print("无样本")
        return
    print(f"{a.symbol} {'put' if a.side=='P' else 'call'} 侧　"
          f"卖腿=第一步选出的墙　买腿=往外 N 档　全部持有到期\n")
    lbl = {"pnl": "总损益$", "credit": "均权利金$", "roi": "均单笔ROI",
           "ann": "年化ROI（单笔ROI×365/DTE）",
           "feeshare": "手续费占权利金", "n": "笔数"}[a.metric]
    print(f"指标：{lbl}　（手续费 ${ws.FEE_PER_LEG}/腿 × {FEE_LEGS} 腿 "
          f"= ${ws.FEE_PER_LEG*FEE_LEGS:.2f}/笔）\n")
    by = defaultdict(list)
    for r in rows:
        by[(r["dte"], r["n"])].append(r)
    print(f"{'DTE':>4}" + "".join(f"{'宽'+str(n)+'档':>12}" for n in widths)
          + f"{'  破墙率':>9}")
    for dte in range(1, a.max_dte + 1):
        cells = []
        any_ = False
        for n in widths:
            g = by.get((dte, n), [])
            if len(g) < 3:
                cells.append(f"{'-':>12}")
                continue
            any_ = True
            if a.metric == "pnl":
                v = f"{sum(x['pnl'] for x in g):>+8.0f}({len(g)})"
            elif a.metric == "credit":
                v = f"{statistics.mean(x['credit'] for x in g):>8.1f}({len(g)})"
            elif a.metric == "roi":
                v = f"{statistics.mean(x['pnl']/x['occ'] for x in g if x['occ']>0):>+7.1%}"
            elif a.metric == "ann":
                # 年化 = 单笔 ROI × (365/DTE)，让不同持有期可比
                vals = [x['pnl']/x['occ']*365/x['dte'] for x in g if x['occ'] > 0]
                v = f"{statistics.mean(vals):>+7.0%}"
            elif a.metric == "feeshare":
                # ⚠️ 权利金可能≈0 甚至为负（深虚 + 让点差），除法会溢出。
                # 这类笔本身就不可交易，按"手续费吃光"计，并单独报告其占比。
                ok = [x for x in g if x['credit'] > 1.0]
                if not ok:
                    v = f"{'全不可交易':>7}"
                else:
                    fs = statistics.mean(x['fee']/x['credit'] for x in ok)
                    dead = len(g) - len(ok)
                    v = f"{fs:>6.0%}" + (f"+{dead}死" if dead else "")
            else:
                v = f"{len(g):>12}"
            cells.append(f"{v:>12}")
        if not any_:
            continue
        allg = [x for n in widths for x in by.get((dte, n), [])]
        br = sum(1 for x in allg if x["broke"]) / len(allg) if allg else 0
        print(f"{dte:>4}" + "".join(cells) + f"{br:>8.1%}")


if __name__ == "__main__":
    main()
