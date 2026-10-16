"""下降楔形自动识别与突破统计（复现 Bulkowski 下降楔形统计的探索口径 wedge-explore-v1）。纯计算，无 I/O。

判据按 Bulkowski（thepatternsite.com/fallwedge.html）与外部作者一 10/15 帖子写成确定性规则，全部参数未校准：
  摆动点 = 分形高低点（左右各 k 根，影线价）的交替投影；一段连续摆动点（≥5 个、高低各 ≥2）构成候选；
  上轨 = 段内第一个与最后一个高点连线，下轨 = 第一个与最后一个低点连线；两线都向下、上轨更陡（收敛）、交点在段之后；
  段内每根 K 线都在两线之间（容差 TOL·ATR），贴线（距线 ≤ 容差）的摆动点合计 ≥5、每侧 ≥2；段长 ≥ MIN_SPAN 根。
  突破 = 之后第一根收盘在上轨之上（向上）或下轨之下（向下）；到交点或 MAX_WAIT 根仍未突破 → 不计方向。
两种认法：
  descriptive = 同一起点取能成立的最长一段（像人事后看图画线，含后视），突破从段末下一根找；
  causal      = 同一起点最早成立的那一段，在最后一个摆动点确认后才算认出，突破从确认根下一根找（交易时能做到的）。
口径：docs/prereg/2026-10-16_falling_wedge_explore_v1.md。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from undertow.analyze import price_structure as ps

K = 3
TOL = 0.3
MIN_SPAN = 15
MAX_SPAN = 250
MAX_WAIT = 120
HORIZON = 120          # 突破后观察 MFE / 目标
THROWBACK_DAYS = 30
FAIL_PCT = 0.05
N_PLACEBO = 50


@dataclass
class Wedge:
    first: int
    last: int
    known: int                 # 认出根：descriptive = 段末；causal = 最后一个摆动点的确认根
    h1: tuple                  # (idx, price) 上轨两点
    h2: tuple
    l1: tuple
    l2: tuple
    touches: int
    apex: float
    hi: float                  # 段内最高价
    lo: float                  # 段内最低价

    def upper(self, t: float) -> float:
        return self.h1[1] + (self.h2[1] - self.h1[1]) / (self.h2[0] - self.h1[0]) * (t - self.h1[0])

    def lower(self, t: float) -> float:
        return self.l1[1] + (self.l2[1] - self.l1[1]) / (self.l2[0] - self.l1[0]) * (t - self.l1[0])


def check(run: list, bars: list, atr: list, tol_atr: float = TOL) -> Wedge | None:
    hs = [p for p in run if p.kind == "H"]
    ls = [p for p in run if p.kind == "L"]
    if len(run) < 5 or len(hs) < 2 or len(ls) < 2:
        return None
    first, last = min(p.idx for p in run), max(p.idx for p in run)
    if last - first < MIN_SPAN or first < 1 or atr[first - 1] is None:
        return None
    h1, h2, l1, l2 = hs[0], hs[-1], ls[0], ls[-1]
    su = (h2.price - h1.price) / (h2.idx - h1.idx)
    sl = (l2.price - l1.price) / (l2.idx - l1.idx)
    if not (su < 0 and sl < 0 and su < sl):
        return None
    apex = (l1.price - h1.price + su * h1.idx - sl * l1.idx) / (su - sl)
    if apex <= last:
        return None
    w = Wedge(first, last, last, (h1.idx, h1.price), (h2.idx, h2.price), (l1.idx, l1.price), (l2.idx, l2.price), 0, apex, 0.0, 0.0)
    tol = tol_atr * atr[first - 1]
    for t in range(first, last + 1):
        if bars[t][2] > w.upper(t) + tol or bars[t][3] < w.lower(t) - tol:
            return None
    th = sum(abs(p.price - w.upper(p.idx)) <= tol for p in hs)
    tl = sum(abs(p.price - w.lower(p.idx)) <= tol for p in ls)
    if th < 2 or tl < 2 or th + tl < 5:
        return None
    w.touches = th + tl
    w.hi = max(b[2] for b in bars[first:last + 1])
    w.lo = min(b[3] for b in bars[first:last + 1])
    return w


def breakout_path(closes, start: int, w: Wedge) -> tuple[str, int | None]:
    """closes 以绝对下标取值（closes(t) 返回收盘或 None 表示数据结束）。从 start 起找第一根收盘越线。"""
    end = min(math.floor(w.apex), start + MAX_WAIT)
    for t in range(start, end + 1):
        c = closes(t)
        if c is None:
            return "incomplete", None
        if c > w.upper(t):
            return "up", t
        if c < w.lower(t):
            return "down", t
    return "no_break", None


def outcome(bars: list, w: Wedge, d: str, t: int) -> dict:
    n = len(bars)
    if t + HORIZON >= n:
        return {"complete": False}
    cb = bars[t][4]
    seg = bars[t + 1:t + 1 + HORIZON]
    if d == "up":
        mfe = max(b[2] for b in seg) / cb - 1
        target = w.hi
        hit = any(b[2] >= target for b in seg)
        lvl = w.upper(t)
        tb = any(b[3] <= lvl for b in seg[:THROWBACK_DAYS])
    else:
        mfe = 1 - min(b[3] for b in seg) / cb
        target = cb - (w.hi - w.lo)
        hit = any(b[3] <= target for b in seg)
        lvl = w.lower(t)
        tb = any(b[2] >= lvl for b in seg[:THROWBACK_DAYS])
    return {"complete": True, "mfe": mfe, "fail5": mfe < FAIL_PCT, "target_hit": hit, "throwback": tb}


def find(bars: list, atr: list, mode: str, k: int = K, tol_atr: float = TOL) -> list:
    """→ [(wedge, direction, breakout_idx)]，按起点贪心、不重叠。mode = descriptive / causal。"""
    seq = ps.alternating(ps.fractal_pivots(bars, k))
    closes = lambda t: bars[t][4] if t < len(bars) else None  # noqa: E731
    out, a = [], 0
    while a <= len(seq) - 5:
        pick = None
        for b in range(a + 4, len(seq)):
            if seq[b].idx - seq[a].idx > MAX_SPAN:
                break
            w = check(seq[a:b + 1], bars, atr, tol_atr)
            if w:
                pick = w
                if mode == "causal":
                    w.known = seq[b].conf
                    break
        if pick is None:
            a += 1
            continue
        start = pick.last + 1 if mode == "descriptive" else pick.known + 1
        if mode == "causal":
            pre, _ = breakout_path(closes, pick.last + 1, _until(pick, pick.known))
            if pre in ("up", "down"):          # 认出之前已经越线：交易时来不及，单列
                out.append((pick, "broke_before_known", None))
                a = _next(seq, a, pick.known)
                continue
        d, t = breakout_path(closes, start, pick)
        out.append((pick, d, t))
        stop = t if t is not None else min(len(bars) - 1, math.floor(pick.apex))
        a = _next(seq, a, stop)
    return out


def _until(w: Wedge, known: int) -> Wedge:
    """只在 (last, known] 内找越线：把交点截到 known。"""
    c = Wedge(**{**w.__dict__})
    c.apex = min(w.apex, known)
    return c


def _next(seq: list, a: int, stop: int) -> int:
    b = a + 1
    while b < len(seq) and seq[b].idx <= stop:
        b += 1
    return b


def placebo_up_share(bars: list, atr: list, w: Wedge, start: int, seed: int, n: int = N_PLACEBO) -> float | None:
    """同一对轨道、同一起点价，换成本品种其它日期的收盘路径（按 ATR% 比例缩放），看先越哪条线。
    衡量「两条下行收敛线 + 这个品种的一般走势」机械地给出多少向上突破，与楔形是否含信息无关。"""
    rng = random.Random(seed)
    C = [b[4] for b in bars]
    p0 = C[start - 1]
    a0 = atr[start - 1] / p0
    ups = downs = 0
    tries = 0
    while ups + downs < n and tries < n * 10:
        tries += 1
        s2 = rng.randrange(20, len(bars) - MAX_WAIT - 2)
        if abs(s2 - start) < 250 or atr[s2 - 1] is None:
            continue
        sc = a0 / (atr[s2 - 1] / C[s2 - 1])
        path = lambda t: (p0 * math.exp(sc * math.log(C[s2 - 1 + (t - start + 1)] / C[s2 - 1]))  # noqa: E731
                          if s2 - 1 + (t - start + 1) < len(C) else None)
        d, _ = breakout_path(path, start, w)
        if d == "up":
            ups += 1
        elif d == "down":
            downs += 1
    return None if ups + downs == 0 else ups / (ups + downs)
