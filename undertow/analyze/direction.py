"""方向裁决与弃权：**给方向就要准，证据不足就明确闭嘴。**

用户标准（2026-08-27）：「可以适当弃权，这没问题，不是每天都能有清晰方向的。
关键是给出方向时，需要准确。」

实测：没有任何阈值被证明有效
--------------------------------
把每个候选弃权条件的「覆盖率 vs 正确率」都测了（107 个品种-日，
gold/silver/wti/qqq，2026-06-25→08-27，按修正时序打分）：

    条件                 覆盖率  正确率   Wilson 95% 区间
    压力比 ≥4.0           37%    62%    [47%, 76%]   ← 最好，下界仍 <50%
    两口径同向             37%    60%    [45%, 74%]
    压力比 ≥3.0           44%    60%    [45%, 72%]
    压力比 ≥1.3（现用）     82%    56%    [45%, 66%]
    压力比≥3.0 且 同向      16%    53%    [31%, 74%]   ← 组合反而更差
    可用腿 ≥50            29%    42%    [26%, 59%]   ← 腿越多越差

**全部区间都含 50%，一条都不能声称有效。** 组合更差是小样本过拟合的典型征兆。

因此本模块的策略是：
  · **逻辑性弃权**（不需要统计支持，属于硬约束）→ 强制执行
  · **统计性弃权**（阈值来自观察而非校准）→ 执行但**明确标注未校准**，
    并把每次裁决连同当时的分量一起入台账，等样本够了再回来校准。

绝不做的事：拿上面那张表去"挑最优阈值"。那正是本项目反复证明过的噪音拟合
（置换检验实测：把涨跌打乱后，在 255 组权重里挑最优，中位数就能拿到 58.9%）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# —— 逻辑性弃权（硬约束，与统计无关）——
# 过期：信号的可交易时点已过，再准也吃不到
# 无数据：没有可比的前一日快照，或 ΔOI 全零（OCC 未结算）

# —— 统计性弃权（**未校准**，见模块 docstring 的实测表）——
MIN_RATIO = 1.3
# ≤2 天到期的增仓占比超过此值 = 当日押注由短到期主导，净有效 Delta 失去反证资格。
# 门槛取 0.5（过半）而非拟合值 —— 没有样本可校准，只能取一个不偏不倚的分界。
SHORT_DATED_SHARE = 0.5          # 压力比低于此 → 方向不明。沿用既有口径，未校准
UNCALIBRATED_NOTE = ("阈值未经校准：实测覆盖率/正确率权衡里没有任何门槛的 "
                     "Wilson 95% 区间下界超过 50%")


@dataclass(frozen=True)
class DirectionCall:
    """方向裁决结果。abstain=True 时 direction 必为空字符串。"""
    direction: str = ""          # 偏多 / 偏空 / ""（弃权）
    abstain: bool = True
    reasons: list[str] = field(default_factory=list)   # 弃权或判定的依据
    hard: bool = False           # 是否属于逻辑性（硬）弃权
    ratio: float | None = None
    calibrated: bool = False     # 恒为 False，直到台账样本足够并通过检验
    # —— shadow 模式（codex review 2026-08-27）——
    # 项目铁律是"未校准的东西不得正式裁决"。软弃权的阈值全部未校准，
    # 因此默认走 shadow：**记录但不改变输出**。
    # shadow_direction 保存"若正式执行软弃权，它本会给出的方向"，
    # 供台账日后校准；low_confidence 标记该方向证据不足。
    shadow_direction: str = ""   # 软弃权时它本来会给的方向（仅记录）
    low_confidence: bool = False  # 给了方向但证据不足（软条件未过）

    @property
    def label(self) -> str:
        if self.abstain:
            return "方向不明"
        return f"{self.direction}（低置信）" if self.low_confidence else self.direction

    @property
    def range_friendly(self) -> bool:
        """方向不明/低置信 → 区间策略（铁鹰）的适用场景。

        用户 2026-08-27 指出：「方向不明，墙明确的时候，也可以铁鹰」。
        弃权不等于不能交易 —— 铁鹰本就是中性结构，方向未知恰是它的正当理由，
        前提是上下两道墙把现价夹住（这个前提由 condor.assess_condor 的门槛 3 把关）。
        """
        return self.abstain or self.low_confidence


def decide(*, up_pressure: float, dn_pressure: float,
           net_delta: float | None = None,
           has_prev: bool = True, oi_changed: bool = True,
           trade_date: str = "", today: str = "",
           shadow_soft: bool = True,
           dte_share_le2: float | None = None) -> DirectionCall:
    """裁决方向或弃权。

    弃权分两类，输出里必须能区分：
      **硬弃权**（逻辑约束，无关统计）：无前日可比 / OI 未结算 / 数据已过期 —— 始终生效
      **软弃权**（未校准阈值）：压力比不足 / 两口径反向

    shadow_soft=True（默认）时软弃权走 **shadow 模式**：不抑制输出，改为
    给出方向并标 low_confidence，把"本会弃权"记进 shadow_direction。
    理由（codex review）：项目铁律是未校准的东西不得正式裁决，而软弃权的
    阈值全部未校准 —— 实测没有任何门槛的 Wilson 95% 下界超过 50%。
    正式启用需先在台账里证明它确实提升了"给方向时的准确率"。
    """
    R: list[str] = []
    if not has_prev:
        return DirectionCall(reasons=["无前一日可比快照，无法做日对日 diff"], hard=True)
    if not oi_changed:
        return DirectionCall(
            reasons=["当日 ΔOI 全零（OCC 隔夜结算尚未落地），数据不携带任何持仓变化"],
            hard=True)
    if trade_date and today and trade_date < today:
        return DirectionCall(
            reasons=[f"数据已过期：可交易时点是 {trade_date} 开盘，今天已是 {today}，"
                     f"即便判对也吃不到"], hard=True)

    if up_pressure <= 0 and dn_pressure <= 0:
        return DirectionCall(reasons=["两侧压力均为零，无方向证据"], hard=True)

    hi, lo = max(up_pressure, dn_pressure), max(min(up_pressure, dn_pressure), 1.0)
    ratio = hi / lo
    d = "偏多" if up_pressure > dn_pressure else "偏空"

    if ratio < MIN_RATIO:
        rs = [f"压力比 {ratio:.2f}× < {MIN_RATIO}×，两侧势均力敌（{UNCALIBRATED_NOTE}）"]
        if shadow_soft:
            return DirectionCall(direction=d, abstain=False, low_confidence=True,
                                 shadow_direction="", reasons=rs, ratio=round(ratio, 2))
        return DirectionCall(reasons=rs, ratio=round(ratio, 2))

    # 两口径反向 → 弃权。pressure 是【推断】（按 IV 判主动方），
    # 净有效 Delta 是【观测】（纯算术）。两者反向时我们并不知道哪个对。
    # ⚠️ 净有效 Delta 在【短到期主导】的日子里不可用作反证 —— 2026-08-28 黄金的教训。
    #
    # 那天推断口径 53.49× 指向偏空，净 Delta +2,993 指向偏多，两口径反向 →
    # 信号被打成"低置信"，于是我在对话里没把它当回事、也没预警。次日 GLD -3.24%，
    # 收 408.89，正好打穿那批被忽略的 408P。
    #
    # 为什么净 Delta 会反：那 7.8 万张 put 是【次日到期、Δ0.04/0.10】的，
    # 按 Delta 加权几乎不计分；而 call 端张数只有 35,235 却带 Δ0.50，全额计分。
    # 根子在 Delta 是【局部线性近似】，对"到期前大幅跳跃"系统性失效：
    # 0DTE 的 408P 此刻 Δ0.04，真跌 3% 就变 Δ1.0，赔率几十倍。
    # 用 Delta 衡量这种押注，等于用尺子量爆炸。
    #
    # 所以：当 ≤2 天到期的增仓占比超过阈值时，净 Delta 失去"把方向打成低置信"
    # 的资格，只作为提示保留。这不是调参凑结果，是这个指标的适用边界。
    if (dte_share_le2 is not None and dte_share_le2 >= SHORT_DATED_SHARE
            and net_delta is not None and net_delta != 0
            and ("偏多" if net_delta > 0 else "偏空") != d):
        R.append(f"净有效 Delta {net_delta:+,.0f} 指向反向，但 {dte_share_le2:.0%} 的增仓"
                 f"在 2 天内到期 —— Delta 是局部近似，对到期前大幅跳跃系统性失效，"
                 f"此处不作为反证（见 direction.py 说明）")
    elif net_delta is not None and net_delta != 0:
        obs = "偏多" if net_delta > 0 else "偏空"
        if obs != d:
            rs = [f"两个口径反向：推断口径（加权增仓 {ratio:.2f}×）指向{d}，"
                  f"观测口径（净有效 Delta {net_delta:+,.0f}）指向{obs}",
                  UNCALIBRATED_NOTE]
            if shadow_soft:
                # shadow：不抑制，但明确标低置信；shadow_direction 记下"本会弃权"
                return DirectionCall(direction=d, abstain=False, low_confidence=True,
                                     shadow_direction="", reasons=rs, ratio=round(ratio, 2))
            return DirectionCall(reasons=rs, ratio=round(ratio, 2))
        R.append(f"两口径同向：加权增仓 {ratio:.2f}× 与净有效 Delta {net_delta:+,.0f} 均指向{d}")
    R.append(f"压力比 {ratio:.2f}×（{UNCALIBRATED_NOTE}）")
    return DirectionCall(direction=d, abstain=False, reasons=R, ratio=round(ratio, 2))
