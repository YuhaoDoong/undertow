"""筹码区间方向研究 vp-v1（预登记 docs/prereg/2026-09-28_volume_profile_v1.md，已冻结）。纯函数，无 I/O。

把外部作者二「筹码区间 / 真空区 / 介入点第一段 / 恐慌止损」的看图方法翻译成客观规则：
  筹码分布：t−250…t−1 日线成交量按 [low, high] 等比例分摊到宽 0.25×ATR(t−1) 的价格箱；
  密集箱 ≥ 非零箱第 70 百分位，真空箱 ≤ 第 30 百分位；连续密集箱 = 密集区。
  H1 首次触及密集区后「先反弹 1 ATR」vs 同方向未触及任何密集区的对照日；
  H2 收盘在真空箱 vs 密集箱，其后 5 日波动（÷ATR%）之比；
  H3 密集区内放量长下影 → 5 日超额收益。
定义一字不改地来自预登记第 3、6 节；任何改动 = 新版本。未校准、未验证（claims：vp.v1，T3）。
"""
from __future__ import annotations

import math
import random
import statistics as st

LOOKBACK = 250
ATR_N = 14
BIN_ATR = 0.25
P_HI, P_LO = 70, 30
H1_ATR, H1_MAXDAYS = 1.0, 10
H2_DAYS = 5
H3_VOL_X, H3_WICK, H3_DAYS = 2.0, 0.6, 5
NONOVERLAP = 5
ALPHA = 0.05 / 6
THRESH = {"H1": 0.05, "H2": 1.15, "H3": 0.003}
MIN_EVENTS = 30
BOOT = {"block": 20, "iters": 5000, "seed": 20260928}


# ── 数据清洗（预登记第 6 节）──────────────────────────────────────────

def clean(rows: list[dict], *, start: str | None = None) -> list[dict]:
    out = []
    for r in rows:
        if start and r["date"] < start:
            continue
        o, h, l, c, v = (float(r[k]) for k in ("open", "high", "low", "close", "volume"))
        out.append({"date": r["date"], "o": o, "h": max(o, h, l, c), "l": min(o, h, l, c), "c": c, "v": v})
    return out


def atr_series(bars: list[dict]) -> list[float | None]:
    """atr[i] = 以 bars[i] 为最后一天的 14 日平均真实波幅（简单平均）；不足 14 个 TR → None。"""
    trs = [None]
    for i in range(1, len(bars)):
        b, pc = bars[i], bars[i - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    out = []
    for i in range(len(bars)):
        win = trs[max(1, i - ATR_N + 1):i + 1]
        out.append(sum(win) / ATR_N if len(win) == ATR_N else None)
    return out


# ── 筹码分布与区 ──────────────────────────────────────────────────────

def profile(bars: list[dict], i: int, w: float) -> dict[int, float]:
    """bars[i−250 : i]（不含 i）的成交量按价格分箱；箱号 = floor(价格 / w)。"""
    hist: dict[int, float] = {}
    for b in bars[i - LOOKBACK:i]:
        if b["v"] <= 0:
            continue
        lo, hi = b["l"], b["h"]
        k0, k1 = math.floor(lo / w), math.floor(hi / w)
        if k0 == k1 or hi <= lo:
            hist[k0] = hist.get(k0, 0.0) + b["v"]
            continue
        span = hi - lo
        for k in range(k0, k1 + 1):
            a, z = max(lo, k * w), min(hi, (k + 1) * w)
            if z > a:
                hist[k] = hist.get(k, 0.0) + b["v"] * (z - a) / span
    return hist


def _pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        return float("nan")
    x = (len(s) - 1) * p / 100.0
    f = math.floor(x)
    return s[f] if f + 1 >= len(s) else s[f] + (s[f + 1] - s[f]) * (x - f)


def thresholds(hist: dict[int, float]) -> tuple[float, float]:
    nz = [v for v in hist.values() if v > 0]
    return _pct(nz, P_HI), _pct(nz, P_LO)


def hvn_zones(hist: dict[int, float], w: float) -> list[tuple[float, float]]:
    """连续密集箱 → [(下沿, 上沿)]，按价格升序。"""
    hi_t, _ = thresholds(hist)
    ks = sorted(k for k, v in hist.items() if v >= hi_t and v > 0)
    zones, run = [], []
    for k in ks:
        if run and k != run[-1] + 1:
            zones.append((run[0] * w, (run[-1] + 1) * w)); run = []
        run.append(k)
    if run:
        zones.append((run[0] * w, (run[-1] + 1) * w))
    return zones


def bin_class(hist: dict[int, float], price: float, w: float) -> str:
    hi_t, lo_t = thresholds(hist)
    v = hist.get(math.floor(price / w), 0.0)
    return "HVN" if (v >= hi_t and v > 0) else ("LVN" if v <= lo_t else "MID")


# ── 事件与结果 ────────────────────────────────────────────────────────

def first_passage(bars, i, atr, up_is_rebound: bool) -> str:
    """以 bars[i] 收盘为基准，其后 ≤10 日收盘：先 ≥ c+ATR / 先 ≤ c−ATR / 都没有 → rebound / through / undecided。"""
    c = bars[i]["c"]
    for j in range(i + 1, min(len(bars), i + 1 + H1_MAXDAYS)):
        x = bars[j]["c"]
        if x >= c + H1_ATR * atr:
            return "rebound" if up_is_rebound else "through"
        if x <= c - H1_ATR * atr:
            return "through" if up_is_rebound else "rebound"
    return "undecided" if i + H1_MAXDAYS < len(bars) else "immature"


def scan(bars: list[dict], lo_idx: int, hi_idx: int) -> dict:
    """在 [lo_idx, hi_idx) 的日子上逐日计算 H1/H2/H3 的原始记录（不做检验）。"""
    atr = atr_series(bars)
    h1_ev, h1_ctrl, h2, h3, ret5 = [], [], [], [], []
    for i in range(max(lo_idx, LOOKBACK + ATR_N), hi_idx):
        b, pb = bars[i], bars[i - 1]
        a_prev, a_now = atr[i - 1], atr[i]
        if not a_prev or not a_now or b["v"] <= 0:
            continue
        w = BIN_ATR * a_prev
        hist = profile(bars, i, w)
        zones = hvn_zones(hist, w)
        c1 = pb["c"]
        if i + H3_DAYS < len(bars):
            ret5.append({"i": i, "date": b["date"], "r": bars[i + H3_DAYS]["c"] / b["c"] - 1})
        # H1
        cands = []
        for zlo, zhi in zones:
            if c1 > zhi and b["l"] <= zhi:
                cands.append((c1 - zhi, "above", zlo, zhi))
            elif c1 < zlo and b["h"] >= zlo:
                cands.append((zlo - c1, "below", zlo, zhi))
        touched_any = any(b["l"] <= zhi and b["h"] >= zlo for zlo, zhi in zones)
        if cands:
            _, side, zlo, zhi = min(cands)
            h1_ev.append({"i": i, "date": b["date"], "side": side,
                          "res": first_passage(bars, i, a_now, up_is_rebound=(side == "above"))})
        if not touched_any:
            if b["l"] < c1:
                h1_ctrl.append({"i": i, "date": b["date"], "side": "above",
                                "res": first_passage(bars, i, a_now, up_is_rebound=True)})
            if b["h"] > c1:
                h1_ctrl.append({"i": i, "date": b["date"], "side": "below",
                                "res": first_passage(bars, i, a_now, up_is_rebound=False)})
        # H2
        if i + H2_DAYS < len(bars):
            rs = [math.log(bars[j]["c"] / bars[j - 1]["c"]) for j in range(i + 1, i + 1 + H2_DAYS)]
            h2.append({"i": i, "date": b["date"], "cls": bin_class(hist, b["c"], w),
                       "y": st.stdev(rs) / (a_now / b["c"])})
        # H3
        rng = b["h"] - b["l"]
        prior = [x["v"] for x in bars[i - 20:i] if x["v"] > 0]
        if (rng > 0 and len(prior) == 20 and b["v"] >= H3_VOL_X * (sum(prior) / 20)
                and (min(b["o"], b["c"]) - b["l"]) >= H3_WICK * rng and b["c"] >= b["l"] + 0.5 * rng
                and any(zlo <= b["l"] <= zhi for zlo, zhi in zones) and i + H3_DAYS < len(bars)):
            h3.append({"i": i, "date": b["date"], "r": bars[i + H3_DAYS]["c"] / b["c"] - 1})
    return {"h1_ev": h1_ev, "h1_ctrl": h1_ctrl, "h2": h2, "h3": h3, "ret5": ret5,
            "sma200": _sma_flags(bars)}


def _sma_flags(bars):
    out, s = {}, 0.0
    for i, b in enumerate(bars):
        s += b["c"]
        if i >= 200:
            s -= bars[i - 200]["c"]
        if i >= 199:
            out[b["date"]] = "牛" if b["c"] >= s / 200 else "熊"
    return out


def nonoverlap(events: list[dict]) -> list[dict]:
    keep, last = [], -10 ** 9
    for e in sorted(events, key=lambda x: x["i"]):
        if e["i"] - last >= NONOVERLAP:
            keep.append(e); last = e["i"]
    return keep


# ── 统计 ──────────────────────────────────────────────────────────────

def _betacf(a, b, x):
    """正则化不完全 Beta 的连分式（Numerical Recipes betacf）。"""
    fpmin = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)),
                   -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            if abs(d) < fpmin:
                d = fpmin
            c = 1.0 + aa / c
            if abs(c) < fpmin:
                c = fpmin
            d = 1.0 / d
            de = d * c
            h *= de
        if abs(de - 1.0) < 3e-14:
            break
    return h


def _ibeta(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    bt = math.exp(lbt)
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: float) -> float:
    """Student t 单侧上尾 P(T > t)。"""
    x = df / (df + t * t)
    p2 = _ibeta(df / 2.0, 0.5, x)          # = P(|T| > |t|)
    return p2 / 2.0 if t > 0 else 1.0 - p2 / 2.0


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def test_h1(ev: list[dict], ctrl: list[dict]) -> dict:
    e = [x for x in nonoverlap(ev) if x["res"] in ("rebound", "through")]
    c = [x for x in ctrl if x["res"] in ("rebound", "through")]
    ne, nc = len(e), len(c)
    if ne == 0 or nc == 0:
        return {"n_events": ne, "n_ctrl": nc, "status": "insufficient"}
    pe = sum(x["res"] == "rebound" for x in e) / ne
    pc = sum(x["res"] == "rebound" for x in c) / nc
    pool = (pe * ne + pc * nc) / (ne + nc)
    se = math.sqrt(pool * (1 - pool) * (1 / ne + 1 / nc)) or float("nan")
    z = (pe - pc) / se if se == se and se > 0 else float("nan")
    return {"n_events": ne, "n_ctrl": nc, "rebound_events": pe, "rebound_ctrl": pc, "diff": pe - pc,
            "z": z, "p_one_sided": norm_sf(z) if z == z else None,
            "n_undecided_events": sum(x["res"] == "undecided" for x in nonoverlap(ev))}


def test_h2(h2: list[dict]) -> dict:
    lv = [x["y"] for x in h2 if x["cls"] == "LVN"]
    hv = [x["y"] for x in h2 if x["cls"] == "HVN"]
    if not lv or not hv:
        return {"n_lvn": len(lv), "n_hvn": len(hv), "status": "insufficient"}
    ratio = st.fmean(lv) / st.fmean(hv)
    rng = random.Random(BOOT["seed"])
    n, blk = len(h2), BOOT["block"]
    boots = []
    for _ in range(BOOT["iters"]):
        idx = []
        while len(idx) < n:
            s0 = rng.randrange(n)
            idx.extend((s0 + k) % n for k in range(blk))
        samp = [h2[k] for k in idx[:n]]
        a = [x["y"] for x in samp if x["cls"] == "LVN"]
        b = [x["y"] for x in samp if x["cls"] == "HVN"]
        if a and b:
            boots.append(st.fmean(a) / st.fmean(b))
    boots.sort()
    lo = boots[int(ALPHA * len(boots))] if boots else None
    p = sum(x <= 1.0 for x in boots) / len(boots) if boots else None
    return {"n_lvn": len(lv), "n_hvn": len(hv), "n_mid": sum(x["cls"] == "MID" for x in h2),
            "ratio": ratio, "lower_one_sided": lo, "p_boot_le1": p}


def test_h3(h3: list[dict], ret5: list[dict]) -> dict:
    ev = nonoverlap(h3)
    base = st.fmean(x["r"] for x in ret5) if ret5 else float("nan")
    up_base = sum(x["r"] > 0 for x in ret5) / len(ret5) if ret5 else float("nan")
    ex = [x["r"] - base for x in ev]
    if len(ex) < 2:
        return {"n_events": len(ex), "status": "insufficient"}
    m, s = st.fmean(ex), st.stdev(ex)
    t = m / (s / math.sqrt(len(ex))) if s > 0 else float("nan")
    return {"n_events": len(ex), "mean_excess": m, "t": t, "p_one_sided": t_sf(t, len(ex) - 1) if t == t else None,
            "win_rate": sum(x["r"] > 0 for x in ev) / len(ev), "base_up_rate": up_base, "base_mean_5d": base}


def verdict(name: str, res: dict) -> str:
    n = res.get("n_events", min(res.get("n_lvn", 0), res.get("n_hvn", 0)))
    if res.get("status") == "insufficient" or n < MIN_EVENTS:
        return "证据不足（事件数 < 30）"
    if name == "H2":
        eff, lo = res["ratio"], res["lower_one_sided"]
        sig = lo is not None and lo > 1.0
        if sig and eff >= THRESH["H2"]:
            return "支持"
        if sig:
            return "统计为正、未达经济门槛"
        return "不支持" if eff <= 1.0 else "未决（方向为正但未显著）"
    p = res.get("p_one_sided")
    eff = res["diff"] if name == "H1" else res["mean_excess"]
    if p is not None and p < ALPHA:
        return "支持" if eff >= THRESH[name] else "统计为正、未达经济门槛"
    return "不支持" if eff <= 0 else "未决（方向为正但未显著）"
