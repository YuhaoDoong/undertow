"""ps-exit-v1：同一批道氏 A 型入场上比较五种出场政策（≤2015 设计集；配置 data/backtest/ps-exit-v1/config.json）。

  python3 scripts/ps_exit_policies.py --run-id run1

只读 ps-explore 的原始输入（sha256 核对），run-id 必填、目录存在即拒绝。描述性，无 p 值；R 不是账户收益。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics as st
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

BASE = ROOT / "data/backtest/ps-exit-v1"
CFG = BASE / "config.json"
GROUPS = {"ETF": ("GLD", "SLV", "USO", "QQQ", "SPY"), "个股": ("GOOGL", "AAPL", "MSFT", "NVDA", "AMD", "INTC", "TSLA"), "连续期货": ("GC=F", "SI=F")}
CONT_BARS, CONT_ATR = 20, 2.0


def trades_for(sym: str, ref_dates: list) -> list:
    d, sha = pe.fetch(sym)
    bars_all, _, actions = pe.parse(d)
    assert bars_all[-1][0] <= pe.DESIGN_END
    segs, _ = pe.segments(bars_all, [b[0] for b in bars_all] if sym in pe.FUTURES else ref_dates)
    out = []
    for si, bars in enumerate(segs):
        if len(bars) < ps.WARMUP + 5:
            continue
        atr = ps.wilder_atr(bars)
        piv, eps = ps.detect(bars, atr)
        last_i = len(bars) - 1
        busy = {"long": -1, "short": -1}
        for e in sorted(eps, key=lambda e: (e.signal_at if e.signal_at is not None else e.start, e.start)):
            if e.kind != "A" or e.status != "signal":
                continue
            sig, ei, s = e.signal_at, e.signal_at + 1, (1 if e.direction == "long" else -1)
            a_ref = atr[sig - 1]
            stop0 = e.extreme.price - s * ps.STOP_BUF * a_ref
            p0 = ps.simulate_policy(bars, piv, ei, stop0, e.direction, ps.POLICIES["P0"], last_i=last_i)
            if p0["status"] != "entered" or ei <= busy[e.direction]:
                continue                                   # 入场队列 = P0 下实际入场且未被占用（与 ps-explore 一致）
            busy[e.direction] = p0["exit_i"]
            stop1 = e.hl1.price - s * ps.STOP_BUF * a_ref
            row = {"id": f"{sym}-{si}-{e.direction}-{e.start}", "symbol": sym, "direction": e.direction,
                   "entry_date": str(bars[ei][0]), "entry": bars[ei][1], "atr_entry": atr[ei - 1], "stop_extreme": stop0, "stop_pullback": stop1,
                   "risk_p0": p0["risk"], "policies": {}}
            for name, pol in ps.POLICIES.items():
                stp = stop0 if pol["stop"] == "extreme" else stop1
                r = ps.simulate_policy(bars, piv, ei, stp, e.direction, pol, last_i=last_i)
                if r["status"] == "entered":
                    x = r["exit_i"]
                    r["exit_date"] = str(bars[x][0])
                    r["pct"] = s * (r["exit_price"] / r["entry"] - 1)
                    r["atr_units"] = s * (r["exit_price"] - r["entry"]) / atr[ei - 1]
                    r["r_on_p0_risk"] = s * (r["exit_price"] - r["entry"]) / p0["risk"]
                    sp_ = r.get("audit", {}).get("struct_point")
                    r["struct_lag"] = sp_["conf"] - sp_["idx"] if sp_ else None
                    if "stop" in r["exit_reason"]:
                        win = bars[x + 1:x + 1 + CONT_BARS]
                        r["continued_after_stop"] = (any(s * (b[4] - r["entry"]) >= CONT_ATR * atr[ei - 1] for b in win)
                                                     if len(win) == CONT_BARS else None)
                    p0x = p0["exit_i"]
                    r["move_to_p0_exit_atr"] = s * (bars[p0x][4] - r["exit_price"]) / atr[ei - 1] if p0x > x else None
                r.pop("audit", None)
                row["policies"][name] = r
            out.append(row)
    return out


def summarize(rows: list) -> dict:
    def agg(rs, pol):
        xs = [r["policies"][pol] for r in rs]
        ent = [x for x in xs if x["status"] == "entered"]
        if not ent:
            return {"n": 0}
        f = lambda k: [x[k] for x in ent]
        reasons = {}
        for x in ent:
            reasons[x["exit_reason"]] = reasons.get(x["exit_reason"], 0) + 1
        cont = [x.get("continued_after_stop") for x in ent if x.get("continued_after_stop") is not None]
        return {"n": len(ent), "not_entered": {k: sum(1 for x in xs if x["status"] == k) for k in {x["status"] for x in xs} if k != "entered"},
                "pct_mean": _r(st.mean(f("pct"))), "pct_median": _r(st.median(f("pct"))),
                "atr_mean": _r(st.mean(f("atr_units"))), "atr_median": _r(st.median(f("atr_units"))),
                "r_own_mean": _r(st.mean(f("r_mult"))), "r_p0risk_mean": _r(st.mean(f("r_on_p0_risk"))),
                "win_rate_pct_gt0": _r(sum(1 for v in f("pct") if v > 0) / len(ent)), "worst_atr": _r(min(f("atr_units"))),
                "days_median": st.median(f("days_held")), "exit_reasons": reasons,
                "breakeven_triggered": sum(1 for x in ent if x.get("breakeven_from_i") is not None),
                "gap_loss_r_lt_minus1": sum(1 for x in ent if x["r_mult"] < -1),
                "stopped_then_continued": f"{sum(cont)}/{len(cont)}",
                "struct_lag_median": _med([x["struct_lag"] for x in ent if x.get("struct_lag") is not None])}
    out = {}
    for g, syms in list(GROUPS.items()) + [("全部", tuple(GROUPS["ETF"] + GROUPS["个股"] + GROUPS["连续期货"]))]:
        for d in ("long", "short"):
            rs = [r for r in rows if r["symbol"] in syms and r["direction"] == d]
            c60 = [r for r in rs if all(r["policies"][p]["status"] in ("entered", "open_beyond_stop") for p in ("P0", "P1", "P2", "P3"))]
            c250 = [r for r in rs if all(r["policies"][p]["status"] in ("entered", "open_beyond_stop") for p in ("P3", "P4"))]
            both60 = [r for r in c60 if all(r["policies"][p]["status"] == "entered" for p in ("P0", "P1", "P2", "P3"))]
            paired = {p: _r(st.mean([r["policies"][p]["atr_units"] - r["policies"]["P0"]["atr_units"] for r in both60])) if both60 else None
                      for p in ("P1", "P2", "P3")}
            both250 = [r for r in c250 if all(r["policies"][p]["status"] == "entered" for p in ("P3", "P4"))]
            out[f"{g}|{d}"] = {"queue": len(rs), "cohort60": len(c60), "cohort250": len(c250),
                               "P0_P3_on_cohort60": {p: agg(c60, p) for p in ("P0", "P1", "P2", "P3")},
                               "paired_diff_atr_vs_P0_all_entered": paired, "paired_n": len(both60),
                               "P3_P4_on_cohort250": {p: agg(c250, p) for p in ("P3", "P4")},
                               "paired_diff_atr_P4_minus_P3": _r(st.mean([r["policies"]["P4"]["atr_units"] - r["policies"]["P3"]["atr_units"]
                                                                          for r in both250])) if both250 else None}
    return out


def _r(x):
    return None if x is None else round(x, 4)


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def report(summ: dict, meta: dict) -> str:
    L = [f"# ps-exit-v1 {meta['run_id']}：同一批道氏 A 型入场上的五种出场（≤2015 设计集，描述）\n",
         "**性质：** 探索、无 p 值、同日相关；入场队列来自旧规则（P0）；收益未计手续费与分红；做空未计融券。共同尺度 = ATR 单位与标的百分比；R 只在各自风险下解读。"
         f"同距离对照尚未实现（pending）。配置 sha256 {meta['config_sha256'][:12]}（运行前提交），代码 HEAD `{meta['git_head'][:8]}`。\n"]
    for key, v in summ.items():
        g, d = key.split("|")
        L.append(f"\n## {g} · {'做多' if d == 'long' else '做空'}（入场 {v['queue']}，60 根共同队列 {v['cohort60']}，250 根共同队列 {v['cohort250']}）\n")
        L.append("| 政策 | n | 平均 ATR | 中位 ATR | 平均 % | 胜率(%>0) | 最差 ATR | 持有中位 | 新风险 R 均值 | 原风险 R 均值 | 移成本 | 跳空 R<−1 | 止损后原方向继续 | 出场原因 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        rows = [(p, x) for p, x in v["P0_P3_on_cohort60"].items()] + [(p + "（250 队列）", x) for p, x in v["P3_P4_on_cohort250"].items()]
        for p, x in rows:
            if not x.get("n"):
                L.append(f"| {p} | 0 |" + " |" * 12); continue
            pct = f"{x['pct_mean']:+.2%}"
            L.append(f"| {p} | {x['n']} | {x['atr_mean']} | {x['atr_median']} | {pct} | {x['win_rate_pct_gt0']} | {x['worst_atr']} | {x['days_median']} | "
                     f"{x['r_own_mean']} | {x['r_p0risk_mean']} | {x['breakeven_triggered']} | {x['gap_loss_r_lt_minus1']} | {x['stopped_then_continued']} | "
                     f"{', '.join(f'{k} {n}' for k, n in sorted(x['exit_reasons'].items()))} |")
        L.append(f"\n逐笔配对（四政策都入场的 {v['paired_n']} 笔，ATR 单位，相对 P0）：P1 {v['paired_diff_atr_vs_P0_all_entered']['P1']}，"
                 f"P2 {v['paired_diff_atr_vs_P0_all_entered']['P2']}，P3 {v['paired_diff_atr_vs_P0_all_entered']['P3']}；P4 − P3（250 队列）{v['paired_diff_atr_P4_minus_P3']}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    t0 = time.time()
    ref, _ = pe.fetch("SPY")
    ref_dates = [b[0] for b in pe.parse(ref)[0]]
    rows = []
    for syms in GROUPS.values():
        for sym in syms:
            rows += trades_for(sym, ref_dates)
    out.mkdir(parents=True)
    meta = {"run_id": a.run_id, "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
            "config_sha256": hashlib.sha256(CFG.read_bytes()).hexdigest(),
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in
                            ("undertow/analyze/price_structure.py", "scripts/ps_exit_policies.py", "scripts/ps_explore.py")},
            "inputs_sha256": json.loads((ROOT / "data/backtest/ps-explore-v0.3/inputs_sha256.json").read_text("utf-8")), "n_trades": len(rows)}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    with open(out / "trades.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = summarize(rows)
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report(summ, meta), "utf-8")
    print(f"{len(rows)} 笔入场，用时 {time.time() - t0:.1f} 秒 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
