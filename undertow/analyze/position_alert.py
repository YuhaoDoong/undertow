"""持仓 × 信号冲突检测 —— 纯确定性，无 I/O（持仓与信号由调用方喂入）。

**为什么需要它**（用户 2026-08-28 的直接批评）：
当天研报里黄金亮了 ⚡极强看跌（加权增仓 53.5 倍），用户手上持着白银多头。
金银是同向品种（日收益相关约 0.89），黄金极强看跌本该让人回头质疑白银的
「偏多」结论、并对白银持仓预警 —— 但三件事一件都没发生：
信号没被提到对话里、相反结论没被质疑、持仓没有告警。次日 SLV -4.38%。

所以这个模块只干两件事，都是当时缺的：
1. 手上有仓的品种出现【反向强信号】→ 告警；
2. 与持仓品种高相关的另一品种出现反向强信号 → 交叉告警
   （黄金的信号必须能惊动白银的持仓）。

⚠️ 输出含账户持仓，调用方一律落 data/account/（已 gitignore），绝不入库、
   绝不写进任何 HTML 研报。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 期权代码：SLV260918C73000 → 标的 SLV / 到期 260918 / C / 行权 73000
# ⚠️ 必须容忍交易所后缀：长桥实际返回的是 "SLV260918C70000.US"。
# 早先的正则以 $ 收尾，真实持仓一条都解析不出来 → 被当成"没持仓" → 静默无告警。
# 这和这个模块要防的失职是同一类根因，所以下面 unparsed() 让解析失败必须出声。
_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d+)(?:\.[A-Z]{1,4})?$")

# 高相关同族：一方出现强信号，必须惊动另一方的持仓。
# 数值是日收益相关的量级，只用于【是否交叉告警】的开关，不参与任何计算。
CORRELATED: dict[str, list[tuple[str, float]]] = {
    "GLD": [("SLV", 0.89)],
    "SLV": [("GLD", 0.89)],
    "QQQ": [("TQQQ", 0.99), ("SPY", 0.95)],
    "TQQQ": [("QQQ", 0.99), ("SPY", 0.95)],
    "SPY": [("QQQ", 0.95), ("IWM", 0.85)],
    "IWM": [("SPY", 0.85)],
}
CROSS_MIN_CORR = 0.80


def parse_symbol(symbol: str) -> tuple[str, str] | None:
    """拆出 (标的, C/P)。非期权代码返回 None。"""
    m = _OCC.match(symbol.strip().upper())
    return (m.group(1), m.group(3)) if m else None


def leg_bias(symbol: str, qty: int) -> int:
    """单腿的方向敞口符号：+1 看涨、-1 看跌、0 无法判定。

    买 call / 卖 put = 看涨；卖 call / 买 put = 看跌。
    """
    p = parse_symbol(symbol)
    if p is None or not qty:
        return 0
    kind = p[1]
    long_leg = qty > 0
    if kind == "C":
        return 1 if long_leg else -1
    return -1 if long_leg else 1


def unparsed(held: dict[str, int]) -> list[str]:
    """解析不出来的持仓代码。

    调用方**必须**检查并报出来：解析失败会让持仓被当成不存在，
    于是告警静默消失 —— 而告警消失正是这个模块存在的原因。
    """
    return [s for s in held if parse_symbol(s) is None]


def position_bias(held: dict[str, int]) -> dict[str, int]:
    """按标的聚合出持仓方向：{标的: +1 看涨 / -1 看跌 / 0 无法判定}。

    两步：
    1. 张数加权的单腿方向。裸腿、不等量组合到这里就定了。
    2. **完全抵消时用行权价定夺** —— 价差组合两腿张数相同、单腿方向相反，
       第 1 步必然得 0。此时方向由「买的那条腿在哪一侧」唯一决定，
       这是价差的定义不是推测：
         · call 价差：买低行权 / 卖高行权 = 看涨（牛市看涨价差）
         · put  价差：买高行权 / 卖低行权 = 看跌（熊市看跌价差）
       早先版本这里直接返回 0、要求调用方拿 soul/plan 兜底 —— 可 plan 未必
       记全，漏一个就等于没告警，而告警缺失正是这个模块存在的原因。
    """
    agg: dict[str, int] = {}
    # 标的 → [(到期, C/P, 行权, 数量)]。**到期必须带上** —— 丢了它就分不出
    # 日历价差与垂直价差（codex 2026-08-29 P0）。
    legs: dict[str, list[tuple[str, str, float, int]]] = {}
    for sym, qty in held.items():
        p = parse_symbol(sym)
        if p is None:
            continue
        und, kind = p
        agg[und] = agg.get(und, 0) + leg_bias(sym, qty) * abs(int(qty))
        m = _OCC.match(sym.strip().upper())
        legs.setdefault(und, []).append(
            (m.group(2), kind, float(m.group(4)), int(qty)))

    out: dict[str, int] = {}
    for und, v in agg.items():
        if v:
            out[und] = 1 if v > 0 else -1
            continue
        out[und] = _spread_bias(legs.get(und, []))
    return out


def _spread_bias(legs: list[tuple[str, str, float, int]]) -> int:
    """张数抵消时按行权价定方向 —— **只认最简单那一种结构**。

    ⚠️ 只处理「同到期、同 C/P、恰好两腿、张数绝对值相等、一买一卖」的
    垂直价差。其余一律返回 0（未知），由调用方报出来。

    codex 2026-08-29 P0：上一版丢掉到期日、只要单腿方向抵消就按
    「最小买入行权价 vs 最大卖出行权价」强行定性，会误判：
      · 同行权价的日历价差 → 被任意判成看涨或看跌
      · 铁鹰 → 只看 call 部分，通常误判成看跌
      · 蝶式 → 误判成单向价差
      · 跨式/宽跨式/比例价差 → 张数聚合本就不成立
      · 同标的多组独立价差 → 跨组配腿
    方向判错会漏掉真实风险告警，或制造假告警 —— 两者都比"说不知道"更糟。
    """
    # 按 (到期, C/P) 分组，只有恰好一组、恰好两腿时才敢下结论
    groups: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for exp, kind, strike, q in legs:
        groups.setdefault((exp, kind), []).append((strike, q))
    if len(groups) != 1:
        return 0                      # 跨到期或跨 C/P（铁鹰、日历、跨式…）
    (exp, kind), rows = next(iter(groups.items()))
    if len(rows) != 2:
        return 0                      # 三腿以上（蝶式、比例…）
    (s1, q1), (s2, q2) = rows
    if q1 * q2 >= 0 or abs(q1) != abs(q2):
        return 0                      # 不是一买一卖、或张数不等
    buy_strike = s1 if q1 > 0 else s2
    sell_strike = s2 if q1 > 0 else s1
    if buy_strike == sell_strike:
        return 0                      # 同行权价（日历价差已被上面拦掉，双保险）
    if kind == "C":
        # 买低卖高 = 牛市看涨价差；买高卖低 = 熊市看涨价差
        return 1 if buy_strike < sell_strike else -1
    # put：买高卖低 = 熊市看跌价差；买低卖高 = 牛市看跌价差（收权利金，看涨）
    return -1 if buy_strike > sell_strike else 1


@dataclass(frozen=True)
class Conflict:
    underlying: str          # 持仓标的
    holding: str             # 持仓方向（看涨/看跌）
    source: str              # 信号来自哪个标的（交叉告警时不同于 underlying）
    signal: str              # 信号方向（看涨/看跌）
    level: str               # 强/极强
    ratio: float             # 压力比
    corr: float | None       # 交叉告警时的相关性；同品种为 None
    reasons: list[str] = field(default_factory=list)

    @property
    def cross(self) -> bool:
        return self.source != self.underlying

    def headline(self) -> str:
        who = (f"{self.source}（与你持仓的 {self.underlying} 相关约 {self.corr:.2f}·粗估）"
               if self.cross else self.underlying)
        return (f"⚠️ 你持有 {self.underlying} {self.holding}，"
                f"而 {who} 出现 ⚡{self.level}{self.signal}信号（{self.ratio:.1f}×）")


def check_conflicts(held: dict[str, int], signals: dict[str, object],
                    *, plan_bias: dict[str, int] | None = None) -> list[Conflict]:
    """持仓方向 × 强信号方向 → 冲突清单（同品种优先，其次高相关交叉）。

    held    : {期权代码: 数量}，卖出为负。
    signals : {标的: StrongSignal|None}。
    plan_bias: 张数完全抵消时的方向兜底（来自 soul/plan 的结构描述）。
    """
    bias = position_bias(held)
    if plan_bias:
        for k, v in plan_bias.items():
            if bias.get(k, 0) == 0 and v:
                bias[k] = v
    out: list[Conflict] = []
    for und, b in bias.items():
        if not b:
            continue
        hold_txt = "看涨" if b > 0 else "看跌"
        # ① 同品种
        ss = signals.get(und)
        seen_src = set()
        if ss is not None and getattr(ss, "direction", "") != hold_txt:
            out.append(Conflict(und, hold_txt, und, ss.direction,
                                getattr(ss, "level", ""),
                                float(getattr(ss, "pressure_ratio", 0) or 0), None,
                                list(getattr(ss, "reasons", []) or [])))
            seen_src.add(und)
        # ② 高相关交叉 —— 黄金的信号必须能惊动白银的持仓
        for other, corr in CORRELATED.get(und, []):
            if corr < CROSS_MIN_CORR or other in seen_src:
                continue
            so = signals.get(other)
            if so is None or getattr(so, "direction", "") == hold_txt:
                continue
            out.append(Conflict(und, hold_txt, other, so.direction,
                                getattr(so, "level", ""),
                                float(getattr(so, "pressure_ratio", 0) or 0), corr,
                                list(getattr(so, "reasons", []) or [])))
    # 同品种排在交叉前；同类里压力比大的在前
    out.sort(key=lambda c: (c.cross, -c.ratio))
    return out


def render(conflicts: list[Conflict]) -> str:
    """告警文本（落 data/account/，不入库）。"""
    if not conflicts:
        return ""
    L = ["## ⚠️ 持仓与信号方向冲突", ""]
    for c in conflicts:
        L.append(f"- {c.headline()}")
        for r in c.reasons[:2]:
            L.append(f"    · {r}")
    L.append("")
    L.append("> 强信号阈值未经校准，这不是平仓指令；但**方向相反时你至少该知道**。")
    return "\n".join(L)
