"""第四步：开仓 DTE × 持有天数 的完整网格。

用户 2026-09-02：「交易 DTE 7 的，但是我们第四天平仓，你都统计下来，
建立一个大的数据表」。

此前把「开仓选多长的到期」和「实际持有多久」绑死了（只测过持有到期
与破墙即平）。这两个是独立变量：
· 开仓 DTE 决定权利金水平与 theta 曲线上的位置
· 持有天数决定实际吃到多少时间价值、暴露多久

逐日推进用当天快照的真实报价算平仓成本
（买回卖腿吃 ask 方向、卖出买腿吃 bid 方向，中价让 25%），
提前平仓计两次手续费（$3.20 × 2），持有到期只计一次。

落盘 data/backtest/step4_holding.jsonl，一行一个
(开仓日, DTE, 持有天数, 宽度, 位置, 侧) 组合。
"""
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                  # noqa: E402
from undertow.analyze.gamma import pick_sell_wall               # noqa: E402
from undertow.cli import snapshot_from_payload                  # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars        # noqa: E402
from undertow.collect.store import SnapshotStore                # noqa: E402

FEE = 3.20
INST = {"SLV": "silver", "GLD": "gold"}
OFFSETS = (0, 1)
WIDTHS = (2, 3, 5)
MAX_DTE = 15
OUT = pathlib.Path("data/backtest/step4_holding.jsonl")


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


def run(sym, kind):
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
        W, rule = r["strike"], r["rule"]
        legs = defaultdict(dict)
        for c in snap.contracts:
            if c.kind == kind and c.bid is not None and c.ask:
                legs[c.expiry][c.strike] = c
        for exp in sorted(legs):
            dte = (exp - sess).days
            if not (1 <= dte <= MAX_DTE):
                continue
            ks = sorted(legs[exp])
            if W not in ks:
                continue
            i0 = ks.index(W)
            for off in OFFSETS:
                si = i0 + off if kind == "P" else i0 - off
                if si < 0 or si >= len(ks):
                    continue
                S = ks[si]
                if (kind == "P" and S >= spot) or (kind == "C" and S <= spot):
                    continue
                for wn in WIDTHS:
                    bi = si - wn if kind == "P" else si + wn
                    if bi < 0 or bi >= len(ks):
                        continue
                    B = ks[bi]
                    credit = ws._fill(legs[exp][S], legs[exp][B])
                    width = abs(B - S) * 100
                    if width <= 0:
                        continue
                    base = dict(sym=sym, side=kind, off=off, width_n=wn, dte=dte,
                                open=sess.isoformat(), exp=exp.isoformat(),
                                sell=S, buy=B, wall=W, rule=rule,
                                spot=round(spot, 4),
                                buf_pct=round(abs(S / spot - 1) * 100, 3),
                                credit=round(credit, 2), width=round(width, 2),
                                occ=round(width - credit, 2))
                    # 持有 1..dte 天后平仓；hold==dte 即持有到期
                    for j, sess2 in enumerate(sess_list[i + 1:], start=1):
                        hold = (sess2 - sess).days
                        if hold > dte:
                            break
                        px = closes.get(sess2.isoformat())
                        if px is None:
                            continue
                        broke = (px < S) if kind == "P" else (px > S)
                        if sess2 >= exp:
                            itr = (max(0.0, min(S - px, width / 100)) if kind == "P"
                                   else max(0.0, min(px - S, width / 100))) * 100
                            pnl, mode = credit - itr - FEE, "到期"
                        else:
                            sc = quote(snaps[sess2], kind, S, exp)
                            bc = quote(snaps[sess2], kind, B, exp)
                            if sc is None or bc is None:
                                continue
                            cost = ws.close_cost(sc, bc)
                            pnl, mode = credit - cost - FEE * 2, "提前平"
                        out.append({**base, "hold": hold,
                                    "close_day": sess2.isoformat(),
                                    "close_px": round(px, 4), "mode": mode,
                                    "pnl": round(pnl, 2), "broke": bool(broke)})
    return out


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    syms = sys.argv[1].split(",") if len(sys.argv) > 1 else ["SLV"]
    total = 0
    with OUT.open("w", encoding="utf-8") as f:
        for sym in syms:
            for kind in ("P", "C"):
                rows = run(sym, kind)
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                total += len(rows)
                print(f"  {sym} {kind}: {len(rows):,} 行")
    print(f"\n共 {total:,} 行 → {OUT}（{OUT.stat().st_size/1e6:.1f} MB）")
    print(f"DTE 1~{MAX_DTE} × 持有 1~DTE × 宽度 {WIDTHS} × 位置 {OFFSETS}")


if __name__ == "__main__":
    main()
