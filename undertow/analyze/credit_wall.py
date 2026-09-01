"""墙位卖方价差 —— 把「墙难破」这件事直接做成仓位。

用户 2026-08-31 提出：「我们目前最可靠的期权结构/墙等数据，最适合的交易策略
应该是卖方价差。因为事实证明破墙很难。」回测证实了这个直觉，但需要三道闸门，
少一道就变成亏损策略。

═══ 回测（2026-08-31，样本区间 2026-06-25~08-31，7 个品种）═══

【为什么必须用「近端加总墙」而不是「同到期专属墙」】
用户当时追问「墙的日期也要考虑」——我第一版正是用近端(≤14天)加总的墙去卖
22~45 天的价差，口径错配。但改成同到期专属墙后结果反而崩了：

  近端加总墙 · 厚度≥10%   45笔  84%胜率  +3.62%/笔  年化 +50%
  近端加总墙 · 厚度≥15%   14笔  86%胜率  +9.73%/笔  年化 +133%
  近端加总墙 · 厚度≥20%    9笔 100%胜率 +11.56%/笔  年化 +147%
  同到期专属墙 · ≥25%     55笔  76%胜率  -5.26%/笔  年化 -72%
  同到期专属墙 · ≥45%     22笔  68%胜率 -16.55%/笔  年化 -208%

同到期口径提高门槛不但没改善、反而更差，所以这不是幸存者偏差：
**跨到期都堆在同一位置＝真关键位；单个到期的最大 OI＝噪音。**
正确用法是把同到期当【第二重确认】——加总墙的位置上，该到期自己也要有墙：

  加总≥10% + 同到期≥20% · 15~45天   13笔 92%胜率 +8.93%/笔 年化+98% p=0.003
  加总≥10% + 同到期≥20% ·  4~14天   23笔 78%胜率 +1.15%/笔 年化+47% p=0.011

【为什么必须是极强信号】亏损全集中在中等信号：
  极强(≥5×)  亏损笔最惨 -24
  中等(2~5×) 亏损笔 -1250 / -499 / -260 —— 大亏 50 倍
【为什么必须看墙的厚度】亏损笔的墙平均 29,969、盈利笔 65,789（差 2.2 倍）；
  墙 OI <30,000 时破墙率 55%，≥60,000 时降到 21%。
  绝对 OI 不可跨品种比较（QQQ 的 4 万 ≠ SLV 的 4 万），故一律用相对占比。

【到期时间】用户猜「越快到期越不容易破墙」——实测不成立，破墙率与 DTE 无关
（20~29% 横跨 1~45 天）。但胜率随 DTE 单调上升（56%→86%），因为收到的
权利金更厚、缓冲更大。15~45 天单笔收益率最高。

【铁鹰不成立】双边同时卖，破墙率飙到 57~80%（任一边破就亏）：
  全信号 4~14天 50%胜率 -3.06%/笔　全信号 15~45天 64%胜率 -4.70%/笔
  极强 15~45天 71%胜率 -3.14%/笔 —— 全负。已否决，勿再试。

⚠️ 所有阈值都是在同一批数据上选出来的，多重比较风险实打实；
   最优组合仅 13 笔。这套参数需要样本外验证才算数。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MIN_RATIO = 5.0          # 只在极强信号做（中等信号的亏损是极强的 50 倍）
MIN_WALL_SHARE = 0.10    # 加总墙厚度：占该侧近端总 OI 的比例
MIN_EXP_SHARE = 0.20     # 第二重确认：该到期自己的墙也要够厚
WALL_MATCH_TOL = 0.02    # 同到期墙必须落在加总墙 ±2% 内才算「同一位置」
FEE_PER_LEG = 0.80

# ═══ 三个风险档 ═══════════════════════════════════════════════════════════
# 2026-08-31 完整网格（136 份快照、45 个极强信号、逐日真实盘口重估）。
# 卖腿位置用【相对墙】的偏移：负=墙内（更靠近现价、权利金厚、破墙率高）。
#
# 用户当初那笔 SLV 61P/60P（8/21 开、8/26 到期）正是 aggressive 档：
# 墙在 60、卖 61 = 墙内 1.7%、6 天到期 —— 落在数据上年化最高的区域。
#
# ⚠️ 提前平仓实测【更差】：破墙率只有 5~35%，而平仓要双向吃点差，
#    不如让它到期作废。赚50%平 vs 持有到期：卖墙上 +5% vs +40% 年化。
#    这与"卖方应该收50%就跑"的通行说法相反，但数据如此。
RISK_TIERS = {
    "conservative": {
        "label": "稳健", "offset": 0.02, "width": 0.020, "dte": (15, 45),
        "n": 38, "win_rate": 0.82, "break_rate": 0.11, "per_trade_pct": 2.84,
        "annual_pct": 37, "median_occupancy": 96, "worst_pct": -103,
        "note": "破墙率 11% 最低。账户小的时候先活下来。",
    },
    "balanced": {
        "label": "平衡", "offset": 0.0, "width": 0.025, "dte": (15, 45),
        "n": 45, "win_rate": 0.76, "break_rate": 0.20, "per_trade_pct": -0.43,
        "annual_pct": -6, "median_occupancy": 117, "worst_pct": -103,
        "note": "卖在墙上。窄宽度下单笔为负——权利金没覆盖住破墙损失。",
    },
    "aggressive": {
        "label": "激进", "offset": -0.02, "width": 0.025, "dte": (4, 14),
        "n": 60, "win_rate": 0.63, "break_rate": 0.35, "per_trade_pct": 9.99,
        "annual_pct": 413, "median_occupancy": 118, "worst_pct": -105,
        "note": "年化最高，但胜率仅 63%、破墙 35%。连亏 3 次概率 5.1%，"
                "对小账户是爆仓级风险。",
    },
}
DEFAULT_TIER = "conservative"

# 兼容旧调用
SELL_OFFSET = RISK_TIERS[DEFAULT_TIER]["offset"]
WIDTH_FRAC = RISK_TIERS[DEFAULT_TIER]["width"]
DTE_MIN, DTE_MAX = RISK_TIERS[DEFAULT_TIER]["dte"]

BACKTEST = {
    "n": 38, "win_rate": 0.82, "per_trade_pct": 2.84, "annual_pct": 37,
    "p_value": 0.000, "median_occupancy": 96,
    "caveat": "38 笔，阈值在同一批数据上选出，需样本外验证；"
              "年化是「单笔 × 365/持有天数」的外推，未扣信号空窗",
}

# 卖腿位置 → 权利金比例与破墙率的完整对照（宽5%、15~45天、持有到期）
# 这张表是「为什么不能一味卖近」的凭证：权利金和风险是同一枚硬币。
OFFSET_TRADEOFF = [
    # (偏移, credit/width, 破墙率, 胜率, 单笔%, 年化%)
    (-0.04, 0.28, 0.30, 0.77, 3.20, 44),
    (-0.02, 0.21, 0.21, 0.81, 0.61, 8),
    (0.00, 0.16, 0.20, 0.89, 2.87, 40),
    (0.02, 0.12, 0.09, 0.84, 3.62, 50),
    (0.04, 0.09, 0.05, 0.86, 3.68, 50),
]


@dataclass(frozen=True)
class WallSpread:
    kind: str                # 'C' 卖看涨价差 / 'P' 卖看跌价差
    expiry: date
    dte: int
    sell_strike: float
    buy_strike: float
    credit: float            # 每张净收权利金($)，卖腿吃 bid、买腿吃 ask
    width: float
    occupancy: float         # 保证金占用 = (宽度 - 权利金)×100
    wall_strike: float
    wall_share: float        # 加总墙厚度占比
    exp_share: float         # 该到期自己的墙厚度占比
    buffer_pct: float        # 卖腿距现价的缓冲

    @property
    def max_loss(self) -> float:
        return self.occupancy + FEE_PER_LEG * 4

    @property
    def roi(self) -> float:
        return self.credit / self.occupancy if self.occupancy > 0 else 0.0

    @property
    def annual_roi(self) -> float:
        return self.roi * 365 / max(self.dte, 1)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    spreads: list[WallSpread]
    gates: dict


def _walls_aggregate(snap, obs: date, kind: str, spot: float, max_dte: int = 14):
    """近端加总墙 + 其占该侧总 OI 的比例。"""
    agg: dict[float, int] = {}
    total = 0
    for c in snap.contracts:
        if c.kind != kind:
            continue
        d = (c.expiry - obs).days
        if not (1 <= d <= max_dte):
            continue
        if kind == "C" and c.strike < spot:
            continue
        if kind == "P" and c.strike > spot:
            continue
        if not (spot * 0.85 <= c.strike <= spot * 1.15):
            continue
        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
        total += c.open_interest
    if not agg or total <= 0:
        return None, 0, 0.0
    k, v = max(agg.items(), key=lambda x: x[1])
    return k, v, v / total


def _wall_same_expiry(snap, exp: date, kind: str, spot: float):
    agg: dict[float, int] = {}
    total = 0
    for c in snap.contracts:
        if c.expiry != exp or c.kind != kind:
            continue
        if kind == "C" and c.strike < spot:
            continue
        if kind == "P" and c.strike > spot:
            continue
        if not (spot * 0.85 <= c.strike <= spot * 1.15):
            continue
        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
        total += c.open_interest
    if not agg or total <= 0:
        return None, 0, 0.0
    k, v = max(agg.items(), key=lambda x: x[1])
    return k, v, v / total


def tier_params(tier: str = DEFAULT_TIER) -> dict:
    """取风险档参数。未知档位直接抛，不静默回落 —— 档位决定的是爆仓风险。"""
    if tier not in RISK_TIERS:
        raise ValueError(f"未知风险档 {tier!r}，可选：{list(RISK_TIERS)}")
    return RISK_TIERS[tier]


def propose(snap, obs: date, spot: float, direction: str, ratio: float,
            tier: str = DEFAULT_TIER) -> Verdict:
    """给出墙位卖方价差候选。三道闸门任一不过就不出候选，并说明卡在哪。

    direction: '看涨'/'看跌' —— 卖【逆向】侧：看涨卖 put 价差、看跌卖 call 价差。
    """
    gates = {"ratio": ratio, "min_ratio": MIN_RATIO}
    if ratio < MIN_RATIO:
        return Verdict(False, (f"压力倍数 {ratio:.1f}× 未达 {MIN_RATIO:g}× —— "
                               f"回测里中等信号(2~5×)的亏损笔达 -1250/-499/-260，"
                               f"是极强信号(最惨 -24)的 50 倍。这道闸门不能松。"),
                       [], gates)
    if direction not in ("看涨", "看跌"):
        return Verdict(False, f"方向不明（{direction}），无从决定卖哪一侧", [], gates)

    kind = "P" if direction == "看涨" else "C"
    gates["side"] = kind
    wk, woi, wshare = _walls_aggregate(snap, obs, kind, spot)
    gates.update({"wall_strike": wk, "wall_oi": woi, "wall_share": wshare,
                  "min_wall_share": MIN_WALL_SHARE})
    if wk is None or wshare < MIN_WALL_SHARE:
        return Verdict(False, (f"{kind} 侧近端加总墙厚度 "
                               f"{wshare:.0%} < {MIN_WALL_SHARE:.0%} —— "
                               f"回测里薄墙(OI<30,000)的破墙率 55%，厚墙(≥60,000)只有 21%；"
                               f"亏损笔的墙平均只有盈利笔的一半。"),
                       [], gates)

    tp = tier_params(tier)
    dte_lo, dte_hi = tp["dte"]
    gates["tier"] = tier
    gates["tier_label"] = tp["label"]
    legs_by_exp: dict[date, list] = {}
    for c in snap.contracts:
        if c.kind != kind or not (c.bid and c.ask and c.bid > 0):
            continue
        d = (c.expiry - obs).days
        if dte_lo <= d <= dte_hi:
            legs_by_exp.setdefault(c.expiry, []).append(c)

    out: list[WallSpread] = []
    skipped: list[str] = []
    for exp in sorted(legs_by_exp):
        ewk, ewoi, eshare = _wall_same_expiry(snap, exp, kind, spot)
        # 第二重确认：该到期自己的墙必须落在加总墙同一位置且够厚
        if ewk is None or abs(ewk - wk) / wk > WALL_MATCH_TOL:
            skipped.append(f"{exp} 该到期的墙在 "
                           f"{ewk:g}（与加总墙 {wk:g} 不同位）" if ewk else f"{exp} 无墙")
            continue
        if eshare < MIN_EXP_SHARE:
            skipped.append(f"{exp} 同到期墙厚度 {eshare:.0%} < {MIN_EXP_SHARE:.0%}")
            continue
        ls = sorted(legs_by_exp[exp], key=lambda c: c.strike)
        off = tp["offset"]
        tgt = wk * (1 + off) if kind == "C" else wk * (1 - off)
        # ⚠️ 墙内偏移不得越过现价：墙本身可能已经很贴近现价，再往内推就成了
        # 卖【实值】腿 —— 那不是收权利金，是直接接货。2026-08-31 实测：
        # GLD 现价 407.23、put 墙 405，墙内 2% 推到 413.1，越过现价 6 美元。
        if kind == "C":
            tgt = max(tgt, spot * 1.001)
            pool = [c for c in ls if c.strike > spot]
        else:
            tgt = min(tgt, spot * 0.999)
            pool = [c for c in ls if c.strike < spot]
        if not pool:
            skipped.append(f"{exp} 无虚值腿可卖")
            continue
        sell = min(pool, key=lambda c: abs(c.strike - tgt))
        wf = tp["width"]
        wt = sell.strike * (1 + wf) if kind == "C" else sell.strike * (1 - wf)
        cands = [c for c in ls if (c.strike > sell.strike if kind == "C"
                                   else c.strike < sell.strike)]
        # 缓冲太薄的直接弃 —— 卖腿贴着现价时权利金再厚也扛不住一天的波动
        if abs(sell.strike / spot - 1) < 0.005:
            skipped.append(f"{exp} 卖腿 {sell.strike:g} 距现价不足 0.5%")
            continue
        if not cands:
            continue
        buy = min(cands, key=lambda c: abs(c.strike - wt))
        credit = (sell.bid - buy.ask) * 100
        width = abs(buy.strike - sell.strike) * 100
        if credit <= 0 or width <= 0:
            continue
        out.append(WallSpread(
            kind=kind, expiry=exp, dte=(exp - obs).days,
            sell_strike=sell.strike, buy_strike=buy.strike,
            credit=credit, width=width, occupancy=width - credit,
            wall_strike=wk, wall_share=wshare, exp_share=eshare,
            buffer_pct=abs(sell.strike / spot - 1) * 100))
    gates["skipped"] = skipped[:5]
    if not out:
        return Verdict(False, (f"加总墙在 {wk:g}（厚度 {wshare:.0%}，已达标），"
                               f"但 {dte_lo}~{dte_hi} 天内没有到期能通过第二重确认"
                               f"（该到期自己的墙需落在同一位置且占比 ≥{MIN_EXP_SHARE:.0%}）。"
                               f"　当前档位 {tp['label']}：{dte_lo}~{dte_hi} 天、"
                               f"卖腿{'墙内' if tp['offset'] < 0 else '墙外'}"
                               f"{abs(tp['offset']) * 100:.0f}%、宽{tp['width'] * 100:.1f}%。"
                               + ("　跳过原因：" + "；".join(skipped[:3]) if skipped else "")),
                       [], gates)
    out.sort(key=lambda s: -s.annual_roi)
    return Verdict(True, (f"压力比 {ratio:.1f}× 过闸，{kind} 侧加总墙 {wk:g} "
                          f"厚度 {wshare:.0%}，{len(out)} 个到期通过同到期确认。"),
                   out, gates)
