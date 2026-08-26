"""持仓/拟开仓体检（纯确定性规则，无 I/O）。

吃 PortfolioReview（+ AccountCapital），跑一组规则化检查，把常见的坑显性预警：
  - 近到期 + 贴价/价内的空头腿 → 被指派风险（尤其资金不够接货）
  - 卖方盈亏比过低（冒大险赚小钱）→ 折算所需胜率
  - 窄价差 + 近到期 → gamma 风险（小波动大摆动、无时间修复）
  - 裸卖未封顶、逆势于综合研判、单品种集中度过高

**立场**：只作波段级风险情景预警，**非投资建议、非交易指令**；建议均为"权衡/参考"口径。
数字全部来自上游确定性模块，LLM 不碰算术。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from undertow.analyze import blackscholes as _bs

# —— 阈值（集中可调）——
NEAR_EXPIRY_DTE = 7          # DTE ≤ 此算"近到期"
TIGHT_WIDTH_PCT = 0.03       # 价差宽度/现价 ≤ 此算"窄价差"
POOR_RR_SELLER = 1.0         # 方向性/借方结构 max_profit/max_loss < 此 = 盈亏比偏低
CONC_HIGH_FRAC = 0.40        # 单品种风险资金 / 净资产 ≥ 此 = 集中度偏高
SELLER_EDGE_MIN_PP = 10.0    # 收权金结构：隐含胜率 − 盈亏平衡胜率 至少要有的安全边际(pp)
SINGLE_LONG_MAX_SIGMA = 1.0  # 单腿买方：回本幅度不应超过到期前 1σ（超过=彩票腿）
SINGLE_LONG_MIN_DELTA = 0.30 # 单腿买方：delta 下限（低于此＝标的动了也赚不到）
MIN_NET_DELTA_DEBIT = 0.10   # 借方结构净Δ下限：低于此＝对标的移动几乎无反应（提前平仓打法下无意义）
CONTRACT_MULT = 100
FEE_PER_CONTRACT = 0.80      # 实测长桥期权费率（$/张/笔），可按券商改
FEE_MAX_FRAC_OF_EV = 0.30    # 手续费占毛期望值超过此比例 = 被费用吃掉太多


@dataclass(frozen=True)
class HealthFinding:
    severity: str            # 高 / 中 / 低
    code: str
    title: str
    detail: str
    suggestion: str          # 权衡/参考口径，非指令
    scope: str = ""          # 涉及的品种/合约


_SEV_ORDER = {"高": 0, "中": 1, "低": 2}


def _breakeven_winrate(max_profit, max_loss) -> float | None:
    """收权金结构折算盈亏平衡胜率 = 最大亏/(最大亏+最大盈)。"""
    if not max_profit or not max_loss:
        return None
    mp, ml = abs(max_profit), abs(max_loss)
    return ml / (ml + mp) if (ml + mp) > 0 else None


def seller_edge(combo):
    """收权金结构的【胜率边际】——卖方该用的闸门，而不是裸看 R:R。

    卖方定义风险价差的 R:R 天生 < 1（收 0.38 / 宽 1 → 0.61），拿 R:R≥1 卡它是错的闸门。
    正确比较：**隐含胜率 vs 盈亏平衡胜率**。
      盈亏平衡胜率 = 最大亏 / (最大亏 + 最大盈)
      隐含胜率     ≈ 1 − |短腿每股 delta|（delta 作到期价内概率的常用代理）
      边际(pp)     = (隐含 − 盈亏平衡) × 100
    返回 (盈亏平衡胜率, 隐含胜率, 边际pp)；非收权金结构或缺 delta 时返回 None。
    """
    if not getattr(combo, "net_credit", None) or combo.net_credit <= 0:
        return None
    be = _breakeven_winrate(combo.max_profit, combo.max_loss)
    if be is None:
        return None
    shorts = [l for l in combo.legs
              if getattr(l, "qty", 0) < 0 and getattr(l, "pos_delta", None) is not None and l.qty]
    if not shorts:
        return None
    # 短腿每股 delta（pos_delta = 每股delta × 100 × 张数，含方向；取绝对值还原）
    def _per_share(l):
        return abs(l.pos_delta / (CONTRACT_MULT * abs(l.qty)))
    d = max(_per_share(l) for l in shorts)
    if not (0.0 < d < 1.0):
        return None
    implied = 1.0 - d
    return be, implied, (implied - be) * 100.0


def buyer_edge(combo, spot: float | None = None):
    """借方(付权金)结构的【胜率边际】——把卖方那套统一到买方。

    统一原理：**市场隐含胜率 vs 盈亏平衡胜率**，只是"隐含胜率"的估法不同。
      盈亏平衡胜率 = 最大亏 / (最大亏 + 最大盈) = 净付权金 / 宽度
      盈亏平衡价   = 长腿行权 ± 净付权金（call 加、put 减）
      隐含胜率     ≈ |盈亏平衡价处的 BS delta|（价格越过盈亏平衡的概率代理）
    注意与卖方公式的差别：卖方比的是「权金/宽度 − 短腿delta」，买方比的是
    「盈亏平衡价的 delta − 付权金/宽度」——**不是同一个公式，但是同一个原理**。
    返回 (盈亏平衡胜率, 隐含胜率, 边际pp)；非借方结构/数据不足返回 None。
    """
    nc = getattr(combo, "net_credit", None)
    if nc is None or nc >= 0:          # 需为净付权金
        return None
    if not combo.max_profit or not combo.max_loss or not spot or spot <= 0:
        return None
    debit = -nc
    be_wr = combo.max_loss / (combo.max_loss + combo.max_profit)
    longs = [l for l in combo.legs
             if getattr(l, "qty", 0) > 0 and getattr(l, "strike", None) and l.dte is not None]
    if not longs:
        return None
    lg = longs[0]
    be_price = lg.strike + debit if lg.kind == "C" else lg.strike - debit
    iv = getattr(lg, "iv", None) or 0.35
    T = max(lg.dte, 0) / 365.0
    d = abs(_bs.delta(spot, be_price, T, iv, kind=lg.kind))
    if not (0.0 < d < 1.0):
        return None
    return be_wr, d, (d - be_wr) * 100.0


def single_long_edge(combo, spot: float | None = None):
    """**单腿买方**的闸门（无宽度、无最大盈，套不了价差那套）。

    单腿买方赢的条件是"价格走到回本点之外"，所以该问两件事：
      ① **回本要走的幅度，相对到期前的合理波动够不够近**
         1σ 幅度 ≈ IV × √(DTE/365)；σ倍数 = 回本涨跌幅 ÷ 1σ。>1σ = 彩票腿。
      ② **delta 够不够**（效率）：delta 太小＝标的动了你也赚不到。
    返回 (回本价, 回本幅度%, σ倍数, delta)；非单腿买方/数据不足返回 None。
    """
    legs = getattr(combo, "legs", [])
    if len(legs) != 1 or not spot or spot <= 0:
        return None
    lg = legs[0]
    if getattr(lg, "qty", 0) <= 0 or lg.kind not in ("C", "P"):
        return None
    if lg.strike is None or lg.dte is None or lg.dte <= 0:
        return None
    cost = lg.cost_price
    be = lg.strike + cost if lg.kind == "C" else lg.strike - cost
    move_pct = abs(be - spot) / spot * 100.0
    iv = getattr(lg, "iv", None) or 0.35
    sigma1 = iv * ((lg.dte / 365.0) ** 0.5) * 100.0
    if sigma1 <= 0:
        return None
    delta = abs(lg.pos_delta / (CONTRACT_MULT * abs(lg.qty))) if lg.pos_delta and lg.qty else None
    return be, move_pct, move_pct / sigma1, delta


def after_fee_ev(combo, implied_p: float | None = None, spot: float | None = None):
    """⚠️ **这不是期望值，是最坏情形的二值近似**（codex review 2026-08-27 指出的命名错误）。

    公式 `p*最大盈利 − (1−p)*最大亏损` 把价差当成只有两种结局的赌局，
    但价差在两个行权价之间是**连续 payoff**——落在中间时既不是最大盈利也不是最大亏损。
    且 p 用短腿 delta 近似，delta 至多近似「到期价内概率」，不是「拿到最大盈利的概率」。
    两处近似都偏保守，所以本函数系统性**低估**真实期望。

    因此：可用作「连最坏情形都算不过账吗」的压力测试，**不可当作期望值使用**，
    更不该单凭它否决交易。风险中性下价差的真实 EV 本就≈0，靠它选结构没有意义。
    """
    """**扣费后期望值**——比"获利 > 手续费"严格得多的检验。

    关键修正：手续费该跟【期望盈利】比，不是跟【最大盈利】比。最大盈利你拿不到，
    期望值才是长期兑现的那个数。
      毛期望 = p × 最大盈 − (1−p) × 最大亏
      手续费 = 腿数 × 张数 × 每张费 × 2（开 + 平）
      净期望 = 毛期望 − 手续费
    p 优先用调用方给的隐含胜率（卖方=1−短腿delta；买方=盈亏平衡价delta）。
    返回 (毛期望, 手续费, 净期望, 费用占毛期望比)；数据不足返回 None。
    """
    if not combo.max_profit or not combo.max_loss:
        return None
    if implied_p is None:
        e = seller_edge(combo) or buyer_edge(combo, spot)
        if e is None:
            return None
        implied_p = e[1]
    if not (0.0 < implied_p < 1.0):
        return None
    gross = implied_p * combo.max_profit - (1 - implied_p) * combo.max_loss
    n_legs = max(len(getattr(combo, "legs", [])), 1)
    qty = max(int(getattr(combo, "qty", 1) or 1), 1)
    fees = n_legs * qty * FEE_PER_CONTRACT * 2
    net = gross - fees
    frac = (fees / gross) if gross > 0 else None
    return gross, fees, net, frac


def stop_risk(combo):
    """**按预设止损了结时的亏损**（软风险）——与 max_loss（跳空硬风险）区分。

    口径对齐档案的出场规则：
      · 收权金结构：亏到所收权金的 ~2 倍即平 → 损失 ≈ 净权金 × 100 × 张
      · 借方/买方结构：亏到成本 ~50% 即平 → 损失 ≈ 0.5 × 已付权金
    **注意**：软止损只在【正常行情】成立；跳空时直接吃 max_loss，所以两者都要设限。
    返回美元金额；无法估算返回 None。
    """
    nc = getattr(combo, "net_credit", None)
    qty = max(int(getattr(combo, "qty", 1) or 1), 1)
    if nc is None or not combo.max_loss:
        return None
    if nc > 0:                                   # 收权金：价差翻倍了结
        risk = nc * CONTRACT_MULT * qty
    else:                                        # 付权金：亏一半了结
        risk = 0.5 * (-nc) * CONTRACT_MULT * qty
    return min(risk, combo.max_loss)             # 不可能超过最大亏损


def net_delta_per_contract(combo) -> float | None:
    """组合的【每张净Δ】——标的每动 $1，组合市值动多少（×100=美元/张）。

    **为什么重要**：在【提前平仓】的打法下（不持有到期、不追求越过行权价），
    真正决定你能否捕捉上涨的是净Δ，不是"到期越过行权价的概率"。
    窄价差两腿几乎抵消 → 到期概率很高、但净Δ 极低 → 标的涨了你也赚不到。
    实例：TQQQ 买71卖72 到期打平概率 47%（看着不错），净Δ 仅 0.04——
    标的涨 $1 只赚 $4，对"涨了就走"的策略毫无意义。
    """
    legs = getattr(combo, "legs", [])
    qty = max(int(getattr(combo, "qty", 1) or 1), 1)
    ds = [l.pos_delta for l in legs if getattr(l, "pos_delta", None) is not None]
    if not ds or len(ds) != len(legs):
        return None
    return sum(ds) / (CONTRACT_MULT * qty)


def buyer_carry(combo, spot: float | None = None):
    """**买方结构的持有成本**：净Θ（每日损耗）与「每天需标的动多少才打平」。

    买方与卖方的框架**根本不同**：
      · 卖方收权金 → theta 是朋友，最优是**持有到接近到期**让权金归零 → 看【到期概率】
      · 买方付权金 → theta 是敌人，必须**提前平仓** → 看【净Δ 与每日损耗】
    到期概率对买方意义有限：你根本不打算持有到那天。
    返回 (每张净Θ美元/日, 净Δ, 每日打平所需标的涨幅%)；数据不足返回 None。
    """
    legs = getattr(combo, "legs", [])
    qty = max(int(getattr(combo, "qty", 1) or 1), 1)
    if not spot or spot <= 0 or not legs:
        return None
    nd = net_delta_per_contract(combo)
    # ⚠️ 用 |Δ| 衡量方向暴露效率，符号只用于展示方向。
    # 旧写法 `nd <= 0` 直接返回 → 看跌借方价差与多头 put 的净Δ本就为负，
    # 整个买方框架（每日 theta 打平、净Δ 闸门）只覆盖了看涨结构。
    # （codex review 2026-08-27）
    if nd is None or abs(nd) < 1e-9:
        return None
    nd_abs = abs(nd)
    th = 0.0
    for l in legs:
        if l.strike is None or l.dte is None or l.dte <= 0:
            return None
        iv = getattr(l, "iv", None) or 0.35
        sign = 1 if l.qty > 0 else -1
        th += sign * _bs.theta(spot, l.strike, l.dte / 365.0, iv, kind=l.kind)
    theta_usd = th * CONTRACT_MULT * qty
    # 用 |Δ| 做分母：打平所需波动是【幅度】问题，与方向无关。
    # 返回带符号的 nd 供展示方向，调用方比较闸门时须用 abs()。
    need_pct = (abs(theta_usd) / (nd_abs * CONTRACT_MULT * qty)) / spot * 100
    return theta_usd, nd, need_pct


def _combo_min_dte(combo) -> int | None:
    dtes = [l.dte for l in combo.legs if getattr(l, "dte", None) is not None]
    return min(dtes) if dtes else None


def check_group(g, capital) -> list[HealthFinding]:
    out: list[HealthFinding] = []
    spot = None
    # 现价从任一腿的 dist 反推不稳，改由 combo 用不到；被指派判断用腿的 moneyness。

    # —— 组合级：盈亏比 / 窄价差近到期 / 未封顶 ——
    for c in g.combos:
        dte = _combo_min_dte(c)
        # 闸门分两套：收权金结构看【胜率边际】；方向性/借方结构看【盈亏比】
        is_credit = bool(c.net_credit and c.net_credit > 0 and c.max_profit and c.max_loss)
        edge = seller_edge(c) if is_credit else None
        if edge is not None:
            be, implied, pp = edge
            if pp < SELLER_EDGE_MIN_PP:
                sev = "高" if (dte is not None and dte <= NEAR_EXPIRY_DTE) else "中"
                out.append(HealthFinding(
                    severity=sev, code="SELLER_EDGE_THIN", title="卖方胜率边际不足",
                    detail=(f"{c.label}：盈亏平衡需胜率 {be*100:.0f}%，短腿 delta 隐含胜率仅约 "
                            f"{implied*100:.0f}%，边际 {pp:+.0f}pp（低于 {SELLER_EDGE_MIN_PP:.0f}pp 安全线）"
                            + (f"；且剩 {dte} 天近到期（gamma 大、无时间修复）"
                               if dte is not None and dte <= NEAR_EXPIRY_DTE else "")),
                    suggestion="卖方该比的是【隐含胜率 vs 盈亏平衡胜率】而非 R:R：把短腿卖得更远(delta 更小)、"
                               "或放宽间距提升权金，把边际拉开。",
                    scope=f"{g.underlying} · {c.label}"))
        elif is_credit:
            # 缺 delta 无法算边际 → 退回用所需胜率提示（不拿 R:R<1 硬卡卖方结构）
            wr = _breakeven_winrate(c.max_profit, c.max_loss)
            if wr and wr >= 0.70:
                out.append(HealthFinding(
                    severity=("高" if (dte is not None and dte <= NEAR_EXPIRY_DTE) else "中"),
                    code="HIGH_WINRATE_NEEDED", title="收权金结构所需胜率偏高",
                    detail=(f"{c.label}：最大盈 ${c.max_profit:,.0f} / 最大亏 ${c.max_loss:,.0f}，"
                            f"折算需胜率 > {wr*100:.0f}% 才不亏期望（无 delta 数据，未能算隐含胜率）"),
                    suggestion="需要极高胜率的结构容错很低；放宽间距/拉远到期提升权金，或降规模。",
                    scope=f"{g.underlying} · {c.label}"))
        elif len(c.legs) == 1 and c.legs[0].qty > 0 and c.legs[0].kind in ("C", "P"):
            # 单腿买方：看【回本σ倍数 + delta 下限】
            sl = single_long_edge(c, getattr(g, "spot", None))
            if sl is not None:
                be_px, move, sig, dlt = sl
                bad = []
                if sig > SINGLE_LONG_MAX_SIGMA:
                    bad.append(f"回本需走 {move:.1f}%＝{sig:.2f}σ（>{SINGLE_LONG_MAX_SIGMA:.0f}σ 属彩票腿）")
                if dlt is not None and dlt < SINGLE_LONG_MIN_DELTA:
                    bad.append(f"delta 仅 {dlt:.2f}（<{SINGLE_LONG_MIN_DELTA:.2f}，标的动了也赚不到）")
                if bad:
                    out.append(HealthFinding(
                        severity="中", code="SINGLE_LONG_THIN", title="单腿买方效率不足",
                        detail=f"{c.label}：回本价 {be_px:.2f}；" + "；".join(bad),
                        suggestion="买方效率看 delta 与回本距离：换更价内的行权价(delta≥0.3)、"
                                   "或改用价差把成本降下来，别用深度价外博弹性。",
                        scope=f"{g.underlying} · {c.label}"))
        else:
            # 借方/方向性结构：先看胜率边际（同一原理、不同算法），再用盈亏比兜底
            # 买方主闸门＝净Δ + 每日损耗（提前平仓框架）；到期概率仅作次要参考
            carry = buyer_carry(c, getattr(g, "spot", None))
            if carry is not None:
                th_usd, nd, need = carry
                if need is not None and need > 0.5:
                    out.append(HealthFinding(
                        severity="中", code="THETA_HEAVY", title="每日损耗过重（买方持有成本高）",
                        detail=(f"{c.label}：每张净Θ ${th_usd:+.2f}/日、净Δ {nd:.2f} → "
                                f"**标的每天要涨 {need:.2f}% 才打平**。买方 theta 是敌人，"
                                f"必须提前平仓；这个消耗速度下拖不起。"),
                        suggestion="拉远到期(theta 更慢)、或把长腿买得更价内(净Δ更大)，"
                                   "让『每日打平所需涨幅』降到 0.5% 以内。",
                        scope=f"{g.underlying} · {c.label}"))
            bedge = buyer_edge(c, getattr(g, "spot", None))
            if bedge is not None and bedge[2] < SELLER_EDGE_MIN_PP and carry is None:
                be, implied, pp = bedge
                out.append(HealthFinding(
                    severity="低", code="BUYER_EDGE_THIN", title="买方到期口径边际不足（次要参考）",
                    detail=(f"{c.label}：若**持有到期**，盈亏平衡需胜率 {be*100:.0f}%、"
                            f"隐含仅 {implied*100:.0f}%，边际 {pp:+.0f}pp。"
                            f"注意：买方通常提前平仓，到期口径仅供参考，主看净Δ与每日损耗。"),
                    suggestion="买方真正该看的是净Δ（对上涨的反应）与 theta（每日成本）。",
                    scope=f"{g.underlying} · {c.label}"))
            nd = net_delta_per_contract(c)
            # ⚠️ 用 |净Δ| 判闸门：看跌借方价差/多头 put 的净Δ本就为负，
            # 旧写法 `0 < nd` 会把整个看跌侧排除在闸门之外。（codex review 2026-08-27）
            if nd is not None and 0 < abs(nd) < MIN_NET_DELTA_DEBIT and len(c.legs) >= 2:
                _dir = "涨" if nd > 0 else "跌"
                out.append(HealthFinding(
                    severity="中", code="LOW_NET_DELTA",
                    title=f"净Δ过低：对{_dir}几乎无反应",
                    detail=(f"{c.label}：每张净Δ {nd:+.3f}——标的每{_dir} $1 组合只变动 "
                            f"${abs(nd)*CONTRACT_MULT:.0f}。两腿相互抵消，**在『提前平仓』的打法下**"
                            f"（不持有到期），走对方向也赚不到多少。"),
                    suggestion="拉开两腿间距或把长腿买得更价内，让 |净Δ| ≥ 0.10；"
                               "到期概率高的窄价差，在不打算持有到期的策略里意义不大。",
                    scope=f"{g.underlying} · {c.label}"))
            elif c.max_profit and c.max_loss and (c.max_profit / c.max_loss) < POOR_RR_SELLER:
                out.append(HealthFinding(
                    severity="中", code="POOR_RR", title="方向性结构盈亏比偏低",
                    detail=f"{c.label}：最大盈 ${c.max_profit:,.0f} / 最大亏 ${c.max_loss:,.0f}"
                           f"（R:R {c.max_profit / c.max_loss:.2f} < 1）",
                    suggestion="低胜率的买方/方向性结构要靠大赔率：R:R < 1 直接不做，2:1 才配得上一次进场。",
                    scope=f"{g.underlying} · {c.label}"))
        # 扣费后期望值——费用按张数计，小仓位尤其致命
        fe = after_fee_ev(c, spot=getattr(g, "spot", None))
        if fe is not None:
            gross, fees, net, frac = fe
            if net <= 0:
                out.append(HealthFinding(
                    # ⚠️ 降为「中」：这个数不是期望值，是最坏情形的二值近似（见 after_fee_ev 文档），
                    # 系统性低估真实期望。用它做高危否决会错杀本可接受的结构。
                    severity="中", code="NEGATIVE_EV_AFTER_FEES",
                    title="最坏情形二值估算为负（非真期望值）",
                    detail=(f"{c.label}：二值估算 ${gross:+,.1f}、手续费 ${fees:.2f}"
                            f"（{len(c.legs)}腿×{c.qty}张×${FEE_PER_CONTRACT:.2f}×开平2次）"
                            f"→ **扣费后 ${net:+,.1f}**。"
                            f"⚠️ 该数把价差当成「要么最大盈、要么最大亏」的二值赌局，"
                            f"忽略两个行权价之间的连续 payoff，且用短腿 delta 近似胜率——"
                            f"**系统性低估，只能当压力测试，不可当期望值**。"),
                    suggestion="仅作「连最坏情形都算不过账吗」的参考。真要改善，"
                               "拉开边际或减少腿数张数；费用按张计，近月便宜合约张数多＝费用膨胀。",
                    scope=f"{g.underlying} · {c.label}"))
            elif frac is not None and frac > FEE_MAX_FRAC_OF_EV:
                out.append(HealthFinding(
                    severity="中", code="FEE_HEAVY", title="手续费吃掉过多期望值",
                    detail=(f"{c.label}：手续费 ${fees:.2f} 占毛期望 ${gross:,.1f} 的 "
                            f"{frac*100:.0f}%（>{FEE_MAX_FRAC_OF_EV*100:.0f}% 安全线），净期望仅 ${net:+,.1f}"),
                    suggestion="小仓位的费用占比天然高：要么把边际做厚、要么降低交易频率——"
                               "费用是按【张数×腿数×开平】累加的。",
                    scope=f"{g.underlying} · {c.label}"))
        # 窄价差 + 近到期（gamma 风险，本次对话那条教训）
        if len(c.legs) == 2 and dte is not None and dte <= NEAR_EXPIRY_DTE:
            strikes = [l.strike for l in c.legs if l.strike is not None]
            if len(strikes) == 2:
                width = abs(strikes[0] - strikes[1])
                ref = max(strikes)
                if ref > 0 and width / ref <= TIGHT_WIDTH_PCT:
                    out.append(HealthFinding(
                        severity="中", code="TIGHT_NEAR", title="窄价差 + 近到期（gamma 风险）",
                        detail=f"{c.label} 宽仅 {width:g}、剩 {dte} 天：近到期 gamma 大，"
                               f"标的一点波动就把薄缓冲击穿，且无时间均值回归。",
                        suggestion="θ 要赚，但别拖到最后贴价几天；常见做法 30–45 DTE 开、到 ~50% 利润或 ~21 DTE 前了结。",
                        scope=f"{g.underlying} · {c.label}"))
        # 未封顶风险
        if not c.defined_risk and c.stance and ("卖" in c.label or "空头" in c.label):
            out.append(HealthFinding(
                severity="中", code="UNDEFINED_RISK", title="风险未封顶的裸卖结构",
                detail=f"{c.label} 无对侧保护腿，下行/上行风险未定义。",
                suggestion="小账户尤其慎用裸卖；可加保护腿转成定义风险价差。",
                scope=f"{g.underlying} · {c.label}"))

    # —— 腿级：近到期被指派 + 资金不够接货 ——
    for lg in g.legs:
        if lg.kind not in ("C", "P") or lg.qty >= 0 or lg.dte is None:
            continue
        near = lg.dte <= NEAR_EXPIRY_DTE and lg.moneyness in ("贴价", "价内")
        if not near:
            continue
        if lg.kind == "P":
            assign = lg.strike * 100 * abs(lg.qty)
            if capital is not None and capital.buy_power < assign:
                out.append(HealthFinding(
                    severity="高", code="ASSIGN_CAPITAL_GAP",
                    title="近到期被指派 × 资金不够接货",
                    detail=(f"{lg.name}：剩 {lg.dte} 天且{lg.moneyness}；接货需 ${assign:,.0f}，"
                            f"购买力仅 ${capital.buy_power:,.0f}。到期被指派会触发垫付/强平。"),
                    suggestion="资金不足接货就别拖到期：到期前平仓或向下/向后展期(roll)，别让它到期指派。",
                    scope=f"{g.underlying} · {lg.name}"))
            else:
                out.append(HealthFinding(
                    severity="中", code="ASSIGN_NEAR",
                    title="近到期被指派风险",
                    detail=f"{lg.name}：剩 {lg.dte} 天且{lg.moneyness}，被指派概率上升。",
                    suggestion="愿接货可留（备足现金）；不愿则到期前平仓/展期。",
                    scope=f"{g.underlying} · {lg.name}"))
        else:  # 卖 call 被叫走
            out.append(HealthFinding(
                severity="中", code="CALLAWAY_NEAR", title="近到期被叫走风险",
                detail=f"{lg.name}：剩 {lg.dte} 天且{lg.moneyness}。",
                suggestion="持正股可接受被行权；否则平仓/向上展期。",
                scope=f"{g.underlying} · {lg.name}"))

    # —— 逆势于综合研判 ——
    bad = [lg for lg in g.legs if lg.align == "逆势"]
    if bad:
        names = "、".join(lg.name for lg in bad)
        out.append(HealthFinding(
            severity="低", code="COUNTER_TREND", title="有腿逆势于综合研判",
            detail=f"{names} 方向与综合研判（{g.bias}）相反。",
            suggestion="逆势属负 edge 的押注，注意仓位与止损。",
            scope=g.underlying))

    # —— 单品种集中度 ——
    if capital is not None and capital.net_assets > 0:
        risk = sum((c.capital_at_risk or 0) for c in g.combos)
        if risk >= CONC_HIGH_FRAC * capital.net_assets:
            out.append(HealthFinding(
                severity="中", code="CONCENTRATION", title="单品种集中度偏高",
                detail=f"{g.display_name} 风险资金 ${risk:,.0f} ≈ 净资产 {risk/capital.net_assets*100:.0f}%。",
                suggestion="单一标的占比过高，一次逆行冲击全账户；分散或降规模可控回撤。",
                scope=g.underlying))
    return out


def run_healthcheck(review, capital=None) -> list[HealthFinding]:
    """对整个组合跑体检，按严重度排序返回。"""
    findings: list[HealthFinding] = []
    for g in review.groups:
        findings += check_group(g, capital)
    findings.sort(key=lambda f: _SEV_ORDER.get(f.severity, 9))
    return findings
