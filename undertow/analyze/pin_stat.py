"""到期收盘磁吸检验的统计量（expiry-pin-v4r1 草案，待 Codex 审定；纯函数，无 I/O）。

Codex 044 指出 v4 原统计量 d = u(K*) − mean u(K_p) 识别不了：零模型整体估错波动（真实 U[−.5,.5]、零模型 U[−1,1]）、
没有任何墙力时，墙距 .10 / 安慰剂距 .35 就给出 d = −.1025 的「假磁吸」。安慰剂相减不能一般性抵消全局波动错配。

v4r1 改用【镜像点】：参考价 r = C[S−1]，墙 K* 关于 r 的镜像 K_m = 2r − K*。行统计量（ATR 标准化）
    m = (|C_E − K*| − |C_E − K_m|) / ATR14
若 C_E − r 的分布关于 0 对称（无漂移；尺度、厚尾、波动估多估少都不影响），E[m] = 0 恰好成立 —— 不需要零模型，
也就没有「零模型波动估错」这一条路。m < 0 = 收盘离墙比离镜像点近。
- 线性漂移会让墙在上方与墙在下方的行朝相反方向偏；主统计量取两侧各自均值的等权平均（side_balanced_mean），一阶抵消。
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


def verdict(ci: dict) -> str:
    """三选一（写死）；区间缺失 → 不能识别。"""
    if ci["lo"] is None or ci["hi"] is None:
        return "本数据不能识别"
    if ci["hi"] < 0:
        return "收盘离墙比离镜像点近（描述性关联，非因果）"
    if ci["lo"] > 0:
        return "收盘反而离墙更远"
    return "未检出，也未排除"
