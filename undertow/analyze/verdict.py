"""当日决策研判（确定性合成）——把已算好的四件套：近端/中期分层(outlook)、买卖方
资金流(flow)、强信号(strong_signal)、斐波盈亏比闸门(risk_reward)，用**规则**合成成
交易者真正要的三问：**能不能做空？现价能不能追？短线/长线该怎么处理？**

设计原则（本 skill 的铁律）：这里**不含任何 LLM、不做任何算术**——所有数字来自上游确定性
模块，本模块只按规则把它们组织成决策问句。跑在每日无人值守定时任务里；交互式会话中，
LLM（助手）可读它的结构化结论再叠流畅叙述，但结论与数字以本模块为准。

复刻波段交易纪律：先看盈亏比、别追高、等回调、长短线资金分离、防守≠看空、趋势没坏别逆势。
仅波段级情景研判，非投资建议、非交易指令。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from undertow.analyze.outlook import Outlook
from undertow.analyze.flow import FlowAnalysis, StrongSignal
from undertow.analyze.fibonacci import FibAnalysis
from undertow.analyze.risk_reward import RiskRewardPlan, Setup


@dataclass(frozen=True)
class DailyVerdict:
    """当日三问的规则化结论。字段皆为可直接呈现的中文短句。"""
    ok: bool
    headline: str                 # 一句话总纲
    short_answer: str             # 做空？
    chase_answer: str             # 现价能不能追？（顺势方向）
    swing_action: str             # 短线波段仓怎么处理
    core_action: str              # 长线底仓怎么处理
    bullets: list[str] = field(default_factory=list)   # 供报告逐条呈现
    note: str = ""


def _setup(plan: RiskRewardPlan | None, kind: str) -> Setup | None:
    if not plan or not plan.ok:
        return None
    return next((s for s in plan.setups if s.kind == kind), None)


def _leg_top(fib: FibAnalysis | None) -> bool:
    """现价是否在摆动腿的"顶部区"（浅回撤 0–0.236 或已突破摆动极值）＝追势 R:R 最差处。"""
    if not fib or not fib.ok:
        return False
    z = fib.current_zone or ""
    return ("摆动高" in z) or ("摆动低" in z) or ("0.236" in z) or ("站上" in z) or ("跌破" in z and False)


def _dir_word(long_side: bool) -> str:
    return "做多" if long_side else "做空"


def build_verdict(o: Outlook, fa: FlowAnalysis | None,
                  strong_sig: StrongSignal | None,
                  fib: FibAnalysis | None,
                  rr_plan: RiskRewardPlan | None) -> DailyVerdict:
    """按规则合成当日三问。缺某层输入时对应结论降级为保守措辞，不臆造。"""
    near = getattr(o, "near_bias", "") or ""
    mid = getattr(o, "mid_bias", "") or ""
    tilt = (fa.flow_tilt if fa else "") or ""
    up_leg = bool(fib and fib.ok and fib.direction == "up")
    down_leg = bool(fib and fib.ok and fib.direction == "down")
    chase = _setup(rr_plan, "chase")
    pull = _setup(rr_plan, "pullback")
    leg_top = _leg_top(fib)

    def _sign(s: str) -> int:
        return 1 if "偏多" in s else (-1 if "偏空" in s else 0)

    # 趋势方向锚在【中期】(持仓/宏观·durable)；中期中性则退回近端。摆动腿是【短期】结构。
    trend = _sign(mid) or _sign(near)
    leg = 1 if up_leg else (-1 if down_leg else 0)
    # 逆势微腿：短腿方向与中期趋势相反＝上升趋势里的回调 / 下降趋势里的反抽
    counter = (leg != 0 and trend != 0 and leg != trend)
    long_side = leg == 1 if leg else (trend == 1)
    dirw = _dir_word(long_side)

    # ── 1) 做空？──────────────────────────────────────────────
    # ⚠️ 强信号【未经回测校准】（见 signal_ledger 模块 docstring：核心三闸门需历史
    # 逐行 OI，免费源没有，只能向前累积）。因此它不得单独把结论翻成「可空」去压过
    # 已校准的中期趋势层——旧版本正是这么写的：看跌强信号是第一个分支，
    # 直接盖掉「中期偏多、趋势未坏」，一个未校准的单层规则否决了综合研判。
    # 现在改为：与中期趋势【同向】时才加持，【背离】时如实报冲突、不给方向结论。
    # ⚠️ 低置信的强信号（方向裁决软条件未过）不得驱动任何交易结论 ——
    # 它已在渲染层降级为琥珀观察项，判定层必须同步，否则又是"指标互相打架"。
    if strong_sig is not None and getattr(strong_sig, "low_confidence", False):
        strong_sig = None
    if strong_sig and strong_sig.direction == "看跌" and trend == 1:
        short_answer = (f"⚠️ 冲突：⚡近端强看跌信号（{strong_sig.level}，**未经回测校准**）"
                        f"撞上中期偏多、趋势未坏。近端资金流只是一层未校准的观察，"
                        f"不足以单独否定趋势——**不据此做空**；若要减仓请按仓位规则，"
                        f"别当反转信号用。")
    elif strong_sig and strong_sig.direction == "看跌":
        short_answer = (f"⚡近端强看跌信号（{strong_sig.level}，未经回测校准）——"
                        f"中期未站在多头一侧，空头有近端资金流支持，"
                        f"可考虑波段空，但仍先过盈亏比闸门、控仓。")
    elif "偏空" in near and "偏空" in mid:
        short_answer = "近端+中期共识偏空——做空有结构支持，择机（看盈亏比、别追空）。"
    elif trend == 1:   # 中期偏多（或退回近端偏多）：趋势未坏
        extra = "近端在回调、正是买点不是卖点" if counter else "防守型买盘≠空头进场"
        short_answer = (f"不是做空位置：中期偏多、趋势未坏（{extra}）；"
                        f"逆一个没坏的趋势去空，正是所谓负 edge。")
    elif "偏空" in near:   # 近端偏空但中期未确认（中性/分歧）
        short_answer = ("可轻仓短空跟近端，但中期未背书——只算弱势跟随、不是趋势空，"
                        "务必控仓、先过盈亏比闸门（别当反转来重仓）。")
    else:
        short_answer = "无明确做空依据：近端无看跌信号、中期未转空——观望或顺中期，别逆势凑空单。"

    # ── 2) 现价能不能追？──────────────────────────────────────
    # ⚠️ 详细结论与总纲标签必须在**同一个分支**里产出。早先版本用两套独立的 if 链，
    # 结果漂移出三处自相矛盾（如详细写「无结构依据·观望为上」、标签却断言「追不划算」）。
    if counter and trend == 1:
        chase_answer = ("近端在回调（下跌微腿），顺中期偏多者别追空——"
                        "等回调到斐波关键区/企稳再做多（回调买、不追）。")
        chase_tag = "回调买·不追"
    elif counter and trend == -1:
        chase_answer = ("近端在反抽（上涨微腿），顺中期偏空者别追多——"
                        "等反抽到斐波关键区再做空（反抽卖、不追）。")
        chase_tag = "反抽卖·不追"
    elif chase is None:
        chase_answer = "无有效摆动腿/盈亏比闸门，追势与否无结构依据——观望为上。"
        chase_tag = "追势无依据"          # 不能写「追不划算」——我们根本没算出盈亏比
    elif chase.grade == "差":
        tail = f"；想{dirw}，等回调到 {pull.entry_label}（R:R {pull.rr:.1f}·{pull.grade}）再看。" if pull else ""
        chase_answer = f"别追：现价{dirw}盈亏比仅 {chase.rr:.1f}（差，＜1:1）{tail}"
        chase_tag = "别追·等回调"
    elif chase.grade == "中":
        tail = f"；更好的点在回调 {pull.entry_label}（R:R {pull.rr:.1f}）。" if pull else "。"
        chase_answer = f"现价{dirw}盈亏比 {chase.rr:.1f}（中，偏弱）——追不划算{tail}"
        chase_tag = "追不划算"
    else:  # 优
        tail = (f"；回调到 {pull.entry_label} 更优（R:R {pull.rr:.1f}）,纪律上仍偏好等回调。"
                if (pull and pull.rr > chase.rr) else "（仍须自定胜率与仓位）。")
        chase_answer = f"现价{dirw}盈亏比 {chase.rr:.1f}（优）——空间/风险结构占优{tail}"
        chase_tag = "现价结构占优"

    # ── 3) 短线波段仓 ────────────────────────────────────────
    # 同上：标签跟着分支走。旧版标签链只认「有没有 strong_sig」，导致
    # 强信号方向与摆动腿不符时详细写「回调进行中·别顺短腿追空」、标签却写「短线跟势」。
    chase_poor = bool(chase and chase.grade == "差")
    # ⚠️ 与「做空？」一格同一条原则：强信号未经回测校准，只能【加持同向】读数，
    # 不得【反转反向】读数。旧版本里，中期偏多 + 逆势下腿时非信号分支写的是
    # "别顺短腿追空"，一个未校准信号却能把它翻成"可跟空"——和它在总纲那格
    # 直接盖掉"中期偏多、趋势未坏"是同一个缺陷，只是低一格。
    if strong_sig and strong_sig.direction == "看涨" and up_leg and trend != -1:
        swing_action = f"短线可跟多：⚡{strong_sig.level}看涨信号在（主翼买盘一边倒），顺势波段。"
        swing_tag = "短线跟多"
    elif strong_sig and strong_sig.direction == "看跌" and (down_leg or trend == -1) and trend != 1:
        swing_action = f"短线可跟空：⚡{strong_sig.level}看跌信号在，顺势波段。"
        swing_tag = "短线跟空"
    elif counter and trend == 1:
        swing_action = "回调进行中：等企稳/回踩斐波关键区做多，别顺短腿追空。"
        swing_tag = "短线等回调"
    elif counter and trend == -1:
        swing_action = "反抽进行中：等反抽到位做空，别顺短腿追多。"
        swing_tag = "短线等反抽"
    elif up_leg and leg_top and chase_poor:
        swing_action = "腿顶附近、追多 R:R 差、近端进攻结构已散——短线宜获利了结/收紧止损，别加。"
        swing_tag = "短线止盈/收紧"
    elif down_leg and leg_top and chase_poor:
        swing_action = "腿底附近、追空 R:R 差——短线空单宜止盈/收紧，别追。"
        swing_tag = "短线止盈/收紧"
    elif up_leg and not leg_top:
        swing_action = "短线持多：回调不破起涨点可续持，等回调关键区加而非现价追。"
        swing_tag = "短线持多"
    elif down_leg and not leg_top:
        swing_action = "短线持空：反抽不破起跌点可续持，等反抽关键区加而非现价追。"
        swing_tag = "短线持空"
    else:
        swing_action = "短线观望：等回调/反抽给出好盈亏比再动手（别在中性区追）。"
        swing_tag = "短线观望"

    # ── 4) 长线底仓 ──────────────────────────────────────────
    if "偏多" in mid:
        core_action = "中期偏多逻辑仍在——底仓按年度持有，不因短线转防守而清仓（长短线分离）。"
    elif "偏空" in mid:
        core_action = "中期偏空——底仓需重新评估、逢反弹减，别硬扛。"
    else:
        core_action = "中期中性/分歧——底仓维持，等方向明朗再动。"

    # ── 总纲一句话 ───────────────────────────────────────────
    # 未校准的强信号不得单独把总纲翻成「可空」；与中期偏多背离时标注冲突。
    _bear_sig = bool(strong_sig and strong_sig.direction == "看跌")
    short_tag = ("信号与趋势冲突" if (_bear_sig and trend == 1)
                 else ("可空" if _bear_sig or ("偏空" in near and "偏空" in mid)
                       else ("轻仓短空" if ("偏空" in near and trend != 1) else "不做空")))
    # chase_tag / swing_tag 已在上面各自的分支里定出，此处不得重算——
    # 重算就是把刚拆掉的那两套并行 if 链又装回来。
    core_tag = ("长线拿住" if "偏多" in mid else ("长线减" if "偏空" in mid else "长线维持"))
    headline = f"{short_tag} · {chase_tag} · {swing_tag} · {core_tag}"

    bullets = [
        f"做空？ {short_answer}",
        f"现价追？ {chase_answer}",
        f"短线仓： {swing_action}",
        f"长线仓： {core_action}",
    ]
    note = ("规则化合成 近端/中期分层＋买卖方资金流＋强信号＋斐波盈亏比闸门，"
            "确定性、无 LLM、数字全来自上游模块；波段级情景研判，非投资建议。")
    return DailyVerdict(ok=True, headline=headline, short_answer=short_answer,
                        chase_answer=chase_answer, swing_action=swing_action,
                        core_action=core_action, bullets=bullets, note=note)
