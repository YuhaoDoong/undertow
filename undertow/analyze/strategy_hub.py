"""策略统筹层：把多个【独立策略子模块】的输出汇总成一张"策略总纲"。

设计：
  * 每个策略子模块（strategy.py 的方向性情景、condor.py 的铁鹰……）独立判断
    "当前信号是否适配自己"，各自产出结构化结果。
  * 本层不做交易判断，只做【调度与汇总】：遍历各子模块结论，生成统一的
    StrategyProposal 列表，供报告的策略板块作总纲呈现。
  * 未来新增策略（单边价差、日历价差……）只需再加一个子模块 + 在此登记一行，
    报告板块无需改结构。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyProposal:
    name: str          # 策略子模块名
    applicable: bool   # 当前信号是否适配
    tag: str           # 结构/方向标签（如 贴墙B型 / 做空 / 不适配）
    headline: str      # 一句话结论


def assemble_strategies(*, directional=None, condor=None, credit_spread=None) -> list[StrategyProposal]:
    """汇总各独立策略子模块 → 统一提案列表。参数均可为 None（该子模块未运行）。"""
    props: list[StrategyProposal] = []

    if directional is not None:
        applic = directional.direction != "观望"
        props.append(StrategyProposal(
            name="方向性情景（顺/逆结构）",
            applicable=applic,
            tag=directional.direction,
            headline=directional.verdict or f"方向：{directional.direction}"))

    if credit_spread is not None:
        props.append(StrategyProposal(
            name="方向性信用价差（顺向卖方）",
            applicable=credit_spread.applicable,
            tag=(credit_spread.direction if credit_spread.applicable else "不适配"),
            headline=credit_spread.headline))

    if condor is not None:
        props.append(StrategyProposal(
            name="铁鹰（区间卖方）",
            applicable=condor.applicable,
            tag=(condor.condor_type or "适配") if condor.applicable else "不适配",
            headline=condor.headline))

    return props
