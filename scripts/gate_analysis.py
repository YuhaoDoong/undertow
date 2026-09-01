"""闸门净效果分析 —— validation.REGISTRY["gate_net_effect"] 的可复现入口

2026-09-01 codex review 指出：该条目的 89 条逐闸门结果原本只以散文硬编码进
登记簿，仓库里没有生成入口，等于结论不可复现、不可审计。本脚本补上。

═══ 样本构造（这里的每个选择都会改变结论，改前想清楚）═══
· 数据源：data/history/signals/*.json（signal_ledger.record 自动落盘）
· 每条台账行拆成【看涨/看跌两条候选记录】，只保留 pressure_ok=True 的那一侧。
  ⚠️ 同一行的两个方向可能同时 pressure_ok（罕见），此时会贡献两条记录 ——
     `--dedupe-row` 可只保留最终开火的那一侧（或压力比更大的一侧）。
· 收益：forward_1d(=D+0，信号日当天) / forward_2d(=D+1，次日)，
  去趋势：减 horizon × drift_60d（与 signal_ledger.summarize 同口径）
· 方向化：看跌取负，使"正数=信号方向被兑现"
· 抽稀：--thin 用 signal_ledger._thin 按品种去重叠

用法：
    python3 scripts/gate_analysis.py                 # 默认口径（复现登记簿数字）
    python3 scripts/gate_analysis.py --thin          # 加抽稀
    python3 scripts/gate_analysis.py --dedupe-row    # 一行只取一个方向
    python3 scripts/gate_analysis.py --exclude wti   # 剔除某品种，查构成偏倚
"""
import argparse
import os
import pathlib
import random
import statistics
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from undertow.analyze import signal_ledger as sl        # noqa: E402

GATES = (("pressure", 0), ("wing", 1), ("scale", 2))


def build(rows, *, dedupe_row=False, exclude=()):
    """台账行 → 候选记录。只保留压力比已达标的一侧。"""
    out = []
    for r in rows:
        if r.get("instrument") in exclude:
            continue
        drift = r.get("drift_60d")
        if drift is None:
            continue
        cands = []
        for side, gk, mk in (("看涨", "bull_gates", "bull_contra_margin"),
                             ("看跌", "bear_gates", "bear_contra_margin")):
            g = r.get(gk) or []
            if len(g) != 3 or not g[0]:
                continue
            sgn = -1.0 if side == "看跌" else 1.0
            rec = dict(inst=r["instrument"], date=r["date"], side=side,
                       pressure=bool(g[0]), wing=bool(g[1]), scale=bool(g[2]),
                       contra=(r.get(mk) or 0) > 0,
                       fired=bool(r.get("fired")) and r.get("direction") == side)
            for h, lb in ((1, "d0"), (2, "d1")):
                v = r.get(f"forward_{h}d")
                rec[lb] = None if v is None else (v - h * drift) * sgn
            rec["ratio"] = (r.get("bull_pressure_ratio") if side == "看涨"
                            else r.get("bear_pressure_ratio")) or 0.0
            cands.append(rec)
        if dedupe_row and len(cands) > 1:
            fired = [c for c in cands if c["fired"]]
            cands = fired[:1] or [max(cands, key=lambda c: c["ratio"])]
        out.extend(cands)
    return out


def thin(recs, key, horizon):
    items = [({"instrument": c["inst"], "date": c["date"], "direction": c["side"]},
              c[key]) for c in recs if c[key] is not None]
    return [v for _, v in sl._thin(items, horizon)]


def stat(vals):
    if len(vals) < 3:
        return None
    w = sum(1 for v in vals if v > 0)
    return dict(n=len(vals), hits=w, rate=w / len(vals),
                mean=statistics.mean(vals), median=statistics.median(vals))


def show(label, recs, use_thin):
    parts = []
    for key, h in (("d0", 1), ("d1", 2)):
        v = thin(recs, key, h) if use_thin else [c[key] for c in recs if c[key] is not None]
        s = stat(v)
        parts.append(f"{key.upper()} {s['hits']}/{s['n']}={s['rate']:>4.0%} "
                     f"均{s['mean']:+6.2f}%" if s else f"{key.upper()} n<3      ")
    print(f"  {label:<26}{len(recs):>4} 条   " + "   ".join(parts))


def perm_diff(a, b, *, trials=20000, seed=5):
    """置换检验：b 组均值是否真的高于 a 组（单尾）。"""
    if len(a) < 3 or len(b) < 3:
        return None
    obs = statistics.mean(b) - statistics.mean(a)
    pool = list(a) + list(b)
    rng = random.Random(seed)
    ge = 0
    for _ in range(trials):
        rng.shuffle(pool)
        if statistics.mean(pool[:len(b)]) - statistics.mean(pool[len(b):]) >= obs:
            ge += 1
    return obs, ge / trials


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thin", action="store_true", help="按品种抽稀成不重叠样本")
    ap.add_argument("--dedupe-row", action="store_true", help="一行只取一个方向")
    ap.add_argument("--exclude", default="", help="逗号分隔的品种，用于查构成偏倚")
    a = ap.parse_args()
    exclude = tuple(x for x in a.exclude.split(",") if x)

    keys = [f[:-5] for f in sorted(os.listdir("data/history/signals"))
            if f.endswith(".json")]
    recs = build(sl.load_all(keys), dedupe_row=a.dedupe_row, exclude=exclude)
    print(f"样本：{len(recs)} 条（压力比已达标）"
          f"　抽稀={'是' if a.thin else '否'}"
          f"　一行单方向={'是' if a.dedupe_row else '否'}"
          f"　剔除={exclude or '无'}")
    print(f"品种构成：{dict(Counter(c['inst'] for c in recs))}")
    print(f"方向构成：{dict(Counter(c['side'] for c in recs))}")
    print(f"D+0/D+1 有值：{sum(1 for c in recs if c['d0'] is not None)}"
          f"/{sum(1 for c in recs if c['d1'] is not None)}\n")

    print("① 主翼比闸门")
    for ok in (True, False):
        show(f"wing_ok={ok}", [c for c in recs if c["wing"] == ok], a.thin)
    print("\n② 净建仓规模闸门")
    for ok in (True, False):
        show(f"scale_ok={ok}", [c for c in recs if c["scale"] == ok], a.thin)
    print("\n③ 逆向价格闸门（前三条全过之后）")
    base = [c for c in recs if c["wing"] and c["scale"]]
    for ok in (True, False):
        show(f"contra_ok={ok}", [c for c in base if c["contra"] == ok], a.thin)
    print("\n④ 最终开火 vs 压力比够但被拦")
    for ok in (True, False):
        show(f"fired={ok}", [c for c in recs if c["fired"] == ok], a.thin)

    print("\n⑤ 显著性（D+1，去趋势）")
    fa = thin([c for c in recs if c["fired"]], "d1", 2) if a.thin else \
        [c["d1"] for c in recs if c["fired"] and c["d1"] is not None]
    fb = thin([c for c in recs if not c["fired"]], "d1", 2) if a.thin else \
        [c["d1"] for c in recs if not c["fired"] and c["d1"] is not None]
    if len(fa) >= 3 and len(fb) >= 3:
        t = sl.welch_t(fa, fb)
        print(f"  开火 n={len(fa)} 均 {statistics.mean(fa):+.2f}%　"
              f"被拦 n={len(fb)} 均 {statistics.mean(fb):+.2f}%")
        print(f"  Welch t = {t:+.3f}　（模块标准 |t|≥2 才算有区分度）"
              f"→ {'达标' if abs(t) >= 2 else '未达标'}")
        pd = perm_diff(fa, fb)
        if pd:
            print(f"  置换检验（被拦组更好）：差 {pd[0]:+.2f}pp　p={pd[1]:.3f}")
    else:
        print("  样本不足，不出结论")

    print("\n⑥ 净建仓规模闸门是否形同虚设")
    n_block = sum(1 for c in recs if not c["scale"])
    print(f"  scale_ok=False 的记录数：{n_block} / {len(recs)}"
          f"　→ {'【从未拦过，是摆设】' if n_block == 0 else '有实际拦截'}")


if __name__ == "__main__":
    main()
