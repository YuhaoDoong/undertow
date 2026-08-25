"""交易灵魂档案（trading soul）——用户专属的交易体系、纪律与已知弱点。

这是 undertow 里**唯一以"人"为对象**的模块：其它层分析市场，这一层**约束交易者自己**。

三件事：
  1. **沉淀**：把与 AI 讨论中确立的交易哲学、铁律、纪律、历史教训写成结构化档案（可版本演进）；
  2. **执行**：把其中【可机器检查】的限额（单笔风险%、集中度、最低盈亏比、最短 DTE、禁止项）
     变成对当前持仓/拟开仓的确定性检查——纪律不再靠记忆，靠代码；
  3. **贯通**：档案进 `consult` 上下文包，任何与你讨论的 AI（我或你接入的其它模型）都先读到
     **你的规则**，据此给意见，而不是给一套通用说辞。

**隐私**：档案含个人交易史与心理弱点，属敏感数据——落 gitignore 的 `data/soul/`，**绝不进公开仓库**。
**立场**：这里的规则是**用户自己定的纪律**，不是投资建议；模块只负责忠实记录与检查。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

SCHEMA = "undertow.soul/v1"


@dataclass(frozen=True)
class Rule:
    id: str
    text: str                  # 规则本身（第一人称，用户口径）
    why: str = ""              # 为什么有这条（通常来自一次真实教训）
    severity: str = "铁律"      # 铁律（不可违）/ 纪律（应遵守）/ 偏好


@dataclass(frozen=True)
class Weakness:
    id: str
    name: str
    evidence: str = ""         # 来自历史的证据（真实数据/真实交易）
    trigger: str = ""          # 什么情形下容易犯
    counter: str = ""          # 对策


@dataclass(frozen=True)
class Lesson:
    when: str                  # 日期或时间段
    what: str                  # 做了什么
    outcome: str               # 结果
    lesson: str                # 提炼


@dataclass(frozen=True)
class Limits:
    """可机器检查的限额。None = 该项不检查。"""
    max_risk_per_trade_pct: float | None = None   # 单笔最大风险占净资产 %
    max_concentration_pct: float | None = None    # 单品种风险资金占净资产 %
    min_rr: float | None = None                   # 最低盈亏比
    min_dte_open: int | None = None               # 新开仓最短到期天数
    min_dte_hold_short: int | None = None         # 空头腿最晚持有到剩几天（少于则应了结）
    max_trades_per_day: int | None = None         # 单日最多交易笔数（防冲动扎堆）
    forbid_liquidation_risk: bool = False         # 禁止会被强平的结构（高杠杆/裸卖）
    forbid_binary_event: bool = False             # 禁止在二元事件前建投机仓


@dataclass(frozen=True)
class SoulProfile:
    schema: str = SCHEMA
    updated: str = ""
    owner: str = ""
    phase: str = ""                                    # 当前阶段（如"重建期：先证明能活满N笔"）
    north_star: str = ""                               # 一句话总纲
    rules: list = field(default_factory=list)          # list[Rule]
    weaknesses: list = field(default_factory=list)     # list[Weakness]
    lessons: list = field(default_factory=list)        # list[Lesson]
    limits: Limits = field(default_factory=Limits)
    notes: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rules or self.limits != Limits())


DEFAULT_PATH = Path("data/soul/profile.json")


def load_profile(path: Path | None = None) -> SoulProfile | None:
    """读档案；不存在返回 None（调用方优雅降级）。"""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return SoulProfile(
        schema=raw.get("schema", SCHEMA),
        updated=raw.get("updated", ""),
        owner=raw.get("owner", ""),
        phase=raw.get("phase", ""),
        north_star=raw.get("north_star", ""),
        rules=[Rule(**r) for r in raw.get("rules", [])],
        weaknesses=[Weakness(**w) for w in raw.get("weaknesses", [])],
        lessons=[Lesson(**l) for l in raw.get("lessons", [])],
        limits=Limits(**raw.get("limits", {})),
        notes=raw.get("notes", ""),
    )


def save_profile(profile: SoulProfile, path: Path | None = None) -> Path:
    """写档案（含敏感个人内容，路径须在 gitignore 的 data/soul/ 下）。"""
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ————————————————————————————————————————————————— 纪律检查


@dataclass(frozen=True)
class SoulViolation:
    severity: str        # 违反铁律 / 触碰纪律
    rule_id: str
    title: str
    detail: str
    scope: str = ""


def check_against_profile(review, capital, profile: SoulProfile | None) -> list[SoulViolation]:
    """把当前持仓/拟开仓对照【用户自己的限额】做确定性检查。

    与 healthcheck（通用市场风险）互补：这里检查的是**你自己定的纪律有没有被破**。
    """
    if profile is None or not profile.ok:
        return []
    lim = profile.limits
    out: list[SoulViolation] = []
    net = capital.net_assets if capital else None

    for g in review.groups:
        # 单品种集中度
        risk = sum((c.capital_at_risk or 0) for c in g.combos)
        if lim.max_concentration_pct is not None and net:
            pct = risk / net * 100
            if pct > lim.max_concentration_pct:
                out.append(SoulViolation(
                    severity="违反铁律", rule_id="max_concentration_pct",
                    title="单品种集中度超过自定上限",
                    detail=f"{g.display_name} 风险资金 ${risk:,.0f} ≈ 净资产 {pct:.0f}%，"
                           f"超过你设的 {lim.max_concentration_pct:.0f}%。",
                    scope=g.underlying))

        for c in g.combos:
            # 单笔风险
            if lim.max_risk_per_trade_pct is not None and net and c.capital_at_risk:
                pct = c.capital_at_risk / net * 100
                if pct > lim.max_risk_per_trade_pct:
                    out.append(SoulViolation(
                        severity="违反铁律", rule_id="max_risk_per_trade_pct",
                        title="单笔风险超过自定上限",
                        detail=f"{c.label} 风险 ${c.capital_at_risk:,.0f} ≈ 净资产 {pct:.0f}%，"
                               f"超过你设的 {lim.max_risk_per_trade_pct:.0f}%。",
                        scope=f"{g.underlying} · {c.label}"))
            # 最低盈亏比（对有明确最大盈亏的结构）
            if lim.min_rr is not None and c.max_profit and c.max_loss:
                rr = c.max_profit / c.max_loss
                if rr < lim.min_rr:
                    out.append(SoulViolation(
                        severity="触碰纪律", rule_id="min_rr",
                        title="盈亏比低于自定闸门",
                        detail=f"{c.label} R:R {rr:.2f} < 你设的 {lim.min_rr:.1f}。",
                        scope=f"{g.underlying} · {c.label}"))
            # 风险未封顶（会被强平的结构）
            if lim.forbid_liquidation_risk and not c.defined_risk and \
               ("卖" in c.label or "空头" in c.label):
                out.append(SoulViolation(
                    severity="违反铁律", rule_id="forbid_liquidation_risk",
                    title="持有会被强平/风险未封顶的结构",
                    detail=f"{c.label} 无对侧保护腿。你的铁律：在建立出场纪律前不碰会强平的东西。",
                    scope=f"{g.underlying} · {c.label}"))

        # 空头腿到期纪律
        if lim.min_dte_hold_short is not None:
            for lg in g.legs:
                if lg.kind in ("C", "P") and lg.qty < 0 and lg.dte is not None \
                   and 0 <= lg.dte < lim.min_dte_hold_short:
                    out.append(SoulViolation(
                        severity="触碰纪律", rule_id="min_dte_hold_short",
                        title="空头腿持有过近到期",
                        detail=f"{lg.name} 剩 {lg.dte} 天，短于你设的"
                               f"「空头腿剩 {lim.min_dte_hold_short} 天前了结」。",
                        scope=f"{g.underlying} · {lg.name}"))
    return out


def render_profile_md(p: SoulProfile | None) -> str:
    """档案的人读版。"""
    if p is None or not p.ok:
        return ("# 交易灵魂档案\n\n- 尚未建立（`data/soul/profile.json` 不存在）。\n"
                "- 与 AI 讨论中逐步确立你的铁律/纪律/弱点后写入即可。\n")
    L = ["# 交易灵魂档案（个人专属 · 本地私有）", ""]
    if p.north_star:
        L.append(f"> **总纲**：{p.north_star}")
        L.append("")
    meta = " · ".join(x for x in [p.owner, p.phase, f"更新 {p.updated}" if p.updated else ""] if x)
    if meta:
        L.append(f"*{meta}*")
        L.append("")
    if p.rules:
        L.append("## 铁律与纪律")
        for r in p.rules:
            L.append(f"- **[{r.severity}] {r.text}**" + (f"\n  - 因为：{r.why}" if r.why else ""))
        L.append("")
    lim = p.limits
    rows = [
        ("单笔最大风险", f"{lim.max_risk_per_trade_pct:.0f}% 净资产" if lim.max_risk_per_trade_pct else None),
        ("单品种集中度上限", f"{lim.max_concentration_pct:.0f}%" if lim.max_concentration_pct else None),
        ("最低盈亏比", f"{lim.min_rr:.1f}" if lim.min_rr else None),
        ("新开仓最短到期", f"{lim.min_dte_open} 天" if lim.min_dte_open else None),
        ("空头腿了结线", f"剩 {lim.min_dte_hold_short} 天前" if lim.min_dte_hold_short else None),
        ("单日最多交易", f"{lim.max_trades_per_day} 笔" if lim.max_trades_per_day else None),
        ("禁止强平风险结构", "是" if lim.forbid_liquidation_risk else None),
        ("禁止二元事件投机", "是" if lim.forbid_binary_event else None),
    ]
    rows = [(k, v) for k, v in rows if v]
    if rows:
        L.append("## 机器检查的限额（每次 account/consult 自动核）")
        L.append("")
        L.append("| 项 | 值 |")
        L.append("|---|---|")
        for k, v in rows:
            L.append(f"| {k} | **{v}** |")
        L.append("")
    if p.weaknesses:
        L.append("## 已知弱点（来自真实历史）")
        for w in p.weaknesses:
            L.append(f"- **{w.name}**")
            if w.evidence:
                L.append(f"  - 证据：{w.evidence}")
            if w.trigger:
                L.append(f"  - 触发情形：{w.trigger}")
            if w.counter:
                L.append(f"  - 对策：{w.counter}")
        L.append("")
    if p.lessons:
        L.append("## 教训档案")
        for ls in p.lessons:
            L.append(f"- **{ls.when}** {ls.what} → {ls.outcome}")
            L.append(f"  - 提炼：{ls.lesson}")
        L.append("")
    if p.notes:
        L.append(f"> {p.notes}")
    return "\n".join(L)
