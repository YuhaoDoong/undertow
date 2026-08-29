"""指标强度 —— 把六组指标各自压成一个【连续】强度值，供回测与加权评分。

**为什么必须做**（用户 2026-08-29）：
「综合评分需要重塑。按各指标强度加权。」

现状的病根：Flow 层权重恒定 0.8，压力比 1.4× 和 53.5× 在综合投票里一样重。
2026-08-28 黄金 53.5× 的极强看跌因此只值 0.8 票，被中期的 +5.3 票压过，
综合输出「偏多」。当天 GLD −3.24%。

设计原则（这三条决定了它不是拍脑袋）：
1. **强度与方向分开**。strength ∈ [0,1] 只说"这层证据有多硬"，
   sign ∈ {-1,0,+1} 只说方向。加权分 = Σ sign × strength × 组权重。
2. **饱和而非线性**。53.5× 不该是 5.3× 的十倍话语权 —— 用 log 压缩后截断，
   否则一个极端读数就能单独决定结论，那和"只看一个指标"没区别。
3. **标定点写死在代码里，不从数据里挑**。每组的 lo/hi 取自该指标自身的
   含义（如压力比 1.3× 是方向裁决的既有门槛、3.0× 是强信号门槛），
   不是回测调出来的 —— 否则就是拿结果拟合参数。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _sat(x: float, lo: float, hi: float) -> float:
    """把 x 从 [lo,hi] 映射到 [0,1]，两端截断。"""
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _log_sat(ratio: float, lo: float, hi: float) -> float:
    """比值型指标用对数刻度：4× 与 2× 的差距，应等同 8× 与 4×。"""
    if ratio <= 0:
        return 0.0
    return _sat(math.log(max(ratio, 1e-9)), math.log(lo), math.log(hi))


@dataclass(frozen=True)
class Strength:
    key: str
    sign: int
    strength: float      # 0~1
    raw: float           # 原始读数，便于审计
    note: str = ""

    @property
    def signed(self) -> float:
        return self.sign * self.strength


# ⚠️⚠️ 这套权重【尚未通过本项目自己的验证门槛】，是探索性的，不是结论。
#
# 2026-08-29 回测（138 个品种日 / **只有 35 个独立日期聚类** / 前瞻 1 日）：
#   💰 增仓  命中 64.3%，日聚类 bootstrap 95% 区间 [54.5%, 73.1%]
#   📈 价格  命中 60.2%，区间 [50.0%, 69.9%]
#   🌊 波动  命中 52.1%，区间 [41.8%, 62.5%]
#
# 我一度据此宣布「增仓已验证有效」并按它分权。**codex 2026-08-29 P1 指出这是错的**：
#   一旦承认跨品种同日高度相关（金银 0.89、QQQ/TQQQ 0.99），有效独立单位就是
#   【日期聚类】而不是品种日。35 < 50，**不满足项目铁律的 n≥50**。
#   继续拿 138 个品种日去凑 n≥50，等于一边承认相关、一边按独立计数。
#   —— 所以 Flow / Vol / Price 三层目前都只能算【探索性观察】，一个都没验证通过。
#
# 另一层问题：权重本身是看着同一批回测的命中结果定的，属于 outcome-informed
# selection（结果驱动的参数选择），即便没做网格搜索也是拟合。
#
# 因此：本权重只用于**并列展示**的实验分，绝不替换现行综合分；
# 需要至少 50 个独立日期聚类 + walk-forward 外样本验证后才谈采纳。
EXPERIMENTAL_UNVALIDATED = True

GROUP_W = {"flow": 1.0, "struct": 0.5, "vol": 0.3,
           "price": 0.7, "cot": 0.5, "macro": 0.5}

# ⚠️ 强度是否该乘进去：**回测不支持**。
#   高强度组 vs 低强度组命中率差 +2.5pp / 0.0pp / −1.2pp，几乎没有区分度；
#   「按强度加权」减「只用方向」的命中率差中位 +3.1pp，但 95% 区间
#   [−3.7pp, +9.7pp] 跨 0；压力比 ≥20× 的 11 个饱和样本命中 6/11 = 55%，
#   反而低于全体的 64.3%。
#   —— 黄金 8/28 是全样本强度第一且命中，但那更像方向对了，不是"极强"带来的。
# 故 USE_STRENGTH 默认 False：只用方向 × 组权重。保留开关是为了继续攒样本对照，
# 不是为了随手打开。
USE_STRENGTH = False

# 标定点：来自各指标自身既有的门槛，不是回测挑的
CAL = {
    # 压力比：1.3× 是 direction.decide 的既有门槛，3.0× 是强信号门槛，
    # 上限 20× —— 再高也不该有更多话语权（黄金 53.5× 会落在饱和区）
    "flow": (1.3, 20.0),
    # 25Δ skew 变化：0.3pp 是 detect_strong_signal 的既有显著门槛
    "vol": (0.3, 2.0),
    # 价格分位：距 50% 中位越远越"拉伸"，70/30 是既有的档位分界
    "price": (10.0, 40.0),
}


def from_flow(fa) -> Strength | None:
    """增仓：压力比（对数饱和）。"""
    up = getattr(fa, "upside_pressure", 0) or 0
    dn = getattr(fa, "downside_pressure", 0) or 0
    if not (up or dn):
        return None
    sign = 1 if up > dn else (-1 if dn > up else 0)
    ratio = max(up, dn) / max(min(up, dn), 1.0)
    lo, hi = CAL["flow"]
    return Strength("flow", sign, _log_sat(ratio, lo, hi), ratio,
                    f"加权增仓比 {ratio:.1f}×")


def from_vol(fa) -> Strength | None:
    """波动率面：25Δ skew 变化。正 = put 变贵 = 看跌。"""
    vs = getattr(fa, "vol", None)
    if vs is None or not getattr(vs, "prev", None):
        return None
    d = getattr(vs, "d_skew25_pp", None)
    if d is None:
        return None
    lo, hi = CAL["vol"]
    sign = -1 if d > 0 else (1 if d < 0 else 0)
    return Strength("vol", sign, _sat(abs(d), lo, hi), d, f"Δskew25 {d:+.2f}pp")


def from_price(stretch) -> Strength | None:
    """价格拉伸：偏离中位多远。超买→看跌，超卖→看涨。"""
    p = getattr(stretch, "pctile", None)
    if p is None:
        return None
    pct = p * 100.0 if p <= 1.0 else p
    dev = abs(pct - 50.0)
    lo, hi = CAL["price"]
    sign = -1 if pct > 50 else (1 if pct < 50 else 0)
    return Strength("price", sign, _sat(dev, lo, hi), pct, f"分位 {pct:.0f}%")


def from_votes(votes, layers, key: str) -> Strength | None:
    """票型层（结构/大资金/宏观）：净票数，3 票封顶。

    这几层本来就是离散投票，没有更细的连续量可用 —— 如实用票数，
    不硬造精度。
    """
    sub = [v for v in votes if getattr(v, "layer", "") in layers]
    if not sub:
        return None
    score = sum(v.weight * v.sign for v in sub)
    sign = 1 if score > 0.3 else (-1 if score < -0.3 else 0)
    return Strength(key, sign, _sat(abs(score), 0.3, 3.0), score,
                    f"净 {score:+.1f} 票")


def collect(outlook, fa=None, stretch=None) -> dict[str, Strength]:
    """一个品种一天的六组强度。缺哪组就少哪组，不补零。"""
    votes = list(getattr(outlook, "votes", None) or [])
    out: dict[str, Strength] = {}
    for key, layers in (("struct", ("Gamma",)), ("cot", ("COT",)), ("macro", ("Macro",))):
        s = from_votes(votes, layers, key)
        if s:
            out[key] = s
    if fa is not None:
        for f in (from_flow(fa), from_vol(fa)):
            if f:
                out[f.key] = f
    s = from_price(stretch)
    if s:
        out["price"] = s
    return out


def weighted_score(st: dict[str, Strength]) -> tuple[float, float]:
    """实验性综合分，以及总权重。

    ⚠️ 名字里的"加权"容易误解：USE_STRENGTH=False 时**完全不使用连续强度**，
    实际行为是「方向 × 组权重后归一」（codex 2026-08-29 P2 指出 docstring 不准）。

    返回 (score, w_total)。score 已按总权重归一，量纲与现行综合分不同，
    **不要直接和旧分比大小** —— 两者是不同的尺子。
    """
    def _v(s: "Strength") -> float:
        # 默认只取方向：强度未被回测支持（见 USE_STRENGTH 上方的说明）
        return s.signed if USE_STRENGTH else float(s.sign)
    num = sum(_v(s) * GROUP_W.get(k, 1.0) for k, s in st.items())
    den = sum(GROUP_W.get(k, 1.0) for k in st)
    return (num / den if den else 0.0), den


def near_weighted(st: dict[str, Strength]) -> float:
    """只用近端四组（增仓/结构/波动/价格）—— 高杠杆短线交易真正吃的那一层。"""
    near = {k: v for k, v in st.items() if k in ("flow", "struct", "vol", "price")}
    return weighted_score(near)[0]
