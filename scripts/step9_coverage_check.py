"""S03 追加（Codex 008 v5 复核）：W04 的 Garwood 区间在【二项】破墙事件下的实际覆盖率。

Garwood 是 Poisson 计数的精确区间；W04 的破墙是固定 n 行的 Bernoulli（各行概率 θ·F_i），不是 Poisson。
这里不宣称「精确二项覆盖」，而是逐一枚举：给定 n、F_i、真实比值 θ，按 Poisson-二项分布精确算出
P(θ 落在 Garwood 区间内)。只检查观测部分；与基准 bootstrap 的合成区间是最不利组合，覆盖只会更高或相同
（区间更宽），但那不是精确联合覆盖，这里不对它下结论。

用法：python3 scripts/step9_coverage_check.py [--emit PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
from step9_wall_hold_rates import garwood  # noqa: E402


def poisson_binomial(ps: list[float]) -> list[float]:
    """P(K=k)，K = Σ Bernoulli(p_i)。动态规划，精确。"""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nxt[k] += v * (1 - p)
            nxt[k + 1] += v * p
        dist = nxt
    return dist


def coverage(Fs: list[float], theta: float, alpha: float = 0.05) -> float | None:
    ps = [theta * f for f in Fs]
    if any(p > 1 for p in ps):
        return None
    E = sum(Fs)
    cov = 0.0
    for k, pk in enumerate(poisson_binomial(ps)):
        lo, hi = garwood(k, E, alpha)
        if lo is not None and lo <= theta <= hi:
            cov += pk
    return cov


def grid():
    rows = []
    for n in (5, 10, 20, 40):
        for F in (0.05, 0.1, 0.2, 0.3, 0.5):
            for th in (0.3, 0.5, 0.7, 1.0, 1.3, 1.6):
                c = coverage([F] * n, th)
                if c is not None:
                    rows.append({"n": n, "F": F, "theta": th, "E": round(n * F, 3), "coverage": round(c, 4),
                                 "kind": "homogeneous"})
    # 异质：W04 各行缓冲不同 → F_i 不同
    for n in (10, 20):
        Fs = [0.05 + 0.4 * i / (n - 1) for i in range(n)]
        for th in (0.5, 1.0, 1.5):
            c = coverage(Fs, th)
            if c is not None:
                rows.append({"n": n, "F": "0.05~0.45", "theta": th, "E": round(sum(Fs), 3),
                             "coverage": round(c, 4), "kind": "heterogeneous"})
    return rows


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", type=Path)
    a = ap.parse_args()
    rows = grid()
    mn = min(rows, key=lambda r: r["coverage"])
    below = [r for r in rows if r["coverage"] < 0.95]
    print(f"Garwood（Poisson 精确 95%）在二项/Poisson-二项破墙下的枚举覆盖率：{len(rows)} 个格")
    print(f"  最低覆盖 {mn['coverage']:.4f}（n={mn['n']}, F={mn['F']}, θ={mn['theta']}）；低于 0.95 的格 {len(below)} 个")
    for r in below[:10]:
        print(f"    {r}")
    out = {"schema": 1, "identity": "Poisson 精确区间用于二项事件：覆盖率由枚举给出，不宣称精确二项覆盖",
           "alpha": 0.05, "n_cells": len(rows), "min_coverage": mn, "n_below_nominal": len(below), "rows": rows}
    if a.emit:
        a.emit.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        print(f"已落盘 {a.emit}")
    return out


if __name__ == "__main__":
    main()
