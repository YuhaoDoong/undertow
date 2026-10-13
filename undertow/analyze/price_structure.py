"""价格结构（道氏 HH/HL）状态机与交易模拟 —— ps-explore-v0.3（探索阶段，未冻结）。

协议：docs/prereg/2026-10-13_price_structure_v1_DRAFT_v0.2.md + v0.3 替换节；Codex 037 放行设计集（≤2015）探索。
纯计算：只吃 [(date, open, high, low, close), ...]，无 I/O、无网络。全部参数未校准（m=2、2R、60 日等）。

时序约定（防前视）：
- 第 t 根收盘后才处理第 t 根；判断只用 ATR_{t−1}；第 t 根上的突破只用第 t−1 根收盘后已知的关键位；
- 信号日 s（收盘后可知）→ s+1 开盘入场；
- 同一根最多确认一个摆动点；追加未来数据不改变已产生的摆动点、episode 与信号（前缀不变性，测试守住）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

M_ATR = 2.0            # 之字形反转门槛（收盘极值、收盘反转），未校准
ATR_N = 14
WARMUP = ATR_N + 1     # 第 15 根（下标 14）起才有 ATR；判断用 ATR_{t−1} → 下标 15 起
STOP_BUF = 0.1         # 止损缓冲 0.1·ATR
TARGET_R = 2.0
HOLD_MAX = 60          # 最长持有交易日（入场日计第 1 日）
WAIT_MAX = 60          # 突破后 / HL₁ᴮ 后最长等待交易日
H4_DAYS = (5, 10)


def wilder_atr(bars: list) -> list:
    """ATR[i]：用到第 i 根为止的真实波幅；前 ATR_N 根为 None。种子 = 第 1..14 根 TR 的简单平均（第 0 根无前收盘不计）。"""
    n = len(bars)
    atr = [None] * n
    tr = [None] * n
    for i in range(1, n):
        h, lo, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        tr[i] = max(h, pc) - min(lo, pc)
    if n > ATR_N:
        atr[ATR_N] = sum(tr[1:ATR_N + 1]) / ATR_N
        for i in range(ATR_N + 1, n):
            atr[i] = (atr[i - 1] * (ATR_N - 1) + tr[i]) / ATR_N
    return atr


@dataclass
class Pivot:
    kind: str          # "H" / "L"
    idx: int           # 极值所在根
    conf: int          # 确认根（该根收盘后可知）
    price: float       # 收盘价


def zigzag(bars: list, atr: list, m: float = M_ATR) -> list:
    """收盘极值、收盘反转之字形。逐根顺序：更新候选极值（相等不替换）→ 检查反转（含等号，用 ATR_{t−1}）→ 确认。
    初始化：同时跟踪最高 / 最低收盘；同根两侧都满足时先确认 extreme 更早的那个；每根最多确认一个。"""
    C = [b[4] for b in bars]
    piv: list[Pivot] = []
    d = None                       # None / "up" / "down"
    hi = lo = cand = None
    for t in range(WARMUP, len(bars)):
        a = atr[t - 1]
        if a is None:
            continue
        if d is None:
            if hi is None:
                hi = lo = t
                continue
            if C[t] > C[hi]:
                hi = t
            if C[t] < C[lo]:
                lo = t
            cH = C[t] <= C[hi] - m * a
            cL = C[t] >= C[lo] + m * a
            if cH and (not cL or hi < lo):
                piv.append(Pivot("H", hi, t, C[hi])); d = "down"
                cand = min(range(hi + 1, t + 1), key=lambda i: (C[i], i))
            elif cL:
                piv.append(Pivot("L", lo, t, C[lo])); d = "up"
                cand = max(range(lo + 1, t + 1), key=lambda i: (C[i], -i))
            continue
        if d == "down":
            if C[t] < C[cand]:
                cand = t
            elif C[t] >= C[cand] + m * a:
                piv.append(Pivot("L", cand, t, C[cand])); d = "up"
                lo_i = cand
                cand = max(range(lo_i + 1, t + 1), key=lambda i: (C[i], -i))
        else:
            if C[t] > C[cand]:
                cand = t
            elif C[t] <= C[cand] - m * a:
                piv.append(Pivot("H", cand, t, C[cand])); d = "down"
                hi_i = cand
                cand = min(range(hi_i + 1, t + 1), key=lambda i: (C[i], i))
    return piv


@dataclass
class Episode:
    kind: str                       # "A"（H1/H3 主事件）或 "H2"
    direction: str                  # "long" / "short"
    regime_at: int                  # 进入下降（上涨）结构的根
    key: Pivot                      # 关键 LH（做空：关键 HL）
    extreme: Pivot                  # LL（做空：HH）
    start: int                      # A：突破根；H2：HL₁ᴮ 确认根
    hl1: Pivot | None = None        # A：HL₁；H2：HL₁ᴮ
    hl2: Pivot | None = None        # H2 等待政策的 HL₂
    break_at: int | None = None
    status: str = "waiting"
    signal_at: int | None = None
    policy_w_status: str | None = None
    policy_w_signal: int | None = None
    notes: dict = field(default_factory=dict)


def _sign(direction: str) -> int:
    return 1 if direction == "long" else -1


def detect(bars: list, atr: list | None = None, m: float = M_ATR) -> tuple[list, list]:
    """返回 (pivots, episodes)。做多来自下降结构，做空来自上涨结构（镜像）。
    下降结构：fresh 摆动点中最近两高、两低都依次下降时进入，保持到突破；突破后 fresh_from = 突破根，只认 extreme 晚于它的摆动点。
    LL = 本结构起点（进入时两个依次下降高点中较早者）之后最低的已确认摆动低点；关键 LH = 结构起点之后、extreme 早于 LL 的
    最近一个已确认摆动高点（run2 修订）。"""
    atr = atr if atr is not None else wilder_atr(bars)
    piv = zigzag(bars, atr, m)
    C = [b[4] for b in bars]
    by_conf: dict[int, list] = {}
    for p in piv:
        by_conf.setdefault(p.conf, []).append(p)
    eps: list[Episode] = []
    known: list[Pivot] = []
    regime, regime_at, fresh_from, anchor = None, None, -1, -1
    waiting_a: list[Episode] = []
    waiting_h2: list[Episode] = []
    h2_started_for: set = set()

    def struct():
        f = [p for p in known if p.idx > fresh_from]
        hs, ls = [p for p in f if p.kind == "H"], [p for p in f if p.kind == "L"]
        return f, hs, ls

    def levels(direction):
        # 只在本结构的起点（进入结构时两个依次下降高点中较早那个，做空镜像）之后找 LL / 关键位（run2 修订，见 research_decisions）：
        # run1 用 fresh 全部低点取最低，最低点若早于结构形成、它前面没有高点 → 关键位为空 → 结构永远破不了、状态机卡死。
        f = [p for p in known if p.idx >= max(anchor, fresh_from + 1)]
        hs, ls = [p for p in f if p.kind == "H"], [p for p in f if p.kind == "L"]
        if direction == "long":
            if not ls:
                return None, None
            ext = min(ls, key=lambda p: (p.price, p.idx))                       # LL（价相同取更早）
            keys = [p for p in hs if p.idx < ext.idx]
        else:
            if not hs:
                return None, None
            ext = max(hs, key=lambda p: (p.price, -p.idx))                      # HH
            keys = [p for p in ls if p.idx < ext.idx]
        return (keys[-1] if keys else None), ext

    for t in range(len(bars)):
        # —— 1. 用第 t−1 根收盘后已知的状态判突破 ——
        if regime in ("down", "up") and t >= WARMUP:
            direction = "long" if regime == "down" else "short"
            key, ext = levels(direction)
            s = _sign(direction)
            if key is not None and s * C[t] > s * key.price:
                # 突破前 LL 之后是否已有更高低点（做空：更低高点）→ A 型 vs B 型（B 型属 H2，已在 HL₁ᴮ 确认时登记）
                after = [p for p in known if p.idx > ext.idx and p.kind == ("L" if direction == "long" else "H")
                         and s * p.price > s * ext.price]
                if not after:
                    e = Episode("A", direction, regime_at, key, ext, start=t, break_at=t)
                    eps.append(e); waiting_a.append(e)
                for e in waiting_h2:                                              # 等待政策：记录突破
                    if e.direction == direction and e.break_at is None and e.policy_w_status == "waiting":
                        e.break_at = t
                regime, fresh_from = "broken", t
        # —— 2. 等待中的 A 型：先查收盘失效，再看新确认的回踩点 ——
        new = by_conf.get(t, [])
        for e in list(waiting_a):
            s = _sign(e.direction)
            want = "L" if e.direction == "long" else "H"
            if t - e.start > WAIT_MAX:
                e.status = "timeout_no_entry"; waiting_a.remove(e); continue
            if t > e.start and s * C[t] < s * e.extreme.price:                 # 收盘越过 LL（严格）
                e.status = "invalidated_close_beyond_extreme"; waiting_a.remove(e); continue
            for p in new:
                if p.kind == want and p.idx > e.start:
                    if s * p.price > s * e.extreme.price:
                        e.hl1, e.signal_at, e.status = p, t, "signal"
                    else:
                        e.hl1, e.status = p, "invalidated_new_extreme"
                    waiting_a.remove(e); break
        # —— 3. H2 等待政策 ——
        for e in list(waiting_h2):
            s = _sign(e.direction)
            want = "L" if e.direction == "long" else "H"
            if t - e.start > WAIT_MAX:
                e.policy_w_status = "timeout_no_entry"; waiting_h2.remove(e); continue
            if e.break_at is None:
                if s * C[t] < s * e.extreme.price:
                    e.policy_w_status = "invalidated_close_beyond_extreme"; waiting_h2.remove(e)
                continue
            if t > e.break_at and s * C[t] < s * e.hl1.price:
                e.policy_w_status = "invalidated_close_beyond_hl1"; waiting_h2.remove(e); continue
            for p in new:
                if p.kind == want and p.idx > e.break_at:
                    if s * p.price > s * e.hl1.price:
                        e.hl2, e.policy_w_signal, e.policy_w_status = p, t, "signal"
                    else:
                        e.hl2, e.policy_w_status = p, "invalidated_pullback_beyond_hl1"
                    waiting_h2.remove(e); break
        # —— 4. 第 t 根新确认的摆动点生效（从 t+1 起参与突破判断）——
        known.extend(new)
        # H2：下降结构中、关键 LH 未破，LL 之后第一个更高低点在确认当日登记
        if regime in ("down", "up"):
            direction = "long" if regime == "down" else "short"
            key, ext = levels(direction)
            s = _sign(direction)
            want = "L" if direction == "long" else "H"
            for p in new:
                if key is not None and p.kind == want and p.idx > ext.idx and s * p.price > s * ext.price \
                        and (direction, ext.idx) not in h2_started_for:
                    h2_started_for.add((direction, ext.idx))
                    e = Episode("H2", direction, regime_at, key, ext, start=t, hl1=p, status="signal",
                                signal_at=t, policy_w_status="waiting")
                    eps.append(e); waiting_h2.append(e)
        # —— 5. 结构状态（fresh 摆动点）——
        if regime not in ("down", "up"):
            f, hs, ls = struct()
            if len(hs) >= 2 and len(ls) >= 2:
                if hs[-1].price < hs[-2].price and ls[-1].price < ls[-2].price:
                    regime, regime_at, anchor = "down", t, hs[-2].idx
                elif hs[-1].price > hs[-2].price and ls[-1].price > ls[-2].price:
                    regime, regime_at, anchor = "up", t, ls[-2].idx
    for e in waiting_a:
        e.status = "open_at_end"
    for e in waiting_h2:
        e.policy_w_status = "open_at_end"
    return piv, eps


def simulate(bars: list, entry_i: int, stop: float, direction: str, *, last_i: int,
             target_r: float = TARGET_R, hold: int = HOLD_MAX) -> dict:
    """entry_i 开盘入场。逐日：开盘越过止损按开盘出（跳空，可超 1R）→ 同根同时触及止损与目标记止损（保守）→ 止损 → 目标（含开盘跳过目标：按限价目标计）。
    需要的完整窗口超出 last_i（分区末根或数据末根）→ boundary_excluded，不只保留提前了结的交易。"""
    s = _sign(direction)
    if entry_i > last_i:
        return {"status": "no_entry_bar"}
    o = bars[entry_i][1]
    if o is None:
        return {"status": "no_open"}
    r = s * (o - stop)
    if r <= 0:
        return {"status": "open_beyond_stop", "entry": o, "stop": stop}
    if entry_i + hold - 1 > last_i:
        return {"status": "boundary_excluded", "entry": o, "stop": stop}
    tgt = o + s * target_r * r
    for k in range(hold):
        i = entry_i + k
        _, op, hi, lo, cl = bars[i]
        adverse = lo if direction == "long" else hi
        favor = hi if direction == "long" else lo
        if k > 0 and s * op <= s * stop:
            return _done(o, stop, r, op, "gap_stop", i, k, s)
        hit_stop = s * adverse <= s * stop
        hit_tgt = s * favor >= s * tgt
        if hit_stop:
            return _done(o, stop, r, stop, "stop_both_touched" if hit_tgt else "stop", i, k, s)
        if hit_tgt:
            return _done(o, stop, r, tgt, "target", i, k, s)
    i = entry_i + hold - 1
    return _done(o, stop, r, bars[i][4], "time_exit", i, hold - 1, s)


def _done(entry, stop, r, px, why, i, k, s):
    return {"status": "entered", "entry": entry, "stop": stop, "risk": r, "exit_price": px, "exit_reason": why,
            "exit_i": i, "days_held": k + 1, "r_mult": round(s * (px - entry) / r, 6)}


def barrier_survival(bars: list, entry_i: int, stop: float, direction: str, days: int) -> bool | None:
    """入场日起第 1..days 个交易日收盘都没越过止损价（做多：收盘 < 止损才算越过，等于不算）→ True；任一越过 → False；
    窗口不完整 → None。"""
    s = _sign(direction)
    if entry_i + days - 1 >= len(bars):
        return None
    return all(s * bars[entry_i + k][4] >= s * stop for k in range(days))


# ================= 局部分形认点（structure-v0，Codex 039：统一高低点引擎的简单基线；未校准） =================
FRACTAL_K = 2          # 左右各 2 根；极值 i 最早在 i+2 收盘后确认


@dataclass
class FPivot:
    kind: str          # "H" / "L"
    idx: int           # 极值所在根
    conf: int          # 确认根 = idx + k（该根收盘后可知）
    price: float       # 影线价：H 用 high、L 用 low（price_basis 固定，不在单个形态内混用收盘）
    price_basis: str = "high_low"
    ambiguous_order: bool = False   # 同一根既是高点候选又是低点候选：日内先后未知，需要先后顺序的形态不使用
    supersedes: int | None = None   # 交替投影中，被本点取代的同类前一点的 idx（原始点流不改写）


def fractal_pivots(bars: list, k: int = FRACTAL_K) -> list:
    """原始分形点流（按确认根排序）：第 i 根 high 严格高于左边 k 根、且不低于右边 k 根中任何一根 → 高点；low 镜像。
    平台等值：左侧严格、右侧非严格 → 一段相等高点只认最早那根（固定取一端）。只用到第 i+k 根，所以第 i+k 根收盘后才可知。"""
    out = []
    n = len(bars)
    for i in range(k, n - k):
        hi, lo = bars[i][2], bars[i][3]
        isH = all(hi > bars[j][2] for j in range(i - k, i)) and all(hi >= bars[j][2] for j in range(i + 1, i + k + 1))
        isL = all(lo < bars[j][3] for j in range(i - k, i)) and all(lo <= bars[j][3] for j in range(i + 1, i + k + 1))
        amb = isH and isL
        if isH:
            out.append(FPivot("H", i, i + k, hi, ambiguous_order=amb))
        if isL:
            out.append(FPivot("L", i, i + k, lo, ambiguous_order=amb))
    return out


def alternating(piv: list) -> list:
    """交替投影（as-of 流式）：按确认顺序处理；同类相邻时，更极端的新点取代前一点（记 supersedes），否则丢弃新点。
    只决定「此刻的结构表示」；已经发出的事件引用的是当时的投影，不因后来的取代而改写。ambiguous_order 的点不进入投影。"""
    seq: list[FPivot] = []
    for p in sorted(piv, key=lambda p: (p.conf, p.idx, p.kind)):
        if p.ambiguous_order:
            continue
        if seq and seq[-1].kind == p.kind:
            prev = seq[-1]
            better = p.price > prev.price if p.kind == "H" else p.price < prev.price
            if better:
                seq[-1] = FPivot(p.kind, p.idx, p.conf, p.price, p.price_basis, False, prev.idx)
            continue
        seq.append(p)
    return seq


# ================= 出场政策 P0–P4（ps-exit-v1，Codex 040；用户「甲」：做多出现已确认 LH 即离场，做空出现 HL 即离场） =================
POLICIES = {
    "P0": {"stop": "extreme", "target": True, "struct_exit": False, "breakeven": False, "hold": 60},
    "P1": {"stop": "pullback", "target": True, "struct_exit": False, "breakeven": False, "hold": 60},
    "P2": {"stop": "pullback", "target": False, "struct_exit": True, "breakeven": False, "hold": 60},
    "P3": {"stop": "pullback", "target": False, "struct_exit": True, "breakeven": True, "hold": 60},
    "P4": {"stop": "pullback", "target": False, "struct_exit": True, "breakeven": True, "hold": 250},
    # ps-exit-v2（用户 2026-10-14 澄清：做多止损放在 LL 附近、做空放在 HH 附近；离场看 LH / HL；移成本「只是提议，可以测试」）
    "U1": {"stop": "extreme", "target": False, "struct_exit": True, "breakeven": False, "hold": 60},
    "U2": {"stop": "extreme", "target": False, "struct_exit": True, "breakeven": True, "hold": 60},
    "U3": {"stop": "extreme", "target": False, "struct_exit": True, "breakeven": False, "hold": 250},
    "U4": {"stop": "extreme", "target": False, "struct_exit": True, "breakeven": True, "hold": 250},
    # ps-exit-v3 基准（Codex 041）：同初止损、无目标、无结构离场、无移成本 —— 把「取消 2R」与「加 LH 离场」分开
    "N60": {"stop": "extreme", "target": False, "struct_exit": False, "breakeven": False, "hold": 60},
    "N250": {"stop": "extreme", "target": False, "struct_exit": False, "breakeven": False, "hold": 250},
    # ps-exit-v4（用户 2026-10-14：「最新的 HH 后，出现新的 LH，离场。等于随着盈利增加不断抬高」；入场前回踩前的高点「不算，要等入场后的新 HH」）
    # 结构离场要先「上膛」：入场后须先确认一个高于最新旧高点的新 HH；之后出现低于最新 HH 的已确认高点才离场；HH 抬高则参照随之抬高
    "U5": {"stop": "extreme", "target": False, "struct_exit": True, "breakeven": False, "hold": 60, "arm_after_new_hh": True},
    "U6": {"stop": "extreme", "target": False, "struct_exit": True, "breakeven": False, "hold": 250, "arm_after_new_hh": True},
}


def simulate_policy(bars: list, piv: list, entry_i: int, stop: float, direction: str, pol: dict, *, last_i: int,
                    target_r: float = TARGET_R, entry_price: float | None = None) -> dict:
    """entry_i 开盘入场。逐根顺序（固定）：
      ① 开盘：上一根收盘生成的结构离场意图 → 按开盘成交；开盘已越过有效止损 → 跳空止损按开盘（两者同时触发记两者、只成交一次）；
      ② 盘中：触及有效止损 → 按止损价（与目标同根触及记止损）；触及 2R 目标（若有）→ 按目标价；
      ③ 收盘：本根确认、且极值在入场根或之后的摆动点 ——
         做多：高点低于前一个已确认高点（LH）→ 生成离场意图（下一根开盘执行）；高点高于前一个（HH）→ 下一根起止损 = max(止损, 入场价)；
         做空镜像（HL 离场、LL 移成本）。「前一个同类点」可在入场前，记审计引用；入场前形成、入场后才确认的点不触发。
      ④ 第 hold 根收盘仍在场 → 时间出场。窗口超出 last_i → boundary_excluded。"""
    s = _sign(direction)
    if entry_i > last_i:
        return {"status": "no_entry_bar"}
    o = bars[entry_i][1] if entry_price is None else entry_price      # entry_price：盘中挂单成交价（ps-trigger-v2 的 limit 入场）
    if o is None:
        return {"status": "no_open"}
    r = s * (o - stop)
    if r <= 0:
        return {"status": "open_beyond_stop", "entry": o, "stop": stop}
    if entry_i + pol["hold"] - 1 > last_i:
        return {"status": "boundary_excluded", "entry": o, "stop": stop}
    tgt = o + s * target_r * r if pol["target"] else None
    want, opp = ("H", "L") if direction == "long" else ("L", "H")
    by_conf: dict[int, list] = {}
    for p in piv:
        by_conf.setdefault(p.conf, []).append(p)
    eff_stop, pending, be_at, audit = stop, None, None, {}
    arm = pol.get("arm_after_new_hh", False)
    armed, latest = False, None                     # 上膛模式：latest = 当前参照的最新 HH（入场前为最近的旧同类点）
    if arm:
        pre = [q for q in piv if q.kind == want and q.conf < entry_i]
        latest = pre[-1] if pre else None
    for k in range(pol["hold"]):
        i = entry_i + k
        _, op, hi, lo, cl = bars[i]
        if k > 0:
            gap = s * op <= s * eff_stop
            if pending is not None or gap:
                gs = "breakeven_gap_stop" if be_at is not None else "gap_stop"
                why = f"struct_exit+{gs}" if (pending is not None and gap) else ("struct_exit" if pending is not None else gs)
                return _done_p(o, stop, r, op, why, i, k, s, eff_stop, be_at, audit)
        adverse, favor = (lo, hi) if direction == "long" else (hi, lo)
        if s * adverse <= s * eff_stop:
            hit_t = tgt is not None and s * favor >= s * tgt
            why = ("breakeven_stop" if be_at is not None else "stop") + ("_both_touched" if hit_t else "")
            return _done_p(o, stop, r, eff_stop, why, i, k, s, eff_stop, be_at, audit)
        if tgt is not None and s * favor >= s * tgt:
            return _done_p(o, stop, r, tgt, "target", i, k, s, eff_stop, be_at, audit)
        for p in by_conf.get(i, []):
            if p.kind != want or p.idx < entry_i:
                continue
            if arm:
                if latest is None or s * p.price > s * latest.price:          # 新 HH（做空：新 LL）→ 上膛 / 参照抬高
                    if not armed:
                        audit["armed_at"] = {"idx": p.idx, "conf": p.conf, "price": p.price,
                                             "ref_idx": latest.idx if latest else None, "ref_price": latest.price if latest else None}
                    armed, latest = True, p
                elif armed and s * p.price < s * latest.price and pending is None:   # 上膛后低于最新 HH → LH → 下一根离场
                    pending = i
                    audit["struct_point"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref_idx": latest.idx, "ref_conf": latest.conf,
                                             "ref_price": latest.price, "decided_at": i, "execute_at": i + 1}
                continue
            prev = [q for q in piv if q.kind == want and q.conf < p.conf and q.idx < p.idx]
            if not prev:
                continue
            ref = prev[-1]
            weaker = s * p.price < s * ref.price            # 做多：更低的高点（LH）；做空：更高的低点（HL）
            if weaker and pol["struct_exit"] and pending is None:
                pending = i
                audit["struct_point"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref_idx": ref.idx, "ref_conf": ref.conf,
                                         "ref_price": ref.price, "decided_at": i, "execute_at": i + 1}
            elif not weaker and s * p.price > s * ref.price and pol["breakeven"] and be_at is None:
                be_at = i + 1
                audit["breakeven_point"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref_idx": ref.idx, "ref_conf": ref.conf,
                                            "ref_price": ref.price, "decided_at": i, "effective_at": i + 1}
        if be_at is not None and i + 1 >= be_at:
            eff_stop = max(eff_stop, o) if direction == "long" else min(eff_stop, o)
    i = entry_i + pol["hold"] - 1
    return _done_p(o, stop, r, bars[i][4], "time_exit", i, pol["hold"] - 1, s, eff_stop, be_at, audit)


def _done_p(entry, stop, r, px, why, i, k, s, eff_stop, be_at, audit):
    return {"status": "entered", "entry": entry, "stop": stop, "risk": r, "exit_price": px, "exit_reason": why, "exit_i": i,
            "days_held": k + 1, "r_mult": round(s * (px - entry) / r, 6), "final_stop": eff_stop, "breakeven_from_i": be_at,
            "audit": audit}


# ================= ps-trigger-v1：更早的入场/离场确认（用户复核「入场和离场时机一塌糊涂，全面太晚」，2026-10-14） =================
# 结构（关键前高、LL、突破）仍用 2ATR 收盘之字形（用户：「高点低点我觉得还行」）；入场与离场的「确认」改用更快的局部确认，四种都试：
#   bar   ：低点那根之后，有一根收盘高于它的最高价（做空镜像）
#   two   ：低点之后连续 2 根最低价都高于它
#   atr   ：从低点反弹 0.75·ATR（收盘），用户说的 0.5–1 倍取中值，未校准
#   limit ：突破后回踩到关键前高价位挂单买入（无确认；离场用 bar 确认）
# 「相同点位」门槛 tol（HL 须高于 LL ≥ tol 才算结构；LH 须低于最新 HH ≥ tol 才算；新 HH 须高出 ≥ tol 才上膛）：0.25 / 0.5 / 1 ATR、0.5%。全部未校准。
TRIGGER_ATR = 0.75


def local_swings(bars: list, atr: list, method: str) -> list:
    """交替的局部摆动点（影线价），按指定确认方式流式确认；每个点含 idx（极值根）与 conf（确认根，收盘后可知）。"""
    out: list[Pivot] = []
    look = "L"                       # 先找低点
    cand = None
    n = len(bars)
    for t in range(1, n):
        h, l, c = bars[t][2], bars[t][3], bars[t][4]
        if cand is None:
            cand = t
            continue
        if look == "L":
            if l < bars[cand][3]:
                cand = t
                continue
            ok = False
            if method in ("bar", "limit"):
                ok = c > bars[cand][2]
            elif method == "two":
                ok = t - cand >= 2 and all(bars[j][3] > bars[cand][3] for j in range(cand + 1, t + 1))
            elif method == "atr":
                a = atr[t - 1]
                ok = a is not None and c >= bars[cand][3] + TRIGGER_ATR * a
            if ok:
                out.append(Pivot("L", cand, t, bars[cand][3])); look = "H"
                cand = max(range(cand + 1, t + 1), key=lambda i: (bars[i][2], -i))
        else:
            if h > bars[cand][2]:
                cand = t
                continue
            ok = False
            if method in ("bar", "limit"):
                ok = c < bars[cand][3]
            elif method == "two":
                ok = t - cand >= 2 and all(bars[j][2] < bars[cand][2] for j in range(cand + 1, t + 1))
            elif method == "atr":
                a = atr[t - 1]
                ok = a is not None and c <= bars[cand][2] - TRIGGER_ATR * a
            if ok:
                out.append(Pivot("H", cand, t, bars[cand][2])); look = "L"
                cand = min(range(cand + 1, t + 1), key=lambda i: (bars[i][3], i))
    return out


def _tol(t: str, price: float, a: float) -> float:
    return price * float(t[:-1]) / 100 if t.endswith("%") else float(t) * a


def trigger_trade(bars: list, atr: list, ep, loc: list, method: str, tol: str, *, last_i: int, hold: int = 250,
                  wait: int = WAIT_MAX) -> dict:
    """一个 A 型突破（ep：break_at、key、extreme）上的入场 + 离场。做多：突破后，第一个【极值在突破之后】、确认在突破之后、
    低点高于 LL + tol 的局部低点 → 确认根下一根开盘入场（limit：突破后 low ≤ 关键前高 且 > LL + tol 的第一根，按关键前高成交，跳空低开按开盘）。
    收盘跌破 LL → 失效；突破后 wait 根内没入场 → timeout。止损 = LL − 0.1·ATR[入场前一根]。
    离场（上膛式）：入场前最新局部高点为参照；入场后确认的局部高点高出参照 ≥ tol → 上膛并抬高参照；上膛后确认的局部高点低于参照 ≥ tol → 下一根开盘离场。
    同根顺序：开盘（离场意图 / 跳空止损）→ 盘中止损 → 收盘确认新点。"""
    d = ep.direction
    s = _sign(d)
    want_h = "H" if d == "long" else "L"
    ll = ep.extreme.price
    fe = fast_entry(bars, atr, ep, loc, method, tol, last_i=last_i, wait=wait)
    if fe["status"] != "entered":
        return fe
    entry_i, entry_px = fe["entry_i"], fe["entry"]
    stop = ll - s * STOP_BUF * atr[entry_i - 1]
    if s * (entry_px - stop) <= 0:
        return {"status": "open_beyond_stop"}
    if entry_i + hold - 1 > last_i:
        return {"status": "boundary_excluded"}
    return _fast_exit(bars, atr, loc, entry_i, entry_px, stop, d, tol, hold=hold)


def fast_entry(bars: list, atr: list, ep, loc: list, method: str, tol: str, *, last_i: int, wait: int = WAIT_MAX) -> dict:
    """ps-trigger 入场部分（见 trigger_trade 文档）：返回 {"status": "entered", "entry_i", "entry"} 或未入场原因。"""
    d = ep.direction
    s = _sign(d)
    want_l = "L" if d == "long" else "H"
    b0, ll = ep.break_at, ep.extreme.price
    entry_i = entry_px = None
    for t in range(b0 + 1, min(last_i, b0 + wait) + 1):
        A = atr[t - 1]
        if A is None:
            continue
        if s * bars[t][4] < s * ll:
            return {"status": "invalidated_close_beyond_extreme"}
        if method == "limit":
            lvl = ep.key.price
            adv = bars[t][3] if d == "long" else bars[t][2]
            if s * adv <= s * lvl and s * (adv - ll) >= _tol(tol, lvl, A):
                entry_i, entry_px = t, (min(bars[t][1], lvl) if d == "long" else max(bars[t][1], lvl))
                break
            continue
        hit = [p for p in loc if p.conf == t and p.kind == want_l and p.idx > b0]
        if hit and s * (hit[0].price - ll) >= _tol(tol, hit[0].price, A):
            if t + 1 <= last_i:
                entry_i, entry_px = t + 1, bars[t + 1][1]
            break
    if entry_i is None:
        return {"status": "timeout_no_entry"}
    return {"status": "entered", "entry_i": entry_i, "entry": entry_px}


def _fast_exit(bars, atr, loc, entry_i, entry_px, stop, d, tol, *, hold):
    s = _sign(d)
    want_h = "H" if d == "long" else "L"
    pre = [p for p in loc if p.kind == want_h and p.conf < entry_i]
    ref = pre[-1].price if pre else None
    armed, pending, sig = False, None, {}
    for k in range(hold):
        i = entry_i + k
        _, op, hi, lo, cl = bars[i]
        if k > 0:
            gap = s * op <= s * stop
            if pending is not None or gap:
                return _tdone(entry_i, entry_px, stop, op, "struct_exit" if pending is not None and not gap else ("gap_stop" if pending is None else "struct_exit+gap_stop"), i, s, sig)
        if s * (lo if d == "long" else hi) <= s * stop:
            return _tdone(entry_i, entry_px, stop, stop, "stop", i, s, sig)
        for p in loc:
            if p.conf != i or p.kind != want_h or p.idx < entry_i:
                continue
            A = atr[i - 1] or atr[i]
            if ref is None or s * (p.price - ref) >= _tol(tol, p.price, A):
                if not armed:
                    sig["armed"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref": ref}
                armed, ref = True, p.price
            elif armed and s * (ref - p.price) >= _tol(tol, p.price, A) and pending is None:
                pending = i
                sig["lh"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref": ref}
    i = entry_i + hold - 1
    return _tdone(entry_i, entry_px, stop, bars[i][4], "admin_cap", i, s, sig)


def _tdone(ei, ep_, stop, px, why, i, s, sig):
    r = s * (ep_ - stop)
    return {"status": "entered", "entry_i": ei, "entry": ep_, "stop": stop, "exit_i": i, "exit_price": px, "exit_reason": why,
            "days_held": i - ei + 1, "r_mult": round(s * (px - ep_) / r, 6), "signals": sig}
