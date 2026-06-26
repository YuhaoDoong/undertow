"""信号解读层：把 PositioningAnalysis 翻译成规则化、带方向的提示。

重要立场（务必牢记，已在文档中反复强调）:
  - 这些信号是"概率性的风险情境"，不是确定性的涨跌预言。
  - COT 滞后约 3 天，只适合波段级判断，不适合日内。
  - 投机资金（Managed Money）极端持仓常作【反指】用，而非跟随。
  - Swap Dealers 方向含大量 OTC 对冲，方向解读歧义大，仅作辅助。
阈值集中在本文件顶部，便于回测后调参。
"""
from __future__ import annotations

from dataclasses import dataclass

from .positioning import PositioningAnalysis

# —— 可调阈值 ——
CROWD_HIGH_PCT = 85.0       # 净头寸分位高于此 -> 拥挤
CROWD_EXTREME_PCT = 95.0    # 极端拥挤
CROWD_LOW_PCT = 15.0
CROWD_EXTREME_LOW_PCT = 5.0
WEAK_CONVICTION_ABS = 3000  # 净变化绝对值超过此才认为"显著"


@dataclass(frozen=True)
class Signal:
    code: str
    title: str
    direction: str  # bullish / bearish / risk-up(挤空风险) / risk-down(回调风险) / neutral
    detail: str
    strength: str = "中"  # 强 / 中 / 弱


def _crowding_signals(an: PositioningAnalysis) -> list[Signal]:
    out: list[Signal] = []
    mm = an.categories["managed_money"]
    if mm.net_percentile is None:
        return out

    p = mm.net_percentile
    if mm.net >= 0 and p >= CROWD_HIGH_PCT:
        extreme = p >= CROWD_EXTREME_PCT
        out.append(Signal(
            code="MM_CROWDED_LONG",
            title="投机资金多头拥挤",
            direction="risk-down",
            strength="强" if extreme else "中",
            detail=(
                f"Managed Money 净多 {mm.net:,} 手，处于近 {an.lookback_used} 周的 "
                f"{p:.0f}% 分位（z={mm.net_zscore:+.1f}）。多头过度拥挤，一旦出现利空"
                f"催化，平仓踩踏会放大下行——作【反指】看待回调脆弱性。"
            ),
        ))
    elif mm.net <= 0 and p <= CROWD_LOW_PCT:
        extreme = p <= CROWD_EXTREME_LOW_PCT
        out.append(Signal(
            code="MM_CROWDED_SHORT",
            title="投机资金空头拥挤",
            direction="risk-up",
            strength="强" if extreme else "中",
            detail=(
                f"Managed Money 净空（净 {mm.net:,} 手），处于近 {an.lookback_used} 周的 "
                f"{p:.0f}% 分位（z={mm.net_zscore:+.1f}）。空头拥挤，利多催化下易触发挤空反弹。"
            ),
        ))
    return out


def _conviction_signal(an: PositioningAnalysis) -> list[Signal]:
    """本周投机资金净变化的'质量'：主动建仓 vs 被动平仓。"""
    mm = an.categories["managed_money"]
    d = mm.decomposition
    if abs(d.net_change) < WEAK_CONVICTION_ABS:
        return []
    direction = "bullish" if d.net_change > 0 else "bearish"
    return [Signal(
        code="MM_FLOW_QUALITY",
        title=f"投机资金本周{d.driver}",
        direction=direction,
        strength=d.conviction,
        detail=(
            f"Managed Money 净{'增' if d.net_change>0 else '减'} {abs(d.net_change):,} 手"
            f"（Δ多 {d.long_change:+,} / Δ空 {d.short_change:+,}），主导为「{d.driver}」，"
            f"持续性{d.conviction}。"
            + ("空头回补/多头了结型变化往往一两周就被打回，别当趋势。"
               if d.conviction == "弱" else "主动建仓持续性相对更强。")
        ),
    )]


def _smart_money_divergence(an: PositioningAnalysis) -> list[Signal]:
    """聪明钱（Other Reportables）与投机资金的背离 —— 文章重点。"""
    mm = an.categories["managed_money"]
    other = an.categories["other_reportables"]
    md, od = mm.decomposition, other.decomposition
    # 反弹/上涨中投机加多，但聪明钱反而净减 -> 防守背离
    if md.net_change > 0 and od.net_change < -WEAK_CONVICTION_ABS // 2:
        return [Signal(
            code="SMART_DIVERGE_BEAR",
            title="聪明钱背离（防守）",
            direction="bearish",
            strength="中",
            detail=(
                f"投机资金本周净多 {md.net_change:+,}，但 Other Reportables 净{('减' )} "
                f"{abs(od.net_change):,} 手（在主动做防守）。聪明钱不跟，反弹质量存疑。"
            ),
        )]
    if md.net_change < 0 and od.net_change > WEAK_CONVICTION_ABS // 2:
        return [Signal(
            code="SMART_DIVERGE_BULL",
            title="聪明钱背离（吸筹）",
            direction="bullish",
            strength="中",
            detail=(
                f"投机资金本周净减 {md.net_change:+,}，但 Other Reportables 逆势净增 "
                f"{od.net_change:+,} 手。聪明钱在下跌中吸筹，关注企稳。"
            ),
        )]
    return []


def _swap_dealer_pressure(an: PositioningAnalysis) -> list[Signal]:
    """互换交易商方向性压力 —— 复刻文章逻辑，但强标歧义。"""
    swap = an.categories["swap_dealers"]
    d = swap.decomposition
    # 文章逻辑：short 大增 且 spread 减少 -> 更清晰的方向性空头压力
    if d.short_change > WEAK_CONVICTION_ABS and d.spread_change < 0:
        return [Signal(
            code="SWAP_DIR_SHORT",
            title="互换商方向性空头压力",
            direction="bearish",
            strength="中",
            detail=(
                f"Swap Dealers 空头大增 {d.short_change:+,}、跨期套利反而减少 "
                f"{d.spread_change:+,}，方向性空头特征较清晰。但注意：这可能是 OTC 客户做空、"
                f"实体套保或期权 delta 对冲，未必是主动砸盘——仅作辅助佐证。"
            ),
        )]
    if d.long_change > WEAK_CONVICTION_ABS and d.spread_change < 0:
        return [Signal(
            code="SWAP_DIR_LONG",
            title="互换商方向性多头压力",
            direction="bullish",
            strength="弱",
            detail=(
                f"Swap Dealers 多头增 {d.long_change:+,}、跨期套利减少 {d.spread_change:+,}。"
                f"方向偏多，但同样含 OTC 对冲歧义，仅作辅助。"
            ),
        )]
    return []


def generate_signals(an: PositioningAnalysis) -> list[Signal]:
    """汇总所有规则信号。顺序大致按重要性。"""
    signals: list[Signal] = []
    signals += _crowding_signals(an)
    signals += _conviction_signal(an)
    signals += _smart_money_divergence(an)
    signals += _swap_dealer_pressure(an)
    return signals


def net_bias(signals: list[Signal]) -> str:
    """对信号做一个粗的方向汇总（仅供速览，不可当交易指令）。"""
    score = 0
    weight = {"强": 2, "中": 1, "弱": 1}
    for s in signals:
        w = weight.get(s.strength, 1)
        if s.direction in ("bullish", "risk-up"):
            score += w
        elif s.direction in ("bearish", "risk-down"):
            score -= w
    if score >= 2:
        return "偏多"
    if score <= -2:
        return "偏空"
    return "中性/分歧"
