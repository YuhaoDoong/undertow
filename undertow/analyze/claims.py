"""主张权限登记表（Codex 015；用户 2026-09-28：「只把可靠的指标利用起来，其他指标可以只记录数据，不进行应用」）。

分级的对象是【具体主张 × 用途】，不是整个指标：
- 同一个超买超卖读数可以如实描述「距均线多少 ATR」（观测），却没有证明「未来 5 日会涨」（预测）；
- 标的方向的预测即使成立，也不等于卖 put 价差扣费后盈利。

四种角色（role）：
  observation  当时可观测的事实、确定性的结构计算 —— 不需要 p 值证明「这个报价存在」，只能展示
  prediction   方向、收益、尾部、适用行情等预测性主张 —— 只有它分 T1/T2/T3
  feasibility  缺价、报价倒挂、腿不匹配、风险算不出、权限未核实等「能不能算/能不能做」的条件
  policy       用户认可的风险预算与交易纪律（标来源与版本；不分 T 级，也不伪称最优）

预测主张的等级：
  T1  该具体用途有足够验证（事前可得、相关结构匹配的推断、完整选择族的多重比较、效应区间、
      经济门槛、未参与选择的验证样本、适用范围）。**当前为空。**
  T2  历史探索：只展示，不改变方向、分数、置信度、排序、仓位、入场或否决（Codex 015 P1-C）
  T3  研究观察：原始计算与台账照常保留，不进入任何决策路径

权限只能由本表授予：未登记的预测主张默认没有决策权限，并在审计输出里可见，不会悄悄绕过。
升级（T3→T2→T1）必须有事前方案 + 完整证据审查 + 用途映射，不是存在一份 md 就通过；
降级、发现前视、修错【立即生效】并留审计记录，不需要新的预登记（Codex 015 问题 5）。
数字不在这里复制：evidence_refs 指向 validation.REGISTRY 的键或具体文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field

ROLES = ("observation", "prediction", "feasibility", "policy")
USES = ("direction", "filter", "confidence", "ranking", "sizing", "holding", "display")
TIERS = ("T1", "T2", "T3")
# T3 的原因分开写，不统称「已无效」（Codex 015 §四）
REASONS = {
    "untested": "未检验",
    "insufficient": "样本或精度不足 / 多重比较后不显著",
    "inference_defect": "推断方法有已知缺陷（跨资产依赖、口径择优等）",
    "ex_post_only": "只在按事后信息分层时成立，事前用不上",
    "no_increment_detected": "当前样本未检出增量（不等于证明零效果）",
    "negative_sample": "当前样本表现为负",
    "invalid_inputs": "依赖的输入本身未通过或已失效",
    "prospective_study": "正在前瞻预登记研究中，结果未出",
    "": "",
}
REVIEW = "Codex 015（2026-09-28）逐条归类；用户同意分级方向"


@dataclass(frozen=True)
class Claim:
    claim_id: str
    producer: str                      # 模块:函数 / 字段
    role: str
    scope: str                         # 品种 / regime / 周期 / 预测目标 / 基准
    tier: str | None = None            # 只有 prediction 有
    reason: str = ""                   # T3 原因代码（见 REASONS）
    allowed_uses: tuple[str, ...] = ("display",)
    consumers: tuple[str, ...] = ()    # 迁移前会改变结论的消费者（审计用）
    target_action: str = ""
    migration: str = "pending"         # pending / done
    evidence_refs: tuple[str, ...] = ()
    policy_ref: str = ""
    prereg_ref: str = ""
    as_of: str = ""
    note: str = ""
    review_record: str = REVIEW
    algorithm_version: str = "as-of-HEAD"


def _p(cid, producer, scope, tier, reason, consumers, action, refs=(), note="", uses=("display",), **kw):
    return Claim(cid, producer, "prediction", scope, tier, reason, uses, tuple(consumers), action,
                 evidence_refs=tuple(refs), note=note, **kw)


def _o(cid, producer, scope, note=""):
    return Claim(cid, producer, "observation", scope, allowed_uses=("display",), target_action="保留展示",
                 migration="done", note=note)


def _f(cid, producer, scope, note=""):
    return Claim(cid, producer, "feasibility", scope, allowed_uses=("filter", "display"),
                 target_action="保留：阻止无法完成的那项计算，并写明具体原因", migration="done", note=note)


def _pol(cid, producer, scope, ref, note=""):
    return Claim(cid, producer, "policy", scope, allowed_uses=("filter", "sizing", "display"),
                 target_action="保留：标来源与版本", policy_ref=ref, migration="done", note=note)


_ALL = [
    # ── 预测主张（全部 T1 空缺；金银增仓为 T2 历史探索）──
    _p("outlook.mid_bias", "analyze/outlook.py: Outlook.mid_bias（COT+宏观加权投票）",
       "五品种 · 中期方向 · 目标未定义", "T3", "untested",
       ["verdict.build_verdict: trend 锚点、做空?/长线仓", "consult 上下文"],
       "移出研判；原「已校准的中期趋势层」注释撤回",
       ["undertow/analyze/outlook.py 模块 docstring（权重未回测）", "undertow/analyze/indicators.py"]),
    _p("outlook.near_bias", "analyze/outlook.py: Outlook.near_bias（墙位空间+P/C+资金流投票）",
       "五品种 · 近端方向", "T3", "untested",
       ["verdict.build_verdict: trend 回退", "strategy.build_strategy: 方向", "credit_spread: 方向"],
       "移出研判与策略方向", ["wall_space_vote"]),
    _p("outlook.bias_label", "analyze/outlook.py: _bias（±0.8/±2.0/1.5 阈值）", "分数→偏多/偏空标签",
       "T3", "untested", ["全部研判分支（经 mid/near）"], "只作观察标签，不带交易含义"),
    _p("outlook.cot_signals", "analyze/signals.py（拥挤/背离/掉期/挤仓）", "期货 COT · 中期", "T3", "untested",
       ["outlook.mid_bias 投票"], "原始 COT 读数保留展示", ["undertow/analyze/indicators.py（COT 未回测、有前视风险）"]),
    _p("outlook.macro_drivers", "analyze/macro.py（实际利率/美元/通胀预期，权重 1.5/1.0/0.6）", "宏观 · 中期",
       "T3", "untested", ["outlook.mid_bias 投票"], "原始宏观读数保留展示"),
    _p("outlook.wall_space_vote", "analyze/outlook.py: Gamma 墙位空间投票（权重 0.6）", "五品种 · 近端方向",
       "T3", "no_increment_detected", ["outlook.near_bias 投票"], "撤销决策投票；墙位位置作为观测保留",
       ["wall_space_vote"]),
    _p("outlook.pc_ratio_vote", "analyze/outlook.py: P/C OI 比反向投票", "近端方向", "T3", "untested",
       ["outlook.near_bias 投票"], "只观察"),
    _p("flow.direction.gold_silver", "analyze/flow.py: 增仓层方向（call_direction）",
       "黄金、白银 · 次日去趋势收益方向 · 2026-06/08 两个月", "T2", "insufficient",
       ["outlook.near_bias 投票", "strategy 方向（经 near_bias）"],
       "T2 历史探索：只展示，不参与方向与权重",
       ["AGENTS.md「单品种时序 vs 跨品种合并」表（黄金 n=32 p=0.020、白银 n=28 p=0.036；四品种 Bonferroni 不过）"],
       note="不外推其它品种、其它时期，也不代表卖方价差盈利", as_of="2026-08-29"),
    _p("flow.direction.other", "analyze/flow.py: 增仓层方向", "原油、QQQ 等 · 次日方向", "T3", "insufficient",
       ["outlook.near_bias 投票"], "只观察", ["AGENTS.md 同表（原油 53%、QQQ 58%）"]),
    _p("flow.strong_signal", "analyze/flow.py: detect_strong_signal（强/极强）", "五品种 · D+0…D+4",
       "T3", "insufficient", ["verdict: 可空/跟空/冲突", "报告顶部 ⚡ 横幅"], "降为资金流异常观察，不出交易结论",
       ["strong_signal_dir", "gate_net_effect"]),
    _p("flow.tradeable_gate", "analyze/flow.py: tradeable_info（≥2× 压力）", "可交易信息闸门", "T3", "insufficient",
       ["cli: 解锁 credit-wall 与成本闸门板块", "报告横幅"], "不再解锁任何操作板块", ["tradeable_gate"]),
    _p("flow.vol_surface_confirm", "analyze/flow.py: 波动率面确认（强→极强）", "强信号升级", "T3", "ex_post_only",
       ["strong_signal 升级", "strategy._vetoes"], "只观察", ["vol_surface_as_filter"]),
    _p("flow.surface_gate", "analyze/flow.py: 固定 Δ 波面闸门", "逐腿买卖方向", "T3", "insufficient",
       ["逐腿方向否决"], "只观察", ["surface_gate"]),
    _p("direction.soft_abstain", "analyze/direction.py: decide（MIN_RATIO 1.3 等）", "方向软弃权", "T3", "untested",
       ["verdict: low_confidence 压制强信号", "condor 区间判断"], "只观察（calibrated=False）"),
    _p("stretch.oversold_bull", "analyze/stretch.py: CALIB 牛市极/强超卖", "GLD/SLV/USO/QQQ/SPY 池化 · 5 日相对收益",
       "T3", "inference_defect", ["（未进研判；报告标签）"], "研究观察；方法审计另行（固定原口径）",
       ["undertow/analyze/stretch.py CALIB_META.caveats"], note="t=3.18/3.24 已知跨资产依赖与口径择优，修正后可评 T2"),
    _p("stretch.other_cells", "analyze/stretch.py: CALIB 其余格", "同上", "T3", "inference_defect",
       ["（报告标签）"], "研究观察", ["undertow/analyze/stretch.py CALIB_META.caveats"]),
    _p("fibonacci.swing_leg", "analyze/fibonacci.py（REVERSAL 3%、MIN_LEG 2%）", "摆动腿方向与位置", "T3", "untested",
       ["verdict: 追/回调买/反抽卖/短线持"], "只作情景描述"),
    _p("risk_reward.auto_targets", "analyze/risk_reward.py: 由斐波自动生成的目标与止损", "追单盈亏比", "T3", "untested",
       ["verdict: 别追/追不划算/现价结构占优"], "移出；盈亏比下限只在用户给定目标与止损时作为政策使用",
       note="R:R 下限是政策，但自动目标是未校准预测，不能借政策重新获得否决权"),
    _p("strategy.vetoes", "analyze/strategy.py: _vetoes（正 gamma、零 gamma 上方、偏斜、波面）", "方向性策略",
       "T3", "untested", ["strategy: 2 条以上否决→不开枪"], "移除预测性否决"),
    _p("strategy.direction_default", "analyze/strategy.py / condor.py: 无方向时的默认结构", "结构选择", "T3", "untested",
       ["strategy_hub 排序与推荐"], "不自动填方向；无用户方向时并列结构"),
    _p("volregime.stance", "analyze/volregime.py（IV 分位 70/30、IV−RV 2pp）", "偏买方/偏卖方", "T3", "untested",
       ["condor 硬闸门"], "只观察"),
    _p("credit_spread.gates", "analyze/credit_spread.py（IV−RV≥2/5、缓冲 1%）", "卖方价差适用性", "T3", "untested",
       ["credit_spread: 适用/不适用、适合度评分"], "移除预测性适用判断，保留结构计算"),
    _p("condor.gates", "analyze/condor.py（DTE、Δ、OI、1/3 权利金、偏斜、墙夹持）", "铁鹰适用性", "T3", "untested",
       ["condor: 适用/不适用、适合度评分"], "移除预测性适用判断，保留结构计算与流动性检查"),
    _p("credit_wall.v1", "analyze/credit_wall.py", "墙位卖方价差三档", "T3", "negative_sample",
       ["cli: 渲染操作推荐"], "停止作为操作推荐，保留历史证据",
       ["credit_wall_conservative", "credit_wall_aggressive"]),
    _p("kelly.sizing", "Kelly 仓位", "仓位", "T3", "invalid_inputs", ["（仓位建议）"], "不作仓位依据", ["kelly_sizing"]),
    _p("cost_gate.expected_move", "analyze/cost_gate.py", "预期波动 vs 回本门槛", "T3", "insufficient",
       ["cli: 成本闸门板块（经 ≥2× 解锁）"], "只观察", ["expected_move"]),
    _p("research.misc", "resonance / strength / squeeze / TA / Supertrend", "各类", "T3", "untested",
       ["（观察与评分）"], "只观察", ["ta_indicators_direction", "trend_as_filter"]),
    _p("wall.leg_selection", "analyze/shadow.py: 墙选腿 A（v5）", "ETF 池 · 2–4 天卖方价差 · 损益/风险", "T3",
       "prospective_study", ["（影子账，不进报告结论）"], "冻结研究照常，不因展示降级中断",
       ["docs/prereg/2026-09-26_direction_v1.3.md", "wall_edge_vs_placebo"],
       prereg_ref="shadow-v5-20260926 + dir-analysis-v1.3"),
    # ── 观测：只展示 ──
    _o("obs.flow_raw", "analyze/flow.py: ΔOI、成交、买卖方分侧", "当日持仓变化（事实）"),
    _o("obs.walls", "analyze/gamma.py: 墙位位置与 OI", "OI 集中位置（事实；「支撑/可卖」需预测证据）"),
    _o("obs.stretch_reading", "analyze/stretch.py: 距均线 ATR 数", "当前读数（事实）"),
    _o("obs.vol_surface", "analyze/vol: ATM IV、偏斜、期限结构", "当前读数（事实）"),
    _o("obs.cot_macro_raw", "collect: COT 持仓、宏观序列原值", "原始数据（事实）"),
    _o("obs.structure_calc", "strategy/credit_spread/condor 的权利金、最大亏损、盈亏平衡、Greeks", "确定性计算",
       note="静态到期最大亏损不是所有提前执行情景的实际亏损上界；残腿未知规则保留"),
    _o("obs.calendar_events", "core/calendar: 已知日历事件", "事件提示（除非有用户规则或已验证主张，不自动否决方向）"),
    # ── 可行性 ──
    _f("feas.quote_quality", "报价完整、时效、倒挂", "结构计算前提"),
    _f("feas.leg_match", "合约匹配、到期/行权价存在", "结构计算前提"),
    _f("feas.risk_computable", "风险能否算出（缺值为未知，不折零）", "风控前提"),
    _f("feas.broker_unverified", "券商保证金、单腿退出、到期处置未核实", "执行性（未知就说未知，不给「可执行」）"),
    # ── 用户政策 ──
    _pol("policy.risk_limits", "analyze/risk_policy.py", "单笔止损风险 ≤10%、单笔最大亏损 ≤20%、同簇 ≤20%（草案）",
         "analyze/risk_policy.py POLICY['version']"),
    _pol("policy.soul_rules", "soul/profile.py 置顶铁律", "可交易范围、计划后不乱动、亏后不加码",
         "data/soul/profile.json（私有；只引用标识）"),
    _pol("policy.rr_minimum", "analyze/risk_reward.py RR_MIN", "盈亏比下限",
         "AGENTS.md 金融语义 + 用户纪律", note="只在用户给定目标与止损时适用；自动斐波目标不适用"),
]

CLAIMS: dict[str, Claim] = {}
for _c in _ALL:
    if _c.claim_id in CLAIMS:
        raise ValueError(f"重复 claim_id：{_c.claim_id}")
    CLAIMS[_c.claim_id] = _c


def decision_allowed(claim_id: str, use: str) -> bool:
    """该主张能否用于某个用途。未登记 → False（默认无权限）。

    prediction：只有 T1 且用途在 allowed_uses 里才可用于展示以外的用途；T2/T3 只能展示。
    observation：只能展示。feasibility / policy：按 allowed_uses。"""
    if use not in USES:
        raise ValueError(f"未知用途：{use}")
    c = CLAIMS.get(claim_id)
    if c is None:
        return False
    if use == "display":
        return True
    if c.role == "prediction":
        return c.tier == "T1" and use in c.allowed_uses
    if c.role == "observation":
        return False
    return use in c.allowed_uses


def tier_of(claim_id: str) -> str | None:
    c = CLAIMS.get(claim_id)
    return c.tier if c else None


def t1_claims() -> list[Claim]:
    return [c for c in CLAIMS.values() if c.role == "prediction" and c.tier == "T1"]


def render_md() -> str:
    """权限清单（审计输出）：用 `undertow claims` 打印。"""
    out = ["# 主张权限清单", "", f"T1 可决策：{len(t1_claims())} 条"
           + ("（当前为空：本系统暂无通过验证的方向依据）" if not t1_claims() else ""), "",
           "| claim_id | 角色 | 等级 | 原因 | 允许用途 | 适用范围 | 迁移前消费者 | 目标动作 | 状态 | 证据 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for c in CLAIMS.values():
        out.append(f"| {c.claim_id} | {c.role} | {c.tier or '—'} | {REASONS.get(c.reason, c.reason)} | "
                   f"{','.join(c.allowed_uses)} | {c.scope} | {'；'.join(c.consumers) or '—'} | {c.target_action} | "
                   f"{c.migration} | {'；'.join(c.evidence_refs) or c.policy_ref or '—'} |")
    return "\n".join(out) + "\n"
