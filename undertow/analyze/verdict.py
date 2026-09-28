"""当日决策研判（证据门控版；Codex 015 / 用户 2026-09-28）。

用户：「各种指标很多很杂，但是真正能够相信的似乎没有几个……只把可靠的指标利用起来，
其他指标可以只记录数据，不进行应用。」

旧版把 中期(mid)/近端(near) 分层、强信号、斐波摆动腿、自动盈亏比 用规则合成「做空？/追？/短线/长线」四问。
盘点发现它的锚点 trend = sign(mid) or sign(near)，而 mid 是 COT+宏观的加权投票、权重未回测
（outlook.py 模块 docstring 已写明），旧注释却称它为「已校准的中期趋势层」；强信号 56%（p=0.804）
仍能驱动「可空/跟空」；自动斐波目标价未经验证，却经盈亏比分级硬性决定「别追」。

现在：方向、过滤、持有结论只接收 analyze/claims.py 里【有该用途权限】的主张。
- T1（可决策）当前为空 → 主结论是「本系统暂无通过验证的方向依据」。这说的是【模型没有已验证依据】，
  不是「市场不适合交易」；也不自动平掉任何现仓。
- T2（历史探索，当前只有金银增仓层）只展示，不改变方向、置信度或任何结论字段。
- T3（其余）不进入这里；原始计算与台账照常保留，由研报的观察区展示。
- 用户自定方向与用户纪律不在本模块产生：盈亏比下限只在用户给定目标与止损时适用（policy.rr_minimum）。

旧函数签名保留（调用方不变），但 strong_sig / fib / rr_plan / outlook 的方向读数不再影响任何结论字段 ——
tests/test_verdict.py 用「只改 T2/T3 输入、结论字段完全不变」的行为测试守住这一点。
确定性、无 LLM、不做算术。仅情景研判，非投资建议、非交易指令。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from undertow.analyze import claims as cl

NO_T1 = "本系统暂无通过验证的方向依据"


@dataclass(frozen=True)
class DailyVerdict:
    """当日研判。字段皆为可直接呈现的中文短句。结论字段只由有权限的主张产生。"""
    ok: bool
    headline: str                 # 一句话总纲
    short_answer: str             # 方向（原「做空？」）
    chase_answer: str             # 入场（原「现价能不能追？」）
    swing_action: str             # 已有短线仓
    core_action: str              # 已有长线仓
    bullets: list[str] = field(default_factory=list)
    note: str = ""
    evidence: str = ""            # 模型证据状态
    exploratory: list[str] = field(default_factory=list)   # T2 历史探索（只展示）


def _t2_lines(instrument: str | None, flow_direction: str | None) -> list[str]:
    """T2 历史探索：只描述，不给结论。当前只有金银增仓层。"""
    c = cl.CLAIMS.get("flow.direction.gold_silver")
    if not c or c.tier != "T2" or instrument not in ("gold", "silver") or not flow_direction:
        return []
    return [f"探索观察（T2，不参与本系统方向结论）：增仓层今日方向「{flow_direction}」。"
            f"历史探索范围：{c.scope}（as_of {c.as_of}），样本不足 50、四品种多重比较校正后不显著；"
            f"{c.note}。"]


def build_verdict(o=None, fa=None, strong_sig=None, fib=None, rr_plan=None, *,
                  instrument: str | None = None, flow_direction: str | None = None) -> DailyVerdict:
    """证据门控的当日研判。o/fa/strong_sig/fib/rr_plan 保留在签名里只为兼容调用方：
    它们对应的主张都是 T3（见 claims.py），不进入任何结论字段。"""
    t1 = cl.t1_claims()
    if t1:
        # 走到这里说明登记表里出现了 T1：必须先写好该主张的决策适配器并通过审查，不能静默当作无 T1。
        raise NotImplementedError(f"登记表出现 T1 主张 {[c.claim_id for c in t1]}，但研判尚未接入其适配器")
    evidence = f"模型证据：{NO_T1}（T1 可决策主张 0 条）"
    short_answer = (f"{NO_T1}：做多还是做空，本系统不给结论。"
                    "这不等于市场不适合交易；你自己有方向时，用下方的结构计算和你的风险纪律评估。")
    chase_answer = ("入场与否需要你给定目标价与止损，再按你的盈亏比下限判断；"
                    "自动斐波目标价未经验证，不作「追/不追」的依据。")
    swing_action = "已有短线仓：按你开仓时的计划与止损执行；本系统不给加减仓信号（持仓风险监测照常）。"
    core_action = "已有底仓：按你的计划执行；中期方向（COT+宏观投票）未经验证，不作持有或减仓依据。"
    exploratory = _t2_lines(instrument, flow_direction)
    bullets = [f"方向： {short_answer}", f"入场： {chase_answer}",
               f"短线仓： {swing_action}", f"长线仓： {core_action}", *exploratory]
    note = ("证据门控：方向、过滤、持有结论只接收登记表中有该用途权限的主张（undertow claims 可查）；"
            "未验证的指标照常采集与展示，但不参与结论。仅情景研判，非投资建议。")
    return DailyVerdict(ok=True, headline=NO_T1, short_answer=short_answer, chase_answer=chase_answer,
                        swing_action=swing_action, core_action=core_action, bullets=bullets, note=note,
                        evidence=evidence, exploratory=exploratory)
