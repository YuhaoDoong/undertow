"""结构读数：机构口径的期权结构分析，**不输出方向票**。

为什么单独成模块（2026-08-27，用户第三次抱怨"指标互相打架"后）
----------------------------------------------------------------
`outlook.py` 的投票层把性质完全不同的东西统一翻译成 `sign × weight` 再相加：

    COT 中期持仓背景 / Gamma 墙位（价格位置）/ 超买超卖（历史分布位置）
    / flow pressure（由 IV 推断谁主动）/ 强信号（同一份 flow 数据的二次摘要）

**它们根本不在回答同一个问题**，硬翻成"多/空"，假矛盾必然产生。而且同一批腿
被计票三次（_judge → pressure → _flow_vote → detect_strong_signal → verdict）。

本模块走另一条路，复刻机构研究的输出结构：

  1. **状态**（不是方向）—— 沿"防守强度轴"定级，见 DEFENSE_AXIS。
     防守增强 ≠ 看跌：它与"偏多/偏空"正交，因此不会和别的层打架。
  2. **位置** —— 承接区 / 风险区 / 上方供给区。
  3. **逐腿可靠度** —— 严格分级，**噪音与低可靠度对汇总贡献恒为 0**，
     不是 0.5、0.6。多数日子多数腿应当判为无信息，这是正常的。
  4. **证伪清单** —— "这是趋势转折还是短暂回撤"有明确的可证伪条件，
     全不满足即判回撤，不靠感觉。

关键判读规则（机构口径，反直觉但重要）
--------------------------------------
* **远虚 Put 加保护 ≠ 看跌到那个价位**，只是"防跌到那里的风险"。
  保护买盘的【位置】比【数量】重要：近价保护=现实防御，极远保护=尾部保险。
* **成交量大 + OI 变化大 ≠ 有方向信息**。多数是调仓换月，
  必须看新仓纯净度（|ΔOI|/成交）与 Delta 修正后的相对 IV 是否一致。
* **同行权价相对 IV** 剔除全市场 vol 平移；**Delta 修正后**再剔除现价移动
  沿偏斜"继承"来的机械项。两者都不做，就会把随市波动当成主动定价。

本模块只描述状态，**不产生任何交易方向结论**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 固定 Delta 阶梯：机构惯例看这几档的 Call/Put 相对 IV 变化
DELTA_LADDER = (0.40, 0.30, 0.25, 0.20, 0.15, 0.10)
# 主翼：真正承载现实防御意义的区间（更虚的是尾部保险，更实的方向含糊）
WING_LO, WING_HI = 0.20, 0.30
# 判定阈值（沿用 flow.py 既有口径，不新增拍脑袋的数）
SIG_PP = 0.30          # |ΔIV| 超过此(pp)才算有信息
SKEW_SIG_PP = 0.50     # |Δskew| 超过此(pp)才算明显
VOL_SURGE = 1.30       # 成交较近期均值放大此倍数才算"明显放量"
ATM_SURGE_PP = 1.00    # ATM IV 涨此(pp)以上才算"大涨"
PURITY_MIN = 0.30      # 新仓纯净度低于此 → 该腿降级（多为日内换手）
# ⚠️ 纯净度 > 1 在物理上不可能：每张成交最多产生一张 OI。出现即说明成交量没统计全
# （盘前/夜盘/场外未计入），此时 ΔOI 与成交量不同源，纯净度这个比值失去意义 →
# 必须降级，**绝不能因为比值大就当成最干净的新仓**（那恰好是反的）。
PURITY_IMPLAUSIBLE = 1.10

# —— 防守强度轴：与"偏多/偏空"正交，因此不会与方向层打架 ——
DEFENSE_AXIS = ("进攻", "中性", "中性偏防守", "短线偏防守", "恐慌防守")

RELIABILITY = ("噪音", "低", "中", "高")


@dataclass(frozen=True)
class LegRead:
    """单腿结构读数 + 可靠度分级。"""
    strike: float
    kind: str
    d_oi: int
    volume: int
    purity: float | None          # 新仓纯净度 |ΔOI|/成交（PNT 的替代）
    rel_iv_pp: float              # 同行权价相对 IV 变化
    delta_adj_pp: float           # Delta 修正后
    delta: float
    effective_delta: float        # ΔOI × delta（观测型方向敞口）
    reliability: str
    interpretation: str
    excluded_why: tuple[str, ...] = ()

    @property
    def counts(self) -> bool:
        """是否有资格进入汇总。**噪音与低可靠度恒为 False**。"""
        return self.reliability in ("中", "高")


@dataclass(frozen=True)
class LadderRow:
    delta: float
    d_call_pp: float | None
    d_put_pp: float | None
    d_skew_pp: float | None


@dataclass(frozen=True)
class StructureRead:
    ok: bool
    reason: str = ""
    spot: float = 0.0
    prev_date: str = ""
    curr_date: str = ""
    atm_iv_pp: float = 0.0
    d_atm_pp: float = 0.0
    skew25_pp: float = 0.0
    d_skew25_pp: float = 0.0
    skew10_pp: float = 0.0
    d_skew10_pp: float = 0.0
    ladder: list[LadderRow] = field(default_factory=list)
    legs: list[LegRead] = field(default_factory=list)
    eff_delta_call: float = 0.0
    eff_delta_put: float = 0.0
    eff_delta_total: float = 0.0
    defense: str = "中性"
    defense_why: list[str] = field(default_factory=list)
    # (条件, True满足/False不满足/None测不了, 实测说明)
    checklist: list[tuple[str, bool | None, str]] = field(default_factory=list)
    trend_break: bool = False
    state_summary: str = ""

    @property
    def usable_legs(self) -> list[LegRead]:
        return [l for l in self.legs if l.counts]

    @property
    def noise_ratio(self) -> float:
        """无信息腿占比。多数日子应当很高——这是正常的，不是缺陷。"""
        return 0.0 if not self.legs else 1 - len(self.usable_legs) / len(self.legs)


def _interp(pts, x):
    """按 x 升序线性插值；超界取最近端点。"""
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


def _iv_at_delta(contracts, kind: str, target: float) -> float | None:
    """在给定 |delta| 处插值该侧 IV（小数）。"""
    pts = [(abs(c.delta), c.iv) for c in contracts
           if c.kind == kind and c.iv > 0 and 0.01 < abs(c.delta) < 0.95]
    return _interp(pts, target)


def _pick_common_expiry(prev_live, curr_live):
    """两份快照共有、且 C/P 两侧报价都够数的到期里，取最接近一个月的那个。

    必须两侧共有——只在一侧存在的到期无法做日对日对比。
    """
    from undertow.analyze.flow import (VOL_MIN_QUOTES, VOL_TARGET_DAYS,
                                       VOL_MIN_DAYS, VOL_MAX_DAYS)

    def ok_exps(cs):
        by = {}
        for c in cs:
            if c.iv <= 0 or not (0.02 <= abs(c.delta) <= 0.85):
                continue
            by.setdefault(c.expiry, {"C": 0, "P": 0})[c.kind] += 1
        return {e for e, n in by.items()
                if n["C"] >= VOL_MIN_QUOTES and n["P"] >= VOL_MIN_QUOTES}

    common = ok_exps(prev_live) & ok_exps(curr_live)
    if not common:
        return None
    # ⚠️ 兜底路径：此处拿不到 today，只能以最早共有到期为基准推 30 天，
    # 可能选错月份。正常调用方必须传 expiry（来自 fa.vol，已按距今天 30 日选定），
    # 走到这里说明波动率面缺失，返回的阶梯只作参考。
    ref = min(common)
    return sorted(common, key=lambda e: abs((e - ref).days - VOL_TARGET_DAYS))[0]


def build_ladder(prev_live, curr_live, *, expiry=None) -> list[LadderRow]:
    """固定 Delta 阶梯上的 Call/Put 相对 IV 变化与偏斜变化。

    固定 Delta（而非固定行权价）比较，才能把"现价移动导致某行权价换了 moneyness"
    这个机械项排除掉——这是机构看偏斜的标准做法。

    ⚠️ **必须限定单一到期。** 不同到期的 IV 期限结构不同，把 60 天窗口内多个到期
    混在一起按 |delta| 插值，插出来的是期限结构而非偏斜，结果是纯噪音。
    2026-08-27 实测（混算版）：QQQ 的 Put 端阶梯 **0/11 天符号一致**，
    还出现 -10.64pp 这种不可信的单日跳变；机构口径始终分月份看
    （近月 / 次月各自一张表）。expiry=None 时自动取两侧共有、最接近
    VOL_TARGET_DAYS 的那个到期。
    """
    exp = expiry or _pick_common_expiry(prev_live, curr_live)
    if exp is None:
        return [LadderRow(d, None, None, None) for d in DELTA_LADDER]
    pl = [c for c in prev_live if c.expiry == exp]
    cl = [c for c in curr_live if c.expiry == exp]
    out = []
    for d in DELTA_LADDER:
        pc, cc = _iv_at_delta(pl, "C", d), _iv_at_delta(cl, "C", d)
        pp_, cp = _iv_at_delta(pl, "P", d), _iv_at_delta(cl, "P", d)
        dc = (cc - pc) * 100 if (pc and cc) else None
        dp = (cp - pp_) * 100 if (pp_ and cp) else None
        ds = (dp - dc) if (dc is not None and dp is not None) else None
        out.append(LadderRow(d, None if dc is None else round(dc, 2),
                             None if dp is None else round(dp, 2),
                             None if ds is None else round(ds, 2)))
    return out


def grade_leg(ch) -> LegRead:
    """给一条腿定可靠度。**只用数据质量做保守降级，不新增任何预测阈值。**

    分级原则（机构口径）：
      噪音 —— 相对 IV 变动在噪音带内 / 相对与绝对方向矛盾 / 已识别价差的保护腿
      低   —— 昨日无 IV（主动方未知）/ 减仓（只能推测了结撤退）/ 新仓纯净度低
      中   —— 有昨日可比、增仓、相对与 Delta 修正后同向、纯净度正常
      高   —— **目前恒不产生**：CBOE 日快照无法确认逐笔主动方，也无前瞻校准，
             不该假装拥有高可靠度单腿。待台账样本足够且通过检验后才开放。
    """
    why: list[str] = []
    purity = ch.oi_conversion
    rel, adj = ch.d_iv_pp, ch.adj_iv_pp
    eff = ch.d_oi * ch.delta

    def mk(rel_grade, interp):
        return LegRead(strike=ch.strike, kind=ch.kind, d_oi=ch.d_oi,
                       volume=ch.curr_volume, purity=purity,
                       rel_iv_pp=round(rel, 2), delta_adj_pp=round(adj, 2),
                       delta=ch.delta, effective_delta=round(eff, 1),
                       reliability=rel_grade, interpretation=interp,
                       excluded_why=tuple(why))

    if ch.spread_note and "保护" in ch.spread_note:
        why.append("已识别价差的保护腿——方向含义由整体结构决定，不单独计票")
        return mk("噪音", "价差保护腿")
    if abs(adj) < SIG_PP:
        why.append(f"Delta 修正后相对 IV |{adj:.2f}|pp < {SIG_PP}pp 噪音带")
        return mk("噪音", "无有效定价变化")
    if rel * adj < 0:
        why.append("相对 IV 与 Delta 修正后方向矛盾——多为随市波动被相对化放大")
        return mk("噪音", "定价方向存疑")
    if ch.prev_iv <= 0:
        why.append("昨日无 IV 报价，无法判主动方")
        return mk("低", "新挂/无可比（主动方未知）")
    if ch.d_oi <= 0:
        why.append("减仓——只能推测了结/撤退，主动方向不确定")
        return mk("低", "了结或撤退")
    if purity is not None and purity > PURITY_IMPLAUSIBLE:
        why.append(f"新仓纯净度 {purity:.2f} > 1——物理不可能，成交量未统计全，比值失效")
        return mk("低", "成交量口径不全（纯净度>1）")
    if purity is not None and purity < PURITY_MIN:
        why.append(f"新仓纯净度 {purity:.2f} < {PURITY_MIN}——多为日内换手，ΔOI 不载方向")
        return mk("低", "换手为主，非新建仓")
    # —— 到这里才是可用腿：有昨日可比 + 增仓 + 定价方向一致 + 纯净度正常 ——
    if ch.kind == "P":
        interp = "买方保护（下方防御）" if adj > 0 else "卖方做支撑（收权利金）"
    else:
        interp = "买方追价（上行需求）" if adj > 0 else "卖方压制（上方供给）"
    return mk("中", interp)


def _defense_level(d_atm, d_skew25, wing_put_pp, near_protect, far_protect):
    """沿防守强度轴定级。**这不是方向判断**——防守增强与看跌是两回事。"""
    why = []
    score = 0
    if d_skew25 >= SKEW_SIG_PP:
        score += 2; why.append(f"25Δ 偏斜向 put 侧走陡 {d_skew25:+.2f}pp（下方保险相对变贵）")
    elif d_skew25 <= -SKEW_SIG_PP:
        score -= 2; why.append(f"25Δ 偏斜向 call 侧修复 {d_skew25:+.2f}pp（上行溢价回升）")
    if wing_put_pp is not None and wing_put_pp >= SIG_PP:
        score += 2; why.append(f"主翼 20~30Δ put 相对变贵 {wing_put_pp:+.2f}pp（现实防御，非尾部）")
    if d_atm >= ATM_SURGE_PP:
        score += 2; why.append(f"ATM IV 大涨 {d_atm:+.2f}pp（整体避险需求上升）")
    elif d_atm <= -SIG_PP:
        score -= 1; why.append(f"ATM IV 回落 {d_atm:+.2f}pp（整体波动率需求减退）")
    if near_protect and far_protect and far_protect > near_protect * 3:
        score -= 1
        why.append("新增保护绝大部分在极远行权价——尾部保险性质，"
                   "**不代表预期跌到那里**，只是防那段风险")
    idx = 1 + max(-1, min(3, (score + 1) // 2))
    return DEFENSE_AXIS[max(0, min(len(DEFENSE_AXIS) - 1, idx))], why


def analyze_structure(fa, prev_live, curr_live, *, vol_prev=None, vol_curr=None,
                      recent_volumes=None) -> StructureRead:
    """主入口。fa=FlowAnalysis（提供逐腿 diff），prev/curr_live=近月合约列表。"""
    if not fa or not fa.changes or not fa.prev_date:
        return StructureRead(ok=False, reason="需要相邻两日快照才能读结构")
    vs = fa.vol
    if not (vs and vs.prev):
        return StructureRead(ok=False, reason="波动率面缺少可比昨日，读不出偏斜变化")

    legs = [grade_leg(c) for c in fa.changes]
    usable = [l for l in legs if l.counts]
    # ⚠️ 阶梯必须与 ATM/skew 用【同一个到期】，否则同一张卡里两个月份混着讲。
    # fa.vol 已按 VOL_TARGET_DAYS 选定到期并强制两日同月可比，直接沿用它。
    ladder = build_ladder(prev_live, curr_live,
                          expiry=getattr(vs.curr, "expiry", None))

    # 主翼 put 相对变贵程度（20~30Δ 段的偏斜变化均值）
    wing = [r.d_put_pp for r in ladder if WING_LO <= r.delta <= WING_HI and r.d_put_pp is not None]
    wing_put = round(sum(wing) / len(wing), 2) if wing else None

    # 新增保护的【位置】：近价 vs 极远（机构口径：位置比数量重要）
    spot = fa.spot or 1.0
    near_p = sum(l.d_oi for l in usable
                 if l.kind == "P" and l.d_oi > 0 and abs(l.strike / spot - 1) <= 0.05)
    far_p = sum(l.d_oi for l in usable
                if l.kind == "P" and l.d_oi > 0 and abs(l.strike / spot - 1) > 0.10)

    defense, why = _defense_level(vs.d_atm_pp, vs.d_skew25_pp, wing_put, near_p, far_p)

    # —— 证伪清单：趋势性转折 vs 短暂回撤 ——
    # 机构口径：真正的趋势下杀应当【同时】出现放量 + ATM IV 大涨 + 主翼 put 大幅变贵。
    # 全不满足即判回撤。有明确可证伪条件，不靠感觉。
    tv = fa.total_call_volume + fa.total_put_volume
    avg = (sum(recent_volumes) / len(recent_volumes)) if recent_volumes else None
    surge = (avg is not None and tv > avg * VOL_SURGE)
    # ⚠️ 三态而非布尔：数据不足 ≠ 条件不满足。把"测不了"记成 ❌ 会让证伪清单
    # 在缺数据时自动倒向"不是趋势转折"，那是把无知包装成结论。
    checks = [
        ("成交明显放量", (None if avg is None else surge),
         f"当日 {tv:,}" + (f" vs 近期均值 {avg:,.0f}（×{tv/avg:.2f}）" if avg else " · 无近期均值可比 → 测不了")),
        ("ATM IV 大涨", vs.d_atm_pp >= ATM_SURGE_PP, f"ΔATM {vs.d_atm_pp:+.2f}pp（门槛 +{ATM_SURGE_PP}pp）"),
        ("主翼 20~30Δ put 大幅变贵",
         (None if wing_put is None else wing_put >= SKEW_SIG_PP),
         f"主翼 Δput {wing_put:+.2f}pp（门槛 +{SKEW_SIG_PP}pp）" if wing_put is not None else "数据不足 → 测不了"),
    ]
    hit = sum(1 for _, ok, _ in checks if ok is True)
    miss = sum(1 for _, ok, _ in checks if ok is False)
    unknown = sum(1 for _, ok, _ in checks if ok is None)
    trend_break = hit == len(checks)

    ec = round(sum(l.effective_delta for l in legs if l.kind == "C"), 1)
    ep = round(sum(l.effective_delta for l in legs if l.kind == "P"), 1)

    if trend_break:
        verdict = "三项全部满足 → 具备趋势性转折特征"
    elif unknown:
        verdict = (f"{miss} 项不满足、{unknown} 项测不了 → "
                   f"证据不完整，不下转折结论（缺的那项不算「不满足」）")
    elif hit == 0:
        verdict = "三项全不满足 → 判为短暂回撤/调仓，不是趋势转折"
    else:
        verdict = f"三项中满足 {hit} 项 → 证据不完整，不下转折结论"

    summary = (f"结构状态 **{defense}**。逐腿 {len(legs)} 条中仅 {len(usable)} 条载有效信息"
               f"（{(1-len(usable)/len(legs))*100:.0f}% 为噪音/低可靠度，属正常）。{verdict}。")

    return StructureRead(
        ok=True, spot=fa.spot, prev_date=fa.prev_date or "", curr_date=fa.curr_date,
        atm_iv_pp=vs.curr.atm_iv_pp, d_atm_pp=vs.d_atm_pp,
        skew25_pp=vs.curr.skew25_pp, d_skew25_pp=vs.d_skew25_pp,
        skew10_pp=vs.curr.skew10_pp, d_skew10_pp=vs.d_skew10_pp,
        ladder=ladder, legs=legs,
        eff_delta_call=ec, eff_delta_put=ep, eff_delta_total=round(ec + ep, 1),
        defense=defense, defense_why=why, checklist=checks,
        trend_break=trend_break, state_summary=summary)


def render_md(sr: StructureRead, name: str = "") -> str:
    if not sr.ok:
        return f"## 结构读数\n\n（{sr.reason}）"
    L = [f"## 结构读数{' · ' + name if name else ''}（机构口径 · **不输出方向票**）", "",
         f"**{sr.state_summary}**", "",
         "### 一、偏斜与波动率面", "",
         "| 指标 | 当前 | 日变化 |", "|---|---:|---:|",
         f"| ATM IV | {sr.atm_iv_pp:.2f}% | {sr.d_atm_pp:+.2f}pp |",
         f"| 25Δ Put−Call Skew | {sr.skew25_pp:+.2f}pp | {sr.d_skew25_pp:+.2f}pp |",
         f"| 10Δ Put−Call Skew | {sr.skew10_pp:+.2f}pp | {sr.d_skew10_pp:+.2f}pp |", "",
         "**固定 Delta 相对 IV 变化**（固定 Delta 比较可排除现价移动的机械项）", "",
         "| Delta | Call 变化 | Put 变化 | Skew 变化 |", "|---|---:|---:|---:|"]
    for r in sr.ladder:
        f = lambda v: "—" if v is None else f"{v:+.2f}pp"
        L.append(f"| {r.delta*100:.0f}Δ | {f(r.d_call_pp)} | {f(r.d_put_pp)} | {f(r.d_skew_pp)} |")
    L += ["", "### 二、有效 Delta（观测口径 · Σ ΔOI×delta）", "",
          f"- Call `{sr.eff_delta_call:+,.0f}` ／ Put `{sr.eff_delta_put:+,.0f}` ／ "
          f"**净 `{sr.eff_delta_total:+,.0f}`**",
          "> 纯算术，不需判断谁是主动方——与「按 IV 推断主动方」的口径互为参照。", "",
          "### 三、防守强度", "", f"**{sr.defense}**"]
    L += [f"- {w}" for w in sr.defense_why] or ["- （无显著偏斜/波动率变化）"]
    L += ["", "### 四、趋势转折证伪清单", "",
          "> 机构口径：真正的趋势下杀应**同时**出现放量 + ATM IV 大涨 + 主翼 put 大幅变贵。",
          "", "| 条件 | 满足 | 实测 |", "|---|:--:|---|"]
    for label, ok, detail in sr.checklist:
        mark = "❓" if ok is None else ("✅" if ok else "❌")
        L.append(f"| {label} | {mark} | {detail} |")
    L += ["", "### 五、可用腿（噪音与低可靠度**不计入**，贡献恒为 0）", ""]
    if not sr.usable_legs:
        L.append("**今日无可用腿——没有方向性信息。** 这是正常结果，不是数据缺失。")
    else:
        L += ["| 行权价 | 类型 | ΔOI | 成交 | 纯净度 | 相对IV | Delta修正后 | 有效Δ | 解读 |",
              "|---|:--:|---:|---:|---:|---:|---:|---:|---|"]
        for l in sorted(sr.usable_legs, key=lambda x: -abs(x.d_oi))[:12]:
            pu = "—" if l.purity is None else f"{l.purity:.2f}"
            L.append(f"| {l.strike:g} | {l.kind} | {l.d_oi:+,} | {l.volume:,} | {pu} | "
                     f"{l.rel_iv_pp:+.2f}pp | {l.delta_adj_pp:+.2f}pp | {l.effective_delta:+,.0f} | "
                     f"{l.interpretation} |")
    L += ["", "> ⚠️ 机构口径的两条反直觉规则：",
          "> **①远虚 Put 加保护 ≠ 看跌到那个价位**，只是防那段风险；保护的【位置】比【数量】重要。",
          "> **②成交量大 + OI 变化大 ≠ 有方向信息**——多数是调仓换月，需纯净度与定价方向同时印证。"]
    return "\n".join(L)
