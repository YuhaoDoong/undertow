"""波动压缩 —— 「蓄势」的量化，观察项（未通过显著性门槛）。

用户 2026-08-29：「技术分析的时候，有种三角形形态，就是波动率压缩，
震荡越来越小，趋向三角形后，可能会产生波动巨大的行情。」

📌 这条线索的由来值得记一笔：我先测了「ATM IV 高 → 次日大波动」，得到
   高 IV 组 23% / 低 IV 组 40%，当成"预判失败"记下了。用户提醒后才发现
   **同一份数字读反了方向** —— 不是 IV 高预示爆发，是 IV 被压到低位才预示爆发。

实测（146 样本 / 各品种自身 |日涨跌| 70 分位为门槛 / 基准大波动率 30%）：
    ATM IV 低位      最压缩 1/3 → 45%，最舒张 1/3 → 25%   +20pp  区间 [-5,+44]
    ATR5/ATR60 收缩  最压缩 1/3 → 45%，最舒张 1/3 → 27%   +18pp  区间 [-6,+41]
    10/60 日区间收敛  −9pp  ❌ 不成立，故【不纳入】
    三重同时           44% vs 29%  +15pp（n=18）

⚠️ 两重保留，缺一不可：
   1. 日聚类 bootstrap 的 95% 区间【跨 0】，且只有约 40 个日期聚类（<50）；
   2. 上面那些分位是按【整个回测区间】排序算的，含未来信息 ——
      属事后描述，不是盘前可复现的预测回测（codex 2026-08-29 P1-5）。
   所以本模块只输出观察标记，**不参与任何方向判定、不进综合分**。
   它说的是"可能要变天"，从不说"往哪边变" —— 方向仍归增仓层。
"""
from __future__ import annotations

from dataclasses import dataclass

# ⚠️ 这两个阈值是【拍的，未经校准】（codex 2026-08-29 P1 指出）。
# 回测里的 45% 是"各【单项】最低三分之一"的数字，不是下面这个 AND 规则的胜率 ——
# 精确布尔规则本身从未单独回测过。所以：
#   · 阈值只用来点亮一个观察标记，不参与任何判定；
#   · 用户可见文案里【不得出现具体胜率】，那会让人以为它被验证过。
IV_TIGHT = 0.35        # ATM IV 处于自身历史分位 ≤ 此值 = 被压
ATR_TIGHT = 0.85       # ATR5/ATR60 ≤ 此值 = 短期波幅收缩


@dataclass(frozen=True)
class Squeeze:
    ok: bool = False
    iv_pctile: float | None = None      # 0~1，当前 ATM IV 在自身历史里的分位
    atr_ratio: float | None = None      # ATR5 / ATR60
    tight: bool = False                 # 两项都满足
    note: str = ""

    @property
    def label(self) -> str:
        if not self.ok:
            return ""
        if self.tight:
            return "⏳ 波动压缩中"
        if (self.iv_pctile is not None and self.iv_pctile <= IV_TIGHT) or \
           (self.atr_ratio is not None and self.atr_ratio <= ATR_TIGHT):
            return "· 波动偏低"
        return ""


def _tr(highs, lows, closes) -> list[float]:
    return [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))]


def assess(*, iv_pctile: float | None,
           highs: list[float] | None, lows: list[float] | None,
           closes: list[float] | None) -> Squeeze:
    """两个维度都算不出来就返回 ok=False —— 不猜、不用单腿硬撑。

    iv_pctile：ATM IV 在自身历史里的分位（0~1），由 volregime 算好后传入。
    上一版让本函数自己从 VolRegime.history 算 —— 那个字段根本不存在，
    于是 tight 在生产里永远是 False（codex 2026-08-29 P1）。
    """
    iv_p = iv_pctile if (iv_pctile is not None and 0.0 <= iv_pctile <= 1.0) else None
    atr_r = None
    if closes and highs and lows and len(closes) >= 61:
        tr = _tr(highs, lows, closes)
        a5 = sum(tr[-5:]) / 5
        a60 = sum(tr[-60:]) / 60
        atr_r = (a5 / a60) if a60 else None
    if iv_p is None and atr_r is None:
        return Squeeze()
    tight = bool(iv_p is not None and iv_p <= IV_TIGHT
                 and atr_r is not None and atr_r <= ATR_TIGHT)
    bits = []
    if iv_p is not None:
        bits.append(f"ATM IV 处自身 {iv_p*100:.0f}% 分位")
    if atr_r is not None:
        bits.append(f"近5日波幅是60日均值的 {atr_r*100:.0f}%")
    note = "；".join(bits)
    if tight:
        # ⚠️ 不写具体胜率：这个 AND 规则没被单独回测过（见文件头说明）。
        note += "　→ 两项都被压到低位（阈值未校准，仅作观察，不构成判断）"
    return Squeeze(ok=True, iv_pctile=iv_p, atr_ratio=atr_r, tight=tight, note=note)


def render_pill(sq: Squeeze, esc) -> str:
    if not sq.ok or not sq.label:
        return ""
    col = "#9a6700" if sq.tight else "#6e7781"
    return (f'<span style="display:inline-block;margin:2px 4px 2px 0;padding:1px 7px;'
            f'border-radius:9px;font-size:11.5px;border:1px solid {col}44;'
            f'background:{col}12;color:{col}" title="{esc(sq.note)}">'
            f'{esc(sq.label)}</span>')
