"""下降楔形自动识别与突破统计（复现 Bulkowski 下降楔形统计的探索口径 wedge-explore-v1）。纯计算，无 I/O。

判据按 Bulkowski（thepatternsite.com/fallwedge.html）与外部作者一 10/15 帖子写成确定性规则，全部参数未校准：
  摆动点 = 分形高低点（左右各 k 根，影线价）的交替投影；一段连续摆动点（≥5 个、高低各 ≥2）构成候选；
  上轨 = 段内第一个与最后一个高点连线，下轨 = 第一个与最后一个低点连线；两线都向下、上轨更陡（收敛）、交点在段之后；
  段内每根 K 线都在两线之间（容差 TOL·ATR），贴线（距线 ≤ 容差）的摆动点合计 ≥5、每侧 ≥2；段长 ≥ MIN_SPAN 根。
  突破 = 之后第一根收盘在上轨之上（向上）或下轨之下（向下）；到交点或 MAX_WAIT 根仍未突破 → 不计方向。
认法：
  descriptive   = 同一起点取能成立的最长一段（像人事后看图画线，含后视），突破从段末下一根找；
  causal        = v1 口径，【有前视缺陷，只为复现 run1/run2 保留】：在全历史【最终】交替投影上找段，
                  后来更极端的同类点会改写之前的投影，已完成的事件可因未来数据消失（Codex 053 合成反例）；
  causal_stream = v1.1 可交易口径（`stream`）：按确认根逐根推进，只用当时已确认的分形点更新交替投影；
                  当时成立就冻结轨道与身份，突破/失效/到交点只按冻结轨道判定；已发事件只追加、不改写、不删除。
口径：docs/prereg/2026-10-16_falling_wedge_explore_v1.md；v1.1 = docs/prereg/2026-10-16_falling_wedge_explore_v1.1.md。
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
    pivots: tuple = ()         # causal_stream：认出时冻结的摆动点身份 ((kind, idx, conf, price), ...)

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
    """→ [(wedge, direction, breakout_idx)]，按起点贪心、不重叠。mode = descriptive / causal / causal_stream。
    causal（v1）有前视缺陷，只为复现旧 run 保留；可交易口径用 causal_stream。"""
    if mode == "causal_stream":
        return [(e.wedge, e.direction or "incomplete", e.t) for e in stream(bars, atr, k, tol_atr)[0]]
    if mode not in ("descriptive", "causal"):
        raise ValueError(mode)
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


# ================= v1.1 causal_stream：逐根流式识别（Codex 053 C 包） =================
@dataclass
class StreamEvent:
    eid: int
    wedge: Wedge                     # 认出时冻结：轨道、身份、known 之后不再改
    direction: str | None = None     # None = 尚未定性；broke_before_known / up / down / no_break 只写一次
    t: int | None = None             # 突破根（up/down）
    resolved_at: int | None = None   # 定性所在根（认出前已破 = 认出根；no_break = 等待截止根）
    pre_break: tuple | None = None   # 认出前已破：(根, "up"/"down")，只作审计

    def resolve(self, direction: str, t: int | None, at: int, log: list) -> None:
        if self.direction is not None:          # 已发出的定性永不改写
            raise RuntimeError(f"事件 {self.eid} 已定性为 {self.direction}，拒绝改写为 {direction}")
        self.direction, self.t, self.resolved_at = direction, t, at
        log.append((at, "resolve", self.eid, direction, t))


def _alt_push(seq: list, p) -> bool:
    """与 ps.alternating 的单步完全相同（测试逐步核对，防漂移）。返回投影末端是否变化。"""
    if p.ambiguous_order:
        return False
    if seq and seq[-1].kind == p.kind:
        prev = seq[-1]
        better = p.price > prev.price if p.kind == "H" else p.price < prev.price
        if better:
            seq[-1] = ps.FPivot(p.kind, p.idx, p.conf, p.price, p.price_basis, False, prev.idx)
            return True
        return False
    seq.append(p)
    return True


def stream(bars: list, atr: list, k: int = K, tol_atr: float = TOL) -> tuple[list, list]:
    """v1.1 causal_stream。第 T 根收盘后能做的事只用 bars[:T+1]、atr[:T+1]：
      ① 冻结中的事件：用第 T 根收盘对冻结轨道判上破/下破；到 min(交点, 认出+1+MAX_WAIT) 仍未越线 = no_break；
      ② 把确认根 = T 的分形点推进交替投影（同类更极端者取代末端，只按当时可知的点）；
      ③ 无冻结事件且投影末端变化：在【当时】投影里找以末端为终点、首点 idx > floor、跨度 ≤ MAX_SPAN 的段，
         取首点最早的成立段（与 v1 按起点贪心同序）→ 认出根 = T；(段末, T] 内已有收盘越线 → broke_before_known，
         否则冻结并从 T+1 起等突破。
    不重叠：突破 → floor = 突破根；no_break → floor = ⌊交点⌋；认出前已破 → floor = 认出根（与 v1 相同）。
    → (events, log)。log 只追加：(根, "recognize"|"resolve", eid, ...)。运行结束仍未定性的事件 direction=None（= 不完整）。"""
    piv = sorted(ps.fractal_pivots(bars, k), key=lambda p: (p.conf, p.idx, p.kind))
    seq: list = []
    events: list[StreamEvent] = []
    log: list = []
    active: StreamEvent | None = None
    floor_idx = -1
    pi = 0
    for T in range(len(bars)):
        if active is not None:                                   # ① 只按冻结轨道判定
            w = active.wedge
            c = bars[T][4]
            if c > w.upper(T):
                active.resolve("up", T, T, log)
                floor_idx, active = T, None
            elif c < w.lower(T):
                active.resolve("down", T, T, log)
                floor_idx, active = T, None
            elif T >= min(math.floor(w.apex), w.known + 1 + MAX_WAIT):
                active.resolve("no_break", None, T, log)
                floor_idx, active = math.floor(w.apex), None
        changed = False
        while pi < len(piv) and piv[pi].conf <= T:               # ② 只推进已确认的点
            changed |= _alt_push(seq, piv[pi])
            pi += 1
        if active is not None or not changed or len(seq) < 5:
            continue
        b = len(seq) - 1                                         # ③ 当时成立的段
        pick = None
        for a in range(0, b - 3):
            if seq[a].idx <= floor_idx or seq[b].idx - seq[a].idx > MAX_SPAN:
                continue
            w = check(seq[a:b + 1], bars, atr, tol_atr)
            if w:
                pick = w
                pick.known = T
                pick.pivots = tuple((p.kind, p.idx, p.conf, p.price) for p in seq[a:b + 1])
                break
        if pick is None:
            continue
        ev = StreamEvent(len(events), pick)
        events.append(ev)
        log.append((T, "recognize", ev.eid, pick.first, pick.last))
        for t in range(pick.last + 1, min(math.floor(pick.apex), T) + 1):
            c = bars[t][4]
            side = "up" if c > pick.upper(t) else "down" if c < pick.lower(t) else None
            if side:
                ev.pre_break = (t, side)
                ev.resolve("broke_before_known", None, T, log)
                floor_idx = T
                break
        else:
            if math.floor(pick.apex) < T + 1:                    # 认出时已过交点、期间未越线
                ev.resolve("no_break", None, T, log)
                floor_idx = math.floor(pick.apex)
            else:
                active = ev
    return events, log


def event_identity(e: StreamEvent) -> tuple:
    w = e.wedge
    return (w.first, w.last, w.known, w.h1, w.h2, w.l1, w.l2, w.touches, w.apex, w.pivots)


def prefix_audit(bars: list, cuts, k: int = K, tol_atr: float = TOL, runner=None) -> list:
    """前缀重放核对：对每个 cut 单独用 bars[:cut]（ATR 也只在前缀上算）重跑，要求
    日志 == 全数据日志里根 < cut 的部分；认出根 < cut 的事件身份/轨道一致；定性根 < cut 的定性一致、否则前缀里仍未定性。
    → 不一致的 cut 列表（空 = 通过）。runner 只供测试注入有缺陷的识别器以证明本核对能抓到前视。"""
    runner = runner or (lambda b: stream(b, ps.wilder_atr(b), k, tol_atr))
    full, flog = runner(bars)
    bad = []
    for cut in cuts:
        pre, plog = runner(bars[:cut])
        want = [e for e in full if e.wedge.known < cut]
        ok = plog == [x for x in flog if x[0] < cut] and [event_identity(e) for e in pre] == [event_identity(e) for e in want]
        for p, f in zip(pre, want) if ok else ():
            if f.resolved_at is not None and f.resolved_at < cut:
                ok = ok and (p.direction, p.t, p.resolved_at) == (f.direction, f.t, f.resolved_at)
            else:
                ok = ok and p.direction is None and p.t is None
        if not ok:
            bad.append(cut)
    return bad


def audit_cuts(events: list, n: int, stride: int = 250) -> list:
    """设计集核对用的截断点：每个事件认出根、认出根+1、定性根、定性根+1，加每 stride 根一个。"""
    cuts = set(range(stride, n + 1, stride))
    for e in events:
        for x in (e.wedge.known, e.wedge.known + 1, e.resolved_at, None if e.resolved_at is None else e.resolved_at + 1):
            if x is not None and 0 < x <= n:
                cuts.add(x)
    return sorted(cuts)


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
