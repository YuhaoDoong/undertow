"""模拟仓已了结仓位的「方向还是墙」归因（用户 2026-10-14：「你需要统计一下，模拟仓已经完成的，究竟是方向对了，还是墙撑住了。
统计从开仓到结束的正股价格变化」；同意把这一栏常设进每日报告）。

口径（描述性，不是检验；检验见 docs/prereg/2026-10-14_expiry_pin_v4_DRAFT.md）：
- 押的方向：卖 put 价差 / 买 call 价差 = 看涨（+1）；卖 call 价差 / 买 put 价差 = 看跌（−1）。宽跨等无方向结构不归因。
- 正股价：入场时刻之前最后一个分钟价（data/history/option_intraday/<根>/<ET日>.json.gz，收盘后存档）；
  了结价：到期结算用结算收盘，提前了结用了结时刻之前最后一个分钟价。旧仓（9/29 前）没有分钟存档，用回填在
  legacy_positions.jsonl 的 underlying_* 字段（出处写在 underlying_px_source）。缺任一 → None，不估、不折零。
- 卖腿距离（贷记）：入场时正股离卖腿多远（正 = 价外）；最不利 = 持有期内朝不利方向的最大偏移；
  盘中越过卖腿 = 持有期内价格路径是否到过卖腿另一侧（分钟收盘价，不含分钟内高低）。
- 分组：方向对（正股朝有利方向走）/ 方向错·了结时仍在卖腿有利一侧 / 方向错·越过卖腿。借记价差只分方向对错。
- 零模型基线（「没有墙时」）：入场前 250 个交易日、同持有交易日数的收盘涨跌分布下，了结时仍在卖腿有利一侧的概率 p；
  报告 Σp 与实际笔数。不处理漂移与窗口重叠，只作量级参考；同一判断日的仓位不独立。
"""
from __future__ import annotations

import gzip
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
MINUTE_DIR = ROOT / "data/history/option_intraday"
BASELINE_WINDOW = 250


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _ts(x) -> datetime | None:
    if not x:
        return None
    t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=ET)


def direction(side: str | None, structure: str | None) -> int:
    """+1 看涨 / −1 看跌 / 0 无方向。"""
    if side not in ("P", "C"):
        return 0
    bull = (side == "P") if structure != "debit" else (side == "C")
    return 1 if bull else -1


def load_minutes(root: str, day: str, base: Path = MINUTE_DIR) -> list[tuple[datetime, float]] | None:
    """某 ET 日的正股分钟价（升序）；存档不存在或损坏 → None（不是空列表，空列表表示存档里确实没有行）。"""
    p = base / root / f"{day}.json.gz"
    if not p.exists():
        return None
    try:
        d = json.loads(gzip.decompress(p.read_bytes()))
        c = (d.get("contracts") or {}).get(f"{root}.US") or {}
        if c.get("status") != "ok":
            return None
        return sorted((datetime.fromisoformat(r[0].replace("Z", "+00:00")), float(r[1])) for r in c.get("rows") or [])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def price_path(root: str, t0: datetime, t1: datetime, *, loader=load_minutes, trading_day=None) -> dict:
    """t0..t1 之间的分钟价路径。任一交易日存档缺失 → complete=False（不拿残缺路径算最不利偏移）。"""
    if trading_day is None:
        from undertow.core import market_calendar as mc
        trading_day = mc.is_trading_day
    rows, missing = [], []
    d = t0.astimezone(ET).date()
    while d <= t1.astimezone(ET).date():
        if trading_day(d) is not False:
            m = loader(root, d.isoformat())
            if m is None:
                missing.append(d.isoformat())
            else:
                rows += [(t, p) for t, p in m if t0 <= t <= t1]
        d += timedelta(days=1)
    return {"rows": rows, "missing": missing, "complete": not missing and bool(rows)}


def price_at(root: str, t: datetime, *, loader=load_minutes) -> float | None:
    """t 之前（含）最后一个分钟价；当天存档缺失或 t 早于当天第一根 → None。"""
    m = loader(root, t.astimezone(ET).date().isoformat())
    prev = [p for tt, p in (m or []) if tt <= t]
    return prev[-1] if prev else None


def attribute(t: dict) -> dict:
    """纯函数。t：{dirn, structure, k_sell, S0, S1, path_prices(list|None)}。返回归因字段；缺价 → category=None。"""
    dirn, S0, S1, ks = t["dirn"], t.get("S0"), t.get("S1"), t.get("k_sell")
    out = {"chg": None, "fav": None, "cushion": None, "mae": None, "crossed_intraday": None, "exit_ok": None, "category": None}
    if dirn == 0 or not (_num(S0) and _num(S1)) or S0 <= 0:
        return out
    chg = S1 / S0 - 1
    out.update(chg=round(chg, 6), fav=round(chg * dirn, 6))
    credit = t.get("structure") != "debit"
    path = t.get("path_prices")
    if path:
        worst = min(path) if dirn > 0 else max(path)
        out["mae"] = round(min((worst / S0 - 1) * dirn, 0.0), 6)
    if credit and _num(ks):
        out["cushion"] = round(((S0 - ks) if dirn > 0 else (ks - S0)) / S0, 6)
        out["exit_ok"] = (S1 > ks) if dirn > 0 else (S1 < ks)
        if path:
            out["crossed_intraday"] = any((p < ks) if dirn > 0 else (p > ks) for p in path)
    if out["fav"] > 0:
        out["category"] = "方向对"
    elif not credit:
        out["category"] = "方向错"
    elif out["exit_ok"] is None:
        out["category"] = None
    else:
        out["category"] = "方向错·仍在卖腿有利一侧" if out["exit_ok"] else "方向错·越过卖腿"
    return out


def baseline_p(closes: list[tuple[date, float]], entry_day: date, exit_day: date, dirn: int, cushion: float,
               window: int = BASELINE_WINDOW) -> float | None:
    """零模型：入场日之前 window 个收盘里，h 日（入场日→了结日的交易日数）涨跌不越过卖腿距离的比例。
    只用入场日之前的收盘（不含入场日），避免前视。h = 0 或样本不足 → None。"""
    ds = [d for d, _ in closes]
    h = sum(1 for d in ds if entry_day < d <= exit_day)
    if h == 0 or not _num(cushion):
        return None
    prior = [c for d, c in closes if d < entry_day][-window:]
    rets = [prior[i + h] / prior[i] - 1 for i in range(len(prior) - h)]
    if len(rets) < 50:
        return None
    return sum(1 for r in rets if r * dirn > -cushion) / len(rets)


def build(closed_rows: list, legacy: list, *, loader=load_minutes, trading_day=None) -> list[dict]:
    """把 paper_book.classify 的已了结行与已结算旧仓，整理成归因输入并算出归因字段。"""
    out = []
    for r in closed_rows:
        root = (r.get("underlying") or r.get("instrument") or "").split(".")[0]
        dirn = direction(r.get("side"), r.get("structure"))
        t0 = _ts(r.get("entered_at"))
        settled = r.get("state") == "settled"
        t1 = (datetime.fromisoformat(r["expiry"]).replace(hour=16, tzinfo=ET) if settled and r.get("expiry")
              else _ts(r.get("closed_at")))
        S0 = price_at(root, t0, loader=loader) if t0 else None
        S1 = r.get("settle_close") if settled else (price_at(root, t1, loader=loader) if t1 else None)
        pth = price_path(root, t0, t1, loader=loader, trading_day=trading_day) if (t0 and t1) else {"complete": False, "rows": [], "missing": []}
        out.append({"id": r["id"], "batch": r.get("batch"), "root": root, "side": r.get("side"), "structure": r.get("structure"),
                    "k_sell": r.get("k_sell"), "dirn": dirn, "t0": t0, "t1": t1, "S0": S0, "S1": S1, "pnl": r.get("pnl"),
                    "judgment": r.get("judgment"), "how": r.get("state"),
                    "path_prices": [p for _, p in pth["rows"]] if pth["complete"] else None, "path_missing": pth["missing"]})
    for r in legacy:
        if not str(r.get("state", "")).startswith("settled"):
            continue
        dirn = direction(r.get("side"), "credit" if r.get("entry_credit") is not None else "debit")
        t0 = _ts(r["entered_at"].replace(" ET", "").replace(" ", "T")) if r.get("entered_at") else None
        out.append({"id": r["id"], "batch": "legacy", "root": r.get("underlying"), "side": r.get("side"), "structure": "credit",
                    "k_sell": r.get("k_sell"), "dirn": dirn, "t0": t0,
                    "t1": datetime.fromisoformat(r["expiry"]).replace(hour=16, tzinfo=ET) if r.get("expiry") else None,
                    "S0": r.get("underlying_entry_px"), "S1": r.get("settle_close"), "pnl": r.get("pnl_usd_with_fee_3_20"),
                    "judgment": r.get("judgment"), "how": "settled",
                    "path_prices": [x for x in (r.get("underlying_path_low"), r.get("underlying_path_high")) if _num(x)] or None,
                    "path_missing": [] if r.get("underlying_path_low") is not None else ["legacy_no_path"]})
    for t in out:
        t.update(attribute(t))
    return out


def summarize(rows: list[dict], baselines: dict | None = None) -> dict:
    """按分组汇总笔数与含费盈亏；baselines：{id: p}（可缺）。"""
    groups: dict = {}
    for t in rows:
        k = t["category"] or ("无方向" if t["dirn"] == 0 else "价格缺失")
        g = groups.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0, "pnl_known": True})
        g["n"] += 1
        if _num(t["pnl"]):
            g["pnl"] += t["pnl"]; g["wins"] += t["pnl"] > 0
        else:
            g["pnl_known"] = False
    base = None
    if baselines is not None:
        cred = [t for t in rows if t["dirn"] != 0 and t["exit_ok"] is not None]
        ps = [baselines.get(t["id"]) for t in cred]
        if cred and all(_num(p) for p in ps):
            base = {"n": len(cred), "expected": round(sum(ps), 2), "observed": sum(1 for t in cred if t["exit_ok"]),
                    "var": round(sum(p * (1 - p) for p in ps), 2), "judgments": len({t["judgment"] for t in cred})}
        else:
            base = {"n": len(cred), "missing": [t["id"] for t, p in zip(cred, ps) if not _num(p)]}
    return {"groups": groups, "baseline": base}


def fetch_closes(roots: set[str]) -> tuple[dict, list[str]]:
    """长桥日线收盘（只读）。失败的品种列入 errors，由调用方显式标注「基线不可得」。"""
    from undertow.collect.longbridge_kline import KlineUnavailable, fetch_series
    out, errs = {}, []
    for r in sorted(roots):
        try:
            s = fetch_series(f"{r}.US", period="day", count=600)
            out[r] = list(zip(s.dates, s.closes))
        except (KlineUnavailable, OSError, ValueError) as e:
            errs.append(f"{r}: {type(e).__name__}: {str(e)[:80]}")
    return out, errs


def baselines_for(rows: list[dict], closes: dict) -> dict:
    out = {}
    for t in rows:
        if t["dirn"] == 0 or t["cushion"] is None or t["root"] not in closes or not (t["t0"] and t["t1"]):
            continue
        out[t["id"]] = baseline_p(closes[t["root"]], t["t0"].astimezone(ET).date(), t["t1"].astimezone(ET).date(),
                                  t["dirn"], t["cushion"])
    return out


def _pct(x, signed=True) -> str:
    return "—" if x is None else (f"{x:+.2%}" if signed else f"{x:.2%}")


def section(rows: list[dict], sm: dict, notes: list[str] | None = None) -> list[str]:
    """渲染成 paper_book 的 Markdown 片段。"""
    md = ["", "## 方向还是墙（已了结：开仓 → 了结的正股变化）", "",
          "> 描述性归因，不是检验。卖腿距离 = 入场时正股离卖腿多远（正 = 价外）；最不利 = 持有期内朝不利方向的最大偏移；"
          "盘中越过 = 持有期内分钟价到过卖腿另一侧。同一判断日的仓位不独立。墙的检验见预登记草案 expiry-pin-v4。", ""]
    md += ["| 分组 | 笔数 | 胜 | 含费盈亏 |", "|---|---|---|---|"]
    for k in ("方向对", "方向错·仍在卖腿有利一侧", "方向错·越过卖腿", "方向错", "无方向", "价格缺失"):
        g = sm["groups"].get(k)
        if g:
            md.append(f"| {k} | {g['n']} | {g['wins']} | ${g['pnl']:+.2f}{'' if g['pnl_known'] else '（有行缺盈亏）'} |")
    b = sm.get("baseline")
    if b and "expected" in b:
        md += ["", f"零模型基线（入场前 {BASELINE_WINDOW} 日同持有期涨跌、无墙）：{b['n']} 笔贷记价差里，预计 {b['expected']} 笔了结时仍在卖腿有利一侧，"
               f"实际 {b['observed']} 笔（Σp(1−p)={b['var']}；只来自 {b['judgments']} 个判断，不当独立样本）。"]
    elif b:
        md += ["", f"零模型基线：不完整（缺 {len(b.get('missing') or [])} 笔：{', '.join((b.get('missing') or [])[:5])}）"]
    for n in notes or []:
        md += ["", f"⚠️ {n}"]
    md += ["", "| 仓位 | 押的方向 | 入场 → 了结正股 | 涨跌 | 顺向 | 卖腿距离 | 最不利 | 盘中越过 | 分组 | 含费盈亏 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for t in sorted(rows, key=lambda x: x["t0"] or datetime.min.replace(tzinfo=timezone.utc)):
        dn = {1: "看涨", -1: "看跌", 0: "无方向"}[t["dirn"]]
        px = f"{t['S0'] if _num(t['S0']) else '—'} → {t['S1'] if _num(t['S1']) else '—'}"
        mae = _pct(t["mae"]) if t["mae"] is not None else \
            ("路径缺：" + ",".join(t["path_missing"][:2]) if t["path_missing"] and t["dirn"] != 0 else "—")
        cr = {True: "是", False: "否", None: "—"}[t["crossed_intraday"]]
        md.append(f"| {t['id']} | {dn} | {px} | {_pct(t['chg'])} | {_pct(t['fav'])} | {_pct(t['cushion'])} | {mae} | {cr} | "
                  f"{t['category'] or '—'} | {'$%+.2f' % t['pnl'] if _num(t['pnl']) else '—'} |")
    return md
