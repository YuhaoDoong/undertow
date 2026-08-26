"""拉伸度校准回测 —— 纯确定性，无 I/O（数据由调用方喂进来）。

**为什么这个模块必须存在**：`stretch.py` 里的 CALIB 表是一组具体数字，谁都可以
拍一个。有了本模块，那张表就是**可重跑、可证伪**的——改了指标参数（MA_N/ATR_N/
分档边界）就重跑，数字自己会变。任何"我觉得超买该止盈"式的断言都不该进代码。

**方法学上刻意做对的三件事**（都是踩过的坑）：

  1. **局部去趋势**：超额 = 前瞻收益 − 该品种【过去 DRIFT 日】的漂移，而不是减全样本
     均值。后者会把一整段牛市的上涨当成"基准外收益"白送给抄底信号——用 2 年金银数据
     测出来的"超卖很灵"就是这么来的假象。

  2. **同 regime 内比较**：熊市里局部漂移是负的，减掉它会让熊市样本的超额整体抬高
     （实测熊市中性桶 +0.46%、牛市中性桶 −0.42%）。所以边缘一律定义为
     【该桶 − 同 regime 中性桶】，跨 regime 才可比。

  3. **不重叠子样本 + Welch 双样本 t**：相邻交易日的前瞻窗口高度重叠，直接做 t 检验
     会把 n 虚增几倍。故每 horizon 根取 1 根，且 t 检验的对象必须与"边缘"同源
     （桶 vs 中性桶的均值差），不能拿原始超额的单样本 t 来冒充。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .stretch import (BANDS, DD_LOOK, MA_N, PCT_WINDOW, REGIME_N,
                      band_of, drawdown_series, stretch_series)

DRIFT_N = 60        # 局部漂移窗口
DEFAULT_HZ = (2, 3, 5, 10)
NEUTRAL = "中性"


def _sma_series(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _atr_series(highs, lows, closes, n=14):
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


def _pct_rank_series(xs, window=PCT_WINDOW):
    out = [None] * len(xs)
    for i in range(len(xs)):
        if xs[i] is None:
            continue
        hist = [x for x in xs[max(0, i - window):i] if x is not None]
        if len(hist) < window // 2:
            continue
        out[i] = sum(1 for x in hist if x < xs[i]) / len(hist)
    return out


def welch_t(a: list, b: list) -> float:
    """Welch 双样本 t（不假设等方差）。样本不足返回 0。"""
    if len(a) < 3 or len(b) < 3:
        return 0.0
    va, vb = statistics.variance(a), statistics.variance(b)
    den = math.sqrt(va / len(a) + vb / len(b))
    return (statistics.fmean(a) - statistics.fmean(b)) / den if den > 0 else 0.0


@dataclass(frozen=True)
class Sample:
    name: str
    i: int
    pctile: float                     # 合成分位（两维均值），档位由它决定
    regime: str                       # 牛 / 熊
    p_stretch: float = 0.0            # 维度一：偏离度分位
    p_dd: float = 0.0                 # 维度二：回撤度分位
    excess: dict = field(default_factory=dict)   # {horizon: 去趋势超额%}


# 可选信号口径，供对照实验用；默认 combo（实测 t 最高）
SIGNAL_MODES = ("combo", "stretch", "drawdown")


def build_samples(name, highs, lows, closes, *, horizons=DEFAULT_HZ,
                  warmup=None, drift_n=DRIFT_N, mode="combo") -> list:
    """把一条价序转成回测样本。第 i 个样本只用 ≤i 的数据算信号，无前视。

    mode 决定用哪个口径定档：
      combo    两维分位均值（默认）—— 实测 t=4.86，优于任一单维
      stretch  只用偏离度 (c−MA20)/ATR    —— t=3.99
      drawdown 只用回撤度 (c−60日高)/ATR  —— t=4.67
    """
    n = len(closes)
    warm = warmup if warmup is not None else max(PCT_WINDOW + DD_LOOK, REGIME_N + 50)
    if n < warm + max(horizons) + 10:
        return []
    p_s = _pct_rank_series(stretch_series(highs, lows, closes))
    p_d = _pct_rank_series(drawdown_series(highs, lows, closes))
    m200 = _sma_series(closes, REGIME_N)
    out = []
    for i in range(warm, n - max(horizons)):
        if p_s[i] is None or p_d[i] is None or m200[i] is None or i < drift_n:
            continue
        pct = {"combo": (p_s[i] + p_d[i]) / 2.0,
               "stretch": p_s[i], "drawdown": p_d[i]}[mode]
        d = (closes[i] / closes[i - drift_n] - 1) / drift_n      # 日均局部漂移
        out.append(Sample(name=name, i=i, pctile=pct,
                          regime="牛" if closes[i] > m200[i] else "熊",
                          p_stretch=p_s[i], p_dd=p_d[i],
                          excess={k: (closes[i + k] / closes[i] - 1) * 100 - d * k * 100
                                  for k in horizons}))
    return out


def diverge_stats(samples: list, *, horizon: int = 5,
                  extreme: float = 0.10, flat: float = 0.25) -> dict:
    """两维分歧 vs 一致时的边缘对比 —— 用来回答"分歧了该信谁"。

    结论应当是"都不太该信"：分歧组的边缘显著低于一致组。
    """
    neu = {}
    for rg in ("牛", "熊"):
        pool = [s.excess[horizon] for s in samples
                if s.regime == rg and 0.4 <= s.pctile <= 0.6]
        neu[rg] = statistics.fmean(pool) if len(pool) >= 50 else 0.0

    def stat(sel, label):
        t = [s for s in samples if sel(s)]
        if len(t) < 50:
            return None
        ex = [s.excess[horizon] - neu[s.regime] for s in t]
        nov = [s.excess[horizon] - neu[s.regime] for s in t if s.i % horizon == 0]
        base = [s.excess[horizon] - neu[s.regime] for s in samples
                if 0.4 <= s.pctile <= 0.6 and s.i % horizon == 0]
        return {"label": label, "n": len(t), "edge_pp": statistics.fmean(ex),
                "win_rate": sum(1 for x in ex if x > 0) / len(ex) * 100,
                "t": welch_t(nov, base)}

    rows = [
        stat(lambda s: s.p_dd <= extreme and s.p_stretch <= extreme, "两维都超卖（一致）"),
        stat(lambda s: s.p_dd <= extreme and s.p_stretch > flat, "回撤深但偏离不深（分歧）"),
        stat(lambda s: s.p_stretch <= extreme and s.p_dd > flat, "偏离深但回撤不深（分歧）"),
        stat(lambda s: s.p_dd >= 1 - extreme and s.p_stretch >= 1 - extreme, "两维都超买（一致）"),
    ]
    return {"rows": [r for r in rows if r]}


def calibrate(samples: list, *, horizon: int = 5, min_n: int = 50) -> dict:
    """按 (档位, regime) 算相对同 regime 中性桶的边缘 + Welch t。

    返回 {(band, regime): {"n","edge_pp","win_rate","t","mean_excess"}}。
    """
    out = {}
    for regime in ("牛", "熊"):
        pool = [s for s in samples if s.regime == regime]
        neu = [s for s in pool if band_of(s.pctile) == NEUTRAL]
        if len(neu) < min_n:
            continue
        neu_mean = statistics.fmean([s.excess[horizon] for s in neu])
        # 不重叠子样本：每 horizon 根取 1 根
        neu_nov = [s.excess[horizon] for s in neu if s.i % horizon == 0]
        for _, band in BANDS:
            tgt = [s for s in pool if band_of(s.pctile) == band]
            if len(tgt) < min_n:
                continue
            ex = [s.excess[horizon] for s in tgt]
            tgt_nov = [s.excess[horizon] for s in tgt if s.i % horizon == 0]
            out[(band, regime)] = {
                "n": len(tgt),
                "mean_excess": statistics.fmean(ex),
                # 边缘与 t 同源：都是"本桶 vs 同 regime 中性桶"的均值差
                "edge_pp": statistics.fmean(ex) - neu_mean,
                "win_rate": sum(1 for x in ex if x > 0) / len(ex) * 100,
                "t": 0.0 if band == NEUTRAL else welch_t(tgt_nov, neu_nov),
                "n_nov": len(tgt_nov),
            }
    return out


def render_table_md(cal: dict, *, horizon: int = 5, total: int = 0, span: str = "") -> str:
    L = [f"### 拉伸度校准表（+{horizon} 日）", ""]
    if total:
        L.append(f"*样本 {total:,}{('，' + span) if span else ''}；"
                 f"边缘 = 本桶 − 同 regime 中性桶（局部去趋势后）；t = Welch 双样本、不重叠子样本*")
        L.append("")
    L.append("| 档位 | regime | 触发 | 边缘 | 胜率 | t | 判定 |")
    L.append("|---|---|---:|---:|---:|---:|---|")
    order = [b for _, b in BANDS]
    for regime in ("牛", "熊"):
        for band in order:
            r = cal.get((band, regime))
            if not r:
                continue
            if band == NEUTRAL:
                verdict = "基准"
            elif abs(r["t"]) >= 2.0:
                verdict = "✅ 显著"
            elif abs(r["t"]) >= 1.5:
                verdict = "~ 边缘"
            else:
                verdict = "❌ 不显著"
            L.append(f"| {band} | {regime}市 | {r['n']} | **{r['edge_pp']:+.3f}pp** | "
                     f"{r['win_rate']:.0f}% | {r['t']:+.2f} | {verdict} |")
    return "\n".join(L)


def render_calib_literal(cal: dict) -> str:
    """输出可直接粘回 stretch.py CALIB 的 Python 字面量。"""
    L = ["CALIB = {"]
    for regime in ("牛", "熊"):
        for _, band in BANDS:
            r = cal.get((band, regime))
            if not r:
                continue
            L.append(f'    ("{band}", "{regime}"): '
                     f'({r["edge_pp"]:+.3f}, {r["win_rate"]:.0f}, {r["n"]}, {r["t"]:.2f}),')
    L.append("}")
    return "\n".join(L)


# ── 卖方口径：不突破率 ────────────────────────────────────────────────
# 买方问"会不会朝我这边走"，卖方问的是**"会不会继续朝反方向走"**——卖一张虚值 call，
# 只要价格不涨破行权价就赢，根本不需要它跌。所以对卖方价差而言，正确的准确率是
# 【不突破率】，而不是方向准确率。
#
# 两种口径都要给，因为它们对应两种真实风险：
#   * 终值不突破 —— 到期时是否在行权价之内（决定到期损益）
#   * 路径不突破 —— 期间是否一次都没碰到（决定被指派焦虑与止损出局）
# 路径口径必然更严格，两者之差就是"中途吓一跳但最后没事"的比例。
#
# 距离一律用 ATR 倍数表示：行权价该离多远本就该按波动率定，用百分比会让
# 白银(IV≈黄金两倍)和黄金的同一个数字含义完全不同。
DEFAULT_DISTANCES = (1.0, 1.5, 2.0, 3.0)     # 单位：ATR14


def containment_stats(name, highs, lows, closes, samples, *,
                      horizon: int = 5, distances=DEFAULT_DISTANCES,
                      atr_n: int = 14) -> dict:
    """各档位的【不突破率】：此后 horizon 日内，价格没有继续朝原方向走出 d 个 ATR。

    超买档看【向上】不突破（对应卖 call / 熊市看涨价差）；
    超卖档看【向下】不突破（对应卖 put / 牛市看跌价差）。
    中性桶同时给出两个方向，作为基准。
    """
    atr = _atr_series(highs, lows, closes, atr_n)
    out: dict = {}
    for s in samples:
        i = s.i
        if atr[i] is None or not atr[i] or i + horizon >= len(closes):
            continue
        base, a = closes[i], atr[i]
        end = closes[i + horizon]
        seg = closes[i + 1:i + horizon + 1]
        hi = max(highs[i + 1:i + horizon + 1])
        lo = min(lows[i + 1:i + horizon + 1])
        band = band_of(s.pctile)
        rec = out.setdefault(band, {"n": 0, "atr_pct": [],
                                    "up_end": {d: 0 for d in distances},
                                    "up_path": {d: 0 for d in distances},
                                    "dn_end": {d: 0 for d in distances},
                                    "dn_path": {d: 0 for d in distances}})
        rec["n"] += 1
        rec["atr_pct"].append(a / base * 100)
        for d in distances:
            up_k, dn_k = base + d * a, base - d * a
            if end < up_k:
                rec["up_end"][d] += 1
            if hi < up_k:
                rec["up_path"][d] += 1
            if end > dn_k:
                rec["dn_end"][d] += 1
            if lo > dn_k:
                rec["dn_path"][d] += 1
        _ = seg
    for rec in out.values():
        n = rec["n"] or 1
        for k in ("up_end", "up_path", "dn_end", "dn_path"):
            rec[k] = {d: v / n * 100 for d, v in rec[k].items()}
        rec["atr_pct"] = statistics.fmean(rec["atr_pct"]) if rec["atr_pct"] else 0.0
    return out


def merge_containment(parts: list) -> dict:
    """把多品种的 containment_stats 按样本数加权合并。"""
    acc: dict = {}
    for p in parts:
        for band, rec in p.items():
            a = acc.setdefault(band, {"n": 0, "atr_pct": 0.0,
                                      "up_end": {}, "up_path": {},
                                      "dn_end": {}, "dn_path": {}})
            w = rec["n"]
            a["atr_pct"] += rec["atr_pct"] * w
            for k in ("up_end", "up_path", "dn_end", "dn_path"):
                for d, v in rec[k].items():
                    a[k][d] = a[k].get(d, 0.0) + v * w
            a["n"] += w
    for a in acc.values():
        n = a["n"] or 1
        a["atr_pct"] /= n
        for k in ("up_end", "up_path", "dn_end", "dn_path"):
            a[k] = {d: v / n for d, v in a[k].items()}
    return acc


def render_containment_md(acc: dict, *, horizon: int, distances=DEFAULT_DISTANCES) -> str:
    """卖方视角表：超买档看向上不突破、超卖档看向下不突破，均与中性桶对照。"""
    order = [n for _, n in BANDS]
    neu = acc.get("中性")
    L = [f"### 不突破率（卖方口径，+{horizon} 日）", "",
         "*卖方赢的条件不是「方向猜对」，而是「价格不继续朝反方向走出去」。*",
         "*行权价距离用 ATR14 倍数表示——该离多远本就按波动率定，用百分比会让不同品种不可比。*", ""]

    def block(title, end_key, path_key, bands):
        L.append(f"**{title}**")
        L.append("")
        L.append("| 档位 | 样本 | " + " | ".join(
            f"{d:g}σ 终值 / 路径" for d in distances) + " |")
        L.append("|---|---:|" + "---:|" * len(distances))
        for b in bands:
            r = acc.get(b)
            if not r or r["n"] < 50:
                continue
            cells = []
            for d in distances:
                e, p = r[end_key][d], r[path_key][d]
                if neu and neu["n"] >= 50:
                    de = e - neu[end_key][d]
                    cells.append(f"{e:.1f}% / {p:.1f}%<br><small>({de:+.1f}pp)</small>")
                else:
                    cells.append(f"{e:.1f}% / {p:.1f}%")
            L.append(f"| {b} | {r['n']} | " + " | ".join(cells) + " |")
        if neu and neu["n"] >= 50:
            cells = [f"{neu[end_key][d]:.1f}% / {neu[path_key][d]:.1f}%" for d in distances]
            L.append(f"| *基准（中性桶）* | {neu['n']} | " + " | ".join(cells) + " |")
        L.append("")

    block("超买之后 · 向上不突破（对应卖 call / 熊市看涨价差）",
          "up_end", "up_path", [b for b in order if "超买" in b])
    block("超卖之后 · 向下不突破（对应卖 put / 牛市看跌价差）",
          "dn_end", "dn_path", [b for b in order if "超卖" in b])
    L.append("> 「终值」= 到期日收盘在行权价内（决定到期损益）；"
             "「路径」= 期间一次都没碰到（决定被指派焦虑与止损出局）。")
    L.append("> 括号内为相对中性桶的提升（终值口径）。**提升才是 edge，绝对值高只是因为虚值远。**")
    return "\n".join(L)


def horizon_profile(samples: list, *, bands=("极超卖", "极超买"),
                    horizons=DEFAULT_HZ) -> dict:
    """各档位在不同前瞻天数上的边缘 —— 看信号能持续多久。"""
    out = {}
    for band in bands:
        row = {}
        for k in horizons:
            tgt = [s.excess[k] for s in samples if band_of(s.pctile) == band]
            neu = [s.excess[k] for s in samples if band_of(s.pctile) == NEUTRAL]
            if len(tgt) < 50 or len(neu) < 50:
                continue
            row[k] = statistics.fmean(tgt) - statistics.fmean(neu)
        out[band] = row
    return out
