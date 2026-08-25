"""持仓/拟开仓体检（纯确定性规则，无 I/O）。

吃 PortfolioReview（+ AccountCapital），跑一组规则化检查，把常见的坑显性预警：
  - 近到期 + 贴价/价内的空头腿 → 被指派风险（尤其资金不够接货）
  - 卖方盈亏比过低（冒大险赚小钱）→ 折算所需胜率
  - 窄价差 + 近到期 → gamma 风险（小波动大摆动、无时间修复）
  - 裸卖未封顶、逆势于综合研判、单品种集中度过高

**立场**：只作波段级风险情景预警，**非投资建议、非交易指令**；建议均为"权衡/参考"口径。
数字全部来自上游确定性模块，LLM 不碰算术。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# —— 阈值（集中可调）——
NEAR_EXPIRY_DTE = 7          # DTE ≤ 此算"近到期"
TIGHT_WIDTH_PCT = 0.03       # 价差宽度/现价 ≤ 此算"窄价差"
POOR_RR_SELLER = 1.0         # 卖方 max_profit/max_loss < 此 = 盈亏比偏低
CONC_HIGH_FRAC = 0.40        # 单品种风险资金 / 净资产 ≥ 此 = 集中度偏高


@dataclass(frozen=True)
class HealthFinding:
    severity: str            # 高 / 中 / 低
    code: str
    title: str
    detail: str
    suggestion: str          # 权衡/参考口径，非指令
    scope: str = ""          # 涉及的品种/合约


_SEV_ORDER = {"高": 0, "中": 1, "低": 2}


def _breakeven_winrate(max_profit, max_loss) -> float | None:
    """收权金结构折算盈亏平衡胜率 = 最大亏/(最大亏+最大盈)。"""
    if not max_profit or not max_loss:
        return None
    mp, ml = abs(max_profit), abs(max_loss)
    return ml / (ml + mp) if (ml + mp) > 0 else None


def _combo_min_dte(combo) -> int | None:
    dtes = [l.dte for l in combo.legs if getattr(l, "dte", None) is not None]
    return min(dtes) if dtes else None


def check_group(g, capital) -> list[HealthFinding]:
    out: list[HealthFinding] = []
    spot = None
    # 现价从任一腿的 dist 反推不稳，改由 combo 用不到；被指派判断用腿的 moneyness。

    # —— 组合级：盈亏比 / 窄价差近到期 / 未封顶 ——
    for c in g.combos:
        dte = _combo_min_dte(c)
        # 卖方盈亏比过低（收权金结构：max_profit 小、max_loss 大）
        if c.net_credit and c.net_credit > 0 and c.max_profit and c.max_loss:
            rr = c.max_profit / c.max_loss
            if rr < POOR_RR_SELLER:
                wr = _breakeven_winrate(c.max_profit, c.max_loss)
                sev = "中"
                extra = ""
                if dte is not None and dte <= NEAR_EXPIRY_DTE:
                    sev = "高"
                    extra = f"，且剩 {dte} 天近到期（gamma 大、无时间修复）"
                out.append(HealthFinding(
                    severity=sev, code="POOR_RR", title="盈亏比偏低的收权金结构",
                    detail=(f"{c.label}：最大盈 ${c.max_profit:,.0f} / 最大亏 ${c.max_loss:,.0f}"
                            f"（R:R {rr:.2f}）"
                            + (f"，折算需胜率 > {wr*100:.0f}% 才不亏期望" if wr else "") + extra),
                    suggestion="冒大险赚小钱要靠高胜率；先看盈亏比：<1 的卖方结构要么放宽间距/拉远到期提升权金，要么控制仓位。",
                    scope=f"{g.underlying} · {c.label}"))
        # 窄价差 + 近到期（gamma 风险，本次对话那条教训）
        if len(c.legs) == 2 and dte is not None and dte <= NEAR_EXPIRY_DTE:
            strikes = [l.strike for l in c.legs if l.strike is not None]
            if len(strikes) == 2:
                width = abs(strikes[0] - strikes[1])
                ref = max(strikes)
                if ref > 0 and width / ref <= TIGHT_WIDTH_PCT:
                    out.append(HealthFinding(
                        severity="中", code="TIGHT_NEAR", title="窄价差 + 近到期（gamma 风险）",
                        detail=f"{c.label} 宽仅 {width:g}、剩 {dte} 天：近到期 gamma 大，"
                               f"标的一点波动就把薄缓冲击穿，且无时间均值回归。",
                        suggestion="θ 要赚，但别拖到最后贴价几天；常见做法 30–45 DTE 开、到 ~50% 利润或 ~21 DTE 前了结。",
                        scope=f"{g.underlying} · {c.label}"))
        # 未封顶风险
        if not c.defined_risk and c.stance and ("卖" in c.label or "空头" in c.label):
            out.append(HealthFinding(
                severity="中", code="UNDEFINED_RISK", title="风险未封顶的裸卖结构",
                detail=f"{c.label} 无对侧保护腿，下行/上行风险未定义。",
                suggestion="小账户尤其慎用裸卖；可加保护腿转成定义风险价差。",
                scope=f"{g.underlying} · {c.label}"))

    # —— 腿级：近到期被指派 + 资金不够接货 ——
    for lg in g.legs:
        if lg.kind not in ("C", "P") or lg.qty >= 0 or lg.dte is None:
            continue
        near = lg.dte <= NEAR_EXPIRY_DTE and lg.moneyness in ("贴价", "价内")
        if not near:
            continue
        if lg.kind == "P":
            assign = lg.strike * 100 * abs(lg.qty)
            if capital is not None and capital.buy_power < assign:
                out.append(HealthFinding(
                    severity="高", code="ASSIGN_CAPITAL_GAP",
                    title="近到期被指派 × 资金不够接货",
                    detail=(f"{lg.name}：剩 {lg.dte} 天且{lg.moneyness}；接货需 ${assign:,.0f}，"
                            f"购买力仅 ${capital.buy_power:,.0f}。到期被指派会触发垫付/强平。"),
                    suggestion="资金不足接货就别拖到期：到期前平仓或向下/向后展期(roll)，别让它到期指派。",
                    scope=f"{g.underlying} · {lg.name}"))
            else:
                out.append(HealthFinding(
                    severity="中", code="ASSIGN_NEAR",
                    title="近到期被指派风险",
                    detail=f"{lg.name}：剩 {lg.dte} 天且{lg.moneyness}，被指派概率上升。",
                    suggestion="愿接货可留（备足现金）；不愿则到期前平仓/展期。",
                    scope=f"{g.underlying} · {lg.name}"))
        else:  # 卖 call 被叫走
            out.append(HealthFinding(
                severity="中", code="CALLAWAY_NEAR", title="近到期被叫走风险",
                detail=f"{lg.name}：剩 {lg.dte} 天且{lg.moneyness}。",
                suggestion="持正股可接受被行权；否则平仓/向上展期。",
                scope=f"{g.underlying} · {lg.name}"))

    # —— 逆势于综合研判 ——
    bad = [lg for lg in g.legs if lg.align == "逆势"]
    if bad:
        names = "、".join(lg.name for lg in bad)
        out.append(HealthFinding(
            severity="低", code="COUNTER_TREND", title="有腿逆势于综合研判",
            detail=f"{names} 方向与综合研判（{g.bias}）相反。",
            suggestion="逆势属负 edge 的押注，注意仓位与止损。",
            scope=g.underlying))

    # —— 单品种集中度 ——
    if capital is not None and capital.net_assets > 0:
        risk = sum((c.capital_at_risk or 0) for c in g.combos)
        if risk >= CONC_HIGH_FRAC * capital.net_assets:
            out.append(HealthFinding(
                severity="中", code="CONCENTRATION", title="单品种集中度偏高",
                detail=f"{g.display_name} 风险资金 ${risk:,.0f} ≈ 净资产 {risk/capital.net_assets*100:.0f}%。",
                suggestion="单一标的占比过高，一次逆行冲击全账户；分散或降规模可控回撤。",
                scope=g.underlying))
    return out


def run_healthcheck(review, capital=None) -> list[HealthFinding]:
    """对整个组合跑体检，按严重度排序返回。"""
    findings: list[HealthFinding] = []
    for g in review.groups:
        findings += check_group(g, capital)
    findings.sort(key=lambda f: _SEV_ORDER.get(f.severity, 9))
    return findings
