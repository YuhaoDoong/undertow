"""到期日磁吸 v3：同品种事前匹配对照（协议 docs/prereg/2026-09-29_expiry_pin_v3_match_protocol.md；探索，非检验）。

  python3 scripts/expiry_pin_match.py --start 2026-06-25 --end 2026-09-25 --asof 2026-09-29

第一版只输出共同支持表、未匹配清单、逐行配对表与描述（不给 p 值、不给区间）。产物写 data/backtest/expiry_pin/<标签>/，
已存在拒绝覆盖；保存日线原文与哈希、快照路径与哈希、代码哈希、git HEAD。只读、纯标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import expiry_pin as ep  # noqa: E402
from undertow.collect.cboe_history import CBOE_HIST_URL  # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload  # noqa: E402
from undertow.collect.store import SnapshotStore  # noqa: E402
from undertow.core.config import load_config  # noqa: E402
from undertow.dirledger_cli import session_index  # noqa: E402
from scripts.expiry_pin_explore import MIN_DAYS, daily_ohlc, sha  # noqa: E402

OUT = ROOT / "data/backtest/expiry_pin"
VERSION = "expiry-pin-match-v3-20260929"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True); ap.add_argument("--asof", required=True)
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    run_dir = OUT / f"{VERSION}_{a.start}_{a.end}_asof{a.asof}"
    if run_dir.exists():
        sys.exit(f"{run_dir} 已存在：不覆盖历史产物")
    cfg, store = load_config(), SnapshotStore()
    key_of = {i.options.symbol: i.key for i in cfg.instruments.values() if i.options}
    syms = a.symbols or sorted(k for k in key_of if len(session_index(store, k)) >= MIN_DAYS)
    run_dir.mkdir(parents=True)
    rows, inputs = [], {"ohlc": {}, "snapshots": {}}
    for sym in syms:
        bars, raw = daily_ohlc(sym, end)
        blob = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
        (run_dir / f"ohlc_{sym}.json").write_bytes(blob)
        inputs["ohlc"][sym] = {"sha256": sha(blob), "rows": len(raw)}
        dates = sorted(bars)
        for sess, f in sorted(session_index(store, sym).items()):
            if not (start <= sess <= end) or sess not in bars:
                continue
            path = store.path_of("options", sym, f)
            inputs["snapshots"][f"{sym}|{sess}"] = {"path": str(path.relative_to(ROOT)), "sha256": sha(path.read_bytes())}
            cs = snapshot_from_payload(store.load("options", sym, f), key_of.get(sym, sym.lower()), sym).contracts
            exps = sorted({x.expiry for x in cs if x.expiry >= sess})
            prior = [x for x in dates if x < sess]
            if not exps or not prior:
                continue
            ref = bars[prior[-1]][3]                                            # C[D−1]
            k = ep.max_oi_strike(cs, {exps[0]}, ref)
            if k is None:
                continue
            feat = ep.match_features(dates, bars, sess, k)
            o, _, _, c = bars[sess]
            r = {"sym": sym, "d": sess.isoformat(), "is_expiry": exps[0] == sess, "expiry": exps[0].isoformat(), "k": k,
                 "feat": feat, "open": o, "close": c}
            if feat:
                r.update(pull=ep.pull(o, c, k, feat["atr"]), dist_open_atr=abs(o - k) / feat["atr"])
            rows.append(r)
        print(f"{sym}: {sum(r['sym'] == sym for r in rows)} 个认证交易日", file=sys.stderr)
    m = ep.match_pairs(rows)

    lines = [f"# 到期日磁吸 v3 同品种事前匹配（{VERSION}；{a.start}–{a.end}，asof {a.asof}；探索性描述，非检验）",
             "协议：docs/prereg/2026-09-29_expiry_pin_v3_match_protocol.md。匹配字段 = 方向 × 墙距箱 × 波动档 × 20 日趋势 × 事件（历史全 unknown）。",
             "配对差 = pull(到期日) − pull(对照日)，pull = (|开−K|−|收−K|)/ATR14。不给 p 值与区间（对照复用、跨日依赖未处理）。", ""]
    lines.append("## 品种覆盖")
    for s in syms:
        rs = [r for r in rows if r["sym"] == s]
        ne, nn = sum(r["is_expiry"] for r in rs), sum(not r["is_expiry"] for r in rs)
        np_ = sum(p["expiry"]["sym"] == s for p in m["pairs"])
        note = "（仅本对照剔除：缺非到期日）" if nn == 0 else ("（缺到期日）" if ne == 0 else "")
        lines.append(f"  {s:5s} 到期日 {ne:3d} 非到期日 {nn:3d} 配对 {np_:3d} {note}")
    lines += ["", "## 未匹配（原因）"]
    why = Counter((u["sym"], u["why"]) for u in m["unmatched"])
    for (s, w), n in sorted(why.items()):
        lines.append(f"  {s:5s} {w}: {n}")
    lines += ["", "## 共同支持（只列同时有到期日与非到期日的格子）"]
    both = {k: v for k, v in m["cells"].items() if v["expiry"] and v["nonexpiry"]}
    for k, v in sorted(both.items()):
        lines.append(f"  {k}: 到期日 {v['expiry']} 非到期日 {v['nonexpiry']}")
    lines.append(f"  有共同支持的格子 {len(both)} / 全部格子 {len(m['cells'])}")
    lines += ["", "## 配对描述（每品种与合并；不作推断）"]

    def desc(ps, label):
        if not ps:
            lines.append(f"  {label:6s} 配对 0 —— 本数据不能识别"); return
        ds = [p["expiry"]["pull"] - p["control"]["pull"] for p in ps]
        imb = [abs(p["expiry"]["feat"]["dist_prev_atr"] - p["control"]["feat"]["dist_prev_atr"]) for p in ps]
        imb_o = [abs(p["expiry"]["dist_open_atr"] - p["control"]["dist_open_atr"]) for p in ps]
        ctrls = {(p["control"]["sym"], p["control"]["d"]) for p in ps}
        lines.append(f"  {label:6s} 配对 {len(ps):3d}（不同对照日 {len(ctrls)}）差均值 {statistics.fmean(ds):+.3f} 中位 "
                     f"{statistics.median(ds):+.3f} 为正 {sum(x > 0 for x in ds)}/{len(ds)} | 间隔中位 "
                     f"{statistics.median(abs(p['gap_days']) for p in ps):g} 日 | 残余墙距差（前收）中位 {statistics.median(imb):.2f} ATR、"
                     f"（开盘）中位 {statistics.median(imb_o):.2f} ATR")
    for s in syms:
        desc([p for p in m["pairs"] if p["expiry"]["sym"] == s], s)
    desc(m["pairs"], "合并")
    lines += ["", "局限：同品种内到期日与对照日仍在 DTE、到期类型上不同；对照复用与跨日相关未处理；事件态全 unknown；",
              "匹配字段与分箱是锁定的探索设计，不是校准结果；本批快照此前看过。"]

    def flat(r):
        return {k: v for k, v in r.items() if k != "feat"} | ({"feat": r["feat"]} if r["feat"] else {"feat": None})
    with open(run_dir / "pairs.jsonl", "w", encoding="utf-8") as fh:
        for p in m["pairs"]:
            fh.write(json.dumps({"expiry": flat(p["expiry"]), "control": flat(p["control"]), "gap_days": p["gap_days"],
                                 "control_reuse": p["control_reuse"],
                                 "pull_diff": p["expiry"]["pull"] - p["control"]["pull"]}, ensure_ascii=False) + "\n")
    with open(run_dir / "unmatched.jsonl", "w", encoding="utf-8") as fh:
        for u in m["unmatched"]:
            fh.write(json.dumps(flat(u), ensure_ascii=False) + "\n")
    (run_dir / "summary.txt").write_text("\n".join(lines) + "\n", "utf-8")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    code = {p: sha((ROOT / p).read_bytes()) for p in ("scripts/expiry_pin_match.py", "scripts/expiry_pin_explore.py",
                                                     "undertow/analyze/expiry_pin.py",
                                                     "docs/prereg/2026-09-29_expiry_pin_v3_match_protocol.md")}
    (run_dir / "manifest.json").write_text(json.dumps({"version": VERSION, "start": a.start, "end": a.end, "asof": a.asof,
                                                       "symbols": syms, "git_head": head, "code_sha256": code,
                                                       "inputs": inputs, "n_rows": len(rows), "n_pairs": len(m["pairs"])},
                                                      ensure_ascii=False, indent=1), "utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
