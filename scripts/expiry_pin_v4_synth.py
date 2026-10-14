"""expiry-pin-v4r1 草案的纯合成校准（Codex 044 E5）：只用模拟价格，不读任何真实快照、日线或私有数据。

回答两个问题，供冻结前审定：
1. 无墙（零假设成立）时，镜像统计量 + 循环移动周块 bootstrap 的 95% 区间是否大致覆盖 0、假检出率是否接近 2.5%：
   厚尾、漂移、墙侧不平衡+漂移、负偏、离散行权价网格、GLD/SLV 相关、h=2 持有窗跨周重叠；块长 1 周 vs 2 周对比。
2. 有磁吸（到期收盘被拉向墙 λ 比例）时，13 / 24 / 52 周各有多少检出功效。
另外数值复现 Codex 044 的反例：旧 u 差统计量在全局波动错配下 d = −.1025，镜像统计量为 0。
「知情墙」（墙放在价格将要去的方向）不是零假设，单列，用来说明检出 ≠ 磁吸因果。

  python3 scripts/expiry_pin_v4_synth.py --out data/backtest/expiry_pin_v4_synth/<run>   # 目录已存在则拒绝
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import pin_stat as ps  # noqa: E402

SEED = 20261014
H = 2


def _t4(rng):
    """单位方差的 t(4)。"""
    z = rng.gauss(0, 1)
    chi = sum(rng.gauss(0, 1) ** 2 for _ in range(4))
    return z / math.sqrt(chi / 4) / math.sqrt(2.0)


def simulate(weeks: int, sc: dict, rng: random.Random) -> list[dict]:
    """一条合成样本：GLD（每日到期、$1 网格）与 SLV（一三五到期、$0.5 网格），日收益相关 0.85、t(4) 厚尾。"""
    n = weeks * 5 + 40
    g, s = [380.0], [54.0]
    sg, ss = 0.012, 0.020
    for _ in range(n):
        zg = _t4(rng)
        zs = 0.85 * zg + math.sqrt(1 - 0.85 ** 2) * _t4(rng)
        if sc.get("skew") and rng.random() < 0.05:
            zg -= 3.0; zs -= 3.0
        if sc.get("skew"):
            zg += 0.15; zs += 0.15                               # 负偏但均值 0
        mu = sc.get("drift", 0.0)
        g.append(g[-1] * (1 + sg * (zg + mu))); s.append(s[-1] * (1 + ss * (zs + mu)))
    rows = []
    for name, px, grid, days in (("GLD", g, 1.0, range(5)), ("SLV", s, 0.5, (0, 2, 4))):
        for e in range(40, n + 1):
            if (e % 5) not in days:
                continue
            S = e - H
            ref = px[S - 1]
            atr = sum(abs(px[i] - px[i - 1]) for i in range(S - 14, S)) / 14
            lo, hi = math.ceil((ref - atr) / grid), math.floor((ref + atr) / grid)
            strikes = [k * grid for k in range(lo, hi + 1)]
            if not strikes:
                continue
            ce = px[e]
            if sc.get("informed"):                               # 墙放在价格将要去的方向（不是零假设）
                k_star = min(strikes, key=lambda k: abs(k - ce) + rng.random() * atr * 0.5)
            elif sc.get("side_bias"):
                below = [k for k in strikes if k < ref] or strikes
                above = [k for k in strikes if k > ref] or strikes
                k_star = rng.choice(below if rng.random() < sc["side_bias"] else above)
            else:
                k_star = rng.choice(strikes)
            lam = sc.get("pin", 0.0)
            if lam and abs(ce - k_star) <= atr:                  # 模型甲：1 ATR 内按比例拉向墙
                ce = ce + lam * (k_star - ce)
            if sc.get("pin_prob") and abs(ce - k_star) <= atr and rng.random() < sc["pin_prob"]:
                ce = k_star                                      # 模型乙：1 ATR 内以概率 π 恰收在墙上（点质量）
            side = (k_star > ref) - (k_star < ref)
            m = ps.mirror_k(ce, ref, k_star, atr) if sc.get("_stat") == "kernel" else ps.mirror_m(ce, ref, k_star, atr)
            rows.append({"inst": name, "week": (S - 40) // 5, "side": side, "m": m})
    return rows


def run_signflip(name: str, sc: dict, *, weeks: int, n_sims: int, seed: int) -> dict:
    """周级符号翻转检验的假检出率（零假设下应 ≈ 2.5%）与功效。"""
    rng = random.Random(f"{seed}|sf|{name}|{weeks}")
    det = rev = unid = 0
    for i in range(n_sims):
        r = ps.signflip_test(simulate(weeks, sc, rng), n_perm=2000, seed=seed + i)
        v = ps.verdict_signflip(r)
        unid += v == "本数据不能识别"; det += v.startswith("收盘离墙"); rev += v == "收盘反而离墙更远"
    k = n_sims - unid
    se = lambda p: round(math.sqrt(p * (1 - p) / k), 4) if k else None
    return {"scenario": name, "test": "signflip_week", "params": sc, "weeks": weeks, "n_sims": n_sims,
            "detect_rate": round(det / k, 4) if k else None, "detect_mc_se": se(det / k) if k else None,
            "reverse_rate": round(rev / k, 4) if k else None, "unidentifiable": unid}


def run_scenario(name: str, sc: dict, *, weeks: int, n_sims: int, n_boot: int, block: int, seed: int) -> dict:
    rng = random.Random(f"{seed}|{name}|{weeks}|{block}")
    cover = det = rev = unid = 0
    ests = []
    for i in range(n_sims):
        rows = simulate(weeks, sc, rng)
        ci = ps.block_bootstrap(rows, block_weeks=block, n_boot=n_boot, seed=seed + i)
        v = ps.verdict(ci)
        if ci["est"] is not None:
            ests.append(ci["est"])
        if ci["lo"] is None:
            unid += 1; continue
        cover += ci["lo"] <= 0 <= ci["hi"]
        det += ci["hi"] < 0
        rev += ci["lo"] > 0
    k = n_sims - unid
    mc = lambda p: round(math.sqrt(p * (1 - p) / k), 4) if k else None    # Monte Carlo 误差
    return {"scenario": name, "params": sc, "weeks": weeks, "block_weeks": block, "n_sims": n_sims, "n_boot": n_boot,
            "mean_est": round(sum(ests) / len(ests), 5) if ests else None,
            "coverage_of_0": round(cover / k, 4) if k else None, "coverage_mc_se": mc(cover / k) if k else None,
            "detect_rate": round(det / k, 4) if k else None, "reverse_rate": round(rev / k, 4) if k else None,
            "unidentifiable": unid}


def codex_counterexample(n: int = 100000) -> dict:
    """Codex 044：真实偏移 U[−.5,.5]，错误零模型 U[−1,1]，K* = +.10、安慰剂 Kp = +.35（ATR 单位，参考价 0）。"""
    def u(k, c):                                                  # 零模型 U[−1,1] 下 P(|X−k| ≤ |c−k|)
        r = abs(c - k)
        return (min(1, k + r) - max(-1, k - r)) / 2
    cs = [-0.5 + (i + 0.5) / n for i in range(n)]
    uk = sum(u(0.10, c) for c in cs) / n
    up = sum(u(0.35, c) for c in cs) / n
    mm = sum(ps.mirror_m(c, 0.0, 0.10, 1.0) for c in cs) / n
    return {"mean_u_kstar": round(uk, 4), "mean_u_placebo": round(up, 4), "old_d": round(uk - up, 4),
            "mirror_mean_m": round(mm, 6)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--boot", type=int, default=600)
    ap.add_argument("--signflip", action="store_true", help="r2：只跑周级符号翻转检验的校准")
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        print(f"拒绝覆盖已存在的 {out}"); return 1
    nulls = {"base_t4": {}, "drift_+0.1sd/day": {"drift": 0.1}, "side_bias_70%below+drift": {"side_bias": 0.7, "drift": 0.1},
             "neg_skew": {"skew": True}}
    res = {"schema": 1, "seed": SEED, "h": H, "codex_044_counterexample": codex_counterexample(), "null": [], "power": [],
           "informed_wall_not_null": None}
    res["informed_wall_not_null"] = []
    for stat in (() if a.signflip else ("kernel", "linear")):
        for name, sc in nulls.items():
            for block in (1, 2):
                r = run_scenario(f"{stat}|{name}", {**sc, "_stat": stat}, weeks=13, n_sims=a.sims, n_boot=a.boot,
                                 block=block, seed=SEED)
                res["null"].append(r); print(json.dumps(r, ensure_ascii=False))
        for alt in ({"pin": 0.1}, {"pin": 0.2}, {"pin": 0.3}, {"pin_prob": 0.05}, {"pin_prob": 0.10}):
            for weeks in (13, 24, 52):
                key = "_".join(f"{k}{v}" for k, v in alt.items())
                r = run_scenario(f"{stat}|{key}", {**alt, "_stat": stat}, weeks=weeks, n_sims=max(100, a.sims // 2),
                                 n_boot=a.boot, block=2, seed=SEED)
                res["power"].append(r); print(json.dumps(r, ensure_ascii=False))
        res["informed_wall_not_null"].append(run_scenario(f"{stat}|informed_wall", {"informed": True, "_stat": stat}, weeks=13,
                                                          n_sims=max(100, a.sims // 2), n_boot=a.boot, block=2, seed=SEED))
    if a.signflip:                                           # r2：主检验改为周级符号翻转（r1 显示 bootstrap 区间欠覆盖）
        res = {"schema": 1, "seed": SEED, "h": H, "test": "signflip_week", "null": [], "power": []}
        for name, sc in nulls.items():
            r = run_signflip(f"kernel|{name}", {**sc, "_stat": "kernel"}, weeks=13, n_sims=a.sims, seed=SEED)
            res["null"].append(r); print(json.dumps(r, ensure_ascii=False))
        for alt in ({"pin": 0.1}, {"pin": 0.2}, {"pin": 0.3}, {"pin_prob": 0.05}, {"pin_prob": 0.10}):
            for weeks in (13, 24, 52):
                key = "_".join(f"{k}{v}" for k, v in alt.items())
                r = run_signflip(f"kernel|{key}", {**alt, "_stat": "kernel"}, weeks=weeks, n_sims=max(100, a.sims // 2), seed=SEED)
                res["power"].append(r); print(json.dumps(r, ensure_ascii=False))
    src = [ROOT / "undertow/analyze/pin_stat.py", Path(__file__)]
    res["code_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in src}
    res["git_head"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    out.mkdir(parents=True)
    (out / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    print(f"→ {out}/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
