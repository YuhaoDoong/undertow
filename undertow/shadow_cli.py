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
from collections import Counter
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
    sb = sample_bounds(d)                             # 盘中时段采样 [lo, hi)；打印闭区间末分钟 hi−1 供调度脚本
    if sb is not None:
        print(f"sample {sb[0]} {sb[1] - 1}")
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
    # 口径（Codex 024-6）：v5 的一条「腿」(leg_id 如 P-A) = 一个候选价差（卖腿 + 买腿两个合约）；状态字段名沿用 expected_legs
    print(f"  {wkey}：应有 {counts['expected_legs']} 个候选价差（每个含卖、买两个合约），有效 {counts['valid_legs']} → {overall}")
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
    """方向次要分析（预登记 dir-analysis-v1.3，docs/prereg/2026-09-26_direction_v1.3.md）。只读、纯报告。

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
                      f"成熟有结果对象 {fr('matured_result_object_present')}，"
                      f"成熟收益完整识别 {fr('matured_return_identified')}，端到端可配对 {fr('end_to_end_paired')}")
                if op.get("collection_pending_basis"):
                    print(f"                 当日采集待定（{op['collection_pending_basis']}）；运维告警另见任务状态，不受此影响")
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


BARS_SCOPE = {"pool": "etf", "rules": ("A", "B1")}   # 长桥历史 K 线按不同代码数限额 → 只补主池的主比较两臂


SAMPLE = {"version": "shadow-sample-v2-20260927", "start": "09:45", "end": "13:00", "step_min": 15,
          "selection": "每个合约取该时段内第一次成功观测（不择优）；失败尝试全部保留"}
SAMPLE_DIR = Path("data/history/shadow_samples")


def sample_bounds(d: date) -> tuple[int, int] | None:
    """盘中采样区间 [lo, hi)（ET 分钟，右开）：用户可操作的 09:45–13:00（用户 2026-09-27），
    且不晚于收市前 15 分钟；非交易日/日历未知 → None。正常日 = 09:45…12:45 共 13 个 15 分钟桶。"""
    ct = mc.close_time(d)
    if ct is None:
        return None
    lo = int(SAMPLE["start"][:2]) * 60 + int(SAMPLE["start"][3:])
    hi = min(int(SAMPLE["end"][:2]) * 60 + int(SAMPLE["end"][3:]), int(ct[:2]) * 60 + int(ct[3:]) - 15)
    return (lo, hi) if hi > lo else None


def sample_slot(minute: int, bd: tuple[int, int]) -> str | None:
    """所在 15 分钟桶的标签 "HH:MM"（桶起点）；不在 [lo, hi) 内 → None。
    标签只是桶名，不是采样时刻 —— 实际时刻看记录里的 started_at / ended_at。"""
    if not (bd[0] <= minute < bd[1]):
        return None
    m = bd[0] + (minute - bd[0]) // SAMPLE["step_min"] * SAMPLE["step_min"]
    return f"{m // 60:02d}:{m % 60:02d}"


def sample_slots(bd: tuple[int, int]) -> list[str]:
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(bd[0], bd[1], SAMPLE["step_min"])]


def sample_legs(rows: list[dict], today: date) -> list[dict]:
    """今天在场的候选腿：当日入场的 + 尚未到期的持仓（session ≤ 今天 ≤ 到期日）。纯函数。"""
    t = today.isoformat()
    return [{"key": r["key"], "session": r["session"], "leg_id": l["leg_id"], "rule": l.get("rule"),
             "side": l["side"], "expiry": l["expiry"], "sell": l["sell"], "buy": l["buy"]}
            for r in rows for l in r.get("legs", [])
            if l.get("status") == "candidate" and r["session"] <= t <= l["expiry"]]


def quote_observed(q: dict | None) -> bool:
    """一次合约取数是否算【成功观测】：数据源正常返回（无 error）即算，哪怕一侧无挂单（bid/ask 为 None 或 0）——
    那是真实的市场状态，不能为了补出更好的价格而反复重试。"""
    return bool(q) and not q.get("error")


def _read_samples(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def slot_state(recs: list[dict], slot: str, wanted: set[str]) -> tuple[set[str], str]:
    """该桶里已成功观测的代码集合，以及状态：complete / partial / failed / missing（没有任何尝试）。"""
    tries = [r for r in recs if r.get("slot") == slot]
    ok = {s for r in tries for s, q in (r.get("quotes") or {}).items() if quote_observed(q)}
    ok |= {r["underlying_symbol"] for r in tries if quote_observed(r.get("underlying"))}
    got = ok & wanted
    if not tries:
        return got, "missing"
    return got, ("complete" if got == wanted else "partial" if got else "failed")


def cmd_sample(args) -> int:
    """盘中时段采样（用户 2026-09-27：「扩大记录时间」）：ET [09:45, 13:00) 每 15 分钟一个桶，
    对在场候选腿与标的各记一次盘口。与 v5 预登记无关：不写 v5 账本、不改入场窗（10:00–10:20）与主终点。

    v2（Codex 014 N14-01）：逐合约的取数错误算失败，不再被当成「本桶已完成」。每次运行只重取本桶内
    尚未成功观测的代码，新尝试追加为一条记录（旧尝试不覆盖）；研究时每个合约取本桶第一次成功观测。
    状态：complete（全部成功）/ unchanged（本桶已齐）→ rc=0；partial / failed（本次需要的全部失败）→ rc=1。
    --check [DATE]：收尾核对当日各品种各桶的状态，有缺/败 → rc=1。只读，从不下单。"""
    if getattr(args, "check", None):
        return _sample_check(args)
    from undertow.collect.asof_history import locked
    from undertow.collect.longbridge_options import _lb_symbol
    from undertow.collect.longbridge_quote import fetch_depth, fetch_stock_quotes
    from undertow.core.config import load_config
    now = datetime.now(ET)
    today = now.date()
    bd = sample_bounds(today)
    phase = _phase_now()
    slot = sample_slot(now.hour * 60 + now.minute, bd) if bd else None
    if slot is None or phase != "rth":
        print(f"不在盘中采样时段（{_fmt_bounds(bd)} ET，右开）或非盘中。", file=sys.stderr)
        return 2
    cfg = load_config()
    issues, done = [], []
    counts = {"slot": slot, "wanted": 0, "observed_before": 0, "observed_now": 0, "failed_now": 0,
              "underlying_failed": 0, "instruments": 0}
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key, False)
        if not p.exists():
            continue
        legs = sample_legs(jl.load(p, KEY), today)
        if not legs:
            continue
        root = inst.options.symbol
        under_sym = f"{root}.US"
        for l in legs:
            l["sell_sym"] = _lb_symbol(root, date.fromisoformat(l["expiry"]), l["side"], l["sell"])
            l["buy_sym"] = _lb_symbol(root, date.fromisoformat(l["expiry"]), l["side"], l["buy"])
        wanted = {l["sell_sym"] for l in legs} | {l["buy_sym"] for l in legs} | {under_sym}
        out = SAMPLE_DIR / today.isoformat() / f"{inst.key}.jsonl"
        with locked(out):
            old = _read_samples(out)
            got, _ = slot_state(old, slot, wanted)
            counts["wanted"] += len(wanted); counts["observed_before"] += len(got)
            need = sorted(wanted - got - {under_sym})
            need_under = under_sym not in got
            if not need and not need_under:
                continue
            t0 = _now_iso()
            quotes = {}
            if need:
                try:
                    dep = fetch_depth(need)
                except Exception as e:            # 全部失败：照样记下失败尝试
                    dep, err = {}, f"{type(e).__name__}: {e}"[:200]
                else:
                    err = "not_returned"
                for s in need:
                    d = dep.get(s)
                    quotes[s] = ({"bid": d.bid, "ask": d.ask, "bid_size": d.bid_size, "ask_size": d.ask_size,
                                  "error": d.error or None} if d is not None
                                 else {"bid": None, "ask": None, "bid_size": 0, "ask_size": 0, "error": err})
                # 当场重试一次出错的代码（2026-09-28 IWM 10:00 桶：一次 connect timeout，下次唤醒已进下一桶，
                # 这一格就永久缺了）。重试结果另记 retried=True；仍失败保留原错误，不改变桶状态判定逻辑。
                retry = [s for s in need if quotes[s].get("error")] if SAMPLE_INLINE_RETRY else []
                if retry:
                    import time as _t
                    _t.sleep(SAMPLE_RETRY_SLEEP_S)
                    t1 = _now_iso()
                    try:
                        dep2, err2 = fetch_depth(retry), "not_returned"
                    except Exception as e:
                        dep2, err2 = {}, f"{type(e).__name__}: {e}"[:200]
                    for s in retry:
                        first = {"at": t0, "error": quotes[s]["error"]}            # 每次尝试的实际时刻与错误（024-6）
                        d = dep2.get(s)
                        if d is not None and not d.error:
                            quotes[s] = {"bid": d.bid, "ask": d.ask, "bid_size": d.bid_size, "ask_size": d.ask_size,
                                         "error": None, "retried": True,
                                         "attempts": [first, {"at": t1, "error": None}]}
                        else:
                            quotes[s]["attempts"] = [first, {"at": t1, "error": (d.error if d is not None else err2)}]
            under = None
            if need_under:
                try:
                    uq = fetch_stock_quotes([under_sym]).get(under_sym)
                    under = ({"freshest": uq.freshest, "kind": uq.freshest_kind, "error": None} if uq
                             else {"error": "not_returned"})
                except Exception as e:
                    under = {"error": f"{type(e).__name__}"}
            rec = {"schema": 2, "version": SAMPLE["version"], "slot": slot, "started_at": t0, "ended_at": _now_iso(),
                   "phase": phase, "underlying_symbol": under_sym, "underlying": under, "legs": legs,
                   "quotes": quotes}
            out.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in old + [rec])
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text(body, "utf-8")
            if tmp.read_text("utf-8") != body:
                tmp.unlink(missing_ok=True)
                raise ValueError(f"{out} 回读校验失败；原文件未改")
            tmp.replace(out)
        ok_now = sum(quote_observed(q) for q in quotes.values())
        bad = [s for s, q in quotes.items() if not quote_observed(q)]
        counts["observed_now"] += ok_now + (1 if quote_observed(under) else 0)
        counts["failed_now"] += len(bad)
        counts["instruments"] += 1
        if need_under and not quote_observed(under):
            counts["underlying_failed"] += 1
            issues.append({"instrument": inst.key, "error": f"标的 {under_sym} 取数失败（{(under or {}).get('error')}）"
                                                            "：本桶腿报价仍有效，只缺标的价诊断"})
        if bad:
            issues.append({"instrument": inst.key,
                           "error": f"{len(bad)}/{len(quotes)} 个合约取数失败（{quotes[bad[0]]['error']}），本桶内下次唤醒重试"})
        else:
            done.append(inst.key)
    tried = counts["observed_now"] + counts["failed_now"] + counts["underlying_failed"]
    if tried == 0:
        overall = "unchanged"
    elif not issues:
        overall = "complete"
    elif counts["observed_now"] > 0:
        overall = "partial"
    else:
        overall = "failed"
    print(f"  盘中采样 {today} 桶 {slot}：本次成功 {counts['observed_now']}、失败 {counts['failed_now']}"
          f"（标的失败 {counts['underlying_failed']}）；此前已成功 {counts['observed_before']}/{counts['wanted']} → {overall}")
    _status(args, "sample", done, issues, overall=overall, counts=counts)
    return 0 if overall in ("complete", "unchanged") else 1


def _sample_check(args) -> int:
    """收尾核对：当日（或指定日）每个品种每个桶的状态。有 missing/failed/partial → rc=1，逐项列出。
    只看落盘记录，不联网；品种范围 = 当日 v5 前瞻行里有在场候选腿的品种。"""
    from undertow.core.config import load_config
    d = date.fromisoformat(args.check) if args.check != "today" else market_today()
    bd = sample_bounds(d)
    if bd is None:
        print(f"{d} 无采样时段（非交易日或日历未知）")
        return 0
    cfg = load_config()
    bad, total = [], 0
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key, False)
        legs = sample_legs(jl.load(p, KEY), d) if p.exists() else []
        if not legs:
            continue
        recs = _read_samples(SAMPLE_DIR / d.isoformat() / f"{inst.key}.jsonl")
        wanted = set()
        for r in recs:
            wanted |= {x for l in r.get("legs", []) for x in (l.get("sell_sym"), l.get("buy_sym")) if x}
            wanted.add(r.get("underlying_symbol"))
        wanted.discard(None)
        for sl in sample_slots(bd):
            total += 1
            got, st_ = slot_state(recs, sl, wanted) if wanted else (set(), "missing")
            if st_ != "complete":
                bad.append(f"{inst.key} {sl} {st_}（成功 {len(got)}/{len(wanted) or '?'}）")
    print(f"盘中采样核对 {d}：{total} 个（品种, 桶），异常 {len(bad)}")
    for b in bad:
        print(f"  ⚠️ {b}")
    _status(args, "sample-check", [], [{"instrument": b.split()[0], "error": b} for b in bad],
            counts={"cells": total, "bad": len(bad)})
    return 1 if bad else 0


FIELDCHECK_DIR = Path("data/history/fieldcheck")


def cmd_fieldcheck(args) -> int:
    """每日现场核验（用户 2026-09-28：「设置好自动触发，不要等我来提醒」；项目源自 Codex 013/014 的周一验收清单）。

    --phase pre   盘前：主池各品种当日机会行、两份来源时刻、台账方向指纹、方向准入结论
    --phase open  开盘后：开盘窗各腿入场报价、近价全链快照内容、已结束的盘中采样桶
    --phase close 收盘后：收盘窗各腿报价、全天采样桶、待发布记录中「已发布却未清理」的条目
    未到时点的项目记「待定」，不算异常。报告写 data/history/fieldcheck/<日>_<阶段>.md（随每日任务入库）；
    有异常 → rc=1（调度层弹通知）。只读，从不下单；不含账户数据。"""
    import gzip
    import subprocess
    from undertow.analyze import shadow_direction as sd
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    phase = args.phase
    day = date.fromisoformat(args.session) if args.session else market_today()
    now = datetime.now(ET)
    cfg, store = load_config(), SnapshotStore()
    lines = [f"# 现场核验 {day} · {phase}", "", f"运行于 {now:%Y-%m-%d %H:%M} ET；方向分析 {sd.ANALYSIS['version']}；"
             f"影子账 {sh.CONFIG['version']}", ""]
    bad = []

    def ended(minute: int) -> bool:
        return now.date() > day or (now.date() == day and now.hour * 60 + now.minute >= minute)

    if mc.is_trading_day(day) is not True:
        known = mc.is_trading_day(day) is False
        print(f"{day} {'非交易日' if known else '日历未覆盖'}：不核验。", file=sys.stderr)
        return 0 if known else 1
    for inst in sh.CONFIG["pools"][sh.CONFIG["primary_pool"]]:
        p = _path(inst, False)
        lines.append(f"## {inst}")
        try:
            rows_i = jl.load(p, KEY) if p.exists() else []
        except Exception as e:                        # 台账损坏：报告出来，不让核验本身崩掉
            lines.append(f"- ❌ 台账无法读取：{str(e)[:160]}"); bad.append(f"{inst} 台账无法读取")
            continue
        r = next((x for x in rows_i if x["session"] == day.isoformat()), None)
        if r is None:
            lines.append("- ❌ 无当日机会行（盘前 capture 未跑或失败）"); bad.append(f"{inst} 无机会行")
            continue
        if phase == "pre":
            f = (r.get("decision") or {}).get("flow") or {}
            src = f.get("source_captured_at") or {}
            el = sd.eligibility(r)
            lines += [f"- recorded_at {r.get('recorded_at')}；identity {(r.get('identity') or {}).get('status')}",
                      f"- 来源抓取 current {src.get('current')} / previous {src.get('previous')}；available_at {f.get('available_at')}",
                      f"- 方向 {f.get('call_direction')!r}；ledger_code_sha {f.get('ledger_code_sha')}"
                      f"（冻结 {sd.ANALYSIS['call_code_sha']}）；准入 {el[0]} {el[1]}"]
            if el[0] == "identity_fail":
                bad.append(f"{inst} 方向准入 identity_fail：{el[1]}")
            continue
        legs = [l for l in r.get("legs", []) if l.get("status") == "candidate"]
        for w in (("open",) if phase == "open" else ("open", "close")):
            bd = sh.window_bounds(day, w)
            wk = f"{day.isoformat()}|{w}"
            if bd is None:
                continue
            if not ended(bd[1] + 1):
                lines.append(f"- {w} 窗：待定（{_fmt_bounds(bd)} 未结束）"); continue
            if w == "open":
                st_ = {l["leg_id"]: sh.window_leg(r, l, wk, "entry")["status"] for l in legs}
            else:
                st_ = {l["leg_id"]: sh.window_leg(r, l, wk, "exit")["status"]
                       for l in legs if r["session"] <= day.isoformat() <= l["expiry"]}
            nv = sum(v == "valid" for v in st_.values())
            lines.append(f"- {w} 窗：有效 {nv}/{len(st_)}；{st_}")
            if st_ and nv == 0:
                bad.append(f"{inst} {w} 窗零有效报价")
        if phase == "open":
            sym = cfg.get(inst).options.symbol
            cp = store.path_of("options_open", sym, day)
            lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in CHAIN_WINDOW)
            if not cp.exists():
                if ended(hi + 1):
                    lines.append(f"- ❌ 全链快照：窗口已过仍无文件"); bad.append(f"{inst} 全链快照缺失")
                else:
                    lines.append("- 全链快照：待定（窗口未结束）")
            else:
                rec = json.loads(gzip.decompress(cp.read_bytes()))
                m = (rec.get("payload") or rec).get("undertow_filter") or {}
                lines.append(f"- 全链快照：status {m.get('status')}；保留 {m.get('n_kept')}/{m.get('n_full')}（未解析 "
                             f"{m.get('n_unparsed')}）；报价时刻代理 {m.get('quote_time_et_proxy')}；标的代理在窗内 "
                             f"{m.get('underlying_proxy_in_window')}")
                if not m.get("n_kept"):
                    bad.append(f"{inst} 全链快照无合约")
        # 盘中采样：只核已经结束的桶
        sb = sample_bounds(day)
        if sb is not None:
            legs_s = sample_legs(rows_i, day)
            recs = _read_samples(SAMPLE_DIR / day.isoformat() / f"{inst}.jsonl")
            wanted = {x for rr in recs for l in rr.get("legs", []) for x in (l.get("sell_sym"), l.get("buy_sym")) if x}
            wanted |= {rr.get("underlying_symbol") for rr in recs if rr.get("underlying_symbol")}
            done_slots = [s_ for s_ in sample_slots(sb)
                          if ended(int(s_[:2]) * 60 + int(s_[3:]) + SAMPLE["step_min"])]
            if legs_s and done_slots:
                states = {s_: slot_state(recs, s_, wanted)[1] if wanted else "missing" for s_ in done_slots}
                badslots = {k: v for k, v in states.items() if v != "complete"}
                lines.append(f"- 盘中采样：已结束 {len(done_slots)} 桶，异常 {len(badslots)} {badslots or ''}")
                if badslots:
                    bad.append(f"{inst} 采样异常桶 {len(badslots)}")
    if phase == "close":
        pend = Path("data/logs/.publish_pending_auto")
        rows_ = [x.split("\t") for x in pend.read_text().splitlines() if x.strip()] if pend.exists() else []
        stale = []
        for path, h in rows_:
            rp = subprocess.run(["git", "rev-parse", "--verify", "-q", f"HEAD:{path}"], capture_output=True, text=True)
            if rp.returncode == 0 and rp.stdout.strip() == h:
                stale.append(path)
        lines += ["", f"## 待发布记录", f"- 共 {len(rows_)} 行；已发布却未清理 {len(stale)} 行 {sorted(set(stale))[:5]}"]
        if stale:
            bad.append(f"待发布记录有 {len(stale)} 行已发布却未清理")
    lines += ["", "## 结论", *(f"- ⚠️ {b}" for b in bad)] if bad else ["", "## 结论", "- 无异常（待定项除外）"]
    _write_fieldcheck(day, phase, lines)
    print("\n".join(lines))
    _status(args, f"fieldcheck-{phase}", [], [{"instrument": b.split()[0], "error": b} for b in bad],
            counts={"problems": len(bad)})
    return 1 if bad else 0


def _write_fieldcheck(day, phase, lines):
    FIELDCHECK_DIR.mkdir(parents=True, exist_ok=True)
    out = FIELDCHECK_DIR / f"{day.isoformat()}_{phase}.md"
    tmp = out.with_name(out.name + ".tmp")
    body = "\n".join(lines) + "\n"
    tmp.write_text(body, "utf-8")
    tmp.replace(out)


def bars_plan(rows: list[dict], *, last_day: date, insts=None, rules=None) -> dict:
    """影子账候选价差需要补的逐分钟 K 线：{(root, 交易日): {合约代码, …, 标的代码}}。

    每条候选腿的卖腿与买腿，从 session 到到期日（含）的每个交易日，截到 last_day。
    insts / rules 为 None 时不筛选。纯函数。"""
    from undertow.collect import longbridge_bars as lbb
    need: dict = {}
    for r in rows:
        root = r.get("symbol")
        if not root or (insts is not None and r.get("instrument") not in insts):
            continue
        for leg in r.get("legs", []):
            if leg.get("status") != "candidate" or (rules is not None and leg.get("rule") not in rules):
                continue
            days = mc.trading_days(date.fromisoformat(r["session"]), min(date.fromisoformat(leg["expiry"]), last_day))
            for d in days or []:
                s = need.setdefault((root, d), set())
                s.add(f"{root}.US")
                for k in ("sell", "buy"):
                    s.add(lbb.option_symbol(root, leg["expiry"], leg["side"], leg[k]))
    return need


def cmd_bars(args) -> int:
    """补影子账候选价差的【历史盘中成交价】（长桥 1 分钟 K 线，只读）。用户 2026-09-27 要求。

    已到期合约约一周后长桥就查不到 → 按日期从早到晚补（最早的最先消失）。已存的不重抓；
    「查不到 / 无效代码」如实记为状态（含查询时刻与原始返回），默认不重查；--retry-missing 可重查 ——
    「本次通过该接口未取得」不等于永久不可得（Codex 014）。网络/CLI 故障 → issue、rc=1。
    口径：只有成交价、无买卖价 —— 是 v5 保守入场价之外的补充数据，不替代它。
    长桥历史 K 线按【不同代码数】限额（实测 400）→ 默认只补主池 A、B1；配额用尽 → rc=3（不是故障）。"""
    from undertow.collect import longbridge_bars as lbb
    rows = []
    for d in (_vdir(False), _vdir(True)):
        for p in sorted(d.glob("*.jsonl")):
            rows += jl.load(p, KEY)
    today = market_today()
    now_et = datetime.now(ET)
    last_day = today if (mc.is_trading_day(today) and (now_et.hour, now_et.minute) >= (16, 20)) \
        else mc.prev_trading_day(today)
    if last_day is None:
        _status(args, "bars", [], [{"instrument": "-", "error": "日历未覆盖今天"}])
        return 1
    if args.since:
        rows = [r for r in rows if r["session"] >= args.since]
    if args.all:
        plan = bars_plan(rows, last_day=last_day)
    else:
        plan = bars_plan(rows, last_day=last_day, insts=set(sh.CONFIG["pools"][BARS_SCOPE["pool"]]),
                         rules=BARS_SCOPE["rules"])
    if args.gaps:
        import subprocess as _sp
        from undertow.collect.asof_history import atomic_write_json
        gaps = bars_gap_ledger(plan, today=today)
        ver = _sp.run(["longbridge", "--version"], capture_output=True, text=True).stdout.strip() or None
        atomic_write_json(BARS_GAPS, {"generated_at": _now_iso(), "cli_version": ver, "last_day": last_day.isoformat(),
                                      "scope": "all" if args.all else "primary_A_B1", "n": len(gaps), "gaps": gaps,
                                      "note": "「到期约一周后查不到」为本地观察，非长桥承诺；配额恢复后不保证补回"})
        c = Counter(g["last_status"] for g in gaps)
        print(f"缺口台账 → {BARS_GAPS}：{len(gaps)} 项；上次状态 {dict(c)}；"
              f"已有逐分钟收盘可替代 {sum(g['close_available_from_intraday'] for g in gaps)}；CLI {ver}")
        for g in gaps[:5]:
            print(f"  #{g['priority']} {g['symbol']} 交易日 {g['day']} 到期 {g['expiry']} 上次 {g['last_status']}")
        return 0
    # 补数顺序（Codex 024-4）：按该 (标的, 日) 内最近的合约到期日由近到远（离「查不到」最近的先补），再按交易日
    todo = sorted(plan.items(), key=lambda kv: (min((e for e in (_expiry_of(x) for x in kv[1]) if e), default=date.max),
                                                kv[0][1]))
    n_new = n_ok = n_gone = 0
    issues, done = [], []
    for (root, day), syms in todo:
        path = lbb.path_of(root, day)
        try:
            cur = lbb.load_day(path) or lbb.new_day(root, day)
        except lbb.BarsFileCorrupt as e:
            q = lbb.quarantine(path)
            print(f"  ⚠️ {e}；已隔离为 {q.name}，重新抓取（旧文件保留）", file=sys.stderr)
            cur = lbb.new_day(root, day)
        covered = lbb.intraday_covered(root, day, need=args.need)   # need=close：全时段逐分钟收盘已存 → 省配额；ohlc：不跳过
        missing = sorted(s for s in syms if s not in covered and (s not in cur["contracts"]
                         or (args.retry_missing and cur["contracts"][s].get("status") in ("not_found", "invalid_symbol"))))
        if not missing:
            continue
        try:
            for s in missing:
                res = lbb.fetch_day(s, day)
                cur["contracts"][s] = res
                n_new += 1
                n_ok += res["status"] == "ok"
                n_gone += res["status"] in ("not_found", "invalid_symbol")
        except lbb.BarsQuotaExhausted as e:
            if any(s in cur["contracts"] for s in missing):
                lbb.save_day(path, cur)
                done.append(f"{root} {day}")
            print(f"逐分钟 K 线：长桥历史 K 线配额用尽（{e}）；本次新抓 {n_new}（有数据 {n_ok}），"
                  f"未完成的下次自动续补。配额重置周期未查证。", file=sys.stderr)
            _status(args, "bars", done, [], overall="quota_exhausted",
                    counts={"new": n_new, "ok": n_ok, "gone": n_gone})
            return 3
        except lbb.BarsUnavailable as e:
            issues.append({"instrument": f"{root} {day}", "error": str(e)})
        if missing and any(s in cur["contracts"] for s in missing):
            lbb.save_day(path, cur)
            done.append(f"{root} {day}")
        if issues and len(issues) >= 3:
            print("  ⚠️ 连续故障，停止本次补抓（下次从断点继续）", file=sys.stderr)
            break
    print(f"逐分钟 K 线：计划 {len(todo)} 个（标的, 日）文件、本次新抓 {n_new} 个合约日"
          f"（有数据 {n_ok}，长桥已查不到/无此合约 {n_gone}），截至 {last_day}")
    _status(args, "bars", done, issues, counts={"new": n_new, "ok": n_ok, "gone": n_gone})
    return 1 if issues else 0


SAMPLE_INLINE_RETRY = True      # 采样：出错代码当场重试一次（下次唤醒可能已进下一桶）
SAMPLE_RETRY_SLEEP_S = 2.0
INTRADAY_PACE_S = 0.55          # 长桥行情接口约 60 次 / 30 秒（官方文档写于 history candlestick 页）；留余量


EXPIRY_PROFILE_DIR = Path("data/history/expiry_profile")
EXPIRY_PROFILE_DTE = 35
EXPIRY_PROFILE_BAND = 0.10


def expiry_profile_row(inst: str, sym: str, session: date, snap, ident: dict | None, now: datetime) -> dict:
    """某品种某交易日开盘前可得的逐到期持仓画像（纯组装）。到期日磁吸研究的事前记录（用户 2026-09-29）。
    只记录，不产生信号：各到期的类型 Q/M/W/D、剩余交易日历天数、C/P 总 OI 与成交量、±10% 内各侧 OI 最大的 5 个行权价。"""
    from undertow.analyze.expiry_type import classify
    spot = snap.spot
    by: dict = {}
    for c in snap.contracts:
        dte = (c.expiry - session).days
        if not 0 <= dte <= EXPIRY_PROFILE_DTE:
            continue
        e = by.setdefault(c.expiry, {"oi": {"C": 0, "P": 0}, "vol": {"C": 0, "P": 0}, "near": {"C": {}, "P": {}}})
        e["oi"][c.kind] += c.open_interest
        e["vol"][c.kind] += c.volume
        if spot and abs(c.strike / spot - 1) <= EXPIRY_PROFILE_BAND:
            e["near"][c.kind][c.strike] = e["near"][c.kind].get(c.strike, 0) + c.open_interest
    exps = []
    for exp, e in sorted(by.items()):
        t = classify(exp)
        exps.append({"expiry": exp.isoformat(), "type": t["type"], "is_monthly": t["is_monthly"],
                     "is_quarterly": t["is_quarterly"], "dte": (exp - session).days,
                     "oi_c": e["oi"]["C"], "oi_p": e["oi"]["P"], "vol_c": e["vol"]["C"], "vol_p": e["vol"]["P"],
                     "top_c": sorted(sorted(e["near"]["C"].items(), key=lambda x: -x[1])[:5]),
                     "top_p": sorted(sorted(e["near"]["P"].items(), key=lambda x: -x[1])[:5])})
    return {"key": f"{inst}|{session.isoformat()}", "instrument": inst, "symbol": sym, "session": session.isoformat(),
            "recorded_at": now.astimezone(timezone.utc).isoformat(),
            "before_open": now.astimezone(ET) < datetime.combine(session, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30),
            "snapshot_sha": (ident or {}).get("sha256"), "captured_at": (ident or {}).get("captured_at"),
            "spot": spot, "band": EXPIRY_PROFILE_BAND, "max_dte": EXPIRY_PROFILE_DTE, "expiries": exps}


def cmd_expiry_profile(args) -> int:
    """每个交易日开盘前冻结记录各品种逐到期持仓画像（首份冻结，重复运行 exists）。只读、从不下单。"""
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    cfg, store = load_config(), SnapshotStore()
    session = market_today()
    if mc.is_trading_day(session) is not True:
        print(f"{session} 非交易日：不记录。")
        return 0
    now = datetime.now(timezone.utc)
    rc, done, issues = 0, [], []
    for inst in _instruments(cfg, args.instruments):
        sym = inst.options.symbol
        try:
            idx = session_index(store, sym)
            f = idx.get(session)
            if f is None:
                print(f"  {inst.key:6s} {session} 当日快照未到（未认证到该交易日），下次重试")
                continue
            payload, ident = store.load_with_identity("options", sym, f)
            row = expiry_profile_row(inst.key, sym, session, snapshot_from_payload(payload, inst.key, sym), ident, now)
            st = jl.insert_frozen(EXPIRY_PROFILE_DIR / f"{inst.key}.jsonl", row, key_field="key",
                                  frozen=lambda r: {k: v for k, v in r.items() if k not in ("recorded_at", "before_open")})
            q = [f"{e['expiry'][5:]}{e['type']}" for e in row["expiries"] if e["type"] in ("Q", "M")]
            print(f"  {inst.key:6s} {session} {st}；到期 {len(row['expiries'])} 个，其中月度/季度 {q}"
                  + ("" if row["before_open"] else "（⚠️ 开盘后记录）"))
            done.append(inst.key)
        except Exception as e:
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]}); rc = 1
    _status(args, "expiry-profile", done, issues)
    return rc


BARS_GAPS = Path("data/history/option_bars/_gaps.json")
BARS_EVIDENCE = Path("data/history/option_bars/_evidence.json")


def _expiry_of(sym: str):
    import re as _re
    m = _re.match(r"^[A-Z]+(\d{6})[CP]\d+\.US$", sym)
    return date(2000 + int(m.group(1)[:2]), int(m.group(1)[2:4]), int(m.group(1)[4:])) if m else None


def bars_gap_ledger(plan: dict, *, today: date) -> list[dict]:
    """逐项缺口台账（Codex 024-4）：交易日、到期日、所需字段、上次返回状态与时刻、当天逐分钟能否替代收盘、优先级。
    优先级：已有全时段逐分钟收盘的（只缺 OHLC）排后；其余按到期日由近到远（离「查不到」最近的先补），
    同到期按交易日由早到晚；已请求过返回查不到的排最后。「到期约一周后查不到」是本地观察，不是长桥承诺 —— 不保证配额恢复后一定补得回。"""
    from undertow.collect import longbridge_bars as lbb
    gaps = []
    for (root, day), syms in plan.items():
        try:
            cur = lbb.load_day(lbb.path_of(root, day)) or {"contracts": {}}
        except lbb.BarsFileCorrupt:
            cur = {"contracts": {}, "_corrupt": True}
        close_ok = lbb.intraday_covered(root, day, need="close")
        for sym in syms:
            v = cur["contracts"].get(sym)
            if v and v.get("status") in ("ok", "empty"):
                continue
            exp = _expiry_of(sym)
            gaps.append({"day": day.isoformat(), "root": root, "symbol": sym,
                         "expiry": exp.isoformat() if exp else None,
                         "days_since_expiry": (today - exp).days if exp else None,
                         "last_status": (v or {}).get("status", "never_requested"),
                         "last_attempt_at": (v or {}).get("fetched_at"),
                         "last_error": str((v or {}).get("error"))[:160] if v and v.get("error") else None,
                         "close_available_from_intraday": sym in close_ok,
                         "fields_missing": "ohlc" if sym in close_ok else "ohlc+close"})
    # 排序：①无任何替代且从未请求 ②从未请求但已有逐分钟收盘（只缺 OHLC）③已请求过、返回查不到（仅 --retry-missing 重查）；
    # 各组内按到期日由近到远、交易日由早到晚
    grp = lambda g: (0 if g["last_status"] == "never_requested" and not g["close_available_from_intraday"]
                     else 1 if g["last_status"] == "never_requested" else 2)
    gaps.sort(key=lambda g: (grp(g), g["expiry"] or "9999", g["day"]))
    for i, g in enumerate(gaps, 1):
        g["priority"] = i
    return gaps


INTRADAY_LOCK = Path("data/logs/.intraday.flock")


def intraday_plan_state(plan: dict, base) -> dict:
    """按【落盘后的整个计划】判状态（Codex 024-1）：逐代码回读文件 → complete / empty_confirmed /
    gone_confirmed / pending；坏文件整组记 corrupt。"""
    from undertow.collect import longbridge_bars as lbb
    counts = {"complete": 0, "empty_confirmed": 0, "gone_confirmed": 0, "pending": 0, "corrupt": 0}
    pend = []
    for (root, day), syms in plan.items():
        try:
            cur = lbb.load_day(lbb.path_of(root, day, base)) or {"contracts": {}}
        except lbb.BarsFileCorrupt:
            counts["corrupt"] += len(syms)
            continue
        for sym in syms:
            st = lbb.symbol_state(cur["contracts"].get(sym))
            counts[st] += 1
            if st == "pending":
                pend.append(sym)
    return {"counts": counts, "pending": sorted(pend)[:20]}


def cmd_intraday(args) -> int:
    """收盘后存【当天】候选合约与标的的逐分钟（longbridge intraday；不占按月计的历史 K 线配额）。

    用户 2026-09-28：「记住数据最重要」。Codex 024 修订：
    - 状态由【落盘后的整个计划】计算，区分「请求成功 / 数据可用 / 计划已覆盖」；只有全部代码进入终态
      （full_session 完成 / empty 连续 3 次 / 查不到连续 2 次）才 overall=complete、rc=0；否则 partial、rc=1，下次唤醒续补。
    - 质量标签 full_session / partial_session / empty / invalid；更差的一次不覆盖已有的 full_session；每次尝试都留痕。
    - 零计划区分：今天候选账尚未生成 → plan_unavailable（rc=1，重试）；账已生成但无候选腿 → no_candidates（终态）。
    - 进程级文件锁（fcntl.flock）：进程被杀由内核释放，不会留下失效锁；锁被占 → rc=4（上一轮仍在跑）。
    只读、从不下单。"""
    import fcntl
    import time as _time
    from undertow.collect import longbridge_bars as lbb
    today = market_today()
    now_et = datetime.now(ET)
    if mc.is_trading_day(today) is not True:
        print(f"{today} 非交易日：不抓当天逐分钟。")
        _status(args, "intraday", [], [], overall="unchanged", counts={"planned": 0})
        return 0
    if (now_et.hour, now_et.minute) < (16, 5) and not args.force:
        print(f"ET {now_et:%H:%M} 未到 16:05：当天逐分钟未完整，不抓（--force 可强制）。")
        _status(args, "intraday", [], [], overall="unchanged", counts={"planned": 0})
        return 0
    INTRADAY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lk = open(INTRADAY_LOCK, "a+")
    try:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("当天逐分钟：上一轮仍在运行（文件锁被占），本次跳过。")
        _status(args, "intraday", [], [], overall="busy", counts={"planned": None})
        lk.close()
        return 4
    try:
        return _intraday_locked(args, today, lbb, _time)
    finally:
        fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
        lk.close()


def _intraday_locked(args, today, lbb, _time) -> int:
    rows = []
    for d in (_vdir(False), _vdir(True)):
        for p in sorted(d.glob("*.jsonl")):
            rows += jl.load(p, KEY)
    today_rows = [r for r in rows if r.get("session") == today.isoformat()]
    plan = {k: v for k, v in bars_plan(rows, last_day=today).items() if k[1] == today}
    if not plan:
        overall = "no_candidates" if today_rows else "plan_unavailable"
        print(f"当天逐分钟 {today}：计划为空（{overall}：今日候选账{'已生成但无在场候选腿' if today_rows else '尚未生成'}）")
        _status(args, "intraday", [], [], overall=overall, counts={"planned": 0, "ledger_rows_today": len(today_rows)})
        return 0 if overall == "no_candidates" else 1
    n_req = n_ok = 0
    issues, done = [], []
    for (root, day), syms in sorted(plan.items()):
        path = lbb.path_of(root, day, lbb.INTRADAY_DIR)
        try:
            cur = lbb.load_day(path) or lbb.new_intraday_day(root, day)
        except lbb.BarsFileCorrupt as e:
            q = lbb.quarantine(path)
            print(f"  ⚠️ {e}；已隔离为 {q.name}，重新抓取（旧文件保留）", file=sys.stderr)
            cur = lbb.new_intraday_day(root, day)
        todo = sorted(s for s in syms if lbb.symbol_state(cur["contracts"].get(s)) == "pending")
        if not todo:
            continue
        try:
            for s in todo:
                res = lbb.fetch_intraday_today(s, day)
                cur["contracts"][s] = lbb.merge_attempt(cur["contracts"].get(s), res)
                n_req += 1
                n_ok += (res.get("quality") or {}).get("label") == "full_session"
                _time.sleep(INTRADAY_PACE_S)
        except (lbb.BarsUnavailable, lbb.BarsQuotaExhausted) as e:
            issues.append({"instrument": f"{root} {day}", "error": str(e)[:200]})
        if any(s in cur["contracts"] for s in todo):
            lbb.save_day(path, cur)
            done.append(f"{root} {day}")
        if len(issues) >= 3:
            print("  ⚠️ 连续故障，停止本次（下次从断点继续）", file=sys.stderr)
            break
    st = intraday_plan_state(plan, lbb.INTRADAY_DIR)             # 回读落盘文件，按整个计划判
    c = st["counts"]
    total = sum(len(v) for v in plan.values())
    terminal = c["complete"] + c["empty_confirmed"] + c["gone_confirmed"]
    overall = "complete" if terminal == total and not issues else ("failed" if terminal == 0 else "partial")
    print(f"当天逐分钟 {today}：计划 {len(plan)} 个（标的, 日）、{total} 个代码；本次请求 {n_req}（全时段 {n_ok}）；"
          f"整计划：完成 {c['complete']}、确认空 {c['empty_confirmed']}、确认查不到 {c['gone_confirmed']}、"
          f"待续 {c['pending']}、坏文件 {c['corrupt']} → {overall}")
    _status(args, "intraday", done, issues, overall=overall,
            counts={"planned": total, "requested_now": n_req, "full_session_now": n_ok, **c,
                    "pending_sample": st["pending"]})
    return 0 if overall == "complete" else 1


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
    fc = ss.add_parser("fieldcheck", help="每日现场核验（pre/open/close；session_hooks 自动触发，报告入 data/history/fieldcheck/）")
    fc.add_argument("--phase", choices=("pre", "open", "close"), required=True)
    fc.add_argument("--session", help="核验哪一天（默认今天 ET）"); fc.add_argument("--status-file")
    fc.set_defaults(func=cmd_fieldcheck)
    sp = ss.add_parser("sample", help="盘中时段采样：ET 09:45–13:00 每 15 分钟记在场候选腿盘口（与 v5 预登记无关）")
    sp.add_argument("instruments", nargs="*"); sp.add_argument("--status-file")
    sp.add_argument("--check", nargs="?", const="today", metavar="YYYY-MM-DD",
                    help="收尾核对：列出该日各品种各桶的缺失/失败（只读落盘记录）")
    sp.set_defaults(func=cmd_sample)
    b = ss.add_parser("bars", help="补候选价差的历史盘中成交价（长桥 1 分钟 K 线；到期约一周后查不到，尽早补）")
    b.add_argument("--since", help="只补 session ≥ 该日的机会行"); b.add_argument("--status-file")
    b.add_argument("--gaps", action="store_true", help="只写缺口台账（逐项到期日/字段/上次状态/优先级），不抓取")
    b.add_argument("--need", choices=("close", "ohlc"), default="close",
                   help="所需字段：close（默认；全时段逐分钟收盘已存的代码跳过，省配额）/ ohlc（分钟 OHLC，不跳过）")
    b.add_argument("--all", action="store_true", help="全部品种与规则（默认只补主池的 A、B1：长桥按不同代码数限额）")
    b.add_argument("--retry-missing", action="store_true",
                   help="重查此前记为 not_found / invalid_symbol 的合约日（默认不重查；「本次未取得」不等于永久不可得）")
    b.set_defaults(func=cmd_bars)
    ep = ss.add_parser("expiry-profile", help="开盘前冻结记录各品种逐到期持仓画像（类型 Q/M/W/D、OI、近价最大行权价）")
    ep.add_argument("instruments", nargs="*"); ep.add_argument("--status-file")
    ep.set_defaults(func=cmd_expiry_profile)
    it = ss.add_parser("intraday", help="收盘后存当天候选合约与标的的逐分钟成交价量（不占历史 K 线月配额）")
    it.add_argument("--status-file"); it.add_argument("--force", action="store_true", help="16:05 前也抓（盘中不完整）")
    it.set_defaults(func=cmd_intraday)
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
    dr = ss.add_parser("direction", help="方向次要分析（预登记 dir-analysis-v1.3；只读报告）")
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
