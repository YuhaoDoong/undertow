"""墙位卖方价差 v2 —— 白银/黄金已激活，参数逐品种实测

取代 credit_wall.py（那版用近端分层墙 + 墙外偏移 + 盘中触及口径，全部是错的）。
2026-08-31 用户逐条纠正后重建，六条口径见 memory/wall-credit-spread-strategy.md：
  ① 墙 = 跨到期累计 OI 最大行权价（cap 参数控制到期上限）
  ② 破墙 = 到期日收盘越过卖腿，不是盘中触及
  ③ 价差按组合单中价成交（让 25% 点差），不是两边吃满
  ④ 锁定墙位，墙确认切换才换（conf 天）；换墙当天平掉旧仓
  ⑤ 价差宽度按【现价百分比】，且每品种单独定
  ⑥ 反向极强信号平仓要加距离条件（价格距卖腿 ≤5% 才响应）

⚠️ 样本期 GLD +10.5%、SLV +14.8%，单边上涨。put 侧零破墙有相当部分来自方向，
   未经跌市验证。call 侧在两个品种上都不赚钱，默认不出候选。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

FEE_PER_LEG = 0.80
SIG_EXIT_RATIO = 30.0      # 反向极强信号平仓阈值
SIG_EXIT_DIST = 0.05       # 且价格须距卖腿 ≤5%（离得远就不用平）
MIN_CREDIT_MULT = 3.0      # 权利金至少要是手续费(4腿)的几倍，否则这笔没有交易意义

# band = 找墙时往现价看多远。2026-08-31 实测：硬编码 15% 会让 GLD 选中距现价
# -14% 的 350（累计 OI 126k，比 400 的 112k 还大），但那里一周到期的 put 报价
# 是 0.00 —— 卖它收不到钱还要付 $3.2 手续费。远处的大 OI 是长期持仓堆积
# （LEAPS/年度对冲），不是近期支撑位。band=5% 后选中 400，权利金 $171。
# 这一改把 GLD 的年化从 +92% 抬到 +564%。

# 逐品种参数（2026-08-31 扫参数定下）。未列出的品种不出候选。
PARAMS: dict[str, dict] = {
    "silver": {
        "cap": 9999, "confirm": 1, "band": 0.05, "width_pct": 0.03, "dte": (4, 11),
        "sides": ("P",),           # call 侧实测 ≈ 打平，暂不出
        "n": 37, "unbroken": 1.00, "roi": 0.069, "annual": 2.62,
        "occ": 122, "credit": 37.7, "fee_share": 0.08,
        "note": "band=8%/宽度2% 的年化更高(+503%)但只有 22 笔；"
                "按用户要求取样本更多的 band=5%。宽度 3% 是拐点："
                "1% 时权利金被手续费吃掉 48%",
    },
    # ⚠️ 未激活。band 维度的结论存疑：ROI 随 band 变大而升高（5%→15% 是
    # 6.9%→15.4%），但权利金是【下降】的（$37.7→$24.2）——升高来自权利金门槛
    # 筛掉了低权利金的笔。配对比较（同 21 天）显示 band 大确实更好，
    # 但那是「墙离得远、缓冲厚」的效果，与样本期单边上涨无法分离。
    "gold": {
        "cap": 9999, "confirm": 3, "band": 0.05, "width_pct": 0.01, "dte": (4, 11),
        "sides": ("P",),           # call 侧 -$487，明确不出
        "n": 25, "unbroken": 1.00, "roi": 0.192, "annual": 5.64,
        "occ": 364, "credit": 62.0, "fee_share": 0.05,
        "note": "band 从 15% 改 5% 后年化 +92%→+564%：原来选中距现价 -14% 的 350，"
                "那里一周到期报价 0.00。conf=3 优于 1，与白银相反；"
                "极强信号全都离墙远，信号平仓在黄金上触发 0 次",
    },
}
# QQQ 参数已测出（cap=9999/conf=1/宽度1%/年化+31%）但样本仅 19 个快照且占用 $697，
# 暂不激活。其余品种快照不足 5 份，无法回测。
# 2026-08-31 用户定：先只激活白银。黄金的 band=5%/+564% 同样有筛选嫌疑
# （被权利金门槛筛掉的笔全是低权利金的，剩下的自然好看），需单独查证后再开。
ACTIVE = {"silver"}


@dataclass(frozen=True)
class Candidate:
    kind: str
    expiry: date
    dte: int
    sell: float
    buy: float
    credit: float          # 每张净收权利金($)，按中价让 25% 点差
    width: float           # 实际宽度($)
    occupancy: float
    wall: float
    spot: float

    @property
    def roi(self) -> float:
        return self.credit / self.occupancy if self.occupancy else 0.0

    @property
    def buffer_pct(self) -> float:
        return abs(self.sell / self.spot - 1) * 100

    @property
    def max_loss(self) -> float:
        return self.occupancy + FEE_PER_LEG * 4

    @property
    def fee_share(self) -> float:
        return FEE_PER_LEG * 4 / self.credit if self.credit else float("inf")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    wall: float | None = None
    candidates: list[Candidate] = field(default_factory=list)
    params: dict = field(default_factory=dict)


def accum_wall(snap, spot: float, kind: str, obs: date, cap: int,
               band: float = 0.15) -> tuple[float | None, int, float]:
    """跨到期累计 OI 最大的行权价（≤cap 天到期）。

    cap 的选择实测：≤14 天抖动 16~19%（8/21 SLV 选出 55 而非用户看到的 60）；
    全到期在 GLD 上会被深虚污染（8/11 470C/460C 各增 4 万张把墙顶到 460，
    而现价 402）——但 SLV 上全到期反而最稳。所以 cap 逐品种定。
    """
    agg: dict[float, int] = defaultdict(int)
    total = 0
    for c in snap.contracts:
        if c.kind != kind:
            continue
        if kind == "C" and not (spot <= c.strike <= spot * (1 + band)):
            continue
        if kind == "P" and not (spot * (1 - band) <= c.strike <= spot):
            continue
        if not (1 <= (c.expiry - obs).days <= cap):
            continue
        agg[c.strike] += c.open_interest
        total += c.open_interest
    if not agg or total <= 0:
        return None, 0, 0.0
    k = max(agg, key=agg.get)
    return k, agg[k], agg[k] / total


def _fill(sell, buy, give: float = 0.25) -> float:
    """组合单成交价：中价往不利方向让 give。

    「卖腿吃 bid、买腿吃 ask」对窄价差是毁灭性的假设 —— 它把 SLV 55/54
    一周价差的 $7 权利金算成 $0，进而得出「手续费比权利金大」的荒谬结论。
    """
    sb, sa = sell.bid or 0, sell.ask or 0
    bb, ba = buy.bid or 0, buy.ask or 0
    mid = ((sb + sa) / 2 - (bb + ba) / 2) * 100
    worst = (sb - ba) * 100
    return mid + (worst - mid) * give


def propose(snap, instrument: str, obs: date, execution_date: date,
            spot: float | None = None) -> Verdict:
    """给出该品种的墙位卖方价差候选。未激活的品种直接说明原因。"""
    if instrument not in ACTIVE:
        return Verdict(False, f"{instrument} 未激活：仅白银/黄金有足够快照完成参数实测"
                              f"（QQQ 19 份、其余各 4 份，无法回测）。")
    p = PARAMS[instrument]
    spot = spot if spot is not None else snap.spot
    out: list[Candidate] = []
    wall_used = None
    for kind in p["sides"]:
        wk, woi, wsh = accum_wall(snap, spot, kind, obs, p["cap"],
                                  band=p.get("band", 0.05))
        if wk is None:
            continue
        wall_used = wk
        legs: dict[date, dict[float, object]] = defaultdict(dict)
        for c in snap.contracts:
            if c.kind != kind or c.bid is None or not c.ask:
                continue
            d = (c.expiry - execution_date).days
            if p["dte"][0] <= d <= p["dte"][1]:
                legs[c.expiry][c.strike] = c
        for exp in sorted(legs):
            ks = sorted(legs[exp])
            pool = [x for x in ks if (x > spot if kind == "C" else x < spot)]
            if not pool:
                continue
            sk = min(pool, key=lambda x: abs(x - wk))
            w_abs = spot * p["width_pct"]
            far = [x for x in ks if (x > sk if kind == "C" else x < sk)]
            if not far:
                continue
            bk = min(far, key=lambda x: abs(x - (sk + w_abs if kind == "C" else sk - w_abs)))
            credit = _fill(legs[exp][sk], legs[exp][bk])
            width = abs(bk - sk) * 100
            # 权利金连手续费 3 倍都不到 → 这笔没有交易意义（GLD 350P 收 $0 的教训）
            if credit <= FEE_PER_LEG * 4 * MIN_CREDIT_MULT or width <= credit:
                continue
            out.append(Candidate(kind=kind, expiry=exp,
                                 dte=(exp - execution_date).days,
                                 sell=sk, buy=bk, credit=credit, width=width,
                                 occupancy=width - credit, wall=wk, spot=spot))
    if not out:
        return Verdict(False, f"{p['dte'][0]}~{p['dte'][1]} 天内没有可用的价差组合"
                              f"（宽度 {p['width_pct']:.0%}≈${spot * p['width_pct']:.1f}）。",
                       wall_used, [], p)
    out.sort(key=lambda c: -c.roi)
    return Verdict(True,
                   f"墙 {wall_used:g}（≤{p['cap']} 天累计 OI 最大），"
                   f"卖在墙上、宽度 {p['width_pct']:.0%}、{p['dte'][0]}~{p['dte'][1]} 天。"
                   f"实测 {p['n']} 笔未破墙 {p['unbroken']:.0%}、单笔 ROI {p['roi']:.1%}、"
                   f"年化 {p['annual']:.0%}。",
                   wall_used, out, p)


def should_exit(kind: str, sell_strike: float, spot: float,
                signal_side: str | None, signal_ratio: float) -> tuple[bool, str]:
    """是否该因反向极强信号提前平仓。

    两个条件缺一不可（用户 2026-08-31）：
      · 信号足够极端（≥30×）—— ≥10× 触发太频繁，白平的损失超过避开的亏损
      · 价格已经贴近卖腿（≤5%）—— 「如果离得很远，其实也无所谓」
    put 侧默认不用这条：它在样本里从没破过墙，平仓只是把时间价值还回去。
    """
    if kind == "P":
        return False, "put 侧不用信号平仓（实测平仓反而降低收益）"
    adverse = (signal_side == "看涨") if kind == "C" else (signal_side == "看跌")
    if not adverse or signal_ratio < SIG_EXIT_RATIO:
        return False, f"信号 {signal_side or '无'} {signal_ratio:.1f}× 未达 {SIG_EXIT_RATIO:g}×"
    dist = abs(sell_strike / spot - 1)
    if dist > SIG_EXIT_DIST:
        return False, (f"信号达标但价格距卖腿 {dist:.1%} > {SIG_EXIT_DIST:.0%}，"
                       f"离得远，不必平")
    return True, (f"反向极强信号 {signal_ratio:.1f}× 且价格距卖腿仅 {dist:.1%} → 当天平仓")
