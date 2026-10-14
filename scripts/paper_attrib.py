"""模拟仓已了结仓位的「方向还是墙」归因（用户 2026-10-14：「你需要统计一下，模拟仓已经完成的，究竟是方向对了，还是墙撑住了。
统计从开仓到结束的正股价格变化」；同意把这一栏常设进每日报告）。

口径（描述性，不是检验；检验见 docs/prereg/2026-10-14_expiry_pin_v4_DRAFT.md）：
- 押的方向：卖 put 价差 / 买 call 价差 = 看涨（+1）；卖 call 价差 / 买 put 价差 = 看跌（−1）。宽跨等无方向结构不归因。
- 正股价：入场时刻之前最后一个分钟价（data/history/option_intraday/<根>/<ET日>.json.gz，收盘后存档）；
  了结价：到期结算用结算收盘，提前了结用了结时刻之前最后一个分钟价。旧仓（9/29 前）没有分钟存档，用回填在
  legacy_positions.jsonl 的 underlying_* 字段（出处写在 underlying_px_source）。缺任一 → None，不估、不折零。
- 卖腿距离（贷记）：入场时正股离卖腿多远（正 = 价外）；最不利 = 持有期内朝不利方向的最大偏移；
  盘中越过卖腿 = 持有期内价格路径是否到过卖腿另一侧。分钟存档是 1m 收盘价（分钟内高低未观察）；旧仓是长桥 30m K 线高低。
  路径有缺失日、空存档日或日内缺口 > GAP_OK_MIN 分钟 → 没观察到越过也只写「未观察到」，不写「否」。
- 分组（Codex 044 E1 修订）：未变（正股收盘与入场完全相同，不设死区）/ 方向对 / 方向错·仍在卖腿有利一侧 /
  方向错·收在卖腿上（恰等于行权价：触及，不叫越过）/ 方向错·越过卖腿。借记价差只分方向对错。
  「小幅反向」「没越卖腿」「赚钱」是三件不同的事：好侧组可以因提前退出亏钱，越过组可以因权利金覆盖而赚钱。
- 基线（只作描述性量级参考）：入场前 250 个交易日、同持有交易日数的收盘涨跌分布下，了结时仍在卖腿有利一侧的概率 p，
  报告 Σp 与实际笔数。持有期取【实际】退出日，而退出时间可能由路径决定（止盈/止损/时间出场），所以这不是事前的
  「无墙概率」；拿历史收盘→收盘比实际盘中入场→盘中退出，口径也不同。不能用它确认或否定墙的作用（检验见预登记）。
  同一判断日的仓位不独立，不报标准误。同日开平（h=0）没有基线，单独列出。
- 每行写明价格来源与分辨率；补记仓位的入场时刻 = 补记所用报价时刻（不是登记时刻），也写明。
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
GAP_OK_MIN = 5          # 1m 存档的日内缺口容忍（分钟）；超过 → 路径不完整，只能说「未观察到」越过。操作约定，未校准
EPS = 1e-9


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
    """t0..t1 之间的分钟价路径。任一交易日存档缺失、或该日在持有窗口内一行都没有（空存档）→ 记入 missing；
    complete 还要求每个交易日内（含开头到第一根、最后一根到收尾）缺口 ≤ GAP_OK_MIN 分钟。
    Codex 044 E1：旧版把「某日存档为空」当作不缺，两天持有、第二天无行仍 complete=True。"""
    if trading_day is None:
        from undertow.core import market_calendar as mc
        trading_day = mc.is_trading_day
    rows, missing, max_gap = [], [], 0.0
    d = t0.astimezone(ET).date()
    while d <= t1.astimezone(ET).date():
        if trading_day(d) is not False:
            m = loader(root, d.isoformat())
            lo = max(t0, datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET))
            hi = min(t1, datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))
            day_rows = [(t, p) for t, p in (m or []) if t0 <= t <= t1]
            if m is None:
                missing.append(d.isoformat())
            elif not day_rows:
                if lo < hi:                                     # 窗口内本该有行
                    missing.append(f"{d.isoformat()}:无行")
            else:
                ts_ = [lo] + [t for t, _ in day_rows] + [hi]
                max_gap = max([max_gap] + [(b - a).total_seconds() / 60 for a, b in zip(ts_, ts_[1:])])
            rows += day_rows
        d += timedelta(days=1)
    return {"rows": rows, "missing": missing, "max_gap_min": round(max_gap, 1),
            "complete": not missing and bool(rows) and max_gap <= GAP_OK_MIN}


def price_at(root: str, t: datetime, *, loader=load_minutes) -> float | None:
    """t 之前（含）最后一个分钟价；当天存档缺失或 t 早于当天第一根 → None。"""
    m = loader(root, t.astimezone(ET).date().isoformat())
    prev = [p for tt, p in (m or []) if tt <= t]
    return prev[-1] if prev else None


def attribute(t: dict) -> dict:
    """纯函数。t：{dirn, structure, k_sell, S0, S1, path_prices(list|None), path_complete(bool, 缺省 = 有路径即完整)}。
    返回归因字段；缺价 → category=None。exit_side ∈ 有利 / 等于 / 越过（恰等于行权价是「等于」，不是越过）。
    crossed_intraday：True = 观察到越过；False = 路径完整且没越过；None = 没路径，或路径不完整且没观察到（不能排除）。"""
    dirn, S0, S1, ks = t["dirn"], t.get("S0"), t.get("S1"), t.get("k_sell")
    out = {"chg": None, "fav": None, "cushion": None, "mae": None, "crossed_intraday": None, "touched_intraday": None,
           "exit_side": None, "exit_ok": None, "category": None}
    if dirn == 0 or not (_num(S0) and _num(S1)) or S0 <= 0:
        return out
    chg = S1 / S0 - 1
    out.update(chg=round(chg, 6), fav=round(chg * dirn, 6))
    credit = t.get("structure") != "debit"
    path = t.get("path_prices")
    full = bool(path) and t.get("path_complete", True)
    if full:
        worst = min(path) if dirn > 0 else max(path)
        out["mae"] = round(min((worst / S0 - 1) * dirn, 0.0), 6)
    if credit and _num(ks):
        out["cushion"] = round(((S0 - ks) if dirn > 0 else (ks - S0)) / S0, 6)
        d1 = (S1 - ks) * dirn
        out["exit_side"] = "等于" if abs(S1 - ks) < EPS else ("有利" if d1 > 0 else "越过")
        out["exit_ok"] = out["exit_side"] == "有利"
        if path:
            crossed = any(((p - ks) * dirn) < -EPS for p in path)
            out["crossed_intraday"] = True if crossed else (False if full else None)
            out["touched_intraday"] = any(abs(p - ks) < EPS for p in path) or crossed
    if abs(S1 - S0) < EPS:
        out["category"] = "未变"
    elif out["fav"] > 0:
        out["category"] = "方向对"
    elif not credit:
        out["category"] = "方向错"
    elif out["exit_side"] is None:
        out["category"] = None
    else:
        out["category"] = {"有利": "方向错·仍在卖腿有利一侧", "等于": "方向错·收在卖腿上", "越过": "方向错·越过卖腿"}[out["exit_side"]]
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
        pth = price_path(root, t0, t1, loader=loader, trading_day=trading_day) if (t0 and t1) else \
            {"complete": False, "rows": [], "missing": [], "max_gap_min": None}
        src = "入场 1m 收盘 @ 入场时刻"
        if r.get("retrospective_fill"):
            src += f"（补记：入场时刻 = 补记所用报价时刻，登记于 {str(r.get('recorded_at') or '?')[:16]}）"
        src += "；了结 " + ("结算收盘" if settled else "1m 收盘 @ 了结时刻") + "；路径 1m 收盘（分钟内高低未观察）"
        out.append({"id": r["id"], "batch": r.get("batch"), "root": root, "side": r.get("side"), "structure": r.get("structure"),
                    "k_sell": r.get("k_sell"), "dirn": dirn, "t0": t0, "t1": t1, "S0": S0, "S1": S1, "pnl": r.get("pnl"),
                    "judgment": r.get("judgment"), "how": r.get("state"), "price_source": src,
                    "path_prices": [p for _, p in pth["rows"]] or None, "path_complete": pth["complete"],
                    "path_missing": pth["missing"], "path_max_gap_min": pth.get("max_gap_min")})
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
                    "price_source": "回填：" + (r.get("underlying_px_source") or "出处未记；路径 = 30m K 线最高/最低"),
                    "path_prices": [x for x in (r.get("underlying_path_low"), r.get("underlying_path_high")) if _num(x)] or None,
                    "path_complete": r.get("underlying_path_low") is not None,
                    "path_missing": [] if r.get("underlying_path_low") is not None else ["legacy_no_path"], "path_max_gap_min": None})
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
        no_h = [t["id"] for t in cred if t["id"] in baselines and baselines[t["id"]] is None and t.get("same_day")]
        cred_b = [t for t in cred if t["id"] not in no_h]
        ps = [baselines.get(t["id"]) for t in cred_b]
        if cred_b and all(_num(p) for p in ps):
            base = {"n": len(cred_b), "expected": round(sum(ps), 2), "observed": sum(1 for t in cred_b if t["exit_ok"]),
                    "at_strike": sum(1 for t in cred_b if t["exit_side"] == "等于"),
                    "judgments": len({t["judgment"] for t in cred_b}), "same_day_no_baseline": no_h}
        else:
            base = {"n": len(cred_b), "missing": [t["id"] for t, p in zip(cred_b, ps) if not _num(p)], "same_day_no_baseline": no_h}
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
        t["same_day"] = t["t0"].astimezone(ET).date() == t["t1"].astimezone(ET).date()
        out[t["id"]] = baseline_p(closes[t["root"]], t["t0"].astimezone(ET).date(), t["t1"].astimezone(ET).date(),
                                  t["dirn"], t["cushion"])
    return out


def _pct(x, signed=True) -> str:
    return "—" if x is None else (f"{x:+.2%}" if signed else f"{x:.2%}")


def section(rows: list[dict], sm: dict, notes: list[str] | None = None) -> list[str]:
    """渲染成 paper_book 的 Markdown 片段。"""
    md = ["", "## 方向还是墙（已了结：开仓 → 了结的正股变化）", "",
          "> 路径分类描述，不是墙的因果贡献。卖腿距离 = 入场时正股离卖腿多远（正 = 价外）；最不利 = 持有期内朝不利方向的最大偏移；"
          "盘中越过：是 = 观察到；否 = 路径完整且没越过；未观察到 = 路径有缺口、不能排除。恰收在行权价上记「收在卖腿上」，不叫越过。"
          "「没越卖腿」不等于「赚钱」。同一判断日的仓位不独立。墙的检验见预登记（修订中）。", ""]
    md += ["| 分组 | 笔数 | 胜 | 含费盈亏 |", "|---|---|---|---|"]
    for k in ("方向对", "未变", "方向错·仍在卖腿有利一侧", "方向错·收在卖腿上", "方向错·越过卖腿", "方向错", "无方向", "价格缺失"):
        g = sm["groups"].get(k)
        if g:
            md.append(f"| {k} | {g['n']} | {g['wins']} | ${g['pnl']:+.2f}{'' if g['pnl_known'] else '（有行缺盈亏）'} |")
    b = sm.get("baseline")
    if b and "expected" in b:
        md += ["", f"历史量级参考（入场前 {BASELINE_WINDOW} 日、同【实际】持有交易日数的收盘涨跌）：{b['n']} 笔贷记价差里，"
               f"按历史涨跌约 {b['expected']} 笔会停在卖腿有利一侧，实际 {b['observed']} 笔"
               + (f"（另有 {b['at_strike']} 笔恰收在卖腿上）" if b.get("at_strike") else "")
               + f"。只来自 {b['judgments']} 个判断；持有期由实际退出决定、收盘口径对盘中，不能用来确认或否定墙。"]
    elif b:
        md += ["", f"历史量级参考：不完整（缺 {len(b.get('missing') or [])} 笔：{', '.join((b.get('missing') or [])[:5])}）"]
    if b and b.get("same_day_no_baseline"):
        md += [f"同日开平、没有基线：{', '.join(b['same_day_no_baseline'])}"]
    for n in notes or []:
        md += ["", f"⚠️ {n}"]
    md += ["", "| 仓位 | 押的方向 | 入场 → 了结正股 | 涨跌 | 顺向 | 卖腿距离 | 最不利 | 盘中越过 | 分组 | 含费盈亏 | 价格来源 |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in sorted(rows, key=lambda x: x["t0"] or datetime.min.replace(tzinfo=timezone.utc)):
        dn = {1: "看涨", -1: "看跌", 0: "无方向"}[t["dirn"]]
        px = f"{t['S0'] if _num(t['S0']) else '—'} → {t['S1'] if _num(t['S1']) else '—'}"
        mae = _pct(t["mae"]) if t["mae"] is not None else \
            ("路径缺：" + ",".join(t["path_missing"][:2]) if t["path_missing"] and t["dirn"] != 0 else
             (f"路径缺口 {t.get('path_max_gap_min')} 分钟" if t.get("path_prices") and not t.get("path_complete") else "—"))
        if t["crossed_intraday"] is None and t.get("path_prices") and t["dirn"] != 0 and t["cushion"] is not None:
            cr = "未观察到（路径不完整）"
        else:
            cr = {True: "是", False: "否", None: "—"}[t["crossed_intraday"]]
        md.append(f"| {t['id']} | {dn} | {px} | {_pct(t['chg'])} | {_pct(t['fav'])} | {_pct(t['cushion'])} | {mae} | {cr} | "
                  f"{t['category'] or '—'} | {'$%+.2f' % t['pnl'] if _num(t['pnl']) else '—'} | {t.get('price_source') or '—'} |")
    return md
