"""到期收盘磁吸检验的统计量（expiry-pin-v4r1 草案，待 Codex 审定；纯函数，无 I/O）。

Codex 044 指出 v4 原统计量 d = u(K*) − mean u(K_p) 识别不了：零模型整体估错波动（真实 U[−.5,.5]、零模型 U[−1,1]）、
没有任何墙力时，墙距 .10 / 安慰剂距 .35 就给出 d = −.1025 的「假磁吸」。安慰剂相减不能一般性抵消全局波动错配。

v4r1 改用【镜像点】：参考价 r = C[S−1]，墙 K* 关于 r 的镜像 K_m = 2r − K*。行统计量（ATR 标准化）
    m = (|C_E − K*| − |C_E − K_m|) / ATR14
若 C_E − r 的分布关于 0 对称（无漂移；尺度、厚尾、波动估多估少都不影响），E[m] = 0 恰好成立 —— 不需要零模型，
也就没有「零模型波动估错」这一条路。m < 0 = 收盘离墙比离镜像点近。收盘恰在墙上时线性 m = −2|K* − r|/ATR。
- 漂移：上方墙距 d 与下方墙距 d 的两行，在同一偏移分布下期望恰好互为相反数（m(x; −d) = −m(x; +d)），所以
  【同距离】两侧等权在任意漂移下都抵消；距离不同则不抵消（Codex 045：上墙 .1 ATR、下墙 .8 ATR、偏移 U[−.5,.5]+.1 →
  等权核均值 +.0266）。v4r1 修订：按事前墙距分档配对两侧（distance_matched_weights），没有两侧的档不进主统计量。
  这只消除「两侧墙距不同」这一条；两侧在波动、趋势环境上若系统不同，仍可能残留，由合成情景量化。
- |K* − r| 很小的行（墙贴着参考价）m 恒近 0，不提供信息：检验对象是「偏离参考价的墙」，这一点要在结论里写明。
- 镜像点不是挂牌行权价、不需要 OI 条件，所以不会像同侧安慰剂那样系统性排除低 ATR 日。
- 检出也只是「收盘离墙更近」的关联，不是因果：OI 也可能是知情资金押在了价格要去的地方。

区间：按决策日 S 所在日历周分组，循环移动块 bootstrap（块长按周，事先固定），保住相邻周之间 h 日持有窗的重叠。
"""
from __future__ import annotations

import math
import random


def mirror_m(close_e: float, ref: float, k_star: float, atr: float) -> float | None:
    """一行的镜像统计量；任一输入非有限或 ATR ≤ 0 → None。"""
    xs = (close_e, ref, k_star, atr)
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs) or atr <= 0:
        return None
    k_m = 2 * ref - k_star
    return (abs(close_e - k_star) - abs(close_e - k_m)) / atr


KERNEL_BW_ATR = 0.25     # 核宽（ATR 单位）：冻结前由纯合成功效比较定，不看真实数据；未用真实结果校准


def mirror_k(close_e: float, ref: float, k_star: float, atr: float, bw: float = KERNEL_BW_ATR) -> float | None:
    """核版镜像统计量：m_k = φ(C_E − K_m) − φ(C_E − K*)，φ(x) = exp(−x²/2(bw·ATR)²)。
    同样只靠对称性：C_E − r 关于 0 对称时 E[m_k] = 0 恰好成立（与尺度、厚尾、零模型无关）。
    收盘远离墙与镜像点时两项都≈0，不像线性版那样被「落在哪一侧」的方向噪声主导；收盘贴近墙 → m_k ≈ −1。"""
    xs = (close_e, ref, k_star, atr, bw)
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs) or atr <= 0 or bw <= 0:
        return None
    s = bw * atr
    k_m = 2 * ref - k_star
    return math.exp(-((close_e - k_m) ** 2) / (2 * s * s)) - math.exp(-((close_e - k_star) ** 2) / (2 * s * s))


def side_balanced_mean(rows: list[dict]) -> float | None:
    """rows：{"m", "side"(+1 墙在参考价上方 / −1 下方)}。两侧各自均值的等权平均；任一侧没有行 → None（不退回单侧）。"""
    up = [r["m"] for r in rows if r["side"] > 0]
    dn = [r["m"] for r in rows if r["side"] < 0]
    if not up or not dn:
        return None
    return (sum(up) / len(up) + sum(dn) / len(dn)) / 2


def _week_sums(rows: list[dict]) -> dict:
    """每周 (上方和, 上方数, 下方和, 下方数)；side == 0（墙恰在参考价）不进任何一侧。"""
    out: dict = {}
    for r in rows:
        s = out.setdefault(r["week"], [0.0, 0, 0.0, 0])
        if r["side"] > 0:
            s[0] += r["m"]; s[1] += 1
        elif r["side"] < 0:
            s[2] += r["m"]; s[3] += 1
    return out


def _balanced(sums: list) -> float | None:
    su, nu, sd, nd = (sum(x[i] for x in sums) for i in range(4))
    return None if nu == 0 or nd == 0 else (su / nu + sd / nd) / 2


def block_bootstrap(rows: list[dict], *, block_weeks: int, n_boot: int, seed: int) -> dict:
    """rows 需带 "week"（决策日 S 所在周的序号）、"side"、"m"。统计量固定为 side_balanced_mean。
    循环移动块：随机起点、连续 block_weeks 周为一块，拼到与原周数相同为止。
    返回 {"est", "lo", "hi", "n_weeks", "n_rows", "n_valid"}；周数 < 2 × 块长 → 不给区间；
    超过 10% 的重采样缺一侧 → 不给区间。"""
    ws = _week_sums(rows)
    weeks = sorted(ws)
    est = side_balanced_mean(rows)
    out = {"est": est, "lo": None, "hi": None, "n_weeks": len(weeks), "n_rows": len(rows), "n_valid": 0}
    W = len(weeks)
    if est is None or W < 2 * block_weeks:
        return out
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        sample = []
        while len(sample) < W:
            s = rng.randrange(W)
            sample += [ws[weeks[(s + j) % W]] for j in range(block_weeks)]
        v = _balanced(sample[:W])
        if v is not None:
            vals.append(v)
    if len(vals) < 0.9 * n_boot:
        return {**out, "n_valid": len(vals)}
    vals.sort()
    q = lambda p: vals[min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))]
    return {**out, "lo": q(0.025), "hi": q(0.975), "n_valid": len(vals)}


DIST_BINS_ATR = (0.0, 0.25, 0.5, 1.0 + 1e-9)   # 墙距 |K* − r| / ATR 的分档（事前固定；1 ATR 近价带的上沿含在末档）


def _bin(d: float) -> int | None:
    for i in range(len(DIST_BINS_ATR) - 1):
        if DIST_BINS_ATR[i] <= d < DIST_BINS_ATR[i + 1]:
            return i
    return None


def distance_matched_weights(rows: list[dict]) -> dict:
    """v4r1 修订主估计量（Codex 045 §6/§8）的行权重；权重只由事前协变量（品种、墙侧、墙距档）决定，不看结果。
    每个品种 × 墙距档：两侧都有行 → 档估计 = (上方均值 + 下方均值) / 2；品种估计 = 各完整档按行数加权；
    总估计 = 各品种等权平均（为回答「金银」，不按挂牌频率加权）。缺侧的档不进；任一品种没有完整档 → 不可识别（不退成单品种）。
    rows 需带 "inst"、"side"、"dist"（|K* − r| / ATR）。返回 {"w": 每行权重（与 rows 对齐，0 = 不进）, "identifiable",
    "per_inst": {品种: 完整档数、所用行数}, "excluded": {原因: 行数}}。"""
    insts = sorted({r["inst"] for r in rows})
    w = [0.0] * len(rows)
    excl: dict = {}
    per: dict = {}
    for inst in insts:
        cells: dict = {}
        for i, r in enumerate(rows):
            if r["inst"] != inst:
                continue
            if r["side"] == 0:
                excl["墙恰在参考价（side 0）"] = excl.get("墙恰在参考价（side 0）", 0) + 1; continue
            b = _bin(r["dist"])
            if b is None:
                excl["墙距超出近价带"] = excl.get("墙距超出近价带", 0) + 1; continue
            cells.setdefault(b, {1: [], -1: []})[r["side"]].append(i)
        full = {b: c for b, c in cells.items() if c[1] and c[-1]}
        for b, c in cells.items():
            if b not in full:
                excl["该墙距档缺一侧"] = excl.get("该墙距档缺一侧", 0) + len(c[1]) + len(c[-1])
        tot = sum(len(c[1]) + len(c[-1]) for c in full.values())
        per[inst] = {"complete_bins": len(full), "rows_used": tot}
        for c in full.values():
            wb = (len(c[1]) + len(c[-1])) / tot if tot else 0.0
            for sd in (1, -1):
                for i in c[sd]:
                    w[i] = wb * 0.5 / len(c[sd])
    ok = bool(insts) and all(per[k]["complete_bins"] > 0 for k in insts)
    if ok:
        w = [x / len(insts) for x in w]
    return {"w": w if ok else [0.0] * len(rows), "identifiable": ok, "per_inst": per, "excluded": excl}


def signflip_weighted(rows: list[dict], weights: list[float], *, n_perm: int = 20000, seed: int = 20261014,
                      exact_max_weeks: int = 16) -> dict:
    """加权线性统计量 Σ w·m 的周级符号翻转检验；权重只依赖事前协变量，所以翻转某周 ⇔ 该周贡献 c_w 变号。
    【所需的强零假设】（Codex 045 §5）：给定全部事前协变量（墙、墙距、侧、ATR、品种），各周贡献组成的整个向量
    对「各周独立翻转符号」分布不变。仅逐周或逐行边际对称不够 —— 相邻周共享冲击、周收益自相关、波动与上周符号相关
    （杠杆效应）、墙位置追随上周走势，都可能破坏它。exact = 枚举了全部 2^W 种符号，只说明没有抽样误差，不说明零分布有效。
    主设计用「周五到期、h = 2、周三决策」让每行的收益窗口完整落在同一周内，去掉直接的跨周窗口重叠；剩余依赖由合成情景量化。"""
    c: dict = {}
    for r, wt in zip(rows, weights):
        if wt:
            c[r["week"]] = c.get(r["week"], 0.0) + wt * r["m"]
    est = sum(c.values()) if c else None
    out = {"est": est, "p_lower": None, "p_upper": None, "n_weeks": len(c), "exact": False}
    if est is None or len(c) < 2:
        return out
    cs = [c[k] for k in sorted(c)]
    if len(cs) <= exact_max_weeks:
        out["exact"] = True
        vals = [0.0]
        for cw in cs:
            vals = [v + cw for v in vals] + [v - cw for v in vals]
        lo = sum(v <= est + 1e-12 for v in vals); hi = sum(v >= est - 1e-12 for v in vals); tot = len(vals)
    else:
        rng = random.Random(seed)
        lo = hi = 0
        for _ in range(n_perm):
            v = sum(cw if rng.random() < 0.5 else -cw for cw in cs)
            lo += v <= est + 1e-12; hi += v >= est - 1e-12
        lo += 1; hi += 1; tot = n_perm + 1
    out.update(p_lower=lo / tot, p_upper=hi / tot)
    return out


def signflip_test(rows: list[dict], *, n_perm: int = 20000, seed: int = 20261014, exact_max_weeks: int = 16) -> dict:
    """r2 版周级符号翻转（统计量 = 两侧等权、按行加权；保留供 r1/r2 产物复现，v4r1 修订后主检验用 signflip_weighted）。
    Codex 045 §5 更正：原说明「按周翻转得到的就是零分布」过强 —— 需要整个周向量对独立翻转不变，边际对称推不出；
    相邻周共享对称冲击（12 周、6 对）时，名义下尾 2.5% 实际拒绝 7/64 = 10.9%。exact 只表示枚举了全部 2^W 种。
    返回 {"est", "p_lower"(est 这么负或更负的比例), "p_upper", "n_weeks", "exact"}；缺一侧 → est None、p None。"""
    ws = _week_sums(rows)
    weeks = sorted(ws)
    est = _balanced([ws[w] for w in weeks]) if weeks else None
    out = {"est": est, "p_lower": None, "p_upper": None, "n_weeks": len(weeks), "exact": False}
    if est is None or len(weeks) < 2:
        return out
    S = [ws[w] for w in weeks]
    W = len(S)
    nu, nd = sum(x[1] for x in S), sum(x[3] for x in S)
    c = [(x[0] / nu + x[2] / nd) / 2 for x in S]         # 统计量对各周符号是线性的：stat = Σ ±c_w
    if W <= exact_max_weeks:
        out["exact"] = True
        vals = [0.0]
        for cw in c:
            vals = [v + cw for v in vals] + [v - cw for v in vals]
        lo = sum(v <= est + 1e-12 for v in vals); hi = sum(v >= est - 1e-12 for v in vals); tot = len(vals)
    else:
        rng = random.Random(seed)
        lo = hi = 0
        for _ in range(n_perm):
            v = sum(cw if rng.random() < 0.5 else -cw for cw in c)
            lo += v <= est + 1e-12; hi += v >= est - 1e-12
        lo += 1; hi += 1; tot = n_perm + 1                   # 抽样版计入观测值本身
    out.update(p_lower=lo / tot, p_upper=hi / tot)
    return out


def verdict_signflip(r: dict, alpha_each: float = 0.025) -> str:
    """三选一（写死）：单侧各 2.5%。"""
    if r["p_lower"] is None:
        return "本数据不能识别"
    if r["p_lower"] < alpha_each:
        return "收盘离墙比离镜像点近（描述性关联，非因果）"
    if r["p_upper"] < alpha_each:
        return "收盘反而离墙更远"
    return "未检出，也未排除"


def verdict(ci: dict) -> str:
    """三选一（写死）；区间缺失 → 不能识别。"""
    if ci["lo"] is None or ci["hi"] is None:
        return "本数据不能识别"
    if ci["hi"] < 0:
        return "收盘离墙比离镜像点近（描述性关联，非因果）"
    if ci["lo"] > 0:
        return "收盘反而离墙更远"
    return "未检出，也未排除"
