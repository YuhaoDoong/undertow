"""`undertow shadow ...` 编排（W05）：唯一把 collect 与 analyze.shadow 接起来的地方。

  capture  盘前：冻结当日机会（A/B1/B2 × P/C），无候选也记原因。--as-of 回放写 replay/。
  quote    盘中：抓两腿实时盘口 → 入场情景；已触发退出的腿 → 退出报价。非盘中默认拒绝。
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
REPLAY_DIR = DIR / "replay"
KEY = "key"


def _path(inst, replay):
    return (REPLAY_DIR if replay else DIR) / f"{inst}.jsonl"


def _instruments(cfg, names):
    keys = names or [k for k, v in cfg.instruments.items() if v.options and v.price]
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


def cmd_quote(args) -> int:
    from undertow.collect.longbridge_options import _lb_symbol
    from undertow.collect.longbridge_quote import fetch_depth
    from undertow.core.config import load_config
    phase = _phase_now()
    if phase != "rth" and not args.allow_off_hours:
        print("非盘中：盘口不可作为可成交报价。加 --allow-off-hours 可记录（标 executable=false）。", file=sys.stderr)
        return 2
    cfg = load_config(); today = market_today(); issues, done = [], []
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key, False)
        if not p.exists():
            continue
        root = inst.options.symbol
        rows = jl.load(p, KEY)
        need = {}
        for r in rows:
            for l in r["legs"]:
                if l["status"] != "candidate":
                    continue
                entry_needed = r["session"] == today.isoformat() and r["entry"] is None
                trig = next((m for m in r.get("monitor", []) if m["leg_id"] == l["leg_id"]), None)
                exit_needed = trig and l["leg_id"] not in r["exits"] and trig["trigger_date"] < today.isoformat() \
                    and today.isoformat() <= l["expiry"]
                if entry_needed or exit_needed:
                    for K in (l["sell"], l["buy"]):
                        need[(l["side"], K, l["expiry"])] = _lb_symbol(root, date.fromisoformat(l["expiry"]), l["side"], K)
        if not need:
            continue
        try:
            dep = fetch_depth(sorted(set(need.values())))
        except Exception as e:
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]}); continue
        obs = _now_iso()

        def q(side, K, exp):
            d = dep.get(need[(side, K, exp)])
            return None if d is None else {"bid": d.bid, "ask": d.ask, "bid_size": d.bid_size,
                                           "ask_size": d.ask_size, "error": d.error}

        def fn(r):
            ch = False
            if r["session"] == today.isoformat() and r["entry"] is None:
                depth = {(l["side"], K): q(l["side"], K, l["expiry"]) for l in r["legs"] if l["status"] == "candidate"
                         for K in (l["sell"], l["buy"])}
                r["entry"] = sh.price_legs(r, depth, observed_at=obs, phase=phase); ch = True
            for m in r.get("monitor", []):
                l = next(x for x in r["legs"] if x["leg_id"] == m["leg_id"])
                if l["leg_id"] in r["exits"] or m["trigger_date"] >= today.isoformat() or today.isoformat() > l["expiry"]:
                    continue
                sq, bq = q(l["side"], l["sell"], l["expiry"]), q(l["side"], l["buy"], l["expiry"])
                r["exits"][l["leg_id"]] = {"observed_at": obs, "phase": phase, "trigger_date": m["trigger_date"],
                                           "sell": sq, "buy": bq,
                                           "cost_conservative": sh.exit_cost(sq, bq, l["width_usd"]) if phase == "rth" else None}
                ch = True
            return ch
        n = jl.update(p, fn, key_field=KEY, frozen=sh.frozen_part)
        done.append(inst.key); print(f"  {inst.key:7s} 更新 {n} 行（{len(need)} 个合约盘口，phase={phase}）")
    _status(args, "quote", done, issues)
    return 1 if issues else 0


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
                have = {m["leg_id"] for m in r["monitor"]}
                for l in r["legs"]:
                    if l["status"] != "candidate" or l["leg_id"] in have:
                        continue
                    exp = date.fromisoformat(l["expiry"])
                    for d, _h, _lo, c in bars:
                        if T <= d < exp and ((c < l["sell"]) if l["side"] == "P" else (c > l["sell"])):
                            r["monitor"].append({"leg_id": l["leg_id"], "trigger_date": d.isoformat(), "close": c}); ch = True
                            break
                if r["outcome"] is None:
                    res, complete = {}, True
                    for l in r["legs"]:
                        if l["status"] != "candidate":
                            continue
                        el = ((r.get("entry") or {}).get("legs") or {}).get(l["leg_id"])
                        if el and not (r["entry"] or {}).get("executable"):
                            el = None                     # 非盘中报价不作可执行入场
                        o = sh.settle_leg(l, session=T, bars=bars, entry_leg=el, exit_info=r["exits"].get(l["leg_id"]))
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
    base = REPLAY_DIR if args.replay else DIR
    for inst in _instruments(cfg, args.instruments):
        p = base / f"{inst.key}.jsonl"
        if p.exists():
            rows += jl.load(p, KEY)
    mode = "replay" if args.replay else "prospective"
    if not args.replay:
        late = [r for r in rows if not prospective_ok(r)]   # 未认证/迟到：不进主样本，但要数出来
        rows_main = [r for r in rows if prospective_ok(r)]
    else:
        late, rows_main = [], rows
    print(f"影子账 {mode}：共 {len(rows)} 行，主样本 {len(rows_main)} 行，排除（未认证/迟到/回放）{len(late)} 行")
    out = {}
    for basis in (args.basis or [sh.CONFIG["primary_basis"], "exit_rule_quote_conservative", "snapshot_model"]):
        for b in sh.CONFIG["b_rules"]:
            s = sh.paired_summary(rows_main, basis=basis, b_rule=b, mode=mode)
            out[f"{basis}|{b}"] = s
            c = s["coverage"]
            fmt = lambda x: "—" if x is None else f"{x:+.3f}"
            print(f"  {basis:30s} vs {b}: 机会 {c['opportunities']} A有价 {c['a_priced']} 配对 {c['pairs']}（同腿 {c['identical_pairs']}）"
                  f" 日期 {s['n_dates_pairs']}  A={fmt(s['A_mean_norm'])} [{fmt(s['A_ci'][0])},{fmt(s['A_ci'][1])}] {s['A_verdict']}"
                  f"  A−B={fmt(s['AminusB_mean_norm'])} [{fmt(s['AminusB_ci'][0])},{fmt(s['AminusB_ci'][1])}] {s['AminusB_verdict']}")
    if args.output:
        Path(args.output).write_text(json.dumps({"schema": 1, "generated_at": _now_iso(), "config": sh.CONFIG,
                                                 "config_hash": sh.config_hash(), "mode": mode,
                                                 "n_rows": len(rows), "n_main": len(rows_main), "summaries": out},
                                                ensure_ascii=False, indent=1, default=str), "utf-8")
    return 0


def _status(args, cmd, done, issues):
    # 失败必须当场可见：只写状态文件 = 没人知道（AGENTS.md 静默失败第 1 条；本模块首次冒烟就犯了）
    for i in issues:
        print(f"  ⚠️ shadow {cmd} {i['instrument']}: {i['error']}", file=sys.stderr)
    if getattr(args, "status_file", None):
        Path(args.status_file).write_text(json.dumps({"schema": 1, "command": f"shadow {cmd}", "ok": done,
                                                      "issues": issues, "at": _now_iso()}, ensure_ascii=False), "utf-8")


def register(sub):
    p = sub.add_parser("shadow", help="前瞻配对影子账（墙选腿 A vs 无 OI 距离 B；只读，从不下单）")
    ss = p.add_subparsers(dest="shadow_cmd", required=True)
    c = ss.add_parser("capture", help="盘前冻结当日机会"); c.add_argument("instruments", nargs="*")
    c.add_argument("--as-of", help="回放日 YYYY-MM-DD（写 replay/，不进前瞻主样本）"); c.add_argument("--status-file")
    c.set_defaults(func=cmd_capture)
    q = ss.add_parser("quote", help="盘中抓两腿盘口（入场/退出）"); q.add_argument("instruments", nargs="*")
    q.add_argument("--allow-off-hours", action="store_true"); q.add_argument("--status-file")
    q.set_defaults(func=cmd_quote)
    s = ss.add_parser("settle", help="收盘后监控与到期结算"); s.add_argument("instruments", nargs="*")
    s.add_argument("--status-file"); s.set_defaults(func=cmd_settle)
    r = ss.add_parser("report", help="配对统计"); r.add_argument("instruments", nargs="*")
    r.add_argument("--replay", action="store_true"); r.add_argument("--basis", nargs="*")
    r.add_argument("--output"); r.set_defaults(func=cmd_report)
