"""仓位：按 Kelly 算，不按净资产的固定百分比。

用户 2026-08-31：「原本我们的守则是仓位管理风险 10%？但是为了这个 10%，
却可能放弃更优的交易，而选择次优，这反而放大了风险。风险管理得再好，
永远在亏损有啥用呢。」

他说的对，而且这不是主观偏好，是算术：
  净资产 $264 × 10% = $26 —— 买不起任何一张价差（最小占用 $86）。
  能买得起的只有单价 $0.26 以下的期权，而那正是 θ/权利金 >30%、
  点差占比 10~20% 的档位 —— 2026-08-31 那晚四笔全亏，方向基本都对，
  亏在这里。固定百分比对小账户不是风险管理，是【强制选择负期望的工具】。

【正确的框架】先确认策略正期望，再用 Kelly 定仓位：
    f* = (p·b − q) / b       b = 平均盈利率 / 平均亏损率（都相对占用）

2026-08-31 实测（回测反推「赢多少、输多少」）：
    策略        胜率   单笔     赢时    输时     盈亏比b   Kelly  1张占用/净资产
    稳健档      82%  +2.84%  +12%  -38.9%   0.31    24%      36%（超配1.5x）
    激进档      63%  +9.99%  +20%   -7.1%   2.84    50%      45%（≈最优）
    顺向买方    23% +17.80% +250%  -51.6%   4.85     7%      44%（超配6x）

关键在【盈亏比】不在胜率：
  · 稳健档卖得远(墙外2%)，一旦真被打穿就是深度实值，输时亏 38.9%；
  · 激进档买腿保护只在 2.5% 外，破墙也就破一点点，输时只亏 7.1%。
  按平均亏损算耐打程度：稳健档连亏 8 次打光，激进档连亏 32 次。
  真正吃掉小账户的是稳健档，不是激进档 —— 与直觉相反。

⚠️ Kelly 假设期望估计准确。样本 38~60 笔、阈值在同一批数据上选出，
   估计偏乐观。半 Kelly 是常用折中；但对期权价差，1 组是最小不可分单位，
   往往已经超过半 Kelly —— 那时的选择只有「按 1 组做」或「不做」，
   不存在"缩小到半 Kelly"这个选项。
"""
from __future__ import annotations

from dataclasses import dataclass

# 期权价差的最小不可分单位 = 1 组。低于它没有"小一点"这个选项。
MIN_UNIT_NOTE = "期权价差 1 组是最小不可分单位，不存在按比例缩小"


@dataclass(frozen=True)
class KellyResult:
    win_rate: float
    win_roi: float          # 赢时相对占用的收益率
    lose_roi: float         # 输时相对占用的亏损率（正数）
    odds: float             # 盈亏比 b = win_roi / lose_roi（平均赚 ÷ 平均亏）
    kelly: float            # f*
    half_kelly: float
    edge: float             # p·b − q，>0 才有优势

    @property
    def positive_edge(self) -> bool:
        return self.edge > 0


def kelly(win_rate: float, per_trade_pct: float, win_roi_pct: float) -> KellyResult:
    """由「胜率 + 单笔期望 + 赢时收益率」反解输时亏损率，再算 Kelly。

    per_trade_pct = p·win_roi + (1−p)·lose_roi  （都是 %）
    """
    p = max(0.0, min(1.0, win_rate))
    q = 1 - p
    win_roi = win_roi_pct / 100.0
    if q <= 0:
        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
    # codex 2026-08-31 P1-9：原实现用 abs() 把不可能的输入静默修成合法亏损率。
    # E = p·W − q·L 解出的 L 必须为正；为负说明「胜率+期望+赢时收益」三者矛盾
    # （例如声称期望高于全赢），此时应拒绝而不是取绝对值蒙混过去。
    raw = (p * win_roi - per_trade_pct / 100.0) / q
    if raw <= 0:
        return KellyResult(p, win_roi, 0.0, 0.0, 0.0, 0.0, -1.0)
    lose_roi = raw
    if lose_roi <= 0:
        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
    b = win_roi / lose_roi
    edge = p * b - q
    f = edge / b if b > 0 else 0.0
    return KellyResult(p, win_roi, lose_roi, b, max(0.0, f), max(0.0, f / 2), edge)


@dataclass(frozen=True)
class SizeVerdict:
    ok: bool
    n_units: int
    kelly_dollars: float
    unit_occupancy: float
    actual_frac: float      # 实际仓位占净资产
    kelly_frac: float
    over_kelly: float       # 实际 / Kelly，>1 = 超配
    reason: str


# 1 组超过 Kelly 多少倍就必须拒绝。
# codex 2026-08-31 P0-1：原实现里 max_over_kelly 只改文字不构成限制，
# 即使 1 组是 Kelly 的 6 倍（顺向买方档实测 6.2 倍、连亏 3 次概率 45.65%）
# 仍返回 ok=True，「不可分割」被当成了「可以向上取整」的理由 —— 那是鼓励超配。
# 现在设硬上限：≤SOFT 照下并标注超配；SOFT~HARD 之间要显式 allow_over 才放行；
# 超过 HARD 一律拒绝。留 allow_over 是因为用户 2026-08-31 的要求成立
# （「因为仓位选择了更差的交易反而放大风险」），但那说的是不要为省钱去做
# 负期望的廉价合约，不是任何倍数的超配都可以。
OVER_KELLY_SOFT = 1.5
OVER_KELLY_HARD = 3.0


def size(net_assets: float, unit_occupancy: float, k: KellyResult,
         *, buying_power: float | None = None,
         max_over_kelly: float = OVER_KELLY_SOFT,
         hard_over_kelly: float = OVER_KELLY_HARD,
         allow_over: bool = False) -> SizeVerdict:
    """给出该下几组，并在超过 Kelly 上限时【拒绝】。

    对小账户，「按 Kelly 缩小」常常等于「不能交易」，所以 1~1.5 倍超配照放行
    并标注；但 3 倍以上必须拒绝 —— 那已经不是「最小单位不可分」的问题，
    是这个策略对这个账户太大。
    """
    if not k.positive_edge:
        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, 0.0, 0.0,
                           f"负优势（p·b−q = {k.edge:+.2f}）—— 这个策略本身不该做，"
                           f"仓位再小也是慢慢亏。")
    if net_assets <= 0 or unit_occupancy <= 0:
        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, k.kelly, 0.0,
                           "净资产或占用无效")
    kd = net_assets * k.kelly
    n = int(kd // unit_occupancy)
    frac1 = unit_occupancy / net_assets
    over = frac1 / k.kelly if k.kelly > 0 else float("inf")
    cap = buying_power if buying_power is not None else net_assets
    if unit_occupancy > cap:
        return SizeVerdict(False, 0, kd, unit_occupancy, frac1, k.kelly, over,
                           f"1 组占用 ${unit_occupancy:.0f} 超过可用 ${cap:.0f} —— "
                           f"做不了。{MIN_UNIT_NOTE}。")
    if n >= 1:
        return SizeVerdict(True, n, kd, unit_occupancy, n * frac1, k.kelly,
                           n * frac1 / k.kelly,
                           f"Kelly ${kd:.0f} → {n} 组（每组 ${unit_occupancy:.0f}）。"
                           f"盈亏比 {k.odds:.2f}、优势 {k.edge:+.2f}。")
    # 1 组已超 Kelly
    base = (f"Kelly 只允许 ${kd:.0f}，但 1 组要 ${unit_occupancy:.0f}"
            f"（净资产 {frac1:.0%}，是 Kelly 的 {over:.1f} 倍）。{MIN_UNIT_NOTE}")
    if over > hard_over_kelly:
        return SizeVerdict(False, 0, kd, unit_occupancy, frac1, k.kelly, over,
                           base + f" —— 超过 {hard_over_kelly:g} 倍硬上限，**不做**。"
                                  f"这不是「最小单位不可分」的问题，是这个策略对这个"
                                  f"账户太大：换更窄的价差、更小的标的，或等账户长大。")
    if over > max_over_kelly and not allow_over:
        return SizeVerdict(False, 0, kd, unit_occupancy, frac1, k.kelly, over,
                           base + f" —— 超过 {max_over_kelly:g} 倍软上限。"
                                  f"要下需显式确认（allow_over），"
                                  f"否则按不做处理。")
    return SizeVerdict(True, 1, kd, unit_occupancy, frac1, k.kelly, over,
                       base + f" —— 在 {max_over_kelly:g} 倍软上限内，可按 1 组做，"
                              f"但要知道这是超配。")


def consecutive_full_loss_prob(win_rate: float, n: int) -> float:
    """【连续 n 次全损】的概率 —— 注意这【不是】破产概率，更不是它的上限。

    codex 2026-08-31 P0-2：原名 ruin_probability 且报告标成「破产概率上限」，
    这是错的，而且错在偏乐观的方向。(1−p)^n 只回答「接下来恰好连着 n 次全损」，
    而真实破产还可以由以下路径达成，全都不在这个式子里：
      · 非连续的亏损累积（赢几次小的、输几次大的）
      · 部分亏损累积（价差常见的「只破一点」）
      · 同日多品种相关信号一起亏（金银 0.89、QQQ/TQQQ 0.99）
      · 手续费与点差的持续消耗
      · 提前指派、到期强平带来的非模型损失
    所以真实破产概率【高于】这个数。它只能当「最粗的连亏情景」参考。
    """
    return (1 - win_rate) ** max(1, n)


# 旧名保留一个周期，避免调用点静默失配；行为与新名一致。
ruin_probability = consecutive_full_loss_prob
