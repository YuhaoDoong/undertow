"""墙位卖方价差 v2 —— 白银/黄金已激活，参数逐品种实测

取代 credit_wall.py（那版用近端分层墙 + 墙外偏移 + 盘中触及口径，全部是错的）。
2026-08-31 用户逐条纠正后重建，口径见 memory/wall-credit-spread-strategy.md：
  ① 墙 = 跨到期累计 OI 最大行权价（cap 参数控制到期上限）
  ② 破墙 = 到期日收盘越过卖腿，不是盘中触及
  ③ 价差按组合单中价成交（让 25% 点差），不是两边吃满
  ④ 【2026-09-01 推翻】原文"换墙当天平掉旧仓"是错的。用户："换墙是新开仓的
     换墙，不是让你浮亏平仓吧。" 换墙只影响【新开仓】选哪个行权价，
     已持仓一律持有到期，除非触发 should_exit。实测该错误使 37 笔里 27 笔
     被提前平掉（合计 −$37），而持到期的 10 笔赚 +$369。
  ⑤ 价差宽度按【现价百分比】，且每品种单独定
  ⑥ 反向极强信号 + 已破卖腿 → 平仓（见 should_exit）

═══════════════════════════════════════════════════════════════════════
⛔ 2026-09-01：PARAMS 里的全部回测数字（n / unbroken / roi / annual /
   occ / credit）作废，不得引用，重算前不要拿它们做任何决策。
   根因：回测用 `snapshot.spot` 当开仓现价，而 46 个快照里 34 个（74%）
   的 spot 不是文件名当天的价（详见 memory/snapshot-date-alignment-p0）。
   实例：2026-07-07 快照 spot=56.11 实为 7/6 收盘，7/7 真实收盘 54.46 ——
   回测据此"卖 55 put"以为价外 2%，实际卖腿已在价内。
   重算时 spot 必须取 C[D−1]（真实日线收盘），禁用 snapshot.spot。

⛔ 另一处未解决：`accum_wall` 的 band 参数是"在现价附近多大范围内找最大 OI"，
   于是【范围内总能找到一个最大值】，那不是墙。实测 SLV 真墙一直是 50
   （OI 16~20 万张），而 band=5% 每天选出 53/55/60 这些只有 3 万张的档位。
   用户 2026-09-01：「7月6日-7月7日刚从下方穿越上方的墙，最大的 put 墙还是 55，
   这时候肯定是卖 50 put。反倒是卖 call 可以激进点卖 55，保守点则卖 60。」
   → 待实现：(a) 墙要按 OI 绝对量认定，不是范围内相对最大；
             (b) 价格刚穿越某道墙时，该墙作为支撑不可靠，卖腿应退到下一道墙。

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
        # ⛔ 以下参数同样作废（污染网格挑出），保留仅为记录曾用值
        "cap": 9999, "confirm": 1, "band": 0.05, "width_pct": 0.03, "dte": (4, 11),
        "sides": ("P",),           # call 侧实测 ≈ 打平，暂不出
        # ⛔ 下面 6 个数作废（错位 spot），重算前不得引用
        "n": None, "unbroken": None, "roi": None, "annual": None,
        "occ": None, "credit": None, "fee_share": None,
        "note": "band=8%/宽度2% 的年化更高(+503%)但只有 22 笔；"
                "按用户要求取样本更多的 band=5%。宽度 3% 是拐点："
                "1% 时权利金被手续费吃掉 48%",
    },
    # ⚠️ 未激活。band 维度的结论存疑：ROI 随 band 变大而升高（5%→15% 是
    # 6.9%→15.4%），但权利金是【下降】的（$37.7→$24.2）——升高来自权利金门槛
    # 筛掉了低权利金的笔。配对比较（同 21 天）显示 band 大确实更好，
    # 但那是「墙离得远、缓冲厚」的效果，与样本期单边上涨无法分离。
    "gold": {
        # ⛔ 同上作废
        "cap": 9999, "confirm": 3, "band": 0.05, "width_pct": 0.01, "dte": (4, 11),
        "sides": ("P",),           # call 侧 -$487，明确不出
        # ⛔ 同上作废
        "n": None, "unbroken": None, "roi": None, "annual": None,
        "occ": None, "credit": None, "fee_share": None,
        "note": "band 从 15% 改 5% 后年化 +92%→+564%：原来选中距现价 -14% 的 350，"
                "那里一周到期报价 0.00。conf=3 优于 1，与白银相反；"
                "极强信号全都离墙远，信号平仓在黄金上触发 0 次",
    },
}
# QQQ 参数已测出（cap=9999/conf=1/宽度1%/年化+31%）但样本仅 19 个快照且占用 $697，
# 暂不激活。其余品种快照不足 5 份，无法回测。
# 2026-08-31 用户定：先只激活白银。黄金的 band=5%/+564% 同样有筛选嫌疑
# （被权利金门槛筛掉的笔全是低权利金的，剩下的自然好看），需单独查证后再开。
# ⛔ 2026-09-01 停用（codex P0）：此前只清空了 n/unbroken/roi/annual 等【绩效】数字，
#    但 cap / band / width_pct / dte / sides / confirm 这些【策略参数】同样是用
#    错位 snapshot.spot 的网格挑出来的，污染程度一样。只清绩效栏而留着参数继续
#    出候选，等于换个说法照做同一笔交易。
#    重开条件（三项全部满足）：
#      ① 快照捕获时序修好（按 captured_at 定 decision_session，见 P0-1）
#      ② 墙的定义修好（结构主墙 vs 局部 pin 分开，见 P0-5）
#      ③ 用新回测重跑参数网格，且通过日期簇置换检验
ACTIVE: set[str] = set()


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
            spot: float) -> Verdict:
    """给出该品种的墙位卖方价差候选。未激活的品种直接说明原因。

    ⚠️ spot 必填（codex 2026-09-01 P0）：原来允许回退 `snap.spot`，
    而 46 个快照里 34 个的 spot 不是当天的价。调用方必须显式传入
    决策时点已知的价格（真实日线 C[D−1] 或实时报价）。
    """
    if instrument not in ACTIVE:
        return Verdict(False,
                       f"{instrument} 未激活。2026-09-01 起【全品种停用】："
                       f"参数与绩效均出自用 snapshot.spot 当开仓价的污染回测，"
                       f"且快照捕获时序、墙的定义两个前提都还没修好。"
                       f"重开条件见模块内 ACTIVE 处的三项清单。")
    p = PARAMS[instrument]
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
                   f"⚠️ 无可引用的实测绩效：原数字出自污染回测，已全部作废。",
                   wall_used, out, p)


def should_exit(kind: str, sell_strike: float, spot: float,
                signal_side: str | None, signal_ratio: float) -> tuple[bool, str]:
    """是否该因反向极强信号提前平仓。

    两个条件缺一不可（用户 2026-08-31）：
      · 信号足够极端（≥30×）—— ≥10× 触发太频繁，白平的损失超过避开的亏损
      · 价格已经贴近卖腿（≤5%）—— 「如果离得很远，其实也无所谓」

    2026-09-01 改：原来 put 侧直接 return False（理由是"样本里从没破过墙"），
    但那个"从没破过"是用错位 spot 算出来的，不成立。用户明确要求：
    「这种极强信号出现破墙时，应该平仓卖方价差，改为顺向买方。」
    现在两侧同规则。历史上"极强信号 + 已破卖腿"只触发过 1 次
    （2026-07-07 开仓 / 07-10 触发）：持有到期 −$150、触发日平仓 −$105、
    反手买 put 两日 +71% —— n=1 不构成统计证据，采纳的理由是不对称性：
    破墙后卖方剩余收益封顶在已收权利金，剩余风险却是整个价差宽度。
    """
    adverse = (signal_side == "看涨") if kind == "C" else (signal_side == "看跌")
    if not adverse or signal_ratio < SIG_EXIT_RATIO:
        return False, f"信号 {signal_side or '无'} {signal_ratio:.1f}× 未达 {SIG_EXIT_RATIO:g}×"

    # ⚠️ codex 2026-09-01 P0：原实现只看 |sell/spot − 1| 这个【无方向】的距离，
    # 两头都错：
    #   · 卖 60C、现价 59 —— 尚在安全侧（未破墙），却因距离 1.7% 被平掉；
    #   · 卖 60P、现价 50 —— 已经深度破墙 16.7%，却因距离 >5% 拒绝平仓，
    #     等于在风险最大的时候锁死出口。
    # 正确顺序：先判有方向的破墙，再让距离只承担「尚在安全侧时别乱动」。
    breached = (spot > sell_strike) if kind == "C" else (spot < sell_strike)
    if breached:
        gap = abs(spot / sell_strike - 1)
        return True, (f"反向极强信号 {signal_ratio:.1f}× 且【已破卖腿】"
                      f"（现价 {spot:g} vs 卖腿 {sell_strike:g}，越过 {gap:.1%}）→ 当天平仓")

    dist = abs(sell_strike / spot - 1)
    if dist > SIG_EXIT_DIST:
        return False, (f"信号达标但仍在安全侧且距卖腿 {dist:.1%} > {SIG_EXIT_DIST:.0%}，"
                       f"离得远，不必平")
    return True, (f"反向极强信号 {signal_ratio:.1f}×，虽未破墙但价格已逼近卖腿"
                  f"（距 {dist:.1%}）→ 当天平仓")
