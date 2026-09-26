"""`undertow shadow ...` 编排（W05）：唯一把 collect 与 analyze.shadow 接起来的地方。

  capture  盘前：冻结当日机会（A/B1/B2 × P/C），无候选也记原因。--as-of 回放写 replay/。
  quote    盘中两个窗口（--window open ET 10:00–10:20 / close = 核心收市前 30~15 分钟，
           正常日 15:30–15:45、13:00 收市日 12:30–12:45，由预存日历给出）。非窗口默认拒绝。
  windows  打印今天（ET）的两个窗口分钟数，供 session_hooks.sh 调用 —— 窗口时刻只有这一处来源。
  settle   收盘后：认证 session、监控收盘触发、到期结算（三种突破 × 多种损益口径）。
  report   配对统计：A 绝对收益、A−B 差，按日期分块区间；披露覆盖与身份构成。

只读：只调用长桥 depth/quote 与公开数据；从不下单、不改券商侧任何东西。
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from undertow.analyze import shadow as sh
from undertow.collect import jsonl_ledger as jl
from undertow.core import market_calendar as mc
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
    """盘中与否按预存日历（节假日、13:00 收市日）判断；日历未知 → off_hours（不计为有效报价）。"""
    t = datetime.now(ET)
    ct = mc.close_time(t.date())
    if ct is None:
        return "off_hours"
    m = t.hour * 60 + t.minute
    return "rth" if 9 * 60 + 30 <= m < int(ct[:2]) * 60 + int(ct[3:]) else "off_hours"


DIR_MAPPING_VERSION = "dir-map-v1"   # 偏多→P（顺向=卖 put 价差）、偏空→C（顺向=卖 call 价差）、其余→无方向


def _flow_identity(fr: dict, store, sym: str, fd) -> dict:
    """方向信号的时间身份（Codex 009 方向预登记）：信号由前一日与当日两份盘前快照的持仓变化确定性算出，
    写进首份冻结的信号台账。这里记下可审计的来源，事后能验证「开仓前已可得」。"""
    import hashlib
    from datetime import date as _d
    from undertow.analyze import flow as _flow_mod

    def snap_id(d):
        if not d:
            return None, None
        dd = _d.fromisoformat(str(d))
        ca = store.captured_at("options", sym, dd)
        pth = store.path_of("options", sym, dd)
        sha = hashlib.sha256(pth.read_bytes()).hexdigest()[:16] if pth.exists() else None
        return (datetime.fromtimestamp(ca, timezone.utc).isoformat() if ca else None), sha
    cur_at, cur_sha = snap_id(fd.isoformat())
    prev_at, prev_sha = snap_id(fr.get("prev_date"))
    # Codex 012 R01：两份来源的抓取时刻都要已知才算「可得时刻」已知 —— 旧版 max() 跳过缺失值，
    # 前一份没有抓取时刻时拿当前一份的时刻顶替，准入就排除不了前视。任一缺失 → available_at=None。
    return {"call_direction": fr.get("call_direction"), "source_snapshot_date": fd.isoformat(),
            "prev_snapshot_date": fr.get("prev_date"),
            "source_captured_at": {"current": cur_at, "previous": prev_at},
            "available_at": max(cur_at, prev_at) if (cur_at and prev_at) else None,
            "snapshot_sha": cur_sha, "prev_snapshot_sha": prev_sha,
            "ledger_row_sha": hashlib.sha256(json.dumps(fr, sort_keys=True, ensure_ascii=False, default=str)
                                             .encode()).hexdigest()[:16],
            "algorithm": "analyze.flow.probe_strong_signal → call_direction",
            "code_sha": hashlib.sha256(pathlib.Path(_flow_mod.__file__).read_bytes()).hexdigest()[:16],
            # 台账写入那一刻的方向算法指纹（signal_ledger.call_code_sha）；旧台账行没有此字段 → None
            "ledger_code_sha": fr.get("call_code_sha"),
            "mapping": DIR_MAPPING_VERSION}


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
                flow = _flow_identity(fr, store, sym, fd) if fr else None
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
    t = datetime.now(ET)
    bd = sh.window_bounds(t.date(), window)
    return bd is not None and bd[0] <= t.hour * 60 + t.minute <= bd[1]


def _fmt_bounds(bd):
    return "无（非交易日或日历未知）" if bd is None else f"{bd[0] // 60:02d}:{bd[0] % 60:02d}–{bd[1] // 60:02d}:{bd[1] % 60:02d}"


def cmd_windows(args) -> int:
    """打印今天（ET）的影子账窗口：每行「open|close 起始分 结束分」。

    非交易日 → 无输出、rc=0；日历覆盖外 → rc=3（调度层必须告警：窗口来源失效不能静默）。
    """
    d = market_today()
    if mc.is_trading_day(d) is None:
        print(f"日历 {mc.VERSION} 不覆盖 {d}：请更新 core/market_calendar.py", file=sys.stderr)
        return 3
    for w in ("open", "close"):
        bd = sh.window_bounds(d, w)
        if bd is not None:
            print(f"{w} {bd[0]} {bd[1]}")
    if mc.close_time(d) is not None:                  # 开盘后近价全链快照窗口（与收市时刻无关）
        lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in CHAIN_WINDOW)
        print(f"chain {lo} {hi}")
    return 0


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
        print(f"不在 {window} 窗口 {_fmt_bounds(sh.window_bounds(market_today(), window))}（ET）或非盘中；"
              "加 --allow-off-hours 可记录（phase 如实标注，不计为有效报价）。", file=sys.stderr)
        return 2
    cfg = load_config(); today = market_today(); wkey = f"{today.isoformat()}|{window}"
    counts = {"expected_legs": 0, "valid_legs": 0, "rows": 0}
    issues, done = [], []

    def expected_today(r, l):
        # v4：outcome 可以部分成熟（提前退出已结算、到期类未成熟），持仓标记一直抓到到期日收盘窗
        if l["status"] != "candidate":
            return False
        if window == "open" and r["session"] == today.isoformat():
            return True                                     # 入场
        return r["session"] <= today.isoformat() <= l["expiry"]   # 持仓标记

    def purpose(r):
        return "entry" if (window == "open" and r["session"] == today.isoformat()) else "exit"

    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key, False)
        if not p.exists():
            if window == "open":
                issues.append({"instrument": inst.key, "error": "no_opportunity_row"})
            continue
        root = inst.options.symbol
        need, pending = {}, []
        rows_ = jl.load(p, KEY)
        if window == "open" and not any(r["session"] == today.isoformat() for r in rows_):
            # 盘前 capture 对每个品种都写一行（无候选也记原因）→ 开盘窗时没有当日行 = 上游失败，不是「没机会」
            issues.append({"instrument": inst.key, "error": "no_opportunity_row"})
        for r in rows_:
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
    missing_rows = [i["instrument"] for i in issues if i.get("error") == "no_opportunity_row"]
    if missing_rows and overall in ("complete", "unchanged"):
        overall = "partial" if counts["valid_legs"] > 0 else "failed"
    if missing_rows:
        print(f"  ⚠️ 应有当日机会行却没有（盘前 capture 未跑或失败）：{', '.join(missing_rows)}", file=sys.stderr)
    counts["missing_rows"] = len(missing_rows)
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
            now = datetime.now(ET)

            def fn(r):
                ch = False
                idt = r["identity"]
                T = date.fromisoformat(r["session"])
                if idt.get("status") == "provisional":
                    # C02：是否交易日由预存日历回答，不看那天有没有日线（缺日线 ≠ 休市）
                    tv = mc.is_trading_day(T)
                    if tv is not None:
                        idt["status"] = "certified" if tv else "non_trading"
                        idt["certified_at"] = _now_iso(); ch = True
                old = r.get("outcome")
                done_before = bool(old) and all(o.get("complete") for o in old.values())
                if done_before and not getattr(args, "rederive", False):
                    return ch
                # outcome 由冻结事前字段 + 原始盘口尝试 + 日线【派生】，各终点独立成熟：
                # 未全部成熟的行每次都重算；--rederive 对已全部成熟的行也重算（修派生逻辑后用），原始观测不动。
                res = {l["leg_id"]: sh.settle_leg(l, r, bars=bars, now=now)
                       for l in r["legs"] if l["status"] == "candidate"}
                if res and res != old:
                    r["outcome"] = res; ch = True
                    if done_before:
                        r["rederived_at"] = _now_iso()
                    if all(o["complete"] for o in res.values()) and not r.get("settled_at"):
                        r["settled_at"] = _now_iso()
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


def _high_vol_days(cfg, insts) -> set | None:
    """各品种 |日收益| ≥ 自身近一年 70 分位的交易日（AGENTS.md：大波动门槛按品种自身分位定）。
    描述缺失是否集中在大波动日用；取数失败 → None（报告写「未知」，不当作没有集中）。"""
    from undertow.collect.cboe_history import CboeHistorySource
    src, out = CboeHistorySource(), set()
    try:
        for k in insts:
            ser = src.fetch_series(cfg.get(k))
            rets = [(ser.dates[i], abs(ser.closes[i] / ser.closes[i - 1] - 1))
                    for i in range(1, len(ser.closes)) if ser.closes[i - 1]]
            recent = sorted(r for _, r in rets[-252:])
            if not recent:
                continue
            thr = recent[int(0.7 * (len(recent) - 1))]
            out |= {(k, d.isoformat()) for d, r in rets if r >= thr}
        return out
    except Exception as e:
        print(f"  ⚠️ 大波动日判定失败（{type(e).__name__}）：缺失×波动一栏记未知", file=sys.stderr)
        return None


def _formal_freeze(summaries: dict, rows_main: list[dict]) -> str:
    """正式检验的首份产物永久保留；之后不同内容只追加修订（Codex 007：formal 不能只靠日期变成）。"""
    import hashlib
    import inspect
    from undertow.collect.asof_history import append_revisions, atomic_write_json, load_json, locked
    path = _vdir(False) / "formal" / "formal_result.json"
    body = {"config_version": sh.CONFIG["version"], "config_hash": sh.config_hash(),
            "code_sha": hashlib.sha256(inspect.getsource(sh).encode()).hexdigest()[:16],
            "input": {"n_rows": len(rows_main),
                      "rows_sha": hashlib.sha256(json.dumps(sorted(
                          [[r["key"], r.get("outcome")] for r in rows_main], key=lambda x: x[0]),
                          sort_keys=True, default=str).encode()).hexdigest()[:16]},
            "summaries": summaries}
    with locked(path):
        old = load_json(path, {})
        if not old:
            atomic_write_json(path, {**body, "first_published_at": _now_iso()})
            return f"首份正式结果已冻结：{path}"
        if {k: old.get(k) for k in body} != body:
            append_revisions(path, [{"revised_at": _now_iso(), "revision": body}])
            return f"⚠️ 正式结果与首份不同（数据迟到/更正/代码变化）：首份保留，差异已追加到 {path.name}.revisions.jsonl"
        return "正式结果与首份一致"


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
    as_of = market_today()
    ident = sh.formal_identity(as_of)
    prim, pb = sh.CONFIG["primary_endpoint"], sh.CONFIG["primary_b"]
    print(f"  身份：{'正式判定' if ident == 'formal' else '探索（正式检验日 ' + sh.CONFIG['formal_test']['date'] + ' 之前，一切判定都不是放行依据）'}；"
          f"主终点 {prim}，主池 {sh.CONFIG['primary_pool']}；日历 {mc.VERSION}")

    def ci(x):
        mb = x.get("mean_bounds") or [x.get("mean"), x.get("mean")]
        m = fmt(mb[0]) if mb[0] == mb[1] else f"[{fmt(mb[0])}…{fmt(mb[1])}]"
        return f"{m} [{fmt(x['lo'])},{fmt(x['hi'])}]"

    def line(sm, label):
        c = sm["coverage"]
        extra = ""
        if c["a_unbounded"] or c["pairs_unbounded"]:
            extra += f"（残腿处置未知：A {c['a_unbounded']}、配对 {c['pairs_unbounded']}）"
        if c["excluded_conditional"]:
            extra += f"（条件样本外 {c['excluded_conditional']}）"
        return (f"  {label}: 机会 {c['opportunities']} A有值 {c['a_priced']} 配对 {c['pairs']}（同腿 {c['identical_pairs']}）"
                f"日期 {sm['n_dates_pairs']}{extra}\n"
                f"      A全体={ci(sm['A'])} {sm['A_verdict']}｜A配对={ci(sm['A_paired'])}｜B配对={ci(sm['B_paired'])}\n"
                f"      A−B={ci(sm['AminusB'])} {sm['AminusB_verdict']}"
                f"  （{sm['sensitivity']['block_days']}日块：[{fmt(sm['sensitivity']['AminusB']['lo'])},"
                f"{fmt(sm['sensitivity']['AminusB']['hi'])}] {sm['sensitivity']['AminusB_verdict']}）")

    for basis in bases:
        bd = sh.status_breakdown(rows_main, basis)
        out[f"{basis}|status"] = bd
        st_line = "，".join(f"{k} {v}" for k, v in bd["status"].items()) or "无候选腿"
        extra = ""
        if basis == prim:
            m = bd["marks"]
            extra = (f"；持仓标记 应有 {m['expected']} 有效 {m['valid']} 未运行 {m['not_run']} 缺失 {m['missing']}"
                     f" 未到 {m['pending']}"
                     + (f"；入场缺失原因 {bd['entry_missing_reasons']}" if bd["entry_missing_reasons"] else "")
                     + (f"；退出方式 {bd['exit_modes']}" if bd["exit_modes"] else "")
                     + (f"；其中 {bd['model_disposition_points']} 个点值依赖残腿到期处置模型（未经实际记录确认）"
                        if bd.get("model_disposition_points") else ""))
        print(f"  {basis:30s} 状态：{st_line}{extra}")
        # 主终点：每个池都列；其它终点只列主池（--detail 全列）。池之间从不合并。
        pools = list(sh.CONFIG["pools"]) if (basis == prim or args.detail) else [sh.CONFIG["primary_pool"]]
        for pool in pools:
            for b in sh.CONFIG["b_rules"]:
                for lab, sides, fa in subsets:
                    sm = sh.paired_summary(rows_main, basis=basis, b_rule=b, mode=mode, sides=sides,
                                           flow_aligned_only=fa, pool=pool, as_of=as_of)
                    out[f"{basis}|{pool}|{b}|{lab}"] = sm
                    if lab != "两侧" and not args.detail:
                        continue
                    print(line(sm, f"{basis} [{pool}] vs {b} [{lab}]"))
            if basis == prim:
                for est, lab in (("both_legs_only", "仅双腿整体退出·条件样本"), ("residual0", "残腿按0·情景")):
                    sm = sh.paired_summary(rows_main, basis=basis, b_rule=pb, mode=mode, pool=pool,
                                           as_of=as_of, estimate=est)
                    out[f"{basis}|{pool}|{pb}|{est}"] = sm
                    print(line(sm, f"{basis} [{pool}] vs {pb} [{lab}]"))
                sm = sh.paired_summary(rows_main, basis=basis, b_rule=pb, mode=mode, pool=pool,
                                       non_overlap=True, as_of=as_of)
                out[f"{basis}|{pool}|{pb}|不重叠"] = sm
                print(line(sm, f"{basis} [{pool}] vs {pb} [不重叠入场·敏感性]"))

    # ── S02：机会分母、逐品种权重、缺失×大波动、描述性指标 ──
    print(f"\n  机会分母（{prim}，A vs {pb}；每格 = 品种×侧×交易日）")
    for pool in sh.CONFIG["pools"]:
        hv = _high_vol_days(cfg, sh.CONFIG["pools"][pool])
        led_end = None
        if mode == "prospective":
            led_end = min(as_of, date.fromisoformat(sh.CONFIG["formal_test"]["date"])) if ident == "formal" else as_of
        led = sh.opportunity_ledger(rows_main, basis=prim, b_rule=pb, pool=pool, mode=mode,
                                    end=led_end, high_vol=hv)
        out[f"denominator|{pool}"] = led
        if led["status"] != "ok":
            print(f"  [{pool}] {led['status']}"); continue
        tot = "，".join(f"{k} {v}" for k, v in led["totals"].items())
        print(f"  [{pool}] {led['start']}～{led['end']} 共 {led['cells']} 格：{tot}")
        for inst, c in led["by_instrument"].items():
            print(f"      {inst:7s} 格 {c['cells']:4d} 可配对 {c['pairable']:4d}（率 {c['pairable_rate'] if c['pairable_rate'] is not None else '—'}）"
                  f" 池内权重 {c['weight_in_pool'] if c['weight_in_pool'] is not None else '—'}"
                  f"｜未生成 {c['not_generated']} 无候选 {c['no_candidate']} 入场缺 {c['entry_missing']}"
                  f" 未成熟 {c['immature']} 未知 {c['unknown']}")
        if led["no_candidate_reasons"]:
            print(f"      无候选原因：{led['no_candidate_reasons']}")
        mv = led["missing_by_vol"]
        if mv is None:
            print("      缺失×大波动：未知（取数失败）")
        else:
            rate = lambda x: f"{x['missing']}/{x['cells']}" if x["cells"] else "—"
            print(f"      缺失（入场缺+未知）大波动日 {rate(mv['high'])}，其余日 {rate(mv['other'])}")
        mt = sh.metrics_table(rows_main, basis=prim, pool=pool, mode=mode, as_of=as_of)
        out[f"metrics|{pool}"] = mt
        for rule, m in mt.items():
            print(f"      {rule:3s} 点值 {m['n_point']} 仅区间 {m['n_interval_only']} 无价 {m['n_unpriced']}"
                  f"｜净$ {fmt(m['net_usd'])} 损益/宽 {fmt(m['pnl_per_width'])} 损益/风险 {fmt(m['pnl_per_max_risk'])}"
                  f" 收/宽 {fmt(m['credit_per_width'])} 费/收 {fmt(m['fee_per_credit'])}")
    print("      （描述性：池内等机会权重是研究估计量，不是账户组合权重；池均值不外推到单个品种）")

    if ident == "formal" and not args.replay:
        # 大波动门槛随近一年数据滚动，不进冻结内容（否则每天都是一条假修订）
        frozen = {k: ({kk: vv for kk, vv in v.items() if kk != "missing_by_vol"} if k.startswith("denominator|") else v)
                  for k, v in out.items()}
        print("  " + _formal_freeze(frozen, rows_main))
    if args.output:
        Path(args.output).write_text(json.dumps({"schema": 3, "generated_at": _now_iso(), "config": sh.CONFIG,
                                                 "config_hash": sh.config_hash(), "mode": mode, "identity": ident,
                                                 "n_rows": len(rows), "n_main": len(rows_main), "summaries": out},
                                                ensure_ascii=False, indent=1, default=str), "utf-8")
    return 0


# ── 开盘后近价全链快照（2026-09-26 用户要求：每天开盘后记录各价位期权价格）──────────────
# 影子账只抓预登记候选腿的实时盘口；这里另存一份【开盘后】的近价全链，供以后在任意行权价上重做模拟，
# 不受预登记腿位限制。来源 CBOE 延迟约 15 分钟：ET 10:15–10:35 抓到的约是 10:00–10:20 的报价，
# 与影子账入场窗对齐；可成交性以长桥实时盘口（shadow quote）为准，这里是研究用报价。
# 只存 ≤CHAIN_MAX_DTE 天到期、|K/现价−1| ≤ CHAIN_BAND 的合约：全链每天 15 品种约 6MB（一年约 1.5GB 进 git），
# 策略只用 2–4 DTE、±5% 内，墙的定义看 ≤14 天 → 过滤后足够重算任何规则。
CHAIN_WINDOW = ("10:15", "10:35")
CHAIN_MAX_DTE, CHAIN_BAND = 14, 0.10


class ChainSchemaError(ValueError):
    """CBOE 返回的结构不对（缺 data / options 不是列表 / 缺现价）—— 不是「0 个合约」。"""


CHAIN_QUOTE_WINDOW = ("10:00", "10:20")      # 认证为「开盘窗报价」所需的报价时刻（= 影子账入场窗）


def chain_quote_time(payload: dict):
    """报价实际时刻的最佳代理：标的最后成交时间（ET，CBOE 延迟约 15 分钟）。取不到 → None（未知）。

    payload['timestamp'] 是文件生成时刻（UTC），不是报价时刻，另存为 source_timestamp_raw 备查。
    """
    t = ((payload.get("data") or {}).get("last_trade_time"))
    try:
        dt_ = datetime.fromisoformat(str(t)) if t else None
    except ValueError:
        return None
    if dt_ is None:
        return None
    # Codex 011 D04：带时区的一律先转 ET（旧版直接用小时数：UTC 10:05 被当成 ET 10:05 认证）；
    # 不带时区按 CBOE 惯例视为 ET（数据源契约，写明在此）。
    return dt_.astimezone(ET).replace(tzinfo=None) if dt_.tzinfo else dt_


def filter_chain(payload: dict, today: date, *, max_dte: int = CHAIN_MAX_DTE, band: float = CHAIN_BAND,
                 purpose: str = "scheduled_open_window") -> dict:
    """纯函数：保留近价、近期合约，原样字段不改；附过滤说明、状态与时间认证。

    状态（Codex 009 N02）：ok / empty（合法但 0 个合约）/ no_match（有合约但都不在范围内）；
    结构不对 → ChainSchemaError，不当成 0 个合约的正常结果。
    时间：标的最后成交时间（转 ET）落在当天 10:00–10:20 → underlying_proxy_in_window=True；落在别处 → False；
    取不到 → None。它只是标的代理，期权报价时刻本身未知（options_quote_time_verified=None）。
    过滤按当时现价截断：≤max_dte 天、|K/现价−1|≤band 的有限近价链，更远的保护腿/到期不在其中。
    """
    import re
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ChainSchemaError("CBOE payload 缺 data")
    data = dict(payload["data"])
    opts = data.get("options")
    if not isinstance(opts, list):
        raise ChainSchemaError("CBOE payload 的 options 缺失或不是列表")
    spot = data.get("current_price")
    if not isinstance(spot, (int, float)) or spot <= 0:
        raise ChainSchemaError("CBOE payload 缺现价，无法按价位过滤")
    pat = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
    keep, bad = [], 0
    for o in opts:
        m = pat.match(str((o or {}).get("option", "")))
        if not m:
            bad += 1
            continue
        exp = date(2000 + int(m.group(2)[:2]), int(m.group(2)[2:4]), int(m.group(2)[4:]))
        k = int(m.group(4)) / 1000
        if 0 <= (exp - today).days <= max_dte and abs(k / spot - 1) <= band:
            keep.append(o)
    data["options"] = keep
    qt = chain_quote_time(payload)
    lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in CHAIN_QUOTE_WINDOW)
    cert = None if qt is None else (qt.date() == today and lo <= qt.hour * 60 + qt.minute <= hi)
    status = "empty" if not opts else ("no_match" if not keep else "ok")
    return {**payload, "data": data,
            "undertow_filter": {"max_dte": max_dte, "band": band, "spot_at_capture": spot,
                                "n_full": len(opts), "n_kept": len(keep), "n_unparsed": bad, "status": status,
                                "quote_time_et_proxy": qt.isoformat() if qt else None,
                                "quote_time_basis": "标的 last_trade_time（CBOE 延迟约 15 分钟）",
                                "source_timestamp_raw": payload.get("timestamp"),
                                "open_window": list(CHAIN_QUOTE_WINDOW),
                                # Codex 011 D04：这只证明【标的】最后成交在窗口内，不证明每条期权 bid/ask 来自该时刻
                                "underlying_proxy_in_window": cert,
                                "options_quote_time_verified": None,   # 数据源不给期权报价时刻 → 未知
                                "purpose": purpose, "source": "cboe delayed ~15min",
                                "scope": f"有限近价链：≤{max_dte} 天到期、按当时现价 ±{band:.0%}；更远的保护腿/到期不在内"}}


def _chain_file_ok(payload, sym: str) -> bool:
    """当日已有文件是否是一份完整有效的近价链（不是只看存在）。"""
    try:
        f = payload["undertow_filter"]
        return (payload["symbol"].upper() == sym.upper() and isinstance(payload["data"]["options"], list)
                and f["status"] == "ok" and f["n_kept"] == len(payload["data"]["options"]))
    except (KeyError, TypeError, AttributeError):
        return False


def _in_chain_window() -> bool:
    t = datetime.now(ET)
    if mc.close_time(t.date()) is None:
        return False
    m = t.hour * 60 + t.minute
    lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in CHAIN_WINDOW)
    return lo <= m <= hi


def cmd_chain(args) -> int:
    """开盘后近价全链快照 → data/snapshots/options_open/<SYM>/<日期>.json.gz（入 git，不可再生）。只读。

    成败判据（Codex 009 N02）：结构错 / 空链 / 全不匹配 / 时间未认证 各自记录；当日已有文件要严格读回并校验
    （损坏由 store.load 隔离后重抓；能读但内容不对 → 另存隔离副本再重抓），不是「存在即跳过」。
    """
    import time as _time
    from undertow.collect.cboe_options import CboeOptionsSource
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    off = bool(args.allow_off_hours) and not _in_chain_window()
    if not args.allow_off_hours and not _in_chain_window():
        print(f"不在开盘后全链窗口 ET {CHAIN_WINDOW[0]}–{CHAIN_WINDOW[1]}（交易日）；加 --allow-off-hours 可强制"
              "（记为窗外研究用途，不认证为开盘报价）。", file=sys.stderr)
        return 2
    cfg = load_config(); today = market_today(); src = CboeOptionsSource(); store = SnapshotStore()
    done, issues, skipped, uncert = [], [], [], []
    for inst in _instruments(cfg, args.instruments):
        sym = inst.options.symbol
        path = store.path_of("options_open", sym, today)
        if path.exists():
            old = store.load("options_open", sym, today)          # 损坏 → 已隔离、返回 None
            if old is not None and _chain_file_ok(old, sym):
                skipped.append(inst.key); continue
            if old is not None:                                    # 能读但内容不对：保留原件副本再重抓
                q = path.with_name(path.name + f".invalid-{int(_time.time())}")
                path.rename(q)
                issues.append({"instrument": inst.key, "error": f"已有文件内容无效，已隔离为 {q.name}，重抓"})
            else:
                issues.append({"instrument": inst.key, "error": "已有文件损坏，已隔离，重抓"})
        try:
            raw = src.fetch_raw(inst, use_cache=False)
            f = filter_chain(raw, today, purpose=("research_offwindow" if off else "scheduled_open_window"))
            meta = f["undertow_filter"]
            if meta["status"] != "ok":
                issues.append({"instrument": inst.key, "error": f"{meta['status']}：原链 {meta['n_full']} 个合约、"
                                                                f"范围内 {meta['n_kept']} 个，未保存"})
                continue
            store.save("options_open", sym, f, on_date=today, captured_at=_time.time())
            done.append(inst.key)
            if meta["underlying_proxy_in_window"] is not True:
                uncert.append(inst.key)
            print(f"  {inst.key:7s} {meta['n_kept']}/{meta['n_full']} 个合约  报价时刻≈{meta['quote_time_et_proxy']}"
                  f"  标的代理在窗内={meta['underlying_proxy_in_window']}（期权报价时刻未知）")
        except Exception as e:
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]})
    if uncert:
        issues.append({"instrument": ",".join(uncert), "error": "已保存但标的代理时刻未落在 ET 10:00–10:20（或未知）：只作研究用途"})
    overall = ("failed" if issues and not done else
               "partial" if issues else ("complete" if done else "unchanged"))
    _status(args, "chain", done, issues, overall=overall,
            counts={"saved": len(done), "skipped_existing_valid": len(skipped), "failed_or_flagged": len(issues),
                    "saved_uncertified": len(uncert)})
    print(f"  开盘后全链：保存 {len(done)}，已有有效跳过 {len(skipped)}，问题 {len(issues)} → {overall}")
    return 0 if overall in ("complete", "unchanged") else 1


def cmd_direction(args) -> int:
    """方向次要分析（预登记 dir-analysis-v1.2，docs/prereg/2026-09-26_direction_v1.2.md）。只读、纯报告。

    Codex 011 D01/D02：行不在这里预先过滤 —— 准入由 shadow_direction 自己判，拒绝按原因计入机会表。"""
    from undertow.analyze import shadow_direction as sd
    from undertow.core.config import load_config
    cfg = load_config(); rows = []
    for inst in _instruments(cfg, sh.CONFIG["pools"][sd.ANALYSIS["pool"]]):
        p = _vdir(args.replay) / f"{inst.key}.jsonl"
        if p.exists():
            rows += jl.load(p, KEY)
    other = [r for r in rows if r.get("config_version") != sd.ANALYSIS["base_config_version"]]
    rows = [r for r in rows if r.get("config_version") == sd.ANALYSIS["base_config_version"]]
    as_of = market_today()
    fmt = lambda x: "—" if x is None else f"{x:+.3f}"
    print(f"方向次要分析 {sd.ANALYSIS['version']}（基于 {sd.ANALYSIS['base_config_version']}，信号 {sd.ANALYSIS['signal']}，"
          f"单侧 α={sd.ANALYSIS['alpha_one_sided']:.4f}，经济门槛 {sd.ANALYSIS['economic_delta']}；"
          f"{'正式' if sh.formal_identity(as_of) == 'formal' else '探索'}）")
    if other:
        print(f"  ⚠️ {len(other)} 行不是 {sd.ANALYSIS['base_config_version']}，不属于本分析")
    out = {}
    for inst in sh.CONFIG["pools"][sd.ANALYSIS["pool"]]:
        rep = sd.instrument_report(rows, inst, as_of=as_of, now=datetime.now(timezone.utc))
        out[inst] = rep
        print(f"  {inst}")
        for h in ("H-dir", "H-wall|dir"):
            x = rep[h]; ci = x["ci"]; op = x["opportunities"]
            tab = "，".join(f"{k} {v}" for k, v in (op["table"] or {}).items() if v)
            print(f"      {h:10s} 机会 {op['days']} 日（{tab or '—'}）可配对率 {fmt(op['pairable_rate'])}")
            if op["reasons"]:
                print("                 拒绝原因：" + "；".join(f"{k} ×{v}" for k, v in op["reasons"].items()))
            print(f"                 配对 {x['n_pairs']}（无界 {x['n_unbounded']}，日期 {x['n_dates']}）"
                  f" 单侧界 [{fmt(ci.get('lo'))},{fmt(ci.get('hi'))}] → {x['verdict']}")
            cov = op.get("coverage")
            if cov:
                fr = lambda k: f"{cov[k]['num']}/{cov[k]['den']}"
                print(f"                 全日历覆盖：采集 {fr('collection')}，身份可审计 {fr('identity_auditable')}，"
                      f"有方向 {fr('direction_present')}，有候选 {fr('candidate_present')}，"
                      f"成熟有结果 {fr('matured_result_complete')}，端到端可配对 {fr('end_to_end_paired')}")
            print(f"                 适用范围：{x['scope']['conditional']}；推广：{x['scope']['generalization']}")
        cs = rep["common_sample"]
        for rule in (sh.CONFIG["primary_b"], "A"):
            c = cs[rule]
            if c["n_keys"]:
                print(f"      共同样本 {rule}（{c['n_keys']} 日）：" + "；".join(
                    f"{k} 均值 {fmt(c[k]['mean'])} 胜率 {fmt(c[k]['win_rate'])} 最差10% {fmt(c[k]['worst_decile_mean'])}"
                    for k in ("aligned", "counter", "always_put", "always_call")))
        dd, dg = rep["direct_direction"], rep["diagnostics"]
        print(f"      直接做方向命中率（描述，单位不同不可比）：{fmt(dd['hit_rate'])}（n={dd['n']}）；"
              f"开仓前已走 {fmt(dg['pre_move_ATR']['mean'])} ATR、开仓后 {fmt(dg['post_move_ATR']['mean'])} ATR"
              f"（n={dg['pre_move_ATR']['n']}/{dg['post_move_ATR']['n']}，描述）")
    print(f"  注：{sd.ANALYSIS['economic_delta']} 等门槛是设计选择，不保证统计功效；「未证实」≠「已排除」。")
    if args.output:
        Path(args.output).write_text(json.dumps({"analysis": sd.ANALYSIS, "as_of": as_of.isoformat(), "reports": out},
                                                ensure_ascii=False, indent=1, default=str), "utf-8")
    return 0


EXEC_DIR = Path("data/account/shadow_exec")      # 私有：gitignore；研究账在 data/history/shadow/（公开）


def cmd_exec(args) -> int:
    """S05：账户风险预算账。读本账户资金与现有持仓风险 → 对当日每个候选给出理论风险预算（pass/fail/unknown）。
    券商执行性（实际保证金、单腿退出、到期处置）未核实；各候选互为备选，n 不可相加（Codex 009 N03）。

    只读、从不下单。结果含账户金额，只写 data/account/shadow_exec/（gitignore），不进公开仓库。
    """
    from undertow.analyze import shadow_exec as sx
    from undertow.analyze.risk_aggregate import aggregate
    from undertow.collect import longbridge_account as lb
    from undertow.collect.asof_history import atomic_write_json, load_json
    from undertow.core.config import load_config
    from undertow.cli import _load_account_review
    session = args.session or market_today().isoformat()
    cfg = load_config()
    rows = []
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key, False)
        if p.exists():
            rows += [r for r in jl.load(p, KEY) if r["session"] == session and prospective_ok(r)]
    net, acct_ml, acct_cl, acct_note = None, None, None, ""
    try:
        b = _load_account_review(no_cache=False)
        if b["review"] is None:
            acct_ml, acct_cl = 0.0, {}
            a = lb.fetch_assets(); net = a.net_assets
        else:
            net = b["capital"].net_assets if b["capital"] is not None else None
            agg = aggregate(b["review"], b["capital"], asof=session)
            tot = agg["totals"]["max_loss"]
            acct_ml = tot["value"]                               # 有未知/无上限成员 → None
            root_to_key = {i.options.symbol.upper(): i.key for i in cfg.instruments.values() if i.options}
            if acct_ml is not None:
                acct_cl = {}
                for it in agg["items"]:
                    k = sx.cluster_of(root_to_key.get(it["group"], it["group"]))
                    acct_cl[k] = acct_cl.get(k, 0.0) + (it["max_loss"] or 0.0)
            else:
                acct_note = f"现有持仓最大亏损未知：缺 {len(tot['missing'])} 项"
    except lb.LongbridgeUnavailable as e:
        acct_note = f"账户不可用：{e}"[:200]
    out_dir = EXEC_DIR / sh.CONFIG["version"]
    prior = []
    for f in sorted(out_dir.glob("*.json")) if out_dir.exists() else []:
        if f.stem < session:
            prior += load_json(f, {}).get("candidates", [])
    res = sx.evaluate(rows, session=session, net_assets=net, account_open_max_loss=acct_ml,
                      account_cluster_open=acct_cl, prior=prior)
    if net is None:
        res = [{**r, "budget_status": "unknown", "n": 0, "notes": [acct_note or "净资产未知"] + r.get("notes", [])}
               if r["budget_status"] == "pass" else r for r in res]
    body = {"schema": 1, "session": session, "computed_at": _now_iso(), "exec_version": sx.VERSION,
            "config_version": sh.CONFIG["version"], "net_assets": net, "account_open_max_loss": acct_ml,
            "account_note": acct_note, "candidates": res}
    atomic_write_json(out_dir / f"{session}.json", body)
    n_ok = sum(r["budget_status"] == "pass" for r in res)
    print(f"账户风险预算账 {session}（{sx.VERSION}）：候选 {len(res)}，理论预算通过 {n_ok}"
          f"（互斥备选、不可相加；券商执行性未核实）"
          + (f"；{acct_note}" if acct_note else ""))
    for r in res:
        if r["budget_status"] == "no_candidate":
            continue
        econ = (f"收 ${r['credit']:.0f} 最大亏 ${r['max_loss']:.0f}" if r.get("max_loss") is not None else "")
        print(f"  {r['instrument']:7s} {r['leg_id']:5s} 预算 {r['budget_status']} {r['n']} 组  {econ}  {r['notes'][0]}")
    print(f"  （结果含账户金额，只写 {out_dir}/，已 gitignore）")
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
                   help="open=ET 10:00–10:20 入场与持仓标记；close=核心收市前 30~15 分钟（正常 15:30–15:45，"
                        "13:00 收市日 12:30–12:45）持仓标记与到期前平仓")
    q.set_defaults(func=cmd_quote)
    w = ss.add_parser("windows", help="打印今天 ET 的影子账窗口（供调度脚本）"); w.set_defaults(func=cmd_windows)
    ch = ss.add_parser("chain", help="开盘后近价全链快照（ET 10:15–10:35，入 git；只读）")
    ch.add_argument("instruments", nargs="*"); ch.add_argument("--allow-off-hours", action="store_true")
    ch.add_argument("--status-file"); ch.set_defaults(func=cmd_chain)
    dr = ss.add_parser("direction", help="方向次要分析（预登记 dir-analysis-v1.2；只读报告）")
    dr.add_argument("--replay", action="store_true"); dr.add_argument("--output")
    dr.set_defaults(func=cmd_direction)
    e = ss.add_parser("exec", help="S05 账户风险预算账（私有，写 data/account/；理论预算，券商执行性未核实；只读）")
    e.add_argument("instruments", nargs="*"); e.add_argument("--session", help="YYYY-MM-DD，默认今天 ET")
    e.set_defaults(func=cmd_exec)
    s = ss.add_parser("settle", help="收盘后监控与到期结算"); s.add_argument("instruments", nargs="*")
    s.add_argument("--rederive", action="store_true",
                   help="对已全部成熟的行也按当前派生逻辑重算 outcome（原始观测不动；变化时记 rederived_at）")
    s.add_argument("--status-file"); s.set_defaults(func=cmd_settle)
    r = ss.add_parser("report", help="配对统计"); r.add_argument("instruments", nargs="*")
    r.add_argument("--replay", action="store_true"); r.add_argument("--basis", nargs="*")
    r.add_argument("--output"); r.add_argument("--detail", action="store_true", help="同时输出 put/call/顺增仓方向 子集")
    r.set_defaults(func=cmd_report)
