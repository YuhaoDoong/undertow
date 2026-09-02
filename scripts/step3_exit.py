"""第三步：出场规则对比 —— 持有到期 vs 换墙平仓 vs 破墙平仓 vs 信号平仓。

用户 2026-09-02：「测试是否应该提前平仓，尤其是换墙和破墙的时候」。

开仓完全沿用第一步/第二步的口径（选墙、偏移、宽度、DTE 不变），
只替换**出场**逻辑，这样各规则之间可比、也与第二步可比。

逐日推进：对每个持仓，从开仓次日到到期日，每个可交易日都
· 用当天快照取卖腿/买腿的真实报价，算【平仓成本】
  （平仓 = 买回卖腿 + 卖出买腿，按中价往不利方向让 25%）
· 按规则判断是否平仓；平了就记损益并停止
未触发规则则持有到期，损益 = 权利金 − 内在价值 − 手续费。

⚠️ 提前平仓要付第二次手续费（4 腿再收一次），已计入。
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

FEE = 3.20            # 每次进出各 3.20（4 腿 × $0.80）
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
    """平仓成本（正数=要付出）。买回卖腿吃 ask 方向，卖出买腿吃 bid 方向。"""
    smid = (sc.bid + sc.ask) / 2
    bmid = (bc.bid + bc.ask) / 2
    mid = (smid - bmid) * 100
    worst = (sc.ask - bc.bid) * 100
    return mid + (worst - mid) * give


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

        # ── 逐日推进 ──
        exit_day, exit_cost, why = None, None, "持有到期"
        for sess2 in sess_list[i + 1:]:
            if sess2 >= target:
                break
            k2 = sess2.isoformat()
            px = closes.get(k2)
            if px is None:
                continue
            snap2 = snaps[sess2]
            sc = quote(snap2, kind, S, target)
            bc = quote(snap2, kind, B, target)
            if sc is None or bc is None:
                continue
            broke = (px < S) if kind == "P" else (px > S)
            prior2 = [x for x in dates if x < k2]
            spot2 = closes[prior2[-1]] if prior2 else px
            obs2 = datetime.strptime(prior2[-1], "%Y-%m-%d").date() if prior2 else sess2
            r2 = pick_sell_wall(snap2, obs2, spot2, kind)
            wall_moved = (r2 is None) or (r2["strike"] != W0)
            cost = close_cost(sc, bc)
            hit = False
            if rule == "hold":
                hit = False
            elif rule == "break":
                hit = broke
            elif rule == "wallmove":
                hit = wall_moved
            elif rule == "break_and_move":
                hit = broke and wall_moved
            elif rule == "profit50":
                hit = cost <= credit * 0.5
            elif rule == "break_or_profit50":
                hit = broke or cost <= credit * 0.5
            if hit:
                exit_day, exit_cost = sess2, cost
                why = {"break": "破卖腿", "wallmove": "换墙",
                       "break_and_move": "破墙且换墙",
                       "profit50": "浮盈50%",
                       "break_or_profit50": "破墙或浮盈50%"}[rule]
                break

        se = closes.get(target.isoformat())
        if exit_day is None:
            if se is None:
                continue
            itr = (max(0.0, min(S - se, width / 100)) if kind == "P"
                   else max(0.0, min(se - S, width / 100))) * 100
            pnl = credit - itr - FEE
            held = (target - sess).days
        else:
            pnl = credit - exit_cost - FEE * 2      # 提前平要付两次手续费
            held = (exit_day - sess).days
        out.append(dict(d=sess, S=S, B=B, W=W0, exp=target, credit=credit,
                        width=width, occ=width - credit, pnl=pnl, held=held,
                        why=why, early=exit_day is not None,
                        settle=se, broke=(se < S) if (kind == "P" and se) else
                        ((se > S) if se else None)))
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
    print(f"{'DTE':>4}{'规则':>14}{'笔':>4}{'提前平':>7}{'均持有':>7}"
          f"{'总损益$':>9}{'均损益$':>8}{'最差$':>8}")
    for dte in [int(x) for x in a.dtes.split(",")]:
        for rule in rules:
            rows = simulate(a.symbol, a.side, a.off, a.width, dte, rule)
            if len(rows) < 3:
                continue
            ne = sum(1 for r in rows if r["early"])
            print(f"{dte:>4}{names[rule]:>14}{len(rows):>4}{ne/len(rows):>6.0%}"
                  f"{statistics.mean(r['held'] for r in rows):>6.1f}天"
                  f"{sum(r['pnl'] for r in rows):>+9.0f}"
                  f"{statistics.mean(r['pnl'] for r in rows):>+8.1f}"
                  f"{min(r['pnl'] for r in rows):>+8.0f}")
        print()


if __name__ == "__main__":
    main()
