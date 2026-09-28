"""方向台账族 D 的统计合同（可执行版；conviction v1.3 / skew-v1 附录 2 引用本模块常量，唯一来源）。

Codex 020：
- 020-01：单侧下界 = bootstrap 分布的 α 分位（α=0.05/7），不是 1−α 分位（那是上端点）。分位算法写死：
  逆经验分布（type 1）：q_p = 升序第 ceil(p·B) 个值（1 起算，至少第 1 个）。
- 020-03：skew-v1 六项不再用「同期估计上涨率当已知 p0」的二项检验 —— 与 conviction 同一套：事件与对照在
  同一次时间块重抽中一起重算。
- conviction 分层边界：见 regime / stratum_support / MIN_NONEVENT_DAYS 等。

纯计算：只吃数值行，不读文件、不联网。行 = {inst, t(交易日序号), session, quarter, regime, eligible, s, r}：
  t —— 完整交易日轴上的序号（含空事件日），供时间块重抽；
  eligible —— 【仅】决策时资格：该日正式预测在截止前身份与质量都合格（前瞻 v2 policy 的 eligible）。
              与结果是否成熟无关 —— r 成熟与否单独由 r 是否为 None 表示（Codex 021-01/022）；
  s —— 方向 −1/0/+1（None = 未知，不算事件也不算对照）；r —— 按日历终点的 h 日收益。
"""
from __future__ import annotations

import math
import random
from datetime import date

FAMILY_SIZE = 7
ALPHA = 0.05 / FAMILY_SIZE
QUANTILE_METHOD = "inverse_ecdf_type1: q_p = sorted[ceil(p*B)-1]"
BLOCK_DAYS = {1: 10, 5: 10, 10: 20}       # 主规格：≥ 2 × 终点期限（5 日终点取 10；10 日终点取 20）
SENSITIVITY_BLOCKS = (5, 20)               # 只报告，不参与判定、不许挑
ITERS = 10000
SEED = 20260928
MIN_NONEVENT_DAYS = 5                      # 分层共同支持：层内至少 5 个合格的非事件日（固定设计，未校准）
MAX_INVALID_FRAC = 0.05
SMA_DAYS = 200
#: 方向台账族 D 正式前瞻起点（Codex 022 批准冻结；冻结提交在 2026-09-28 ET 盘前、当日盘前记录已完成 →
#: 下一个有效交易日）。早于此日的 session（含 9/28 的 skew 与 conviction 开发期记录）一律不进确认样本。
FAMILY_D_START = date(2026, 9, 29)


def quantile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        raise ValueError("空样本没有分位数")
    k = max(1, math.ceil(p * len(sorted_vals)))
    return sorted_vals[min(k, len(sorted_vals)) - 1]


def regime(closes_through_prev: list[float]) -> str | None:
    """S−1 收盘相对 200 日均线：≥ 均线（含恰好相等）→ "up"；< → "down"；不足 200 个有效收盘 → None（该日不入分析）。"""
    xs = [c for c in closes_through_prev[-SMA_DAYS:] if c is not None]
    if len(closes_through_prev) < SMA_DAYS or len(xs) < SMA_DAYS:
        return None
    return "up" if xs[-1] >= sum(xs) / SMA_DAYS else "down"


def quarter(session: date) -> str:
    """按入组 session 的日历季度（不是收益终点所在季度）。"""
    return f"{session.year}Q{(session.month - 1) // 3 + 1}"


def published(row: dict) -> bool:
    """【决策时】已作为合格正式预测发布的方向事件（与结果是否成熟无关）。"""
    return bool(row.get("eligible")) and row.get("s") in (1, -1)


def usable(row: dict) -> bool:
    """进入统计的行：决策时合格 + 结果已成熟 + regime 与 s 已知。"""
    return bool(row.get("eligible")) and row.get("r") is not None and row.get("regime") is not None \
        and row.get("s") is not None


def nonoverlap(rows: list[dict], h: int) -> list[dict]:
    """在【原始时间轴】上固定事件资格（Codex 021-01：预测资格与结果资格分开）：
      · 只有决策时合格发布的方向事件（published）参与冷却；从未合格发布的行（eligible=False）不占冷却；
      · 已发布但结果缺价/未成熟的事件【照样占冷却】（事前政策：不让后续数据可得性改写事件序列），
        它本身因 r=None 不进统计；
      · 同品种按 t 升序，与上一个保留事件相隔 ≥ h 个交易日才保留。重抽时不重选。
    非事件行原样保留作对照。返回加了 kept 标记的新行列表。"""
    out, last = [], {}
    for r in sorted(rows, key=lambda x: (x["inst"], x["t"])):
        r = dict(r)
        if published(r):
            lt = last.get(r["inst"])
            r["kept"] = lt is None or r["t"] - lt >= h
            if r["kept"]:
                last[r["inst"]] = r["t"]
        else:
            r["kept"] = False
        out.append(r)
    return out


def _stratum(r: dict) -> tuple:
    return (r["inst"], r["regime"], r["quarter"])


def support(rows: list[dict]) -> dict:
    """在【原始面板的唯一 (品种, t)】上冻结共同支持（Codex 021-02）：层内 ≥1 个 kept 事件且
    ≥ MIN_NONEVENT_DAYS 个唯一非事件可用日。重抽只改变权重与 μ_k 重估，不能让原先被排除的层复活。
    返回 {"valid": set(层), "events": set((inst,t)) 最终可比事件, "strata": n, "no_support": n, "excluded_events": n}。"""
    uniq = {}
    for r in rows:
        if usable(r):
            uniq.setdefault((r["inst"], r["t"]), r)
    by: dict = {}
    for r in uniq.values():
        by.setdefault(_stratum(r), []).append(r)
    valid, events, no_sup, excl = set(), set(), 0, 0
    for k, rs in by.items():
        ev = [r for r in rs if r.get("kept") and r["s"] in (1, -1)]
        non = [r for r in rs if r["s"] == 0]
        if not ev:
            continue
        if len(non) < MIN_NONEVENT_DAYS:
            no_sup += 1; excl += len(ev)
            continue
        valid.add(k)
        events.update((r["inst"], r["t"]) for r in ev)
    return {"valid": valid, "events": events, "strata": len(by), "no_support": no_sup, "excluded_events": excl}


def d_reg(rows: list[dict], sup: dict | None = None) -> tuple[float | None, dict]:
    """D_reg = Σ_k n_k·mean_{事件∈k}[s·(r−μ_k)] / Σ_k n_k，只在冻结的合格层 sup["valid"] 内、只对冻结的事件集计算。
    μ_k = 该层在本次（重抽）样本中全部可用行的 r 均值（含事件，副本按出现次数加权）。
    某层在本次样本中没有事件或没有非事件对照行 → 该层本次不贡献；所有层都不贡献 → (None, diag) = 无效重抽。
    sup=None → 以本批 rows 自身冻结（点估计用）。"""
    sup = sup or support(rows)
    strata: dict = {}
    for r in rows:
        if usable(r) and _stratum(r) in sup["valid"]:
            strata.setdefault(_stratum(r), []).append(r)
    num = den = 0.0
    diag = {"strata_valid": len(sup["valid"]), "strata_no_support": sup["no_support"],
            "events_excluded_no_support": sup["excluded_events"], "strata_contributing": 0, "event_rows": 0}
    for rs in strata.values():
        ev = [r for r in rs if r.get("kept") and r["s"] in (1, -1) and (r["inst"], r["t"]) in sup["events"]]
        non = [r for r in rs if r["s"] == 0]
        if not ev or not non:
            continue
        mu = sum(r["r"] for r in rs) / len(rs)
        num += sum(r["s"] * (r["r"] - mu) for r in ev)
        den += len(ev)
        diag["strata_contributing"] += 1
        diag["event_rows"] += len(ev)
    return (num / den if den else None), diag


def final_event_count(rows: list[dict]) -> int:
    """「只数事件」：最终可比事件数 = 冻结共同支持后的唯一事件数。只看 r 是否成熟（is not None），不读收益值。"""
    masked = [dict(r, r=0.0 if r.get("r") is not None else None) for r in rows]
    return len(support(masked)["events"])


def block_bootstrap(rows: list[dict], stat, *, block: int, iters: int = ITERS, seed: int = SEED) -> tuple[list, int]:
    """完整交易日轴上的循环移动块重抽：同一日的所有品种、事件与对照一起抽；空事件日也在轴上。
    每次重抽都重新计算 stat（其中的层基准 μ_k 随之重估）。返回 (有效统计量列表, 无效次数)。"""
    days = sorted({r["t"] for r in rows})
    if not days:
        return [], iters
    t0, t1 = days[0], days[-1]
    axis = list(range(t0, t1 + 1))
    by = {}
    for r in rows:
        by.setdefault(r["t"], []).append(r)
    n = len(axis)
    rng = random.Random(seed)
    out, bad = [], 0
    for _ in range(iters):
        samp = []
        while len(samp) < n:
            start = rng.randrange(n)
            for j in range(block):
                samp.append(axis[(start + j) % n])
        samp = samp[:n]
        v, _ = stat([r for d in samp for r in by.get(d, [])])
        if v is None:
            bad += 1
        else:
            out.append(v)
    return out, bad


def judge(rows: list[dict], h: int, *, min_events: int, block: int | None = None, iters: int = ITERS,
          seed: int = SEED, alpha: float = ALPHA) -> dict:
    """一次判定（唯一顺序）：① 固定事件资格 → ② 只数最终事件，不足即 insufficient（不计算收益统计）→
    ③ 点估计 → ④ 重抽，无效比例 > 5% → insufficient → ⑤ 单侧下界 q_α、双侧 95% 描述区间 → ⑥ 下界 > 0 → detected。"""
    rows = nonoverlap(rows, h)
    n_final = final_event_count(rows)
    res = {"h": h, "alpha": alpha, "quantile_method": QUANTILE_METHOD, "n_final_events": n_final,
           "min_events": min_events}
    if n_final < min_events:
        return {**res, "verdict": "insufficient", "reason": "最终可比事件数不足（未计算任何收益统计）"}
    sup = support(rows)                                   # 原始面板上冻结一次，点估计与重抽用同一分析对象
    point, diag = d_reg(rows, sup)
    samples, bad = block_bootstrap(rows, lambda rs: d_reg(rs, sup), block=block or BLOCK_DAYS.get(h, 2 * h),
                                   iters=iters, seed=seed)
    res.update({"point": point, "diag": diag, "iters": iters, "invalid": bad})
    if not samples or bad / iters > MAX_INVALID_FRAC:
        return {**res, "verdict": "insufficient", "reason": f"无效重抽 {bad}/{iters} 超过 {MAX_INVALID_FRAC:.0%}"}
    s = sorted(samples)
    res.update({"lower_one_sided": quantile(s, alpha), "ci95": (quantile(s, 0.025), quantile(s, 0.975))})
    res["verdict"] = "detected" if res["lower_one_sided"] > 0 else "not_detected"
    return res
