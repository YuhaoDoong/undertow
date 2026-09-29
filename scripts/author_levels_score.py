"""外部作者价位类判断的统一计分（用户 2026-09-29：「多记录多测试……这样才能拿到信服结果」）。

读私有 data/soul/author_levels.jsonl，写私有 data/soul/author_levels_scored.json；本脚本只含规则，不含任何作者内容。
  python3 scripts/author_levels_score.py [--asof ISO时刻]

版本 VERSION（v2，Codex 025-3 修订；这是**未校准的探索评分协议**，阈值不按已看到的结果调）：
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


def _preserve_v1() -> None:
    """v1 结果只保存一次、标需复核，不静默覆盖历史（Codex 025-3）。"""
    if OUT.exists() and not OUT_V1.exists():
        old = json.loads(OUT.read_text("utf-8"))
        if isinstance(old, list):
            OUT_V1.write_text(json.dumps({"version": "author-levels-v1-20260929", "needs_review": True,
                                          "why": "Codex 025-3：未来截止提前 miss、trade_long 无视入场、未完成 K 线、支撑语义与 tol 不一致",
                                          "rows": old}, ensure_ascii=False, indent=1), "utf-8")


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
            (d, h), tol, basis = gold, 0.005, "proxy_unverified（GC=F 持有成本折算）"
        elif "BZZ26" in inst:
            if "BZZ26" not in cache:
                cache["BZZ26"] = (yahoo_daily("BZZ26.NYM", "6mo"), yahoo_hourly("BZZ26.NYM"))
            (d, h), tol, basis = cache["BZZ26"], 0.001, "contract_direct（BZZ26）"
        else:
            out.append({**r, "score": {"result": "no_price_source"}}); continue
        out.append({**r, "score": {**score(r, d, h, tol, asof), "price_basis": basis, "tol": tol}})
    _preserve_v1()
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "asof": asof.isoformat(), "rows": out}, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(OUT)
    for x in out:
        s = x["score"]
        print(f"{x['author']} {x['posted_at'][:16]} {'[事后]' if x.get('retrospective') else '[事前]'} "
              f"{x['claim']} {x.get('params')} → {s['result']}  "
              f"{({k: v for k, v in s.items() if k not in ('result', 'asof', 'price_basis', 'tol')})}")
    # 分母要全：未触及 / 模糊 / 未决与终判一起报告，事前与事后分开（Codex 025：不能只呈现触及后的成功率）
    from collections import Counter
    for (a, retro), cnt in sorted(Counter((x["author"], bool(x.get("retrospective"))) for x in out).items()):
        res = Counter(x["score"]["result"] for x in out if x["author"] == a and bool(x.get("retrospective")) == retro)
        print(f"  {a} {'事后' if retro else '事前'} n={cnt}：" + "、".join(f"{k} {v}" for k, v in sorted(res.items())))


if __name__ == "__main__":
    main()
