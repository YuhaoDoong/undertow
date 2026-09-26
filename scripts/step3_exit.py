"""第三步：出场规则对比 —— 持有到期 vs 换墙平仓 vs 破墙平仓 vs 信号平仓。

用户 2026-09-02：「测试是否应该提前平仓，尤其是换墙和破墙的时候」。

开仓完全沿用第一步/第二步的口径（选墙、偏移、宽度、DTE 不变），
只替换**出场**逻辑，这样各规则之间可比、也与第二步可比。

2026-09-25 更正：旧版使用当天收盘触发、当天盘前报价退出，含前视。
旧版“减损76%”作废。现在每天先观察快照、后观察收盘，收盘触发最早用
后续快照估价；首日收盘也检查，触发后缺报价保留未知，不折为持有到期。
所有收益仍是快照报价模型，未经可成交盘口验证，不能作为启用策略的证据。
费用统一复用 wall_spread.FEE_PER_TRADE 的往返预算，提前退出不重复乘二。
"""
import argparse
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                  # noqa: E402
from undertow.analyze.spread_exit import evaluate_exit          # noqa: E402
from undertow.analyze.gamma import pick_sell_wall               # noqa: E402
from undertow.cli import snapshot_from_payload                  # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars        # noqa: E402
from undertow.collect.store import SnapshotStore                # noqa: E402

FEE = ws.FEE_PER_TRADE  # 往返费用预算；持有到期也保守使用同一预算
INST = {"SLV": "silver", "GLD": "gold"}


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


def quote(snap, kind, strike, expiry):
    for c in snap.contracts:
        if (c.kind == kind and c.strike == strike and c.expiry == expiry
                and c.bid is not None and c.ask):
            return c
    return None


def close_cost(sc, bc, give=0.25):
    """兼容旧研究入口；计算只在分析层有一份实现。"""
    return ws.close_cost(sc, bc, give)


def simulate(sym, kind, off, width_n, dte_target, rule):
    snaps, closes, dates = load(sym)
    sess_list = sorted(snaps)
    out = []
    for i, sess in enumerate(sess_list):
        snap = snaps[sess]
        prior = [x for x in dates if x < sess.isoformat()]
        if not prior:
            continue
        spot = closes[prior[-1]]
        obs = datetime.strptime(prior[-1], "%Y-%m-%d").date()
        r = pick_sell_wall(snap, obs, spot, kind)
        if r is None:
            continue
        W0 = r["strike"]
        legs = defaultdict(dict)
        for c in snap.contracts:
            if c.kind == kind and c.bid is not None and c.ask:
                legs[c.expiry][c.strike] = c
        target = None
        for exp in sorted(legs):
            if (exp - sess).days == dte_target and W0 in legs[exp]:
                target = exp
                break
        if target is None:
            continue
        ks = sorted(legs[target])
        si = ks.index(W0) + (off if kind == "P" else -off)
        if si < 0 or si >= len(ks):
            continue
        S = ks[si]
        if (kind == "P" and S >= spot) or (kind == "C" and S <= spot):
            continue
        bi = si - width_n if kind == "P" else si + width_n
        if bi < 0 or bi >= len(ks):
            continue
        B = ks[bi]
        credit = ws._fill(legs[target][S], legs[target][B])
        width = abs(B - S) * 100
        if width <= 0:
            continue

        result = evaluate_exit(
            kind=kind, sell=S, buy=B, expiry=target, entry_day=sess,
            credit=credit, initial_wall=W0, snapshots=snaps,
            closes={datetime.strptime(d, "%Y-%m-%d").date(): px for d, px in closes.items()},
            rule=rule)
        se = closes.get(target.isoformat())
        out.append(dict(d=sess, S=S, B=B, W=W0, exp=target, credit=credit,
                        width=width, occ=width - credit, **result,
                        settle=se, broke=(se < S if kind == "P" else se > S)
                        if se is not None else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SLV")
    ap.add_argument("--side", default="P")
    ap.add_argument("--off", type=int, default=0)
    ap.add_argument("--width", type=int, default=3)
    ap.add_argument("--dtes", default="4,7,9,14")
    a = ap.parse_args()
    rules = ["hold", "break", "wallmove", "break_and_move",
             "profit50", "break_or_profit50"]
    names = {"hold": "持有到期", "break": "破卖腿即平", "wallmove": "换墙即平",
             "break_and_move": "破墙且换墙", "profit50": "浮盈50%平",
             "break_or_profit50": "破墙或浮盈50%"}
    lab = "墙上" if a.off == 0 else (f"墙内{a.off}档" if a.off > 0 else f"墙外{-a.off}档")
    print(f"{a.symbol} {'put' if a.side=='P' else 'call'} 侧　{lab}　"
          f"宽{a.width}档　出场规则对比\n")
    print("⚠ 快照估价模型，未验证可成交；旧版减损76%作废。未知退出不计入均值，必须同时看覆盖数。")
    print(f"{'DTE':>4}{'规则':>14}{'已估/候选':>10}{'提前平':>7}{'均持有':>7}"
          f"{'总损益$':>9}{'均损益$':>8}{'最差$':>8}")
    for dte in [int(x) for x in a.dtes.split(",")]:
        for rule in rules:
            rows = simulate(a.symbol, a.side, a.off, a.width, dte, rule)
            total = len(rows)
            rows = [r for r in rows if r["pnl"] is not None]
            if not rows:
                print(f"{dte:>4}{names[rule]:>14}  0/{total}  无可估计结果")
                continue
            ne = sum(1 for r in rows if r["early"])
            coverage = f"{len(rows)}/{total}"
            print(f"{dte:>4}{names[rule]:>14}{coverage:>10}{ne/len(rows):>6.0%}"
                  f"{statistics.mean(r['held'] for r in rows):>6.1f}天"
                  f"{sum(r['pnl'] for r in rows):>+9.0f}"
                  f"{statistics.mean(r['pnl'] for r in rows):>+8.1f}"
                  f"{min(r['pnl'] for r in rows):>+8.0f}")
        print()


if __name__ == "__main__":
    main()
