"""技术指标层（纯确定性计算，无 I/O）——补"短线过热度 + 趋势结构"。

从已有价格序列（PriceSeries：dates/closes/highs/lows）算经典技术指标，给出：
  - 趋势结构：均线多头/空头排列（MA5/10/20/30）
  - 短线过热度：RSI / KDJ-J / CCI / BIAS / 布林位置 综合成一个超买-超卖分
  - MACD、多窗口涨幅、ATR

定位：与期权结构层【正交】——结构层答"谁在博弈/墙在哪"，技术层答"短线过不过热/趋势结构"，
两层交叉印证（如"结构偏多 + 短线超买"）。**只作波段级参考、非投资建议**；确定性、LLM 不碰算术。
口径统一用【传入序列本身】（ETF 或商品价），不跨口径混算。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


def _sma(xs: list[float], n: int) -> float | None:
    return statistics.fmean(xs[-n:]) if len(xs) >= n else None


def _ema_series(xs: list[float], n: int) -> list[float]:
    if not xs:
        return []
    k = 2.0 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def _rsi(closes: list[float], n: int) -> float | None:
    """通达信口径 RSI：n 周期涨跌幅简单均值。"""
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = statistics.fmean(gains[-n:])
    al = statistics.fmean(losses[-n:])
    if ag + al == 0:
        return 50.0
    return 100.0 * ag / (ag + al)


def _kdj(highs, lows, closes, n=9):
    """KDJ：RSV(9) → K/D 各 1/3 平滑 → J=3K−2D。返回末值 (K,D,J)。"""
    if len(closes) < n:
        return None
    k, d = 50.0, 50.0
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = 100.0 * (closes[i] - ll) / (hh - ll) if hh > ll else 50.0
        k = 2.0 / 3 * k + 1.0 / 3 * rsv
        d = 2.0 / 3 * d + 1.0 / 3 * k
    return k, d, 3 * k - 2 * d


def _macd(closes, fast=12, slow=26, signal=9):
    """MACD：DIF=EMA12−EMA26，DEA=EMA9(DIF)，柱=2(DIF−DEA)。返回末值 (dif,dea,hist)。"""
    if len(closes) < slow:
        return None
    ef, es = _ema_series(closes, fast), _ema_series(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = _ema_series(dif, signal)
    return dif[-1], dea[-1], 2 * (dif[-1] - dea[-1])


def _cci(highs, lows, closes, n=14):
    if len(closes) < n:
        return None
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    ma = statistics.fmean(tp[-n:])
    md = statistics.fmean([abs(x - ma) for x in tp[-n:]])
    return (tp[-1] - ma) / (0.015 * md) if md > 0 else 0.0


def _bollinger(closes, n=20, k=2.0):
    if len(closes) < n:
        return None
    ma = statistics.fmean(closes[-n:])
    sd = statistics.pstdev(closes[-n:])
    up, lo = ma + k * sd, ma - k * sd
    pctb = (closes[-1] - lo) / (up - lo) if up > lo else 0.5
    return ma, up, lo, pctb


def _atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return statistics.fmean(trs[-n:])


def _ret(closes, n):
    if len(closes) <= n or closes[-n - 1] == 0:
        return None
    return (closes[-1] / closes[-n - 1] - 1.0) * 100


@dataclass(frozen=True)
class TechnicalRead:
    ok: bool
    spot: float = 0.0
    ma: dict = field(default_factory=dict)      # {5,10,20,30}
    trend: str = ""                              # 多头排列 / 空头排列 / 纠缠
    rsi6: float | None = None
    rsi14: float | None = None
    kdj: tuple | None = None                     # (K,D,J)
    macd: tuple | None = None                    # (dif,dea,hist)
    cci: float | None = None
    boll: tuple | None = None                    # (ma,up,lo,%b)
    bias6: float | None = None
    psy: float | None = None
    atr: float | None = None
    rets: dict = field(default_factory=dict)     # {5,10,20}
    heat_score: int = 0                          # 正=超买 负=超卖
    heat: str = "中性"                           # 强超买/偏超买/中性/偏超卖/强超卖
    headline: str = ""
    note: str = ""


def render_md(tr: "TechnicalRead", display_name: str = "") -> str:
    """技术面终端 Markdown。"""
    L = [f"## {display_name} — 技术面（短线过热度 + 趋势结构）"]
    if not tr.ok:
        L.append(f"- {tr.note}")
        return "\n".join(L)
    L.append(f"**{tr.headline}**  ·  现价 {tr.spot:.2f}  ·  过热分 {tr.heat_score:+d}（{tr.heat}）")
    ma = "、".join(f"MA{n} {tr.ma[n]:.1f}" for n in (5, 10, 20, 30) if tr.ma.get(n))
    L.append(f"- 均线：{ma} → **{tr.trend}**")
    osc = []
    if tr.rsi6 is not None:
        osc.append(f"RSI6 {tr.rsi6:.0f}/RSI14 {tr.rsi14:.0f}")
    if tr.kdj:
        osc.append(f"KDJ K{tr.kdj[0]:.0f}/D{tr.kdj[1]:.0f}/J{tr.kdj[2]:.0f}")
    if tr.cci is not None:
        osc.append(f"CCI {tr.cci:.0f}")
    if tr.bias6 is not None:
        osc.append(f"BIAS6 {tr.bias6:+.1f}%")
    L.append(f"- 摆动指标：{' · '.join(osc)}")
    if tr.boll:
        L.append(f"- 布林：中轨 {tr.boll[0]:.1f} / 上轨 {tr.boll[1]:.1f} / 下轨 {tr.boll[2]:.1f}（%b {tr.boll[3]:.2f}）")
    if tr.macd:
        L.append(f"- MACD：DIF {tr.macd[0]:+.2f} / DEA {tr.macd[1]:+.2f} / 柱 {tr.macd[2]:+.2f}")
    r = tr.rets
    rt = "、".join(f"{n}日 {r[n]:+.1f}%" for n in (5, 10, 20) if r.get(n) is not None)
    if rt:
        L.append(f"- 多窗口涨幅：{rt}" + (f" · ATR {tr.atr:.2f}" if tr.atr else ""))
    L.append("> 技术面与期权结构层正交，作短线过热度/趋势结构的交叉印证；非投资建议。")
    return "\n".join(L)


def _heat_label(score: int) -> str:
    if score >= 4:
        return "强超买"
    if score >= 2:
        return "偏超买"
    if score <= -4:
        return "强超卖"
    if score <= -2:
        return "偏超卖"
    return "中性"


def analyze_technicals(series) -> TechnicalRead:
    """从 PriceSeries 算技术面读数。数据不足则 ok=False。"""
    if series is None or len(series.closes) < 30:
        return TechnicalRead(ok=False, note="价序不足 30 根，技术指标跳过")
    c = series.closes
    h = series.highs if (series.highs and len(series.highs) == len(c)) else c
    lo = series.lows if (series.lows and len(series.lows) == len(c)) else c
    spot = c[-1]

    ma = {n: _sma(c, n) for n in (5, 10, 20, 30)}
    if all(ma[n] is not None for n in (5, 10, 20, 30)):
        if ma[5] > ma[10] > ma[20] > ma[30]:
            trend = "多头排列"
        elif ma[5] < ma[10] < ma[20] < ma[30]:
            trend = "空头排列"
        else:
            trend = "纠缠"
    else:
        trend = "纠缠"

    rsi6, rsi14 = _rsi(c, 6), _rsi(c, 14)
    kdj = _kdj(h, lo, c)
    macd = _macd(c)
    cci = _cci(h, lo, c)
    boll = _bollinger(c)
    bias6 = ((spot - ma[5]) / ma[5] * 100) if ma.get(5) else None
    psy = (sum(1 for i in range(len(c) - 12, len(c)) if c[i] > c[i - 1]) / 12 * 100) \
        if len(c) >= 13 else None
    atr = _atr(h, lo, c)
    rets = {n: _ret(c, n) for n in (5, 10, 20)}

    # 综合超买-超卖分
    s = 0
    if rsi6 is not None:
        s += 2 if rsi6 >= 80 else (1 if rsi6 >= 70 else (-2 if rsi6 <= 20 else (-1 if rsi6 <= 30 else 0)))
    if kdj is not None:
        j = kdj[2]
        s += 2 if j >= 90 else (1 if j >= 80 else (-2 if j <= 0 else (-1 if j <= 20 else 0)))
    if cci is not None:
        s += 2 if cci >= 200 else (1 if cci >= 100 else (-2 if cci <= -200 else (-1 if cci <= -100 else 0)))
    if boll is not None:
        pctb = boll[3]
        s += 1 if pctb >= 1.0 else (-1 if pctb <= 0.0 else 0)
    if bias6 is not None:
        s += 1 if bias6 >= 6 else (-1 if bias6 <= -6 else 0)
    heat = _heat_label(s)

    # 一句话
    bits = [trend]
    if heat != "中性":
        detail = []
        if rsi6 is not None:
            detail.append(f"RSI6 {rsi6:.0f}")
        if kdj is not None:
            detail.append(f"KDJ-J {kdj[2]:.0f}")
        if boll is not None and boll[3] >= 0.95:
            detail.append("贴布林上轨")
        elif boll is not None and boll[3] <= 0.05:
            detail.append("贴布林下轨")
        bits.append(f"短线{heat}（{'/'.join(detail)}）" if detail else f"短线{heat}")
    else:
        bits.append("短线中性")
    macd_txt = ""
    if macd is not None:
        macd_txt = "MACD 零轴上方" if macd[0] > 0 else "MACD 零轴下方"
        bits.append(macd_txt)
    headline = " · ".join(bits)

    return TechnicalRead(
        ok=True, spot=spot, ma=ma, trend=trend, rsi6=rsi6, rsi14=rsi14, kdj=kdj,
        macd=macd, cci=cci, boll=boll, bias6=bias6, psy=psy, atr=atr, rets=rets,
        heat_score=s, heat=heat, headline=headline)
