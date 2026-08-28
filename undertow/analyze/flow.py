"""期权资金流 / 持仓异动分析（确定性计算，无 I/O）。

动机（来自对 WTI 6/22 的整理）:
  该方法抓的不是「静态的墙」(那是 gamma.py 的活)，而是【墙的增量 + 买卖方性质】：
  逐行权价看 ΔOI、当前OI、精确Delta、以及【Delta 修正后的相对 IV 变化】，
  据此判断每个行权价是【买方建仓】还是【卖方建仓/撤退】：
    · OI 增 + IV 升 = 买方在抬价买入（看跌买保护 / 看涨买突破）
    · OI 增 + IV 降 = 卖方在写权收钱（写 put 做支撑 / 写 call 做压制）
  据此判断 WTI「80 上方极强卖方压制、65 下方大量买方保护、put 越来越贵→下行风险更大」，
  结果 6/24 原油如其所料走弱。

  关键洞见：延迟数据没有逐笔成交，但【IV 变化的方向】可作买/卖方的代理——
  买方抬价→IV 升，卖方供给→IV 降。这就是不用 tick 数据也能分买卖方的窍门。

本模块两件事:
  1) scan_unusual(snap)        —— 单张快照即可：按 volume/OI 找"今日异常活跃"。
  2) analyze_flow(prev, curr)  —— 两日快照 diff：逐 (到期,行权价,C/P) 求 ΔOI / ΔIV，
     做【Delta 修正】(剔除现价移动沿偏斜的机械 IV 变化)，再按 OI 增减 × 修正IV 方向
     判定买方/卖方，复刻买卖方判定表。需 ≥2 天落盘快照（CBOE 无期权历史，自攒）。

诚实标注:
  * 「Delta 修正后相对 IV 变化」是对该方法论的【原理化近似】(剔除 skew×Δspot 的机械项)，
    不是其精确公式；买卖方判定在边界行可能与人工的酌情判断不同。
  * 仍是 ETF 代理（USO≠WTI，行权价/IV 仅定性）、样本短，只作预警不作预言。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from undertow.core.models import OptionsSnapshot, OptionContract

DEFAULT_HORIZON_DAYS = 60      # 只看近月
NEAR_MONEY_BAND = 0.15        # 只看现价 ±15% 内的行权价
MIN_DOI = 50                  # |ΔOI| 低于此视为噪音（异动表）
TOP_N = 15                    # 单快照异动榜上限
TABLE_N = 14                  # 买卖方表 put/call 各自上限
UNUSUAL_MIN_VOLUME = 50
UNUSUAL_MIN_VOL_OI = 0.5
# —— 买卖方判定阈值（pp，Delta 修正后）——
IV_NOISE = 0.08              # |修正IV| 低于此 = 噪音
IV_MILD = 0.28              # 区分"轻微" vs 正常强度
IV_STRONG = 1.0            # call 卖方"极强压制"门槛
MAX_ABS_DELTA = 0.90       # |delta| 超过此=深 ITM，IV 不可靠，剔除
REL_MIN_STRIKES = 8        # 行权价数 ≥ 此才做"相对化"(减中位 ΔIV，剔全局 vol 平移)
# —— 绝对 IV 闸门：相对化(减中位数)会在全市场 IV 齐涨/齐落时制造【假买方/假卖方】——
# 事件后(CPI/FOMC)IV 溢价整体释放时，call 端全在写权(绝对 IV 齐落)，但相对化后
# "跌得比中位少"的腿会翻成正 adj_iv → 被误判为"买方抬价"(实例：黄金 8/14 415C
# +69,887 手绝对 IV -1.18pp 却被判买方，把偏空硬翻成偏多)。该方法论用【绝对 IV 方向
# (升=买方/降=卖方) + 相对 skew】两者都看；我们只留了相对、丢了绝对。闸门补回：某腿的
# 相对判定(买方需 IV 升 / 卖方需 IV 降)与其【绝对 ΔIV】方向矛盾且绝对变化显著(≥此)时，
# 判为"随市"存疑 → 不投方向票(neutral)。只在事件级 IV 整体位移时触发，日常小波动不误伤。
IV_ABS_GATE = 0.5          # |绝对 ΔIV| ≥ 此(pp)且与相对判定方向矛盾 → 存疑，不计方向
# —— 曲面闸门：用【固定 Delta】曲面方向否决与之矛盾的逐腿判定 ——
# 动机（2026-08-19 黄金复盘，作者当天判"强烈转多"、次日兑现）：
# 当日 ATM IV 齐涨 +2.78pp、现价 +2.68%。逐腿走的是"固定行权价 ΔIV → Delta 修正
# → 再减中位数"两次扣减，在这种大幅单边日会把真实买盘整体翻成卖压：
#   445C ΔOI +55,845 原始ΔIV +1.20pp → 判「卖方压制」（≈作者说的 4800 需求激活）
#   425C ΔOI +55,388 原始ΔIV +1.59pp → 判「极强卖方压制」（≈作者说的 4600 第一关）
# 共 33 条腿、ΔOI +126,777 被反向计票，凑出"看跌加权增仓 253,097"。
# 而【固定 Delta】阶梯同一天读到 Call 六档全线 +3.27~+3.86pp —— 完全正确，
# 因为固定 Delta 天然吸收 moneyness 漂移，不需要机械项修正、也就不会被扣翻。
# 因此：曲面方向明确时，与之矛盾的逐腿判定一律降级为存疑（权重 0），**只否决、不反转**。
SURF_GATE_PP = 0.8         # 固定 Delta 曲面某侧净变动 ≥ 此(pp) 才算"方向明确"
SURF_GATE_CONSISTENT = 5   # 六档中至少几档同号才算曲面一致（避免拿噪音当曲面）
# —— churning 折减：某方向大量换手但净 OI 小 = 结构调整/滚仓，方向压力打折 ——
# 转化率 = |净ΔOI| / 毛|ΔOI|总额（机构口径"成交巨大但净 OI 只 +N = churning"）。
# turnover ≥ 此视为干净方向仓（f=1）；低于则线性折减到 0，杜绝毛增仓被读成方向。
TURNOVER_HEALTHY = 0.5
# —— 价差结构识别（保守，宁缺勿滥，避免把相邻买卖方误配成价差）——
SPREAD_MAX_WIDTH_FRAC = 0.08  # 两腿行权价间距 ≤ 短腿×此（垂直价差两腿应相近）
SPREAD_MIN_SIZE = 2 * MIN_DOI  # 两腿较小者需 ≥ 此（双腿都明显高于噪音才算）
SPREAD_TOP_N = 4             # 最多上报的价差数（按规模取前 N，其余视为噪音）
# —— 速读用结构性异动（墙体滚动/墙上增减/最大建仓，门槛取"结构级"远高于噪音）——
ROLL_MAX_GAP_FRAC = 0.04     # 滚动两腿行权价间距 ≤ 旧腿×此（相邻才算"搬墙"）
ROLL_BALANCE = 0.4           # 两腿规模较小者 ≥ 较大者×此，一减一增才认定为同一笔搬仓
MOVE_MIN_DOI = 1_000         # 进速读的单点 |ΔOI| 门槛
# —— 波动率面（ATM IV / 25Δ·10Δ 偏斜，机构口径的"买方确认"检查）——
VOL_MIN_DAYS = 10            # 到期太近 IV 噪音大，不用
VOL_MAX_DAYS = 90
VOL_TARGET_DAYS = 30         # 优先取最接近 30 天的到期做"标尺"
VOL_MIN_QUOTES = 4           # 单侧（C/P 各自）至少几个有效 IV 报价才可信
VOL_MAX_ABS_DELTA = 0.85     # 深 ITM 的 IV 不可靠，剔除
VOL_MIN_ABS_DELTA = 0.02     # 太深 OTM 报价稀疏，也剔除
D_ATM_SIG = 0.3              # |ΔATM IV| 超过此(pp)才算有信息
D_SKEW_SIG = 0.5             # |Δskew| 超过此(pp)才算明显收敛/走陡
D_SPOT_SIG = 0.5             # |Δspot%| 超过此才算"明显涨/跌"
# —— 强信号检测：资金流"一边倒"的教科书组合，独立于综合投票，触发即置顶显著告警 ——
# 动机(复盘 8/19 黄金)：综合层多因子投票会把单层强信号与慢因子(COT 周频滞后)/正伽马
# 对冲成"分歧/中性"，埋没了"主翼 call 买盘一边倒 + 上行压力压倒下行"这种领先信号——
# 该信号次日(8/20 晚)兑现为金银直线上涨。这里用远比投票严格的门槛(必须一边倒)把它单独
# 拎出来置顶，宁缺勿滥：多数日子不触发，一旦触发就是值得盯的近端领先结构。
STRONG_PRESSURE_RATIO = 3.0   # 顺向压力 ≥ 此×逆向 才算方向压力一边倒（投票只需 1.3）
STRONG_WING_RATIO = 4.0       # 主翼(20~45Δ)买卖权重 ≥ 此×反向 才算主翼一边倒
STRONG_MIN_WING_W = 3.0       # 一边倒方向的主翼最小权重（过滤清淡日的偶发单）
STRONG_MIN_NET_DOI = 3_000    # 顺向净建仓 OI 最小规模（ETF 口径，过滤清淡日）
STRONG_WING_DELTA_LO = 0.18   # 主翼 Delta 下界（更 OTM 的深尾不单独定性）
STRONG_WING_DELTA_HI = 0.45   # 主翼 Delta 上界（避开 ATM 的方向含糊腿）
STRONG_CONTRA_SPOT = 1.5      # 当日价格逆信号方向 ≥ 此% 则抑制（涨后防守型 put 买盘≠看空）


@dataclass(frozen=True)
class UnusualContract:
    expiry: date
    strike: float
    kind: str
    open_interest: int
    volume: int
    iv: float
    delta: float
    vol_oi_ratio: float
    moneyness: float
    note: str


@dataclass(frozen=True)
class FlowChange:
    """两日 diff 里一个 (到期,行权价,C/P) 的持仓异动 + 买卖方判定。"""
    expiry: date
    strike: float
    kind: str
    prev_oi: int
    curr_oi: int
    d_oi: int
    delta: float           # 精确 Delta（CBOE 给）
    prev_iv: float
    curr_iv: float
    d_iv_pp: float         # 原始 ΔIV ×100 (pp)
    adj_iv_pp: float       # Delta 修正后的相对 ΔIV (pp)
    curr_volume: int
    moneyness: float
    bias: str              # bearish / bullish / neutral（粗方向，供聚合）
    judgment: str          # 买方保护 / 卖方做支撑 / 卖方压制 / 卖方撤退 …（细判，机构口径）
    on_wall: str           # put墙 / call墙 / ""
    note: str
    weight: float = 0.0    # 该腿的方向权重（用于压力聚合 / 价差扣减）
    spread_note: str = ""  # 若属某价差结构的腿，标注（如"熊市看涨价差·保护腿"）

    @property
    def oi_conversion(self) -> float | None:
        """**新仓纯净度** = |ΔOI| / 当日成交量。

        同样是 "+200 手"，含义可以天差地别：
          * 成交 203、OI +202 → 转化率 ~1.0：当天几乎每一笔都变成了新仓，
            是干净的新建仓，这个 ΔOI 代表真实的新增意愿
          * 成交 3029、OI -247 → 转化率 0.08：绝大部分是日内换手/对敲平进平出，
            ΔOI 这个数字几乎不携带方向信息

        ΔOI 取绝对值，所以减仓侧同样适用：转化率高 = 当天成交主要在了结旧仓。

        ⚠️ 与机构口径的差异：CME 会单列 PNT（场外协商成交）并从量里剔除，
        因为那部分是预先谈好的、不反映盘中意愿。CBOE 延迟数据没有 PNT 字段，
        所以我们的转化率里仍混着这类成交——**读数只可用于降权，不可用于加权**。
        """
        if not self.curr_volume:
            return None
        return abs(self.d_oi) / self.curr_volume


# 新仓纯净度分档。阈值取自"成交全部转化为 OI"这个理想端的相对距离，
# 不是拟合出来的——目前没有样本可以校准它，故只用于**降权与标注**，不参与打分。
PURITY_CLEAN = 0.70     # ≥ 此值：当天成交绝大部分沉淀为持仓，新仓/了结都算干净
PURITY_MIXED = 0.30     # ≥ 此值：部分沉淀，掺杂日内换手
# < PURITY_MIXED：绝大部分是日内进出，ΔOI 不代表真实意愿

# 转化率理论上不可能 > 1：每一张成交最多只能产生一张新 OI。超过说明
# **ΔOI 与成交量不同源**——CBOE 延迟数据里 OI 来自 OCC 隔夜结算（完整上一交易日），
# 而 volume 可能是快照当刻的当日部分量，两者时点错配。这种行必须显式标出，
# 绝不能因为"比值大"就当成最干净的新仓（那正好是反的：该行的量根本没统计全）。
PURITY_IMPLAUSIBLE = 1.10   # 留 10% 余量给舍入/结算微差


def purity_label(ratio: float | None) -> str:
    """把转化率翻成一句可读判定。None（无成交数据）返回空串而不是猜。"""
    if ratio is None:
        return ""
    if ratio > PURITY_IMPLAUSIBLE:
        return "⚠OI变动>成交·口径错配"
    if ratio >= PURITY_CLEAN:
        return "干净新仓"
    if ratio >= PURITY_MIXED:
        return "掺换手"
    return "多为日内换手"


def purity_reliability(ratio: float | None) -> str:
    """可靠度标签，对齐机构口径里的「可靠度 高/中/低」。

    口径错配的行判「存疑」而非「高」——比值大恰恰说明成交量没统计全。
    """
    if ratio is None:
        return "未知"
    if ratio > PURITY_IMPLAUSIBLE:
        return "存疑"
    if ratio >= PURITY_CLEAN:
        return "高"
    if ratio >= PURITY_MIXED:
        return "中"
    return "低"


@dataclass(frozen=True)
class Spread:
    """检测到的疑似垂直价差结构（同 C/P、**同到期**、相邻行权价、卖一腿 + 买一腿）。"""
    kind: str
    expiry: date          # 垂直价差必须同到期；标签回填也按它匹配，防跨月串标签
    name: str             # 熊市看涨价差(Bear Call) 等
    short_strike: float   # 卖出腿（决定方向）
    long_strike: float    # 买入腿（封顶/保护腿）
    size: int
    net_bias: str         # bearish / bullish（看短腿）
    detail: str


@dataclass(frozen=True)
class VolRead:
    """某一天、某个到期的波动率面读数（单位 pp，即百分数点）。"""
    expiry: date
    days_out: int
    atm_iv_pp: float       # ATM 隐含波动率
    skew25_pp: float       # 25Δ put IV − 25Δ call IV（越大 = put 越贵 = 下行担忧越重）
    skew10_pp: float       # 10Δ 同理（更极端的尾部保护）


@dataclass(frozen=True)
class VolSurface:
    """两日波动率面对比 + 机构口径判读。

    一次黄金分析的方法：大涨若无买方在期权端追价（ATM IV 反被压、
    put-call skew 不明显收敛），说明上涨是空头回补而非新多进场，动力存疑。
    诚实边界：事件日（非农/CPI/FOMC 兑现后）IV 回落含事件溢价释放的机械成分，判读要打折。
    """
    curr: VolRead
    prev: VolRead | None
    d_spot_pct: float      # 现价两日变化 %
    verdict: str           # 中文判读

    @property
    def d_atm_pp(self) -> float:
        return (self.curr.atm_iv_pp - self.prev.atm_iv_pp) if self.prev else 0.0

    @property
    def d_skew25_pp(self) -> float:
        return (self.curr.skew25_pp - self.prev.skew25_pp) if self.prev else 0.0

    @property
    def d_skew10_pp(self) -> float:
        return (self.curr.skew10_pp - self.prev.skew10_pp) if self.prev else 0.0


@dataclass(frozen=True)
class FlowAnalysis:
    instrument: str
    proxy_symbol: str
    spot: float
    horizon_days: int
    curr_date: str
    curr_asof: str
    prev_date: str | None
    # 单快照异动
    unusual: list[UnusualContract] = field(default_factory=list)
    total_call_volume: int = 0
    total_put_volume: int = 0
    # 两日 diff
    changes: list[FlowChange] = field(default_factory=list)
    net_call_doi: int = 0          # 近月增建 call OI 净增（kind 求和，向后兼容）
    net_put_doi: int = 0           # 近月增建 put OI 净增
    downside_pressure: float = 0.0  # 买卖方判定加权的下行压力（已扣价差保护腿）
    upside_pressure: float = 0.0
    flow_tilt: str = "—"
    spreads: list[Spread] = field(default_factory=list)  # 检测到的疑似价差结构
    call_wall: float | None = None
    put_wall: float | None = None
    vol: VolSurface | None = None  # 波动率面：ATM IV / skew 日变化（买方确认检查）
    # churning 折减系数（已作用于 upside/downside_pressure）。此前只出现在 tilt 字符串里，
    # 台账拿不到 → 无法回答"强信号是不是建立在滚仓上"。暴露成字段供记录，不改判定逻辑。
    churn_call: float = 1.0
    churn_put: float = 1.0
    # —— 净有效 Delta：Σ(ΔOI × delta)，机构常用的方向敞口口径（外部分析者 8/26 帖同款）——
    # ⚠️ 它与 upside/downside_pressure 是【两种不同性质的量】，务必分清：
    #   · 净有效 Delta 是【观测】：纯算术，不需要判断谁是主动方。
    #     高 Delta call 减仓 + 远虚 call 增仓 → 即便 call 总 OI 上升，净 Delta 仍为负。
    #   · 加权增仓(pressure) 是【推断】：先用 IV 方向判买卖方，再按 |ΔOI|×权重聚合。
    # 2026-08-27 实测：两者在有明确方向的 30 个样本里方向相反 18 个（60%），
    # QQQ 近 8 天更是无一日一致。
    # ⚠️ 口径更新（同日晚些）：净 Delta 已**不再只是展示项** —— direction.decide()
    # 用"两口径反向"作为软弃权条件之一，因此它会否决方向裁决（进而否决强信号）。
    # 但它仍**不单独产生方向**：反向时的结果是"方向不明"，不是"按净 Delta 判"。
    net_delta_call: float = 0.0
    net_delta_put: float = 0.0
    net_delta_total: float = 0.0
    # —— 方向裁决（含弃权与理由）。见 analyze/direction.py ——
    # flow_tilt 是给人读的散文；这个是给下游模块判定用的结构化结果。
    # 两者必须同源：tilt 由它渲染而来，不得各算各的。
    call: object | None = None


@dataclass(frozen=True)
class StrongSignal:
    """近端资金流"一边倒"的强信号（独立于综合投票，供置顶告警）。"""
    direction: str           # 看涨 / 看跌
    level: str               # 强 / 极强（极强＝波动率面同向追认）
    pressure_ratio: float    # 顺向压力 / 逆向压力
    wing_ratio: float        # 主翼顺向权重 / 逆向权重
    net_doi: int             # 顺向净建仓 OI
    vol_confirms: bool       # 波动率面是否同向追认
    reasons: list[str] = field(default_factory=list)
    diverges: bool = False   # 与【近端】方向不同向
    outlook_bias: str = ""   # 对照用的近端方向
    mid_bias: str = ""       # 中期方向（供文案讲清"与哪一层一致、与哪一层冲突"）
    conflicts_mid: bool = False
    # 方向裁决为"低置信"（软条件未过、走 shadow）时置位。
    # **检测与可执行性分离**（codex review 2026-08-27）：检测口径保持稳定，
    # 由渲染层决定降级为琥珀提示还是置顶红色告警 —— 不再用 return None 把两件事耦死。
    low_confidence: bool = False


def wing_weights(fa: "FlowAnalysis") -> tuple[float, float]:
    """主翼(20~45Δ)方向权重 (看涨, 看跌)。

    方向聚合：看涨=call 买盘 + put 卖方做支撑；看跌=call 卖压制 + put 买保护。
    只计新建仓(d_oi>0)——减仓腿由 _judge 半权定性，不参与"一边倒"判定。
    ⚠️ 单一实现：detect_strong_signal 与 probe_strong_signal 共用，别复制第二份
    （否则台账记下的数会和告警用的数悄悄漂移，回测就白攒了）。
    """
    def _w(kind: str, bias: str) -> float:
        return sum(c.weight for c in fa.changes
                   if c.kind == kind and c.bias == bias and c.d_oi > 0
                   and STRONG_WING_DELTA_LO <= abs(c.delta) <= STRONG_WING_DELTA_HI)
    return (_w("C", "bullish") + _w("P", "bullish"),
            _w("C", "bearish") + _w("P", "bearish"))


def detect_strong_signal(fa: "FlowAnalysis", *, outlook_bias: str = "",
                         mid_bias: str = "") -> "StrongSignal | None":
    """检测资金流是否"一边倒"到值得置顶的强信号；不满足严格门槛则返回 None。

    看涨：上行压力 ≫ 下行 + 主翼(20~45Δ)买盘(call 买 + put 卖支撑)权重 ≫ 反向 +
    净建 call OI 显著。看跌为镜像。门槛远严于综合投票，宁缺勿滥。
    与综合 outlook_bias 方向不一致时置 diverges（提示"近端领先、可能抢跑于慢因子"）。
    """
    if not fa.prev_date:
        return None
    # —— 与方向裁决保持一致：裁决弃权时，强信号不得独自开火 ——
    # 强信号用的就是同一套 upside/downside_pressure，它不是第二份独立证据
    # （实测与 pressure 方向 100% 共线）。裁决因"两口径反向"或"数据过期/未结算"
    # 而弃权时，让红色横幅照亮，就是我们反复修掉的那种"指标互相打架"。
    # 2026-08-27 实测：QQQ/WTI 裁决为"方向不明（两口径反向）"，⚡ 却仍在亮。
    # ⚠️ call 缺失（旧构造/测试桩）时**按开火处理**，与历史行为一致；
    # 但生产路径的 analyze_flow 一定会填 call，所以真实行为由裁决决定。
    # codex review 指出：现有强信号测试大多传 call=None，因此它们**守不住生产路径** ——
    # 已补 test_strong_signal_obeys_abstention 专门覆盖 call 存在的情形。
    # 注：这里 return None 只影响【展示与下游判定】；台账走的是 probe_strong_signal，
    # 它独立记录三条闸门的连续值与通过情况，候选样本不会因此丢失。
    _call = getattr(fa, "call", None)
    if _call is not None and getattr(_call, "abstain", False):
        return None      # 硬弃权（无数据/未结算/已过期）→ 检测本身不成立
    _lowc = bool(getattr(_call, "low_confidence", False)) if _call is not None else False

    bull_wing, bear_wing = wing_weights(fa)
    up, dn = fa.upside_pressure, fa.downside_pressure

    def _diverges(direction: str) -> bool:
        """是否与【对照方向】不同向。

        ⚠️ 对照对象必须是【近端】层，不是综合分。强信号本身就是 Flow 子层的产物，
        拿它去比被中期主导的综合 bias，会得出荒谬结论：2026-08-27 QQQ 近端＝偏空(弱)、
        中期＝偏多、综合＝偏多，强看跌信号与【近端一致】，横幅却写"与综合研判背离"，
        把真正调和二者的那一层藏了起来 —— 这正是用户看到"偏多"和"强看跌"并列
        觉得自相矛盾的根源。调用方应传 near_bias；未传时退回综合分并在文案里说明。
        """
        b = outlook_bias or ""
        key = "多" if direction == "看涨" else "空"
        return key not in b

    def _atm_conf(want_up: bool) -> bool:
        # 涨且 ATM IV 抬升 = 买方追价（看涨确认）；跌且 ATM IV 抬升 = 恐慌买保护（看跌确认）
        vs = fa.vol
        if not vs or not vs.prev:
            return False
        return (vs.d_spot_pct > D_SPOT_SIG and vs.d_atm_pp > D_ATM_SIG) if want_up \
            else (vs.d_spot_pct < -D_SPOT_SIG and vs.d_atm_pp > D_ATM_SIG)

    def _skew_conf(want_up: bool) -> bool:
        # 25Δ skew = put IV − call IV：下降=call 相对变贵=抢 call（看涨）；上升=put 变贵（看跌）。
        # 复刻"skew 全面向上倾斜(call 端)"＝转多的核心信号之一。
        vs = fa.vol
        if not vs or not vs.prev:
            return False
        return (vs.d_skew25_pp <= -D_SKEW_SIG) if want_up else (vs.d_skew25_pp >= D_SKEW_SIG)

    def _vc(want_up: bool) -> bool:
        return _atm_conf(want_up) or _skew_conf(want_up)

    # 价格背离闸门：当日价格朝信号反方向大幅移动＝该"压力"多半是移动后的对冲/防守
    # （如大涨日的 put 保护买盘），而非方向性建仓，不配置顶红标。d_spot 缺失时不设闸。
    dsp = fa.vol.d_spot_pct if (fa.vol and fa.vol.prev) else None

    def _contra(want_up: bool) -> bool:
        if dsp is None:
            return False
        return (dsp <= -STRONG_CONTRA_SPOT) if want_up else (dsp >= STRONG_CONTRA_SPOT)

    # —— 看涨 ——
    if (up >= STRONG_PRESSURE_RATIO * max(dn, 1.0)
            and bull_wing >= STRONG_WING_RATIO * max(bear_wing, 0.5)
            and bull_wing >= STRONG_MIN_WING_W
            and fa.net_call_doi >= STRONG_MIN_NET_DOI
            and not _contra(True)):
        pr, wr = up / max(dn, 1.0), bull_wing / max(bear_wing, 0.5)
        vc = _vc(True)
        reasons = [
            f"看涨加权增仓 {up:,.0f} ≫ 看跌 {dn:,.0f}（{pr:.1f}×，压倒性）"
            f"——即 call 买盘＋put 卖方做支撑 全面压过 call 卖压＋put 买保护",
            f"主翼 20~45Δ call 买盘权重 {bull_wing:.1f} ≫ 卖压 {bear_wing:.1f}（{wr:.1f}×）",
            f"近月 call 净建仓 +{fa.net_call_doi:,} 手",
        ]
        if _atm_conf(True):
            reasons.append(f"波动率面追认：价涨 {fa.vol.d_spot_pct:+.1f}% 且 ATM IV {fa.vol.d_atm_pp:+.2f}pp（买方追价，非空头回补）")
        if _skew_conf(True):
            reasons.append(f"skew 向 call 倾斜：25Δ(put−call) {fa.vol.d_skew25_pp:+.2f}pp（call 相对变贵＝抢 call）")
        return StrongSignal("看涨", "极强" if vc else "强", round(pr, 1), round(wr, 1),
                            fa.net_call_doi, vc, reasons, _diverges("看涨"), outlook_bias,
                            mid_bias, "空" in (mid_bias or ""), _lowc)

    # —— 看跌 ——
    if (dn >= STRONG_PRESSURE_RATIO * max(up, 1.0)
            and bear_wing >= STRONG_WING_RATIO * max(bull_wing, 0.5)
            and bear_wing >= STRONG_MIN_WING_W
            and fa.net_put_doi >= STRONG_MIN_NET_DOI
            and not _contra(False)):
        pr, wr = dn / max(up, 1.0), bear_wing / max(bull_wing, 0.5)
        vc = _vc(False)
        reasons = [
            f"看跌加权增仓 {dn:,.0f} ≫ 看涨 {up:,.0f}（{pr:.1f}×，压倒性）"
            f"——即 call 卖压＋put 买保护 全面压过 call 买盘＋put 卖方支撑",
            f"主翼 20~45Δ 卖压/买保护权重 {bear_wing:.1f} ≫ 反向 {bull_wing:.1f}（{wr:.1f}×）",
            f"近月 put 净建仓 +{fa.net_put_doi:,} 手",
        ]
        if _atm_conf(False):
            reasons.append(f"波动率面追认：价跌 {fa.vol.d_spot_pct:+.1f}% 且 ATM IV {fa.vol.d_atm_pp:+.2f}pp（恐慌买保护）")
        if _skew_conf(False):
            reasons.append(f"skew 向 put 倾斜：25Δ(put−call) {fa.vol.d_skew25_pp:+.2f}pp（put 相对变贵＝抢保护）")
        return StrongSignal("看跌", "极强" if vc else "强", round(pr, 1), round(wr, 1),
                            fa.net_put_doi, vc, reasons, _diverges("看跌"), outlook_bias,
                            mid_bias, "多" in (mid_bias or ""), _lowc)

    return None


def wing_weights_oi(fa: "FlowAnalysis") -> tuple[float, float]:
    """主翼方向权重的【按 ΔOI 加权】版本 (看涨, 看跌)。

    ⚠️ 与 wing_weights 的区别，是理解"主翼买卖比 4×"到底是什么的关键：
    wing_weights 累加的是每一行的分类系数 c.weight —— 50 手的一行和 50,000 手的一行
    贡献几乎相同，所以那个比值本质是【被分到某方向的行权价条数/强度之比】，
    不含任何规模含义。这里用 |ΔOI|×weight 给出真正带规模的版本。

    **不参与任何判定**，只供台账记录，日后回答"该用哪个口径当闸门"。
    """
    def _w(kind: str, bias: str) -> float:
        return sum(abs(c.d_oi) * c.weight for c in fa.changes
                   if c.kind == kind and c.bias == bias and c.d_oi > 0
                   and STRONG_WING_DELTA_LO <= abs(c.delta) <= STRONG_WING_DELTA_HI)
    return (_w("C", "bullish") + _w("P", "bullish"),
            _w("C", "bearish") + _w("P", "bearish"))


def concentration_stats(fa: "FlowAnalysis") -> dict:
    """规模 / 广度 / 集中度 / 删除稳健性 —— 四组 shadow 字段。**纯记录，不参与任何判定。**

    动机（2026-08-27 稳健性验证 + codex 建议）：实测发现方向判定极不稳健 ——
      · 随机删某一侧 20% 报价 → 方向翻转 42%
      · 方向由中位仅 6 条腿决定（Top1 占该侧 18%、Top3 占 41%、Top5 占 52%）
      · 最脆弱：gold 2026-08-25（259 条腿，Top1 占 70%，移除 1 条即翻向）

    但**所有稳健化手段都无效**（秩次 59% / 广度 58% / 份额封顶 59~61%，
    与不封顶的 59% 无差别；分位 winsorize P90 的 63% 是 7 选 1 的产物，
    而理论驱动的份额封顶针对同一机制却毫无改善 → 判为噪音）。
    ⇒ 不稳定是真的，但改聚合方式改善不了准确率：**信号本身弱，不只是脆弱**。

    因此正确做法不是删掉规模信息，而是把「规模」与「广度/稳定性」拆成两个证据轴：
    规模强但单点依赖 → 那不是"市场共识"，是**一笔大单**（鲸鱼异动），两者该分开说。

    这些字段只入台账，供样本攒够后做一次性、簇级、预注册的检验。
    """
    ch = [c for c in fa.changes if c.bias in ("bullish", "bearish")]
    if not ch:
        return {}
    bull = sorted((abs(c.d_oi) * c.weight for c in ch if c.bias == "bullish"), reverse=True)
    bear = sorted((abs(c.d_oi) * c.weight for c in ch if c.bias == "bearish"), reverse=True)
    if not bull or not bear:
        return {}
    sb, sr = sum(bull), sum(bear)
    win, gap = (bull if sb > sr else bear), abs(sb - sr)
    # 删除稳健性 k：需移除几条最大顺向腿才会翻向。k 越小 = 越依赖单点。
    acc = 0.0
    k = 0
    for v in win:
        acc += v
        k += 1
        if acc >= gap:
            break
    tot = sum(win) or 1.0
    return {
        # —— 规模轴 ——
        "size_bull": round(sb, 1), "size_bear": round(sr, 1),
        "size_ratio": round(max(sb, sr) / max(min(sb, sr), 1.0), 2),
        # —— 广度轴（腿数，与规模无关）——
        "breadth_bull": len(bull), "breadth_bear": len(bear),
        "breadth_ratio": round(max(len(bull), len(bear))
                               / max(min(len(bull), len(bear)), 1), 2),
        # —— 集中度：优势侧被前 N 条腿占多少 ——
        "top1_share": round(win[0] / tot, 3),
        "top3_share": round(sum(win[:3]) / tot, 3),
        "top5_share": round(sum(win[:5]) / tot, 3),
        # —— 删除稳健性 ——
        "flip_k": k, "n_legs": len(ch),
        # 规模强但集中度高 = 一笔大单，不是市场共识。仅作标记，不改判定。
        "whale_like": bool(k <= 2 and win[0] / tot >= 0.35),
    }


def probe_strong_signal(fa: "FlowAnalysis") -> dict:
    """台账用：强信号各分量的原始数值 + 逐条闸门通过情况。**不下结论。**

    与 detect_strong_signal 的分工——那个回答"要不要置顶告警"，这个回答"当时到底是
    什么数"。台账要能回答"闸门该不该这么设"，就必须把【被闸门拦下的】一起记下来，
    否则永远没有反事实样本，闸门阈值就只能靠讲故事去调（2026-08-27 差点因此改错）。

    共用 wing_weights 与 fa 的压力字段，不另起一套实现。
    """
    bull_wing, bear_wing = wing_weights(fa)
    bwo, bearwo = wing_weights_oi(fa)
    up, dn = fa.upside_pressure, fa.downside_pressure
    vs = fa.vol if (fa.vol and fa.vol.prev) else None
    nc, npu = fa.net_call_doi, fa.net_put_doi
    _c = getattr(fa, "call", None)
    out: dict = {
        "up_pressure": round(up, 1), "dn_pressure": round(dn, 1),
        "bull_wing": round(bull_wing, 2), "bear_wing": round(bear_wing, 2),
        "net_call_doi": nc, "net_put_doi": npu,
        # 主翼的两个口径：行数权重 vs |ΔOI| 加权。闸门用的是前者（无规模含义）。
        "bull_wing_oi": round(bwo, 1), "bear_wing_oi": round(bearwo, 1),
        # churning 折减系数：压力已被它缩放，但主翼门与规模门【没有】同步折减。
        "churn_call": getattr(fa, "churn_call", None),
        "churn_put": getattr(fa, "churn_put", None),
        # —— 方向裁决与弃权（含类型与理由），入台账供日后校准阈值 ——
        # 现在所有软弃权阈值都未校准：实测覆盖率/正确率权衡里没有任何门槛的
        # Wilson 95% 下界超过 50%。攒够样本才谈得上定值。
        # ⚠️ 用 getattr 兜底：probe 是【记录器】，任何字段缺失都只能记成 None，
        # 绝不能因此抛异常拖垮当日报告或台账写入。
        "call_direction": (getattr(_c, "direction", "") or None),
        "call_abstain": (bool(getattr(_c, "abstain", True)) if _c is not None else None),
        "call_hard_abstain": (bool(getattr(_c, "hard", False)) if _c is not None else None),
        "call_ratio": getattr(_c, "ratio", None),
        "call_reason": ((getattr(_c, "reasons", None) or [""])[0][:120]) if _c is not None else None,
        # 全部理由 + 校准标记：只存第一条会丢掉"未经校准"提示（它常在第二条）
        "call_reasons": [r[:160] for r in (getattr(_c, "reasons", None) or [])] if _c is not None else None,
        "call_calibrated": bool(getattr(_c, "calibrated", False)) if _c is not None else None,
        # 规模/广度/集中度/删除稳健性四轴（shadow，纯记录不判定）
        **concentration_stats(fa),
        # 观测型方向敞口，与推断型的 pressure 并列记录，供日后比较谁更有预测力
        "net_delta_call": getattr(fa, "net_delta_call", None),
        "net_delta_put": getattr(fa, "net_delta_put", None),
        "net_delta_total": getattr(fa, "net_delta_total", None),
        # ⚠️ net_*_doi 实为【增仓总额】(analyze_flow 里只累加 d_oi>0)，不是净额。
        # 建仓比 = 两侧增仓总额之比。规模闸门只查信号侧绝对量、从不与另一侧比，
        # 所以"名为一边倒、实则两边都在建"完全可能通过——记下来供日后检验。
        "oi_build_ratio": round(max(nc, npu) / max(min(nc, npu), 1), 2),
        "d_spot_pct": round(vs.d_spot_pct, 3) if vs else None,
        "d_atm_pp": round(vs.d_atm_pp, 3) if vs else None,
        "d_skew25_pp": round(vs.d_skew25_pp, 3) if vs else None,
    }
    for direction in ("看涨", "看跌"):
        up_side = direction == "看涨"
        fwd, rev = (up, dn) if up_side else (dn, up)
        wf, wr = (bull_wing, bear_wing) if up_side else (bear_wing, bull_wing)
        doi = nc if up_side else npu
        dsp = out["d_spot_pct"]
        adverse = (-dsp if up_side else dsp) if dsp is not None else None
        out[direction] = {
            # ⚠️ 连续比值必须和布尔一起记：只存"过没过 3×"，日后永远无法回答
            # "3× 这个数对不对"——画不出阈值与结果的关系曲线。
            "pressure_ratio": round(fwd / max(rev, 1.0), 3),
            "wing_ratio": round(wf / max(wr, 0.5), 3),
            "wing_abs": round(wf, 2),
            "scale_doi": doi,
            "pressure_ok": fwd >= STRONG_PRESSURE_RATIO * max(rev, 1.0),
            "wing_ok": wf >= STRONG_WING_RATIO * max(wr, 0.5) and wf >= STRONG_MIN_WING_W,
            "scale_ok": doi >= STRONG_MIN_NET_DOI,
            # 逆向闸门余量：正=离触发还差多少 pp（越小越险），负=已被抑制。
            # 2026-08-27 QQQ 以 +0.010 擦过 —— 记下余量才能事后问"1.5 这个数对不对"。
            "contra_margin": (round(STRONG_CONTRA_SPOT - adverse, 3)
                              if adverse is not None else None),
        }
    return out


def _px_fmt(fa: "FlowAnalysis", conv):
    """速读拼句用的行权价格式器。

    ⚠️ 行权价必须【先报期权自己的行权价】，换算价放括号里。
    起因（用户 2026-08-28）：QQQ 速读里写「put 端 26,970 买方保护 +9,395 手」——
    26,970 是换算后的 NQ 点位，可用户买卖的是 QQQ 655P。报告报了一个
    在交易软件里根本搜不到的数字，等于没说。
    """
    def _one(v: float, kind: str = "") -> str:
        return (f"{v:,.0f}" if v >= 500 else f"{v:,.1f}") + kind

    if conv is None:
        return _one

    def _fmt(v: float, kind: str = "") -> str:
        cv = conv(v)
        # 换算值与原值几乎相同（无代理换算）时不重复啰嗦
        if abs(cv - v) < max(0.01, abs(v) * 0.005):
            return _one(v) + kind
        return f"{_one(v)}{kind}（≈{cv:,.0f}）"
    return _fmt


def counter_signals(fa: "FlowAnalysis", direction: str, *,
                    conv=None, top_n: int = 2) -> list[str]:
    """与研判方向相反的最强 ΔOI 信号（对手盘警示素材，确定性挑选）。

    direction 含"空"→ 找 bullish 异动；含"多"→ 找 bearish；其余（观望/中性）不出。
    只取结构级（|ΔOI| ≥ MOVE_MIN_DOI）；changes 已按 |ΔOI| 降序，天然取最强。
    """
    if not fa.changes:
        return []
    if "空" in direction:
        want = "bullish"
    elif "多" in direction:
        want = "bearish"
    else:
        return []
    fmt = _px_fmt(fa, conv)
    out: list[str] = []
    for c in fa.changes:
        if c.bias != want or abs(c.d_oi) < MOVE_MIN_DOI:
            continue
        if "保护" in c.spread_note or "长腿" in c.spread_note:
            continue    # 价差保护腿的方向已被扣除，不再当独立对手盘
        wall = f"（{c.on_wall}）" if c.on_wall else ""
        sp = f"，{c.spread_note}" if c.spread_note else ""
        out.append(f"{fmt(c.strike, c.kind)}{wall} "
                   f"{c.judgment}（{c.d_oi:+,} 手{sp}）")
        if len(out) >= top_n:
            break
    return out


def structural_moves(fa: "FlowAnalysis", *, conv=None, top_n: int = 2) -> list[str]:
    """从两日 ΔOI 里挑最结构性的动作，拼成速读用短句（确定性拼句，无新判断）。

    优先级：墙体滚动（同类相邻行权价一减一增 = 防线/压制位平移，如 360P→355P）
    > 墙上大额增减 > 最大单点建仓。conv 把 ETF 行权价换算成展示口径（商品价）。
    """
    if not fa.changes:
        return []
    fmt = _px_fmt(fa, conv)
    moves: list[str] = []
    used: set[tuple[float, str]] = set()

    # 1) 墙体滚动：一减一增、行权价相邻、规模相当（changes 已按 |ΔOI| 降序）
    big = [c for c in fa.changes if abs(c.d_oi) >= MOVE_MIN_DOI]
    for dn in (c for c in big if c.d_oi < 0):
        if (dn.strike, dn.kind) in used:
            continue
        for up in (c for c in big if c.d_oi > 0 and c.kind == dn.kind
                   and (c.strike, c.kind) not in used):
            gap = abs(up.strike - dn.strike)
            if not (0 < gap <= dn.strike * ROLL_MAX_GAP_FRAC):
                continue
            lo, hi = sorted((abs(dn.d_oi), up.d_oi))
            if lo < hi * ROLL_BALANCE:
                continue
            # 双腿各带买卖方判定入句（机构口径：谁在撤、谁在进），结尾给净方向
            def _leg(c: FlowChange) -> str:
                j = c.judgment
                if j == "噪音" or "减仓" in j:      # 无 IV 方向信息时只描述 OI 动作
                    j = "增仓" if c.d_oi > 0 else "减仓"
                wall = f"（{c.on_wall}）" if c.on_wall else ""
                return f"{fmt(c.strike, c.kind)}{wall} {j}（{c.d_oi:+,} 手）"

            # 净方向只看有 IV 信息的腿（中性/减仓腿不投票）；同向即明说资本方向
            sides = {b for b in (dn.bias, up.bias) if b != "neutral"}
            if sides == {"bearish"}:
                concl = "资本更看跌"
            elif sides == {"bullish"}:
                concl = "资本更看多"
            else:  # 两腿判定对立或全无信息：退回仓位平移的位置学描述
                buyer = "买方" in up.judgment
                if dn.kind == "P":
                    concl = (("看跌目标下探" if buyer else "支撑防线后撤")
                             if up.strike < dn.strike else
                             ("看跌/保护重心上移" if buyer else "承接位上移"))
                else:
                    concl = (("买方目标下移" if buyer else "压制位下压")
                             if up.strike < dn.strike else
                             ("上方目标上移" if buyer else "压制位上移"))
            side = "put 端" if dn.kind == "P" else "call 端"
            moves.append(f"{side} {_leg(dn)}、{_leg(up)}——{concl}")
            used.update({(dn.strike, dn.kind), (up.strike, up.kind)})
            break

    # 2) 墙上大额增减
    for c in fa.changes:
        if (c.strike, c.kind) in used or not c.on_wall or abs(c.d_oi) < MOVE_MIN_DOI:
            continue
        note = ("增厚，" + ("天花板更结实" if c.on_wall == "call墙" else "承接更结实")
                ) if c.d_oi > 0 else ("被削，" + ("压制松动" if c.on_wall == "call墙"
                                                  else "承接减弱"))
        moves.append(f"{c.on_wall} {fmt(c.strike, c.kind)} {note}（ΔOI {c.d_oi:+,} 手）")
        used.add((c.strike, c.kind))

    # 3) 最大单点建仓（带买卖方判定），补足条数
    for c in fa.changes:
        if len(moves) >= top_n:
            break
        if (c.strike, c.kind) in used or abs(c.d_oi) < MOVE_MIN_DOI:
            continue
        act = "新增" if c.d_oi > 0 else "减仓"
        moves.append(f"{fmt(c.strike, c.kind)} {act} {abs(c.d_oi):,} 手（{c.judgment}）")
        used.add((c.strike, c.kind))

    return moves[:top_n]


def _yearfrac(expiry: date, today: date) -> float:
    return (expiry - today).days / 365.0


def _live(snap: OptionsSnapshot, today: date, horizon_days: int,
          band_spot: float | None = None) -> list[OptionContract]:
    """近月窗口 + 近价带过滤。band_spot：近价带的锚定现价（默认用 snap 自身 spot）。
    做两日 diff 时，昨日链必须用【今日】spot 锚定近价带——否则现价移动后刚进带的
    行权价在昨日基线里缺失，全量存量 OI 会被误判成"单日新建"（行权价维度的窗口
    伪影，同 R8b 到期滚落一族；实例：SLV 60C 存量 134,701 手被整体误报为新建买方）。"""
    if snap.spot <= 0:
        return []
    anchor = band_spot if band_spot and band_spot > 0 else snap.spot
    lo, hi = anchor * (1 - NEAR_MONEY_BAND), anchor * (1 + NEAR_MONEY_BAND)
    out = []
    for c in snap.contracts:
        T = _yearfrac(c.expiry, today)
        if 0 < T <= horizon_days / 365.0 and lo <= c.strike <= hi:
            out.append(c)
    return out


def _lin_slope(pts: list[tuple[float, float]]) -> float:
    """最小二乘斜率 dY/dX（用于估期权偏斜 ∂IV/∂K）。"""
    n = len(pts)
    if n < 2:
        return 0.0
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den <= 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in pts)
    return num / den


def scan_unusual(snap: OptionsSnapshot, *, today: date,
                 horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[UnusualContract]:
    spot = snap.spot
    out: list[UnusualContract] = []
    for c in _live(snap, today, horizon_days):
        if c.volume < UNUSUAL_MIN_VOLUME:
            continue
        ratio = c.volume / c.open_interest if c.open_interest > 0 else float("inf")
        if ratio < UNUSUAL_MIN_VOL_OI:
            continue
        kind_cn = "看跌put" if c.kind == "P" else "看涨call"
        fresh = "量≫OI(疑全新建仓)" if ratio >= 1.0 else "量/OI偏高"
        out.append(UnusualContract(
            expiry=c.expiry, strike=c.strike, kind=c.kind,
            open_interest=c.open_interest, volume=c.volume, iv=c.iv, delta=c.delta,
            vol_oi_ratio=ratio, moneyness=(c.strike / spot - 1.0) if spot else 0.0,
            note=f"{kind_cn}·{fresh}",
        ))
    out.sort(key=lambda u: (u.expiry, -u.volume))
    return out[:TOP_N]


def _iv_at_delta_strict(pts, target):
    """在报价范围【内】插值；超界返回 None，**绝不复制端点**。

    ⚠️ 通用的 _interp 超界时取最近端点，用在固定 Delta 阶梯上会造成致命错觉：
    某到期若只有 [0.403, 0.838] 这一段报价（2026-08-25 GLD Call 实测，仅 16 个报价），
    六档 0.40~0.10 会**全部**落在范围外、全取同一个端点值 ——
    于是"六档同号"看起来像 6 份独立证据，实际是 1 份重复了 6 次。
    """
    if len(pts) < 2:
        return None
    lo = min(x for x, _ in pts)
    hi = max(x for x, _ in pts)
    if not (lo <= target <= hi):
        return None
    return _interp(pts, target)


def _surface_dirs(prev_live, curr_live) -> dict:
    """逐到期、逐 C/P 侧算【固定 Delta 曲面】的方向。返回 {(到期, C/P): +1/-1/0}。

    固定 Delta（而非固定行权价）比较，天然吸收现价移动带来的 moneyness 漂移，
    因此**不需要机械项修正、也就不会被"两次扣减"翻转符号**——这正是它能在
    2026-08-19 那种"现价 +2.68%、ATM IV 齐涨 +2.78pp"的日子读对方向的原因。

    ⚠️ 方向必须取【该侧相对 ATM 的重定价】，不能取绝对涨跌。
    2026-08-18 实测：Call 六档 -1.15~-1.73pp、Put 六档 -0.80~-0.91pp、ATM -0.92pp
    —— 两侧绝对值都在跌，用绝对方向会把"put 跌得少"误判成 put 卖方做支撑(看多)，
    与作者当天"Call 端重新建卖墙、短期防守"完全相反。
    减去 ATM 之后：Call 相对 -0.23~-0.81（真卖压）、Put 相对 +0.01~+0.12（防御保留），
    才与作者的表一致。这也正是机构那张"固定 Delta 相对 IV 变化"表的读法。

    判"方向明确"要同时满足：六档中至少 SURF_GATE_CONSISTENT 档同号，
    且净变动 ≥ SURF_GATE_PP。宁可判 0（不设闸）也不拿噪音当曲面。
    """
    LADDER = (0.40, 0.30, 0.25, 0.20, 0.15, 0.10)

    def by_exp(cs):
        m: dict = {}
        for c in cs:
            if c.iv > 0 and VOL_MIN_ABS_DELTA <= abs(c.delta) <= VOL_MAX_ABS_DELTA:
                m.setdefault((c.expiry, c.kind), []).append((abs(c.delta), c.iv))
        return m

    P, C = by_exp(prev_live), by_exp(curr_live)

    def atm_shift(exp) -> float | None:
        """该到期整体的 IV 位移：取 C/P 两侧 40Δ 档变动的均值作 ATM 代理。"""
        vals = []
        for kind in ("C", "P"):
            k = (exp, kind)
            if k in P and k in C and len(P[k]) >= VOL_MIN_QUOTES and len(C[k]) >= VOL_MIN_QUOTES:
                a, b = _iv_at_delta_strict(P[k], 0.40), _iv_at_delta_strict(C[k], 0.40)
                if a and b:
                    vals.append((b - a) * 100.0)
        return sum(vals) / len(vals) if vals else None

    out: dict = {}
    shifts = {}
    for exp in {e for e, _ in set(P) & set(C)}:
        shifts[exp] = atm_shift(exp)
    for k in set(P) & set(C):
        if len(P[k]) < VOL_MIN_QUOTES or len(C[k]) < VOL_MIN_QUOTES:
            continue
        base = shifts.get(k[0])
        if base is None:
            continue
        diffs = []
        for d in LADDER:
            a, b = _iv_at_delta_strict(P[k], d), _iv_at_delta_strict(C[k], d)
            if a and b:
                # 减去该到期的整体 IV 位移 → 该侧【相对】重定价
                diffs.append((b - a) * 100.0 - base)
        if len(diffs) < 4:
            continue
        pos = sum(1 for x in diffs if x > 0)
        neg = len(diffs) - pos
        net = sum(diffs) / len(diffs)
        if max(pos, neg) >= SURF_GATE_CONSISTENT and abs(net) >= SURF_GATE_PP:
            out[k] = 1 if net > 0 else -1
    return out


def _judge(kind: str, d_oi: int, adj_pp: float, prev_known: bool,
           d_iv_abs_pp: float = 0.0, surf: int = 0) -> tuple[str, str, float]:
    """按 持仓C/P × OI增减 × Delta修正IV方向 判定买卖方。
    返回 (粗方向 bearish/bullish/neutral, 细判中文, 聚合权重系数 0~1)。

    复刻机构口径：IV 升=买方抬价、IV 降=卖方供给；OI 增=建仓、OI 减=平仓/撤退。
    d_iv_abs_pp：该腿【绝对】ΔIV(未相对化，pp)。相对判定(adj_pp)与绝对方向矛盾且
    绝对变化显著时触发闸门——买方需 IV 升、卖方需 IV 降，若绝对方向相反=相对化产生的
    "随市"假信号(见 IV_ABS_GATE)，判存疑不投方向票。

    surf：该侧【固定 Delta 曲面】的方向（+1 该侧 IV 整体在涨 / -1 在跌 / 0 不明）。
    曲面方向明确而逐腿判定与之矛盾时，一律降级为存疑（权重 0）——**只否决、不反转**。
    见 SURF_GATE_PP 处的 2026-08-19 复盘：两次扣减会在大幅单边日把买盘整体翻成卖压，
    而固定 Delta 曲面同一天是对的。
    """
    if not prev_known:  # 无昨日 IV：无法判主动方，只按 OI 方向定性且降权
        if kind == "P":
            return ("bearish", "买方保护(新建·主动方未知)", 0.5) if d_oi > 0 else ("neutral", "看跌减仓", 0.0)
        return ("bullish", "买方(新建·主动方未知)", 0.5) if d_oi > 0 else ("neutral", "看涨减仓", 0.0)

    a = adj_pp
    if abs(a) < IV_NOISE:
        return "neutral", "噪音", 0.0
    # —— 曲面闸门：逐腿的"抬价/压价"结论不得与固定 Delta 曲面方向相反 ——
    # 只对【新建仓】设闸（减仓腿本就半权定性、不受相对化拖累），且只否决不反转。
    if surf and d_oi > 0 and (1 if a > 0 else -1) != surf:
        side = "抬价" if surf > 0 else "压价"
        return "neutral", f"与固定Delta曲面矛盾存疑(该侧整体在{side})", 0.0
    up_oi = d_oi > 0
    strong = abs(a) >= IV_STRONG
    mild = abs(a) < IV_MILD
    # 绝对 IV 闸门：相对判"买方"(需 IV 升)但绝对 IV 明显跌 / 相对判"卖方"(需 IV 降)但
    # 绝对 IV 明显涨 = 相对化在全市场 IV 齐落/齐涨里造出的假信号，仅对【新建仓(OI 增)】
    # 的方向票设闸(减仓/撤退本就半权定性、不受相对化拖累)。
    abs_sell = d_iv_abs_pp <= -IV_ABS_GATE   # 绝对在写权（IV 齐落）
    abs_buy = d_iv_abs_pp >= IV_ABS_GATE     # 绝对在抬价（IV 齐涨）
    if kind == "P":
        if a > 0:   # 买方抬 IV → 下方保护买盘（看跌）
            if up_oi:
                if abs_sell:  # 绝对 IV 却随市回落 → 非真买保护，只是相对抗跌
                    return "neutral", "保护买盘存疑(绝对IV随市回落)", 0.0
                return "bearish", ("买方轻微保护" if mild else "买方保护"), (0.6 if mild else 1.0)
            return "bearish", "卖方撤退", 0.5          # OI 降 + IV 升：支撑卖方退场，偏空
        else:       # 卖方压 IV → 写 put 做支撑（看多）
            if up_oi:
                if abs_buy:   # 绝对 IV 却随市抬升 → 非真写权支撑，只是相对抗涨
                    return "neutral", "支撑写权存疑(绝对IV随市抬升)", 0.0
                return "bullish", "卖方做支撑", 1.0
            return "bullish", "买方了结", 0.5
    else:  # CALL
        if a < 0:   # 卖方压 IV → 上方压制（看空）
            if up_oi:
                if abs_buy:   # 绝对 IV 却随市抬升 → 非真写权压制，只是相对抗涨
                    return "neutral", "压制写权存疑(绝对IV随市抬升)", 0.0
                lvl = "极强卖方压制" if strong else ("轻微卖方压制" if mild else "卖方压制")
                return "bearish", lvl, (1.3 if strong else (0.6 if mild else 1.0))
            return "bearish", "买方了结", 0.5
        else:       # 买方抬 IV → 上方突破买盘（看多）
            if up_oi:
                if abs_sell:  # 绝对 IV 却随市回落 → 非真买方追涨，只是相对抗跌
                    return "neutral", "买盘存疑(绝对IV随市回落)", 0.0
                return "bullish", ("轻微买方" if mild else "买方"), (0.6 if mild else 1.0)
            return "bullish", "卖方撤退", 0.5


def detect_spreads(changes: list[FlowChange]) -> list[Spread]:
    """检测疑似垂直价差：同 C/P、卖一腿 + 买一腿、量级相当、行权价相邻。

    复刻一次 WTI 识破案例——表面"上方大量买 Call"实为 Bear Call Spread 的保护腿，
    净头寸看短腿（卖 70C）的压制。垂直价差方向由"卖低买高/卖高买低"判定：
      Call: 卖低买高=熊市看涨(净空)；卖高买低=牛市看涨(净多)
      Put : 卖高买低=牛市看跌(净多)；卖低买高=熊市看跌(净空)
    """
    out: list[Spread] = []
    used: set = set()
    # —— 先识别【平仓中的价差】：两腿同到期、同 C/P、都在减仓、量级相当 ——
    # 平掉一个价差是方向中性的，但逐腿看会变成"卖方撤退(看涨) + 买方了结(看跌)"
    # 各投半票，净出一个假方向。2026-08-18 黄金实测：
    #   410C 昨OI 56,174→22,827 (ΔOI -33,347)、430C 昨OI 56,082→24,293 (-31,789)
    #   两腿昨日 OI 仅差 92 张、同为 9/04 到期 —— 明显是一笔约 5.6 万张的
    #   410/430 看涨价差平掉一半。旧版只认 d_oi>0 的建仓价差，完全看不见它，
    #   于是凑出 34,724 的假看涨压力，与作者当天"Call端重新建卖墙、短期防守"完全相反。
    for kind in ("C", "P"):
        closing = [c for c in changes if c.kind == kind and c.d_oi < 0]
        for a in sorted(closing, key=lambda x: x.d_oi):
            if (a.expiry, a.strike, kind) in used:
                continue
            mw = SPREAD_MAX_WIDTH_FRAC * a.strike
            cands = [b for b in closing
                     if (b.expiry, b.strike, kind) not in used
                     and b is not a and b.expiry == a.expiry
                     and 1e-6 < abs(b.strike - a.strike) <= mw
                     and 0.4 <= (b.d_oi / a.d_oi) <= 2.5
                     # 建仓时两腿 OI 必然接近（同一笔价差建起来的）
                     and a.prev_oi > 0 and 0.5 <= (b.prev_oi / a.prev_oi) <= 2.0]
            if not cands:
                continue
            b = min(cands, key=lambda x: abs(x.strike - a.strike))
            size = min(abs(a.d_oi), abs(b.d_oi))
            if size < SPREAD_MIN_SIZE:
                continue
            used.add((a.expiry, a.strike, kind)); used.add((b.expiry, b.strike, kind))
            lo, hi = sorted((a.strike, b.strike))
            out.append(Spread(
                kind=kind, expiry=a.expiry, name="价差平仓(中性)",
                short_strike=lo, long_strike=hi, size=size, net_bias="neutral",
                detail=f"{lo:.0f}/{hi:.0f}{kind} 两腿同步减仓各约 {size:,}"
                       f"（昨日 OI {a.prev_oi:,}/{b.prev_oi:,} 接近）→ 判为**平掉旧价差**，"
                       f"方向中性，两腿均不计方向票"))
    for kind in ("C", "P"):
        building = [c for c in changes if c.kind == kind and c.d_oi > 0]
        sellers = [c for c in building if "卖方" in c.judgment]
        buyers = [c for c in building if "买方" in c.judgment]
        for s in sorted(sellers, key=lambda x: -x.d_oi):
            if (s.expiry, s.strike, kind) in used:
                continue
            max_width = SPREAD_MAX_WIDTH_FRAC * s.strike
            cands = [b for b in buyers if (b.expiry, b.strike, kind) not in used
                     and b.expiry == s.expiry                          # 垂直价差必须同到期
                     and 1e-6 < abs(b.strike - s.strike) <= max_width  # 两腿相近
                     and 0.4 <= (b.d_oi / s.d_oi) <= 2.5]              # 量级相当
            if not cands:
                continue
            b = min(cands, key=lambda x: abs(x.strike - s.strike))
            size = min(s.d_oi, b.d_oi)
            if size < SPREAD_MIN_SIZE:   # 双腿都得明显高于噪音
                continue
            if kind == "C":
                name, net = ("熊市看涨价差(Bear Call)", "bearish") if s.strike < b.strike \
                    else ("牛市看涨价差(Bull Call)", "bullish")
            else:
                name, net = ("牛市看跌价差(Bull Put)", "bullish") if s.strike > b.strike \
                    else ("熊市看跌价差(Bear Put)", "bearish")
            used.add((s.expiry, s.strike, kind)); used.add((b.expiry, b.strike, kind))
            dir_cn = "看空" if net == "bearish" else "看多"
            out.append(Spread(
                kind=kind, expiry=s.expiry, name=name,
                short_strike=s.strike, long_strike=b.strike,
                size=size, net_bias=net,
                detail=f"卖 {s.strike:.0f}{kind} + 买 {b.strike:.0f}{kind}（各约 {size:,}）"
                       f" → {name}，净{dir_cn}（与净向相反的腿为封顶/保护，不计方向）",
            ))
    out.sort(key=lambda sp: -sp.size)
    return out[:SPREAD_TOP_N]   # 只报规模最大的几笔，宁缺勿滥


def _interp(pts: list[tuple[float, float]], x: float) -> float | None:
    """按 x 升序线性插值；x 超界取最近端点。pts 需 ≥2 个。"""
    if len(pts) < 2:
        return None
    pts = sorted(pts)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y0
    return None


def read_vol(snap: OptionsSnapshot, *, today: date,
             expiry: date | None = None) -> VolRead | None:
    """从单张快照读某个到期的 ATM IV 与 25Δ/10Δ 偏斜。

    到期选择：[VOL_MIN_DAYS, VOL_MAX_DAYS] 内、C/P 两侧有效报价都够数的到期里，
    取最接近 VOL_TARGET_DAYS 的那个（约一个月，机构口径里的"主力月份"口径）。
    传入 expiry 则强制用它（对比昨日时保证同一到期，才可比）。
    """
    if snap.spot <= 0:
        return None
    by_exp: dict[date, dict[str, list]] = {}
    for c in snap.contracts:
        d = (c.expiry - today).days
        if not (VOL_MIN_DAYS <= d <= VOL_MAX_DAYS):
            continue
        if c.iv <= 0 or not (VOL_MIN_ABS_DELTA <= abs(c.delta) <= VOL_MAX_ABS_DELTA):
            continue
        by_exp.setdefault(c.expiry, {"C": [], "P": []})[c.kind].append(c)
    if expiry is not None:
        cands = [expiry] if expiry in by_exp else []
    else:
        cands = [e for e, s in by_exp.items()
                 if len(s["C"]) >= VOL_MIN_QUOTES and len(s["P"]) >= VOL_MIN_QUOTES]
        cands.sort(key=lambda e: abs((e - today).days - VOL_TARGET_DAYS))
    if not cands:
        return None
    exp = cands[0]
    calls, puts = by_exp[exp]["C"], by_exp[exp]["P"]
    if len(calls) < 2 or len(puts) < 2:
        return None
    # ATM：C/P 各按行权价插值到现价，再取两侧均值（剔单侧报价噪音）
    atm_c = _interp([(c.strike, c.iv) for c in calls], snap.spot)
    atm_p = _interp([(c.strike, c.iv) for c in puts], snap.spot)
    atm = [v for v in (atm_c, atm_p) if v is not None]
    if not atm:
        return None
    # 偏斜：按 |Delta| 插值（比按行权价稳，自动适配现价移动）
    civ = [(abs(c.delta), c.iv) for c in calls]
    piv = [(abs(c.delta), c.iv) for c in puts]
    c25, p25 = _interp(civ, 0.25), _interp(piv, 0.25)
    c10, p10 = _interp(civ, 0.10), _interp(piv, 0.10)
    if None in (c25, p25, c10, p10):
        return None
    return VolRead(
        expiry=exp, days_out=(exp - today).days,
        atm_iv_pp=round(100.0 * sum(atm) / len(atm), 2),
        skew25_pp=round(100.0 * (p25 - c25), 2),
        skew10_pp=round(100.0 * (p10 - c10), 2),
    )


def _vol_verdict(d_spot_pct: float, d_atm: float, d_skew25: float) -> str:
    """机构口径：价格变动 × ATM IV 变动 × 偏斜收敛与否 → 期权端是否"确认"价格。"""
    if d_spot_pct >= D_SPOT_SIG:                    # 明显上涨
        if d_atm <= -D_ATM_SIG:
            base = "价涨而 ATM IV 被压 → 没有买方追价抢筹（涨势更像空头回补）"
            if d_skew25 <= -D_SKEW_SIG:
                return base + "；但偏斜明显收敛，下行担忧同步减退 → 信号中性"
            return base + "；且偏斜未明显收敛 → 期权端未确认涨势，后续动力存疑"
        if d_atm >= D_ATM_SIG:
            return "价涨且 ATM IV 抬升 → 买方追价，涨势获期权端确认"
        return "价涨、ATM IV 大体持平 → 期权端未表态"
    if d_spot_pct <= -D_SPOT_SIG:                   # 明显下跌
        if d_atm >= D_ATM_SIG:
            if d_skew25 >= D_SKEW_SIG:
                return "价跌且 ATM IV / put 偏斜齐升 → 恐慌买保护，下行动能获确认"
            return "价跌且 ATM IV 抬升 → 保护需求上升"
        if d_atm <= -D_ATM_SIG:
            return "价跌而 ATM IV 回落 → 恐慌有限，跌势未获期权端追认"
        return "价跌、ATM IV 大体持平 → 期权端未表态"
    # 价格大体持平：只看偏斜
    if d_skew25 >= D_SKEW_SIG:
        return "价平但 put 偏斜走陡 → 下行担忧升温"
    if d_skew25 <= -D_SKEW_SIG:
        return "价平且偏斜收敛 → 下行担忧减退"
    return "价平、波动率面无方向信息"


def vol_surface(prev: OptionsSnapshot | None, curr: OptionsSnapshot, *,
                today: date) -> VolSurface | None:
    """两日波动率面对比。prev 缺失时只给当日水平（明日起点亮判读）。"""
    cr = read_vol(curr, today=today)
    if cr is None:
        return None
    pr = read_vol(prev, today=today, expiry=cr.expiry) if prev is not None else None
    if pr is None:
        return VolSurface(curr=cr, prev=None, d_spot_pct=0.0,
                          verdict="仅当日水平（无可比昨日同到期快照），明日起可出日变化判读")
    d_spot = 100.0 * (curr.spot / prev.spot - 1.0) if prev.spot > 0 else 0.0
    verdict = _vol_verdict(d_spot, cr.atm_iv_pp - pr.atm_iv_pp, cr.skew25_pp - pr.skew25_pp)
    return VolSurface(curr=cr, prev=pr, d_spot_pct=round(d_spot, 2), verdict=verdict)


# ═══════════════════════════════════════════════════════════════════════════
# 【时序约定】—— 所有回测、台账、复盘都必须按此对齐，写错就是前视或错位
# ═══════════════════════════════════════════════════════════════════════════
#   快照文件日期 D
#     · 抓取时刻：D 当天【盘前】（ET 01:00~08:30；实测 8/19 08:27、8/20 08:00、
#       8/21 07:00。链内 asof 字段是 UTC，比 captured_at 晚约 4 小时，不是另一个时点）
#     · 其 OI：OCC 在 **D−1 收盘后**隔夜结算 → 反映 **D−1 收盘时**的持仓
#     · 其 spot：抓取时刻的延迟报价，介于 D−1 收盘与 D 盘前之间，**不精确等于任一收盘**
#
#   diff(D−1, D) = 从 D−2 收盘 到 D−1 收盘的持仓变化
#                = **D−1 那一整个交易日**的持仓变化
#
#   可交易时点：diff(D−1, D) 在 **D 开盘前**就能读到 → 可用于交易 **D 当天**
#
#   因此复盘打分必须是：
#     「快照对 (X, X+1) 描述交易日 X 的资金流；其可兑现结果是交易日 X+1 的走势」
#   拿交易日 X 自己的走势去评判，等于用信号发出后才知道的信息 = 前视。
# ═══════════════════════════════════════════════════════════════════════════


# —— 相对化基准的三种【预注册】方案（供稳健性检验；默认 pooled = 现行口径）——
#   pooled   : 按到期取全体（C+P 合并）中位数 —— 保留 Put-Call skew 维度
#   side_mid : 按到期分别取 C 侧、P 侧中位数，再取两者中点 —— C/P 等权，
#              避免报价数多的一侧主导 pooled median
#   per_side : 按 (到期, C/P) 各自取中位数 —— **会消掉 skew 维度**（对照组）
# 预注册的意思：三种方案在看结果之前就定好，不许看完数据再加方案。
#
# ⚠️ 2026-08-27 稳健性验证结论（118 品种-日，**38 个日期簇**，按簇 block bootstrap）：
#       pooled 55.7% [44%,67%] / side_mid 55.2% [44%,66%] / per_side 54.8% [44%,65%]
#     **三者统计上无法区分。** 此前"按侧拆会消掉 skew、导致 54%→49%"的说法是逐行
#     统计的假象（把 118 行当 118 个独立样本，实际只有 38 簇），已撤回。
#     保留三方案不是为了将来挑一个，而是为了**记住它们没差别**、防止有人再去调它。
#
# 同日还验证了其它稳健化手段，**全部无效**：
#     秩次聚合 59% / 广度(腿数)聚合 58% / 脆弱度当置信度（反向，脆弱的反而更准）
#     份额封顶（单腿 ≤50%/35%/25%/15% 该侧总量）59~61%，与不封顶的 59% 无差别
#     分位 winsorize P90 看似 63%，但那是 7 个分位里挑的；**理论驱动的份额封顶
#     直接针对同一机制却毫无改善** → 判定 P90 为噪音，不采纳。
# 真实存在的问题是：随机删 20% 报价方向翻转 42%，方向由中位仅 6 条腿决定
# （Top1 占该侧 18%、Top3 占 41%）。但改聚合方式改善不了准确率 ——
# 说明信号本身弱，不只是脆弱。
REL_SCHEMES = ("pooled", "side_mid", "per_side")


def analyze_flow(
    prev: OptionsSnapshot | None,
    curr: OptionsSnapshot,
    *,
    today: date,
    rel_scheme: str = "pooled",
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    call_wall: float | None = None,
    put_wall: float | None = None,
    prev_date: str | None = None,
    curr_date: str | None = None,
) -> FlowAnalysis:
    spot = curr.spot
    live = _live(curr, today, horizon_days)
    f_call = f_put = 1.0     # 无 prev（单快照）时不折减，见下方 churning 段
    # 单快照时没有日对日 diff → 硬弃权（逻辑约束，不是阈值问题）
    from undertow.analyze.direction import decide as _decide0
    call = _decide0(up_pressure=0.0, dn_pressure=0.0, has_prev=False)

    unusual = scan_unusual(curr, today=today, horizon_days=horizon_days)
    tcv = sum(c.volume for c in live if c.kind == "C")
    tpv = sum(c.volume for c in live if c.kind == "P")

    changes: list[FlowChange] = []
    spreads: list[Spread] = []
    net_call = net_put = 0
    downside = upside = 0.0
    # churning 折减用的分 kind 桶：方向压力按 call/put 拆分，便于按各自转化率缩放
    up_call = dn_call = up_put = dn_put = 0.0
    gabs_call = gabs_put = 0        # 毛 |ΔOI| 总额
    nsig_call = nsig_put = 0        # 带符号净 ΔOI（含减仓，用于识破 churning）
    tilt = "—（仅一份快照，明天起可出 ΔOI/ΔIV 与买卖方判定）"

    if prev is not None:
        d_spot = spot - prev.spot
        prev_live = _live(prev, today, horizon_days, band_spot=spot)
        # 当前链的偏斜 ∂IV/∂K，用于 Delta 修正。
        # ⚠️ **必须逐到期拟合**：不同到期的偏斜陡峭度不同（近月更陡），
        # 跨期混合拟合出的斜率对任何单一到期都不成立，会让"机械项"扣错。
        slopes: dict[tuple, float] = {}
        by_exp_kind: dict[tuple, list] = {}
        for c in live:
            if c.iv > 0:
                by_exp_kind.setdefault((c.expiry, c.kind), []).append((c.strike, c.iv))
        for k, pts in by_exp_kind.items():
            slopes[k] = _lin_slope(pts)
        # 跨期兜底（某到期报价太少时用）
        put_slope = _lin_slope([(c.strike, c.iv) for c in live if c.kind == "P" and c.iv > 0])
        call_slope = _lin_slope([(c.strike, c.iv) for c in live if c.kind == "C" and c.iv > 0])

        def _agg(contracts):
            """按 **(到期, 行权价, C/P)** 合并同一合约的多条记录。

            ⚠️ 主键必须含到期。旧版按 (行权价, C/P) 把 60 天窗口内所有月份合成一条，
            后果（2026-08-27 codex review 查出）：
              · IV 跨期按 OI 加权平均后再做日差 → ΔIV 可能纯粹来自**期限权重变化**，
                与任何真实的定价变动无关；
              · 换月（近月平、远月开）在同一个桶里互相抵消，ΔOI 失真；
              · 价差识别拿到的"同一到期"其实是主导到期，可能配出伪垂直价差。
            污染会传导到 pressure / 强信号 / 结构读数 / 墙位增量 / 台账 —— 即全部下游。
            机构口径始终分月份看（近月/次月各一张表）。
            """
            m: dict[tuple, dict] = {}
            for c in contracts:
                k = (c.expiry, c.strike, c.kind)
                a = m.setdefault(k, {"oi": 0, "ivw": 0.0, "dlw": 0.0, "vol": 0})
                a["oi"] += c.open_interest
                if c.iv > 0:
                    a["ivw"] += c.iv * c.open_interest
                a["dlw"] += c.delta * c.open_interest
                a["vol"] += c.volume
            return m

        cagg, pagg = _agg(live), _agg(prev_live)
        # 固定 Delta 曲面方向（逐到期逐侧），用于否决与之矛盾的逐腿判定
        surf_dirs = _surface_dirs(prev_live, live)
        # 第一遍：每个行权价的 Delta 修正 ΔIV（尚未相对化）
        rows = []
        for (expiry, strike, kind), a in cagg.items():
            coi = a["oi"]
            if coi <= 0:
                continue
            cdelta = a["dlw"] / coi
            if abs(cdelta) > MAX_ABS_DELTA:   # 深 ITM，IV 不可靠
                continue
            civ = a["ivw"] / coi if a["ivw"] > 0 else 0.0
            p = pagg.get((expiry, strike, kind))
            poi = p["oi"] if p else 0
            piv = (p["ivw"] / p["oi"]) if (p and p["oi"] > 0 and p["ivw"] > 0) else 0.0
            d_oi = coi - poi
            if abs(d_oi) < MIN_DOI:
                continue
            prev_known = bool(p and piv > 0 and civ > 0)
            d_iv = (civ - piv) if prev_known else 0.0
            # 该到期自己的偏斜斜率；报价太少时回落到跨期斜率
            slope = slopes.get((expiry, kind))
            if slope is None:
                slope = put_slope if kind == "P" else call_slope
            # 机械项：现价动 ΔS 后固定行权价沿偏斜'继承'的 IV ≈ -slope×ΔS
            # （sticky-moneyness 近似），去除机械项 = d_iv - (-slope×ΔS)
            corrected = (d_iv + slope * d_spot) if prev_known else 0.0
            rows.append({"strike": strike, "kind": kind, "coi": coi, "poi": poi, "d_oi": d_oi,
                         "delta": cdelta, "civ": civ, "piv": piv, "d_iv": d_iv,
                         "corrected": corrected, "known": prev_known, "vol": a["vol"],
                         "exp": expiry})

        # "相对化"基准：减去中位修正 ΔIV，剔除全市场 vol 平移。
        # ⚠️ **按【到期】分组，但【不能按 C/P 侧】再分**。
        # · 按到期分组是必须的：不同到期期限结构不同，跨期混算的基准对谁都不成立
        #   （"跨期混算"根 bug 的最后一处残留，codex review 2026-08-27 指出）。
        # · 但【不能】再按 C/P 拆：那样每条腿只和"同到期同侧"比，
        #   **Put-Call skew 这个维度就被整个消掉了** —— 而 skew 恰恰是方向信息的
        #   主要载体（机构口径的核心就是 Put-Call skew）。
        #   实测：按 (到期,侧) 分组后全样本正确率 54%→49%、silver 55%→36%、
        #   黄金 8/19「强烈转多」由"偏多"退回"分歧"。只按到期分组则不会。
        # 分组样本不足 REL_MIN_STRIKES 时回落到全局中位（聊胜于无，并如实降级）。
        from collections import defaultdict as _dd
        if rel_scheme not in REL_SCHEMES:
            raise ValueError(f"rel_scheme 须为 {REL_SCHEMES} 之一，收到 {rel_scheme!r}")
        _grp = _dd(list)
        _grp_side = _dd(list)
        for r in rows:
            if r["known"]:
                _grp[r["exp"]].append(r["corrected"])          # 只按到期，保留 C/P 对比
                _grp_side[(r["exp"], r["kind"])].append(r["corrected"])
        def _median(xs):
            """真中位数。⚠️ 偶数长度必须取中间两个的均值。
            旧版写的是 xs[len(xs)//2]，对偶数长度系统性偏高 —— 当 call/put 两侧
            恰好对称分裂时（如各半、幅度相反），它会取到上半支的最小值，
            使一侧相对值整体归零、另一侧被翻倍，skew 读数被扭曲。"""
            n = len(xs)
            return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0)

        _all = sorted(r["corrected"] for r in rows if r["known"])
        ref_all = _median(_all) if len(_all) >= REL_MIN_STRIKES else 0.0
        refs, refs_side = {}, {}
        for k, vs_ in _grp.items():
            vs_ = sorted(vs_)
            refs[k] = _median(vs_) if len(vs_) >= REL_MIN_STRIKES else ref_all
        for k, vs_ in _grp_side.items():
            vs_ = sorted(vs_)
            refs_side[k] = _median(vs_) if len(vs_) >= REL_MIN_STRIKES else None
        if rel_scheme == "side_mid":
            # C/P 等权：两侧中位数的中点，避免报价多的一侧主导
            for e in list(refs):
                a, b = refs_side.get((e, "C")), refs_side.get((e, "P"))
                if a is not None and b is not None:
                    refs[e] = (a + b) / 2.0

        for r in rows:
            if rel_scheme == "per_side":
                ref = refs_side.get((r["exp"], r["kind"]))
                if ref is None:
                    ref = refs.get(r["exp"], ref_all)
            else:
                ref = refs.get(r["exp"], ref_all)
            adj_iv_pp = ((r["corrected"] - ref) * 100.0) if r["known"] else 0.0
            d_iv_pp = r["d_iv"] * 100.0
            # 闸门用【Delta 修正后的绝对】ΔIV(corrected*100)——剔除现价移动沿偏斜的机械项、
            # 但保留全市场 vol 整体位移(正是要闸门看见的"齐落/齐涨")，比生 d_iv 更稳。
            abs_iv_pp = (r["corrected"] * 100.0) if r["known"] else 0.0
            bias, judgment, w = _judge(r["kind"], r["d_oi"], adj_iv_pp, r["known"], abs_iv_pp,
                                       surf_dirs.get((r["exp"], r["kind"]), 0))
            on_wall = ""
            if call_wall is not None and abs(r["strike"] - call_wall) < 1e-6:
                on_wall = "call墙"
            elif put_wall is not None and abs(r["strike"] - put_wall) < 1e-6:
                on_wall = "put墙"
            note = judgment if r["poi"] > 0 else "【昨日无此行】" + judgment
            changes.append(FlowChange(
                expiry=r["exp"], strike=r["strike"], kind=r["kind"],
                prev_oi=r["poi"], curr_oi=r["coi"], d_oi=r["d_oi"], delta=r["delta"],
                prev_iv=r["piv"], curr_iv=r["civ"], d_iv_pp=d_iv_pp, adj_iv_pp=adj_iv_pp,
                curr_volume=r["vol"], moneyness=(r["strike"] / spot - 1.0) if spot else 0.0,
                bias=bias, judgment=judgment, on_wall=on_wall, note=note, weight=w,
            ))
            if r["d_oi"] > 0:  # kind 求和（向后兼容字段）
                if r["kind"] == "C":
                    net_call += r["d_oi"]
                else:
                    net_put += r["d_oi"]
            # churning 度量：分 kind 累加带符号净额与毛 |ΔOI| 总额
            if r["kind"] == "C":
                nsig_call += r["d_oi"]; gabs_call += abs(r["d_oi"])
            else:
                nsig_put += r["d_oi"]; gabs_put += abs(r["d_oi"])
            mag = abs(r["d_oi"]) * w
            if bias == "bearish":
                downside += mag
                if r["kind"] == "C": dn_call += mag
                else: dn_put += mag
            elif bias == "bullish":
                upside += mag
                if r["kind"] == "C": up_call += mag
                else: up_put += mag

        changes.sort(key=lambda x: -abs(x.d_oi))

        # —— 价差结构识别：扣除"封顶/保护腿"的方向压力，避免把价差误读为方向 ——
        spreads = detect_spreads(changes)
        if spreads:
            labels: dict[tuple, str] = {}
            for sp in spreads:
                for c in changes:
                    if c.kind != sp.kind or c.expiry != sp.expiry:
                        continue
                    is_short = abs(c.strike - sp.short_strike) < 1e-6
                    is_long = abs(c.strike - sp.long_strike) < 1e-6
                    if not (is_short or is_long):
                        continue
                    labels[(c.expiry, c.strike, c.kind)] = sp.name + ("·短腿(方向)" if is_short else "·长腿(保护)")
                    # 平仓价差：两腿都不计方向（平掉旧结构是中性动作）
                    if sp.net_bias == "neutral":
                        pp_ = min(abs(c.d_oi), sp.size) * c.weight
                        if c.bias == "bearish":
                            downside -= pp_
                            if c.kind == "C": dn_call -= pp_
                            else: dn_put -= pp_
                        elif c.bias == "bullish":
                            upside -= pp_
                            if c.kind == "C": up_call -= pp_
                            else: up_put -= pp_
                        continue
                    # 与净方向相反的腿 = 封顶腿，只扣【匹配数量】的压力
                    # （腿不等量时，未配对的剩余仓仍是方向仓）
                    if c.bias != sp.net_bias and c.bias in ("bearish", "bullish"):
                        p = min(abs(c.d_oi), sp.size) * c.weight
                        if c.bias == "bearish":
                            downside -= p
                            if c.kind == "C": dn_call -= p
                            else: dn_put -= p
                        else:
                            upside -= p
                            if c.kind == "C": up_call -= p
                            else: up_put -= p
            downside, upside = max(0.0, downside), max(0.0, upside)
            dn_call, dn_put = max(0.0, dn_call), max(0.0, dn_put)
            up_call, up_put = max(0.0, up_call), max(0.0, up_put)
            changes = [replace(c, spread_note=labels.get((c.expiry, c.strike, c.kind), "")) for c in changes]

        # —— churning 折减：按各 kind 的净额/毛额转化率缩放方向压力 ——
        # 机构口径：成交/毛增仓再大，若净 OI 几乎没动就是滚仓/结构调整，不是方向。
        def _churn_f(nsig: int, gabs: int) -> float:
            if gabs <= 0:
                return 1.0
            return min(1.0, (abs(nsig) / gabs) / TURNOVER_HEALTHY)
        f_call, f_put = _churn_f(nsig_call, gabs_call), _churn_f(nsig_put, gabs_put)
        downside = dn_call * f_call + dn_put * f_put
        upside = up_call * f_call + up_put * f_put
        cbits = []
        if f_call < 0.9 and gabs_call > 0:   # 阈值对齐折减：只要明显缩放就标注，别让数字悄悄变小
            cbits.append(f"call 端 churning 净 {nsig_call:+,}/毛 {gabs_call:,}→压力×{f_call:.2f}")
        if f_put < 0.9 and gabs_put > 0:
            cbits.append(f"put 端 churning 净 {nsig_put:+,}/毛 {gabs_put:,}→压力×{f_put:.2f}")
        cnote = ("；" + "、".join(cbits)) if cbits else ""

        # —— 方向裁决：统一走 direction.decide()，弃权是一等输出而非兜底 ——
        # pressure 是【推断】（先按 IV 判主动方），净有效 Delta 是【观测】（纯算术）。
        # 实测两者 60% 的日子方向相反。两者反向时，我们并不知道哪个对 ——
        # 此时**不许给方向票**，如实说"方向不明"。
        # ⚠️ 只撤销裁决权，不改任何计算：pressure 原值照常进 signal_ledger 累积，
        # 否则反事实样本作废、这层永远无法校准。
        from undertow.analyze.direction import decide as _decide
        _nd = round(sum(c.d_oi * c.delta for c in changes), 1)
        call = _decide(up_pressure=upside, dn_pressure=downside, net_delta=_nd,
                       has_prev=True, oi_changed=bool(changes),
                       trade_date=curr_date or "", today="")
        if call.abstain:
            tilt = f"方向不明（{call.reasons[0]}）{cnote}"
        elif call.direction == "偏空":
            tilt = (f"偏空（看跌加权增仓 {downside:,.0f} > 看涨 {upside:,.0f}；"
                    f"put 买保护/call 卖压制占优{cnote}）")
        else:
            tilt = (f"偏多（看涨加权增仓 {upside:,.0f} > 看跌 {downside:,.0f}；"
                    f"call 买盘/put 卖方做支撑占优{cnote}）")

    return FlowAnalysis(
        instrument=curr.instrument,
        proxy_symbol=curr.proxy_symbol,
        spot=spot,
        horizon_days=horizon_days,
        curr_date=curr_date or "",
        curr_asof=curr.asof,
        prev_date=prev_date,
        unusual=unusual,
        total_call_volume=tcv,
        total_put_volume=tpv,
        changes=changes,
        net_call_doi=net_call,
        net_put_doi=net_put,
        downside_pressure=round(downside, 1),
        upside_pressure=round(upside, 1),
        churn_call=round(f_call, 3),
        churn_put=round(f_put, 3),
        call=call,
        net_delta_call=round(sum(c.d_oi * c.delta for c in changes if c.kind == "C"), 1),
        net_delta_put=round(sum(c.d_oi * c.delta for c in changes if c.kind == "P"), 1),
        net_delta_total=round(sum(c.d_oi * c.delta for c in changes), 1),
        flow_tilt=tilt,
        spreads=spreads,
        call_wall=call_wall,
        put_wall=put_wall,
        vol=vol_surface(prev, curr, today=today),
    )


FLIP_DRIVER_BAND = 0.12       # 驱动分解的近价带（moneyness）
FLIP_DRIVER_MIN_DOI = 1_000   # 一条腿够格进叙事的 |ΣΔOI| 门槛


def flip_driver_summary(fa: "FlowAnalysis") -> str:
    """零伽马位移的驱动分解一句话（用户口径的叙事体）：

    近价带按【现价上方 put / 现价下方 put / 近价 call】三段聚合 ΔOI 与
    （|ΔOI| 加权的）**Delta 修正后相对 ΔIV**——与买卖方判定同一套去噪（R2），
    免被全市场 IV 齐涨齐落带偏。结论按分段模式组合，确定性拼句，无新判断。
    """
    if not fa.changes:
        return ""

    def agg(rows) -> tuple[int, float]:
        doi = sum(r.d_oi for r in rows)
        known = [r for r in rows if r.prev_iv > 0 and r.d_oi]
        w = sum(abs(r.d_oi) for r in known)
        div = sum(r.adj_iv_pp * abs(r.d_oi) for r in known) / w if w else 0.0
        return doi, div

    near = [c for c in fa.changes if abs(c.moneyness) <= FLIP_DRIVER_BAND]
    p_up = agg([c for c in near if c.kind == "P" and c.strike > fa.spot])
    p_dn = agg([c for c in near if c.kind == "P" and c.strike <= fa.spot])
    c_nr = agg([c for c in near if c.kind == "C"])

    def leg(name: str, doi: int, div: float) -> str | None:
        if abs(doi) < FLIP_DRIVER_MIN_DOI:
            return None
        act = "增仓" if doi > 0 else "减仓"
        ivw = ("相对 IV 走强" if div > 0.1 else
               ("相对 IV 回落" if div < -0.1 else "相对 IV 持平"))
        return f"{name}{act} {abs(doi):,} 手且{ivw}"

    bits = [b for b in (leg("现价上方 put ", *p_up), leg("现价下方 put ", *p_dn),
                        leg("近价 call ", *c_nr)) if b]
    if not bits:
        return ""

    # 分段结论组合（各段独立判定，拼在一起就是叙事）
    concls: list[str] = []
    if p_up[0] < -FLIP_DRIVER_MIN_DOI and p_up[1] <= 0.1:
        concls.append("现价上方的保护单在了结，恐慌保护退潮")
    elif p_up[0] > FLIP_DRIVER_MIN_DOI and p_up[1] > 0.1:
        concls.append("现价上方保护加码，下方风险重新加价")
    if c_nr[0] > FLIP_DRIVER_MIN_DOI:
        if c_nr[1] > 0.1:
            concls.append("近价 call 由买方推动，有多头资金谨慎入场")
        elif c_nr[1] < -0.1:
            concls.append("近价 call 由卖方写入——压制位前移，天花板下压")
        else:
            concls.append("近价 call 增仓但买卖方不明")
    elif c_nr[0] < -FLIP_DRIVER_MIN_DOI:
        concls.append("近价 call 撤退，看涨需求退潮")
    if p_dn[0] > FLIP_DRIVER_MIN_DOI:
        if p_dn[1] < -0.1:
            concls.append("下方 put 由卖方写入做支撑（承接位跟进）")
        elif p_dn[1] > 0.1:
            concls.append("下方买保护加深（下行风险仍被付费定价——空头押注或多头买保险，防御性动作）")
    concl = "；".join(concls) if concls else "多空双向换仓，期权端方向分歧"
    return "驱动分解：" + "、".join(bits) + " → " + concl
