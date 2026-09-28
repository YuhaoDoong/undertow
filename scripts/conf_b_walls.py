"""conf-v1 · B：期权墙 × 价格层（成交密集区 / 同向 OB）→ 墙的可信度（破墙率）。

  python3 scripts/conf_b_walls.py            # 历史快照 2026-06-26 ~ 最近（探索：这批快照在 step8/9 被看过）

⚠️ Codex 018 #1/#10：本脚本在结果成熟之后才组行、事后重新分类，没有盘前冻结的预测记录 —— 只按 --since 切日期
【不是】前瞻台账，所以禁止 --label prospective。真正的前瞻需要先建冻结预测账（墙到期、实际 A 腿关联），再另立版本。
另：按日期独立重抽没有处理三日窗口的跨日重叠；缓冲只分四个宽层、未控制品种/侧别/到期 —— 结果只作历史探索描述。

每个（品种, 认证交易日, 侧）：v5 同口径局部墙 local_wall(max, ±5%, ≤14 天)；spot = 前一交易日收盘；
墙所在价格箱 HVN/LVN/MID（vp-v1 口径，只用 t−1 以前日线）；是否落在同向有效 OB（put↔看涨、call↔看跌，t−500…t−1）；
破墙 = 当日及之后 2 个交易日（3 个收盘）任一收盘越过墙；缓冲 = |spot−K|/ATR 分 4 层。
检验：同层内 Mantel–Haenszel 合并风险差（B1：LVN − HVN；B2：交汇 0 级 − 2 级），按日期聚类 bootstrap 求单侧界。
预登记：docs/prereg/2026-09-28_confluence_v1.md（草案）。只读、纯标准库、不联网。
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import smc  # noqa: E402
from undertow.analyze import volume_profile as vp  # noqa: E402
from undertow.analyze.gamma import local_wall  # noqa: E402

POOL = {"gold": "GLD", "silver": "SLV", "wti": "USO", "qqq": "QQQ", "tlt": "TLT", "spy": "SPY", "iwm": "IWM"}
BUF_EDGES = (0.5, 1.0, 2.0)
HORIZON = 3
OUT = ROOT / "data/backtest/conf_v1"


def _bars(sym):
    rec = json.loads(gzip.decompress((ROOT / f"data/history/inputs/monthly/2026-09/cboehist_{sym}.json.gz").read_bytes()))
    return [b for b in vp.clean(rec["raw"]["data"]["data"]) if b["v"] > 0]


def _layer(buf):
    return sum(buf >= e for e in BUF_EDGES)


def classify(bars, idx, K, kind, atr):
    w = vp.BIN_ATR * atr[idx - 1]
    hist = vp.profile(bars, idx, w)
    cls = vp.bin_class(hist, K, w)
    win = bars[max(0, idx - 500):idx]
    zones = smc.order_blocks([b["o"] for b in win], [b["h"] for b in win], [b["l"] for b in win],
                             [b["c"] for b in win], size=smc.INTERNAL_LENGTH, count=smc.OB_COUNT)
    want = smc.BULLISH if kind == "P" else smc.BEARISH
    in_ob = any(z.bias == want and min(z.lo, z.hi) <= K <= max(z.lo, z.hi) for z in zones)
    grade = (cls == "HVN") + in_ob
    return cls, in_ob, grade


def collect(since: str, until: str | None):
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    from undertow.dirledger_cli import session_index
    store, rows = SnapshotStore(), []
    for inst, sym in POOL.items():
        bars = _bars(sym)
        pos = {b["date"]: i for i, b in enumerate(bars)}
        atr = vp.atr_series(bars)
        for sess, fday in sorted(session_index(store, sym).items()):
            s = sess.isoformat()
            if s < since or (until and s > until) or s not in pos:
                continue
            idx = pos[s]
            if idx < 520 or not atr[idx - 1]:
                continue
            payload = store.load("options", sym, fday)
            if payload is None:
                continue
            snap = snapshot_from_payload(payload, inst, sym)
            spot = bars[idx - 1]["c"]
            for kind in ("P", "C"):
                wl = local_wall(snap, sess, spot, kind)
                if not wl:
                    continue
                K = float(wl["strike"])
                closes = [bars[j]["c"] for j in range(idx, min(len(bars), idx + HORIZON))]
                if len(closes) < HORIZON:
                    continue
                breach = any((c < K) if kind == "P" else (c > K) for c in closes)
                cls, in_ob, grade = classify(bars, idx, K, kind, atr)
                rows.append({"inst": inst, "date": s, "kind": kind, "K": K, "spot": spot,
                             "buf_atr": abs(spot - K) / atr[idx - 1], "cls": cls, "in_ob": in_ob,
                             "grade": grade, "breach": breach})
    return rows


def mh_rd(rows, grp_a, grp_b):
    """Mantel–Haenszel 合并风险差 P(breach|a) − P(breach|b)，按缓冲层。"""
    num = den = 0.0
    for L in range(len(BUF_EDGES) + 1):
        a = [r for r in rows if _layer(r["buf_atr"]) == L and grp_a(r)]
        b = [r for r in rows if _layer(r["buf_atr"]) == L and grp_b(r)]
        if not a or not b:
            continue
        wgt = len(a) * len(b) / (len(a) + len(b))
        num += wgt * (sum(r["breach"] for r in a) / len(a) - sum(r["breach"] for r in b) / len(b))
        den += wgt
    return num / den if den else None


def boot(rows, fn, iters=5000, seed=20260928):
    dates = sorted({r["date"] for r in rows})
    by = {d: [r for r in rows if r["date"] == d] for d in dates}
    rng = random.Random(seed)
    out = []
    for _ in range(iters):
        samp = [r for _ in dates for r in by[rng.choice(dates)]]
        v = fn(samp)
        if v is not None:
            out.append(v)
    out.sort()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-26"); ap.add_argument("--until")
    ap.add_argument("--label", default="exploratory_historical")
    a = ap.parse_args()
    if "prospective" in a.label:
        sys.exit("conf-B 没有盘前冻结的预测账，按日期切片不构成前瞻（Codex 018 #1）；禁止用 prospective 标签。")
    rows = collect(a.since, a.until)
    OUT.mkdir(parents=True, exist_ok=True)
    tests = {"B1_LVN_minus_HVN": (lambda r: r["cls"] == "LVN", lambda r: r["cls"] == "HVN"),
             "B2_grade0_minus_grade2": (lambda r: r["grade"] == 0, lambda r: r["grade"] == 2)}
    res = {"label": a.label, "since": a.since, "n_walls": len(rows), "n_dates": len({r["date"] for r in rows}),
           "counts": {k: sum(1 for r in rows if r[k2] == v) for k, (k2, v) in
                      {"HVN": ("cls", "HVN"), "LVN": ("cls", "LVN"), "MID": ("cls", "MID"),
                       "grade2": ("grade", 2), "grade1": ("grade", 1), "grade0": ("grade", 0)}.items()},
           "breach_rate_all": sum(r["breach"] for r in rows) / len(rows) if rows else None, "tests": {}}
    for name, (ga, gb) in tests.items():
        est = mh_rd(rows, ga, gb)
        bs = boot(rows, lambda rs: mh_rd(rs, ga, gb))
        res["tests"][name] = {"mh_risk_diff": est, "n_a": sum(ga(r) for r in rows), "n_b": sum(gb(r) for r in rows),
                              "lower_one_sided_alpha_0.025": bs[int(0.025 * len(bs))] if bs else None,
                              "p_boot_le0": (sum(x <= 0 for x in bs) / len(bs)) if bs else None}
    (OUT / f"B_{a.label}.json").write_text(json.dumps({"summary": res, "rows": rows}, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
