"""第二步全网格落盘：品种 × 侧 × 偏移 × 宽度 × DTE，逐笔存 JSONL。

用户 2026-09-02：「你要都测试一遍，后续第三步还要重新算。」
第三步（提前平仓、换墙）要在同一批开仓上重算出场，所以这里把
**开仓与持有到期的结果**一次性落盘，第三步只需替换出场逻辑，
不必也不应该重跑选墙与建仓 —— 否则两步的样本会对不上。

每行一笔：
  sym side off width dte  开仓日 到期日 卖腿 买腿 墙位 决策价 到期收盘
  缓冲% 权利金 宽度$ 占用 手续费 内在价值 损益 是否破卖腿 选墙规则

口径与 2.1/2.2/2.3 完全一致（见 docs/wall_spread_3steps.md）。
"""
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import wall_spread as ws                  # noqa: E402
from undertow.analyze.gamma import pick_sell_wall               # noqa: E402
import importlib.util                                            # noqa: E402
_s2 = importlib.util.spec_from_file_location(
    "s2", str(pathlib.Path(__file__).parent / "step2_spread.py"))
_m = importlib.util.module_from_spec(_s2)
_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_s2.loader.exec_module(_m)
sys.argv = _argv

OFFSETS = (-3, -2, -1, 0, 1, 2, 3)
WIDTHS = (1, 2, 3, 5)
MAX_DTE = 45
FEE = 3.20
OUT = pathlib.Path("data/backtest/step2_grid.jsonl")


def run(sym, kind):
    snaps, closes, dates = _m.load(sym)
    rows = []
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
            se = closes.get(exp.isoformat())
            if se is None:
                continue
            i = ks.index(W)
            for off in OFFSETS:
                si = i + off if kind == "P" else i - off
                if si < 0 or si >= len(ks):
                    continue
                S = ks[si]
                if (kind == "P" and S >= spot) or (kind == "C" and S <= spot):
                    continue                      # 不卖实值
                for wn in WIDTHS:
                    bi = si - wn if kind == "P" else si + wn
                    if bi < 0 or bi >= len(ks):
                        continue
                    B = ks[bi]
                    credit = ws._fill(legs[exp][S], legs[exp][B])
                    width = abs(B - S) * 100
                    if width <= 0:
                        continue
                    itr = (max(0.0, min(S - se, width / 100)) if kind == "P"
                           else max(0.0, min(se - S, width / 100))) * 100
                    rows.append({
                        "sym": sym, "side": kind, "off": off, "width_n": wn,
                        "dte": dte, "open": sess.isoformat(),
                        "exp": exp.isoformat(), "sell": S, "buy": B, "wall": W,
                        "rule": rule, "spot": round(spot, 4),
                        "settle": round(se, 4),
                        "buf_pct": round(abs(S / spot - 1) * 100, 3),
                        "credit": round(credit, 2), "width": round(width, 2),
                        "occ": round(width - credit, 2), "fee": FEE,
                        "itr": round(itr, 2),
                        "pnl": round(credit - itr - FEE, 2),
                        "broke": bool(se < S) if kind == "P" else bool(se > S),
                    })
    return rows


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
                print(f"  {sym} {kind}: {len(rows):,} 笔")
    print(f"\n共 {total:,} 笔 → {OUT}（{OUT.stat().st_size/1e6:.1f} MB）")
    print(f"偏移 {OFFSETS}　宽度 {WIDTHS}　DTE 1~{MAX_DTE}")


if __name__ == "__main__":
    main()
