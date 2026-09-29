"""按到期类型 Q/M/W/D × 剩余天数，描述期权流动性（用户 2026-09-29：「月度/季度流动性应显著高于周度」）。

  python3 scripts/expiry_liquidity.py GLD SLV QQQ SPY

只描述已存快照（认证交易日），不检验、不产生信号：近平值（|Δ| 0.3–0.7）相对买卖价差中位、成交/持仓中位、
单个到期总持仓中位。DTE 以「快照报价日（session 前一交易日）」起算。只读、纯标准库。
"""
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze.expiry_type import classify  # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload  # noqa: E402
from undertow.collect.store import SnapshotStore  # noqa: E402
from undertow.core import market_calendar as mc  # noqa: E402
from undertow.core.config import load_config  # noqa: E402
from undertow.dirledger_cli import session_index  # noqa: E402

BUCKETS = ("0-2", "3-7", "8-21", "22-45")


def bucket(d):
    return "0-2" if d <= 2 else ("3-7" if d <= 7 else ("8-21" if d <= 21 else "22-45"))


def main():
    cfg, s = load_config(), SnapshotStore()
    key_of = {i.options.symbol: i.key for i in cfg.instruments.values() if i.options}
    for sym in sys.argv[1:] or ["GLD", "SLV"]:
        idx = session_index(s, sym)
        spr, vo, oi_tot = defaultdict(list), defaultdict(list), defaultdict(list)
        for sess, f in sorted(idx.items()):
            snap = snapshot_from_payload(s.load("options", sym, f), key_of.get(sym, sym.lower()), sym)
            q = mc.prev_trading_day(sess)
            per = defaultdict(lambda: [0, 0])
            for c in snap.contracts:
                dte = (c.expiry - q).days
                t = classify(c.expiry)["type"]
                if not 0 <= dte <= 45 or t is None:
                    continue
                k = (t, bucket(dte))
                per[(c.expiry, k)][0] += c.open_interest
                per[(c.expiry, k)][1] += c.volume
                if 0.3 <= abs(c.delta) <= 0.7 and c.bid > 0 and c.ask > c.bid:
                    spr[k].append((c.ask - c.bid) / ((c.ask + c.bid) / 2))
            for (e, k), (oi, v) in per.items():
                oi_tot[k].append(oi)
                if oi > 0:
                    vo[k].append(v / oi)
        print(f"== {sym}（{len(idx)} 个认证交易日）类型·DTE：近平值相对价差中位 | 成交/持仓中位 | 单个到期总持仓中位（n）")
        for k in sorted(oi_tot, key=lambda k: ("QMWD".index(k[0]), BUCKETS.index(k[1]))):
            sp = statistics.median(spr[k]) * 100 if spr[k] else float("nan")
            v = statistics.median(vo[k]) if vo[k] else float("nan")
            print(f"  {k[0]} {k[1]:>5s}天  价差 {sp:5.1f}%  成交/持仓 {v:.2f}  持仓 {statistics.median(oi_tot[k]):>10,.0f}  (n={len(oi_tot[k])})")


if __name__ == "__main__":
    main()
