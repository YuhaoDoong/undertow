"""`undertow shadow ...` 编排（W05）：唯一把 collect 与 analyze.shadow 接起来的地方。

  capture  盘前：冻结当日机会（A/B1/B2 × P/C），无候选也记原因。--as-of 回放写 replay/。
  quote    盘中两个时点（--window open ET 10:00 / close ET 15:30）：开盘窗做入场、已触发退出、持仓标记；
           收盘窗只做持仓标记（止损口径用）。非盘中默认拒绝。
  settle   收盘后：认证 session、监控收盘触发、到期结算（三种突破 × 多种损益口径）。
  report   配对统计：A 绝对收益、A−B 差，按日期分块区间；披露覆盖与身份构成。

只读：只调用长桥 depth/quote 与公开数据；从不下单、不改券商侧任何东西。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from undertow.analyze import shadow as sh
from undertow.collect import jsonl_ledger as jl
from undertow.core.clock import ET, is_before_open, market_today

DIR = Path("data/history/shadow")
KEY = "key"


def _vdir(replay: bool) -> Path:
    """账本按配置版本分目录：不同版本的样本绝不混入同一主检验（v1 只有回放行，留在原位作历史）。"""
    d = DIR / sh.CONFIG["version"]
    return d / "replay" if replay else d


def _path(inst, replay):
    return _vdir(replay) / f"{inst}.jsonl"


def _instruments(cfg, names):
    """默认 = CONFIG 冻结的品种池（S00），不再随注册表变化。"""
    keys = names or list(sh.CONFIG["instruments"])
    return [cfg.get(k) for k in keys]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _phase_now():
    t = datetime.now(ET)
    if t.weekday() >= 5:
        return "off_hours"
    m = t.hour * 60 + t.minute
    return "rth" if 9 * 60 + 30 <= m < 16 * 60 else "off_hours"


def cmd_capture(args) -> int:
    from undertow.analyze.gamma import local_wall
    from undertow.analyze.spread_ledger import decision_context
    from undertow.analyze.stretch import _atr_series
    from undertow.cli import _certify_report_session, _sess_meta_row
    from undertow.collect.cboe_history import CboeHistorySource
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    from undertow.core.calendar import load_events
    from undertow.core.config import load_config
    cfg = load_config(); store = SnapshotStore(); px = CboeHistorySource()
    replay = bool(args.as_of)
    today = date.fromisoformat(args.as_of) if replay else market_today()
    events = load_events()
    issues, done = [], []
    for inst in _instruments(cfg, args.instruments):
        sym = inst.options.symbol
        try:
            ds = [d for d in store.dates("options", sym) if d <= today]
            if not ds:
                raise ValueError("无快照")
            fd = ds[-1]
            snap = snapshot_from_payload(store.load("options", sym, fd), inst.key, sym)
            meta = _certify_report_session(store, px, inst, fd.isoformat(), today, replay=replay, no_cache=False)
            if meta["status"] == "unmappable":
                raise ValueError(f"session 无法认证（{meta['source']}）")
            T = meta["session"]
            if replay and T != today:
                raise ValueError(f"回放日 {today} 的最新快照映射到 {T}，不是该日机会")
            ser = px.fetch_series(inst)
            idx = [i for i, d in enumerate(ser.dates) if d < T]
            if not idx:
                raise ValueError("拿不到 T 之前的收盘")
            i = idx[-1]
            atr = _atr_series(ser.highs[:i + 1], ser.lows[:i + 1], ser.closes[:i + 1], 14)[-1]
            ctx = decision_context(ser.highs, ser.lows, ser.closes, ser.dates, T)
            flow = None
            try:
                led = {r["date"]: r for r in json.load(open(f"data/history/signals/{inst.key}.json"))}
                fr = led.get(fd.isoformat())
                flow = {"call_direction": fr.get("call_direction"), "source_snapshot_date": fd.isoformat()} if fr else None
            except FileNotFoundError:
                pass
            exp = sh.target_expiry(snap, T, tuple(sh.CONFIG["dte"]))
            evs = [{"date": e.date.isoformat(), "name": e.name, "importance": e.importance}
                   for e in events if exp and T <= e.date <= exp and inst.key in (e.instruments or ())
                   and str(e.importance).lower() == "high"]
            wcfg = sh.CONFIG["wall"]
            row = sh.build_opportunity(
                inst=inst.key, sym=sym, snap=snap, session=T, spot=ser.closes[i], atr=atr,
                wall_fn=lambda k: local_wall(snap, T, ser.closes[i], k, band=wcfg["band"], hi_dte=wcfg["hi_dte"], mode="max"),
                labels={"base_date": ser.dates[i].isoformat(), "atr_expand_5": ctx.get("atr_expand_5"),
                        "atr_pct_250": ctx.get("atr_pct_250"), "flow": flow, "events_in_window": evs,
                        "events_note": "日历来源 config/calendar.json；事前可知性未逐条认证"},
                identity={"mode": "replay" if replay else "prospective", **_sess_meta_row(meta, fd.isoformat())})
            row["recorded_at"] = _now_iso()
            r = jl.insert_frozen(_path(inst.key, replay), row, key_field=KEY, frozen=sh.frozen_part)
            n_c = sum(l["status"] == "candidate" for l in row["legs"])
            done.append(inst.key)
            print(f"  {inst.key:7s} {T} [{meta['status']}] 候选腿 {n_c}/6  {r}")
        except Exception as e:
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]})
            print(f"  ⚠️ {inst.key}: {type(e).__name__}: {e}", file=sys.stderr)
    _status(args, "capture", done, issues)
    return 0 if done else 1


def _window_now(window: str) -> bool:
    lo, hi = sh.CONFIG["quote"]["windows"][window]
    t = datetime.now(ET); m = t.hour * 60 + t.minute
    h1, m1 = map(int, lo.split(":")); h2, m2 = map(int, hi.split(":"))
    return t.weekday() < 5 and h1 * 60 + m1 <= m <= h2 * 60 + m2


def cmd_quote(args) -> int:
    """在一个窗口里追加原始盘口尝试。窗口内可多次运行：只重抓尚无有效报价的腿（R02）。

    结构化状态（--status-file）：complete=所有应有腿都有有效报价 / partial / failed / unchanged（无应有任务）。
    调度层只在 complete 或 unchanged 时写成功哨兵；partial/failed 下一次唤醒继续重试。
    """
    from undertow.collect.longbridge_options import _lb_symbol
    from undertow.collect.longbridge_quote import fetch_depth, fetch_stock_quotes
    from undertow.core.config import load_config
    phase = _phase_now()
    window = args.window
    if not args.allow_off_hours and (phase != "rth" or not _window_now(window)):
        print(f"不在 {window} 窗口 {sh.CONFIG['quote']['windows'][window]}（ET）或非盘中；"
              "加 --allow-off-hours 可记录（phase 如实标注，不计为有效报价）。", file=sys.stderr)
        return 2
    cfg = load_config(); today = market_today(); wkey = f"{today.isoformat()}|{window}"
    counts = {"expected_legs": 0, "valid_legs": 0, "rows": 0}
    issues, done = [], []

    def expected_today(r, l):
        if l["status"] != "candidate" or r["outcome"] is not None:
            return False
        if window == "open" and r["session"] == today.isoformat():
            return True                                     # 入场
        return r["session"] <= today.isoformat() <= l["expiry"]   # 持仓标记

    def purpose(r):
        return "entry" if (window == "open" and r["session"] == today.isoformat()) else "exit"

    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key, False)
        if not p.exists():
            continue
        root = inst.options.symbol
        need, pending = {}, []
        for r in jl.load(p, KEY):
            for l in r["legs"]:
                if not expected_today(r, l):
                    continue
                counts["expected_legs"] += 1
                if sh.window_leg(r, l, wkey, purpose(r))["status"] == "valid":
                    counts["valid_legs"] += 1
                    continue
                pending.append((r["key"], l["leg_id"]))
                for K in (l["sell"], l["buy"]):
                    need[sh.qkey(l["side"], K), l["expiry"]] = _lb_symbol(root, date.fromisoformat(l["expiry"]), l["side"], K)
        if not need:
            continue
        t0 = _now_iso()
        try:
            dep = fetch_depth(sorted(set(need.values())))
        except Exception as e:
            dep = {}
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]})
        try:
            uq = fetch_stock_quotes([f"{root}.US"]).get(f"{root}.US")
            under = {"freshest": uq.freshest, "kind": uq.freshest_kind} if uq else None
        except Exception as e:
            under = {"error": f"{type(e).__name__}"}
        t1 = _now_iso()

        def fn(r):
            quotes = {}
            for l in r["legs"]:
                if l["status"] != "candidate":
                    continue
                for K in (l["sell"], l["buy"]):
                    sym = need.get((sh.qkey(l["side"], K), l["expiry"]))
                    if sym is None:
                        continue
                    d = dep.get(sym)
                    quotes[sh.qkey(l["side"], K)] = (
                        {"bid": d.bid, "ask": d.ask, "bid_size": d.bid_size, "ask_size": d.ask_size,
                         "error": d.error or None, "quote_time": None} if d is not None
                        else {"bid": None, "ask": None, "bid_size": 0, "ask_size": 0,
                              "error": "not_returned", "quote_time": None})
            if not quotes:
                return False
            w = r.setdefault("windows", {}).setdefault(wkey, {"attempts": []})
            w["attempts"].append({"started_at": t0, "ended_at": t1, "phase": phase,
                                  "underlying": under, "quotes": quotes})
            return True
        n = jl.update(p, fn, key_field=KEY, frozen=sh.frozen_part)
        counts["rows"] += n
        for r in jl.load(p, KEY):                             # 回读后重新计有效腿
            for l in r["legs"]:
                if (r["key"], l["leg_id"]) in pending and sh.window_leg(r, l, wkey, purpose(r))["status"] == "valid":
                    counts["valid_legs"] += 1
        done.append(inst.key)
        print(f"  {inst.key:7s} 追加 {n} 行尝试（{len(set(need.values()))} 个合约，{wkey}，phase={phase}）")
    if counts["expected_legs"] == 0:
        overall = "unchanged"
    elif counts["valid_legs"] == counts["expected_legs"]:
        overall = "complete"
    elif counts["valid_legs"] > 0:
        overall = "partial"
    else:
        overall = "failed"
    print(f"  {wkey}：应有 {counts['expected_legs']} 条腿，有效 {counts['valid_legs']} → {overall}")
    _status(args, f"quote-{window}", done, issues, overall=overall, counts=counts)
    return 0 if overall in ("complete", "unchanged") else 1


def cmd_settle(args) -> int:
    from undertow.collect.cboe_history import CboeHistorySource
    from undertow.core.config import load_config
    cfg = load_config(); px = CboeHistorySource(); issues, done = [], []
    for inst in _instruments(cfg, args.instruments):
        for replay in (False, True):
            p = _path(inst.key, replay)
            if not p.exists():
                continue
            try:
                ser = px.fetch_series(inst)
            except Exception as e:
                issues.append({"instrument": inst.key, "error": str(e)[:200]}); continue
            bars = list(zip(ser.dates, ser.highs, ser.lows, ser.closes))
            tdays = set(ser.dates); last = ser.dates[-1]

            def fn(r):
                ch = False
                idt = r["identity"]
                T = date.fromisoformat(r["session"])
                if idt.get("status") == "provisional" and T <= last:
                    idt["status"] = "certified" if T in tdays else "non_trading"
                    idt["certified_at"] = _now_iso(); ch = True
                if r["outcome"] is None:
                    res, complete = {}, True
                    for l in r["legs"]:
                        if l["status"] != "candidate":
                            continue
                        o = sh.settle_leg(l, r, bars=bars)
                        if o is None:
                            complete = False; break
                        res[l["leg_id"]] = o
                    if complete and res:
                        r["outcome"] = res; r["settled_at"] = _now_iso(); ch = True
                return ch
            try:
                n = jl.update(p, fn, key_field=KEY, frozen=sh.frozen_part)
                done.append(inst.key); print(f"  {inst.key:7s}{' (replay)' if replay else ''} 更新 {n} 行")
            except Exception as e:
                issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]})
    _status(args, "settle", done, issues)
    return 1 if issues else 0


def prospective_ok(r: dict) -> bool:
    idt = r.get("identity") or {}
    return (idt.get("mode") == "prospective" and idt.get("status") == "certified"
            and is_before_open(datetime.fromisoformat(r["recorded_at"]).timestamp(), date.fromisoformat(r["session"])))


def cmd_report(args) -> int:
    from undertow.core.config import load_config
    cfg = load_config(); rows = []
    base = _vdir(args.replay)
    for inst in _instruments(cfg, args.instruments):
        p = base / f"{inst.key}.jsonl"
        if p.exists():
            rows += jl.load(p, KEY)
    mode = "replay" if args.replay else "prospective"
    other_ver = [r for r in rows if r.get("config_version") != sh.CONFIG["version"]]
    rows = [r for r in rows if r.get("config_version") == sh.CONFIG["version"]]
    rows_main = rows if args.replay else [r for r in rows if prospective_ok(r)]
    print(f"影子账 {mode}（{sh.CONFIG['version']}，hash {sh.config_hash()}）：共 {len(rows)} 行，主样本 {len(rows_main)} 行，"
          f"排除（未认证/迟到）{len(rows) - len(rows_main)} 行，其它配置版本 {len(other_ver)} 行")
    fmt = lambda x: "—" if x is None else f"{x:+.3f}"
    out = {}
    bases = args.basis or [sh.CONFIG["primary_endpoint"]] + list(sh.CONFIG["secondary_endpoints"])
    subsets = [("两侧", None, False), ("put", ["P"], False), ("call", ["C"], False), ("顺增仓方向", None, True)]
    for basis in bases:
        for b in sh.CONFIG["b_rules"]:
            for lab, sides, fa in subsets:
                sm = sh.paired_summary(rows_main, basis=basis, b_rule=b, mode=mode, sides=sides, flow_aligned_only=fa)
                out[f"{basis}|{b}|{lab}"] = sm
                c = sm["coverage"]
                if lab != "两侧" and not args.detail:
                    continue
                print(f"  {basis:30s} vs {b} [{lab}]: 机会 {c['opportunities']} A有价 {c['a_priced']} 配对 {c['pairs']}"
                      f"（同腿 {c['identical_pairs']}）日期 {sm['n_dates_pairs']}  "
                      f"A={fmt(sm['A_mean_norm'])} [{fmt(sm['A_ci'][0])},{fmt(sm['A_ci'][1])}] {sm['A_verdict']}  "
                      f"A−B={fmt(sm['AminusB_mean_norm'])} [{fmt(sm['AminusB_ci'][0])},{fmt(sm['AminusB_ci'][1])}] {sm['AminusB_verdict']}"
                      f"  （{sm['sensitivity']['block_days']}日块：A−B [{fmt(sm['sensitivity']['AminusB_ci'][0])},"
                      f"{fmt(sm['sensitivity']['AminusB_ci'][1])}] {sm['sensitivity']['AminusB_verdict']}）")
    if args.output:
        Path(args.output).write_text(json.dumps({"schema": 2, "generated_at": _now_iso(), "config": sh.CONFIG,
                                                 "config_hash": sh.config_hash(), "mode": mode,
                                                 "n_rows": len(rows), "n_main": len(rows_main), "summaries": out},
                                                ensure_ascii=False, indent=1, default=str), "utf-8")
    return 0


def _status(args, cmd, done, issues, *, overall=None, counts=None):
    # 失败必须当场可见：只写状态文件 = 没人知道（AGENTS.md 静默失败第 1 条；本模块首次冒烟就犯了）
    for i in issues:
        print(f"  ⚠️ shadow {cmd} {i['instrument']}: {i['error']}", file=sys.stderr)
    if getattr(args, "status_file", None):
        from undertow.collect.asof_history import atomic_write_json
        atomic_write_json(Path(args.status_file), {
            "schema": 2, "command": f"shadow {cmd}", "ok": done, "issues": issues, "at": _now_iso(),
            "overall": overall or ("failed" if issues and not done else "partial" if issues else "complete"),
            "counts": counts or {}})


def register(sub):
    p = sub.add_parser("shadow", help="前瞻配对影子账（墙选腿 A vs 无 OI 距离 B；只读，从不下单）")
    ss = p.add_subparsers(dest="shadow_cmd", required=True)
    c = ss.add_parser("capture", help="盘前冻结当日机会"); c.add_argument("instruments", nargs="*")
    c.add_argument("--as-of", help="回放日 YYYY-MM-DD（写 replay/，不进前瞻主样本）"); c.add_argument("--status-file")
    c.set_defaults(func=cmd_capture)
    q = ss.add_parser("quote", help="盘中抓两腿盘口（入场/退出）"); q.add_argument("instruments", nargs="*")
    q.add_argument("--allow-off-hours", action="store_true"); q.add_argument("--status-file")
    q.add_argument("--window", choices=("open", "close"), default="open",
                   help="open=ET 10:00–10:20 入场与持仓标记；close=ET 15:30–15:45 持仓标记（止损/到期前平仓口径）")
    q.set_defaults(func=cmd_quote)
    s = ss.add_parser("settle", help="收盘后监控与到期结算"); s.add_argument("instruments", nargs="*")
    s.add_argument("--status-file"); s.set_defaults(func=cmd_settle)
    r = ss.add_parser("report", help="配对统计"); r.add_argument("instruments", nargs="*")
    r.add_argument("--replay", action="store_true"); r.add_argument("--basis", nargs="*")
    r.add_argument("--output"); r.add_argument("--detail", action="store_true", help="同时输出 put/call/顺增仓方向 子集")
    r.set_defaults(func=cmd_report)
