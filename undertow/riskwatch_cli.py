"""盘中持仓风险巡检命令（risk-watch-v1；用户 2026-10-15「有需要提示的再通知我，正常情况不用提示」）。只读，从不下单。

  undertow riskwatch run [--result-file F] [--status-file S --run-id R]   # 巡检一次：实盘 + 模拟仓 + 账户；新告警入私有发件箱
  undertow riskwatch notify-text F                                        # 调度用：ok / alert / crashed + 标题 + 正文
  undertow riskwatch deliver F                                            # 待投递告警写入私有收件箱（fsync）后 ack；失败 rc=1、下轮重发
  undertow riskwatch keys                                                 # 列出当前实盘价差的计划 key（写计划用）
  undertow riskwatch plan-show
  undertow riskwatch plan-set --key K [--cost 1.20 --take-profit-value 2.40 --stop-value …
                              --inv-ref BZ=F --inv-op "<" --inv-level 100 --inv-equiv 147 --inv-basis close
                              --review-date 2026-11-13 --note "…"]

私有文件（data/account/ 已 gitignore，含仓位与金额，绝不入库）：
  data/account/position_plans.json        持仓计划（schema position-plans-v1）
  data/account/riskwatch/state.json       连续缺失计数（只由 run 在锁内写）
  data/account/riskwatch/outbox.jsonl     发件箱：入箱的告警（只追加，alert_id 去重；持久的「待投递」）
  data/account/riskwatch/acks.jsonl       确认账：已落进收件箱的 alert_id（只追加）
  data/account/riskwatch/YYYY-MM-DD.jsonl 每轮巡检全部条目与覆盖状态（含没有新告警的轮次的摘要）
  data/account/riskwatch/inbox_YYYY-MM-DD.log  已送达告警原文（deliver 追加 + fsync）
调度：scripts/session_hooks.sh 的 ⑯（交易日 ET 09:45 起每 30 分钟一个桶，到收盘前 15 分钟）。

投递闭环（Codex 052 I1，复用模拟仓 048 D 的发件箱 pending/ack 设计）：
  run：巡检 → 新告警【先入发件箱】（fsync）→ 结果文件 = 发件箱里全部未确认项（含此前没送达、本轮条件已消失的）。
  deliver：锁内只取尚未确认的项 → 收件箱追加 + fsync 成功 → 才写确认账。收件箱写失败（即使弹窗成功）→ 不确认，rc=1。
  run 与 deliver 共用 RW_DIR/.lock（flock）：run 拿不到锁 = busy（rc 75，不写状态文件）；deliver 等锁至多 120 秒。
  发件箱/确认账只追加，run 不再整文件覆盖任何 deliver 写的东西 —— 旧快照覆盖新 ack 的路径不存在。
  跨日：pending 不分日期，昨天没送达的今天照样投递；「同 key 每日只入箱一次」只看当日入箱行。

估值口径与模拟仓同一处：scripts/paper_trades.py 的 close_value（卖腿 ask − 买腿 bid；买腿缺 bid 按 0 计并标估值假设；
腿出错/交叉/非有限 → 不可估值）；经济量 spread_econ.vertical。
实盘成本（Codex 052 I2）：计划里的 cost_per_share = 用户核定的每股净额【绝对值】，符号由行权价结构决定（借记 = 付、贷记 = 收），
可选 cost_side 校验；没有计划成本才用券商成本价推算，并标「估算、未核定」（部分减仓后会被改写，见 AGENTS）；两者都没有 = 成本缺失。
费用：计划 fee_round_trip 0 合法（不再被 `or 默认费` 改成默认值）；缺省才用 _fee_rt() 模型费。
覆盖：每个仓位逐规则 checked / missing / not_applicable 写进私有轮次日志；核心缺口（标的价、参照价且无显式等价位、成本、费用、日历）
→ 本轮 incomplete（status 文件 overall=partial，调度不写成功哨兵）；连续 ≥2 轮 → 「巡检不完整」提醒。
临收盘窗口：market_calendar.close_time 当日收市（半日市 13:00）前 RISK_WATCH_META.close_warn_minutes 分钟起。
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from undertow.analyze import risk_watch as rw

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
PLANS = ROOT / "data/account/position_plans.json"
RW_DIR = ROOT / "data/account/riskwatch"
RESULT_SCHEMA = "risk-watch-result-v2"          # v2：结果 = 发件箱全部未确认项（pending），不再只是本轮新增
ALERT_SCHEMA = "risk-watch-alert-v1"
BUSY_RC = 75                                      # 与 lockf -t 0 撞锁同码：run_bound 据此判 busy
FEE_ROUND_TRIP_PER_GROUP = None   # 由 healthcheck.FEE_PER_CONTRACT 推出（2 腿 × 2 笔），见 _fee_rt
_OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+)(?:\.[A-Z]{1,4})?$")


def _paper():
    """模拟仓模块（scripts/，非包）：估值与仓位口径复用它，不另写一套。"""
    spec = importlib.util.spec_from_file_location("paper_trades", ROOT / "scripts/paper_trades.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fee_rt() -> float:
    from undertow.analyze.healthcheck import FEE_PER_CONTRACT
    return round(FEE_PER_CONTRACT * 2 * 2, 2)              # 每组：2 腿 × 开平 2 笔（模型假设，指派处置费用未知）


def _now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def _atomic(path: Path, obj) -> None:
    from undertow.collect.asof_history import atomic_write_json
    atomic_write_json(path, obj)


def _isolate(path: Path) -> None:
    try:
        path.replace(path.with_name(f"{path.name}.corrupt-{_now_et():%Y%m%dT%H%M%S}-{os.getpid()}"))
    except OSError:
        pass


def load_plans(path: Path | None = None) -> tuple[dict, str | None]:
    """→ (plans, problem)。不存在 = 没有计划（合法）；损坏/不合格 = problem，plans 为空（调用方报告，不静默）。
    path 默认在【调用时】取模块的 PLANS（Codex 052：原先 `path=PLANS` 在定义时绑定私有路径，测试只 patch PLANS 仍会读真实计划）。"""
    path = PLANS if path is None else path
    if not path.exists():
        return {}, None
    try:
        obj = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        return {}, f"计划文件读不了：{type(e).__name__}"
    bad = rw.validate_plans(obj)
    return ({}, bad) if bad else (obj["plans"], None)


def load_state(today: str) -> dict:
    f = RW_DIR / "state.json"
    if not f.exists():
        return rw.new_state(today)
    try:
        return rw.roll_state(json.loads(f.read_text("utf-8")), today)
    except (OSError, ValueError):
        _isolate(f)                                         # 损坏隔离保留（AGENTS 失败与缺失）；最坏是当天重复提醒一次
        return rw.new_state(today)


# —— 发件箱 / 确认账（私有，只追加）——

class OutboxCorrupt(RuntimeError):
    pass


def _outbox() -> Path:
    return RW_DIR / "outbox.jsonl"


def _acks() -> Path:
    return RW_DIR / "acks.jsonl"


@contextmanager
def _rw_lock(wait_s: float):
    """RW_DIR/.lock 上的 flock。wait_s=0 → 拿不到立即给 False；否则轮询至多 wait_s 秒。进程死亡内核即释放。"""
    RW_DIR.mkdir(parents=True, exist_ok=True)
    with open(RW_DIR / ".lock", "a+") as lk:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def _jsonl(path: Path, key: str) -> list[dict]:
    """逐行解析；任一行坏 → OutboxCorrupt（发件箱/确认账损坏必须显式失败，不静默漏发或重发）。"""
    rows = []
    if path.exists():
        for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if line.strip():
                try:
                    r = json.loads(line)
                    if not isinstance(r, dict) or not isinstance(r[key], str):
                        raise TypeError(key)
                except (ValueError, KeyError, TypeError) as e:
                    raise OutboxCorrupt(f"{path.name} 第 {n} 行损坏：{type(e).__name__}")
                rows.append(r)
    return rows


def _append_rows(path: Path, rows: list[dict]) -> None:
    """只追加 + fsync（文件与父目录）；任一步失败抛 OSError（调用方不得据此确认）。"""
    if not rows:
        return
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def pending_alerts() -> list[dict]:
    """发件箱里尚未确认的告警（按入箱顺序；同一 alert_id 只取第一条）。与本轮条件是否仍存在无关。"""
    acked = {r["alert_id"] for r in _jsonl(_acks(), "alert_id")}
    out, seen = [], set()
    for r in _jsonl(_outbox(), "alert_id"):
        if r["alert_id"] in acked or r["alert_id"] in seen:
            continue
        seen.add(r["alert_id"])
        out.append(r)
    return out


def parse_occ(sym: str):
    m = _OCC.match(sym.upper())
    if not m:
        return None
    root, yy, mm, dd, cp, k = m.groups()
    return root, f"20{yy}-{mm}-{dd}", cp, int(k) / 1000.0


def live_key(root, expiry, side, k_sell, k_buy) -> str:
    return f"live|{root}|{expiry}|{side}|{k_sell:g}/{k_buy:g}"


def group_verticals(positions: list) -> tuple[list[dict], list[str]]:
    """实盘期权腿 → 垂直价差（同标的/到期/类型，一多一空、张数相等）。认不出的腿原样列出（巡检未覆盖，要报告）。"""
    buckets: dict[tuple, list] = {}
    odd = []
    for p in positions:
        o = parse_occ(p.symbol)
        if o is None or not p.quantity:
            if p.quantity:
                odd.append(p.symbol)
            continue
        buckets.setdefault((o[0], o[1], o[2]), []).append((p, o[3]))
    out = []
    for (root, exp, cp), legs in sorted(buckets.items()):
        longs = [x for x in legs if x[0].quantity > 0]
        shorts = [x for x in legs if x[0].quantity < 0]
        if len(longs) == 1 and len(shorts) == 1 and longs[0][0].quantity == -shorts[0][0].quantity \
                and float(longs[0][0].quantity).is_integer():
            (lp, kb), (sp, ks) = longs[0], shorts[0]
            bc = sp.cost_price - lp.cost_price if rw._fin(sp.cost_price, lp.cost_price) else None   # 缺成本价 → None，不折零
            out.append({"key": live_key(root, exp, cp, ks, kb), "root": root, "expiry": exp, "side": cp,
                        "k_sell": ks, "k_buy": kb, "sell": sp.symbol, "buy": lp.symbol, "qty": int(lp.quantity),
                        "broker_entry_credit": None if bc is None else round(bc, 4)})
        else:
            odd += [x[0].symbol for x in legs]
    return out, odd


def _unit_notes(live: list[dict], odd: list[str]) -> dict:
    """同标的还有其它在场价差或未配对腿 → 该标的是多腿整体风险单元（Codex 061/062），两腿成本不代表整轮成本（063 T3）。"""
    from collections import Counter
    n = Counter(v["root"] for v in live)
    for s in odd:
        o = parse_occ(s)
        if o:
            n[o[0]] += 1
    return {r: f"{r} 同时有 {c} 组/条在场价差或腿" for r, c in n.items() if c > 1}


def _quotes(symbols: list[str]) -> dict:
    """逐腿盘口（长桥 depth，经 lb_budget），模拟仓同一形态：{sym: {bid, ask, bid_size, ask_size, error}}。全部失败 → {}。"""
    from undertow.collect import longbridge_quote as lq
    try:
        d = lq.fetch_depth(sorted(set(symbols)))
    except lq.LiveQuotesUnavailable:
        return {}
    return {s: {"bid": x.bid, "ask": x.ask, "bid_size": x.bid_size, "ask_size": x.ask_size, "error": x.error or ""}
            for s, x in d.items()}


def _spots(roots: list[str]) -> dict:
    from undertow.collect import longbridge_quote as lq
    if not roots:
        return {}
    try:
        q = lq.fetch_stock_quotes([f"{r}.US" for r in sorted(set(roots))])
    except Exception:                                       # noqa: BLE001 —— 标的价缺失 → 卖腿/失效位规则跳过并在条目里体现
        return {}
    return {s.split(".")[0]: x.freshest for s, x in q.items() if x.freshest and x.freshest > 0}


def _yahoo(sym: str) -> float | None:
    """商品参照价（如 BZ=F）。可选：失败返回 None，调用方按计划里的标的等价位兜底。"""
    import urllib.parse
    import urllib.request
    try:
        u = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1m&range=1d"
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
            v = json.load(r)["chart"]["result"][0]["meta"].get("regularMarketPrice")
        return float(v) if isinstance(v, (int, float)) and v > 0 else None
    except Exception:                                       # noqa: BLE001
        return None


def _ref_prices(plans: dict, spots: dict) -> dict:
    out = {}
    for p in plans.values():
        inv = p.get("invalidation") or {}
        ref = inv.get("ref")
        if not ref or ref in out:
            continue
        out[ref] = spots.get(ref.split(".")[0]) if ref.endswith(".US") else _yahoo(ref)
    return out


def _tdays(today: date, expiry: str) -> int | None:
    from undertow.core import market_calendar as mc
    try:
        td = mc.trading_days(today, date.fromisoformat(expiry))
    except ValueError:
        return None
    return None if td is None else max(len(td) - 1, 0)


def _trial_view() -> dict | None:
    try:
        from undertow.collect import trial_store as ts
        from undertow.collect import longbridge_account as lb
        from undertow import trial_cli as tcli
        st, prob = ts.load()
        if prob:
            return {"state": "error", "note": str(prob)[:200]}
        if st is None:
            return {"state": "absent"}
        bad = tcli._check_identity(st)
        if bad:
            return {"state": "error", "note": bad}
        try:
            eq = lb.fetch_assets().net_assets
        except Exception:                                   # noqa: BLE001
            eq = None
        fill = tcli.fills_after(st["trade_coverage"]["through"])
        _, v, prob = tcli._evaluate_in_store(eq, eq is not None, datetime.now(timezone.utc).isoformat(), fill, False)
        if prob:
            return {"state": "error", "note": str(prob)[:200]}
        pv = v.get("pause")
        return {"state": "ok", "paused": None if pv is None else bool(pv.get("latched")),
                "consecutive_unknown": v.get("consecutive_losses") is None,
                "note": (v.get("consecutive_losses_note") or v.get("note") or "")[:200]}
    except Exception:                                       # noqa: BLE001 —— 读不到 = 未检查（info），不是通过
        return None


def _book_view() -> dict | None:
    try:
        from undertow.cli import _load_account_review
        b = _load_account_review(no_cache=False)
        if b.get("review") is None:
            return {"overall": "pass", "failed": []}
        r = b["risk"]
        return {"overall": r["overall"], "failed": [f"{c['item']} {c['status']}" for c in r["checks"]
                                                    if c["status"] != "pass"]}
    except Exception:                                       # noqa: BLE001
        return None


def collect_and_evaluate(now: datetime | None = None, *, deps: dict | None = None) -> dict:
    """一轮巡检（I/O 在 deps 里，测试可替换）。→ {items, problems, counts}。problems 非空 = 本轮检查不完整（rc=1）。"""
    d = {"positions": None, "assets": None, "quotes": _quotes, "spots": _spots, "ref_prices": _ref_prices,
         "trial": _trial_view, "book": _book_view, "paper": _paper, "plans": load_plans}
    d.update(deps or {})
    now = now or _now_et()
    today, hm = now.date().isoformat(), now.strftime("%H:%M")
    items, problems = [], []
    plans, pprob = d["plans"]()
    if pprob:   # 损坏 ≠ 没有计划（Codex 054 R1）：本轮记 problem（partial/rc1，不写成功桶哨兵、下次唤醒重试），其它规则照跑照投
        items.append(rw.RiskItem("plans_invalid", "warn", "account",
                                 "持仓计划文件不合格：计划止损/失效位/复核日本轮未决", pprob))
        problems.append(f"持仓计划文件不合格，计划规则未决：{pprob}")
    # 实盘
    from undertow.collect import longbridge_account as lb
    try:
        positions = d["positions"]() if d["positions"] else lb.fetch_positions()
    except Exception as e:                                  # noqa: BLE001
        positions = None
        problems.append(f"实盘持仓读不到：{type(e).__name__}")
    try:
        cash = (d["assets"]() if d["assets"] else lb.fetch_assets()).cash_by_ccy
    except Exception:                                       # noqa: BLE001
        cash = None
    live, odd = group_verticals(positions or [])
    unit_of = _unit_notes(live, odd)
    if odd:
        items.append(rw.RiskItem("live_unmapped|" + ",".join(sorted(odd)), "warn", "live",
                                 f"有 {len(odd)} 条实盘腿不是可识别的垂直价差：巡检未覆盖", "、".join(sorted(odd))[:200]))
    # 模拟仓
    pt, paper_rows = None, []
    try:
        pt = d["paper"]()
        j = json.loads(pt.JOURNAL.read_text("utf-8")) if pt.JOURNAL.exists() else {"theses": []}
        for t in j.get("theses", []):
            p = t.get("paper")
            if t.get("execution") == "模拟" and p and p.get("state") == "entered":
                paper_rows.append((t["id"], p))
    except Exception as e:                                  # noqa: BLE001
        problems.append(f"模拟仓读不到：{type(e).__name__}")
    syms = [x for v in live for x in (v["sell"], v["buy"])] + [x for _, p in paper_rows for x in (p["sell"], p["buy"])]
    roots = [v["root"] for v in live] + [p["underlying"].split(".")[0] for _, p in paper_rows]
    quotes = d["quotes"](syms) if syms else {}
    if syms and not quotes:
        problems.append("全部盘口取不到")
    spots = d["spots"](roots)
    refs = d["ref_prices"](plans, spots)
    state = load_state(today) if "state" not in d else d["state"]
    views = []
    close_value = pt.close_value if pt else None
    for v in live:
        plan = plans.get(v["key"]) or {}
        ec, src = live_entry_credit(v, plan)
        if src == "plan_conflict":
            items.append(rw.RiskItem(f"plan_cost_conflict|{v['key']}", "warn", "live",
                                     f"实盘 {v['root']} {v['expiry'][5:]}：计划登记的成本方向与行权价结构不符，未采用",
                                     "改用券商成本价估算（未核定）；请用 plan-set 更正 cost / cost_side"))
            ec, src = v["broker_entry_credit"], ("broker_estimate" if v["broker_entry_credit"] is not None else "unknown")
        cv = close_value(quotes, {"sell": v["sell"], "buy": v["buy"]}) if close_value else None
        key = v["key"]
        miss = rw.bump_missing(state, key, cv is None)
        views.append((rw.SpreadView(
            key=key, scope="live", label=f"实盘 {v['root']} {v['expiry'][5:]} {min(v['k_buy'], v['k_sell']):g}/{max(v['k_buy'], v['k_sell']):g}{v['side']}",
            underlying=v["root"], expiry=v["expiry"], side=v["side"], k_sell=v["k_sell"], k_buy=v["k_buy"],
            entry_credit=ec, qty=v["qty"],
            fee_round_trip=plan["fee_round_trip"] if plan.get("fee_round_trip") is not None else _fee_rt(),
            value=None if cv is None else cv[0], value_assumption=None if cv is None else cv[1],
            spot=spots.get(v["root"]), tdays_to_expiry=_tdays(now.date(), v["expiry"]), missing_streak=miss,
            cost_source=src, unit_note=unit_of.get(v["root"])), plan))
    for tid, p in paper_rows:
        root = p["underlying"].split(".")[0]
        cv = close_value(quotes, p) if close_value else None
        key = f"paper|{tid}"
        miss = rw.bump_missing(state, key, cv is None)
        views.append((rw.SpreadView(
            key=key, scope="paper", label=f"模拟 {root} {str(p['expiry'])[5:]} {min(p['k_buy'], p['k_sell']):g}/{max(p['k_buy'], p['k_sell']):g}{p['side']}",
            underlying=root, expiry=str(p["expiry"]), side=p["side"], k_sell=p["k_sell"], k_buy=p["k_buy"],
            entry_credit=p.get("entry_credit"), qty=p["qty"], fee_round_trip=p.get("fee_round_trip"),
            value=None if cv is None else cv[0], value_assumption=None if cv is None else cv[1], spot=spots.get(root),
            tdays_to_expiry=_tdays(now.date(), str(p["expiry"])), stop_value=p.get("stop_value"),
            missing_streak=miss, cost_source="paper_fill", advisory_spec=dict(p)), plans.get(key)))
    from undertow.core import market_calendar as mc
    close_hm = mc.close_time(now.date())
    coverage, incomplete = [], []
    for v, plan in views:
        its, cov = rw.assess_spread(v, plan, today=today, now_hm=hm, ref_prices=refs, close_hm=close_hm, plan_problem=pprob,
                                    at=now)
        items += its
        gaps = rw.core_gaps(cov, quote_streak=v.missing_streak)
        alert_gaps = rw.core_gaps(cov, inputs=rw.GAP_ALERT_INPUTS)
        n = rw.bump_missing(state, f"cov|{v.key}", bool(alert_gaps))
        if alert_gaps and n >= rw.RISK_WATCH_META["quote_missing_runs"]:
            items.append(rw.coverage_item(v, alert_gaps, n))
        incomplete += [f"{v.label}：{g}" for g in gaps]
        coverage.append(cov)
    # 持仓读不到 → 账本未知（不是「没有持仓所以通过」）；读到且没有价差 → 空账本通过
    book = None if positions is None else (d["book"]() if positions else {"overall": "pass", "failed": []})
    items += rw.evaluate_account(cash_by_ccy=cash, trial=d["trial"](), book=book)
    return {"items": items, "problems": problems, "state": state, "today": today, "hm": hm,
            "incomplete": incomplete, "coverage": coverage,
            "counts": {"live": len(live), "paper": len(paper_rows), "legs": len(syms), "quoted": len(quotes)}}


def live_entry_credit(v: dict, plan: dict) -> tuple[float | None, str]:
    """实盘入场净额（每股，带符号：贷记 > 0、借记 < 0）与来源。计划 cost_per_share 是绝对值，符号由行权价结构定；
    计划写了 cost_side 且与结构不符 → ("plan_conflict")。没有计划成本 → 券商成本推算（估算）；都没有 → (None, "unknown")。"""
    from undertow.analyze import spread_econ as se
    debit = se.is_debit(v["side"], v["k_sell"], v["k_buy"])
    c = plan.get("cost_per_share")
    if c is not None:
        if plan.get("cost_side") is not None and plan["cost_side"] != ("debit" if debit else "credit"):
            return None, "plan_conflict"
        return (-c if debit else c), "plan"
    bc = v.get("broker_entry_credit")
    return (bc, "broker_estimate") if bc is not None else (None, "unknown")


def _enqueue(r: dict, problems: list) -> list[dict]:
    """新告警入发件箱（锁内调用）。发件箱损坏 → 隔离保留、换新箱并报 problem（宁可当天重复一次，不漏、不覆盖）。
    返回本轮新入箱的行。入箱写失败 → problem，新告警仍随结果给出（标 persisted=False），deliver 照样投递但无法确认。"""
    today, items = r["today"], r["items"]
    try:
        rows = _jsonl(_outbox(), "alert_id")
    except OutboxCorrupt as e:
        _isolate(_outbox())
        problems.append(f"发件箱损坏已隔离：{e}")
        items.append(rw.RiskItem("riskwatch_outbox_corrupt", "warn", "account",
                                 "风险巡检发件箱损坏：已隔离保留，换新箱；此前未送达的提醒可能需要人工看隔离文件",
                                 str(e)[:200]))
        rows = []
    new = rw.select_new(items, rw.enqueued_map(rows, today))
    at = datetime.now(timezone.utc).isoformat()
    fresh = [{"schema": ALERT_SCHEMA, "alert_id": rw.alert_id(today, i), "date": today, "at_et": r["hm"], "enqueued_at": at,
              **rw.as_dicts([i])[0]} for i in new]
    try:
        _append_rows(_outbox(), fresh)
    except OSError as e:
        problems.append(f"发件箱写入失败：{type(e).__name__}")
        for x in fresh:
            x["persisted"] = False
    return fresh


def cmd_run(args) -> int:
    with _rw_lock(0) as got:
        if not got:
            print("风险巡检：另一轮巡检/投递正持锁，本次跳过（busy）", file=sys.stderr)
            return BUSY_RC
        r = collect_and_evaluate()
        problems = list(r["problems"])
        fresh = _enqueue(r, problems)
        try:
            pend = pending_alerts()
        except OutboxCorrupt as e:                          # 确认账坏：隔离保留后按「全未确认」重发（宁重不漏）
            _isolate(_acks())
            problems.append(f"确认账损坏已隔离：{e}")
            pend = pending_alerts()
        known = {p["alert_id"] for p in pend}
        pend += [x for x in fresh if x.get("persisted") is False and x["alert_id"] not in known]
        state, items = r["state"], r["items"]
        incomplete = r.get("incomplete") or []
        res = {"schema": RESULT_SCHEMA, "version": rw.RISK_WATCH_META["version"], "date": r["today"], "at": r["hm"],
               "status": "alert" if pend else "ok", "pending": pend, "new_ids": [x["alert_id"] for x in fresh],
               "digest": rw.digest([x["alert_id"] for x in pend]), "n_all": len(items),
               "problems": problems, "incomplete": incomplete, "counts": r["counts"]}
        _atomic(RW_DIR / "state.json", state)              # 只有连续缺失计数；送达状态在只追加的确认账里
        with open(RW_DIR / f"{r['today']}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "all": rw.as_dicts(items),
                                "new_ids": res["new_ids"], "pending": len(pend), "problems": problems,
                                "incomplete": incomplete, "coverage": r.get("coverage") or [], "counts": r["counts"]},
                               ensure_ascii=False) + "\n")
        if args.result_file:
            _atomic(Path(args.result_file), res)
    bad = problems or incomplete
    overall = "failed" if problems and not r["counts"]["quoted"] and r["counts"]["legs"] else ("partial" if bad else "complete")
    if getattr(args, "status_file", None):
        _atomic(Path(args.status_file), {"schema": 2, "command": "riskwatch run", "run_id": getattr(args, "run_id", None),
                                         "session": r["today"], "overall": overall, "at": r["hm"],
                                         "counts": {"new": len(fresh), "pending": len(pend), "all": len(items),
                                                    "incomplete": len(incomplete)}})
    for pr in problems:
        print(f"⚠️ 风险巡检：{pr}", file=sys.stderr)
    for x in incomplete:
        print(f"⚠️ 风险巡检不完整：{x}", file=sys.stderr)
    if pend:
        print(f"风险巡检：待投递提醒 {len(pend)} 条（本轮新入箱 {len(fresh)}）")
        for i in pend:
            print(f"- [{i['severity']}] {i['title']}")
    else:
        print("风险巡检：无需提醒" + ("（但本轮检查不完整，见上）" if bad else ""))
    return 0 if overall == "complete" else 1


def notify_text(res: dict) -> tuple[str, str]:
    """通知标题与正文（调度脚本用；只在本机弹窗，不入库）。内容 = 全部待投递项（含此前没送达的）。"""
    new = res.get("pending") or []
    worst = max((rw.SEVERITY_RANK[i["severity"]] for i in new), default=0)
    title = {3: "🔴 持仓风险", 2: "⚠️ 持仓提醒", 1: "ℹ️ 持仓提醒"}.get(worst, "持仓提醒") + f" {len(new)} 条"
    body = "；".join(i["title"] for i in sorted(new, key=lambda x: -rw.SEVERITY_RANK[x["severity"]]))

    def clean(x: str) -> str:                               # osascript 字符串里不能有双引号/反斜杠/换行
        return x.replace('"', "'").replace("\\", "/").replace("\n", " ")
    return clean(title), clean(body)[:220]


def _load_result(path) -> dict:
    """typed 结果校验：缺失/损坏/schema 不对/pending 不是列表或条目缺字段 → ValueError（调度判 crashed）。"""
    try:
        res = json.loads(Path(path).read_text("utf-8"))
    except OSError as e:
        raise ValueError(f"读不到：{type(e).__name__}")
    if not isinstance(res, dict) or res.get("schema") != RESULT_SCHEMA or res.get("status") not in ("ok", "alert") \
            or not isinstance(res.get("pending"), list) or not isinstance(res.get("date"), str):
        raise ValueError("schema")
    for i in res["pending"]:
        if not isinstance(i, dict) or not all(isinstance(i.get(k), str) for k in ("alert_id", "key", "severity", "title")) \
                or i["severity"] not in rw.SEVERITY_RANK:
            raise ValueError("pending 条目不合格")
    if (res["status"] == "alert") != bool(res["pending"]):
        raise ValueError("status 与 pending 不一致")
    return res


def cmd_notify_text(args) -> int:
    try:
        res = _load_result(args.result)
    except ValueError:
        print("crashed"); print("结果文件缺失或损坏"); return 1
    t, b = notify_text(res)
    print(res["status"]); print(t); print(b.replace("\n", " "))
    return 0


def cmd_deliver(args) -> int:
    """待投递告警 → 私有收件箱追加 + fsync 成功后才写确认账（「送达」= 收件箱落盘；弹窗只是尽力而为）。
    锁内重新核对确认账：结果文件是旧快照也只投尚未确认的（重试不重复写收件箱、不重复确认）。失败 rc=1，下轮重发。"""
    try:
        res = _load_result(args.result)
    except ValueError as e:
        print(f"送达失败（结果文件不合格，下轮重发）：{e}", file=sys.stderr)
        return 1
    if not res["pending"]:
        print(0)
        return 0
    try:
        with _rw_lock(120) as got:
            if not got:
                raise OSError("锁等待超时")
            acked = {r["alert_id"] for r in _jsonl(_acks(), "alert_id")}
            known = {r["alert_id"] for r in _jsonl(_outbox(), "alert_id")}
            todo = [i for i in res["pending"] if i["alert_id"] not in acked]
            if todo:
                lines = [f"{i.get('date', res['date'])} {i.get('at_et', res.get('at', ''))} ET [{i['severity']}] {i['title']} —— "
                         f"{i.get('detail', '')}（{i['key']}）" for i in todo]
                _paper().fallback_append(RW_DIR / f"inbox_{res['date']}.log",
                                         "\n".join(lines) + f"\n（批次 {rw.digest([i['alert_id'] for i in todo])}，"
                                                             f"risk-watch {res.get('version')}）")
                at = datetime.now(timezone.utc).isoformat()
                _append_rows(_acks(), [{"alert_id": i["alert_id"], "acked_at": at, "via": "inbox"}
                                       for i in todo if i["alert_id"] in known])
    except (OSError, OutboxCorrupt) as e:
        print(f"送达失败（未确认，下轮重发）：{type(e).__name__}", file=sys.stderr)
        return 1
    print(len(todo))
    return 0


def cmd_keys(args) -> int:
    from undertow.collect import longbridge_account as lb
    live, odd = group_verticals(lb.fetch_positions())
    for v in live:
        print(f"{v['key']}  （{v['qty']} 组，券商成本推算净 {v['broker_entry_credit']:+.2f}/股）")
    for s in odd:
        print(f"未识别腿：{s}")
    return 0


def cmd_plan_show(args) -> int:
    plans, prob = load_plans()
    if prob:
        print(f"⚠️ {prob}"); return 1
    print(json.dumps(plans, ensure_ascii=False, indent=1) if plans else "（没有持仓计划）")
    return 0


def cmd_plan_set(args) -> int:
    obj = {"schema": rw.PLAN_SCHEMA, "plans": {}}
    if PLANS.exists():
        plans, prob = load_plans()
        if prob:
            print(f"⚠️ 现有计划文件不合格，拒绝覆盖：{prob}"); return 1
        obj["plans"] = plans
    p = dict(obj["plans"].get(args.key) or {})
    for f, a in (("cost_per_share", "cost"), ("take_profit_value", "take_profit_value"), ("stop_value", "stop_value"),
                 ("review_date", "review_date"), ("review_note", "review_note"), ("note", "note"),
                 ("fee_round_trip", "fee_round_trip"), ("cost_side", "cost_side"), ("entry_fees_usd", "entry_fees"),
                 ("qty_entered", "qty_entered")):
        if getattr(args, a, None) is not None:
            p[f] = getattr(args, a)
    if args.inv_ref:
        p["invalidation"] = {"ref": args.inv_ref, "op": args.inv_op, "level": args.inv_level,
                             "basis": args.inv_basis, "underlying_equiv": args.inv_equiv, "note": args.inv_note}
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    obj["plans"][args.key] = p
    bad = rw.validate_plans(obj)
    if bad:
        print(f"⚠️ 计划不合格，未写入：{bad}"); return 1
    _atomic(PLANS, obj)
    print(f"已写入计划 {args.key}（私有 {PLANS}）")
    return 0


def register(sub):
    p = sub.add_parser("riskwatch", help="盘中持仓风险巡检（实盘+模拟仓；有需要才提醒；只读，从不下单）")
    ss = p.add_subparsers(dest="riskwatch_cmd", required=True)
    r = ss.add_parser("run"); r.add_argument("--result-file"); r.add_argument("--status-file"); r.add_argument("--run-id")
    r.set_defaults(func=cmd_run)
    d = ss.add_parser("deliver"); d.add_argument("result"); d.set_defaults(func=cmd_deliver)
    n = ss.add_parser("notify-text"); n.add_argument("result"); n.set_defaults(func=cmd_notify_text)
    ss.add_parser("keys").set_defaults(func=cmd_keys)
    ss.add_parser("plan-show").set_defaults(func=cmd_plan_show)
    s = ss.add_parser("plan-set")
    s.add_argument("--key", required=True)
    s.add_argument("--cost", type=float, help="用户核定的每股净额（绝对值；借记 = 实付、贷记 = 实收，方向由行权价结构决定）")
    s.add_argument("--cost-side", choices=("debit", "credit"), help="可选：成本方向，与行权价结构不符时巡检不采用")
    s.add_argument("--take-profit-value", type=float); s.add_argument("--stop-value", type=float)
    s.add_argument("--fee-round-trip", type=float, help="每组往返费用（美元）；0 合法，负数/非有限拒绝")
    s.add_argument("--entry-fees", type=float, help="这批实际入场总费用（美元，按现金流水核定）；100%% 含费提醒的分母用它，缺省按模型估计")
    s.add_argument("--qty-entered", type=int,
                   help="这批实际入场组数（按成交核定）；与当前组数不同 → 整轮 ROI 未核实、入场费按比例分摊为估计；不填不默认为当前组数")
    s.add_argument("--inv-ref"); s.add_argument("--inv-op", choices=rw.INVALIDATION_OPS, default="<")
    s.add_argument("--inv-level", type=float); s.add_argument("--inv-equiv", type=float)
    s.add_argument("--inv-basis", choices=("close", "intraday"), default="close"); s.add_argument("--inv-note")
    s.add_argument("--review-date"); s.add_argument("--review-note"); s.add_argument("--note")
    s.set_defaults(func=cmd_plan_set)
