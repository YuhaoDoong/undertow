"""外部作者价位类判断的统一计分（用户 2026-09-29：「多记录多测试……这样才能拿到信服结果」）。

读私有 data/soul/author_levels.jsonl，写私有 data/soul/author_levels_scored.json；本脚本只含规则，不含任何作者内容。
  python3 scripts/author_levels_score.py

规则（事前写死，对所有作者一视同仁；起算 = 发布时刻之后第一个开盘的交易日，含当日 K 线）：
- support L：20 个交易日内首次触及（最低价 ≤ L×1.001）→ 之后 5 日内有收盘 < L×0.995 记 broken；否则若有收盘 ≥ L×1.01
  记 held；都没有记 touched_undecided。20 日内未触及记 untouched（不计分）。
- resistance L：镜像（最高价 ≥ L×0.999；收盘 > L×1.005 为 broken；收盘 ≤ L×0.99 为 held）。
- no_support L（「构不成支撑/底部」）：20 日内有收盘 < L×0.995 → hit，否则 miss。
- break_below L by deadline：截止前最低价 < L → hit，否则 miss。
- range lo–hi 共 N 日：N 日收盘全在 [lo×0.995, hi×1.005] → held，否则 broken（记方向与日期）。
- trade_long entry/stop/target 共 N 日：逐日看，最低 ≤ stop 与最高 ≥ target 谁先；同一天都触及 → ambiguous；都没 → neither。
价格：黄金用 COMEX 期货 GC=F 日线，以持有成本折算现货口径（S = F·e^(−rT)，r=4.5% 固定假设，T 到主力合约交割月中；
误差约 ±0.5%）—— 决定结果的价格离门槛在 0.5% 以内 → ambiguous（不硬判）。原油用作者指定的合约。
数据来自 Yahoo 公开 chart 接口（只读）。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "data/soul/author_levels.jsonl"
OUT = ROOT / "data/soul/author_levels_scored.json"
R = 0.045
GOLD_MONTHS = (2, 4, 6, 8, 12)
TOUCH_DAYS, FOLLOW_DAYS = 20, 5


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
    """Yahoo GC=F 跟踪的主力合约（实测 9/29 与 GCZ26 同价，跳过交投清淡的 10 月）到交割月中的年数。
    主力 = 交割月前一个月月末（约第一通知日）仍在 d 之后的最近一个活跃月。"""
    from datetime import timedelta
    for y in (d.year, d.year + 1):
        for m in GOLD_MONTHS:
            first_of = date(y, m, 1)
            if first_of - timedelta(days=1) > d:
                return (date(y, m, 15) - d).days / 365
    return 0.1


def to_spot(bars):
    out = []
    for d, o, h, lo, c in bars:
        k = math.exp(-R * gold_T(d))
        out.append((d, o * k, h * k, lo * k, c * k))
    return out


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


def near(x: float, lvl: float, tol: float) -> bool:
    return abs(x / lvl - 1) < tol


def score(r: dict, daily, hourly, tol: float) -> dict:
    """触及类一律用【发布之后】的 1 小时线（期货日线从前一日 18:00 ET 起算，会含发布前价格 —— 2026-09-29
    首版就因此把一笔仍在进行的交易误判为先止损）；收盘类用收盘时刻（17:00 ET）晚于发布的日线。"""
    from zoneinfo import ZoneInfo
    posted = datetime.fromisoformat(r["posted_at"]).astimezone(timezone.utc)
    et = ZoneInfo("America/New_York")
    H = [h for h in hourly if h[0] >= posted]
    D = [d for d in daily if datetime(d[0].year, d[0].month, d[0].day, 17, tzinfo=et) > posted]
    if not H or not D:
        return {"result": "pending", "why": "发布后尚无数据"}
    p, c = r["params"], r["claim"]
    Dw = D[:TOUCH_DAYS]
    horizon_end = datetime(Dw[-1][0].year, Dw[-1][0].month, Dw[-1][0].day, 17, tzinfo=et)
    Hw = [h for h in H if h[0] < horizon_end]
    full = len(Dw) >= TOUCH_DAYS
    if c in ("support", "resistance"):
        L, sup = p["level"], c == "support"
        t = next((h for h in Hw if (h[3] <= L * 1.001 if sup else h[2] >= L * 0.999)), None)
        if t is None:
            ext = min(h[3] for h in Hw) if sup else max(h[2] for h in Hw)
            res = "ambiguous" if near(ext, L, tol) else ("untouched" if full else "pending")
            return {"result": res, "extreme": round(ext, 2)}
        tday = t[0].astimezone(et).date()
        fol = [d for d in D if d[0] >= tday][:FOLLOW_DAYS + 1]
        brk = next((d for d in fol if (d[4] < L * 0.995 if sup else d[4] > L * 1.005)), None)
        held = next((d for d in fol if (d[4] >= L * 1.01 if sup else d[4] <= L * 0.99)), None)
        first = min((x for x in (brk, held) if x), key=lambda x: x[0], default=None)
        if first is None:
            res = "touched_undecided" if len(fol) > FOLLOW_DAYS else "pending"
        else:
            res = "broken" if first is brk else "held"
            thr = (L * 0.995 if sup else L * 1.005) if first is brk else (L * 1.01 if sup else L * 0.99)
            if near(first[4], thr, tol):
                res += "(ambiguous)"
        return {"result": res, "touch_at": t[0].strftime("%Y-%m-%d %H:%MZ"),
                "touch_extreme": round(t[3] if sup else t[2], 2), "decisive_day": str(first[0]) if first else None}
    if c == "no_support":
        L = p["level"]
        b = next((d for d in Dw if d[4] < L * 0.995), None)
        return {"result": "hit" if b else ("miss" if full else "pending"), "day": str(b[0]) if b else None,
                "min_close": round(min(d[4] for d in Dw), 2)}
    if c == "break_below":
        L, dl = p["level"], datetime.fromisoformat(p["deadline"]).astimezone(timezone.utc)
        seg = [h for h in H if h[0] < dl]
        if not seg:
            return {"result": "no_data_before_deadline"}
        b = next((h for h in seg if h[3] < L), None)
        mn = min(h[3] for h in seg)
        return {"result": "hit" if b else ("ambiguous" if near(mn, L, tol) else "miss"),
                "at": b[0].strftime("%Y-%m-%d %H:%MZ") if b else None, "min_low": round(mn, 2)}
    if c == "range":
        seg = D[:p["days"]]
        out = next((d for d in seg if not (p["lo"] * 0.995 <= d[4] <= p["hi"] * 1.005)), None)
        if out is None and len(seg) < p["days"]:
            return {"result": "pending", "n": len(seg)}
        return {"result": "broken" if out else "held", "day": str(out[0]) if out else None,
                "side": ("down" if out[4] < p["lo"] else "up") if out else None, "close": round(out[4], 2) if out else None}
    if c == "trade_long":
        nd = p.get("days", 10)
        end = D[min(nd, len(D)) - 1][0]
        seg = [h for h in H if h[0].astimezone(et).date() <= end]
        for h in seg:
            s_hit, t_hit = h[3] <= p["stop"], h[2] >= p["target"]
            if s_hit and t_hit:
                return {"result": "ambiguous", "at": h[0].strftime("%Y-%m-%d %H:%MZ")}
            if s_hit or t_hit:
                return {"result": "stop_first" if s_hit else "target_first", "at": h[0].strftime("%Y-%m-%d %H:%MZ")}
        return {"result": "neither" if len(D) >= nd else "pending", "low": round(min(h[3] for h in seg), 2),
                "high": round(max(h[2] for h in seg), 2)}
    return {"result": "unsupported_claim"}


def main():
    rows = [json.loads(l) for l in SRC.read_text("utf-8").splitlines() if l.strip()]
    gold_d, gold_h = to_spot(yahoo_daily("GC=F", "2y")), to_spot_h(yahoo_hourly("GC=F"))
    cache = {}
    out = []
    for r in rows:
        inst = r["instrument"]
        if inst.startswith("XAU") or inst.startswith("黄金"):
            d, h, tol = gold_d, gold_h, 0.005
        elif "BZZ26" in inst:
            if "BZZ26" not in cache:
                cache["BZZ26"] = (yahoo_daily("BZZ26.NYM", "6mo"), yahoo_hourly("BZZ26.NYM"))
            (d, h), tol = cache["BZZ26"], 0.001
        else:
            out.append({**r, "score": {"result": "no_price_source"}}); continue
        out.append({**r, "score": score(r, d, h, tol)})
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    for x in out:
        s = x["score"]
        print(f"{x['author']} {x['posted_at'][:16]} {'[事后]' if x.get('retrospective') else '[事前]'} "
              f"{x['claim']} {x.get('params')} → {s['result']}  {({k: v for k, v in s.items() if k != 'result'})}")


if __name__ == "__main__":
    main()
