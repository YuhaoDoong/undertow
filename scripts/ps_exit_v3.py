"""ps-exit-v3：N60/N250 基准 + 用户规则 U1–U4，同一批入场上的配对比较（Codex 041；配置 data/backtest/ps-exit-v1/config_v3.json）。

  python3 scripts/ps_exit_v3.py --run-id run4_v3

配置与 POLICIES 不一致即拒绝运行；run-id 目录存在即拒绝。≤2015，描述性，无 p 值，未扣费。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics as st
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_exit_policies as px  # noqa: E402
from scripts import ps_explore as pe  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

CFG = ROOT / "data/backtest/ps-exit-v1/config_v3.json"
SET = ("P0", "N60", "U1", "U2", "N250", "U3", "U4")
PAIRS = (("N60", "P0"), ("U1", "N60"), ("U2", "U1"), ("U3", "N250"), ("U3", "U1"), ("N250", "N60"))
# v4（--version v4）：加用户澄清的「上膛」版 U5/U6
SET_V4 = SET + ("U5", "U6")
PAIRS_V4 = PAIRS + (("U5", "N60"), ("U5", "U1"), ("U6", "N250"), ("U6", "U3"))


def _r(x):
    return None if x is None else round(x, 4)


def pair_stats(rows, a, b):
    out = {}
    for unit in ("atr_units", "pct", "r_on_p0_risk"):
        d = sorted(((r["policies"][a][unit] - r["policies"][b][unit]), r["id"]) for r in rows)
        xs = [x for x, _ in d]
        tot = sum(xs)
        top = sorted(d, key=lambda t: -abs(t[0]))
        out[unit] = {"mean": _r(st.mean(xs)), "median": _r(st.median(xs)), "sum": _r(tot), "n": len(xs),
                     "top1": {"id": top[0][1], "diff": _r(top[0][0])}, "top2": {"id": top[1][1], "diff": _r(top[1][0])} if len(top) > 1 else None,
                     "mean_ex_top1": _r(st.mean([x for x, i in d if i != top[0][1]])) if len(d) > 1 else None,
                     "mean_ex_top2": _r(st.mean([x for x, i in d if i not in (top[0][1], top[1][1])])) if len(d) > 2 else None}
    return out


def pol_stats(rows, p):
    xs = [r["policies"][p] for r in rows]
    gaps = [x for x in xs if "gap" in x["exit_reason"]]
    return {"n": len(xs), "atr_mean": _r(st.mean(x["atr_units"] for x in xs)), "atr_median": _r(st.median(x["atr_units"] for x in xs)),
            "pct_mean": _r(st.mean(x["pct"] for x in xs)), "r_p0_mean": _r(st.mean(x["r_on_p0_risk"] for x in xs)),
            "win_rate": _r(sum(1 for x in xs if x["pct"] > 0) / len(xs)), "worst_atr": _r(min(x["atr_units"] for x in xs)),
            "days_median": st.median(x["days_held"] for x in xs),
            "gap_events": len(gaps), "gap_loss_events": sum(1 for x in gaps if x["pct"] < 0),
            "severe_r_lt_minus1": sum(1 for x in xs if x["r_mult"] < -1),
            "exit_reasons": {k: sum(1 for x in xs if x["exit_reason"] == k) for k in sorted({x["exit_reason"] for x in xs})}}


def summarize(rows):
    groups = {**px.GROUPS, "全部": tuple(px.GROUPS["ETF"] + px.GROUPS["个股"] + px.GROUPS["连续期货"])}
    out = {}
    for g, syms in groups.items():
        for d in ("long", "short"):
            rs = [r for r in rows if r["symbol"] in syms and r["direction"] == d]
            p60 = tuple(p for p in SET if ps.POLICIES[p]["hold"] == 60)
            c60 = [r for r in rs if all(r["policies"][p]["status"] == "entered" for p in p60)]
            c250 = [r for r in rs if all(r["policies"][p]["status"] == "entered" for p in SET)]
            out[f"{g}|{d}"] = {"queue": len(rs), "common60_n": len(c60), "common250_n": len(c250),
                               "excluded_60": {p: sum(1 for r in rs if r["policies"][p]["status"] != "entered") for p in p60},
                               "common60": {p: pol_stats(c60, p) for p in p60} if c60 else {},
                               "common250": {p: pol_stats(c250, p) for p in SET} if c250 else {},
                               "pairs60": {f"{a}−{b}": pair_stats(c60, a, b) for a, b in PAIRS if a in p60 and b in p60} if c60 else {},
                               "pairs250": {f"{a}−{b}": pair_stats(c250, a, b) for a, b in PAIRS} if c250 else {}}
    return out


def report(summ, meta):
    L = [f"# {meta['config_file'].replace('config_', 'ps-exit-').replace('.json', '')} {meta['run_id']}：N60/N250 基准 + 用户规则 U1–U4（同一批道氏 A 型入场；≤2015 设计集，描述）\n",
         "**性质：** 探索、无 p 值、同日相关、未扣费；入场队列来自旧规则 P0；初止损 = 收盘之字形 LL/HH ∓0.1ATR（不是影线）。"
         f"配置 sha256 {meta['config_sha256'][:12]}（运行前提交并与代码断言一致），HEAD `{meta['git_head'][:8]}`。\n",
         "配对含义：N60−P0 = 取消 2R；U1−N60 = 60 根下加入 LH/HL 离场；U2−U1 = 移成本；U3−N250 = 250 根下加入 LH/HL 离场；U3−U1、N250−N60 = 期限变化。\n"]
    for key, v in summ.items():
        g, d = key.split("|")
        L.append(f"\n## {g} · {'做多' if d == 'long' else '做空'}（入场 {v['queue']}；60 根共同 {v['common60_n']}；250 根共同 {v['common250_n']}）\n")
        for cname, cohort, pairs in (("60 根共同队列", v["common60"], v["pairs60"]), ("250 根共同队列", v["common250"], v["pairs250"])):
            if not cohort:
                continue
            L.append(f"**{cname}**\n")
            L.append("| 政策 | 平均 ATR | 中位 ATR | 平均 % | 原风险 R | 胜率 | 最差 ATR | 持有中位 | 跳空事件（其中亏） | R<−1 | 出场原因 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for p, x in cohort.items():
                L.append(f"| {p} | {x['atr_mean']} | {x['atr_median']} | {x['pct_mean']:+.2%} | {x['r_p0_mean']} | {x['win_rate']} | {x['worst_atr']} | "
                         f"{x['days_median']} | {x['gap_events']}（{x['gap_loss_events']}） | {x['severe_r_lt_minus1']} | "
                         f"{', '.join(f'{k} {n}' for k, n in x['exit_reasons'].items())} |")
            L.append("\n| 配对 | ATR 均值 | ATR 中位 | % 均值 | 原风险 R 均值 | 前 1 笔贡献 | 去前 1 / 前 2 后 ATR 均值 |")
            L.append("|---|---|---|---|---|---|---|")
            for k, x in pairs.items():
                a = x["atr_units"]
                L.append(f"| {k} | {a['mean']} | {a['median']} | {x['pct']['mean']:+.2%} | {x['r_on_p0_risk']['mean']} | "
                         f"{a['top1']['id']} {a['top1']['diff']}（总 {a['sum']}） | {a['mean_ex_top1']} / {a['mean_ex_top2']} |")
            L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--version", choices=("v3", "v4"), default="v3")
    a = ap.parse_args()
    global CFG, SET, PAIRS
    if a.version == "v4":
        CFG, SET, PAIRS = ROOT / "data/backtest/ps-exit-v1/config_v4.json", SET_V4, PAIRS_V4
    cfg = json.loads(CFG.read_text("utf-8"))
    for name, spec in cfg["policies"].items():                 # 配置与计算绑定（Codex 041）
        if ps.POLICIES.get(name) != spec:
            raise SystemExit(f"配置与代码不一致：{name} {spec} vs {ps.POLICIES.get(name)}")
    out = px.BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    px.POLSET = SET
    ref, _ = pe.fetch("SPY")
    ref_dates = [b[0] for b in pe.parse(ref)[0]]
    rows = []
    for syms in px.GROUPS.values():
        for sym in syms:
            rows += px.trades_for(sym, ref_dates)
    out.mkdir(parents=True)
    meta = {"run_id": a.run_id, "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
            "config_file": CFG.name, "config_sha256": hashlib.sha256(CFG.read_bytes()).hexdigest(),
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in
                            ("undertow/analyze/price_structure.py", "scripts/ps_exit_policies.py", "scripts/ps_exit_v3.py", "scripts/ps_explore.py")},
            "inputs_sha256": json.loads((ROOT / "data/backtest/ps-explore-v0.3/inputs_sha256.json").read_text("utf-8")), "n_trades": len(rows),
            "replay": f"python3 scripts/ps_exit_v3.py --run-id <新 id>（同一代码与配置哈希应得同一结果）"}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    with open(out / "trades.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = summarize(rows)
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report(summ, meta), "utf-8")
    print(f"{len(rows)} 笔入场 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
