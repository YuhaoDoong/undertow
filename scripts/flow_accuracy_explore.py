"""持仓流方向（call_direction）准确性 —— 探索性描述（flow-accuracy-explore-v1）。

用户 2026-10-15：「我们积累了这么多天数据了，能不能看看持仓流之类指标的准确性？」

性质：探索，不是确认性检验。正式检验在 dir-analysis-v1.x（2026-12-31 首次正式运行）与方向台账族 D（冻结于 2026-09-28），
本脚本不改、不替代它们，结果不能用来改那些冻结规则。口径在运行前提交（本文件），运行目录已存在则拒绝覆盖。

数据（只读）：
- 信号：data/history/signals/<品种>.json 的 call_direction（偏多/偏空；中性/缺失只计数）与 fired/direction（强信号）。
  ⚠️ call_direction 的现行算法写于 9 月下旬，9/28 之前的行是用它【回放】历史快照得到的（规则作者已见过这段数据），
  只有 session ≥ 2026-09-28 的行是冻结后按日前瞻记录的。所以分段：回放段（< 09-28）与前瞻段（≥ 09-28），前者只能算样本内。
- 价格：长桥日线 OHLC（`longbridge kline <SYM>.US --period day`，只读）。

对齐（快照日 D 在 D 盘前可读，OI 描述 D−1 交易日；见 AGENTS 时序约定）：
- 执行日 T = 第一个 ≥ D 的交易日；多行映射到同一 T 时取 D 最大的那行（最新信息）；T 尚无日线 → 待定，不计。
- 指标 ①开盘→收盘 r_oc = C[T]/O[T] − 1（开盘可执行，主口径）；
       ②收盘→收盘去趋势 r_cc_adj = C[T]/C[T−1] − 1 − mean(此前 60 个日收益，至少 20 个)（对照 8/29 表的口径，含隔夜跳空）；
       ③5 日 r_5 = C[T+4]/O[T] − 1（窗口重叠，只给比率不给 p）。
- 命中 = 收益符号与方向一致；收益恰为 0 → 平局，不计入分母。
- 大波动日：|C[T]/C[T−1] − 1| ≥ 该品种本次数据全部日收益绝对值的 70 分位（描述性：事前不知道哪天大波动）。
- 统计：单品种二项双尾 p 与 Wilson 95% 下界（单品种时序，见 AGENTS「两把尺子」）；跨品种不合并成一个 p。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VERSION = "flow-accuracy-explore-v1-20261015"
INSTS = ["gold", "silver", "wti", "qqq", "tlt", "spy", "iwm", "tqqq"]
PROSPECTIVE_START = "2026-09-28"


def bars(sym: str, count: int = 200) -> dict[str, tuple]:
    from undertow.collect import lb_budget                     # 长桥调用一律经限额/槽位（Codex 036 §4）
    out = lb_budget.run(["longbridge", "kline", f"{sym}.US", "--period", "day", "--count", str(count), "--format", "json"],
                        capture_output=True, text=True, timeout=60)
    rows, _ = json.JSONDecoder().raw_decode(out.stdout.lstrip())
    res = {}
    for r in rows:
        d = str(r["time"])[:10]
        res[d] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    return res


def binom_p(k: int, n: int) -> float | None:
    if n == 0:
        return None
    tail = sum(math.comb(n, i) for i in range(max(k, n - k), n + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def wilson_lo(k: int, n: int, z: float = 1.96) -> float | None:
    if n == 0:
        return None
    ph = k / n
    d = 1 + z * z / n
    return (ph + z * z / (2 * n) - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / d


def cell(hits: list[bool], with_p: bool = True) -> dict:
    n, k = len(hits), sum(hits)
    c = {"n": n, "hits": k, "rate": None if n == 0 else round(k / n, 3)}
    if with_p:
        c["p_two_sided"] = None if n == 0 else round(binom_p(k, n), 4)
        c["wilson_lo"] = None if n == 0 else round(wilson_lo(k, n), 3)
    return c


def analyze(key: str, sym: str, rows: list[dict], px: dict) -> dict:
    days = sorted(px)
    rets = {days[i]: px[days[i]][3] / px[days[i - 1]][3] - 1 for i in range(1, len(days))}
    absr = sorted(abs(v) for v in rets.values())
    big_thr = absr[int(0.7 * (len(absr) - 1))] if absr else None
    by_t: dict[str, dict] = {}
    counts = {"rows": len(rows), "neutral_or_none": 0, "pending": 0, "superseded_same_T": 0}
    for r in sorted(rows, key=lambda x: x["date"]):
        T = next((d for d in days if d >= r["date"]), None)
        if T is None:
            counts["pending"] += 1
            continue
        if T in by_t:
            counts["superseded_same_T"] += 1
        by_t[T] = r
    obs = []
    for T, r in sorted(by_t.items()):
        i = days.index(T)
        if i == 0:
            continue
        o, _, _, c = px[T]
        r_oc = c / o - 1
        past = [rets[days[j]] for j in range(max(1, i - 60), i)]
        r_cc = rets[T]
        r_cc_adj = r_cc - sum(past) / len(past) if len(past) >= 20 else None
        r5 = (px[days[i + 4]][3] / o - 1) if i + 4 < len(days) else None
        obs.append({"T": T, "D": r["date"], "dir": r.get("call_direction"), "fired": bool(r.get("fired")),
                    "fired_dir": r.get("direction"), "r_oc": r_oc, "r_cc": r_cc, "r_cc_adj": r_cc_adj, "r5": r5,
                    "big": big_thr is not None and abs(r_cc) >= big_thr, "sha": r.get("call_code_sha")})

    def sgn(d):
        # call_direction 用「偏多/偏空」，强信号 direction 用「看涨/看跌」（run1 漏了后者 → 强信号一栏全 0/0，见 run1/STATUS.md）
        return 1 if d in ("偏多", "看涨") else (-1 if d in ("偏空", "看跌") else 0)

    def hits(sub, field, dir_field="dir"):
        out = []
        for x in sub:
            s, v = sgn(x[dir_field]), x[field]
            if s == 0 or v is None or v == 0:
                continue
            out.append((v > 0) == (s > 0))
        return out

    res = {"inst": key, "sym": sym, "counts": counts, "big_move_threshold": big_thr,
           "n_directional": sum(1 for x in obs if sgn(x["dir"]) != 0),
           "n_neutral": sum(1 for x in obs if sgn(x["dir"]) == 0),
           "code_sha": sorted({x["sha"] for x in obs if x["sha"]}), "periods": {}}
    seg = {"all": obs, "replay_lt_0928": [x for x in obs if x["D"] < PROSPECTIVE_START],
           "prospective_ge_0928": [x for x in obs if x["D"] >= PROSPECTIVE_START]}
    for name, sub in seg.items():
        res["periods"][name] = {
            "open_to_close": cell(hits(sub, "r_oc")),
            "close_to_close_detrended": cell(hits(sub, "r_cc_adj")),
            "five_day_overlapping": cell(hits(sub, "r5"), with_p=False),
            "big_move_days_oc": cell(hits([x for x in sub if x["big"]], "r_oc")),
            "quiet_days_oc": cell(hits([x for x in sub if not x["big"]], "r_oc")),
            "strong_signal_oc": cell(hits([x for x in sub if x["fired"]], "r_oc", "fired_dir")),
            "first_T": sub[0]["T"] if sub else None, "last_T": sub[-1]["T"] if sub else None,
        }
    res["prospective_rows"] = [{k: (round(v, 5) if isinstance(v, float) else v) for k, v in x.items() if k != "sha"}
                               for x in seg["prospective_ge_0928"]]
    return res


def main(argv: list[str]) -> int:
    from undertow.core.config import load_config
    run = argv[1] if len(argv) > 1 else "run1"
    out_dir = ROOT / "data/backtest/flow-accuracy-explore-v1" / run
    if out_dir.exists():
        print(f"{out_dir} 已存在：拒绝覆盖", file=sys.stderr)
        return 2
    cfg = load_config()
    results, errors = [], {}
    for key in INSTS:
        try:
            sym = cfg.instruments[key].options.symbol
            rows = json.loads((ROOT / f"data/history/signals/{key}.json").read_text())
            results.append(analyze(key, sym, rows, bars(sym)))
        except Exception as e:                                 # noqa: BLE001 —— 失败品种显式列出，不静默跳过
            errors[key] = f"{type(e).__name__}: {e}"[:200]
    out_dir.mkdir(parents=True)
    payload = {"version": VERSION, "generated_at": date.today().isoformat(), "nature": "exploratory",
               "results": results, "errors": errors}
    (out_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# {VERSION} · {run}（探索，非确认性）", ""]
    for r in results:
        lines.append(f"## {r['inst']}（{r['sym']}）有方向 {r['n_directional']} / 中性 {r['n_neutral']} · 大波动门槛 {r['big_move_threshold']:.4f}")
        for pn, p in r["periods"].items():
            f = lambda c: f"{c['hits']}/{c['n']}" + ("" if c["rate"] is None else f"={c['rate']:.0%}") + \
                ("" if c.get("p_two_sided") is None else f" p={c['p_two_sided']}")
            lines.append(f"- {pn}（{p['first_T']}→{p['last_T']}）：开→收 {f(p['open_to_close'])} · 收→收去趋势 "
                         f"{f(p['close_to_close_detrended'])} · 5日(重叠) {f(p['five_day_overlapping'])} · "
                         f"大波动 {f(p['big_move_days_oc'])} · 平静 {f(p['quiet_days_oc'])} · 强信号 {f(p['strong_signal_oc'])}")
        lines.append("")
    if errors:
        lines.append(f"失败品种：{errors}")
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
