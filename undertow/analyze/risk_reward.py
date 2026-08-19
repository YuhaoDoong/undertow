"""盈亏比闸门（确定性计算）——把作者"交易哲学"里最硬的一条落地：
**先看盈亏比、再看方不方向；现价追 vs 等回调分开算；不到 1:1 别做。**

外部分析者原话框架（8/18「说一下大家近期关心的问题」）：
  * 不能只考虑"会不会涨/能看到哪"，要先算盈亏比 = 潜在空间 / 止损风险；
  * 现价 4400 追、止损放起涨点 4020（风险 380）、目标 4800（空间 400）＝盈亏比≈1:1 → 差；
  * 等回调到斐波 0.5–0.618（4180–4235）再进，风险骤降、盈亏比才够；
  * 盈亏比不到 1:1 不做；到 2:1 也要胜率≥~40% 才值得；长线/短线资金分开考虑；
  * 别用最终结果倒推当初决策是否正确（赌对≠决策对，大数定律最后要亏）。

本模块吃 `fibonacci` 的摆动腿 + `outlook` 的结构墙位，为【顺摆动腿方向】各生成两张情景票——
"现价追" vs "等回调到斐波区"，各自算 entry/stop/target 与盈亏比，套作者阈值给评级。
只作波段级情景参考，非交易指令、非投资建议。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from undertow.analyze.fibonacci import FibAnalysis
from undertow.analyze.outlook import Outlook, KeyLevel

RR_MIN = 1.0              # 作者硬门槛：盈亏比不到 1:1 不做
RR_GOOD = 2.0            # 作者：到 2:1 也要胜率≥~40% 才值得
STOP_BUF_PCT = 0.4       # 止损放摆动极值之外的缓冲（占价%），防扎针
PULLBACK_RATIO = 0.5     # "等回调"情景默认锚在斐波 0.5（0.382–0.618 区中枢）


@dataclass(frozen=True)
class Setup:
    kind: str                 # chase（现价追）/ pullback（等回调）
    name: str
    direction: str            # 做多 / 做空
    entry: float
    entry_label: str
    stop: float
    stop_label: str
    target: float
    target_label: str
    rr: float                 # 盈亏比 = |target-entry| / |entry-stop|
    grade: str                # 差 / 中 / 优
    verdict: str
    entry_etf: float | None = None
    stop_etf: float | None = None
    target_etf: float | None = None


@dataclass(frozen=True)
class RiskRewardPlan:
    ok: bool
    direction: str            # 顺摆动腿方向：做多 / 做空
    spot: float
    bias_note: str = ""       # 与近端研判是否同向
    setups: list[Setup] = field(default_factory=list)
    headline: str = ""
    caveats: list[str] = field(default_factory=list)
    note: str = ""


def _grade(rr: float) -> tuple[str, str]:
    if rr < RR_MIN:
        return "差", f"盈亏比 {rr:.1f} < 1:1——作者规则：不做（用差盈亏比赌不确定上涨是短线最大的坑）"
    if rr < RR_GOOD:
        return "中", f"盈亏比 {rr:.1f}（1–2）——作者：这种盈亏比胜率至少 ~40% 才考虑做"
    return "优", f"盈亏比 {rr:.1f} ≥ 2:1——空间/风险结构占优（仍须自定胜率与仓位）"


def _rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    return round(abs(target - entry) / risk, 2) if risk > 0 else 0.0


def _nearest_wall(levels: list[KeyLevel], *, above: float | None = None,
                  below: float | None = None, use_comm: bool = True) -> tuple[float, str] | None:
    """结构目标：多头取上方最近阻力墙，空头取下方最近支撑墙。"""
    def v(k: KeyLevel) -> float:
        return k.commodity_level if (use_comm and k.commodity_level is not None) else k.etf_level
    if above is not None:
        cands = sorted([(v(k), k.label.split(" / ")[0].strip()) for k in levels
                        if k.kind in ("resistance", "flip", "flow", "pin") and v(k) > above],
                       key=lambda t: t[0])
        return cands[0] if cands else None
    if below is not None:
        cands = sorted([(v(k), k.label.split(" / ")[0].strip()) for k in levels
                        if k.kind in ("support", "flip", "flow", "pin") and v(k) < below],
                       key=lambda t: -t[0])
        return cands[0] if cands else None
    return None


def build_risk_reward(fib: FibAnalysis, o: Outlook | None = None,
                      *, key_levels: list[KeyLevel] | None = None) -> RiskRewardPlan:
    """顺摆动腿方向生成"现价追 vs 等回调"两张盈亏比情景票。

    o：可选，用于取近端研判方向做同向/反向注记，以及取结构墙位当目标。
    key_levels：显式传入墙位（缺省用 o.key_levels）。
    """
    if not fib.ok:
        return RiskRewardPlan(ok=False, direction="", spot=fib.spot,
                              note=f"无斐波那契摆动腿，盈亏比闸门不适用（{fib.note}）")

    levels = key_levels if key_levels is not None else (o.key_levels if o else [])
    use_comm = fib.ratio is not None
    ratio = fib.ratio
    spot = fib.spot

    def _etf(v: float) -> float | None:
        return (v / ratio) if ratio else None

    long_side = fib.direction == "up"
    direction = "做多" if long_side else "做空"

    # 与近端研判方向的关系（顺摆动腿 vs 我们的近端 bias）
    bias_note = ""
    if o is not None:
        nb = getattr(o, "near_bias", "") or o.bias
        if (long_side and "偏多" in nb) or (not long_side and "偏空" in nb):
            bias_note = f"近端研判 {nb}——与顺势方向同向，确认。"
        elif (long_side and "偏空" in nb) or (not long_side and "偏多" in nb):
            bias_note = f"⚠近端研判 {nb}——与顺摆动腿方向相反；顺势回调单仅作被动挂单参考，逆我方近端结构，须减仓控险。"
        else:
            bias_note = f"近端研判 {nb}（中性/分歧）——正是作者说的'别追、等回调给出好盈亏比再动手'的场景。"

    # 止损：放摆动腿起点（起涨/起跌点）之外一个缓冲——即斐波 1.0 的另一侧
    buf = spot * STOP_BUF_PCT / 100.0
    if long_side:
        stop = fib.swing_low - buf
        stop_label = f"起涨点 {fib.swing_low:.1f} 下方（斐波 1.0，缓冲 {buf:.1f}）"
        tgt = _nearest_wall(levels, above=spot, use_comm=use_comm)
        if tgt is None:
            ext = fib.extensions[0] if fib.extensions else None
            tgt = ((ext.price, ext.label) if ext else (fib.swing_high, "摆动高(0)"))
        pull_entry = fib.level(PULLBACK_RATIO) or spot
    else:
        stop = fib.swing_high + buf
        stop_label = f"起跌点 {fib.swing_high:.1f} 上方（斐波 1.0，缓冲 {buf:.1f}）"
        tgt = _nearest_wall(levels, below=spot, use_comm=use_comm)
        if tgt is None:
            ext = fib.extensions[0] if fib.extensions else None
            tgt = ((ext.price, ext.label) if ext else (fib.swing_low, "摆动低(0)"))
        pull_entry = fib.level(PULLBACK_RATIO) or spot
    target, target_label = tgt

    setups: list[Setup] = []

    # ① 现价追：entry=现价（作者反复警示的坏盈亏比样板）
    rr_c = _rr(spot, stop, target)
    g_c, v_c = _grade(rr_c)
    setups.append(Setup(
        kind="chase", name="现价追（作者反面样板）", direction=direction,
        entry=spot, entry_label=f"现价 {spot:.1f}",
        stop=stop, stop_label=stop_label, target=target, target_label=target_label,
        rr=rr_c, grade=g_c, verdict=v_c,
        entry_etf=_etf(spot), stop_etf=_etf(stop), target_etf=_etf(target)))

    # ② 等回调：entry=斐波 0.5（0.382–0.618 中枢），风险骤降盈亏比改善
    # 仅当回调位与现价同侧合理（多头回调位应低于现价、空头应高于现价）才生成
    valid_pull = (long_side and pull_entry < spot) or (not long_side and pull_entry > spot)
    if valid_pull and abs(pull_entry - stop) > 1e-9:
        rr_p = _rr(pull_entry, stop, target)
        g_p, v_p = _grade(rr_p)
        setups.append(Setup(
            kind="pullback", name=f"等回调到斐波 {PULLBACK_RATIO}（作者做法）", direction=direction,
            entry=pull_entry, entry_label=f"斐波 {PULLBACK_RATIO} 回撤 {pull_entry:.1f}",
            stop=stop, stop_label=stop_label, target=target, target_label=target_label,
            rr=rr_p, grade=g_p, verdict=v_p,
            entry_etf=_etf(pull_entry), stop_etf=_etf(stop), target_etf=_etf(target)))

    # 头条：把两张票的盈亏比对比讲成作者那句核心教训
    if len(setups) == 2:
        c, p = setups[0], setups[1]
        better = p.rr - c.rr
        headline = (f"顺{'上涨' if long_side else '下跌'}腿{direction}：现价追盈亏比仅 {c.rr:.1f}（{c.grade}），"
                    f"等回调到斐波 {PULLBACK_RATIO} 则升到 {p.rr:.1f}（{p.grade}）"
                    f"——{'差' + f'{better:.1f}' if better > 0 else '差别有限'}，印证作者'别追、等回调'。")
    else:
        c = setups[0]
        headline = f"顺{'上涨' if long_side else '下跌'}腿{direction}：现价追盈亏比 {c.rr:.1f}（{c.grade}）。"

    caveats = [
        "盈亏比 = 潜在空间 / 止损风险，是【必要非充分】条件——达标也要自定胜率与仓位，不达标直接放弃。",
        "长线仓与短线仓必须分开算：长线看年、不纠结一两天；这里的情景只服务短线波段。",
        "别用最终结果倒推决策对错——赌对一次不代表这套盈亏比在大数定律下能赢。",
        "目标取自结构墙位/斐波扩展，非价格预测；止损为日收盘口径，盘中扎针不算。",
    ]
    return RiskRewardPlan(
        ok=True, direction=direction, spot=spot, bias_note=bias_note,
        setups=setups, headline=headline, caveats=caveats, note=fib.note)
