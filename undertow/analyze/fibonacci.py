"""斐波那契回撤/扩展位（确定性计算）——把最近一段【显著摆动腿】拆成入场/止损/目标锚。

为什么加它（作者交易哲学的落地）：剑锋无尘明确以"这轮上涨起点 4020 → 高点 4447"的
斐波那契回撤定位加仓/短线入场区（0.382≈4284、0.5≈4234、0.618≈4184），止损放"起涨点
或 0.618 下方"，目标看扩展位。本模块只负责**确定性地**找出那段摆动腿并算出各档价位——
方向锚点交给上层 `risk_reward` 做盈亏比闸门，LLM 不碰算术。

口径：优先吃**真实期货价序列**（GC=F/SI=F/CL=F 的日高低），给出商品价档位；传入
ratio（真实期货价/ETF 价）则同时换算 ETF 行权价锚，实盘下单对得上。只作波段级结构参考，
非交易指令。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from undertow.core.models import PriceSeries

LOOKBACK_BARS = 90        # 摆动检测回看的交易日数
MIN_LEG_PCT = 2.0         # 摆动腿幅度下限（占起点%）——太小的腿没有回撤交易价值
REVERSAL_PCT = 3.0        # zigzag 反转阈值（%）——控制摆动腿的"尺度/粒度"
RETR_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)
EXT_RATIOS = (1.272, 1.618)   # 扩展目标（突破摆动极值后的常用延伸位）


@dataclass(frozen=True)
class FibLevel:
    ratio: float
    price: float              # 商品价（无 ratio 时即 ETF 价）
    etf: float | None         # ETF 行权价锚（有 ratio 时）
    kind: str                 # retr / ext / swing
    label: str                # "0.382" / "起涨点(1.0)" / "扩展1.618" …
    is_key: bool = False      # 黄金分割关键区（0.382 / 0.5 / 0.618）


@dataclass(frozen=True)
class FibAnalysis:
    ok: bool
    direction: str            # up（回撤=下方支撑，顺势=回调买）/ down（回撤=上方阻力，回调空）/ ""
    swing_low: float
    swing_high: float
    swing_low_date: date | None
    swing_high_date: date | None
    leg_pct: float            # 摆动腿幅度（占起点%）
    spot: float               # 现价（商品口径优先）
    etf_spot: float | None
    ratio: float | None
    retracements: list[FibLevel] = field(default_factory=list)
    extensions: list[FibLevel] = field(default_factory=list)
    current_zone: str = ""    # 现价落在哪两档之间
    lookback: int = LOOKBACK_BARS
    note: str = ""

    def level(self, ratio: float) -> float | None:
        for lv in self.retracements + self.extensions:
            if abs(lv.ratio - ratio) < 1e-9:
                return lv.price
        return None


def _zigzag_pivots(highs, lows, s: int, n: int, thr: float):
    """zigzag 转折点序列：价格自上一极值反向 ≥ thr 才确认一个转折点。

    比"窗口全局极值"稳健得多——后者在'低点更近但价格已大幅反弹'时会把当前上涨腿
    误判成老的下跌腿。zigzag 按幅度分段，最后两个转折点 = 【当前正在运行的那条腿】。
    返回 [(idx, price, 'H'|'L'), ...]。
    """
    pivots: list[tuple[int, float, str]] = []
    hi_i, hi = s, highs[s]
    lo_i, lo = s, lows[s]
    trend = 0  # 0 未知 / 1 上 / -1 下
    for i in range(s + 1, n):
        if trend >= 0:                        # 上行或未知：跟踪高点
            if highs[i] > hi:
                hi, hi_i = highs[i], i
            if lows[i] <= hi * (1 - thr):      # 自高点回落 ≥ thr → 确认高转折
                pivots.append((hi_i, hi, 'H'))
                trend, lo, lo_i = -1, lows[i], i
                continue
        if trend <= 0:                        # 下行或未知：跟踪低点
            if lows[i] < lo:
                lo, lo_i = lows[i], i
            if highs[i] >= lo * (1 + thr):     # 自低点反弹 ≥ thr → 确认低转折
                pivots.append((lo_i, lo, 'L'))
                trend, hi, hi_i = 1, highs[i], i
    # 收尾：把当前正在形成的极值当作暂定的最后一个转折点
    if trend == 1:
        pivots.append((hi_i, hi, 'H'))
    elif trend == -1:
        pivots.append((lo_i, lo, 'L'))
    return pivots


def _find_swing(series: PriceSeries, lookback: int, reversal_pct: float = REVERSAL_PCT):
    """在最近 lookback 根内用 zigzag 定位【当前正在运行的那条摆动腿】。

    终点=高（最后转折是 H）→ 上涨腿，回撤位在现价下方＝回调买候选区；
    终点=低（最后转折是 L）→ 下跌腿，回撤位在现价上方＝反抽卖候选区。
    zigzag 找不到转折（极安静序列）时退回窗口全局极值。
    返回 (direction, lo, hi, lo_date, hi_date) 或 None。
    """
    cs = series.closes
    n = len(cs)
    if n < 20:
        return None
    highs = series.highs if (series.highs and len(series.highs) == n) else cs
    lows = series.lows if (series.lows and len(series.lows) == n) else cs
    s = max(0, n - lookback)
    pivots = _zigzag_pivots(highs, lows, s, n, reversal_pct / 100.0)
    if len(pivots) >= 2:
        (i0, p0, k0), (i1, p1, k1) = pivots[-2], pivots[-1]
        if k1 == 'H' and k0 == 'L':
            return ("up", p0, p1, series.dates[i0], series.dates[i1])
        if k1 == 'L' and k0 == 'H':
            return ("down", p1, p0, series.dates[i1], series.dates[i0])
    # 退化：窗口全局极值（谁更靠后谁是终点）
    ih = max(range(s, n), key=lambda i: highs[i])
    il = min(range(s, n), key=lambda i: lows[i])
    if ih >= il:
        start = min(range(s, ih + 1), key=lambda i: lows[i])
        return ("up", lows[start], highs[ih], series.dates[start], series.dates[ih])
    start = max(range(s, il + 1), key=lambda i: highs[i])
    return ("down", lows[il], highs[start], series.dates[il], series.dates[start])


def _zone(spot: float, direction: str, retr: list[FibLevel],
          lo: float, hi: float) -> str:
    """现价落在哪两档回撤之间（用于'该等回调到哪/已进入哪个区'的定位）。"""
    # 组装含端点的有序档位（价格升序）：swing_low(1.0) … 各回撤 … swing_high(0)
    marks = [(lo, "起涨点" if direction == "up" else "摆动低(1.0)"),
             (hi, "摆动高(0)" if direction == "up" else "起跌点")]
    marks += [(lv.price, lv.label) for lv in retr]
    marks.sort(key=lambda t: t[0])
    if spot <= marks[0][0]:
        return f"现价已跌破 {marks[0][1]}（腿失效/需重取摆动）"
    if spot >= marks[-1][0]:
        return f"现价已站上 {marks[-1][1]}（腿已完成/突破）"
    for i in range(len(marks) - 1):
        if marks[i][0] <= spot < marks[i + 1][0]:
            return f"现价位于 {marks[i][1]}–{marks[i + 1][1]} 之间"
    return ""


def build_fibonacci(series: PriceSeries | None, *, ratio: float | None = None,
                    spot: float | None = None, lookback: int = LOOKBACK_BARS,
                    min_leg_pct: float = MIN_LEG_PCT) -> FibAnalysis:
    """从价格序列算斐波那契回撤+扩展。

    series：优先真实期货日线（含高低价）。ratio：真实期货价/ETF 价，用于补 ETF 行权价锚。
    spot：现价（商品口径），缺省用序列最后收盘。
    """
    empty = FibAnalysis(ok=False, direction="", swing_low=0.0, swing_high=0.0,
                        swing_low_date=None, swing_high_date=None, leg_pct=0.0,
                        spot=spot or 0.0, etf_spot=None, ratio=ratio, lookback=lookback)
    if series is None or len(series.closes) < 20:
        return _with_note(empty, "价格序列不足（<20 日），无法定位摆动腿")
    sw = _find_swing(series, lookback)
    if sw is None:
        return _with_note(empty, "未找到有效摆动腿")
    direction, lo, hi, lo_d, hi_d = sw
    leg = hi - lo
    leg_pct = 100.0 * leg / lo if lo else 0.0
    if leg <= 0 or leg_pct < min_leg_pct:
        return _with_note(empty, f"摆动腿幅度过小（{leg_pct:.1f}% < {min_leg_pct:.0f}%），无回撤交易价值")

    px = spot if spot is not None else series.closes[-1]
    etf_spot = (px / ratio) if ratio else None

    def _etf(v: float) -> float | None:
        return (v / ratio) if ratio else None

    retr: list[FibLevel] = []
    for r in RETR_RATIOS:
        # 回撤价：上涨腿从高点往下回撤；下跌腿从低点往上反抽
        price = hi - r * leg if direction == "up" else lo + r * leg
        retr.append(FibLevel(ratio=r, price=price, etf=_etf(price), kind="retr",
                             label=f"{r:.3f}".rstrip("0").rstrip("."),
                             is_key=r in (0.382, 0.5, 0.618)))

    ext: list[FibLevel] = []
    for e in EXT_RATIOS:
        # 扩展目标：上涨腿在高点之上延伸；下跌腿在低点之下延伸
        price = hi + (e - 1.0) * leg if direction == "up" else lo - (e - 1.0) * leg
        ext.append(FibLevel(ratio=e, price=price, etf=_etf(price), kind="ext",
                            label=f"扩展{e:.3f}".rstrip("0").rstrip("."), is_key=False))

    zone = _zone(px, direction, retr, lo, hi)
    # note 按【腿的实际方向】渲染起点→终点（上涨腿 低→高；下跌腿 高→低），避免"下跌腿 62→66"式自相矛盾
    if direction == "up":
        a, b, ad, bd = lo, hi, lo_d, hi_d
    else:
        a, b, ad, bd = hi, lo, hi_d, lo_d
    return FibAnalysis(
        ok=True, direction=direction, swing_low=lo, swing_high=hi,
        swing_low_date=lo_d, swing_high_date=hi_d, leg_pct=leg_pct,
        spot=px, etf_spot=etf_spot, ratio=ratio,
        retracements=retr, extensions=ext, current_zone=zone,
        lookback=lookback,
        note=(f"{'上涨' if direction == 'up' else '下跌'}腿 "
              f"{a:.1f}→{b:.1f}（{ad}→{bd}，幅度 {leg_pct:.1f}%）"))


def _with_note(a: FibAnalysis, note: str) -> FibAnalysis:
    from dataclasses import replace
    return replace(a, note=note)
