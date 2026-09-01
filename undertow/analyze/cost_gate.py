"""成本闸门：把「这天大概能走多少」和「这笔要走多少才不亏」并排放。

起因（用户 2026-08-31 那晚的四笔亏损）：
  SLV 9/2 到期 60P，买 0.75 卖 0.73。方向是【对】的（SLV 60 破到 59.66），
  但那张 put 需要标的跌 1.9% 才覆盖点差+theta，当天只跌 0.75% → 白做。
  同晚 GLD 410C 持有 1 分钟割在 0.70，两小时后 0.89 —— 那 11% 就是点差本身。

两个数缺一不可：
  预期波动  = 这类日子标的大概走多少（由可判定率预测，见下表）
  回本门槛  = 你选的这张合约要走多少才不亏（真实 bid/ask + BS 的 Δ/θ）
预期波动 < 回本门槛 → 不用等开盘就知道做不成。

⚠️ 全部为【事前】量：可判定率来自盘前快照，bid/ask 是下单时的实时盘口。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from undertow.analyze import blackscholes as bs

# ─────────────────────────────────────────────────────────────────────────────
# 预期波动：由可判定率预测（2026-08-31 实测，倍数 ≥2× 的 66 笔）
#
#   可判定率      笔数   命中率   当日|波动|   顺向收益   波动>1%比例
#   <35%          12    67%     0.69%      +0.32%      8%
#   35~60%        25    64%     0.48%      +0.24%     12%
#   60~80%        23    61%     0.89%      +0.45%     39%
#   ≥80%           6    83%     1.84%      +1.50%     50%
#
#   可判定率 vs 当日波动幅度  r=+0.243 (n=66, t=2.01, p≈0.048)
#   可判定率 vs 是否命中     r=+0.053  ← 几乎为零
#
# 关键：可判定率预告的不是【方向准不准】（命中率 65% vs 66%，一样），
#       而是【今天有没有幅度】（波动 0.55% vs 1.09%，翻倍）。
#
# ⚠️ 这是在同一批数据上的第二次找模式（先测倍数闸门、再测可判定率），
#    多重比较风险仍在；≥80% 档只有 6 笔。分档均值只能当量级参考，
#    报告必须连同样本量一起显示，不得呈现为点估计。
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_MOVE_TABLE = [
    (0.00, 0.35, 0.69, 12, 0.08),
    (0.35, 0.60, 0.48, 25, 0.12),
    (0.60, 0.80, 0.89, 23, 0.39),
    (0.80, 1.01, 1.84,  6, 0.50),
]
EXPECTED_MOVE_EVIDENCE = {
    "n": 66, "r_move": 0.243, "t": 2.01, "p": 0.048, "r_hit": 0.053,
    "note": "同批数据第二次找模式，需样本外验证",
}


@dataclass(frozen=True)
class ExpectedMove:
    pct: float          # 该档实测平均 |当日波动|(%)
    n: int              # 该档样本量
    p_over_1pct: float  # 该档里波动 >1% 的比例
    band: str

    @property
    def weak(self) -> bool:
        return self.n < 10


def expected_move(decidable: float) -> ExpectedMove:
    """由可判定率给出这类日子的预期波动幅度（量级参考，非点预测）。"""
    for lo, hi, pct, n, p1 in EXPECTED_MOVE_TABLE:
        if lo <= decidable < hi:
            lab = f"{lo:.0%}~{hi:.0%}" if hi <= 1.0 else f"≥{lo:.0%}"
            return ExpectedMove(pct=pct, n=n, p_over_1pct=p1, band=lab)
    return ExpectedMove(pct=EXPECTED_MOVE_TABLE[-1][2], n=EXPECTED_MOVE_TABLE[-1][3],
                        p_over_1pct=EXPECTED_MOVE_TABLE[-1][4], band="≥80%")


@dataclass(frozen=True)
class Breakeven:
    symbol: str
    strike: float
    expiry: date
    kind: str
    dte: int
    bid: float
    ask: float
    iv: float
    delta: float
    theta: float          # 每日（bs.theta 已是每日，不要再除 365）
    spread_pct: float
    cost: float           # 一张成本（含权利金，不含手续费）

    def need_pct(self, spot: float, held_days: float, fee: float = 0.0) -> float:
        """标的要往顺向走多少 %，这笔才刚好不亏。

        need = (点差 + |θ|×持有天数 + 手续费/100) / |Δ| / 现价
        """
        if not self.delta or spot <= 0:
            return float("inf")
        spread = self.ask - self.bid
        need = (spread + abs(self.theta) * held_days + fee / 100.0) / abs(self.delta)
        return need / spot * 100.0

    @property
    def theta_share(self) -> float:
        """每日损耗占权利金的比例 —— 决定这张能拿多久。"""
        mid = (self.ask + self.bid) / 2
        return abs(self.theta) / mid if mid > 0 else float("inf")


def breakeven(symbol: str, spot: float, strike: float, expiry: date, kind: str,
              today: date, bid: float, ask: float, iv: float) -> Breakeven | None:
    """用真实盘口 + BS 的 Δ/θ 算这张合约的回本门槛。"""
    if not (bid and ask and ask > 0 and iv and iv > 0):
        return None
    dte = (expiry - today).days
    T = max(dte, 0.5) / 365.0
    k = "C" if kind.upper().startswith("C") else "P"
    dl = bs.delta(spot, strike, T, iv, k)
    th = bs.theta(spot, strike, T, iv, k)   # 已是每日
    mid = (ask + bid) / 2
    return Breakeven(symbol=symbol, strike=strike, expiry=expiry, kind=k, dte=dte,
                     bid=bid, ask=ask, iv=iv, delta=dl, theta=th,
                     spread_pct=(ask - bid) / mid if mid > 0 else float("inf"),
                     cost=ask * 100)


@dataclass(frozen=True)
class CostVerdict:
    ok: bool
    exp_move: ExpectedMove
    need_pct: float
    margin: float       # 预期波动 − 回本门槛，正=有余量
    text: str


def judge(be: Breakeven, spot: float, decidable: float, *,
          held_days: float = 1.0, fee: float = 3.20) -> CostVerdict:
    """并排对比：这天大概走多少 vs 这张要走多少才不亏。"""
    em = expected_move(decidable)
    need = be.need_pct(spot, held_days, fee)
    margin = em.pct - need
    ok = margin > 0
    if ok:
        t = (f"预期波动 {em.pct:.2f}% > 回本门槛 {need:.2f}%（余量 {margin:+.2f}pp）"
             f"—— 幅度上说得通。")
    else:
        t = (f"预期波动 {em.pct:.2f}% < 回本门槛 {need:.2f}%（差 {-margin:.2f}pp）"
             f"—— 就算方向做对，这类日子的典型幅度也覆盖不了点差和时间损耗。")
    t += (f" 依据：可判定率落在 {em.band} 档（n={em.n}"
          + ("，样本过小，仅作量级参考" if em.weak else "")
          + f"，该档 {em.p_over_1pct:.0%} 的日子波动超过 1%）。")
    if be.theta_share > 0.25:
        t += (f" ⚠️ 这张每天损耗掉权利金的 {be.theta_share:.0%}，"
              f"拿不过夜（{be.dte} 天到期）。")
    return CostVerdict(ok=ok, exp_move=em, need_pct=need, margin=margin, text=t)


def candidates(snap, spot: float, direction: str, today: date, *,
               decidable: float, max_dte: int = 45, per_bucket: int = 2,
               fee: float = 3.20, held_days: float = 1.0) -> list[tuple]:
    """顺着信号方向，扫出几张典型合约并算回本门槛。

    分三个到期桶（≤7 / 8~21 / 22~45 天）各取最接近平值的几张 —— 这样表里
    同时出现"近月便宜但 theta 高"和"远月贵但扛得住"，选择的代价一眼可见。
    用快照盘口（盘前已知），实际下单时点差可能不同。
    """
    want_call = direction in ("看涨", "偏多", "bullish", "up")
    buckets = ((1, 7), (8, 21), (22, max_dte))
    out: list[tuple] = []
    for lo, hi in buckets:
        pool = []
        for c in snap.contracts:
            if c.is_call != want_call:
                continue
            d = (c.expiry - today).days
            if not (lo <= d <= hi):
                continue
            if not (c.bid and c.ask and c.iv and c.iv > 0):
                continue
            # 只看虚值到轻度实值：太深实值资金占用大，太虚 Δ 太小
            m = (c.strike / spot - 1) * (1 if want_call else -1)
            if not (-0.02 <= m <= 0.06):
                continue
            pool.append((abs(m), c))
        pool.sort(key=lambda x: x[0])
        for _, c in pool[:per_bucket]:
            be = breakeven(f"{snap.proxy_symbol}{c.expiry:%y%m%d}{c.kind}{c.strike:g}",
                           spot, c.strike, c.expiry, c.kind, today,
                           c.bid, c.ask, c.iv)
            if be is None or abs(be.delta) < 0.05:
                continue
            out.append((be, judge(be, spot, decidable,
                                  held_days=held_days, fee=fee)))
    return out
