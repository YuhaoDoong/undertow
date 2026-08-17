"""策略情景参数化（期货）：把综合研判的方向 + 期权结构位，翻译成
【规则化的交易情景参数】——触发条件、参考进出区、失效线、目标与机械盈亏比。

立场（与 outlook 层一致，写进报告）:
  * 这是【策略模块的确定性输出】，给人/顾问做参考框架，不是交易指令。
  * 方向完全由综合研判 bias 决定（"定好方向再定点位"）；中性/分歧时不出点位。
  * 所有区间宽度按 ATR（真实波幅）缩放；实时层（Gamma 环境 / 波动率面）对
    所选方向投否决票，模块只负责如实呈现，不替使用者拍板。
  * 期权策略（价差/保护结构）为占位，待接入真实期货期权链后实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from undertow.analyze.outlook import Outlook, KeyLevel
from undertow.analyze.flow import VolSurface, D_SKEW_SIG
from undertow.core.models import PriceSeries

ATR_WINDOW = 14          # ATR 回看天数
ATR_BASELINE_PCT = 2.0   # 仓位缩放基准日波幅（%）
FALLBACK_BUF_PCT = 1.5   # 无价格序列时缓冲区 = 现价 × 此百分比
ZONE_W_ATR = 1.0         # 墙前触发区宽度（ATR 倍数）
INVALID_W_ATR = 0.5      # 失效缓冲（ATR 倍数）


@dataclass(frozen=True)
class TradeScenario:
    key: str                  # fade / trigger
    name: str
    stance: str               # 逆势型 / 顺结构型
    direction: str            # 做空 / 做多
    trigger: str              # 触发条件（文字）
    entry_lo: float | None    # 参考进场区下沿（None=单点参考）
    entry_hi: float | None
    entry_ref: float          # 盈亏比计算用的参考价
    invalidation: float
    invalidation_note: str
    targets: list[tuple[float, str]] = field(default_factory=list)  # (价位, 标签)
    rr: list[float] = field(default_factory=list)                   # 对应目标的盈亏比
    status: str = ""          # 待命 / 未触发 / 触发观察中 / 结构条件已满足 / 情景作废
    status_note: str = ""
    exit_plan: str = ""       # 规则化止盈止损方案（模板拼句，非交易指令）


@dataclass(frozen=True)
class StrategyPlan:
    direction: str            # 做空 / 做多 / 观望
    direction_source: str     # 方向来源说明（bias + 可信度）
    spot: float               # 与位点同口径的现价（商品价优先）
    unit: str                 # 商品 / ETF
    atr: float | None
    atr_pct: float | None
    atr_note: str
    vetoes: list[str] = field(default_factory=list)   # 实时层否决票
    scenarios: list[TradeScenario] = field(default_factory=list)
    verdict: str = ""
    sizing_note: str = ""
    options_note: str = "期权策略（价差/保护性结构）为占位——待接入真实期货期权链后实现。"
    caveats: list[str] = field(default_factory=list)


# ——————————————————————————————————————————————————————————
# ATR
# ——————————————————————————————————————————————————————————


def compute_atr(series: PriceSeries | None, window: int = ATR_WINDOW) -> tuple[float | None, str]:
    """真实波幅均值。有高低价用标准 ATR；只有收盘价退化为日均 |Δclose|。"""
    if series is None or len(series.closes) < window + 1:
        return None, "无足够价格序列，缓冲区退化为现价百分比"
    cs = series.closes
    if series.highs and series.lows and len(series.highs) == len(cs) == len(series.lows):
        trs = [max(series.highs[i] - series.lows[i],
                   abs(series.highs[i] - cs[i - 1]),
                   abs(series.lows[i] - cs[i - 1]))
               for i in range(1, len(cs))]
        note = f"ATR({window}) 真实波幅"
    else:
        trs = [abs(cs[i] - cs[i - 1]) for i in range(1, len(cs))]
        note = f"收盘波幅近似（{window}日日均|Δ收盘|，序列无高低价）"
    atr = sum(trs[-window:]) / window
    return (atr, note) if atr > 0 else (None, "波幅为零，序列异常")


# ——————————————————————————————————————————————————————————
# 位点抽取（与 plain_summary 同口径：商品价优先，缺则 ETF）
# ——————————————————————————————————————————————————————————


def _val(k: KeyLevel, use_comm: bool) -> float:
    return k.commodity_level if (use_comm and k.commodity_level is not None) else k.etf_level


def _short(label: str) -> str:
    return label.split(" / ")[0].strip()


def _etf_anchor(k: KeyLevel, use_comm: bool, dp: int = 0) -> str:
    """墙/翻转位补 ETF 行权价锚（换算展示时）——分辨墙真动 vs 比值漂移。"""
    return f"（ETF {k.etf_level:.{dp}f}）" if (use_comm and k.etf_level is not None) else ""


def _pick(levels: list[KeyLevel], kind: str) -> KeyLevel | None:
    for k in levels:
        if k.kind == kind:
            return k
    return None


# ——————————————————————————————————————————————————————————
# 情景构建
# ——————————————————————————————————————————————————————————


def _rr(entry: float, invalid: float, target: float) -> float:
    risk = abs(invalid - entry)
    return round(abs(entry - target) / risk, 1) if risk > 0 else 0.0


def _targets_below(levels: list[KeyLevel], ceiling: float, use_comm: bool,
                   n: int = 2, *, ref: float | None = None,
                   min_dist: float = 0.0) -> list[tuple[float, str]]:
    """ceiling 下方的结构位，从近到远取 n 个（flow/pin/flip/support）。

    ref/min_dist：止盈候选须距入场参考 ref 至少 min_dist（= max(1×ATR, 单位风险)），
    保证首目标盈亏比 ≥1——离入场太近的 pin/资金流位不配当止盈（它们是仓位重心
    而非承接墙，够远才有减仓价值）。
    """
    cands = sorted(
        [(v, _short(k.label)) for k in levels
         if k.kind in ("flow", "pin", "flip", "support")
         and (v := _val(k, use_comm)) < ceiling
         and (ref is None or v <= ref - min_dist)],
        key=lambda t: -t[0])
    out: list[tuple[float, str]] = []
    for v, lbl in cands:
        if all(abs(v - o[0]) > 1e-9 for o in out):
            out.append((v, lbl))
        if len(out) >= n:
            break
    return out


def _targets_above(levels: list[KeyLevel], floor_: float, use_comm: bool,
                   n: int = 2, *, ref: float | None = None,
                   min_dist: float = 0.0) -> list[tuple[float, str]]:
    cands = sorted(
        [(v, _short(k.label)) for k in levels
         if k.kind in ("flow", "pin", "flip", "resistance")
         and (v := _val(k, use_comm)) > floor_
         and (ref is None or v >= ref + min_dist)],
        key=lambda t: t[0])
    out: list[tuple[float, str]] = []
    for v, lbl in cands:
        if all(abs(v - o[0]) > 1e-9 for o in out):
            out.append((v, lbl))
        if len(out) >= n:
            break
    return out


def _fade_scenario(direction: str, wall: KeyLevel, levels: list[KeyLevel],
                   spot: float, buf: float, use_comm: bool) -> TradeScenario:
    """情景 A：墙前拒绝（逆势型）。做空=call 墙前，做多=put 墙前。"""
    w = _val(wall, use_comm)
    short_side = direction == "做空"
    if short_side:
        lo, hi = w - ZONE_W_ATR * buf, w
        invalid = w + INVALID_W_ATR * buf
        entry_ref = w - 0.5 * ZONE_W_ATR * buf
        targets = _targets_below(levels, lo, use_comm,
                                 ref=entry_ref, min_dist=max(buf, invalid - entry_ref))
        trigger = (f"价格进入墙前区且当日收盘被打回 {_short(wall.label)}"
                   f"{_etf_anchor(wall, use_comm)}下方（拒绝形态）")
        inv_note = "放量收上失效线；或 call 墙 OI 上移（墙被搬走）即结构性失效"
        if spot > invalid:
            status, note = "情景作废", "现价已在失效线上方，墙被突破，待重评"
        elif spot > w:
            status, note = "突破测试中", "现价在墙上方、失效线下方——等回收或确认"
        elif spot >= lo:
            status, note = "触发观察中", "已进入墙前区，等拒绝形态（收盘打回）"
        else:
            status, note = "待命", f"距触发区下沿还差 {lo - spot:.2f}"
    else:
        lo, hi = w, w + ZONE_W_ATR * buf
        invalid = w - INVALID_W_ATR * buf
        entry_ref = w + 0.5 * ZONE_W_ATR * buf
        targets = _targets_above(levels, hi, use_comm,
                                 ref=entry_ref, min_dist=max(buf, entry_ref - invalid))
        trigger = (f"价格回踩墙前区且当日收盘收回 {_short(wall.label)}"
                   f"{_etf_anchor(wall, use_comm)}上方（承接形态）")
        inv_note = "放量收破失效线；或 put 墙 OI 下移（墙被搬走）即结构性失效"
        if spot < invalid:
            status, note = "情景作废", "现价已在失效线下方，墙被跌破，待重评"
        elif spot < w:
            status, note = "破位测试中", "现价在墙下方、失效线上方——等回收或确认"
        elif spot <= hi:
            status, note = "触发观察中", "已进入墙前区，等承接形态（收盘收回）"
        else:
            status, note = "待命", f"距触发区上沿还差 {spot - hi:.2f}"
    return TradeScenario(
        key="fade", name=f"墙前{'拒绝' if short_side else '承接'}",
        stance="逆势型", direction=direction, trigger=trigger,
        entry_lo=lo, entry_hi=hi, entry_ref=entry_ref,
        invalidation=invalid, invalidation_note=inv_note,
        targets=targets, rr=[_rr(entry_ref, invalid, t) for t, _ in targets],
        status=status, status_note=note)


def _trigger_scenario(direction: str, flip: KeyLevel, levels: list[KeyLevel],
                      spot: float, buf: float, use_comm: bool) -> TradeScenario:
    """情景 B：翻转追认（顺结构型）。零伽马收破/收复 = 对冲环境切换。"""
    f = _val(flip, use_comm)
    short_side = direction == "做空"
    if short_side:
        # 失效线：flip 上方 1 ATR 内最近结构位，无则 flip + 0.5 ATR
        above = _targets_above(levels, f, use_comm, n=1)
        invalid = above[0][0] if above and above[0][0] <= f + buf else f + INVALID_W_ATR * buf
        targets = _targets_below(levels, f, use_comm,
                                 ref=f, min_dist=max(buf, invalid - f))
        trigger = "日收盘跌破零伽马（对冲环境翻负 → 下行重获放大器），次日回抽不收复确认"
        if spot > f:
            status, note = "未触发", f"现价高于触发线 {spot - f:.2f}——环境仍偏正Gamma侧"
        else:
            status, note = "结构条件已满足", "现价低于零伽马——等回抽不收复的确认"
    else:
        below = _targets_below(levels, f, use_comm, n=1)
        invalid = below[0][0] if below and below[0][0] >= f - buf else f - INVALID_W_ATR * buf
        targets = _targets_above(levels, f, use_comm,
                                 ref=f, min_dist=max(buf, f - invalid))
        trigger = "日收盘站上零伽马（对冲环境翻正 → 波动被抑制、回调易被承接），次日回踩不跌破确认"
        if spot < f:
            status, note = "未触发", f"现价低于触发线 {f - spot:.2f}——环境仍偏负Gamma侧"
        else:
            status, note = "结构条件已满足", "现价高于零伽马——等回踩不跌破的确认"
    return TradeScenario(
        key="trigger", name="零伽马翻转追认",
        stance="顺结构型", direction=direction, trigger=trigger,
        entry_lo=None, entry_hi=None, entry_ref=f,
        invalidation=invalid,
        invalidation_note="收回失效线另一侧即触发作废（翻转点被夺回）",
        targets=targets, rr=[_rr(f, invalid, t) for t, _ in targets],
        status=status, status_note=note)


# ——————————————————————————————————————————————————————————
# 否决票（实时层 vs 所选方向）
# ——————————————————————————————————————————————————————————


WINDOW_DAYS = 10          # 窗口状态机回看的收盘日数
RETEST_TOUCH_ATR = 0.5    # 回抽"摸到翻转点附近"的判定带宽（ATR 倍数）


def _trigger_window_status(short_side: bool, struct_history: list[tuple],
                           series: PriceSeries | None, buf: float,
                           ) -> tuple[str, str] | None:
    """翻转追认的跨日状态机：用最近 N 个【已完成日收盘】对逐日零伽马的序列判定。

    状态：未触发(含曾破位被收复) / 破位日(T+0) / 已破位·等回抽 / 追认成立·持仓管理。
    关键语义（用户指正）："追认成立"= 按规则仓位应已在场内，是持仓管理不是新开仓点。
    返回 None = 数据不足，退回单日逻辑。
    """
    if not struct_history or series is None or not series.closes:
        return None
    closes = dict(zip(series.dates, series.closes))
    highs = dict(zip(series.dates, series.highs)) if series.highs else {}
    lows = dict(zip(series.dates, series.lows)) if series.lows else {}
    rows = sorted((d, closes[d], f) for d, f, *_ in struct_history
                  if f and d in closes)[-WINDOW_DAYS:]
    if len(rows) < 2:
        return None

    broken = [(c < f) if short_side else (c > f) for _, c, f in rows]
    if not broken[-1]:
        if any(broken):
            d_re = next(rows[i][0] for i in range(len(rows) - 1, -1, -1) if broken[i])
            side = "收复" if short_side else "跌回"
            return ("未触发", f"近{len(rows)}日曾收盘破位但已被{side}（最近破位侧收盘在 "
                              f"{d_re}）——情景重置，等新的破位日")
        return None   # 一直未破位 → 用单日逻辑的距离提示

    # 最新收盘在破位侧：找本轮破位起点
    k = len(broken) - 1
    while k > 0 and broken[k - 1]:
        k -= 1
    d0 = rows[k][0]
    d0_s = f"{d0} 或更早" if (k == 0 and broken[0]) else str(d0)  # 首日即破位=左截尾
    n = len(rows) - 1 - k
    if n == 0:
        act = "跌破" if short_side else "站上"
        return (f"破位日（T+0）", f"{d0} 收盘首次{act}零伽马——按规则等次日起的"
                                  f"回抽确认，今日不是追认点")

    # 破位后是否出现过"回抽摸到翻转点附近"（有高低价用高/低，否则用收盘近似）
    band = RETEST_TOUCH_ATR * buf
    retest = False
    for d, c, f in rows[k + 1:]:
        probe = highs.get(d, c) if short_side else lows.get(d, c)
        if (short_side and probe >= f - band) or (not short_side and probe <= f + band):
            retest = True
            break
    if retest:
        return (f"追认成立（{d0_s} 破位，T+{n}）",
                "破位后回抽未收复，追认条件已完成——按规则仓位【应已在场内】，当前是"
                "持仓管理阶段（按 🎯 止盈止损执行），不是新开仓点；空仓者等下一次"
                "回抽不收复的二次机会，不追。"
                "（回溯口径：假设破位起即按当前方向执行；彼时的研判方向与否决票未回溯核验）")
    return (f"已破位·等回抽（T+{n}）",
            f"{d0_s} 破位后尚无有效回抽——追认点未出现，等价格回抽零伽马附近且收不回"
            f"时才是上车点，直接追单不取")


def _fade_window_note(short_side: bool, w: float, zone_lo: float, zone_hi: float,
                      struct_history: list[tuple], series: PriceSeries | None,
                      buf: float) -> str:
    """墙前拒绝的窗口注记：近几日是否出现过'摸墙被打回'的拒绝形态。"""
    if series is None or not series.closes:
        return ""
    closes = dict(zip(series.dates, series.closes))
    highs = dict(zip(series.dates, series.highs)) if series.highs else {}
    lows = dict(zip(series.dates, series.lows)) if series.lows else {}
    days = sorted(closes)[-5:]
    for d in reversed(days):
        c = closes[d]
        if short_side:
            touched = highs.get(d, c) >= w - 0.3 * buf
            rejected = c < zone_lo
        else:
            touched = lows.get(d, c) <= w + 0.3 * buf
            rejected = c > zone_hi
        if touched and rejected:
            return f"（{d} 曾摸墙被打回——拒绝形态近日已出现过一次，重复出现更可信）"
    return ""


def _exit_plan(s: TradeScenario, spot: float, cpe: float | None = None) -> str:
    """规则化止盈止损模板（确定性拼句）：止损=失效线、止盈=结构目标分批、
    首目标达成后移损保本。全部日收盘口径，属参考框架而非交易指令。
    cpe=商品价/ETF价：非空时给各价位补 ETF 行权价锚。"""
    if s.invalidation is None or not s.targets:
        return ""
    fmt = (lambda v: f"{v:,.0f}") if spot >= 500 else (lambda v: f"{v:,.1f}")
    et = (lambda v: f"ETF {v / cpe:.1f}，") if cpe else (lambda v: "")
    breach = "收盘站上" if s.direction == "做空" else "收盘跌破"
    risk = abs(s.entry_ref - s.invalidation)
    parts = [f"止损 = 失效线 {fmt(s.invalidation)}（{et(s.invalidation)}{breach}即离场，日收盘口径；"
             f"单位风险 {fmt(risk)} ≈ {100 * risk / s.entry_ref:.1f}%）"]
    if len(s.targets) >= 2:
        (t1, l1), (t2, l2) = s.targets[0], s.targets[1]
        r1 = s.rr[0] if s.rr else 0.0
        r2 = s.rr[1] if len(s.rr) > 1 else 0.0
        parts.append(f"止盈分批 = 首目标 {fmt(t1)}（{et(t1)}{l1}，R:R≈{r1:.1f}）减半仓，"
                     f"次目标 {fmt(t2)}（{et(t2)}{l2}，R:R≈{r2:.1f}）离场")
    else:
        t1, l1 = s.targets[0]
        r1 = s.rr[0] if s.rr else 0.0
        parts.append(f"止盈 = {fmt(t1)}（{et(t1)}{l1}，R:R≈{r1:.1f}）")
    parts.append(f"首目标达成后止损移至入场参考 {fmt(s.entry_ref)}（{et(s.entry_ref)}保本）")
    return "；".join(parts) + "。"


def _vetoes(direction: str, o: Outlook, flip_v: float | None, spot: float,
            vol: VolSurface | None) -> list[str]:
    out: list[str] = []
    if direction == "做空":
        if "正Gamma" in o.regime:
            out.append("正Gamma环境：做市商对冲抑制波动，空头易被磨（钉住风险）")
        if flip_v is not None and spot > flip_v:
            out.append("现价在零伽马上方：对冲环境站在多头一侧")
        if vol is not None and vol.prev is not None:
            if vol.d_skew25_pp <= -D_SKEW_SIG:
                out.append("偏斜明显收敛/翻负：期权端下行担忧退潮，call 端相对走强")
            if "获期权端确认" in vol.verdict:   # 肯定式短语，避免命中"没有买方追价"的否定句
                out.append("波动率面：买方追价，涨势获期权端确认")
    elif direction == "做多":
        if "负Gamma" in o.regime and flip_v is not None and spot < flip_v:
            out.append("现价在零伽马下方且负Gamma：下行同样会被对冲放大")
        if vol is not None and vol.prev is not None:
            if vol.d_skew25_pp >= D_SKEW_SIG:
                out.append("偏斜明显走陡：期权端下行担忧升温")
            if "未确认" in vol.verdict or "空头回补" in vol.verdict:
                out.append("波动率面：涨势未获期权端确认（更像空头回补）")
    return out


# ——————————————————————————————————————————————————————————
# 主入口
# ——————————————————————————————————————————————————————————


def build_strategy(o: Outlook, *, vol: VolSurface | None = None,
                   series: PriceSeries | None = None,
                   struct_history: list[tuple] | None = None) -> StrategyPlan:
    """struct_history：逐日结构位 [(交易日, 零伽马, call墙, put墙), ...]（与位点同口径），
    由调用方从历史快照逐日重算 gamma 得到——有它才启用跨日窗口状态机。"""
    use_comm = o.commodity_spot is not None
    spot = o.commodity_spot if use_comm else o.spot
    unit = "商品" if use_comm else "ETF"
    # 商品价/ETF价 比值（=当日实时换算比）：给 exit_plan 的价位补 ETF 行权价锚
    cpe = (o.commodity_spot / o.spot) if (use_comm and o.spot) else None

    # 方向跟【近端】(墙位/资金流·战术周期)——战术交易跟短线结构，如作者的 bear call
    # 跟短线空，而非中期持仓面；near_bias 缺省(旧数据/直接构造)时回退综合 bias。
    tac_bias = getattr(o, "near_bias", "") or o.bias
    tac_score = getattr(o, "near_score", None)
    tac_score = tac_score if tac_score is not None else o.bias_score
    if "偏空" in tac_bias:
        direction = "做空"
    elif "偏多" in tac_bias:
        direction = "做多"
    else:
        direction = "观望"
    split_note = ""
    if getattr(o, "horizon_split", False):
        split_note = f"；⚠近中分歧：中期(持仓/宏观)为 {o.mid_bias}，本策略只跟近端战术周期，逆中期而行需减仓控险"
    dir_src = (f"近端研判 {tac_bias}（墙位/资金流·战术周期，近端分 {tac_score:+.1f}）"
               f"→ 方向先行，再定点位{split_note}")

    atr, atr_note = compute_atr(series)
    atr_pct = 100.0 * atr / spot if (atr and spot) else None
    buf = atr if atr is not None else spot * FALLBACK_BUF_PCT / 100.0

    caveats = [
        "本区块是策略模块的规则化输出（供人/顾问参考的框架），不是交易指令，不构成投资建议。",
        f"位点为{unit}口径；在其他场所（如伦敦现货）执行需按当时基差自行平移。",
        "触发/失效均以【日收盘】为准，盘中扎针不算——日收盘 = 美东期货日线收盘"
        "（结算价约 13:30 ET 敲定，电子盘日切 17:00 ET = 北京次日凌晨 5/6 点）；"
        "每日 14:05 报告用【昨日已完成的收盘】做状态判定，盘中价只影响距离显示。"
        "事件窗口（CPI/FOMC/OPEX）内位点会失真。",
    ]

    if direction == "观望":
        return StrategyPlan(
            direction="观望", direction_source=dir_src, spot=spot, unit=unit,
            atr=atr, atr_pct=atr_pct, atr_note=atr_note,
            verdict="综合研判中性/分歧——模块不给方向，也就不出点位；仅继续监控结构位与触发条件。",
            sizing_note="", caveats=caveats)

    levels = o.key_levels
    flip = _pick(levels, "flip")
    flip_v = _val(flip, use_comm) if flip else None
    wall = _pick(levels, "resistance") if direction == "做空" else _pick(levels, "support")

    scenarios: list[TradeScenario] = []
    if wall is not None:
        scenarios.append(_fade_scenario(direction, wall, levels, spot, buf, use_comm))
    if flip is not None:
        scenarios.append(_trigger_scenario(direction, flip, levels, spot, buf, use_comm))

    # 窗口状态机：有逐日结构历史时，用最近 N 个已完成收盘的序列覆盖单日判定
    if struct_history:
        short_side = direction == "做空"
        upgraded = []
        for s in scenarios:
            if s.key == "trigger":
                ws = _trigger_window_status(short_side, struct_history, series, buf)
                if ws:
                    s = replace(s, status=ws[0], status_note=ws[1])
            elif s.key == "fade" and wall is not None and s.status == "待命":
                note = _fade_window_note(short_side, _val(wall, use_comm),
                                         s.entry_lo, s.entry_hi,
                                         struct_history, series, buf)
                if note:
                    s = replace(s, status_note=s.status_note + note)
            upgraded.append(s)
        scenarios = upgraded
    scenarios = [replace(s, exit_plan=_exit_plan(s, spot, cpe)) for s in scenarios]

    vetoes = _vetoes(direction, o, flip_v, spot, vol)

    n = len(vetoes)
    _ARMED = ("结构条件已满足", "触发观察中", "破位日", "已破位", "追认成立")
    armed = any(s.status.startswith(_ARMED) for s in scenarios)
    holding = any(s.status.startswith("追认成立") for s in scenarios)
    if not scenarios:
        verdict = f"方向为{direction}但期权结构缺失（无墙/无零伽马）——模块无法参数化，仅按综合研判观察。"
    elif holding:
        # 持仓生命周期优先：追认已完成 = 持仓管理语境，否决票在此语境下是减仓/警惕信号，
        # 不再用"等触发"话术（那是新开仓语境）——两种语境混用曾致卡内自相矛盾
        veto_part = (f"实时层现有 {n} 张否决票提示环境转变——存量仓按 🎯 止盈止损从严管理，"
                     f"逢反弹主动减仓；" if n else "无否决票——存量仓按 🎯 止盈止损正常管理；")
        verdict = f"持仓管理模式：顺结构{direction}的追认已在历史上完成。{veto_part}新开仓一律等新触发，不追。"
    elif n >= 2:
        verdict = (f"今日无有效{direction}信号：实时层给出 {n} 张否决票。"
                   f"模块判定——不开枪，等待情景触发（否决票消失或顺结构情景确认）。")
    elif n == 1:
        verdict = (f"{direction}信号偏弱：存在 1 张否决票，"
                   f"仅顺结构情景（零伽马翻转追认）触发后才值得跟进。")
    elif armed:
        verdict = f"{direction}结构信号在位：无否决票，且有情景进入触发观察——按确认条件执行核对。"
    else:
        verdict = f"无否决票，但各情景均未触发——{direction}方向成立而时机未到，等待。"

    if atr_pct is not None and atr_pct > ATR_BASELINE_PCT * 1.05:  # 留 5% 死区，避免"缩至 1/1.0"的废话
        ratio = atr_pct / ATR_BASELINE_PCT
        sizing = (f"仓位规则：风险单位按 ATR 缩放——当前日波幅 {atr_pct:.1f}%，"
                  f"为基准 {ATR_BASELINE_PCT:.0f}% 的 {ratio:.1f} 倍，"
                  f"同等账户风险下仓位应缩至基准的 1/{ratio:.1f}。")
    elif atr_pct is not None:
        sizing = f"仓位规则：当前日波幅 {atr_pct:.1f}%（≤基准 {ATR_BASELINE_PCT:.0f}%），常规风险单位即可。"
    else:
        sizing = "仓位规则：无波幅数据，缓冲区按现价百分比退化处理，仓位宜更保守。"

    return StrategyPlan(
        direction=direction, direction_source=dir_src, spot=spot, unit=unit,
        atr=atr, atr_pct=atr_pct, atr_note=atr_note,
        vetoes=vetoes, scenarios=scenarios, verdict=verdict,
        sizing_note=sizing, caveats=caveats)
