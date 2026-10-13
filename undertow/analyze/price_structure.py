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
}


def simulate_policy(bars: list, piv: list, entry_i: int, stop: float, direction: str, pol: dict, *, last_i: int,
                    target_r: float = TARGET_R) -> dict:
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
    o = bars[entry_i][1]
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
            prev = [q for q in piv if q.kind == want and q.conf < p.conf and q.idx < p.idx]
            if not prev:
                continue
            ref = prev[-1]
            weaker = s * p.price < s * ref.price            # 做多：更低的高点（LH）；做空：更高的低点（HL）
            if weaker and pol["struct_exit"] and pending is None:
                pending = i
                audit["struct_point"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref_idx": ref.idx, "ref_price": ref.price}
            elif not weaker and s * p.price > s * ref.price and pol["breakeven"] and be_at is None:
                be_at = i + 1
                audit["breakeven_point"] = {"idx": p.idx, "conf": p.conf, "price": p.price, "ref_idx": ref.idx}
        if be_at is not None and i + 1 >= be_at:
            eff_stop = max(eff_stop, o) if direction == "long" else min(eff_stop, o)
    i = entry_i + pol["hold"] - 1
    return _done_p(o, stop, r, bars[i][4], "time_exit", i, pol["hold"] - 1, s, eff_stop, be_at, audit)


def _done_p(entry, stop, r, px, why, i, k, s, eff_stop, be_at, audit):
    return {"status": "entered", "entry": entry, "stop": stop, "risk": r, "exit_price": px, "exit_reason": why, "exit_i": i,
            "days_held": k + 1, "r_mult": round(s * (px - entry) / r, 6), "final_stop": eff_stop, "breakeven_from_i": be_at,
            "audit": audit}
