"""波动率环境研判：期权现在偏贵还是偏便宜 → 波段级"买方 / 卖方"倾向。

判据（全部确定性计算，纯标准库）：
  1. IV 近1年分位（品种"VIX"：GVZ/OVX/VXSLV）——期权相对【自己历史】贵不贵。
  2. ATM IV − 已实现波动率 RV——期权相对【标的实际波动】贵不贵（即波动率溢价）。
  3. IV 20日趋势——在扩张还是回落（不投票，仅作择时/风险提示）。

倾向规则（波段级、非交易指令）：
  * 分位高 + ATM IV 明显高于 RV → 偏卖方（卖权收溢价、赌均值回归）。
  * 分位低 + ATM IV 低于 RV     → 偏买方（期权便宜、有扩张空间）。
  * 判据相互矛盾或都不显著        → 中性。

诚实边界：这是"波动率环境倾向"，不含具体行权价/到期选择（那需结合 Gamma 墙位与
到期日）；事件日（非农/CPI/FOMC 兑现）IV 回落含事件溢价机械释放，判读要打折。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# —— 阈值（pp = 百分数点；分位 0~100）——
IV_PCT_HI = 70.0        # 分位 ≥ 此 → 期权偏贵（利卖方）
IV_PCT_LO = 30.0        # 分位 ≤ 此 → 期权偏便宜（利买方）
IV_RV_SIG = 2.0         # |ATM IV − RV| 超过此(pp)才算显著
IV_TREND_SIG = 1.0      # |20日 IV 变化| 超过此(pp)才算在明显扩张/回落
RV_WINDOW = 20          # 已实现波动率回看窗口（交易日）
RV_MIN_OBS = 6          # 少于此收益样本则不算 RV
_ANNUALIZE = math.sqrt(252.0)


@dataclass(frozen=True)
class VolRegime:
    """波动率环境读数 + 期权买卖方倾向。字段单位：IV/RV 均为 pp。"""
    stance: str                        # 偏卖方 / 中性 / 偏买方 / 数据不足
    score: int                         # 正=偏卖方 负=偏买方
    iv_index_name: str | None = None   # GVZ / OVX / VXSLV
    iv_index_latest: float | None = None
    iv_pct: float | None = None        # 近1年分位 0~100
    iv_chg_20d: float | None = None
    atm_iv_pp: float | None = None     # 近月 ATM IV
    rv_pp: float | None = None         # 近 RV_WINDOW 日已实现波动率（年化）
    iv_minus_rv: float | None = None   # atm_iv − rv（波动率溢价）
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return self.iv_pct is not None or self.iv_minus_rv is not None


def realized_vol(closes: list[float] | None, window: int = RV_WINDOW) -> float | None:
    """close-to-close 对数收益的年化标准差（×100 → pp）。

    需 ≥ RV_MIN_OBS 个有效收益样本；剔除 None / 非正收盘价。
    """
    if not closes:
        return None
    clean = [c for c in closes if c is not None and c > 0]
    if len(clean) < RV_MIN_OBS + 1:
        return None
    rets = [math.log(clean[i] / clean[i - 1]) for i in range(1, len(clean))]
    rets = rets[-window:]
    if len(rets) < RV_MIN_OBS:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)  # 样本方差
    return math.sqrt(var) * _ANNUALIZE * 100.0


def assess_vol_regime(*, iv_reading=None, atm_iv_pp: float | None = None,
                      closes: list[float] | None = None) -> VolRegime:
    """组合三判据 → 波动率环境倾向。

    iv_reading: macro.VolReading | None（提供 latest / chg_20d / percentile_1y / name）。
    atm_iv_pp:  近月 ATM IV（pp）| None。
    closes:     真实标的日线收盘序列（算 RV）| None。
    """
    score = 0
    reasons: list[str] = []
    caveats: list[str] = []

    iv_name = getattr(iv_reading, "name", None)
    iv_latest = getattr(iv_reading, "latest", None)
    iv_pct = getattr(iv_reading, "percentile_1y", None)
    iv_chg = getattr(iv_reading, "chg_20d", None)
    if iv_pct is not None and (iv_pct != iv_pct):  # NaN 防御
        iv_pct = None

    # —— 判据 1：IV 近1年分位 ——
    if iv_pct is not None:
        if iv_pct >= IV_PCT_HI:
            score += 1
            reasons.append(f"{iv_name} 近1年分位 {iv_pct:.0f}% 偏高 → 期权相对自身历史偏贵，利卖方")
        elif iv_pct <= IV_PCT_LO:
            score -= 1
            reasons.append(f"{iv_name} 近1年分位 {iv_pct:.0f}% 偏低 → 期权相对自身历史偏便宜，利买方")
        else:
            reasons.append(f"{iv_name} 近1年分位 {iv_pct:.0f}%（中位）→ 该维度不偏买也不偏卖")

    # —— 判据 2：ATM IV − 已实现波动率 RV ——
    rv = realized_vol(closes)
    iv_minus_rv = None
    if atm_iv_pp is not None and rv is not None:
        iv_minus_rv = atm_iv_pp - rv
        if iv_minus_rv >= IV_RV_SIG:
            score += 1
            reasons.append(
                f"ATM IV {atm_iv_pp:.1f} 高于近{RV_WINDOW}日实际波动 {rv:.1f}"
                f"（+{iv_minus_rv:.1f}pp）→ 含波动率溢价，卖方有正期望空间")
        elif iv_minus_rv <= -IV_RV_SIG:
            score -= 1
            reasons.append(
                f"ATM IV {atm_iv_pp:.1f} 低于近{RV_WINDOW}日实际波动 {rv:.1f}"
                f"（{iv_minus_rv:.1f}pp）→ 期权定价偏低，买方占便宜")
        else:
            reasons.append(
                f"ATM IV {atm_iv_pp:.1f} ≈ 近{RV_WINDOW}日实际波动 {rv:.1f}"
                f"（{iv_minus_rv:+.1f}pp）→ 无明显溢价/折价")

    # —— 判据 3：IV 20日趋势（择时/风险提示，不投票）——
    stance = "数据不足"
    if score > 0:
        stance = "偏卖方"
    elif score < 0:
        stance = "偏买方"
    elif iv_pct is not None or iv_minus_rv is not None:
        stance = "中性"

    if iv_chg is not None:
        if stance == "偏卖方" and iv_chg >= IV_TREND_SIG:
            caveats.append(f"但 IV 近20日仍抬升 {iv_chg:+.1f}pp——卖方留意波动率继续扩张（如临近事件）")
        elif stance == "偏买方" and iv_chg <= -IV_TREND_SIG:
            caveats.append(f"但 IV 近20日在回落 {iv_chg:+.1f}pp——买方留意时间价值/波动率同步流失")

    if stance != "数据不足":
        caveats.append("这是波动率环境倾向，非交易指令；具体行权价/到期需结合 Gamma 墙位与到期日")

    return VolRegime(
        stance=stance, score=score,
        iv_index_name=iv_name, iv_index_latest=iv_latest,
        iv_pct=iv_pct, iv_chg_20d=iv_chg,
        atm_iv_pp=atm_iv_pp, rv_pp=rv, iv_minus_rv=iv_minus_rv,
        reasons=reasons, caveats=caveats,
    )
