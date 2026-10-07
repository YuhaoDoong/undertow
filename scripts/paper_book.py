"""模拟仓台账统计报告（用户 2026-09-30：「展示一下我们现在的模拟仓情况。以及历史模拟仓的成败记录」；
「模拟仓所有记录你应该定时统计分析生成一个report。」）

  python3 scripts/paper_book.py [--live]      # --live：对在场仓位现取报价估当前平仓价值；默认只用已记录的盯市

读私有 data/soul/journal.json 与 data/paper/legacy_positions.jsonl（只读），写 data/paper/reports/paper_<ET日>.html 与 .md（data/paper 不入库）。
统计口径：
- 仓位 = 有结构化规格（paper）的记录；早期没有规格的模拟判断单列「判断记录」，只看对错。
- 批次（claude_selected / rule_dynamic / user_subjective / legacy）分开；同一判断（judgment_id）的多个仓位不当独立样本。
- 盈亏一律含费；比较时同时看含费最大亏损与收益/最大亏损比，不按单笔美元比优劣（Codex 031）。
- 未入场（skipped / missed）按原因计数；被修订替代的版本（superseded）单列、不算失败。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape as _esc
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
OUT = ROOT / "data/paper/reports"               # 模拟仓数据单独一个文件夹（用户 2026-09-30），私有、不入库
CLOSED = ("settled", "closed_stop", "closed_manual", "closed_tp", "closed_time")
HOW = {"settled": "到期结算", "closed_stop": "止损", "closed_manual": "用户主动平仓", "closed_tp": "规则止盈", "closed_time": "规则时间出场"}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _close_assumption(p: dict):
    """旧记录没有 exit_assumption 字段：从了结事件里取估值假设（缺保护腿买价按 0 等）。"""
    for e in reversed(p.get("events") or []):
        if e.get("action") in ("stop", "take_profit", "time_exit", "manual_close"):
            if e.get("valuation_assumption"):
                return e["valuation_assumption"]
            m = next((x for x in reversed(p.get("events") or []) if x.get("action") in ("mark", "mark_offhours")
                      and x.get("at") == e.get("at")), None)
            return (m or {}).get("valuation_assumption")
    return None


def result_class(r: dict) -> str:
    """已了结仓位的结果身份（Codex 032 R2）：
    unknown = 盈亏或最大亏损缺失/非有限（不能折零当打平）；under_review = 两源结算不一致待复核；
    assumed = 退出估值用了假设（如保护腿无买价按 0）——不是两腿完成退出；formal = 完整的模拟结果。"""
    if not _num(r.get("pnl")) or not _num((r.get("econ") or {}).get("max_loss_usd")):
        return "unknown"
    if r.get("result_under_review"):
        return "under_review"
    if r.get("exit_assumption"):
        return "assumed"
    return "formal"


def _last_mark(p: dict) -> dict | None:
    """最近一次可信的盯市估值：未通过估值体检的（如保护腿缺买价却按 0 计、估值越出价差范围）不当作有效估值（2026-10-06 严查）。"""
    from scripts.paper_trades import exit_problem
    for e in reversed(p.get("events") or []):
        if e.get("action") not in ("mark", "mark_offhours"):
            continue
        try:
            bad = p.get("sell") and exit_problem(e.get("quotes") or {}, p, e.get("value"), e.get("valuation_assumption"),
                                                 automatic=False)
        except (KeyError, TypeError):
            bad = None
        if not bad:
            return e
    return None


def classify(theses: list) -> dict:
    """纯函数：把 journal 里的模拟记录分成 在场 / 已了结 / 未入场 / 被修订替代 / 待入场 / 早期判断记录。"""
    from scripts.paper_trades import judgment_id
    j = {"theses": theses}
    out = {"open": [], "closed": [], "not_entered": [], "superseded": [], "planned": [], "legacy_judgments": []}
    for t in theses:
        if t.get("execution") != "模拟":
            continue
        p = t.get("paper")
        if not p:
            out["legacy_judgments"].append(t); continue
        row = {"id": t["id"], "date": t.get("date"), "instrument": t.get("instrument"), "batch": p.get("batch", "legacy"),
               "judgment": judgment_id(j, t), "state": p.get("state"), "side": p.get("side"), "expiry": p.get("expiry"),
               "k_sell": p.get("k_sell"), "k_buy": p.get("k_buy"), "credit": p.get("entry_credit"),
               "structure": p.get("structure", "credit"), "qty": p.get("qty", 1), "hold": p.get("hold_to_expiry"),
               "exit_assumption": p.get("exit_assumption") or _close_assumption(p),
               "econ": p.get("economics") or {}, "pnl": p.get("pnl_usd"), "stop": p.get("stop_value"),
               "exit_rule": p.get("exit_rule"), "strike_rule": p.get("strike_rule"), "entered_at": p.get("entered_at"),
               "reason": p.get("skip_reason") or p.get("invalid_reason"), "mark": _last_mark(p),
               "close_request": p.get("close_request"), "result_under_review": p.get("result_under_review")}
        st = p.get("state")
        if st == "entered":
            out["open"].append(row)
        elif st in CLOSED:
            out["closed"].append(row)
        elif st == "planned":
            out["planned"].append(row)
        elif str(row["reason"] or "").startswith(("superseded_by", "revised_before_entry")):
            out["superseded"].append(row)
        else:
            out["not_entered"].append(row)
    return out


LEGACY = ROOT / "data/paper/legacy_positions.jsonl"


def load_legacy(path: Path | None = None) -> list:
    """9/29 之前的模拟仓：日记里有成交、但没有结构化规格，已按日记逐笔回填（每条注明出处）。"""
    p = path or LEGACY
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()] if p.exists() else []


def summarize_legacy(rows: list) -> dict:
    """纯函数：回填旧仓的汇总（已结算的记账盈亏、按现行费用口径的盈亏、胜负）；未记成交与未执行单列。
    盈亏或最大亏损缺失的已结算行不折零（Codex 032 R2）：计入 incomplete，不进胜负与合计，比值只用同一组完整行。"""
    settled = [r for r in rows if str(r.get("state", "")).startswith("settled")]
    done = [r for r in settled if _num(r.get("pnl_usd_recorded")) and _num(r.get("max_loss_usd_recorded"))]
    pnl = [r["pnl_usd_recorded"] for r in done]
    fee = [r.get("pnl_usd_with_fee_3_20") for r in done]
    return {"closed": len(done), "wins": sum(x > 0 for x in pnl), "losses": sum(x < 0 for x in pnl),
            "flat": sum(x == 0 for x in pnl), "pnl_usd": round(sum(pnl), 2),
            "pnl_usd_with_fee": round(sum(fee), 2) if all(_num(x) for x in fee) else None,
            "max_loss_sum_usd": round(sum(r["max_loss_usd_recorded"] for r in done), 2),
            "judgments": len({r.get("judgment") for r in done}),
            "incomplete": [r["id"] for r in rows if r.get("state") in ("fill_not_recorded", "not_executed")]
                          + [r["id"] for r in settled if r not in done]}


def shadow_summary() -> list:
    """影子账 v5（每天每品种 put/call 自动模拟的卖墙价差候选，预登记研究）的头几行汇总；取不到就如实说。"""
    import subprocess
    try:
        r = subprocess.run([sys.executable, "-m", "undertow.cli", "shadow", "report"], cwd=ROOT, capture_output=True,
                           text=True, timeout=240)
        lines = [x for x in r.stdout.splitlines() if x.strip() and not x.startswith("[留痕]")]
        if r.returncode != 0:                     # Codex 032 R7：有输出也不能忽略非零返回码
            return [f"⚠️（shadow report rc={r.returncode}，以下可能不完整）"] + lines[:3]
        return lines[:3] or [f"（shadow report 无输出，rc={r.returncode}）"]
    except Exception as e:
        return [f"（shadow report 失败：{type(e).__name__}: {e}）"[:160]]


def summarize(c: dict) -> dict:
    """纯函数：按批次汇总已了结仓位（含费盈亏、胜负、最大亏损合计、收益/最大亏损比）与在场、未入场数。"""
    # 正式结果（formal）单独汇总；未知 / 待复核 / 估算退出分别计数，不折零、不混进合计（Codex 032 R2）。
    # 盈亏与最大亏损用同一组 formal 记录，比值的分子分母同源。
    by = defaultdict(lambda: {"closed": 0, "wins": 0, "losses": 0, "flat": 0, "pnl": 0.0, "max_loss_sum": 0.0,
                              "open": 0, "not_entered": 0, "judgments": set(), "unknown": 0, "under_review": 0,
                              "assumed": 0, "assumed_pnl": 0.0})
    for r in c["closed"]:
        b = by[r["batch"]]
        b["judgments"].add(r["judgment"])
        k = result_class(r)
        if k == "unknown":
            b["unknown"] += 1; continue
        if k == "under_review":
            b["under_review"] += 1; continue
        if k == "assumed":
            b["assumed"] += 1; b["assumed_pnl"] += r["pnl"]; continue
        b["closed"] += 1
        b["pnl"] += r["pnl"]
        b["max_loss_sum"] += r["econ"]["max_loss_usd"]
        b["wins" if r["pnl"] > 0 else "losses" if r["pnl"] < 0 else "flat"] += 1
    for r in c["open"]:
        by[r["batch"]]["open"] += 1
        by[r["batch"]]["judgments"].add(r["judgment"])
    for r in c["not_entered"]:
        by[r["batch"]]["not_entered"] += 1
    out = {}
    for k, v in by.items():
        out[k] = {**{x: v[x] for x in ("closed", "wins", "losses", "flat", "open", "not_entered", "unknown",
                                       "under_review", "assumed")},
                  "assumed_pnl_usd": round(v["assumed_pnl"], 2),
                  "complete": v["unknown"] == 0 and v["under_review"] == 0 and v["assumed"] == 0,
                  "pnl_usd": round(v["pnl"], 2), "max_loss_sum_usd": round(v["max_loss_sum"], 2),
                  "pnl_per_max_loss": round(v["pnl"] / v["max_loss_sum"], 4) if v["max_loss_sum"] else None,
                  "judgments": len(v["judgments"])}
    out["_reasons"] = dict(Counter((r["reason"] or "?").split("（")[0] for r in c["not_entered"]))
    return out


def _hold_cell(r: dict) -> str:
    """提前了结的仓位：到期后补记的「假设持有到期」盈亏与提前了结的影响；到期结算的仓位不适用。"""
    if r.get("state") == "settled":
        return "（到期结算，不适用）"
    h = r.get("hold")
    if not h:
        return "待到期后补记"
    eff = h.get("early_exit_effect_usd")
    return (f"${h['pnl_usd']:+.2f}（到期收 {h['close']}）；提前了结影响 "
            + (f"${eff:+.2f}" if _num(eff) else "—"))


def early_exit_section(closed: list) -> list:
    """按批次 × 了结方式汇总：提前了结的实际含费盈亏 vs 假设持有到期，差额 = 提前了结规则/决定的贡献。
    只统计已补记反事实的仓位；样本极小时只作描述，不据此改规则（规则改动须事前登记）。"""
    rows = [r for r in closed if r.get("state") != "settled"]
    if not rows:
        return []
    by = defaultdict(lambda: {"n": 0, "done": 0, "actual": 0.0, "hold": 0.0})
    for r in rows:
        k = (r["batch"], HOW.get(r["state"], r["state"]))
        b = by[k]; b["n"] += 1
        h = r.get("hold")
        if h and _num(h.get("pnl_usd")) and _num(r.get("pnl")):
            b["done"] += 1; b["actual"] += r["pnl"]; b["hold"] += h["pnl_usd"]
    out = ["", "## 提前了结 vs 假设持有到期（反事实对照）", "",
           "> 差额 > 0 表示提前了结比持有到期多赚（或少亏）。样本很小，只作描述；改出场规则须事前登记新版本。", "",
           "| 批次 | 了结方式 | 笔数（已补记） | 实际含费合计 | 持有到期含费合计 | 提前了结的影响 |", "|---|---|---|---|---|---|"]
    for (b, how), v in sorted(by.items()):
        diff = v["actual"] - v["hold"]
        out.append(f"| {b} | {how} | {v['n']}（{v['done']}） | ${v['actual']:+.2f} | ${v['hold']:+.2f} | "
                   f"{'$%+.2f' % diff if v['done'] else '—'} |")
    return out


def _amt(r: dict) -> str:
    """入场权利金：贷记为收入；借记价差 credit 为负，显示成「付 x」。"""
    c = r.get("credit")
    if c is None:
        return "—"
    return f"付 {-c:g}" if r.get("structure") == "debit" else f"{c:g}"


def _struct(r: dict) -> str:
    kind = {"P": "put", "C": "call"}.get(r["side"], "?")
    if r.get("structure") == "debit":
        if r["k_buy"] is None:
            return f"{r['instrument']} {kind} 借记价差（入场时买平值、卖约 +宽度）· {r['expiry']}"
        return f"{r['instrument']} 买 {r['k_buy']:g}{r['side']} / 卖 {r['k_sell']:g}{r['side']}（借记）· {r['expiry']}"
    if r["k_sell"] is None:
        return f"{r['instrument']} {kind} 价差（入场时自动选档）· {r['expiry']}"
    return f"{r['instrument']} 卖 {r['k_sell']:g}{r['side']} / 买 {r['k_buy']:g}{r['side']} · {r['expiry']}" if r["k_buy"] is not None \
        else f"{r['instrument']} 卖 {r['k_sell']:g}{r['side']}（保护腿入场时选）· {r['expiry']}"


def live_marks(rows: list) -> dict:
    """在场仓位现取报价估当前平仓价值（保守：卖腿 ask − 买腿 bid，缺买盘按 0 并标注）。"""
    from scripts.paper_trades import _depth, close_value
    out = {}
    for r in rows:
        sym = r.get("_sell"), r.get("_buy")
        if not all(sym):
            continue
        pp = {"sell": sym[0], "buy": sym[1], "k_sell": r["k_sell"], "k_buy": r["k_buy"], "structure": r.get("structure")}
        qq = _depth(list(sym))
        v = close_value(qq, pp)
        if v is not None:
            from scripts.paper_trades import exit_problem
            bad = exit_problem(qq, pp, v[0], v[1], automatic=False)
            v = (v[0], f"⚠️ 估值不可信：{bad}") if bad else v
        out[r["id"]] = v
    return out


def render(day: str, c: dict, sm: dict, live: dict, generated_at: str, legacy: list | None = None,
           shadow: list | None = None) -> tuple[str, str]:
    batches = sorted(k for k in sm if not k.startswith("_"))
    md = [f"# 模拟仓台账 · {day}", "", f"> 生成于 {generated_at}。盈亏含费；批次分开；同一判断的多个仓位不当独立样本；"
          "比较看含费最大亏损与收益/最大亏损比，不按单笔美元比优劣。数据：私有 journal（只读）。", "",
          "## 汇总（按批次）", "", "> 「正式」= 盈亏与最大亏损齐全、两腿按报价完成退出或到期结算、无待复核。未知 / 待复核 / 估算退出单列，"
          "不折零、不进正式合计；合计后标「不完整」即表示该批还有单列记录。", "",
          "| 批次 | 正式了结 | 胜 / 负 / 平 | 正式含费盈亏 | 正式最大亏损合计 | 盈亏/最大亏损 | 未知 / 待复核 / 估算退出（估算盈亏） | 在场 | 未入场 | 涉及判断数 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for b in batches:
        s = sm[b]
        extra = f"{s['unknown']} / {s['under_review']} / {s['assumed']}" + (f"（${s['assumed_pnl_usd']:+.2f}）" if s["assumed"] else "")
        md.append(f"| {b} | {s['closed']} | {s['wins']} / {s['losses']} / {s['flat']} | ${s['pnl_usd']:+.2f}"
                  f"{'' if s['complete'] else '（不完整）'} | ${s['max_loss_sum_usd']:.2f} | "
                  f"{s['pnl_per_max_loss'] if s['pnl_per_max_loss'] is not None else '—'} | {extra} | {s['open']} | {s['not_entered']} | {s['judgments']} |")
    md += ["", f"未入场原因：{sm.get('_reasons') or '—'}", "", "## 当前在场", "",
           "| 仓位 | 批次 | 入场权利金 | 含费最大收益 / 亏损 | 盈亏平衡 | 止损线 | 最近估值（时刻） | 按估值平仓的含费盈亏 | 出场规则 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in c["open"]:
        m = r["mark"]
        lv = live.get(r["id"])
        val, when, note = (lv[0], "现取", lv[1]) if lv else ((m or {}).get("value"), ((m or {}).get("at") or "")[:16], (m or {}).get("valuation_assumption"))
        e = r["econ"]
        fee, qty = e.get("fee_total_usd"), r.get("qty") or 1     # 整单费用 + 每组 ×100 × 组数（Codex 032 R5：原先漏乘组数）
        pnl = round((r["credit"] - val) * 100 * qty - fee, 2) if _num(val) and _num(r["credit"]) and _num(fee) else None
        md.append(f"| {r['id']}<br>{_struct(r)} | {r['batch']} | {_amt(r)} | ${e.get('max_gain_usd')} / ${e.get('max_loss_usd')} | "
                  f"{e.get('breakeven')} | {r['stop'] if r['stop'] is not None else '不设'} | {val if val is not None else '—'}（{when}{'；' + note if note else ''}） | "
                  f"{'$%+.2f' % pnl if pnl is not None else '—'} | {r['exit_rule'] or ('用户主观' if r['batch'] == 'user_subjective' else '—')}"
                  f"{'；⏳ 平仓请求待开盘执行' if r['close_request'] else ''} |")
    if not c["open"]:
        md.append("| （无） | | | | | | | | |")
    md += ["", "## 待入场", ""] + [f"- {r['id']}：{_struct(r)}（{r['batch']}，{r['strike_rule'] or '固定档位'}）" for r in c["planned"]] + \
          ([] if c["planned"] else ["- （无）"])
    md += ["", "## 已了结", "", "| 仓位 | 批次 | 了结方式 | 入场权利金 | 含费盈亏 | 含费最大亏损 | 盈亏/最大亏损 | 若持有到期（反事实） |",
           "|---|---|---|---|---|---|---|---|"]
    for r in c["closed"]:
        ml, k = r["econ"].get("max_loss_usd"), result_class(r)
        tag = {"unknown": "（结果未知）", "under_review": "（待复核）",
               "assumed": f"（估算退出：{r['exit_assumption']}）", "formal": ""}[k]
        md.append(f"| {r['id']}<br>{_struct(r)} | {r['batch']} | {HOW.get(r['state'], r['state'])}{tag} | {_amt(r)} | "
                  f"{'$%+.2f' % r['pnl'] if _num(r['pnl']) else '—'} | {'$' + str(ml) if _num(ml) else '—'} | "
                  f"{round(r['pnl'] / ml, 3) if _num(r['pnl']) and _num(ml) and ml else '—'} | {_hold_cell(r)} |")
    if not c["closed"]:
        md.append("| （无） | | | | | | | |")
    md += early_exit_section(c["closed"])
    md += ["", "## 未入场（按规则放弃）", ""] + [f"- {r['id']}（{r['batch']}）：{r['reason']}" for r in c["not_entered"]] + \
          ([] if c["not_entered"] else ["- （无）"])
    md += ["", f"## 被修订替代的版本（{len(c['superseded'])}，不算失败）", ""] + [f"- {r['id']}：{str(r['reason'])[:80]}" for r in c["superseded"]]
    legacy = legacy or []
    if legacy:
        ls = summarize_legacy(legacy)
        md += ["", "## 旧模拟仓（9/29 之前，按日记成交逐笔回填）", "",
               f"已结算 {ls['closed']}（胜 {ls['wins']} / 负 {ls['losses']} / 平 {ls['flat']}），记账盈亏 ${ls['pnl_usd']:+.2f}"
               f"（当时未计费；按现行每组 $3.20 为 "
               f"{'$%+.2f' % ls['pnl_usd_with_fee'] if ls['pnl_usd_with_fee'] is not None else '—（有行缺此项）'}），"
               f"最大亏损合计 ${ls['max_loss_sum_usd']:.2f}，"
               f"涉及判断 {ls['judgments']} 个；记录不全或未执行：{ls['incomplete'] or '无'}", "",
               "| 仓位 | 结构 | 入场 | 权利金 / 成本 | 结算收盘 | 记账盈亏 | 含现行费用 | 状态 | 出处与说明 |", "|---|---|---|---|---|---|---|---|---|"]
        for r in legacy:
            legs = (f"卖 {r['k_sell']:g}{r['side']} / 买 {r['k_buy']:g}{r['side']} · {r.get('expiry')}" if r.get("k_sell") is not None
                    else " + ".join(f"{x['side']} {x['sym']}" for x in r.get("legs", [])) or r.get("underlying"))
            amt = r.get("entry_credit") if r.get("entry_credit") is not None else r.get("entry_debit")
            md.append(f"| {r['id']} | {r.get('underlying')} {legs} | {r.get('entered_at') or '—'} | {amt if amt is not None else '—'} | "
                      f"{r.get('settle_close') or '—'} | {('$%+.2f' % r['pnl_usd_recorded']) if r.get('pnl_usd_recorded') is not None else '—'} | "
                      f"{('$%+.2f' % r['pnl_usd_with_fee_3_20']) if r.get('pnl_usd_with_fee_3_20') is not None else '—'} | {r.get('state')} | "
                      f"{r.get('source', '')}；{r.get('note', '')} |")
    if shadow:
        md += ["", "## 影子账 v5（每天每品种 put / call 自动模拟的卖墙价差候选；预登记研究，结论待正式检验）", ""] + [f"> {x}" for x in shadow]
    md += ["", "## 早期判断记录（没有结构化仓位规格，只看判断对错）", "", "| 记录 | 日期 | 方向 | 结果 | 盈亏 |", "|---|---|---|---|---|"]
    for t in c["legacy_judgments"]:
        md.append(f"| {t['id']} | {t.get('date')} | {str(t.get('direction'))[:40]} | {t.get('outcome')} | {t.get('trade_pnl') or '—'} |")
    md_s = "\n".join(md) + "\n"
    html = _md_to_html(md_s, day)
    return md_s, html


def _md_to_html(md: str, day: str) -> str:
    """把上面固定格式的 Markdown（标题、表格、列表、引用）转成简单 HTML（标准库，无第三方）。"""
    out, in_tab, in_ul = [], False, False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [x.strip() for x in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-"):
                continue
            if not in_tab:
                out.append("<table>"); in_tab = True
                out.append("<tr>" + "".join(f"<th>{_esc(x)}</th>" for x in cells) + "</tr>"); continue
            out.append("<tr>" + "".join("<td>" + _esc(x).replace("&lt;br&gt;", "<br>") + "</td>" for x in cells) + "</tr>"); continue
        if in_tab:
            out.append("</table>"); in_tab = False
        if line.startswith("- "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_esc(line[2:])}</li>"); continue
        if in_ul:
            out.append("</ul>"); in_ul = False
        if line.startswith("# "):
            out.append(f"<h1>{_esc(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{_esc(line[3:])}</h2>")
        elif line.startswith("> "):
            out.append(f'<div class="sub">{_esc(line[2:])}</div>')
        elif line.strip():
            out.append(f"<p>{_esc(line)}</p>")
    if in_tab:
        out.append("</table>")
    if in_ul:
        out.append("</ul>")
    css = ('<style>body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:1200px;margin:0 auto;padding:16px;color:#1f2328;background:#fff}'
           'table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}td,th{border-bottom:1px solid #eaeef2;padding:5px 6px;text-align:left;vertical-align:top}'
           '.sub{color:#57606a;font-size:13px}h1{font-size:20px}h2{font-size:16px;margin-top:22px}</style>')
    return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>模拟仓台账 · {_esc(day)}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{css}</head><body>' + "\n".join(out) + "</body></html>")


def main():
    live_flag = "--live" in sys.argv
    j = json.loads((ROOT / "data/soul/journal.json").read_text("utf-8"))
    c = classify(j.get("theses", []))
    sm = summarize(c)
    live, partial = {}, []
    if live_flag:
        by = {t["id"]: t for t in j["theses"]}
        rows = [{**r, "_sell": by[r["id"]]["paper"].get("sell"), "_buy": by[r["id"]]["paper"].get("buy")} for r in c["open"]]
        try:
            live = live_marks(rows)
        except Exception as e:
            print(f"⚠️ 现取报价失败，改用最近盯市：{type(e).__name__}: {e}", file=sys.stderr)
            partial.append(f"live_marks_failed: {type(e).__name__}")
    now = datetime.now(ET)
    day = now.date().isoformat()
    sh = shadow_summary()
    if sh and sh[0].startswith(("⚠️", "（shadow report")):
        partial.append("shadow_summary_incomplete")
    md, html = render(day, c, sm, live, now.strftime("%Y-%m-%d %H:%M ET") + ("（含现取估值）" if live else ""),
                      legacy=load_legacy(), shadow=sh)
    ver = hashlib.sha256(md.encode("utf-8")).hexdigest()[:12]          # md 与 html 共用同一版本号
    stamp = f"\n\n> 版本 {ver}" + (f"；⚠️ 局部不完整：{', '.join(partial)}" if partial else "")
    md += stamp
    html = html.replace("</body>", f'<div class="sub">版本 {ver}' + (f"；⚠️ 局部不完整：{_esc(', '.join(partial))}" if partial else "")
                        + "</div></body>")
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in ((f"paper_{day}.md", md), (f"paper_{day}.html", html)):
        _atomic_write(OUT / name, body)        # 失败则保留上一份（原子替换），不会留下半截文件
    status = {"schema": 1, "day": day, "version": ver, "overall": "partial" if partial else "complete",
              "partial": partial, "generated_at": now.isoformat()}
    _atomic_write(OUT / ".status_paper_book.json", json.dumps(status, ensure_ascii=False))
    print(md)
    print(f"→ {(OUT / f'paper_{day}.html').resolve()}（{status['overall']}）", file=sys.stderr)
    return 3 if partial else 0


def _atomic_write(path: Path, body: str) -> None:
    """同目录临时文件 + fsync + 回读比对文本 + 原子替换（Codex 032 R7）。"""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body); f.flush(); os.fsync(f.fileno())
    if Path(name).read_text("utf-8") != body:
        os.unlink(name)
        raise RuntimeError(f"{path.name} 回读校验失败，保留上一份")
    os.replace(name, path)


if __name__ == "__main__":
    sys.exit(main())
