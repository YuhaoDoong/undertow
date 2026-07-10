"""渲染层：把分析结果渲染成 Markdown 文本报告。

只负责"展示"，不含计算。复刻文章的复盘视角：
总持仓 -> 各类别净头寸与拥挤度 -> 本周变化的来源分解 -> 信号汇总。
"""
from __future__ import annotations

from undertow.analyze.positioning import PositioningAnalysis
from undertow.analyze.signals import Signal, net_bias
from undertow.analyze.gamma import GammaAnalysis

# 类别中文名（Disaggregated 口径）
CAT_LABEL = {
    "managed_money": "投机资金 Managed Money",
    "other_reportables": "聪明钱 Other Reportables",
    "swap_dealers": "互换商 Swap Dealers",
    "producer_merchant": "实体套保 Producer/Merchant",
    "nonreportable": "小户 Non-reportable",
}
# Legacy 口径只有 非商业/商业/非报告；映射到相同槽位但标签不同（金融期货如美元指数走这套）
CAT_LABEL_LEGACY = {
    **CAT_LABEL,
    "managed_money": "非商业(大投机) Non-Commercial",
    "producer_merchant": "商业(套保) Commercial",
}


def _labels(report_kind: str) -> dict:
    return CAT_LABEL_LEGACY if report_kind == "legacy_fut" else CAT_LABEL

# 报告里重点展示的类别（小户信息量低，省略明细）
PRIMARY_CATS = ("managed_money", "other_reportables", "swap_dealers", "producer_merchant")


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _z(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}"


def render(an: PositioningAnalysis, signals: list[Signal], display_name: str,
           report_kind: str = "disaggregated_fut") -> str:
    label = _labels(report_kind)
    lines: list[str] = []
    lines.append(f"## {display_name}")
    span = f"（对比上期 {an.prev_date}）" if an.prev_date else ""
    lines.append(f"**COT 报告日: {an.report_date}** {span}  ·  回看样本 {an.lookback_used} 周")
    oi_arrow = "▲" if an.open_interest_change > 0 else ("▼" if an.open_interest_change < 0 else "—")
    lines.append(
        f"总持仓 OI: {an.open_interest:,} 手  ({oi_arrow} {an.open_interest_change:+,})"
    )
    if an.concentration is not None:
        lines.append(f"大户集中度: {an.concentration.note()}（占OI%，净口径）")
    lines.append("")

    # 持仓总览表
    lines.append("| 类别 | 净头寸 | 占OI | 历史分位 | z | 本周净Δ | 来源 |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for name in PRIMARY_CATS:
        c = an.categories[name]
        if c.gross == 0 and c.net == 0:
            continue  # 该报告未细分此类别（如 Legacy 无 swap/other）→ 不列空行
        d = c.decomposition
        lines.append(
            f"| {label[name]} | {c.net:+,} | {c.net_pct_of_oi:+.1f}% | "
            f"{_pct(c.net_percentile)} | {_z(c.net_zscore)} | {d.net_change:+,} | "
            f"{d.driver}({d.conviction}) |"
        )
    lines.append("")

    # 信号汇总
    if signals:
        lines.append(f"### 信号（综合倾向：**{net_bias(signals)}**）")
        dir_icon = {
            "bullish": "🟢看多", "risk-up": "🟢挤空风险",
            "bearish": "🔴看空", "risk-down": "🔴回调风险",
            "neutral": "⚪中性",
        }
        for s in signals:
            tag = dir_icon.get(s.direction, s.direction)
            lines.append(f"- **[{s.strength}] {s.title}** · {tag}")
            lines.append(f"  - {s.detail}")
    else:
        lines.append("### 信号")
        lines.append("- 当前无触发的显著信号（持仓变化与拥挤度均在常规区间）。")
    lines.append("")
    return "\n".join(lines)


DISCLAIMER = (
    "> ⚠️ 用法提示：COT 数据每周五发布、滞后约 3 天，仅适合**波段级风险情境**判断，"
    "不构成交易指令。投机资金极端持仓常作【反指】；互换商方向含 OTC 对冲歧义。"
    "请与价格行为、期权 Gamma 分布等多因子共振后再决策。"
)


def render_all(blocks: list[str]) -> str:
    header = "# 持仓情报速览（COT / CFTC Disaggregated + Legacy）\n"
    return header + "\n" + DISCLAIMER + "\n\n" + "\n---\n\n".join(blocks)


# ——————————————————————————————————————————————————————————
# 期权 Gamma 报告
# ——————————————————————————————————————————————————————————

GAMMA_DISCLAIMER = (
    "> ⚠️ **代理 & 假设双重提示**：数据用 **ETF 期权**（GLD/SLV/USO）作 COMEX 商品期权的"
    "**代理**——位点以 ETF 计，×乘数≈商品价仅近似（USO 与 WTI 非线性，仅定性）。"
    "GEX 正负依赖\"做市商净多 call、净空 put\"这一**行业惯用但不确定**的假设；"
    "**OI 墙不依赖该假设，最可靠**。延迟数据、日内会变。"
)


def _commodity_hint(ga: GammaAnalysis, etf_level: float | None) -> str:
    c = ga.to_commodity(etf_level)
    return f" (≈商品 {c:,.0f})" if c is not None else ""


def render_gamma(ga: GammaAnalysis, display_name: str) -> str:
    L: list[str] = []
    q = {"good": "尚可", "weak": "弱(仅定性)"}.get(ga.proxy_quality, ga.proxy_quality)
    L.append(f"## {display_name} — 期权 Gamma 结构")
    L.append(f"代理标的 **{ga.proxy_symbol}**（代理质量：{q}）  ·  现价 {ga.spot:.2f}"
             f"{_commodity_hint(ga, ga.spot)}  ·  数据 {ga.asof}")
    L.append(f"近月窗口 ≤{ga.horizon_days} 天  ·  Put/Call OI 比 **{ga.put_call_ratio:.2f}**"
             f"（看跌OI {ga.total_put_oi:,} / 看涨OI {ga.total_call_oi:,}）")
    L.append("")

    # 关键位点
    L.append("### 关键位点（吸附/pin 候选）")
    L.append(f"- 🧱 **看涨墙(阻力) {ga.call_wall:.1f}**{_commodity_hint(ga, ga.call_wall)}"
             f"  ·  call OI {ga.call_wall_oi:,}")
    L.append(f"- 🧱 **看跌墙(支撑) {ga.put_wall:.1f}**{_commodity_hint(ga, ga.put_wall)}"
             f"  ·  put OI {ga.put_wall_oi:,}")
    if ga.zero_gamma is not None:
        rel = "上方" if ga.zero_gamma > ga.spot else "下方"
        L.append(f"- 🔄 **零伽马翻转位 {ga.zero_gamma:.1f}**{_commodity_hint(ga, ga.zero_gamma)}"
                 f"（在现价{rel}）：价格越过它，做市商对冲方向反转、波动特性切换。")
    else:
        L.append("- 🔄 零伽马翻转位：扫描区间内未出现翻转（伽马符号单一）。")
    if ga.nearest_expiry is not None:
        nc = f"{ga.nearest_call_wall:.1f}" if ga.nearest_call_wall else "—"
        npp = f"{ga.nearest_put_wall:.1f}" if ga.nearest_put_wall else "—"
        L.append(f"- 📌 最近到期 **{ga.nearest_expiry}** 的 pin 位：看涨 {nc} / 看跌 {npp}"
                 f"（临近到期，做市商对冲对这两个位最敏感）")
    L.append("")

    # GEX 状态
    L.append("### Gamma 状态")
    L.append(f"- 净 GEX ≈ **{ga.net_gex/1e6:+.0f}M**（相对单位）→ {ga.gex_regime}")
    if ga.net_gex < 0:
        L.append("  - 负伽马环境：跌时做市商卖、涨时买，**助涨助跌**，趋势/波动放大，别逆势接刀。")
    elif ga.net_gex > 0:
        L.append("  - 正伽马环境：做市商**逆向对冲**，价格倾向被钉、回归高伽马区，区间震荡概率大。")
    L.append("")

    # 近价分布表（只显著行：滤掉近零 OI 的噪音，保留墙与最接近现价的行）
    if ga.strike_rows:
        max_comb = max((r.call_oi + r.put_oi) for r in ga.strike_rows) or 1
        floor = 0.05 * max_comb
        keep = {ga.call_wall, ga.put_wall}
        nearest = min(ga.strike_rows, key=lambda r: abs(r.strike - ga.spot)).strike
        keep.add(nearest)
        rows = [r for r in ga.strike_rows if (r.call_oi + r.put_oi) >= floor or r.strike in keep]
        L.append(f"### 现价附近显著 OI / 净GEX 分布（≥{floor:,.0f} 张或墙位）")
        L.append("| 行权价 | call OI | put OI | 净GEX(相对) |")
        L.append("|---:|---:|---:|---:|")
        for r in rows:
            mark = " ←现价" if r.strike == nearest else ""
            L.append(f"| {r.strike:.1f}{mark} | {r.call_oi:,} | {r.put_oi:,} | {r.net_gex/1e6:+.1f}M |")
    L.append("")
    return "\n".join(L)


def render_gamma_all(blocks: list[str]) -> str:
    header = "# 期权 Gamma / OI 结构速览（CBOE 延迟数据，ETF 代理）\n"
    return header + "\n" + GAMMA_DISCLAIMER + "\n\n" + "\n---\n\n".join(blocks)


# ——————————————————————————————————————————————————————————
# 回测报告
# ——————————————————————————————————————————————————————————

BACKTEST_DISCLAIMER = (
    "> ⚠️ **指示性，非显著性检验**：样本仅约 3 年、前瞻窗口重叠（不独立）、"
    "价格用 ETF 代理（USO 对 WTI 有展期偏差）。无前视：逐周只用当周及之前数据，"
    "入场按 COT 周五发布滞后。\n"
    "> 信号表为**对齐收益**=顺信号方向交易的收益（看空信号价格跌→正）；逐周变向的信号用各次自身方向对齐。"
    "**对齐收益须显著为正、且命中率>50% 才算有效**；并要对照「无条件基线」（该品种本身的漂移）。"
)


def _fmt_pct(x: float) -> str:
    return "—" if x != x else f"{x*100:+.2f}%"  # x!=x 判 NaN


def _fmt_hit(h) -> str:
    return "—" if h is None or h != h else f"{h*100:.0f}%"


def render_backtest(bt, display_name: str, price_quality: str) -> str:
    L: list[str] = []
    L.append(f"## {display_name} — 信号回测")
    L.append(f"价格代理 **{bt.price_symbol}**（质量：{price_quality}）  ·  "
             f"样本 {bt.n_events} 周（{bt.date_from} → {bt.date_to}）")
    L.append("")

    hs = bt.horizons

    # 基线
    L.append("### 无条件基线（所有周的前瞻收益均值）")
    L.append("| 期限 | n | 均值 | 中位 |")
    L.append("|---|---:|---:|---:|")
    for h in hs:
        b = bt.baseline[h]
        L.append(f"| {h}日 | {b.n} | {_fmt_pct(b.mean_ret)} | {_fmt_pct(b.median_ret)} |")
    L.append("")

    # 信号表
    L.append("### 各信号（对齐收益 / 方向命中率）")
    head = "| 信号 | 方向 | 次数 | " + " | ".join(f"{h}日对齐" for h in hs) + " | " + " | ".join(f"{h}日命中" for h in hs) + " |"
    L.append(head)
    L.append("|---|---|---:|" + "---:|" * (2 * len(hs)))
    for s in bt.signals:
        means = " | ".join(_fmt_pct(s.by_horizon[h].mean_ret) for h in hs)
        hits = " | ".join(_fmt_hit(s.by_horizon[h].hit_rate) for h in hs)
        L.append(f"| {s.code} | {s.direction} | {s.occurrences} | {means} | {hits} |")
    L.append("")

    # MM 分位分桶（阈值校准核心）
    L.append(f"### 投机资金净分位 → {bt.primary_horizon}日前瞻收益（拥挤反指逻辑校准）")
    L.append("> 若反指成立，应见「分位越高、前瞻收益越低」的单调下行。")
    L.append("| 分桶 | n | 均值 | 中位 |")
    L.append("|---|---:|---:|---:|")
    for b in bt.mm_percentile_buckets:
        L.append(f"| {b.label} | {b.n} | {_fmt_pct(b.mean_fwd)} | {_fmt_pct(b.median_fwd)} |")
    L.append("")

    # bias 分桶
    if bt.bias_buckets:
        L.append(f"### 综合 bias → {bt.primary_horizon}日前瞻收益")
        L.append("| bias | n | 均值 | 中位 |")
        L.append("|---|---:|---:|---:|")
        for b in bt.bias_buckets:
            L.append(f"| {b.label} | {b.n} | {_fmt_pct(b.mean_fwd)} | {_fmt_pct(b.median_fwd)} |")
        L.append("")

    return "\n".join(L)


def render_backtest_all(blocks: list[str]) -> str:
    header = "# COT 信号回测（事件研究 · CBOE ETF 价格代理）\n"
    return header + "\n" + BACKTEST_DISCLAIMER + "\n\n" + "\n---\n\n".join(blocks)


# ——————————————————————————————————————————————————————————
# 期权资金流 / 持仓异动报告
# ——————————————————————————————————————————————————————————

FLOW_DISCLAIMER = (
    "> ⚠️ **启发式 & 代理提示**：复刻文章作者「逐行权价分买卖方」的读法——"
    "**OI增+IV升=买方抬价、OI增+IV降=卖方写权**。延迟数据无逐笔成交，买卖方为**IV方向代理**推断；"
    "「Delta修正ΔIV」是对作者方法的**原理化近似**（剔除现价移动沿偏斜的机械IV项），边界行可能与人工酌情判断不同。"
    "ETF 代理（USO≠WTI，行权价/IV仅定性）、样本短，**只作预警不作预言**。\n"
    "> ΔOI/ΔIV 需要**两天落盘的快照**才有；CBOE 不存期权历史，先 `snapshot` 攒数据，明天起才出 diff。"
)


def _mny(m: float) -> str:
    return f"{m*100:+.1f}%"


def _flow_icon(bias: str) -> str:
    return {"bearish": "🔴", "bullish": "🟢", "unwind": "⚪", "neutral": "·"}.get(bias, "·")


def _flow_kind_table(changes, kind: str) -> list[str]:
    items = sorted([c for c in changes if c.kind == kind], key=lambda x: -abs(x.d_oi))[:14]
    if not items:
        return []
    side = "Put（下方保护/支撑）" if kind == "P" else "Call（上方压制/突破）"
    L = [f"**{side}**",
         "| 行权价 | ΔOI | 当前OI | 精确Delta | Delta修正ΔIV | 判断 |",
         "|---:|---:|---:|---:|---:|---|"]
    for c in sorted(items, key=lambda x: x.strike):
        wall = f" 🧱{c.on_wall}" if c.on_wall else ""
        adj = f"{c.adj_iv_pp:+.2f}pp" if c.prev_iv > 0 else "—（昨无IV）"
        judg = c.judgment + (f" ⟂{c.spread_note}" if c.spread_note else "")
        L.append(f"| {c.strike:.1f}{wall} | {_flow_icon(c.bias)}{c.d_oi:+,} | {c.curr_oi:,} | "
                 f"{c.delta:+.3f} | {adj} | {judg} |")
    L.append("")
    return L


def render_flow(fa, display_name: str) -> str:
    L: list[str] = []
    L.append(f"## {display_name} — 期权资金流 / 持仓异动")
    span = f"（{fa.prev_date} → {fa.curr_date}）" if fa.prev_date else f"（{fa.curr_date}，仅一份快照）"
    L.append(f"代理标的 **{fa.proxy_symbol}**  ·  现价 {fa.spot:.2f}  ·  近月窗口 ≤{fa.horizon_days}天  ·  数据 {fa.curr_asof}")
    L.append(f"对比区间 {span}")
    wall_bits = []
    if fa.put_wall is not None:
        wall_bits.append(f"put墙 {fa.put_wall:.1f}")
    if fa.call_wall is not None:
        wall_bits.append(f"call墙 {fa.call_wall:.1f}")
    if wall_bits:
        L.append("静态墙位（来自 gamma 层，供叠加判断）：" + " · ".join(wall_bits))
    L.append("")

    # —— 波动率面：ATM IV / 偏斜（作者口径的"买方确认"检查）——
    if fa.vol is not None:
        v = fa.vol
        L.append(f"### 波动率面（到期 {v.curr.expiry}，T-{v.curr.days_out}）——期权端是否确认价格")
        if v.prev is not None:
            L.append(f"- 现价 {v.d_spot_pct:+.2f}%  ·  ATM IV {v.prev.atm_iv_pp:.1f} → "
                     f"{v.curr.atm_iv_pp:.1f}（**{v.d_atm_pp:+.2f}pp**）")
            L.append(f"- 25Δ Put-Call skew {v.prev.skew25_pp:+.2f} → {v.curr.skew25_pp:+.2f}pp"
                     f"（{v.d_skew25_pp:+.2f}）  ·  10Δ {v.prev.skew10_pp:+.2f} → "
                     f"{v.curr.skew10_pp:+.2f}pp（{v.d_skew10_pp:+.2f}）")
            L.append(f"- **判读：{v.verdict}**")
            L.append("> 事件日（非农/CPI/FOMC 兑现后）IV 回落含事件溢价释放的机械成分，判读要打折；"
                     "偏斜是否收敛比 ATM IV 单独一条更干净。")
        else:
            L.append(f"- 当日水平：ATM IV {v.curr.atm_iv_pp:.1f}  ·  25Δ skew "
                     f"{v.curr.skew25_pp:+.2f}pp  ·  10Δ {v.curr.skew10_pp:+.2f}pp（{v.verdict}）")
        L.append("")

    # —— 日对日买卖方判定（核心，需两份快照）——
    if fa.prev_date:
        L.append("### 日对日买卖方判定（ΔOI × Delta修正IV → 买/卖方，复刻作者表）")
        L.append(f"**资金流净倾向：{fa.flow_tilt}**")
        if fa.changes:
            L += _flow_kind_table(fa.changes, "P")
            L += _flow_kind_table(fa.changes, "C")
        else:
            L.append("- 近月、现价附近无超过阈值（|ΔOI|≥50）的持仓异动。")
        L.append("")

    # —— 单快照异动（今日活跃，总能出）——
    L.append("### 今日异常活跃（单快照：成交量 / OI 比）")
    if fa.unusual:
        tot = fa.total_put_volume + fa.total_call_volume
        pcr_v = (fa.total_put_volume / fa.total_call_volume) if fa.total_call_volume else float("nan")
        L.append(f"近月成交：看跌 {fa.total_put_volume:,} / 看涨 {fa.total_call_volume:,}"
                 f"（put/call 成交比 {pcr_v:.2f}）")
        L.append("| 到期 | 行权价 | C/P | OI | 今成交 | 量/OI | IV | 价距 | 解读 |")
        L.append("|---|---:|:--:|---:|---:|---:|---:|---:|---|")
        for u in fa.unusual:
            cp = "P" if u.kind == "P" else "C"
            ratio = "∞" if u.vol_oi_ratio == float("inf") else f"{u.vol_oi_ratio:.1f}x"
            L.append(
                f"| {u.expiry} | {u.strike:.1f} | {cp} | {u.open_interest:,} | {u.volume:,} | "
                f"{ratio} | {u.iv*100:.0f}% | {_mny(u.moneyness)} | {u.note} |"
            )
        L.append("")
        L.append("> 量/OI ≫1 = 今日成交远超既有持仓，多为**新建仓**，常在明日兑现为新 OI——"
                 "这是 ΔOI 异动的**当日先兆**，值得盯。")
    else:
        L.append("- 近月、现价附近无明显异常活跃（成交量与 OI 比均在常规区间）。")
    L.append("")
    return "\n".join(L)


def render_flow_all(blocks: list[str]) -> str:
    header = "# 期权资金流 / 持仓异动速览（CBOE 延迟数据，自落盘快照）\n"
    return header + "\n" + FLOW_DISCLAIMER + "\n\n" + "\n---\n\n".join(blocks)
