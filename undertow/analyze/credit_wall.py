"""墙位卖方价差 —— 把「墙难破」这件事直接做成仓位。

用户 2026-08-31 提出：「我们目前最可靠的期权结构/墙等数据，最适合的交易策略
应该是卖方价差。因为事实证明破墙很难。」回测证实了这个直觉，但需要三道闸门，
少一道就变成亏损策略。

═══ 回测结论（2026-08-31 codex review 后重做）：**该策略未通过验证** ═══

最初的回测给出「稳健档 82% 胜率 +2.84%/笔、激进档 63% +9.99%/笔」，
codex 指出四处方法论错误，逐条修掉后结果全部翻负：

  错误①（P1-10 策略定义漂移）一个信号下的多个到期各算一笔，而报告只取
        annual_roi 最高的那一个 —— 统计的策略和执行的策略不是同一个。
        改为【每信号只选一笔】，且回测与报告用同一条事前规则。
  错误②（P0-3）DTE 用 obs_day 算，整体错一天，0DTE 被当成 3DTE。
  错误③（P1-14）到期日无收盘价时向后取最近一天 —— 把到期后的价格变动
        带进内在价值，是明确的 look-ahead。改为缺失即丢弃。
  错误④（P1-6）品种-日当独立样本。金银同日相关 0.89、QQQ/TQQQ 0.99，
        改为同日跨品种合成一个等权组合收益，按【日期簇】统计。

修正后（scripts/backtest_credit_wall.py，逐笔账本在 data/backtest/）：

  规则       档位          笔数 簇数 胜率  簇均ROI   置换p   95%CI            总PnL
  max_roi    conservative  19  14  53%  -25.71%  0.949  [-53.9,  +0.2]   -712
  max_roi    balanced      19  14  63%  -31.70%  0.971  [-60.3,  -3.3]  -1840
  max_roi    aggressive    18  16  67%   -5.57%  0.713  [-26.4, +10.6]   -803
  first      conservative  19  14  58%  -15.52%  0.872  [-40.6,  +7.0]   -553
  first      aggressive    18  16  72%   -5.90%  0.724  [-26.7, +10.2]   -802
  max_credit conservative  17  12  59%  -12.80%  0.804  [-40.4,  +9.1]   +108
  max_credit aggressive    17  15  65%   -8.88%  0.762  [-32.6, +11.8]   -849

**九个组合（3 选择规则 × 3 档）全部负簇均 ROI，置换 p 全部 > 0.7。**
置换检验问的是「日期簇净收益是否 > 0」，而不是「胜率是否 > 50%」——
胜率高不等于赚钱，balanced 档 63~74% 的胜率配的是 -17~-32% 的簇均 ROI。

所以本模块【默认不出候选】。保留代码是因为：
  · 逻辑（三道闸门、墙位分层、虚值钳制）本身是对的，样本外可再验；
  · 「墙难破」这个观察仍成立（破墙率 5~35%），只是权利金覆盖不了破墙的损失；
  · 需要它作为反例，防止日后有人凭「82% 胜率」的记忆把它重新打开。

要用必须显式 force=True，且报告会打上未验证标记。

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
        "n": 19, "clusters": 14, "win_rate": 0.53, "break_rate": None,
        "per_trade_pct": -25.71, "annual_pct": None,
        "median_occupancy": 96, "worst_pct": -104, "win_roi_pct": 12.0,
        "perm_p": 0.949, "ci95": (-53.94, 0.17), "total_pnl": -712,
        "note": "⛔ 未通过验证：簇均 ROI -25.71%，置换 p=0.949，95%CI 上界仅 +0.17%。",
    },
    "balanced": {
        "label": "平衡", "offset": 0.0, "width": 0.025, "dte": (15, 45),
        "n": 19, "clusters": 14, "win_rate": 0.63, "break_rate": None,
        "per_trade_pct": -31.70, "annual_pct": None,
        "median_occupancy": 117, "worst_pct": -104, "win_roi_pct": 18.0,
        "perm_p": 0.971, "ci95": (-60.31, -3.28), "total_pnl": -1840,
        "note": "⛔ 未通过验证：簇均 ROI -31.70%，95%CI 整体为负（上界 -3.28%）。",
    },
    "aggressive": {
        "label": "激进", "offset": -0.02, "width": 0.025, "dte": (4, 14),
        "n": 18, "clusters": 16, "win_rate": 0.67, "break_rate": None,
        "per_trade_pct": -5.57, "annual_pct": None,
        "median_occupancy": 118, "worst_pct": -103, "win_roi_pct": 20.0,
        "perm_p": 0.713, "ci95": (-26.41, 10.62), "total_pnl": -803,
        "note": "⛔ 未通过验证：簇均 ROI -5.57%，置换 p=0.713。三档里最接近零的一个。",
    },
}
# ⛔ 三档全部未通过验证 —— 默认不出候选，除非显式 force=True。
STRATEGY_VALIDATED = False
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


def propose(snap, oi_session: date, spot: float, direction: str, ratio: float,
            tier: str = DEFAULT_TIER, *, execution_date: date | None = None,
            force: bool = False) -> Verdict:
    """给出墙位卖方价差候选。三道闸门任一不过就不出候选，并说明卡在哪。

    direction: '看涨'/'看跌' —— 卖【逆向】侧：看涨卖 put 价差、看跌卖 call 价差。

    ⚠️ 两个日期语义不同，混用会整体错一天（codex 2026-08-31 P0-3 实测）：
      oi_session      = OI 所属交易日（= 快照日前一工作日）。用来【解释结构】：
                        墙在哪、多厚 —— 那是 D−1 收盘的 OCC 结算。
      execution_date  = 实际下单日（= 快照日 D）。用来【筛可交易合约、算 DTE】。
    实测 2026-08-31（周一，obs=周五 8/28）：当天到期的合约按 obs 算成 3 天，
    于是「4~14 天」的激进档窗口实际选进了 1~11 天的合约，含 0DTE。
    """
    exec_day = execution_date or oi_session
    if not STRATEGY_VALIDATED and not force:
        t0 = RISK_TIERS[tier]
        return Verdict(False,
                       f"⛔ 该策略未通过验证，默认不出候选。{t0['label']}档修正方法论后："
                       f"{t0['n']} 笔 / {t0['clusters']} 个日期簇，簇均 ROI "
                       f"{t0['per_trade_pct']:+.2f}%，置换 p={t0['perm_p']:.3f}，"
                       f"95%CI [{t0['ci95'][0]:+.1f}%, {t0['ci95'][1]:+.1f}%]。"
                       f"九个组合（3 选择规则 × 3 档）全部负收益 —— "
                       f"最初那批「82% 胜率 +2.84%」来自四处方法论错误的叠加"
                       f"（策略定义漂移 / DTE 错一天 / 向后取价 / 未按日期簇）。"
                       f"要研究用途可传 force=True。",
                       [], {"tier": tier, "validated": False})
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
    wk, woi, wshare = _walls_aggregate(snap, oi_session, kind, spot)
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
        d = (c.expiry - exec_day).days     # ← 用执行日算 DTE，不是 OI 所属日
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
            kind=kind, expiry=exp, dte=(exp - exec_day).days,
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
