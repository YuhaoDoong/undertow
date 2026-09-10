"""日历 / 对角价差策略子模块：「近月卖腿收租 + 远月买腿做发动机」的结构化核算。

定位（与 condor.py 同级的一个【独立策略子模块】，由 strategy_hub 统筹）：
  * 结构原型来自一套外部实盘 SOP（2026-09-09 经用户转述；**原始材料不入库**）：
    净卖方底盘、theta 主收入、买腿只当配件不表达观点、出场看关键位不看金额。
    那套 SOP 的骨架是对的，本模块把它落成确定性核算，并补上它缺的一环。
  * **他的「四件套」（分段斜率 · 甜点 · 上下沿 BE · 期初净现金流）全是价格轴上的量。**
    而这个结构的真实主敞口是 vega 与期限结构 —— 那套 SOP 有过一次真实亏损：
    商品期货近月合约逼空、远月不跟，**杀死它的是近远月脱钩，价格维度上并没算错**。
    所以本模块加了第五件：
    **近远月 IV 期限结构**，并把它做成【适配闸门】而不是一条参考读数。

结构的三个敞口（卖近月 + 买远月，同 kind）：
    theta  正  ← 近月衰减快于远月（∝ 1/√T），这是"主收入"
    vega   正  ← 远月 vega 远大于近月（∝ √T），这是**隐藏的主敞口**
    gamma  负  ← 近月 gamma 大，标的一动就吃亏
  前提是**近远月同步移动**。前提破了，"对冲"就变成两条各自亏损的腿。

本模块相对原 SOP 的三处改动，都是针对已知的失效模式：
  ① 期限结构闸门：近月 IV 必须比远月贵至少 TERM_EDGE_MIN(pp) 才适配。
     日历多头本质是"卖贵的近月、买便宜的远月"，若建仓时近远月倒挂，
     这个结构从第一天就是逆风的 —— 而原 SOP 里没有任何一步会拦下它。
  ② gamma 临界移动：给出"标的每天动多少就吃光当天 theta 收入"的幅度。
     原 SOP 完全没有 gamma 这个词，但它是负 gamma 结构的日常失血点。
  ③ 事件窗口的可观测定义：原 SOP 的「事件窗口不卖近月 call」若只按财报/非农这类
     日历事件定义，防不住产业性的现货挤仓。挤仓的前兆恰恰写在期限结构里
     （近月 IV 相对远月异常抬升），所以 ① 的读数同时兼任 ③ 的哨兵。

实证（2026-09-10，全部快照 386 个「日期×品种×侧」样本，GLD/SLV/USO/QQQ）：
  近月 IV − 远月 IV：最小 −5.21、**中位 −0.57**、最大 +16.57 pp
    > 0 的比例 38.3%；≥ TERM_EDGE_MIN 的比例 25.9%
  **正向期限结构（远月更贵）在美股 ETF 上是常态**，所以日历多头的窗口天然稀少：
    整体适配 21/386 = 5.4%   GLD 0.9% · SLV 8.7% · USO 10.0% · QQQ 0.0%
  适配样本高度聚集在事件期：USO 2026-07-15 的 +16.57pp、07-28 的 +7.77pp ——
  **而这些正是 TERM_EDGE_RICH 要报警的时刻**，不是要庆祝的时刻。
  QQQ 全程 0 适配（高流动性指数 ETF 的期限结构最稳定正向）。
  → 结论：这个结构不适合当"每天找机会"的常规策略，只适合在期限结构倒挂时按需核算。
    因此它**不进每日研报**，只提供 `undertow cal-spread` 按需入口。

诚实边界：这是【结构核算与体检】，不是交易指令，也不替使用者拍板。
行权价与权利金均为 ETF 口径。快照无 bid/ask 时权利金用各腿 iv 反算 BS 理论中值，
实盘穿价成交会劣于理论值 —— 日历价差两条腿分属不同到期、流动性通常更差，
这个折价比单腿结构更明显。**保证金口径不在本模块**（因券商而异，见 headline 注记）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from undertow.analyze import blackscholes as bs
from undertow.core.models import OptionsSnapshot

# —— 到期选择 ——
# 近月卖腿：theta 最陡的一段。太近则 gamma 风险陡增，太远则收租效率低。
NEAR_DTE_LO, NEAR_DTE_HI = 7, 35
# 远月买腿：要足够远才有 vega 与"发动机"价值，但太远资金占用久、点差宽。
FAR_DTE_LO, FAR_DTE_HI = 45, 120
# 远近月 DTE 的最小倍数。两个到期挨太近时 theta 差与 vega 差都薄，
# 结构退化成"付了两次点差的裸卖"。
MIN_DTE_RATIO = 2.0

# —— 期限结构闸门（本模块的核心增补）——
# 近月 IV − 远月 IV，单位 pp。> 0 = 近月更贵 = 卖贵买便宜 = 顺风。
TERM_EDGE_MIN = 0.5     # 低于此判为不适配：没有期限结构优势就只剩 gamma 空头的风险
TERM_EDGE_RICH = 3.0    # 高于此判为"近月显著偏贵"——但同时是挤仓/事件的哨兵（见 ③）

# —— 选腿 ——
ATM_DELTA_TARGET = 0.50   # 日历默认选 ATM（gamma/theta/vega 都在 ATM 最大）
DIAGONAL_DELTA_MAX = 0.42 # 对角时近月卖腿的 |delta| 上限（更虚 = 更像纯收租）
# 每条腿最低 OI。**近月与远月分开设门，不是为了凑适配率，是两条腿的职责不同**：
# 近月卖腿要按到期日历反复滚动（他的规律 9「平旧卖新」），每一轮都吃两次点差，
# 所以对流动性最敏感；远月买腿建仓后原则上持有到结构解除，只进出各一次。
# 远月合约 OI 天然低于近月，用同一个门槛会把远月一律判死。
NEAR_MIN_OI = 200
FAR_MIN_OI = 100
_ANNUALIZE_DAYS = 365.0
_MULT = 100.0             # 合约乘数（ETF 期权）


@dataclass(frozen=True)
class CalendarLeg:
    action: str          # 卖出 / 买入
    kind: str            # C / P
    expiry: date
    dte: int
    strike: float
    delta: float         # 快照 delta（原符号）
    iv_pp: float
    oi: int
    bs_price: float      # 每股 BS 理论价
    theta: float         # 每股每日（负=损耗）
    gamma: float         # 每股
    vega: float          # 每股每 pp


@dataclass(frozen=True)
class CalendarPlan:
    """日历/对角结构核算。金额：每股为权利金口径，$ 为每 1 组合（×100）。"""
    applicable: bool
    headline: str
    kind: str | None = None            # C / P
    shape: str | None = None           # 日历（同行权价）/ 对角（不同行权价）
    spot: float | None = None
    legs: list[CalendarLeg] = field(default_factory=list)   # [卖近月, 买远月]

    # —— 第五件套：期限结构（本模块的核心增补）——
    term_edge_pp: float | None = None  # 近月 IV − 远月 IV（pp），正=顺风
    term_verdict: str = ""

    # —— 他的四件套 ——
    net_debit: float | None = None     # 期初净现金流，每股。>0=净支出（日历多头常态）
    net_debit_usd: float | None = None # 每组合 $
    be_lo: float | None = None         # 近月到期时的下沿盈亏平衡（标的价）
    be_hi: float | None = None         # 上沿
    sweet_spot: float | None = None    # 甜点 = 近月到期时收益最大处（≈ 卖腿行权价）

    # —— 净 Greeks（每组合）——
    net_theta_usd: float | None = None # 每天 $，正=收租
    net_gamma: float | None = None     # 每组合（负）
    net_vega_usd: float | None = None  # 每 pp $，正=IV 上行受益
    net_delta: float | None = None     # 每组合股数当量

    # —— gamma 临界（本模块的核心增补）——
    daily_be_move: float | None = None      # 标的每日移动多少就吃光当天 theta（价格）
    daily_be_move_pct: float | None = None  # 同上，% of spot

    notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


def _dte(expiry: date, today: date) -> int:
    return (expiry - today).days


def _pick_expiry(expiries: list[date], today: date, lo: int, hi: int) -> date | None:
    """在 [lo, hi] 内选 DTE 最居中的到期 —— 边缘到期的流动性与定价都更差。"""
    cand = [e for e in expiries if lo <= _dte(e, today) <= hi]
    if not cand:
        return None
    mid = (lo + hi) / 2
    return min(cand, key=lambda e: abs(_dte(e, today) - mid))


def _leg(c, spot: float, today: date, action: str) -> CalendarLeg:
    T = max(_dte(c.expiry, today), 0) / _ANNUALIZE_DAYS
    sig = c.iv if c.iv and c.iv > 0 else 0.0
    return CalendarLeg(
        action=action, kind=c.kind, expiry=c.expiry, dte=_dte(c.expiry, today),
        strike=c.strike, delta=c.delta, iv_pp=sig * 100, oi=c.open_interest,
        bs_price=bs.price(spot, c.strike, T, sig, c.kind),
        theta=bs.theta(spot, c.strike, T, sig, c.kind),
        gamma=bs.gamma(spot, c.strike, T, sig),
        vega=bs.vega(spot, c.strike, T, sig))


def build(snap: OptionsSnapshot, *, kind: str = "C", today: date | None = None,
          sell_delta: float | None = None) -> CalendarPlan:
    """从快照构建一组日历/对角结构并做体检。

    kind        "C" 或 "P"（由方向观点决定：看涨用 call 侧，看跌用 put 侧）
    sell_delta  近月卖腿的目标 |delta|。None = ATM 日历（同行权价）；
                给值则构成对角（卖更虚的近月，买 ATM 远月做发动机）。
    """
    today = today or date.today()
    spot = snap.spot
    if not spot or spot <= 0:
        return CalendarPlan(False, "标的现价缺失，无法核算")

    pool = [c for c in snap.contracts
            if c.kind == kind and c.iv and c.iv > 0 and _dte(c.expiry, today) > 0]
    if not pool:
        return CalendarPlan(False, f"快照内无可用 {kind} 合约")

    expiries = sorted({c.expiry for c in pool})
    near_exp = _pick_expiry(expiries, today, NEAR_DTE_LO, NEAR_DTE_HI)
    far_exp = _pick_expiry(expiries, today, FAR_DTE_LO, FAR_DTE_HI)
    blockers: list[str] = []
    if near_exp is None:
        blockers.append(f"无 DTE∈[{NEAR_DTE_LO},{NEAR_DTE_HI}] 的近月到期")
    if far_exp is None:
        blockers.append(f"无 DTE∈[{FAR_DTE_LO},{FAR_DTE_HI}] 的远月到期")
    if blockers:
        return CalendarPlan(False, "；".join(blockers), kind=kind, spot=spot,
                            blockers=blockers)

    n_dte, f_dte = _dte(near_exp, today), _dte(far_exp, today)
    if f_dte < n_dte * MIN_DTE_RATIO:
        blockers.append(
            f"远近月间距不足：{n_dte}d → {f_dte}d（需 ≥{MIN_DTE_RATIO:g}×）"
            "——theta 差与 vega 差都太薄，结构退化成付了两次点差的裸卖")

    near_pool = [c for c in pool if c.expiry == near_exp]
    far_pool = [c for c in pool if c.expiry == far_exp]

    # 近月卖腿：ATM（日历）或按目标 delta（对角）
    if sell_delta is None:
        near = min(near_pool, key=lambda c: abs(c.strike - spot))
        shape = "日历（同行权价）"
    else:
        tgt = min(abs(sell_delta), DIAGONAL_DELTA_MAX)
        near = min(near_pool, key=lambda c: abs(abs(c.delta) - tgt))
        shape = "对角（不同行权价）"
    # 远月买腿：日历取同行权价；对角取 ATM 做发动机（vega/gamma 最大处）
    target_k = near.strike if sell_delta is None else spot
    far = min(far_pool, key=lambda c: abs(c.strike - target_k))
    if sell_delta is None and abs(far.strike - near.strike) > 1e-9:
        shape = "对角（远月无同行权价，已就近取）"

    sell = _leg(near, spot, today, "卖出")
    buy = _leg(far, spot, today, "买入")

    if sell.oi < NEAR_MIN_OI:
        blockers.append(f"近月卖腿 OI {sell.oi:,} < {NEAR_MIN_OI}（流动性不足；"
                        "这条腿要反复滚动，点差会被吃很多次）")
    if buy.oi < FAR_MIN_OI:
        blockers.append(f"远月买腿 OI {buy.oi:,} < {FAR_MIN_OI}（流动性不足）")

    # —— 第五件套：期限结构 ——
    term_edge = sell.iv_pp - buy.iv_pp
    if term_edge < TERM_EDGE_MIN:
        rel = "低于" if term_edge < 0 else "高于"
        term_verdict = (f"近月 IV {sell.iv_pp:.1f}pp {rel} 远月 {buy.iv_pp:.1f}pp"
                        f"（差 {term_edge:+.2f}pp）——**没有期限结构优势**")
        blockers.append(
            f"期限结构逆风：近月 IV {rel}远月 {abs(term_edge):.2f}pp"
            f"（需高出 ≥{TERM_EDGE_MIN}pp）。"
            "日历多头本质是卖贵的近月、买便宜的远月；倒挂时这个结构从第一天就在逆风，"
            "只剩下 gamma 空头的风险敞口")
    elif term_edge >= TERM_EDGE_RICH:
        term_verdict = (f"近月 IV {sell.iv_pp:.1f}pp 显著高于远月 {buy.iv_pp:.1f}pp"
                        f"（差 {term_edge:+.2f}pp）——顺风，但**近月 IV 异常抬升"
                        "本身就是事件/挤仓的前兆**，需确认来源")
    else:
        term_verdict = (f"近月 IV {sell.iv_pp:.1f}pp 略高于远月 {buy.iv_pp:.1f}pp"
                        f"（差 {term_edge:+.2f}pp）——顺风")

    # —— 期初净现金流（他的第四件套）——
    net_debit = buy.bs_price - sell.bs_price

    # —— 净 Greeks（持仓 = 卖近月 1 + 买远月 1）——
    net_theta = buy.theta - sell.theta          # 每股每日，正=收租
    net_gamma = buy.gamma - sell.gamma          # 每股，负
    net_vega = buy.vega - sell.vega             # 每股每 pp，正
    net_delta = buy.delta - sell.delta          # 每股

    # —— gamma 临界移动 ——
    # 每日 theta 收入 = net_theta；移动 ΔS 的 gamma 损益 ≈ ½·net_gamma·ΔS²。
    # 打平：½·|net_gamma|·ΔS² = net_theta  →  ΔS = √(2·net_theta / |net_gamma|)
    # ⚠️ 二阶近似，只在 ΔS 不大时成立；大幅跳空要用全额重定价，这里给的是日常刻度。
    be_move = None
    if net_theta > 0 and net_gamma < 0:
        be_move = (2 * net_theta / abs(net_gamma)) ** 0.5

    # —— 甜点与上下沿 BE（近月到期时刻）——
    # 近月到期时：卖腿归内在价值，买腿仍有 (f_dte − n_dte) 天时间价值。
    # 收益最大处 = 卖腿行权价（卖腿归零、买腿时间价值最大）。
    T_res = (f_dte - n_dte) / _ANNUALIZE_DAYS
    sweet = sell.strike

    def _pnl_at(S: float) -> float:
        """近月到期时、标的为 S 的每股损益（假设远月 IV 不变）。"""
        near_val = max(0.0, S - sell.strike) if kind == "C" else max(0.0, sell.strike - S)
        far_val = bs.price(S, buy.strike, T_res, buy.iv_pp / 100, kind)
        return (far_val - near_val) - net_debit

    # 在现价两侧扫出盈亏平衡点（日历的损益曲线是单峰的，两侧各一个根）
    be_lo = be_hi = None
    step = spot * 0.002
    prev_S, prev_v = sweet, _pnl_at(sweet)
    S = sweet
    for _ in range(1500):                      # 向下
        S -= step
        if S <= 0:
            break
        v = _pnl_at(S)
        if prev_v > 0 >= v:
            be_lo = prev_S + (S - prev_S) * prev_v / (prev_v - v)
            break
        prev_S, prev_v = S, v
    prev_S, prev_v = sweet, _pnl_at(sweet)
    S = sweet
    for _ in range(1500):                      # 向上
        S += step
        v = _pnl_at(S)
        if prev_v > 0 >= v:
            be_hi = prev_S + (S - prev_S) * prev_v / (prev_v - v)
            break
        prev_S, prev_v = S, v

    notes: list[str] = []
    if term_edge >= TERM_EDGE_RICH:
        notes.append(
            f"🚨 期限结构极陡（{term_edge:+.2f}pp）：近月 IV 这样抬升通常不是白给的租金，"
            "而是市场在为近月的某件事定价（事件、逼空、交割博弈）。"
            "**这正是这类结构最诱人也最危险的时刻** —— 卖掉的近月 IV 很贵，"
            "但你同时在裸露负 gamma；一旦近月跳空而远月不跟，两条腿会各自亏损。"
            "建仓前必须先回答：近月这个溢价是什么造成的？")
    if net_debit <= 0:
        notes.append(f"期初净现金流 {net_debit * _MULT:+,.0f}$/组合 —— 净收入结构，"
                     "远月买腿比近月卖腿便宜；确认这是行权价差造成的（对角），"
                     "而非远月流动性差导致的定价失真")
    if be_move is not None:
        notes.append(
            f"负 gamma 日常失血点：标的每天移动超过 {be_move:.2f}（{be_move/spot*100:.2f}%）"
            f"，gamma 亏损就吃光当天 {net_theta * _MULT:.2f}$ 的 theta 收入")
    notes.append("买腿只是配件，不拿它表达方向观点 —— 它是负 theta 的；"
                 "它在这里的职责是 vega 与远端保护")
    notes.append("⚠️ 保证金口径因券商而异（是否识别跨月组合、是否按裸卖收），"
                 "不在本模块核算范围；仓位必须按你实际券商的占用回算")

    applicable = not blockers
    if applicable:
        head = (f"{shape} {kind} 侧：卖 {near_exp} {sell.strike:g} / 买 {far_exp} {buy.strike:g}"
                f"；期初净支出 {net_debit * _MULT:+,.0f}$，日收租 {net_theta * _MULT:+.2f}$，"
                f"vega {net_vega * _MULT:+.2f}$/pp，{term_verdict}")
    else:
        head = "不适配：" + "；".join(blockers)

    return CalendarPlan(
        applicable=applicable, headline=head, kind=kind, shape=shape, spot=spot,
        legs=[sell, buy],
        term_edge_pp=round(term_edge, 3), term_verdict=term_verdict,
        net_debit=round(net_debit, 4), net_debit_usd=round(net_debit * _MULT, 2),
        be_lo=round(be_lo, 3) if be_lo else None,
        be_hi=round(be_hi, 3) if be_hi else None,
        sweet_spot=sweet,
        net_theta_usd=round(net_theta * _MULT, 3),
        net_gamma=round(net_gamma * _MULT, 5),
        net_vega_usd=round(net_vega * _MULT, 3),
        net_delta=round(net_delta * _MULT, 2),
        daily_be_move=round(be_move, 4) if be_move else None,
        daily_be_move_pct=round(be_move / spot * 100, 3) if be_move else None,
        notes=notes, blockers=blockers)


def render_md(p: CalendarPlan) -> str:
    """结构核算的 Markdown 呈现。"""
    if not p.applicable:
        L = ["### 日历 / 对角价差 —— 不适配", ""]
        L += [f"- ⛔ {b}" for b in p.blockers]
        return "\n".join(L)
    L = [f"### 日历 / 对角价差 · {p.shape} · {p.kind} 侧", "",
         f"标的现价 **{p.spot:.2f}**", "",
         "| 腿 | 到期 | DTE | 行权价 | Δ | IV(pp) | OI | 理论价 |",
         "|---|---|---|---|---|---|---|---|"]
    for g in p.legs:
        L.append(f"| {g.action} | {g.expiry} | {g.dte} | {g.strike:g} | {g.delta:+.3f} "
                 f"| {g.iv_pp:.1f} | {g.oi:,} | {g.bs_price:.3f} |")
    L += ["",
          "**第五件套 · 期限结构**（原 SOP 缺的一环）", "",
          f"- {p.term_verdict}", "",
          "**四件套**", "",
          f"- 期初净现金流：**{p.net_debit_usd:+,.0f}$** / 组合"
          f"（{'净支出' if p.net_debit_usd > 0 else '净收入'}）",
          f"- 甜点（近月到期收益最大处）：**{p.sweet_spot:g}**",
          f"- 上下沿 BE：**{p.be_lo if p.be_lo else '—'} ~ {p.be_hi if p.be_hi else '—'}**",
          "- 分段斜率见净 Δ", "",
          "**净 Greeks / 组合**", "",
          f"| Θ | Γ | Vega | Δ |",
          "|---|---|---|---|",
          f"| {p.net_theta_usd:+.2f} $/天 | {p.net_gamma:+.4f} | "
          f"{p.net_vega_usd:+.2f} $/pp | {p.net_delta:+.1f} |", ""]
    if p.daily_be_move is not None:
        L.append(f"**负 Γ 临界**：标的日内移动 > **{p.daily_be_move:.2f}"
                 f"（{p.daily_be_move_pct:.2f}%）** 即吃光当天 Θ 收入")
        L.append("")
    L += [f"- {n}" for n in p.notes]
    L.append("")
    L.append("> 结构核算与体检，非交易指令。权利金为 BS 理论中值，"
             "实盘跨月两腿穿价成交会明显劣于此。")
    return "\n".join(L)
