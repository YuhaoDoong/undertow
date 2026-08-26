"""超买超卖 —— 两个正交维度，全部以 ATR 为单位，全部经回测校准。

**两个维度，各自回答一个不同的问题**

  1. **偏离度** `(现价 − MA20) / ATR14` —— "离常态多远"
  2. **回撤度** `(现价 − 过去60日最高价) / ATR14` —— "从近期高点掉下来多少"

它们不是一回事。2026-08-26 的 QQQ 是最干净的反例：从 8/17 高点 734.58 跌到
710.72（-3.25%，回撤分位 **6%**），但 MA20=712.16，现价几乎正好压在均线上
（偏离分位 **34%**）。只看偏离度会判"中性"，漏掉这次回撤；只看回撤度会判"超卖"，
漏掉"其实只是跌回到最近的成交区间"。

**为什么是这两个、而不是五个**

同一批样本上量过分位序列的相关性（样本量见 CALIB_META）：

        偏离度   回撤60   回撤20   区间位置
  偏离度  1.00    0.73    0.91    0.95
  回撤60  0.73    1.00    0.75    0.74

回撤20 与"20日区间位置"跟偏离度是 0.91/0.95 —— **同一件事换个写法**，加进来
只会重蹈过热分的覆辙（那五个分量彼此 0.79~0.93，是一个指标数了四遍）。
只有 60 日回撤是 0.73，才值得作为第二个维度。合并确有增益：单用各 +0.93/+0.96pp，
分位均值 ≤10% 则 +1.17pp 且 t 从 3.99/4.67 升到 **4.86**。

**为什么用 ATR 归一**：让读数跨品种、跨波动率环境可比（白银 IV 是黄金两倍，同样
3% 的偏离意义完全不同）；且 ATR 正是期权盈亏平衡距离的天然单位——"离均线 2.5 个
ATR"可以直接换算成"买方需要标的走多远"。两个维度都是价格距离之比，**量纲自动
抵消**，所以能从标的直接搬到杠杆 ETF（NQ=F 的读数 ≈ TQQQ 的读数）。位点要换算，
这两个读数不用。

**已知边界（回测实测，不是猜的）**

  * **日线有效，1H / 4H 是噪音**。1H(61k 样本)/4H(12.7k 样本)上各候选信号的高低位
    分离度全在 ±0.2pp 以内且符号不稳定。别在更短周期用。

  * **只有超卖侧稳定过得了显著性**。14 格里 5 格 |t|≥2，其中 4 格在超卖侧
    （极超卖-牛 t=3.16、强超卖-牛 t=3.25、极超卖-熊 t=2.30、强超卖-熊 t=2.07）。
    超买侧只有"强超买-熊"过线，而它的**检验样本量只有 36**（触发 202 次是重叠计数，
    别当证据规模）；其余方向对、大体单调，但 t 在
    -0.7 ~ -1.8 之间晃，**这种不稳定本身就是"边缘很薄"的证据**。超买只能读作
    「追高性价比差」，不能读作「要反转」。

  * **准确率要看清是哪一种**。超卖档"跑赢什么都不做"的比率 60~69%（基准 47~58%），
    但**绝对方向准确率只有 56~60%（基准 55~56%）——提升仅 1~5pp**。前者高是因为
    它扣掉了局部漂移；别把 65% 当成"六成半的时候会涨"。幅度上 5 日 +0.7~1.5pp，
    **对期权买方而言这个量级很容易被点差与 theta 吃光**。

  * **两维分歧时边缘腰斩**。同一批样本上：
        两维都超卖（一致）    n=1817  +1.143pp  跑赢率 63%  t=+3.67
        回撤深但偏离不深      n=1271  +0.455pp  跑赢率 57%  t=+1.72
        偏离深但回撤不深      n= 485  +0.468pp  跑赢率 60%  t=+0.10
        两维都超买（一致）    n=1767  -0.419pp  跑赢率 48%  t=-1.61
    分歧组的边缘只有一致组的四成、且都不显著。**分歧要读作"信号变弱"，
    不是"两个信号里挑一个信"。**

  * 历史教训：早先版本用单样本 t（检验"本桶超额≠0"）而非双样本 t（检验"本桶≠什么
    都不做"），把噪音判成显著。改 CALIB 一律用 `undertow backtest-stretch --emit`
    重跑，不要手改数字。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

MA_N = 20          # 偏离度的基准均线
ATR_N = 14         # 波动率单位
DD_LOOK = 60       # 回撤度的回看窗口（实测 60 日优于 20 日，且与偏离度更正交）
PCT_WINDOW = 250   # 滚动分位窗口（约一年）
REGIME_N = 200     # 牛熊分界均线
MIN_BARS = REGIME_N + 30

# 分位分档（上界，左闭右开）。与回测的分桶一一对应。
BANDS = [
    (0.05, "极超卖"), (0.10, "强超卖"), (0.25, "偏超卖"),
    (0.75, "中性"), (0.90, "偏超买"), (0.95, "强超买"), (1.01, "极超买"),
]

# 两维分歧的判定：一维进最低/最高 10%，另一维却还在 25%~75% 之外的另一侧
DIVERGE_EXTREME = 0.10
DIVERGE_FLAT = 0.25

# ── 回测校准表 ──────────────────────────────────────────────────────────
# 来源：undertow backtest-stretch --emit
#   面板 GLD/SLV/USO/QQQ/SPY 日线，1993→2026，约 3 万样本
#   信号 = (偏离度分位 + 回撤度分位) / 2
#   口径 = +5日收益 − 过去60日局部漂移 − 同 regime 中性桶（即"比什么都不做多赚多少"）
#   t    = Welch 双样本、不重叠子样本（每 5 根取 1 根）
# 数值单位：百分点(pp)。改任何指标参数后必须重跑本表，别手改。
CALIB_ASOF = "2026-08-26"
# 校准产物的元数据。**散文里不要再写死样本数**——面板每天多几根，写死必然过期
# （codex review 2026-08-26 抓到源码里同时存在 29,522 / 29,422 两个数，实际已是 29,477）。
# 任何要引用样本量的地方，一律读这里；重跑 backtest-stretch --emit 时一并更新。
CALIB_META = {
    "asof": "2026-08-26",
    "panel": "GLD/SLV/USO/QQQ/SPY 日线，最长 1993→2026",
    "n_total": 29477,          # 全部样本（重叠）
    "n_test_total": 5964,      # Welch t 实际用的不重叠子样本合计
    "mode": "combo",           # 两维分位均值
    "horizon": 5,
    # ⚠️ 显著性检验用的是【不重叠子样本】，约为 n_total 的 1/horizon。
    # 表里的「触发」是重叠计数，不是检验样本量——两者相差 5 倍，别拿前者当证据规模。
    "n_test_ratio": 0.2,
    # 方向准确率（另一套口径：N日后收盘是否更高，不去趋势）
    "dir_acc": {"极超卖_5d": 61.8, "基准_5d": 55.5, "极超买_5d_跌": 42.2, "基准_5d_跌": 44.2},
    # ⚠️ 已知统计局限，必须与结论一同呈现：
    "caveats": [
        "跨资产不独立：GLD/SLV 与 QQQ/SPY 同日高度相关，池化为独立观测会高估显著性；"
        "临界结果（|t| 2.0~2.5）尤其不稳，应按日期做 cluster 稳健标准误后再下结论。",
        "模型选择偏差：combo/stretch/drawdown 三种口径在同一面板上比较后选了 t 最高的 combo，"
        "再把同一个 t 当作显著性证据——这是选择后偏差，真实显著性低于表面值。",
        "以上两点意味着：本表适合用来【排除没有边缘的做法】，不适合用来【证明某个做法有效】。",
    ],
}
# (5日边缘pp, 跑赢中性桶%, 触发次数, Welch双样本t, 检验样本量)
# ⚠️ 第2项是【跑赢同 regime 中性桶】的比例，与 edge_pp 同源；
#    不是「跑赢局部漂移」（后者在熊市会系统性偏高）。
# ⚠️ 第3项「触发」是重叠计数，第5项才是 t 实际用的不重叠样本量——两者差约 5 倍，
#    引用证据规模时用第5项。（codex review 2026-08-26）
CALIB: dict[tuple[str, str], tuple[float, float, int, float, int]] = {}
_CALIB_RAW = """
极超卖 牛 +1.364 69 471 3.16 102
强超卖 牛 +0.725 65 574 3.25 109
偏超卖 牛 +0.369 60 2250 1.36 454
中性 牛 +0.000 54 10276 0.00 2071
偏超买 牛 -0.341 48 3627 -1.32 682
强超买 牛 -0.293 49 1225 -1.81 236
极超买 牛 -0.416 49 1021 -1.17 221
极超卖 熊 +1.501 63 766 2.30 155
强超卖 熊 +0.752 56 781 2.07 146
偏超卖 熊 +0.121 52 2167 1.10 437
中性 熊 +0.000 50 5139 0.00 1048
偏超买 熊 -0.424 45 830 -0.61 171
强超买 熊 -0.519 42 202 -2.10 36
极超买 熊 -1.062 37 148 -1.71 30
"""
for _ln in _CALIB_RAW.strip().splitlines():
    # 容忍 6 列（旧格式，无检验样本量）与 7 列（新格式）——升级期间不至于整个模块导入失败
    _p = _ln.split()
    _b, _r, _e, _w, _n, _t = _p[:6]
    _nt = _p[6] if len(_p) > 6 else 0
    CALIB[(_b, _r)] = (float(_e), float(_w), int(_n), float(_t), int(_nt))

T_SIGNIFICANT = 2.0    # |t| ≥ 此值才当作可用信号；否则标"不显著"


@dataclass(frozen=True)
class StretchRead:
    ok: bool
    # —— 维度一：偏离度（离 MA20 几个 ATR）——
    stretch: float | None = None
    stretch_pctile: float | None = None
    # —— 维度二：回撤度（距 DD_LOOK 日最高价几个 ATR）——
    drawdown: float | None = None          # 以 ATR 为单位，≤0
    drawdown_pct: float | None = None      # 百分比，直观用
    dd_pctile: float | None = None
    # —— 合成 ——
    pctile: float | None = None            # 两维分位均值，档位由它决定
    band: str = ""
    regime: str = ""                       # 牛 / 熊（MA200 之上/之下）
    diverge: str = ""                      # 两维分歧的说明（无分歧则空）
    ma20: float | None = None
    ma200: float | None = None
    atr: float | None = None
    high_n: float | None = None            # DD_LOOK 日最高价
    edge_pp: float | None = None
    win_rate: float | None = None
    n_hist: int | None = None
    t_stat: float | None = None
    reliable: bool = False
    headline: str = ""
    note: str = ""


def _sma(xs, n):
    return statistics.fmean(xs[-n:]) if len(xs) >= n else None


def _atr(highs, lows, closes, n=ATR_N):
    if len(closes) < n + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    return statistics.fmean(trs[-n:])


def _atr_series(highs, lows, closes, n=ATR_N):
    tr = [None] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    out = [None] * len(closes)
    run = 0.0
    for i in range(1, len(closes)):
        run += tr[i]
        if i > n:
            run -= tr[i - n]
        if i >= n:
            out[i] = run / n
    return out


def stretch_series(highs, lows, closes, ma_n=MA_N, atr_n=ATR_N) -> list:
    """偏离度序列：(收盘 − MA20) / ATR14。第 i 位只用 ≤i 的数据，无前视。"""
    n = len(closes)
    out: list = [None] * n
    atr = _atr_series(highs, lows, closes, atr_n)
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= ma_n:
            s -= closes[i - ma_n]
        if i >= ma_n - 1 and atr[i]:
            out[i] = (closes[i] - s / ma_n) / atr[i]
    return out


def drawdown_series(highs, lows, closes, look=DD_LOOK, atr_n=ATR_N) -> list:
    """回撤度序列：(收盘 − 过去 look 根最高价) / ATR14，恒 ≤0。无前视。"""
    n = len(closes)
    out: list = [None] * n
    atr = _atr_series(highs, lows, closes, atr_n)
    for i in range(look, n):
        if atr[i]:
            out[i] = (closes[i] - max(highs[i - look + 1:i + 1])) / atr[i]
    return out


def pct_rank_last(series, window=PCT_WINDOW) -> float | None:
    """末值在过去 window 个有效值中的百分位（0~1）。窗口不足一半则返回 None。"""
    if not series or series[-1] is None:
        return None
    hist = [x for x in series[max(0, len(series) - 1 - window):-1] if x is not None]
    if len(hist) < window // 2:
        return None
    return sum(1 for x in hist if x < series[-1]) / len(hist)


def band_of(pctile: float) -> str:
    for hi, name in BANDS:
        if pctile < hi:
            return name
    return "极超买"


def _diverge_note(p_stretch: float, p_dd: float) -> str:
    """两维分歧的判定与说明。分歧 = 信号变弱，不是二选一。"""
    if p_dd <= DIVERGE_EXTREME and p_stretch > DIVERGE_FLAT:
        return ("回撤深但偏离度不深：从近期高点掉了不少，价格却还贴着 MA20——"
                "更像「前高是尖顶、现已跌回原区间」，不是「被打穿」。"
                "历史上这种组合 5 日边缘 +0.46pp、t=1.72 不显著，"
                "约为两维都超卖时（+1.14pp, t=3.67）的四成")
    if p_stretch <= DIVERGE_EXTREME and p_dd > DIVERGE_FLAT:
        return ("偏离度深但回撤不深：跌破均线不少，却离 60 日高点不远——"
                "多见于高位横盘后的第一次下探。历史 5 日边缘 +0.47pp 但 t=0.10，"
                "基本是噪音，别当成充分回调")
    if p_dd >= 1 - DIVERGE_EXTREME and p_stretch < 1 - DIVERGE_FLAT:
        return "回撤度已创新高但偏离度平平：接近前高但涨势不陡，属「磨」上去的"
    if p_stretch >= 1 - DIVERGE_EXTREME and p_dd < 1 - DIVERGE_FLAT:
        return "偏离度极高但距前高仍远：反弹很陡但远未收复失地，是反弹不是新高"
    return ""


def analyze_stretch(series) -> StretchRead:
    """从 PriceSeries 算两维超买超卖读数 + 回测校准解读。"""
    if series is None or len(series.closes) < MIN_BARS:
        got = 0 if series is None else len(series.closes)
        return StretchRead(ok=False, note=f"价序 {got} 根 < {MIN_BARS}，超买超卖跳过"
                                          f"（需 MA200 定 regime + 分位窗口）")
    c = series.closes
    h = series.highs if (series.highs and len(series.highs) == len(c)) else c
    lo = series.lows if (series.lows and len(series.lows) == len(c)) else c

    ss, dds = stretch_series(h, lo, c), drawdown_series(h, lo, c)
    v_s, p_s = ss[-1], pct_rank_last(ss)
    v_d, p_d = dds[-1], pct_rank_last(dds)
    ma20, ma200, atr = _sma(c, MA_N), _sma(c, REGIME_N), _atr(h, lo, c)
    if None in (v_s, p_s, v_d, p_d, ma200):
        return StretchRead(ok=False, note="偏离度/回撤度或其滚动分位数据不足")

    hi_n = max(h[-DD_LOOK:])
    pct = (p_s + p_d) / 2.0
    regime = "牛" if c[-1] > ma200 else "熊"
    band = band_of(pct)
    cal = CALIB.get((band, regime))
    edge, wr, nh, t, n_test = cal if cal else (None, None, None, None, None)
    reliable = bool(t is not None and abs(t) >= T_SIGNIFICANT)
    diverge = _diverge_note(p_s, p_d)

    if band == "中性":
        head = f"{band}（合并分位 {pct*100:.0f}%）—— 无方向性边缘"
    elif edge is None:
        head = f"{band}（合并分位 {pct*100:.0f}%）—— 无校准数据"
    elif not reliable:
        head = (f"{band}·{regime}市（合并分位 {pct*100:.0f}%）—— 历史边缘 {edge:+.2f}pp/5日，"
                f"但 t={t:.2f} 未达显著，**当参考不当信号**")
    elif edge > 0:
        head = (f"{band}·{regime}市（合并分位 {pct*100:.0f}%）—— 历史上此后 5 日跑赢"
                f"同 regime 中性桶 {edge:+.2f}pp，跑赢比例 {wr:.0f}%"
                f"（触发 {nh} 次 / 检验 n={n_test}, t={t:.2f}）")
    else:
        head = (f"{band}·{regime}市（合并分位 {pct*100:.0f}%）—— 历史上此后 5 日跑输"
                f"{edge:+.2f}pp（触发 {nh} 次 / 检验 n={n_test}, t={t:.2f}）；"
                f"**是「追高性价比差」，不是「要跌了」**")

    note = (f"偏离度 (现价−MA{MA_N})/ATR{ATR_N} = {v_s:+.2f}（分位 {p_s*100:.0f}%）　|　"
            f"回撤度 距{DD_LOOK}日高 {(c[-1]/hi_n-1)*100:+.2f}% = {v_d:+.2f} 个 ATR"
            f"（分位 {p_d*100:.0f}%）")
    return StretchRead(ok=True, stretch=v_s, stretch_pctile=p_s,
                       drawdown=v_d, drawdown_pct=(c[-1] / hi_n - 1) * 100, dd_pctile=p_d,
                       pctile=pct, band=band, regime=regime, diverge=diverge,
                       ma20=ma20, ma200=ma200, atr=atr, high_n=hi_n,
                       edge_pp=edge, win_rate=wr, n_hist=nh, t_stat=t,
                       reliable=reliable, headline=head, note=note)


def render_md(sr: StretchRead, display_name: str = "") -> str:
    if not sr.ok:
        return f"- 超买超卖：{sr.note}"
    L = [f"**超买超卖**{('（' + display_name + '）') if display_name else ''}：{sr.headline}",
         "",
         f"- 偏离度：现价距 MA{MA_N} **{sr.stretch:+.2f} 个 ATR**"
         f"（MA{MA_N}={sr.ma20:.2f}，ATR{ATR_N}={sr.atr:.2f}）→ 分位 **{sr.stretch_pctile*100:.0f}%**",
         f"- 回撤度：距 {DD_LOOK} 日高 {sr.high_n:.2f} 为 **{sr.drawdown_pct:+.2f}%**"
         f"（{sr.drawdown:+.2f} 个 ATR）→ 分位 **{sr.dd_pctile*100:.0f}%**",
         f"- 合并分位：**{sr.pctile*100:.0f}%** → {sr.band}",
         f"- Regime：MA{REGIME_N}={sr.ma200:.2f} → **{sr.regime}市**"]
    if sr.diverge:
        L.append(f"- ⚠️ **两维分歧**：{sr.diverge}")
    if sr.edge_pp is not None and sr.band != "中性":
        flag = "" if sr.reliable else f"（t={sr.t_stat:+.2f} 未达 2.0，参考级）"
        L.append(f"- 回测校准（{CALIB_ASOF}）：5 日边缘 **{sr.edge_pp:+.2f}pp**、"
                 f"跑赢中性桶 {sr.win_rate:.0f}%、触发 {sr.n_hist} 次{flag}")
    L.append("")
    L.append("> 口径：边缘 = +5日收益 − 过去60日局部漂移 − 同 regime 中性桶，即「比什么都不做多赚多少」。")
    L.append("> 「跑赢中性桶」不是「上涨概率」：超卖档绝对方向准确率仅 56~60%（基准 55~56%）。")
    L.append("> ⚠️ 显著性有两条无法自证的局限（跨资产不独立、模型选择偏差），"
             "详见 stretch.CALIB_META['caveats']——本表宜用于排除无边缘的做法，不宜用于证明有效。")
    L.append("> **本指标只在日线成立**：1H/4H 回测分离度 ±0.2pp 且符号不稳定，属噪音。")
    return "\n".join(L)
