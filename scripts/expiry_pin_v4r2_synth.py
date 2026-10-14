"""expiry-pin-v4r1 修订（Codex 045 包 D）的纯合成校准 r3：只用模拟价格，不读任何真实快照、日线或私有数据。

相对 r1/r2（scripts/expiry_pin_v4_synth.py，保留不动，供旧产物复现）修了生成器口径：
- 请求周数 = 实际周数：暖启动（前 40 个交易日）只用于 ATR，不进样本；行按决策日 S 所在周归周，周号 0..W−1。
- ATR14 = 真实波幅（True Range，含日内高低）：每日 4 个子步模拟日内路径，取高低。
- 墙：近价带（|K − r| ≤ 1 ATR）内每个挂牌行权价以 0.75 概率有 OI（指数分布），有 OI 的行权价少于 3 个 → unmatched；
  K* = 带内 OI 最大者。墙恰在参考价 → side 0，不进主统计量。
- 两种设计：
  fri（v4r1 修订主设计）：周五到期、h = 2、周三决策、参考价 = 周二收盘 → 收益窗口完整落在同一周内；GLD、SLV 各一行/周。
  all（r2 原设计，修口径后按原场景重跑）：GLD 每日到期、SLV 一三五到期，S = E 前 2 个交易日。
- 主估计量：pin_stat.distance_matched_weights（品种 × 墙距档内两侧配对、金银等权）+ signflip_weighted。
零假设情景在 r2 四个之外，加入 Codex 045 要求的依赖与不平衡：墙距×侧不平衡+漂移、相邻周共享冲击、周收益自相关、
波动聚集、杠杆（下跌周后波动放大）、漂移状态切换、墙追随上周走势、随机缺周、按波动缺周。
核宽 0.25 ATR 沿用 r1 的事前选择，这里换新种子只验证、不重新挑选。

  python3 scripts/expiry_pin_v4r2_synth.py --out data/backtest/expiry_pin_v4_synth/<run>   # 目录已存在则拒绝
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

SEED = 20261015            # 与 r1/r2（20261014）不同：核宽等设计选择在新种子上验证
H = 2
WARM = 40                  # 暖启动交易日（8 周），只供 ATR
SUB = 4                    # 每日子步数（日内高低）
SG, SS, RHO = 0.012, 0.020, 0.85


def _t4(rng):
    """单位方差 t(4)。"""
    z = rng.gauss(0, 1)
    chi = sum(rng.gauss(0, 1) ** 2 for _ in range(4))
    return z / math.sqrt(chi / 4) / math.sqrt(2.0)


def _paths(weeks: int, sc: dict, rng: random.Random):
    """GLD/SLV 日线（收盘、高、低）。sc 键：drift（日 sd 单位）、skew、garch、leverage、momentum、regime、paired_shock。"""
    n = WARM + weeks * 5 + 5
    out = {}
    zs_g, zs_s = [], []
    vol = [1.0] * n
    regime = 1
    wk_ret = 0.0
    shock = {}
    for d in range(n):
        wk = (d - WARM) // 5 if d >= WARM else None
        if sc.get("garch") and d > 0:                       # 波动聚集：σ² = .1 + .1 z² + .8 σ²（无条件方差 1）
            vol[d] = math.sqrt(0.1 + 0.1 * (zs_g[-1] * vol[d - 1]) ** 2 + 0.8 * vol[d - 1] ** 2)
        if sc.get("leverage") and d % 5 == 0 and d >= 5:    # 上周下跌 → 本周波动 ×1.5（杠杆效应）
            wk_ret = sum(zs_g[d - 5:d])
        if sc.get("leverage") and d >= 5:
            vol[d] = 1.5 if wk_ret < 0 else 1.0
        zg = _t4(rng)
        zs = RHO * zg + math.sqrt(1 - RHO ** 2) * _t4(rng)
        mu = sc.get("drift", 0.0)
        if sc.get("skew"):
            j = -3.0 if rng.random() < 0.05 else 0.0
            zg += j + 0.15; zs += j + 0.15
        if sc.get("regime") and d % 5 == 0 and rng.random() < 0.1:
            regime = -regime                                 # 漂移状态切换（周级马尔可夫）
        if sc.get("regime"):
            mu = 0.15 * regime
        if sc.get("momentum") and d % 5 == 0 and d >= 5:     # 周收益自相关 ρ：本周漂移 = ρ × 上周标准化周收益 / 5
            wk_ret = sum(zs_g[d - 5:d]) / math.sqrt(5)
        if sc.get("momentum") and d >= 5:
            mu = sc["momentum"] * wk_ret / math.sqrt(5)
        if sc.get("paired_shock") and wk is not None and d % 5 == 3:   # 相邻两周（2k, 2k+1）共享同一个对称冲击（周四）
            pair = wk // 2
            if pair not in shock:
                shock[pair] = rng.choice((-1.0, 1.0)) * sc["paired_shock"]
            zg += shock[pair]; zs += shock[pair]
        zs_g.append(zg); zs_s.append(zs)
        for name, z, sg in (("GLD", zg, SG), ("SLV", zs, SS)):
            c0 = out.setdefault(name, {"c": [380.0 if name == "GLD" else 54.0], "h": [None], "l": [None]})
            p = c0["c"][-1]
            hi = lo = p
            step = sg * vol[d] / math.sqrt(SUB)
            # 子步：前 SUB−1 步为桥式噪声，最后一步落在日收盘（日收益 = sg·vol·(z + mu)）
            target = p * (1 + sg * vol[d] * (z + mu))
            x = p
            for k in range(1, SUB):
                x = p + (target - p) * k / SUB + p * step * rng.gauss(0, 1) * math.sqrt(k * (SUB - k) / SUB)
                hi, lo = max(hi, x), min(lo, x)
            hi, lo = max(hi, target), min(lo, target)
            c0["c"].append(target); c0["h"].append(hi); c0["l"].append(lo)
    return out


def _atr(px, i):
    """S−1 = i 及以前 14 日的真实波幅均值。"""
    tr = [max(px["h"][j], px["c"][j - 1]) - min(px["l"][j], px["c"][j - 1]) for j in range(i - 13, i + 1)]
    return sum(tr) / 14


def simulate(weeks: int, sc: dict, rng: random.Random, design: str) -> tuple[list[dict], dict]:
    px = _paths(weeks, sc, rng)
    rows, unmatched = [], {}
    grids = {"GLD": 1.0, "SLV": 0.5}
    drop = set()
    if sc.get("missing_frac"):
        drop = {w for w in range(weeks) if rng.random() < sc["missing_frac"]}
    for name in ("GLD", "SLV"):
        p = px[name]
        c = p["c"]                                           # c[k] = 第 k−1 个交易日的收盘（c[0] 为起点）
        n = len(c) - 1
        exp_days = (4,) if design == "fri" else ((0, 1, 2, 3, 4) if name == "GLD" else (0, 2, 4))
        for e in range(WARM + H, n):                         # e = 到期交易日序号（0 起），收盘 c[e+1]
            if e % 5 not in exp_days:
                continue
            S = e - H
            wk = (S - WARM) // 5
            if S < WARM or wk >= weeks or wk in drop:
                continue
            ref = c[S]                                       # 参考价 = S−1 的收盘
            atr = _atr(p, S)
            if sc.get("missing_highvol") and atr / ref > sc["missing_highvol"] * (SG if name == "GLD" else SS):
                unmatched["高波动缺周（事前 ATR）"] = unmatched.get("高波动缺周（事前 ATR）", 0) + 1
                continue
            g = grids[name]
            strikes = [k * g for k in range(math.ceil((ref - atr) / g), math.floor((ref + atr) / g) + 1)]
            oi = {k: (rng.expovariate(1.0) if rng.random() < 0.75 else 0.0) for k in strikes}
            ce = c[e + 1]
            if sc.get("side_bias"):                          # 下方行权价 OI 放大 → 墙多在下方
                oi = {k: v * (sc["side_bias"] if k < ref else 1.0) for k, v in oi.items()}
            if sc.get("feedback"):                           # 墙追随上周走势（趋势追随的持仓）
                last = c[S] - c[max(S - 5, 1)]
                oi = {k: v * (3.0 if (k - ref) * last > 0 else 1.0) for k, v in oi.items()}
            live = [k for k, v in oi.items() if v > 0]
            if len(live) < 3:
                unmatched["带内有 OI 的行权价 < 3"] = unmatched.get("带内有 OI 的行权价 < 3", 0) + 1
                continue
            if sc.get("informed"):
                k_star = min(live, key=lambda k: abs(k - ce) + rng.random() * atr * 0.5)
            elif sc.get("dist_imbalance"):                   # Codex 045：上方墙取最近、下方墙取最远
                above = [k for k in live if k > ref]
                below = [k for k in live if k < ref]
                if above and below:
                    k_star = min(above) if rng.random() < 0.5 else min(below)
                else:
                    k_star = max(live, key=lambda k: oi[k])
            else:
                k_star = max(live, key=lambda k: (oi[k], -abs(k - ref), -k))
            lam = sc.get("pin", 0.0)
            if lam and abs(ce - k_star) <= atr:
                ce = ce + lam * (k_star - ce)
            if sc.get("pin_prob") and abs(ce - k_star) <= atr and rng.random() < sc["pin_prob"]:
                ce = k_star
            side = (k_star > ref) - (k_star < ref)
            rows.append({"inst": name, "week": wk, "side": side, "dist": abs(k_star - ref) / atr,
                         "m": ps.mirror_k(ce, ref, k_star, atr)})
    return rows, unmatched


def run(name: str, sc: dict, *, weeks: int, n_sims: int, seed: int, design: str) -> dict:
    rng = random.Random(f"{seed}|{design}|{name}|{weeks}")
    det = rev = unid = 0
    nw, nr, used, below = [], [], [], []
    um: dict = {}
    for i in range(n_sims):
        rows, u = simulate(weeks, sc, rng, design)
        for k, v in u.items():
            um[k] = um.get(k, 0) + v
        dm = ps.distance_matched_weights(rows)
        if not dm["identifiable"]:
            unid += 1; continue
        r = ps.signflip_weighted(rows, dm["w"], n_perm=2000, seed=seed + i)
        if r["p_lower"] is None:
            unid += 1; continue
        nw.append(r["n_weeks"]); nr.append(len(rows)); used.append(sum(1 for x in dm["w"] if x))
        sd = [x["side"] for x in rows if x["side"]]
        below.append(sum(1 for x in sd if x < 0) / len(sd) if sd else 0.0)
        det += r["p_lower"] < 0.025
        rev += r["p_upper"] < 0.025
    k = n_sims - unid
    se = lambda p: round(math.sqrt(p * (1 - p) / k), 4) if k else None
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    return {"scenario": name, "design": design, "params": sc, "weeks_requested": weeks, "n_sims": n_sims,
            "weeks_actual_mean": avg(nw), "weeks_actual_max": max(nw) if nw else None, "rows_mean": avg(nr),
            "rows_used_mean": avg(used), "wall_below_share": avg(below), "unmatched_total": um,
            "detect_rate": round(det / k, 4) if k else None, "detect_mc_se": se(det / k) if k else None,
            "reverse_rate": round(rev / k, 4) if k else None, "unidentifiable": unid}


NULLS = {  # r2 原四个（参数不变）+ Codex 045 要求的依赖与不平衡
    "base_t4": {}, "drift_+0.1sd/day": {"drift": 0.1}, "side_bias_belowOIx2.3+drift": {"side_bias": 2.3, "drift": 0.1},
    "neg_skew": {"skew": True},
    "dist_imbalance+drift": {"dist_imbalance": True, "drift": 0.1},
    "paired_week_shock_1sd": {"paired_shock": 1.0},
    "weekly_momentum_0.2": {"momentum": 0.2},
    "garch_vol_cluster": {"garch": True},
    "leverage_vol": {"leverage": True},
    "regime_drift_switch": {"regime": True},
    "wall_follows_last_week": {"feedback": True},
    "wall_follows+momentum": {"feedback": True, "momentum": 0.2},
    "missing_20%_weeks": {"missing_frac": 0.2},
    "missing_highvol": {"missing_highvol": 1.4},                # 事前 ATR/价 > 1.4 × 该品种日 sd → 缺
}
ALTS = ({"pin": 0.1}, {"pin": 0.2}, {"pin": 0.3}, {"pin_prob": 0.05}, {"pin_prob": 0.10})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--power-sims", type=int, default=200)
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        print(f"拒绝覆盖已存在的 {out}"); return 1
    res = {"schema": 1, "run": "r3", "seed": SEED, "h": H, "warmup_days": WARM, "estimator": "distance_matched_weights + signflip_weighted",
           "kernel_bw_atr": ps.KERNEL_BW_ATR, "dist_bins_atr": list(ps.DIST_BINS_ATR), "null": [], "power": [], "informed_wall_not_null": []}
    for design in ("fri", "all"):
        for name, sc in NULLS.items():
            r = run(name, sc, weeks=13, n_sims=a.sims, seed=SEED, design=design)
            res["null"].append(r); print(json.dumps(r, ensure_ascii=False), flush=True)
        for alt in ALTS:
            for weeks in (13, 24, 36, 52):
                key = "_".join(f"{k}{v}" for k, v in alt.items())
                r = run(key, alt, weeks=weeks, n_sims=a.power_sims, seed=SEED, design=design)
                res["power"].append(r); print(json.dumps(r, ensure_ascii=False), flush=True)
        r = run("informed_wall", {"informed": True}, weeks=13, n_sims=a.power_sims, seed=SEED, design=design)
        res["informed_wall_not_null"].append(r); print(json.dumps(r, ensure_ascii=False), flush=True)
    src = [ROOT / "undertow/analyze/pin_stat.py", Path(__file__)]
    res["code_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in src}
    res["git_head"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    out.mkdir(parents=True)
    (out / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    print(f"→ {out}/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
