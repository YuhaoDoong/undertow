"""综合研判层（outlook）：把 COT / Gamma / Flow 三层 + 回测可信度，
按【确定性规则】聚合成 方向倾向、关键位点、情景与失效位。

立场（务必牢记，已写进报告）:
  * 这是【规则化情景推演】，不是涨跌预言机。
  * 方向 = 各因子按【回测校准的可信度】加权投票的结果——所有权重、依据、贡献都
    显式列出、可审计；LLM 不参与算数。
  * "关键位点"来自期权结构（墙/零伽马/资金流活跃行权价），是客观可观测的；
    "情景"是规则化的 if-then，给的是【该盯哪些位、什么情况证伪】，不是点位预言。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from undertow.analyze.positioning import PositioningAnalysis
from undertow.analyze.signals import Signal
from undertow.analyze.gamma import GammaAnalysis
from undertow.analyze.flow import FlowAnalysis

# —— 信号可信度表（来自回测发现，集中可调）——
# code -> (可信度权重, 标签, 备注)
SIGNAL_RELIABILITY: dict[str, tuple[float, str, str]] = {
    "MM_CROWDED_LONG":   (1.0, "中", "拥挤反指仅在均值回归品种可信；单边趋势里存疑（回测：油有效、金失效）"),
    "MM_CROWDED_SHORT":  (1.0, "中", "同上，趋势行情勿盲信"),
    "SMART_DIVERGE_BULL":(1.2, "中", "金/银上较稳（回测命中 67~75%）"),
    "SMART_DIVERGE_BEAR":(1.0, "中", "防守背离，参考"),
    "MM_FLOW_QUALITY":   (0.6, "低", "逐周变向、弱持续性时易反复"),
    "SWAP_DIR_SHORT":    (0.4, "低", "小样本 + OTC 对冲歧义"),
    "SWAP_DIR_LONG":     (0.4, "低", "小样本 + OTC 对冲歧义"),
}
_STRENGTH_W = {"强": 2.0, "中": 1.0, "弱": 0.5}
_DIR_SIGN = {"bullish": 1, "risk-up": 1, "bearish": -1, "risk-down": -1, "neutral": 0}
_DIR_CN = {1: "看多", -1: "看空", 0: "中性"}


@dataclass(frozen=True)
class FactorVote:
    layer: str        # COT / Gamma / Flow
    factor: str
    direction: str    # 看多 / 看空 / 中性
    sign: int
    weight: float     # 实际计入的权重（已含强度×可信度）
    reliability: str  # 高/中/低
    detail: str


@dataclass(frozen=True)
class KeyLevel:
    label: str
    etf_level: float
    commodity_level: float | None
    kind: str         # resistance / support / flip / pin / flow
    note: str


@dataclass(frozen=True)
class Scenario:
    name: str
    trigger: str
    path: str
    invalidation: str


@dataclass(frozen=True)
class Outlook:
    instrument: str
    display_name: str
    asof: str
    spot: float
    commodity_spot: float | None
    proxy_symbol: str
    bias: str             # 偏多 / 偏空 / 偏多(弱) / 偏空(弱) / 中性 / 分歧(双向)
    bias_score: float
    confidence: str       # 高 / 中 / 低
    regime: str           # 波动率/对冲环境（来自 GEX）
    votes: list[FactorVote] = field(default_factory=list)
    key_levels: list[KeyLevel] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    commodity_symbol: str = ""    # 真实期货符号（GC=F 等），空=未接真实价
    commodity_basis: str = ""     # 换算依据（实时比值 / 静态乘数近似）


def plain_summary(o: Outlook, *, day_chg_pct: float | None = None,
                  vol_verdict: str = "") -> str:
    """大白话速读：现价与日涨跌、上方阻力群、下方第一层支撑、多空分界、环境提示。

    全部由已算好的关键位确定性拼句，不引入任何新判断；措辞刻意保留不确定性
    （"把握不大/值得重视"），不说死"必涨必跌"。
    """
    use_comm = o.commodity_spot is not None
    px = o.commodity_spot if use_comm else o.spot

    def val(k: KeyLevel) -> float:
        return k.commodity_level if (use_comm and k.commodity_level is not None) else k.etf_level

    def short(label: str) -> str:
        return label.split(" / ")[0].strip()

    # 只用结构位（墙/零伽马）叙事；pin 与资金流活跃行留给位点表，避免啰嗦
    core = [k for k in o.key_levels if k.kind in ("resistance", "support", "flip")]
    above = sorted([k for k in core if val(k) > px], key=val)
    below = sorted([k for k in core if val(k) < px], key=val, reverse=True)

    fmt = (lambda v: f"{v:,.0f}") if px >= 500 else (lambda v: f"{v:,.1f}")
    S: list[str] = []

    # 1) 现价 + 日涨跌 + 综合方向
    chg = ""
    if day_chg_pct is not None:
        word = "涨" if day_chg_pct >= 0 else "跌"
        chg = f"，较上一交易日{word} {abs(day_chg_pct):.1f}%"
    S.append(f"现价 {fmt(px)}{chg}；综合研判{o.bias}（可信度{o.confidence}）。")

    # 2) 上方阻力
    if len(above) >= 2:
        chain = " → ".join(f"{fmt(val(k))}（{short(k.label)}）" for k in above[:3])
        up = f"上方 {chain} 阻力密集"
    elif above:
        k = above[0]
        up = f"上方最近阻力 {fmt(val(k))}（{short(k.label)}）"
    else:
        up = "上方近处已无成型 OI 阻力（现价在看涨墙上方，缺锚点）"
    if "信号中性" in vol_verdict:
        up += "，期权端信号中性——没人追价买涨，但看空保护也在撤，方向等表态"
    elif "未确认" in vol_verdict or "空头回补" in vol_verdict:
        up += "，且期权端暂无买方追价确认——短线一举突破的把握不大"
    elif "买方追价" in vol_verdict or "获期权端确认" in vol_verdict:
        up += "，期权端买方在追价——突破尝试值得重视"
    elif above:
        up += "，能否突破先看资金流表里 call 买方是否追价"
    S.append(up + "。")

    # 3) 下方支撑 + 多空分界
    pivot = next((k for k in below if k.kind == "support"), None)  # 看跌墙
    if below:
        first = below[0]
        if pivot is first:
            S.append(f"下方第一层支撑就是更重要的多空分界：{fmt(val(first))}"
                     f"（{short(first.label)}）——守住则区间对待，跌破则对冲盘转向助跌、"
                     "下行容易加速。")
        else:
            down = f"下方第一层支撑 {fmt(val(first))}（{short(first.label)}）"
            if pivot is not None:
                down += (f"；更重要的多空分界在 {fmt(val(pivot))}（{short(pivot.label)}）"
                         "——跌破则下行容易加速")
            S.append(down + "。")
    else:
        sup = next((k for k in core if k.kind == "support"), None)
        tail = f"（已跌破看跌墙 {fmt(val(sup))}，反抽该位先当压力看）" if sup else ""
        S.append(f"下方近处已无成型 OI 支撑{tail}，处破位区运行。")

    # 4) 对冲环境
    if "负Gamma" in o.regime or "负伽马" in o.regime:
        S.append("当前处负伽马环境：涨跌都会被做市商对冲放大，行情容易走过头，追单与接刀都需谨慎。")
    elif "正Gamma" in o.regime or "正伽马" in o.regime:
        S.append("当前处正伽马环境：波动易被对冲盘吸收，行情偏磨蹭，假突破多。")
    return "".join(S)


def macro_to_votes(ma) -> list[FactorVote]:
    """把宏观驱动（实际利率/美元/通胀预期）转成方向因子票。"""
    out: list[FactorVote] = []
    for d in ma.drivers:
        if d.vote_sign == 0:
            continue
        out.append(FactorVote(
            layer="Macro", factor=d.name, direction=_DIR_CN[d.vote_sign],
            sign=d.vote_sign, weight=round(d.weight, 2),
            reliability=d.reliability, detail=d.detail,
        ))
    return out


def _cot_votes(signals: list[Signal]) -> list[FactorVote]:
    out: list[FactorVote] = []
    for s in signals:
        sign = _DIR_SIGN.get(s.direction, 0)
        rel_w, rel_lbl, _note = SIGNAL_RELIABILITY.get(s.code, (0.8, "中", ""))
        w = _STRENGTH_W.get(s.strength, 1.0) * rel_w * (1 if sign else 0)
        out.append(FactorVote(
            layer="COT", factor=s.title, direction=_DIR_CN[sign], sign=sign,
            weight=round(w, 2), reliability=rel_lbl, detail=s.detail.split("。")[0] + "。",
        ))
    return out


def _gamma_vote(ga: GammaAnalysis) -> list[FactorVote]:
    out: list[FactorVote] = []
    # 墙位空间：现价距 call 墙(上方阻力/磁吸) vs 距 put 墙(下方支撑) 的相对空间
    if ga.call_wall_oi > 0 and ga.put_wall_oi > 0 and ga.spot > 0:
        up_room = (ga.call_wall - ga.spot) / ga.spot
        down_room = (ga.spot - ga.put_wall) / ga.spot
        diff = up_room - down_room
        sign = 1 if diff > 0.02 else (-1 if diff < -0.02 else 0)
        out.append(FactorVote(
            layer="Gamma", factor="墙位空间", direction=_DIR_CN[sign], sign=sign,
            weight=round(0.6 * (1 if sign else 0), 2), reliability="中",
            detail=(f"上行至 call 墙 {ga.call_wall:.1f} 空间 {up_room*100:+.1f}%、"
                    f"下行至 put 墙 {ga.put_wall:.1f} 空间 {down_room*100:+.1f}%。"),
        ))
    # Put/Call OI 比极端 → 反指（情绪过度）
    pcr = ga.put_call_ratio
    if pcr == pcr:  # 非 NaN
        sign = 1 if pcr >= 1.6 else (-1 if pcr <= 0.6 else 0)
        if sign:
            out.append(FactorVote(
                layer="Gamma", factor="Put/Call OI 比极端", direction=_DIR_CN[sign], sign=sign,
                weight=0.5, reliability="低",
                detail=f"P/C OI 比 {pcr:.2f}（过度{'看跌→反指偏多' if sign>0 else '看涨→反指偏空'}）。",
            ))
    return out


def _flow_vote(fa: FlowAnalysis) -> list[FactorVote]:
    if fa.prev_date:  # 有日对日 diff：用买卖方加权压力（与报告里的净倾向一致）
        dn, up = fa.downside_pressure, fa.upside_pressure
        sign = 0
        if dn > up * 1.3:
            sign = -1
        elif up > dn * 1.3:
            sign = 1
        if sign:
            return [FactorVote(
                layer="Flow", factor="买卖方资金流", direction=_DIR_CN[sign], sign=sign,
                weight=0.8, reliability="中",
                detail=f"买卖方加权 下行压力 {dn:,.0f} vs 上行 {up:,.0f}"
                       f"（OI增 × IV方向判买卖方）。",
            )]
        return []
    # 仅单快照：用今日成交 put/call 比（弱）
    tcv, tpv = fa.total_call_volume, fa.total_put_volume
    if tcv > 0:
        ratio = tpv / tcv
        sign = -1 if ratio >= 1.4 else (1 if ratio <= 0.7 else 0)
        if sign:
            return [FactorVote(
                layer="Flow", factor="今日成交 put/call 比", direction=_DIR_CN[sign], sign=sign,
                weight=0.4, reliability="低",
                detail=f"近月成交 put/call 比 {ratio:.2f}（单快照、无 ΔOI 确认，仅参考）。",
            )]
    return []


def _key_levels(ga: GammaAnalysis, fa: FlowAnalysis) -> list[KeyLevel]:
    out: list[KeyLevel] = []
    if ga.call_wall_oi > 0:
        out.append(KeyLevel("看涨墙 / 阻力", ga.call_wall, ga.to_commodity(ga.call_wall),
                            "resistance", f"call OI {ga.call_wall_oi:,}，上沿磁吸/阻力"))
    if ga.put_wall_oi > 0:
        out.append(KeyLevel("看跌墙 / 支撑", ga.put_wall, ga.to_commodity(ga.put_wall),
                            "support", f"put OI {ga.put_wall_oi:,}，下沿磁吸/支撑"))
    if ga.zero_gamma is not None:
        rel = "现价上方" if ga.zero_gamma > ga.spot else "现价下方"
        out.append(KeyLevel("零伽马翻转", ga.zero_gamma, ga.to_commodity(ga.zero_gamma),
                            "flip", f"{rel}；越过则做市商对冲方向反转、波动特性切换"))
    if ga.nearest_expiry is not None and ga.nearest_put_wall:
        out.append(KeyLevel(f"近到期 {ga.nearest_expiry} put pin", ga.nearest_put_wall,
                            ga.to_commodity(ga.nearest_put_wall), "pin", "临近到期，对冲最敏感"))
    if ga.nearest_expiry is not None and ga.nearest_call_wall:
        out.append(KeyLevel(f"近到期 {ga.nearest_expiry} call pin", ga.nearest_call_wall,
                            ga.to_commodity(ga.nearest_call_wall), "pin", "临近到期，对冲最敏感"))
    # 资金流活跃行权价（最多 2 个）
    for u in fa.unusual[:2]:
        kind_cn = "看跌" if u.kind == "P" else "看涨"
        out.append(KeyLevel(f"资金流活跃 {kind_cn}{u.strike:.1f}", u.strike,
                            ga.to_commodity(u.strike), "flow",
                            f"今日成交 {u.volume:,}（量/OI {('∞' if u.vol_oi_ratio==float('inf') else f'{u.vol_oi_ratio:.1f}x')}）"))
    # 按 ETF 价从高到低排
    out.sort(key=lambda k: -k.etf_level)
    return out


def _scenarios(ga: GammaAnalysis, bias_sign: int) -> list[Scenario]:
    spot, cw, pw, zg = ga.spot, ga.call_wall, ga.put_wall, ga.zero_gamma
    neg = ga.net_gex < 0
    out: list[Scenario] = []
    # 基准：区间
    out.append(Scenario(
        name="基准 · 区间震荡",
        trigger=f"价格在 put 墙 {pw:.1f} 与 call 墙 {cw:.1f} 之间",
        path=("正伽马环境，做市商逆向对冲、价格易被钉回墙间，区间内高抛低吸为主。"
              if not neg else "负伽马环境，墙间波动也会被放大，区间边沿假突破多，别追。"),
        invalidation=f"放量收破任一墙（{pw:.1f} 或 {cw:.1f}）",
    ))
    # 向下
    down_ref = min(pw, zg) if zg else pw
    out.append(Scenario(
        name="向下 · 破位走弱",
        trigger=f"收破 put 墙 {pw:.1f}" + (f"／零伽马 {zg:.1f}" if zg and zg < spot else ""),
        path=("负伽马助跌：做市商越跌越卖，下行加速，别逆势接刀。"
              if neg else "支撑失守、动能转弱，留意下一档 OI 支撑。"),
        invalidation=f"快速收回 {down_ref:.1f} 上方",
    ))
    # 向上
    out.append(Scenario(
        name="向上 · 突破走强",
        trigger=f"放量站上 call 墙 {cw:.1f}",
        path=("阻力翻支撑，若伴随做市商空头回补（gamma 挤压）上行可能加速。"),
        invalidation=f"站不稳、快速跌回 {cw:.1f} 下方（假突破）",
    ))
    # 把与 bias 一致的情景排前
    if bias_sign < 0:
        out[0], out[1] = out[1], out[0]
    elif bias_sign > 0:
        out[0], out[2] = out[2], out[0]
    return out


def _caveats(an: PositioningAnalysis, ga: GammaAnalysis, fa: FlowAnalysis,
            signals: list[Signal]) -> list[str]:
    cv = ["COT 滞后约 3 天，仅波段级；以下为规则化情景推演，非点位预言，须与价格行为共振后决策。"]
    if any(s.code.startswith("MM_CROWDED") for s in signals):
        cv.append("含拥挤反指信号：回测显示其仅在均值回归品种可信，单边趋势里会失效——别在强趋势中盲做反指。")
    if fa.prev_date is None:
        cv.append("资金流仅一份快照，ΔOI/ΔIV 尚不可用；连续 `snapshot` 攒够两天后，方向研判会更实。")
    elif fa.changes:
        cv.append("资金流为【单腿】买卖方判定，可能把价差组合（如熊市看涨价差）的保护腿误读为"
                  "方向性买盘——尤其原油机构常多腿布局，方向票仅供参考（详见 docs/author_notes.md）。")
    if ga.multiplier is None:
        cv.append(f"{ga.proxy_symbol} 与标的非线性（如 USO/WTI），关键位仅作 ETF 自身参考，换算商品仅定性。")
    if ga.net_gex < 0:
        cv.append("当前负伽马环境：波动被放大、趋势/破位更易延续，区间策略风险偏高。")
    return cv


def _bias(score: float, pos_sum: float, neg_sum: float) -> tuple[str, str]:
    """返回 (bias 文案, confidence)。"""
    # 双向都有较强票 → 分歧
    if pos_sum >= 1.5 and neg_sum >= 1.5 and abs(score) < 1.5:
        bias = "分歧(双向)"
    elif score >= 2.0:
        bias = "偏多"
    elif score <= -2.0:
        bias = "偏空"
    elif score >= 0.8:
        bias = "偏多(弱)"
    elif score <= -0.8:
        bias = "偏空(弱)"
    else:
        bias = "中性"
    mag = abs(score)
    if mag >= 3.0:
        conf = "高"
    elif mag >= 1.5:
        conf = "中"
    else:
        conf = "低"
    return bias, conf


def build_outlook(
    an: PositioningAnalysis,
    signals: list[Signal],
    ga: GammaAnalysis,
    fa: FlowAnalysis,
    *,
    display_name: str,
    commodity_symbol: str = "",
    commodity_basis: str = "",
    extra_votes: list[FactorVote] = (),
) -> Outlook:
    votes = _cot_votes(signals) + _gamma_vote(ga) + _flow_vote(fa) + list(extra_votes)
    pos_sum = sum(v.weight for v in votes if v.sign > 0)
    neg_sum = sum(v.weight for v in votes if v.sign < 0)
    score = pos_sum - neg_sum
    bias, conf = _bias(score, pos_sum, neg_sum)
    # 情景排序按 bias【标签】而非原始分数：中性/分歧时以"基准·区间"打头，不强行给方向
    bias_sign = 1 if bias.startswith("偏多") else (-1 if bias.startswith("偏空") else 0)

    caveats = _caveats(an, ga, fa, signals)
    if any(v.layer == "Macro" for v in votes):
        caveats.append("宏观（实际利率/美元）为背景与交叉验证；2024–2026 金价曾因央行购金"
                       "与实际利率阶段性脱钩，勿单独依赖宏观择时。")

    return Outlook(
        instrument=an.instrument,
        display_name=display_name,
        asof=ga.asof,
        spot=ga.spot,
        commodity_spot=ga.to_commodity(ga.spot),
        proxy_symbol=ga.proxy_symbol,
        bias=bias,
        bias_score=round(score, 2),
        confidence=conf,
        regime=ga.gex_regime,
        votes=sorted(votes, key=lambda v: -v.weight),
        key_levels=_key_levels(ga, fa),
        scenarios=_scenarios(ga, bias_sign),
        caveats=caveats,
        commodity_symbol=commodity_symbol,
        commodity_basis=commodity_basis,
    )
