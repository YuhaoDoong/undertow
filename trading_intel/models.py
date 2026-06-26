"""数据模型：纯数据结构，不含 I/O、不含分析逻辑。

把 CFTC 原始的 194 字段收敛成几个语义清晰的交易者类别，
后续若接入其它数据源（如 CME 期权链）也复用同一套语义模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class TraderCategory:
    """某一类交易者的多/空/套利持仓（手数）。

    spread（跨期套利）部分不计入净头寸，因其方向中性。
    """

    long: int
    short: int
    spread: int = 0

    @property
    def net(self) -> int:
        return self.long - self.short

    @property
    def gross(self) -> int:
        return self.long + self.short


@dataclass(frozen=True)
class CategoryChange:
    """某一类交易者相对上周的持仓变化（CFTC 自带的 change_in_* 字段）。"""

    long: int
    short: int
    spread: int = 0

    @property
    def net(self) -> int:
        return self.long - self.short


@dataclass(frozen=True)
class CotReport:
    """单期 COT 周报的语义化快照。

    类别命名沿用 Disaggregated 报告:
        managed_money     投机资金（CTA/趋势跟踪）—— 常作拥挤度/反指
        other_reportables 其它可报告大户 —— 文中的"聪明钱"
        swap_dealers      互换交易商 —— 常含 OTC 对冲，方向歧义大
        producer_merchant 生产商/贸易商 —— 实体套保
        nonreportable     小户（非报告）
    """

    instrument: str
    report_date: date
    market_name: str
    open_interest: int
    open_interest_change: int

    managed_money: TraderCategory
    other_reportables: TraderCategory
    swap_dealers: TraderCategory
    producer_merchant: TraderCategory
    nonreportable: TraderCategory

    changes: dict[str, CategoryChange] = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    # 便于按名字遍历类别
    CATEGORY_NAMES = (
        "managed_money",
        "other_reportables",
        "swap_dealers",
        "producer_merchant",
        "nonreportable",
    )

    def category(self, name: str) -> TraderCategory:
        return getattr(self, name)

    def iter_categories(self):
        for name in self.CATEGORY_NAMES:
            yield name, getattr(self, name)


# ——————————————————————————————————————————————————————————
# 期权模型（Gamma 层）
# ——————————————————————————————————————————————————————————


@dataclass(frozen=True)
class OptionContract:
    """单个期权合约的快照（来自 CBOE 延迟报价）。"""

    expiry: date
    strike: float
    kind: str  # "C" 或 "P"
    open_interest: int
    volume: int
    gamma: float  # 数据源给的每股 gamma（仅作交叉校验，分析用自算 BS gamma）
    delta: float
    iv: float  # 隐含波动率（小数，如 0.26）

    @property
    def is_call(self) -> bool:
        return self.kind == "C"


@dataclass(frozen=True)
class OptionsSnapshot:
    """某标的的完整期权链快照（含多个到期）。"""

    instrument: str
    proxy_symbol: str  # 实际取数的标的（如 GLD）
    spot: float  # 标的现价（ETF 价）
    asof: str  # 数据时间戳
    contracts: list[OptionContract] = field(default_factory=list)

    def with_oi(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.open_interest > 0]

    def expiries(self) -> list[date]:
        return sorted({c.expiry for c in self.contracts if c.open_interest > 0})


# ——————————————————————————————————————————————————————————
# 价格序列模型（回测层）
# ——————————————————————————————————————————————————————————


@dataclass(frozen=True)
class PriceSeries:
    """日线收盘价序列，按日期升序。用于回测/价格对齐。"""

    symbol: str
    dates: list[date]  # 升序
    closes: list[float]

    def index_on_or_after(self, d: date) -> int | None:
        """第一个日期 >= d 的下标（用二分）。无则 None。"""
        import bisect

        i = bisect.bisect_left(self.dates, d)
        return i if i < len(self.dates) else None

    def forward_return(self, entry_idx: int, steps: int) -> float | None:
        """从 entry_idx 起第 steps 个交易日的收益率。越界 None。"""
        j = entry_idx + steps
        if entry_idx < 0 or j >= len(self.closes):
            return None
        base = self.closes[entry_idx]
        return (self.closes[j] / base - 1.0) if base else None
