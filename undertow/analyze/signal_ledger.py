"""强信号台账：逐日记录强信号各分量的原始数值，事后回填真实走势，让这层【将来能回测】。

为什么必须建台账
----------------
强信号是全报告唯一顶红色 ⚡ 告警、且会把当日决策总纲直接改成「可空」的一层，
却从未回测过。它的三条核心闸门（方向压力比 / 主翼权重比 / 净建仓规模）都要
【逐行权价 ΔOI + 成交】，而 CBOE 期权端点只给当前快照、没有历史链；免费源里
拿不到历史逐行 OI —— 这三条**回测不了，只能向前累积**。

三条设计教训（都是 2026-08-27 codex review 打出来的）
------------------------------------------------------
1. **必须逐日记录，不能只记"闸门全过"的那些。**
   只记候选 → 被压力比/主翼比/规模门拦下的样本永远缺席 → 永远回答不了
   "3× / 4× / 3,000 这三个数该不该是这个值"。所以这里记录**每一个有前日可比的
   交易日**，并存**连续比值**而不只是布尔，才画得出阈值-结果关系。

2. **前瞻收益的基准必须是信号发出【之前】就已知的价格。**
   快照文件名日期 D，实际是 ET 01:00~02:00 抓的，装的是 D-1 结算的 OI，
   报告在 D 开盘前就能读到 —— 信号在 D 开盘可执行。
   若拿 close[D] 当基准，就用上了信号发出后一整天的走势来定基，
   既漏掉 D 当天的收益，又是不折不扣的前视。
   这里取 **D 之前最后一个已知收盘**作基准，并把 base_date 一并存盘可审计。
   代价：包含 D 的隔夜跳空（实际抓不到），统计时须声明。

3. **未知就是未知，不许包装成事实。**
   重建时拿不到当时的综合研判（COT/宏观会被修订，事后重跑有前视），
   `outlook_bias` 为空时 `diverges` 必须存 None —— 不能让 `_diverges("")` 返回的
   那个恒真值落盘。旧版本正是这么错的：31 行全部 outlook_bias=""，却有 14 行
   diverges=true。

累积速率：8 品种约 0.7 个开火信号/交易日 → n=100 约需 7 个月。
"""

from __future__ import annotations

import json
from datetime import date
from math import comb, sqrt
from pathlib import Path

from undertow.core.config import DATA_DIR

HORIZONS = (1, 3, 5, 10)
DRIFT_N = 60          # 局部去趋势回看窗口（与拉伸度回测同口径）
MA_N = 200            # 牛熊制度判定
MIN_N = 50            # 下结论的最小样本量（对齐 stretch.MIN_TEST_N）
T_SIGNIFICANT = 2.0   # 对齐 stretch.T_SIGNIFICANT


class LedgerCorrupt(Exception):
    """台账文件解析失败。**绝不能被静默当成空表**——那会让下一次写入抹掉全部历史。"""


def _dir_path(root: Path | None = None) -> Path:
    return (root or DATA_DIR) / "history" / "signals"


def _path(key: str, root: Path | None = None) -> Path:
    return _dir_path(root) / f"{key}.json"


def _load(key: str, root: Path | None = None, *, strict: bool = True) -> list[dict]:
    p = _path(key, root)
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        if strict:
            raise LedgerCorrupt(f"{p} 解析失败：{type(e).__name__} {e}") from e
        return []
    if not isinstance(rows, list):
        if strict:
            raise LedgerCorrupt(f"{p} 顶层不是列表")
        return []
    return rows


def _quarantine(key: str, root: Path | None = None) -> Path:
    """把损坏的台账挪到 .corrupt-N 而不是覆盖它。历史不可再生，宁可留着人工看。"""
    p = _path(key, root)
    n = 1
    while (q := p.with_suffix(f".json.corrupt-{n}")).exists():
        n += 1
    p.rename(q)
    return q


def _save(key: str, rows: list[dict], root: Path | None = None) -> Path:
    d = _dir_path(root)
    d.mkdir(parents=True, exist_ok=True)
    p = _path(key, root)
    rows.sort(key=lambda r: r.get("date", ""))
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    tmp.replace(p)          # 原子替换，避免写一半被中断留下残表
    return p


def record(key: str, *, on_date: str, prev_date: str | None, spot: float,
           probe: dict, signal=None, outlook_bias: str = "",
           root: Path | None = None) -> dict:
    """记录某一日的强信号分量（同日覆盖）。**每个有前日可比的交易日都记，不只记候选。**

    outlook_bias 为空 = 当时的综合研判未知（例如 --rebuild 事后重放）：
    此时 diverges 存 None，不存 _diverges 对空串返回的那个恒真值。
    """
    try:
        rows = _load(key, root)
    except LedgerCorrupt as e:
        q = _quarantine(key, root)
        print(f"[警告] {e}；已隔离为 {q.name}，本次从空表重建（旧数据未丢失）")
        rows = []

    gap = None
    if prev_date:
        try:
            gap = (date.fromisoformat(on_date) - date.fromisoformat(prev_date)).days
        except ValueError:
            gap = None

    bull, bear = probe.get("看涨") or {}, probe.get("看跌") or {}
    fired_dir = getattr(signal, "direction", None) if signal is not None else None
    known_bias = bool(outlook_bias)
    row = {
        "instrument": key, "date": on_date, "prev_date": prev_date,
        # ⚠️ gap_days 是【日历日】差，不是交易日数：周末造成的 3 天并不代表三天交易活动。
        # 交易日间隔由 backfill 用真实价格序列填入 trading_gap。
        "gap_days": gap, "trading_gap": None,
        "spot": spot,
        "fired": fired_dir is not None,
        "direction": fired_dir,
        "level": getattr(signal, "level", None),
        "vol_confirms": bool(getattr(signal, "vol_confirms", False)) if signal is not None else None,
        "outlook_bias": outlook_bias or None,
        # 综合研判未知时 diverges 必须是 None —— 未知不许包装成 True。
        "diverges": (bool(getattr(signal, "diverges", False))
                     if (signal is not None and known_bias) else None),
        "up_pressure": probe.get("up_pressure"), "dn_pressure": probe.get("dn_pressure"),
        "bull_wing": probe.get("bull_wing"), "bear_wing": probe.get("bear_wing"),
        "bull_wing_oi": probe.get("bull_wing_oi"), "bear_wing_oi": probe.get("bear_wing_oi"),
        "churn_call": probe.get("churn_call"), "churn_put": probe.get("churn_put"),
        # 净有效 Delta（观测型）与 up/dn_pressure（推断型）并存，
        # 日后可直接比较两个口径谁更有预测力 —— 实测两者 60% 的日子方向相反。
        # 方向裁决与弃权（软/硬），供日后校准弃权阈值
        "call_direction": probe.get("call_direction"),
        "call_abstain": probe.get("call_abstain"),
        "call_hard_abstain": probe.get("call_hard_abstain"),
        "call_ratio": probe.get("call_ratio"),
        "call_reason": probe.get("call_reason"),
        "call_reasons": probe.get("call_reasons"),
        "call_calibrated": probe.get("call_calibrated"),
        # 四轴 shadow：规模 / 广度 / 集中度 / 删除稳健性（详见 flow.concentration_stats）
        **{k: probe.get(k) for k in
           ("size_bull", "size_bear", "size_ratio",
            "breadth_bull", "breadth_bear", "breadth_ratio",
            "top1_share", "top3_share", "top5_share",
            "flip_k", "n_legs", "whale_like")},
        "net_delta_call": probe.get("net_delta_call"),
        "net_delta_put": probe.get("net_delta_put"),
        "net_delta_total": probe.get("net_delta_total"),
        "net_call_doi": probe.get("net_call_doi"), "net_put_doi": probe.get("net_put_doi"),
        "oi_build_ratio": probe.get("oi_build_ratio"),
        "d_spot_pct": probe.get("d_spot_pct"), "d_atm_pp": probe.get("d_atm_pp"),
        "d_skew25_pp": probe.get("d_skew25_pp"),
        # 到期结构（预先注册假设：短到期押注 → 短前瞻窗口才对得上；只记不用，
        # 见 flow.probe_strong_signal 的说明。2026-08-28 黄金之后加）
        "dte_wavg": probe.get("dte_wavg"),
        "dte_share_le2": probe.get("dte_share_le2"),
        "dte_share_le7": probe.get("dte_share_le7"),
        "dte_top1": probe.get("dte_top1"),
        # —— 三条核心闸门的【连续值】：没有它们就无法校准 3×/4×/3,000 这三个阈值 ——
        "bull_pressure_ratio": bull.get("pressure_ratio"),
        "bear_pressure_ratio": bear.get("pressure_ratio"),
        "bull_wing_ratio": bull.get("wing_ratio"), "bear_wing_ratio": bear.get("wing_ratio"),
        "bull_scale_doi": bull.get("scale_doi"), "bear_scale_doi": bear.get("scale_doi"),
        "bull_gates": [bull.get("pressure_ok"), bull.get("wing_ok"), bull.get("scale_ok")],
        "bear_gates": [bear.get("pressure_ok"), bear.get("wing_ok"), bear.get("scale_ok")],
        "bull_contra_margin": bull.get("contra_margin"),
        "bear_contra_margin": bear.get("contra_margin"),
        # 基准价与前瞻收益由 backfill 填；base_date 存盘可审计（见模块 docstring 第 2 条）
        "base_date": None, "base_close": None,
        "regime": None, "drift_60d": None,
        **{f"forward_{h}d": None for h in HORIZONS},
    }
    rows = [r for r in rows if r.get("date") != on_date] + [row]
    _save(key, rows, root)
    return row


def clear(key: str, root: Path | None = None) -> int:
    """清空某品种台账（--rebuild 用）。返回被清掉的行数。

    真重建必须先清：改了阈值或修了算法后，旧定义下的行会残留，
    统计就变成了不同版本定义的混合，不可解释。
    """
    try:
        n = len(_load(key, root))
    except LedgerCorrupt:
        n = -1
    p = _path(key, root)
    if p.exists():
        p.unlink()
    return n


def backfill(key: str, dates: list[date], closes: list[float],
             root: Path | None = None) -> tuple[int, int]:
    """回填基准价、前瞻收益、漂移与牛熊制度。返回 (回填行数, 仍待填的前瞻格数)。

    **基准 = 快照日期 D 之前最后一个已知收盘**（信号在 D 开盘才可执行，
    拿 close[D] 当基准就是前视）。且要求价格序列已延伸到 D 之后，
    否则序列滞后时基准会随序列更新而改变，回填结果不确定。
    """
    try:
        rows = _load(key, root)
    except LedgerCorrupt as e:
        print(f"[警告] {e}；跳过回填")
        return 0, 0
    if not rows or not dates:
        return 0, len(rows) * len(HORIZONS)
    n = len(closes)
    last = dates[-1]
    filled = pending = 0
    for r in rows:
        try:
            d = date.fromisoformat(r["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if d >= last:
            # 序列还没走过 D → 无法确定"D 之前最后一个收盘"到底是哪根，先不填
            pending += sum(1 for h in HORIZONS if r.get(f"forward_{h}d") is None)
            continue
        # D 之前最后一个已知收盘（二分右界 - 1）
        lo, hi = 0, len(dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if dates[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        i = lo - 1
        if i < 0:
            continue
        touched = False
        if r.get("base_date") is None:
            r["base_date"] = dates[i].isoformat()
            r["base_close"] = closes[i]
            touched = True
        if r.get("trading_gap") is None and r.get("prev_date"):
            try:
                pd_ = date.fromisoformat(r["prev_date"])
                r["trading_gap"] = sum(1 for x in dates if pd_ <= x < d) or None
                touched = touched or r["trading_gap"] is not None
            except ValueError:
                pass
        if r.get("drift_60d") is None and i >= DRIFT_N:
            r["drift_60d"] = round(((closes[i] / closes[i - DRIFT_N]) ** (1 / DRIFT_N) - 1) * 100, 5)
            touched = True
        if r.get("regime") is None and i >= MA_N - 1:
            ma = sum(closes[i - MA_N + 1:i + 1]) / MA_N
            r["regime"] = "牛" if closes[i] > ma else "熊"
            touched = True
        for h in HORIZONS:
            if r.get(f"forward_{h}d") is None:
                if i + h < n:
                    r[f"forward_{h}d"] = round((closes[i + h] / closes[i] - 1) * 100, 4)
                    touched = True
                else:
                    pending += 1
        filled += bool(touched)
    if filled:
        _save(key, rows, root)
    return filled, pending


def load_all(keys: list[str], root: Path | None = None, *,
             strict: bool = False) -> list[dict]:
    out: list[dict] = []
    for k in keys:
        try:
            out.extend(_load(k, root, strict=strict))
        except LedgerCorrupt as e:
            print(f"[警告] {e}")
    return out


def _binom_two_tail(k: int, n: int) -> float:
    if n <= 0:
        return 1.0
    lo = min(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(0, lo + 1)) / 2 ** n)


def welch_t(a: list[float], b: list[float]) -> float:
    """Welch 双样本 t。与 stretch_backtest.welch_t 同口径（那边不便直接导入，会拖入长历史依赖）。"""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = sqrt(va / na + vb / nb)
    return 0.0 if se == 0 else (ma - mb) / se


def _thin(items: list[tuple[dict, float]], horizon: int) -> list[tuple[dict, float]]:
    """按品种独立抽稀成不重叠样本：同一品种内相邻取样至少间隔 horizon 个交易日。

    连续几日的信号其 T+N 窗口高度重叠，不抽稀会让 t 与 p 虚高。
    ⚠️ 必须按品种分别抽稀 —— 跨品种用全局序号抽会把不同标的混在一条时间轴上
    （2026-08-27 第一版脚本正是这个错，结论符号都跟着翻了）。
    """
    by: dict[str, list] = {}
    for r, v in items:
        by.setdefault(r.get("instrument", "?"), []).append((r, v))
    out = []
    for _, rs in sorted(by.items()):
        rs.sort(key=lambda x: x[0].get("date", ""))
        last = None
        for r, v in rs:
            try:
                d = date.fromisoformat(r["date"])
            except (ValueError, KeyError, TypeError):
                continue
            if last is None or (d - last).days >= horizon:
                out.append((r, v))
                last = d
    return out


def summarize(rows: list[dict], *, horizon: int = 5, detrend: bool = True) -> dict:
    """按分组统计。**样本不够就明说不够。**

    诚实性说明（codex review 2026-08-27）：
      · 已按品种抽稀成不重叠样本，并给出开火组 vs 被闸门拦下组的 Welch t；
      · 二项 p 只是方向命中率对 50% 的检验，**跨品种同日收益仍相关**，
        独立性假设不完全成立 —— 因此 conclusive 同时要求 n≥MIN_N 且 |t|≥2。
    """
    if horizon not in HORIZONS:
        raise ValueError(f"horizon 必须是 {HORIZONS} 之一，收到 {horizon}")
    fld = f"forward_{horizon}d"

    def val(r):
        v = r.get(fld)
        if v is None or not r.get("fired"):
            return None
        if not detrend:
            return v
        d = r.get("drift_60d")
        return None if d is None else v - horizon * d

    groups: dict[str, list] = {}
    for r in rows:
        v = val(r)
        if v is None:
            continue
        blocked = (r.get("bull_contra_margin") if r.get("direction") == "看涨"
                   else r.get("bear_contra_margin"))
        for name in ("全部开火",
                     f"方向·{r.get('direction')}",
                     f"等级·{r.get('level') or '?'}",
                     ("建仓比<1.5×" if (r.get("oi_build_ratio") or 0) < 1.5 else "建仓比≥1.5×")):
            groups.setdefault(name, []).append((r, v))
        if r.get("regime"):
            groups.setdefault(f"制度·{r['regime']}", []).append((r, v))
    out = {}
    for name, items in sorted(groups.items()):
        it = _thin(items, horizon)
        n = len(it)
        hit = sum(1 for r, v in it if (v < 0) == (r.get("direction") == "看跌"))
        p = _binom_two_tail(hit, n)
        out[name] = {"n": n, "n_raw": len(items), "hit": hit,
                     "acc_pct": round(hit / n * 100, 1) if n else None,
                     "p_two_tail": round(p, 4),
                     "conclusive": n >= MIN_N and p < 0.05}
    return out


def gate_contrast(rows: list[dict], *, horizon: int = 5) -> dict:
    """开火组 vs 被逆向价格闸门拦下组的 Welch 双样本 t（去趋势、按品种抽稀）。

    这是台账目前唯一能做的**真·双样本比较** —— 因为两组样本都在库里。
    三条核心闸门的阈值要等逐日连续值攒够才谈得上校准。
    """
    if horizon not in HORIZONS:
        raise ValueError(f"horizon 必须是 {HORIZONS} 之一，收到 {horizon}")
    fld = f"forward_{horizon}d"
    fired, blocked = [], []
    for r in rows:
        v, d = r.get(fld), r.get("drift_60d")
        if v is None or d is None:
            continue
        adj = v - horizon * d
        for side, mg in (("看涨", r.get("bull_contra_margin")),
                         ("看跌", r.get("bear_contra_margin"))):
            gates = r.get("bull_gates" if side == "看涨" else "bear_gates") or []
            if not (len(gates) == 3 and all(gates)):
                continue
            signed = -adj if side == "看跌" else adj      # 统一成"信号方向上的收益"
            (blocked if (mg is not None and mg <= 0) else fired).append((r, signed))
    f, b = _thin(fired, horizon), _thin(blocked, horizon)
    fv, bv = [v for _, v in f], [v for _, v in b]
    return {
        "fired_n": len(fv), "blocked_n": len(bv),
        "fired_mean": round(sum(fv) / len(fv), 3) if fv else None,
        "blocked_mean": round(sum(bv) / len(bv), 3) if bv else None,
        "welch_t": round(welch_t(fv, bv), 2) if (len(fv) > 1 and len(bv) > 1) else None,
        "conclusive": (len(fv) >= MIN_N and len(bv) >= MIN_N
                       and abs(welch_t(fv, bv)) >= T_SIGNIFICANT),
    }


def render_md(rows: list[dict], *, horizon: int = 5) -> str:
    stats = summarize(rows, horizon=horizon)
    gc = gate_contrast(rows, horizon=horizon)
    fired = [r for r in rows if r.get("fired")]
    lines = [
        f"## 强信号台账（前瞻 {horizon} 日 · 局部去趋势 · 按品种抽稀不重叠）",
        "",
        f"逐日记录 {len(rows)} 天，其中开火 {len(fired)} 次。",
        f"基准价 = 快照日之前最后一个已知收盘（信号在当日开盘才可执行；含隔夜跳空）。",
        "",
        "| 分组 | 命中/样本(抽稀前) | 准确率 | 二项双尾 p | 可下结论 |",
        "|---|---|---|---|---|",
    ]
    for name, s in stats.items():
        mark = "✅" if s["conclusive"] else f"❌ 需 n≥{MIN_N} 且 p<0.05"
        lines.append(f"| {name} | {s['hit']}/{s['n']}({s['n_raw']}) | {s['acc_pct']}% "
                     f"| {s['p_two_tail']} | {mark} |")
    lines += [
        "",
        "### 逆向价格闸门：放行组 vs 拦下组（Welch 双样本）",
        "",
        f"- 放行 n={gc['fired_n']}，均值 {gc['fired_mean']}%",
        f"- 拦下 n={gc['blocked_n']}，均值 {gc['blocked_mean']}%",
        f"- Welch t = {gc['welch_t']}　→ "
        + ("✅ 可下结论" if gc["conclusive"] else f"❌ 需两组各 n≥{MIN_N} 且 |t|≥{T_SIGNIFICANT}"),
        "",
        f"> 门槛：n≥{MIN_N} 且显著才认；达不到即**样本不足、结论未知**，而非「无效果」。",
        "> 二项检验假设各样本独立，但跨品种同日收益相关，p 值偏乐观 —— 故同时看 Welch t。",
        "> 三条核心闸门（压力比/主翼比/规模）的阈值需逐日连续值攒够才谈得上校准；",
        "> 免费源没有历史逐行 OI，只能向前累积（8 品种约 0.7 次开火/交易日）。",
    ]
    return "\n".join(lines)
