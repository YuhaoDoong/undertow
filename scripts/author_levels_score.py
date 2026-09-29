"""外部作者价位类判断的统一计分（用户 2026-09-29：「多记录多测试……这样才能拿到信服结果」）。

读私有 data/soul/author_levels.jsonl，写私有 data/soul/author_levels_scored.json；本脚本只含规则，不含任何作者内容。
  python3 scripts/author_levels_score.py [--asof ISO时刻]

当前主结果 = v3（VERSION_V3，Codex 026；见文件中部 v3 段落）：按交易所日历计会话窗口、会话内小时线覆盖 ≥ 90% 才可判
「未发生」（否则 incomplete_window）、日线收盘按结算时刻（GC 13:30 / 布伦特 14:30 ET）判断是否晚于发布、破位严格 < 与触及 ≤
分开、直接合约按最小报价单位规范且 tol=0。v3 是看过 v2 结果后的口径修正，不是新证据；v2 结果并存（score_v2）供对照。

以下为 v2 说明（VERSION，Codex 025-3 修订；这是**未校准的探索评分协议**，阈值不按已看到的结果调）：
- 只用【完整】K 线：小时线 bar_end = 起点 + 1h ≤ asof；日线 session_close（期货 17:00 ET）≤ asof。
  小时线只取起点 ≥ 发布时刻的；跨截止时刻的那根不计入窗口（它可能改变结论时 → ambiguous）。
- 穿越一律三态（所有分支同一口径）：代理误差 tol 内 → maybe；越过 tol → yes。
  yes 才能终判；只有 maybe → ambiguous；窗口不完整且无 yes → pending。
- support L：20 个完整交易日内首次触及（最低 ≤ L×1.001）→ 触及日起 6 个完整交易日（触及日 + 5）内
  **任一**收盘 < L×0.995 → broken；整段都没破 → held（「整段守住」语义）。另记 rebound_first
  （破位前是否先有收盘 ≥ L×1.01）与 later_broken，只作描述，不混入胜率。未触及 → untouched（要报告占比）。
  resistance 镜像。发布时刻已在价位另一侧 → status_at_post=already_through，单列。
- no_support L：20 个完整交易日内有收盘 < L×0.995 → hit；窗口满且无 → miss。
- break_below L by deadline：截止前完整小时线最低 < L → hit；asof 未过截止 → pending；过了且未破 → miss。
- range lo–hi 共 N 日：N 个完整日收盘都在 [lo×0.995, hi×1.005] → held，否则 broken。
- trade_long entry/stop/target 共 N 日：**先要入场触发**（发布后首根的开盘在 entry 之上则最低 ≤ entry，反之最高 ≥ entry）；
  除非台账写明 entry_status=declared_filled。未触发 → not_triggered（不算赢也不算输）。触发那根里若同时碰到止损/目标 → ambiguous；
  之后逐根看止损与目标谁先（同根都碰 → ambiguous）。
价格：黄金用 COMEX 期货 GC=F，以持有成本折算现货（S = F·e^(−rT)，r=4.5% 固定假设，T 到假定主力合约交割月中）——
**proxy_unverified**：换月、利率、储运成本都未经独立现货校准，tol=0.5% 只是设定，不是误差上界。
原油用作者指定的合约（contract_direct）。数据来自 Yahoo 公开 chart 接口（只读）。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "data/soul/author_levels.jsonl"
OUT = ROOT / "data/soul/author_levels_scored.json"
OUT_V1 = ROOT / "data/soul/author_levels_scored_v1.json"
VERSION = "author-levels-v2-20260929"
R = 0.045
GOLD_MONTHS = (2, 4, 6, 8, 12)
TOUCH_DAYS, FOLLOW_DAYS = 20, 5
ET = ZoneInfo("America/New_York")
H1 = timedelta(hours=1)


def yahoo_daily(sym: str, rng: str = "2y") -> list[tuple]:
    from undertow.collect.base import http_get_json
    d = http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d")
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for t, o, h, lo, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, lo, c):
            continue
        out.append((datetime.fromtimestamp(t, timezone.utc).date(), o, h, lo, c))
    return out


def gold_T(d: date) -> float:
    """假定的主力合约（交割月前一个月月末仍在 d 之后的最近活跃月，跳过 10 月）到交割月中的年数。未经逐日合约映射核实。"""
    for y in (d.year, d.year + 1):
        for m in GOLD_MONTHS:
            if date(y, m, 1) - timedelta(days=1) > d:
                return (date(y, m, 15) - d).days / 365
    return 0.1


def to_spot(bars):
    return [(d, *(x * math.exp(-R * gold_T(d)) for x in v)) for d, *v in bars]


def yahoo_hourly(sym: str) -> list[tuple]:
    """(bar 起点 UTC, open, high, low, close)。Yahoo 1h 可回溯约 730 天。"""
    from undertow.collect.base import http_get_json
    d = http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=730d&interval=1h")
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    return [(datetime.fromtimestamp(t, timezone.utc), o, h, lo, c)
            for t, o, h, lo, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, lo, c)]


def to_spot_h(bars):
    return [(t, *(x * math.exp(-R * gold_T(t.date())) for x in v)) for t, *v in bars]


def cross(x: float, thr: float, below: bool, tol: float) -> str:
    """三态穿越：below=True 判 x 是否跌到 thr 以下。越过 tol 才算 yes；tol 内 maybe；否则 no。"""
    if below:
        return "yes" if x <= thr * (1 - tol) else ("maybe" if x <= thr * (1 + tol) else "no")
    return "yes" if x >= thr * (1 + tol) else ("maybe" if x >= thr * (1 - tol) else "no")


def _close_at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 17, tzinfo=ET)


def _first(seq, key):
    """seq 中第一个 yes 的元素；以及它之前是否出现过 maybe。"""
    maybe = False
    for x in seq:
        k = key(x)
        if k == "yes":
            return x, maybe
        maybe = maybe or k == "maybe"
    return None, maybe


def _ts(h) -> str:
    return h[0].strftime("%Y-%m-%d %H:%MZ")


def score(r: dict, daily, hourly, tol: float, asof: datetime) -> dict:
    posted = datetime.fromisoformat(r["posted_at"]).astimezone(timezone.utc)
    asof = asof.astimezone(timezone.utc)
    D = [d for d in daily if _close_at(d[0]) > posted and _close_at(d[0]) <= asof]           # 完整、发布后收盘
    H = [h for h in hourly if h[0] >= posted and h[0] + H1 <= asof]                             # 完整、发布后起点
    before = [h for h in hourly if h[0] + H1 <= posted]
    p, c = r["params"], r["claim"]
    base = {"asof": asof.isoformat(), "n_daily": len(D), "n_hourly": len(H)}

    def res(result, **kw):
        return {**base, "result": result, **kw}

    if c in ("support", "resistance", "no_support"):
        full = len(D) >= TOUCH_DAYS
        Dw = D[:TOUCH_DAYS]
        horizon_end = _close_at(Dw[-1][0]) if full else asof
        Hw = [h for h in H if h[0] + H1 <= horizon_end]
    if c in ("support", "resistance"):
        L, sup = p["level"], c == "support"
        at_post = None
        if before:
            at_post = "already_through" if cross(before[-1][4], L, sup, 0) == "yes" else "approach"
        t, touch_maybe = _first(Hw, lambda h: cross(h[3] if sup else h[2], L * (1.001 if sup else 0.999), sup, tol))
        if t is None:
            if not Hw:
                return res("pending", why="发布后尚无完整小时线", status_at_post=at_post)
            ext = min(h[3] for h in Hw) if sup else max(h[2] for h in Hw)
            if touch_maybe:
                return res("ambiguous", why="只在代理误差内接近", extreme=round(ext, 2), status_at_post=at_post)
            return res("untouched" if full else "pending", extreme=round(ext, 2), status_at_post=at_post)
        tday = t[0].astimezone(ET).date()
        fol = [d for d in D if d[0] >= tday][:FOLLOW_DAYS + 1]
        thr_b, thr_h = (L * 0.995, L * 1.01) if sup else (L * 1.005, L * 0.99)
        b, b_maybe = _first(fol, lambda d: cross(d[4], thr_b, sup, tol))
        reb, _ = _first(fol, lambda d: cross(d[4], thr_h, not sup, 0))
        extra = {"touch_at": _ts(t), "touch_extreme": round(t[3] if sup else t[2], 2), "touch_time_ambiguous": touch_maybe,
                 "status_at_post": at_post, "rebound_first": bool(reb and (b is None or reb[0] < b[0])),
                 "later_broken": bool(b and reb and reb[0] < b[0])}
        if b is not None:
            return res("broken", decisive_day=str(b[0]), **extra)
        if b_maybe:
            return res("ambiguous", why="收盘在破位门槛的代理误差内", **extra)
        return res("held" if len(fol) > FOLLOW_DAYS else "pending", **extra)
    if c == "no_support":
        L = p["level"]
        b, mb = _first(Dw, lambda d: cross(d[4], L * 0.995, True, tol))
        if b is not None:
            return res("hit", day=str(b[0]))
        if mb:
            return res("ambiguous", why="收盘在门槛的代理误差内")
        return res("miss" if full else "pending", min_close=round(min(d[4] for d in Dw), 2) if Dw else None)
    if c == "break_below":
        L, dl = p["level"], datetime.fromisoformat(p["deadline"]).astimezone(timezone.utc)
        seg = [h for h in H if h[0] + H1 <= dl]
        straddle = [h for h in hourly if h[0] < dl < h[0] + H1 and h[0] >= posted and h[0] + H1 <= asof]
        b, mb = _first(seg, lambda h: cross(h[3], L, True, tol))
        if b is not None:
            return res("hit", at=_ts(b))
        if any(cross(h[3], L, True, tol) != "no" for h in straddle):
            mb = True
        if asof < dl:
            return res("pending", why="未到截止时刻", min_low=round(min(h[3] for h in seg), 2) if seg else None)
        if mb:
            return res("ambiguous", why="只在代理误差内接近，或跨截止那根穿越（先后不明）",
                       min_low=round(min(h[3] for h in seg), 2) if seg else None)
        if not seg:
            return res("no_data_before_deadline")
        return res("miss", min_low=round(min(h[3] for h in seg), 2))
    if c == "range":
        n = p["days"]
        seg = D[:n]
        def outside(d):
            k = (cross(d[4], p["lo"] * 0.995, True, tol), cross(d[4], p["hi"] * 1.005, False, tol))
            return "yes" if "yes" in k else ("maybe" if "maybe" in k else "no")
        out, mb = _first(seg, outside)
        if out is not None:
            return res("broken", day=str(out[0]), side="down" if out[4] < p["lo"] else "up", close=round(out[4], 2))
        if len(seg) < n:
            return res("pending", n=len(seg))
        return res("ambiguous" if mb else "held")
    if c == "trade_long":
        n = p.get("days", 10)
        full = len(D) >= n
        end = _close_at(D[n - 1][0]) if full else asof
        seg = [h for h in H if h[0] + H1 <= end]
        if not seg:
            return res("pending", why="发布后尚无完整小时线")
        e, st, tg = p["entry"], p["stop"], p["target"]
        status = p.get("entry_status") or r.get("entry_status") or "pending_trigger"
        extra = {"entry_status": status, "entry_status_assumed": not (p.get("entry_status") or r.get("entry_status"))}
        if status == "declared_filled":
            trig, i0 = seg[0], 0
        else:
            above = seg[0][1] >= e
            i0 = next((i for i, h in enumerate(seg) if cross(h[3] if above else h[2], e, above, 0) == "yes"), None)
            if i0 is None:
                near = any(cross(h[3] if above else h[2], e, above, tol) != "no" for h in seg)
                if near:
                    return res("ambiguous", why="入场价在代理误差内，未确证触发", **extra)
                return res("not_triggered" if full else "pending", **extra)
            trig = seg[i0]
            extra["triggered_at"] = _ts(trig)
        for i, h in enumerate(seg[i0:]):
            s, t = cross(h[3], st, True, tol), cross(h[2], tg, False, tol)
            if s == "no" and t == "no":
                continue
            if (s != "no" and t != "no") or (i == 0 and status != "declared_filled"):
                return res("ambiguous", why="同一根内先后不明（含触发那根）", at=_ts(h), **extra)
            if "maybe" in (s, t):
                return res("ambiguous", why="止损/目标在代理误差内", at=_ts(h), **extra)
            return res("stop_first" if s == "yes" else "target_first", at=_ts(h), **extra)
        return res("neither" if full else "pending", low=round(min(h[3] for h in seg[i0:]), 2),
                   high=round(max(h[2] for h in seg[i0:]), 2), **extra)
    return res("unsupported_claim")


# ═══ v3（Codex 026，2026-09-29；看过 v2 结果后的口径修正，不是新证据）═══════════════════════════════════
# v2 的问题：①「未发生」类结论（miss/held/untouched/not_triggered/neither）只看已有 K 线，窗口里缺数据也会终判；
# ②「20 日」按抓到的行数计，缺一天窗口就被拉长；③ 日线「晚于发布」按 17:00 ET 判，而 Yahoo 期货日线收盘实测是
# 结算价（GC 13:30、布伦特约 14:30 ET 定价；落在 13–15 点小时线区间、≠ 17:00 前最后成交）→ 15:00 发帖会把当日
# 早已定价的结算当成发布后的价格；④ 触及 ≤ 与破位 < 共用一个含等号的判定；⑤ 直接合约也套了 0.1% 代理容差。
VERSION_V3 = "author-levels-v3-20260929"
HOURLY_COVERAGE_MIN = 0.9          # 会话内应有小时线的最低覆盖（设计值，未校准）
SESSION_OPEN_H = 18                # CME/NYMEX Globex：前一日 18:00 ET 开 → 当日 17:00 ET 收（1 小时休市）
SESSION_END_H = 17
SPECS = {                          # 结算时刻来自交易所公开惯例（未逐日核实）；tick = 最小报价单位
    "gold_proxy": {"settle": (13, 30), "tick": None, "tol": 0.005, "basis": "proxy_unverified（GC=F 持有成本折算）"},
    "BZZ26": {"settle": (14, 30), "tick": "0.01", "tol": 0.0, "basis": "contract_direct（BZZ26）"},
}


def _dec(x, tick):
    from decimal import Decimal
    return Decimal(str(x)).quantize(Decimal(tick)) if tick else x


def _thr(level, factor, tick):
    from decimal import Decimal
    return Decimal(str(level)) * Decimal(str(factor)) if tick else level * factor


def cross3(x, thr, below: bool, tol: float, *, strict: bool, tick=None) -> str:
    """三态穿越。strict=True 为破位（< / >），False 为触及（≤ / ≥）。直接合约（tick）先把价格规范到最小报价单位再比较；
    tol 只表示代理换算误差，直接合约为 0。"""
    x = _dec(x, tick)
    if tick:
        from decimal import Decimal
        lo, hi = thr * (1 - Decimal(str(tol))), thr * (1 + Decimal(str(tol)))
    else:
        lo, hi = thr * (1 - tol), thr * (1 + tol)
    if below:
        yes = x < lo if strict else x <= lo
        maybe = x < hi if strict else x <= hi
    else:
        yes = x > hi if strict else x >= hi
        maybe = x > lo if strict else x >= lo
    return "yes" if yes else ("maybe" if maybe else "no")


def _settle_at(d: date, spec) -> datetime:
    return datetime(d.year, d.month, d.day, *spec["settle"], tzinfo=ET)


def _session_end(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, SESSION_END_H, tzinfo=ET)


def _raw_session(t: datetime) -> date:
    e = t.astimezone(ET)
    return e.date() + timedelta(days=1) if e.hour >= SESSION_OPEN_H else e.date()


def session_of(t: datetime) -> date:
    """小时线起点 → 所属交易会话日（18:00 ET 起算入下一日）。落在非交易日（不应出现）→ 顺延到下一交易日。"""
    from undertow.core import market_calendar as mc
    d = _raw_session(t)
    return d if mc.is_trading_day(d) else (mc.next_trading_day(d) or d)


def sessions_after(posted: datetime, n: int, spec) -> list:
    """结算时刻晚于发布的前 n 个交易会话（按交易所日历计，不按抓到的行数；NYSE 日历近似 CME 会话，未核实）。"""
    from undertow.core import market_calendar as mc
    out, d = [], posted.astimezone(ET).date()
    while len(out) < n:
        if mc.is_trading_day(d) is None:
            raise ValueError(f"{d} 超出交易日历覆盖期")
        if mc.is_trading_day(d) and _settle_at(d, spec) > posted:
            out.append(d)
        d += timedelta(days=1)
    return out


def expected_slots(start: datetime, end: datetime) -> list:
    """[start, end) 内应有的整点小时线起点：所属会话（18:00 ET 起算入下一日）是交易日、且不在 17:00 休市那一小时。
    周五 18:00 → 周六会话（非交易日）→ 不计；周日 18:00 → 周一会话。NYSE 日历近似 CME 会话（CME 节假日半日盘未建模）。"""
    from undertow.core import market_calendar as mc
    out = []
    t = start.astimezone(ET).replace(minute=0, second=0, microsecond=0)
    if t < start:
        t += H1
    while t + H1 <= end:
        if t.astimezone(ET).hour != SESSION_END_H and mc.is_trading_day(_raw_session(t)):
            out.append(t.astimezone(timezone.utc))
        t = (t.astimezone(timezone.utc) + H1).astimezone(ET)
    return out


def coverage(hourly, start: datetime, end: datetime) -> dict:
    exp = expected_slots(start, end)
    have = {h[0] for h in hourly}
    got = sum(t in have for t in exp)
    return {"expected": len(exp), "observed": got, "frac": round(got / len(exp), 4) if exp else None}


def score_v3(r: dict, daily, hourly, spec: dict, asof: datetime) -> dict:
    posted = datetime.fromisoformat(r["posted_at"]).astimezone(timezone.utc)
    asof = asof.astimezone(timezone.utc)
    tol, tick = spec["tol"], spec["tick"]
    p, c = r["params"], r["claim"]
    Dmap = {d[0]: d for d in daily}
    H = [h for h in hourly if h[0] >= posted and h[0] + H1 <= asof]
    before = [h for h in hourly if h[0] + H1 <= posted]
    base = {"asof": asof.isoformat(), "version": VERSION_V3}

    def res(result, **kw):
        return {**base, "result": result, **kw}

    def closes(sessions):
        """会话 → 日线（结算已过 asof 的才算有；缺 → None）。"""
        return [(d, Dmap.get(d) if _settle_at(d, spec) <= asof else "future") for d in sessions]

    def not_happened(end: datetime, extra_cov=None, **kw):
        """「未发生」终判前提：窗口已结束且会话内小时线覆盖足够；否则 pending / incomplete_window。"""
        if asof < end:
            return res("pending", why="窗口未结束", **kw)
        cov = extra_cov or coverage(hourly, posted, end)
        if cov["frac"] is None or cov["frac"] < HOURLY_COVERAGE_MIN:
            return res("incomplete_window", coverage=cov, **kw)
        return None

    if c in ("support", "resistance"):
        L, sup = p["level"], c == "support"
        S = sessions_after(posted, TOUCH_DAYS, spec)
        end = _session_end(S[-1])
        Hw = [h for h in H if h[0] + H1 <= end]
        at_post = None
        if before:
            at_post = "already_through" if cross3(before[-1][4], _thr(L, 1, tick), sup, 0, strict=True, tick=tick) == "yes" else "approach"
        zone = _thr(L, 1.001 if sup else 0.999, tick)
        t, touch_maybe = _first(Hw, lambda h: cross3(h[3] if sup else h[2], zone, sup, tol, strict=False, tick=tick))
        if t is None:
            ext = (min(h[3] for h in Hw) if sup else max(h[2] for h in Hw)) if Hw else None
            kw = {"extreme": round(ext, 2) if ext is not None else None, "status_at_post": at_post}
            if touch_maybe:
                return res("ambiguous", why="只在代理误差内接近", **kw)
            return not_happened(end, **kw) or res("untouched", coverage=coverage(hourly, posted, end), **kw)
        ts = session_of(t[0])
        from undertow.core import market_calendar as mc
        fol = [ts]
        while len(fol) < FOLLOW_DAYS + 1:
            fol.append(mc.next_trading_day(fol[-1]))
        cl = closes(fol)
        thr_b, thr_h = (_thr(L, 0.995, tick), _thr(L, 1.01, tick)) if sup else (_thr(L, 1.005, tick), _thr(L, 0.99, tick))
        have = [x for _, x in cl if x not in (None, "future")]
        b, b_maybe = _first(have, lambda d: cross3(d[4], thr_b, sup, tol, strict=True, tick=tick))
        reb, _ = _first(have, lambda d: cross3(d[4], thr_h, not sup, 0, strict=False, tick=tick))
        extra = {"touch_at": _ts(t), "touch_session": ts.isoformat(), "touch_time_ambiguous": touch_maybe,
                 "status_at_post": at_post, "rebound_first": bool(reb and (b is None or reb[0] < b[0])),
                 "later_broken": bool(b and reb and reb[0] < b[0]),
                 "follow_missing": [d.isoformat() for d, x in cl if x is None]}
        if b is not None:
            return res("broken", decisive_session=str(b[0]), **extra)
        if b_maybe:
            return res("ambiguous", why="收盘在破位门槛的代理误差内", **extra)
        if any(x == "future" for _, x in cl):
            return res("pending", why="跟踪期未结束", **extra)
        if any(x is None for _, x in cl):
            return res("incomplete_window", why="跟踪期有会话缺日线", **extra)
        return res("held", **extra)
    if c == "no_support":
        L = p["level"]
        S = sessions_after(posted, TOUCH_DAYS, spec)
        cl = closes(S)
        have = [x for _, x in cl if x not in (None, "future")]
        b, mb = _first(have, lambda d: cross3(d[4], _thr(L, 0.995, tick), True, tol, strict=True, tick=tick))
        if b is not None:
            return res("hit", session=str(b[0]))
        if mb:
            return res("ambiguous", why="收盘在门槛的代理误差内")
        if any(x == "future" for _, x in cl):
            return res("pending", n_sessions_settled=len(have))
        if any(x is None for _, x in cl):
            return res("incomplete_window", missing=[d.isoformat() for d, x in cl if x is None])
        return res("miss", min_close=round(min(d[4] for d in have), 2))
    if c == "break_below":
        L, dl = p["level"], datetime.fromisoformat(p["deadline"]).astimezone(timezone.utc)
        seg = [h for h in H if h[0] + H1 <= dl]
        straddle = [h for h in hourly if h[0] < dl < h[0] + H1 and h[0] >= posted and h[0] + H1 <= asof]
        b, mb = _first(seg, lambda h: cross3(h[3], _thr(L, 1, tick), True, tol, strict=True, tick=tick))
        if b is not None:
            return res("hit", at=_ts(b))
        if any(cross3(h[3], _thr(L, 1, tick), True, tol, strict=True, tick=tick) != "no" for h in straddle):
            mb = True
        if mb and asof >= dl:
            return res("ambiguous", why="只在代理误差内接近，或跨截止那根穿越（先后不明）")
        kw = {"min_low": round(min(h[3] for h in seg), 2) if seg else None}
        return not_happened(dl, **kw) or res("miss", coverage=coverage(hourly, posted, dl), **kw)
    if c == "range":
        n = p["days"]
        S = sessions_after(posted, n, spec)
        cl = closes(S)
        have = [x for _, x in cl if x not in (None, "future")]

        def outside(d):
            k = (cross3(d[4], _thr(p["lo"], 0.995, tick), True, tol, strict=True, tick=tick),
                 cross3(d[4], _thr(p["hi"], 1.005, tick), False, tol, strict=True, tick=tick))
            return "yes" if "yes" in k else ("maybe" if "maybe" in k else "no")
        out, mb = _first(have, outside)
        if out is not None:
            return res("broken", session=str(out[0]), side="down" if out[4] < p["lo"] else "up", close=round(out[4], 2))
        if mb:
            return res("ambiguous", why="收盘在区间边界的代理误差内")
        if any(x == "future" for _, x in cl):
            return res("pending", n_sessions_settled=len(have))
        if any(x is None for _, x in cl):
            return res("incomplete_window", missing=[d.isoformat() for d, x in cl if x is None])
        return res("held")
    if c == "trade_long":
        n = p.get("days", 10)
        S = sessions_after(posted, n, spec)
        end = _session_end(S[-1])
        seg = [h for h in H if h[0] + H1 <= end]
        e, st, tg = _thr(p["entry"], 1, tick), _thr(p["stop"], 1, tick), _thr(p["target"], 1, tick)
        status = p.get("entry_status") or r.get("entry_status") or "pending_trigger"
        extra = {"entry_status": status, "entry_status_assumed": not (p.get("entry_status") or r.get("entry_status"))}
        if not seg:
            return not_happened(end, **extra) or res("incomplete_window", why="窗口内无小时线", **extra)
        if status == "declared_filled":
            i0 = 0
        else:
            above = seg[0][1] >= float(e)
            i0 = next((i for i, h in enumerate(seg)
                       if cross3(h[3] if above else h[2], e, above, 0, strict=False, tick=tick) == "yes"), None)
            if i0 is None:
                if any(cross3(h[3] if above else h[2], e, above, tol, strict=False, tick=tick) != "no" for h in seg):
                    return res("ambiguous", why="入场价在代理误差内，未确证触发", **extra)
                return not_happened(end, **extra) or res("not_triggered", coverage=coverage(hourly, posted, end), **extra)
            extra["triggered_at"] = _ts(seg[i0])
        for i, h in enumerate(seg[i0:]):
            s_ = cross3(h[3], st, True, tol, strict=False, tick=tick)
            t_ = cross3(h[2], tg, False, tol, strict=False, tick=tick)
            if s_ == "no" and t_ == "no":
                continue
            if (s_ != "no" and t_ != "no") or (i == 0 and status != "declared_filled"):
                return res("ambiguous", why="同一根内先后不明（含触发那根）", at=_ts(h), **extra)
            if "maybe" in (s_, t_):
                return res("ambiguous", why="止损/目标在代理误差内", at=_ts(h), **extra)
            return res("stop_first" if s_ == "yes" else "target_first", at=_ts(h), **extra)
        start = datetime.fromisoformat(extra["triggered_at"].replace("Z", "+00:00").replace(" ", "T")) if "triggered_at" in extra else posted
        return not_happened(end, extra_cov=coverage(hourly, start, end), **extra) or \
            res("neither", low=round(min(h[3] for h in seg[i0:]), 2), high=round(max(h[2] for h in seg[i0:]), 2), **extra)
    return res("unsupported_claim")


def _preserve_v1() -> None:
    """v1 结果只保存一次、标需复核，不静默覆盖历史（Codex 025-3）。"""
    if OUT.exists() and not OUT_V1.exists():
        old = json.loads(OUT.read_text("utf-8"))
        if isinstance(old, list):
            OUT_V1.write_text(json.dumps({"version": "author-levels-v1-20260929", "needs_review": True,
                                          "why": "Codex 025-3：未来截止提前 miss、trade_long 无视入场、未完成 K 线、支撑语义与 tol 不一致",
                                          "rows": old}, ensure_ascii=False, indent=1), "utf-8")


OUT_CHANGES = ROOT / "data/soul/author_levels_v2_v3_changes.json"


def main():
    asof = datetime.now(timezone.utc)
    if "--asof" in sys.argv:
        asof = datetime.fromisoformat(sys.argv[sys.argv.index("--asof") + 1])
    rows = [json.loads(l) for l in SRC.read_text("utf-8").splitlines() if l.strip()]
    gold = None
    cache = {}
    out = []
    for r in rows:
        inst = r["instrument"]
        if inst.startswith("XAU") or inst.startswith("黄金"):
            if gold is None:
                gold = (to_spot(yahoo_daily("GC=F", "2y")), to_spot_h(yahoo_hourly("GC=F")))
            (d, h), spec = gold, SPECS["gold_proxy"]
            tol_v2 = 0.005
        elif "BZZ26" in inst:
            if "BZZ26" not in cache:
                cache["BZZ26"] = (yahoo_daily("BZZ26.NYM", "6mo"), yahoo_hourly("BZZ26.NYM"))
            (d, h), spec = cache["BZZ26"], SPECS["BZZ26"]
            tol_v2 = 0.001
        else:
            out.append({**r, "score": {"result": "no_price_source"}}); continue
        v3 = {**score_v3(r, d, h, spec, asof), "price_basis": spec["basis"], "tol": spec["tol"]}
        v2 = {**score(r, d, h, tol_v2, asof), "tol": tol_v2}
        out.append({**r, "score": v3, "score_v2": v2})
    _preserve_v1()
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": VERSION_V3, "asof": asof.isoformat(),
                               "note": "v3 为看过 v2 结果后的口径修正（Codex 026），不是新证据；score_v2 并存供对照",
                               "rows": out}, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(OUT)
    changes = [{"author": x["author"], "posted_at": x["posted_at"], "claim": x["claim"], "params": x.get("params"),
                "retrospective": bool(x.get("retrospective")), "v2": x["score_v2"]["result"], "v3": x["score"]["result"]}
               for x in out if "score_v2" in x]
    OUT_CHANGES.write_text(json.dumps({"asof": asof.isoformat(), "rows": changes}, ensure_ascii=False, indent=1), "utf-8")
    for x in out:
        s = x["score"]
        v2r = (x.get("score_v2") or {}).get("result")
        print(f"{x['author']} {x['posted_at'][:16]} {'[事后]' if x.get('retrospective') else '[事前]'} "
              f"{x['claim']} {x.get('params')} → {s['result']}{'' if v2r == s['result'] else f'（v2: {v2r}）'}  "
              f"{({k: v for k, v in s.items() if k not in ('result', 'asof', 'price_basis', 'tol', 'version')})}")
    # 分母要全：未触及 / 模糊 / 未决 / 窗口不全与终判一起报告，事前与事后分开（Codex 025/026）
    from collections import Counter
    for (a, retro), cnt in sorted(Counter((x["author"], bool(x.get("retrospective"))) for x in out).items()):
        res = Counter(x["score"]["result"] for x in out if x["author"] == a and bool(x.get("retrospective")) == retro)
        print(f"  {a} {'事后' if retro else '事前'} n={cnt}：" + "、".join(f"{k} {v}" for k, v in sorted(res.items())))
    print(f"  v2→v3 变化 {sum(c['v2'] != c['v3'] for c in changes)}/{len(changes)} 条（明细：{OUT_CHANGES.name}，私有）")


if __name__ == "__main__":
    main()
