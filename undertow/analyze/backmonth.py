"""远月结构异动扫描（playbook R16）—— 近月窗口的已知盲区。

flow 层只看 ≤45 天 + 近价带，结构性地看不见远月布局：机构在 12 月合约上
连续几日堆 call、而近月 ATM IV 在降、现价上方 call 卖压未撤 —— 报告零提示。
两个时间尺度的仓位并存并不矛盾（一边持远月上行尾部、一边近月做空）。

【时间尺度隔离纪律】远月异动只作长期背景：
  · 不进综合分、不进日度方向研判、不改任何近月位点
  · 输出必须自带「月度级配置信号，与本周方向无关」标注
  · 与近月压力口径隔离，避免尾部污染 tilt
这条纪律是硬约束，由测试锁住 —— 违反它就等于用季度级的仓位去指导当日交易。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 触发门槛：单日 OI 增幅 ≥50% 且绝对增量 ≥1,000 手。
# 双门槛缺一不可：只看增幅会让 20→60 张这种噪音刷屏；
# 只看绝对量则漏掉基数小但在快速堆积的新建仓（正是要抓的那种）。
BM_MIN_DTE = 46          # 远月起点：与 gamma.WALL_LAYERS 的 far 层对齐
BM_MIN_GROWTH = 0.50
BM_MIN_DOI = 1000
BM_TOP_N = 12


@dataclass(frozen=True)
class BackMonthMove:
    expiry: date
    strike: float
    kind: str
    dte: int
    prev_oi: int
    curr_oi: int
    d_oi: int
    growth: float
    moneyness: float       # (strike/spot-1)，call 为正=虚值
    iv: float
    volume: int

    @property
    def new_build(self) -> bool:
        """基数极小时的堆积 —— 更像新布局而非加仓。"""
        return self.prev_oi < self.d_oi * 0.5

    @property
    def label(self) -> str:
        side = "上行" if self.kind == "C" else "下行"
        return f"{side}尾部" if abs(self.moneyness) > 0.15 else f"{side}结构"


@dataclass(frozen=True)
class BackMonthScan:
    moves: list[BackMonthMove] = field(default_factory=list)
    total_call_doi: int = 0
    total_put_doi: int = 0
    n_expiries: int = 0
    scanned: int = 0

    @property
    def empty(self) -> bool:
        return not self.moves

    @property
    def tilt(self) -> str:
        """仅描述远月这一层的倾向。⚠️ 绝不可用于日度方向研判。"""
        c, p = self.total_call_doi, self.total_put_doi
        if c == 0 and p == 0:
            return "无"
        if c >= p * 2:
            return f"远月上行布局占优（call {c:,} vs put {p:,}）"
        if p >= c * 2:
            return f"远月下行布局占优（put {p:,} vs call {c:,}）"
        return f"两侧并存（call {c:,} / put {p:,}）"


def scan(prev, curr, today: date, spot: float, *,
         min_dte: int = BM_MIN_DTE, min_growth: float = BM_MIN_GROWTH,
         min_doi: int = BM_MIN_DOI, top_n: int = BM_TOP_N) -> BackMonthScan:
    """扫描远月（>45 天）单日 OI 异动。

    只用 OI 结构，不做买卖方判定 —— 远月报价稀疏、深虚合约一个跳动就反推出
    巨大 IV 变化（同 R15 的理由），IV 方向在这里不可信。
    """
    pmap = {(c.expiry, c.strike, c.kind): c.open_interest
            for c in prev.contracts} if prev is not None else {}
    moves: list[BackMonthMove] = []
    scanned = 0
    for c in curr.contracts:
        d = (c.expiry - today).days
        if d < min_dte:
            continue
        scanned += 1
        p_oi = pmap.get((c.expiry, c.strike, c.kind), 0)
        d_oi = c.open_interest - p_oi
        if d_oi < min_doi:
            continue
        growth = (d_oi / p_oi) if p_oi > 0 else float("inf")
        if growth < min_growth:
            continue
        moves.append(BackMonthMove(
            expiry=c.expiry, strike=c.strike, kind=c.kind, dte=d,
            prev_oi=p_oi, curr_oi=c.open_interest, d_oi=d_oi,
            growth=growth, moneyness=(c.strike / spot - 1) if spot > 0 else 0.0,
            iv=c.iv, volume=c.volume))
    moves.sort(key=lambda m: -m.d_oi)
    return BackMonthScan(
        moves=moves[:top_n],
        total_call_doi=sum(m.d_oi for m in moves if m.kind == "C"),
        total_put_doi=sum(m.d_oi for m in moves if m.kind == "P"),
        n_expiries=len({m.expiry for m in moves}),
        scanned=scanned,
    )
