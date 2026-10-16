"""wedge-explore-v1：复现 Bulkowski 下降楔形统计（≤2015 设计期日线，2016 起不取、不算）。

  python3 scripts/wedge_explore.py --spec v1.1 --run-id run3-v1.1   # → data/backtest/wedge-explore-v1/run3-v1.1/
  python3 scripts/wedge_explore.py --spec v1   --run-id ...         # 只为复现 run1/run2（v1 causal 有前视缺陷）
  --spec 与 run-id 必填；目录已存在则拒绝运行。输入复用 ps-explore-v0.3 已存日线并核对 sha256。
口径：v1 = docs/prereg/2026-10-16_falling_wedge_explore_v1.md；
      v1.1 = docs/prereg/2026-10-16_falling_wedge_explore_v1.1.md（causal_stream 流式口径、分母分列、推断待 D 包校准）。纯标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import zlib
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dz_explore import load, pb_z, wilson_lo  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import wedge as wg  # noqa: E402

RUN = "wedge-explore-v1"
BASE = ROOT / "data/backtest" / RUN
SPECS = {
    "v1": (ROOT / "docs/prereg/2026-10-16_falling_wedge_explore_v1.md", ("descriptive", "causal")),
    "v1.1": (ROOT / "docs/prereg/2026-10-16_falling_wedge_explore_v1.1.md", ("descriptive", "causal_stream")),
}
SPEC = SPECS["v1"][0]           # v1 报告标题沿用（run1/run2 复现）
INFER_NOTE = "推断待 D 包校准，不作结论"
STOCK = ("AAPL", "MSFT", "GOOGL", "NVDA", "AMD", "INTC", "TSLA")
ETF = ("GLD", "SLV", "USO", "QQQ", "SPY", "IWM", "TLT", "GC=F", "SI=F")
VARIANTS = {"main": {"k": 3, "tol": 0.3}, "k2": {"k": 2, "tol": 0.3}, "tol0.5": {"k": 3, "tol": 0.5}}


def wilson_hi(k: int, n: int, z: float = 1.96) -> float | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    return (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den


def run_symbol(sym: str, mode: str, k: int, tol: float) -> list:
    bars = load(sym)
    atr = ps.wilder_atr(bars)
    rows = []
    for j, (w, d, t) in enumerate(wg.find(bars, atr, mode, k=k, tol_atr=tol)):
        r = {"symbol": sym, "first": bars[w.first][0].isoformat(), "last": bars[w.last][0].isoformat(),
             "known": bars[min(w.known, len(bars) - 1)][0].isoformat(), "span": w.last - w.first, "touches": w.touches,
             "direction": d, "breakout": None if t is None else bars[t][0].isoformat()}
        if mode == "causal_stream":           # 冻结的身份与轨道，供审计（descriptive / v1 行不加字段，含义不变）
            r["rails"] = {"h1": list(w.h1), "h2": list(w.h2), "l1": list(w.l1), "l2": list(w.l2), "apex": round(w.apex, 4)}
            r["pivots"] = [[kind, bars[i][0].isoformat(), bars[c][0].isoformat(), price] for kind, i, c, price in w.pivots]
        if d in ("up", "down"):
            start = (w.last if mode == "descriptive" else w.known) + 1
            r["break_pos"] = round((t - w.first) / (w.apex - w.first), 4)
            r.update(wg.outcome(bars, w, d, t))
            r["placebo_up"] = wg.placebo_up_share(bars, atr, w, start, seed=zlib.crc32(repr((sym, w.first, mode, k, tol)).encode()))
        rows.append(r)
    return rows


def summarize(rows: list) -> dict:
    br = [r for r in rows if r["direction"] in ("up", "down")]
    up = [r for r in br if r["direction"] == "up"]
    dn = [r for r in br if r["direction"] == "down"]
    pr = [((r["direction"] == "up") * 1, r["placebo_up"]) for r in br if r.get("placebo_up") is not None]
    diffs = [a - b for a, b in pr]
    z, zp = pb_z([a for a, _ in pr], [b for _, b in pr])

    def rate(rs, key):
        c = [r for r in rs if r.get("complete")]
        return None if not c else round(sum(r[key] for r in c) / len(c), 4)
    pl = [r["placebo_up"] for r in br if r.get("placebo_up") is not None]
    return {
        "detected": len(rows), "no_break": sum(r["direction"] == "no_break" for r in rows),
        "broke_before_known": sum(r["direction"] == "broke_before_known" for r in rows),
        "incomplete": sum(r["direction"] == "incomplete" for r in rows),
        "up": len(up), "down": len(dn), "up_share": None if not br else round(len(up) / len(br), 4),
        "up_ci": None if not br else [round(wilson_lo(len(up), len(br)), 4), round(wilson_hi(len(up), len(br)), 4)],
        "placebo_up_mean": None if not pl else round(sum(pl) / len(pl), 4),
        "paired_mean": None if not diffs else round(sum(diffs) / len(diffs), 4),
        "pb_z": None if z is None else round(z, 2), "pb_p": None if zp is None else round(zp, 5),
        "fail5_up": rate(up, "fail5"), "fail5_down": rate(dn, "fail5"),
        "throwback_up": rate(up, "throwback"), "pullback_down": rate(dn, "throwback"),
        "target_up": rate(up, "target_hit"), "target_down": rate(dn, "target_hit"),
        "break_pos_median": None if not br else round(sorted(r["break_pos"] for r in br)[len(br) // 2], 4),
    }


def summarize_v11(rows: list) -> dict:
    """v1.1：分母分列（识别 = 认出前已破 + 不完整 + 未突破 + 有方向），推断量移入 inference_uncalibrated，
    不再以顶层 pb_z / pb_p 字段输出，防止被当成有效 p 消费（Codex 053）。"""
    s = summarize(rows)
    del s["pb_z"], s["pb_p"]                  # p 舍入到 5 位会把极小值显示成 0；下面用未舍入值按有效数字存
    s["direction_n"] = s["up"] + s["down"]
    pr = [((r["direction"] == "up") * 1, r["placebo_up"]) for r in rows
          if r["direction"] in ("up", "down") and r.get("placebo_up") is not None]
    s["paired_n"] = len(pr)
    assert s["detected"] == s["broke_before_known"] + s["incomplete"] + s["no_break"] + s["direction_n"]
    z, zp = pb_z([x for x, _ in pr], [q for _, q in pr])
    s["inference_uncalibrated"] = {"status": INFER_NOTE, "plug_in_z": None if z is None else round(z, 2),
                                   "plug_in_p": None if zp is None else float(f"{zp:.3g}")}
    return s


def impact(v1_rows: list, stream_rows: list) -> dict:
    """v1 causal（最终投影）与 v1.1 causal_stream 的事件对照：按 (起点, 段末, 认出, 轨道) 认同一事件。"""
    key = lambda w: (w.first, w.last, w.known, w.h1, w.h2, w.l1, w.l2)  # noqa: E731
    a = {key(w): (d, t) for w, d, t in v1_rows}
    b = {key(w): (d, t) for w, d, t in stream_rows}
    same = a.keys() & b.keys()
    return {"v1_events": len(a), "stream_events": len(b), "same_identity": len(same),
            "same_identity_same_outcome": sum(a[x] == b[x] for x in same),
            "v1_only": len(a.keys() - b.keys()), "stream_only": len(b.keys() - a.keys()),
            "v1_direction_n": sum(d in ("up", "down") for d, _ in a.values()),
            "stream_direction_n": sum(d in ("up", "down") for d, _ in b.values())}


def fmt_p(p):
    return "—" if p is None else (f"{p:.2g}" if p >= 1e-4 else f"<1e-4（{p:.1e}）")


def report_md_v11(res: dict) -> str:
    L = [f"# {RUN} / {res['run_id']}（口径 v1.1，≤2015 设计期，探索；{res['spec']} sha256 {res['spec_sha256'][:12]}）", "",
         "Bulkowski（牛市、800+ 笔）：向上 68%｜失败率 上 26% 下 29%｜回抽 上 62% 下 74%｜达标 上 62% 下 29%｜突破位置 61–62%", "",
         "- descriptive = 事后最长段（含后视，口径与 v1 相同，只作描述）；causal_stream = v1.1 流式可交易口径（取代有前视缺陷的 v1 causal）。",
         f"- **z / p 一列：{INFER_NOTE}。** plug-in z 把少量安慰剂比例当已知概率，合成零模型下 2 个控制名义 5% 误拒 27%（Codex 053）；"
         "跨品种同日、相邻窗口与共享控制路径也不独立。表中保留只为与 run2 对照。",
         "- 分母：识别 = 认出前已破 + 不完整 + 未突破 + 有方向；向上占比与配对差只用「有方向」且有安慰剂配对的事件。", ""]
    pa = res.get("prefix_audit", {})
    if pa:
        L += ["## 前缀重放核对（causal_stream）", "",
              "| 变体 | 截断点数 | 不一致 |", "|---|---|---|"]
        L += [f"| {v} | {x['cuts']} | {x['mismatches']} |" for v, x in pa.items()]
        L.append("")
    im = res.get("impact", {})
    if im:
        L += ["## v1 causal（最终投影）→ v1.1 causal_stream 的事件变化（全部品种）", "",
              "| 变体 | v1 事件 | v1.1 事件 | 同一身份 | 其中结果也相同 | 仅 v1 | 仅 v1.1 | v1 有方向 | v1.1 有方向 |",
              "|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {v} | {x['v1_events']} | {x['stream_events']} | {x['same_identity']} | {x['same_identity_same_outcome']} | "
              f"{x['v1_only']} | {x['stream_only']} | {x['v1_direction_n']} | {x['stream_direction_n']} |" for v, x in im.items()]
        L.append("")
    for var in VARIANTS:
        for mode in res["modes"]:
            L.append(f"## {var} · {mode}")
            L.append("")
            L.append("| 组 | 识别 | 认出前已破 | 不完整 | 未突破 | 有方向 | 上 | 下 | 向上占比 [95%] | 安慰剂向上 | 配对 n | 配对差 | "
                     f"z / p（{INFER_NOTE}） | 失败率 上/下 | 回抽 上/下 | 达标 上/下 | 突破位置中位 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for g, s in res["results"][var][mode]["groups"].items():
                ci = "" if not s["up_ci"] else f" [{pct(s['up_ci'][0])}–{pct(s['up_ci'][1])}]"
                inf = s["inference_uncalibrated"]
                zs = "—" if inf["plug_in_z"] is None else f"{inf['plug_in_z']} / {fmt_p(inf['plug_in_p'])}（未校准）"
                L.append(f"| {g} | {s['detected']} | {s['broke_before_known']} | {s['incomplete']} | {s['no_break']} | {s['direction_n']} | "
                         f"{s['up']} | {s['down']} | {pct(s['up_share'])}{ci} | {pct(s['placebo_up_mean'])} | {s['paired_n']} | {s['paired_mean']} | "
                         f"{zs} | {pct(s['fail5_up'])}/{pct(s['fail5_down'])} | {pct(s['throwback_up'])}/{pct(s['pullback_down'])} | "
                         f"{pct(s['target_up'])}/{pct(s['target_down'])} | {s['break_pos_median']} |")
            L.append("")
    L.append("安慰剂 = 同一对轨道与起点价，换本品种其它日期的收盘路径（ATR% 缩放）各 50 条；配对差 = 真实向上(0/1) − 安慰剂向上占比。"
             "固定轨道几何的安慰剂已控制掉楔形信息的一部分，配对差不是楔形全部预测价值，也不能据安慰剂接近 68% 作因果分解。")
    return "\n".join(L) + "\n"


def pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def report_md(res: dict) -> str:
    L = [f"# {RUN} / {res['run_id']}（≤2015 设计期，探索；口径 {SPEC.name} sha256 {res['spec_sha256'][:12]}）", "",
         "Bulkowski（牛市、800+ 笔）：向上 68%｜失败率 上 26% 下 29%｜回抽 上 62% 下 74%｜达标 上 62% 下 29%｜突破位置 61–62%", ""]
    for var in VARIANTS:
        for mode in ("descriptive", "causal"):
            L.append(f"## {var} · {mode}")
            L.append("")
            L.append("| 组 | 识别 | 未突破 | 认出前已破 | 上 | 下 | 向上占比 [95%] | 安慰剂向上 | 配对差 | z（对安慰剂） | p | 失败率 上/下 | 回抽 上/下 | 达标 上/下 | 突破位置中位 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for g, s in res["results"][var][mode]["groups"].items():
                ci = "" if not s["up_ci"] else f" [{pct(s['up_ci'][0])}–{pct(s['up_ci'][1])}]"
                L.append(f"| {g} | {s['detected']} | {s['no_break']} | {s['broke_before_known']} | {s['up']} | {s['down']} | {pct(s['up_share'])}{ci} | "
                         f"{pct(s['placebo_up_mean'])} | {s['paired_mean']} | {s['pb_z']} | {s['pb_p']} | {pct(s['fail5_up'])}/{pct(s['fail5_down'])} | "
                         f"{pct(s['throwback_up'])}/{pct(s['pullback_down'])} | {pct(s['target_up'])}/{pct(s['target_down'])} | {s['break_pos_median']} |")
            L.append("")
    L.append("安慰剂 = 同一对轨道与起点价，换本品种其它日期的收盘路径（ATR% 缩放）各 50 条；配对差 = 真实向上(0/1) − 安慰剂向上占比。"
             "z = Σ(真实 − 安慰剂概率)/√Σp(1−p)，以安慰剂概率为零假设（run1 的符号检验用错，见研究决策日志）。跨品种同日不独立，合并的 z 偏乐观，只作描述。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, choices=sorted(SPECS))
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在，拒绝覆盖")
    spec_path, modes = SPECS[a.spec]
    v11 = a.spec == "v1.1"
    summ = summarize_v11 if v11 else summarize
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    res = {"run": RUN, "run_id": a.run_id, "spec": spec_path.name, "spec_version": a.spec, "modes": list(modes), "git_head": head,
           "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
           "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in ("undertow/analyze/wedge.py", "scripts/wedge_explore.py")},
           "ran_at": datetime.now(timezone.utc).isoformat(), "results": {}}
    if v11:
        res["inference_status"] = INFER_NOTE
        res["prefix_audit"], res["impact"] = {}, {}
        for var, prm in VARIANTS.items():           # 先核对流式口径的前缀一致性：不通过就不出结果
            cuts = bad = 0
            tot = {}
            for s in (*STOCK, *ETF):
                bars = load(s)
                atr = ps.wilder_atr(bars)
                ev, _ = wg.stream(bars, atr, prm["k"], prm["tol"])
                cs = wg.audit_cuts(ev, len(bars))
                miss = wg.prefix_audit(bars, cs, prm["k"], prm["tol"])
                cuts += len(cs)
                bad += len(miss)
                if miss:
                    print(f"前缀不一致：{var} {s} cut={miss[:10]}", file=sys.stderr)
                im = impact(wg.find(bars, atr, "causal", prm["k"], prm["tol"]), wg.find(bars, atr, "causal_stream", prm["k"], prm["tol"]))
                tot = {k_: tot.get(k_, 0) + v_ for k_, v_ in im.items()}
            res["prefix_audit"][var] = {"cuts": cuts, "mismatches": bad}
            res["impact"][var] = tot
            if bad:
                raise SystemExit(f"{var}：causal_stream 前缀重放有 {bad} 处不一致，拒绝出结果（未写 {out}）")
    for var, prm in VARIANTS.items():
        res["results"][var] = {}
        for mode in modes:
            rows = {s: run_symbol(s, mode, prm["k"], prm["tol"]) for s in (*STOCK, *ETF)}
            groups = {"个股": [r for s in STOCK for r in rows[s]], "ETF与期货": [r for s in ETF for r in rows[s]]}
            groups["全部"] = groups["个股"] + groups["ETF与期货"]
            res["results"][var][mode] = {"groups": {g: summ(v) for g, v in groups.items()},
                                         "by_symbol": {s: summ(v) for s, v in rows.items()}, "rows": rows}
    md = report_md_v11(res) if v11 else report_md(res)
    out.mkdir(parents=True)
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(md, "utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
