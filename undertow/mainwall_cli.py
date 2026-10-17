"""`undertow mainwall ...` 编排：主墙近侧卖方价差前瞻研究账（Codex 056 合同 v0.1；只读，从不下单）。

  capture  盘前：新鲜 T−1 输入（collect.fresh_daily）+ 当日快照 → 交易到期 E 与两侧主墙图，冻结一行。
           输入不新鲜/快照未到 → 只追加失败尝试（capture_attempts.jsonl），不占当天冻结键，下次唤醒重试；
           开盘后不再冻结（逾时输入不追补成盘前决定）。
  quote    --window open：当日行先观察并冻结 Sdec（长桥常规时段价）→ 选近侧、构造 A/B 候选（写入即冻结）→ 再取盘口；
           盘前没冻结成的品种在这里补一行机会母表（premarket 状态如实写缺失原因）。已在场的旧行同时记持仓标记。
           --window close：在场候选的收盘窗标记（主终点 pre_expiry_exit_policy 用到期前一交易日这一窗）。
  sample   盘中采样（与 shadow sample 同一时段）：标的价、到两侧冻结墙的距离、两侧 A 腿盘口。只记录，不触发交易。
  settle   收盘后派生每条候选的政策结果（v5 settle_leg 原样 + 收益门槛）与机会类别。
  report   机会母表与 A、B、A−B 描述（探索；12/31 只是独立阶段检查）。

墙位卖方价差实盘停用状态不变；本账只回答「主墙 vs 1·ATR 定距」这一个研究问题。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from undertow.analyze import mainwall as mw
from undertow.analyze import shadow as sh
from undertow.collect import jsonl_ledger as jl
from undertow.core import market_calendar as mc
from undertow.core.clock import ET, MARKET_OPEN, market_today

DIR = Path("data/history/mainwall")
KEY = "key"
POST_FIELDS = ("windows", "samples", "outcome", "settled_at")


def _vdir() -> Path:
    return DIR / mw.CONFIG["version"]


def _path(inst: str) -> Path:
    return _vdir() / f"{inst}.jsonl"


def _attempts_path() -> Path:
    return _vdir() / "capture_attempts.jsonl"


def frozen_part(row: dict) -> dict:
    """事前部分 = 除事后观测/派生字段以外的一切。entry_decision/legs 由 _set_entry 一次写入，之后同样冻结。"""
    return {k: v for k, v in row.items() if k not in POST_FIELDS}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code_sha() -> dict:
    return {p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()[:16]
            for p in ("undertow/analyze/mainwall.py", "undertow/collect/fresh_daily.py", "undertow/mainwall_cli.py")}


def _instruments(cfg, names):
    return [cfg.get(k) for k in (names or mw.CONFIG["instruments"])]


def _before_open(t: datetime, T: date) -> bool:
    t = t.astimezone(ET)
    return (t.date(), (t.hour, t.minute)) < (T, MARKET_OPEN)


class AttemptsCorrupt(RuntimeError):
    pass


ATTEMPT_REQUIRED = ("key", "instrument", "session", "status", "tried_at")


def _no_dup(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"重复字段 {sorted({k for k in keys if keys.count(k) > 1})}")
    return dict(pairs)


def _bad_const(x):
    raise ValueError(f"非有限数 {x}")


def validate_attempt(a) -> None:
    """一条失败尝试必须是对象、必需字段为非空字符串。同 key 多行（同日多次重试）是合法的，不套主账的唯一 key 规则。"""
    if not isinstance(a, dict):
        raise ValueError("不是 JSON 对象")
    for k in ATTEMPT_REQUIRED:
        if not isinstance(a.get(k), str) or not a[k]:
            raise ValueError(f"必需字段 {k} 缺失或非法")


def _parse_attempt_line(ln: str) -> dict:
    a = json.loads(ln, object_pairs_hook=_no_dup, parse_constant=_bad_const)
    validate_attempt(a)
    return a


def _quarantine(p: Path, raw: bytes) -> Path:
    import uuid
    q = p.with_name(p.name + f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:6]}")
    q.write_bytes(raw)
    return q


def _load_attempts() -> list:
    """严格读取失败尝试账（Codex 057 R3 / 058 C）：编码、JSON、重复字段、非有限数、对象结构、必需字段任一不合法
    → 原件不动、另存隔离副本、抛 AttemptsCorrupt。不静默跳过坏行。"""
    p = _attempts_path()
    if not p.exists():
        return []
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        q = _quarantine(p, raw)
        raise AttemptsCorrupt(f"capture_attempts 编码非法（隔离副本 {q.name}，原件未动）：{e}") from e
    out = []
    for i, ln in enumerate(text.splitlines()):
        try:
            out.append(_parse_attempt_line(ln))
        except ValueError as e:
            q = _quarantine(p, raw)
            raise AttemptsCorrupt(f"capture_attempts 第 {i + 1} 行不合法（隔离副本 {q.name}，原件未动）：{e}") from e
    return out


def _append_attempt(rec: dict) -> None:
    """失败尝试只追加（文件锁下，fsync 后回读末行核对），不占当天冻结键。写入前同一套校验（拒非有限数/缺字段）；
    追加前先严格读一遍：坏文件不在其后继续追加。"""
    import os
    validate_attempt(rec)
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True, allow_nan=False)
    _parse_attempt_line(line)
    p = _attempts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with jl.locked(p):
        _load_attempts()
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush(); os.fsync(f.fileno())
        if p.read_text("utf-8").splitlines()[-1] != line:
            raise OSError("capture_attempts 追加后回读不一致")


def _status(args, cmd, done, issues, *, overall=None, counts=None):
    for i in issues:
        print(f"  ⚠️ mainwall {cmd} {i['instrument']}: {i['error']}", file=sys.stderr)
    if getattr(args, "status_file", None):
        from undertow.collect.asof_history import atomic_write_json
        atomic_write_json(Path(args.status_file), {
            "schema": 2, "command": f"mainwall {cmd}", "ok": done, "issues": issues, "at": _now_iso(),
            "run_id": getattr(args, "run_id", None), "session": market_today().isoformat(),
            "overall": overall or ("failed" if issues and not done else "partial" if issues else "complete"),
            "counts": counts or {}})


# ── 盘前 ────────────────────────────────────────────────────────────────

def _snapshot_for(store, sym: str, T: date):
    """当日（文件日 = T）且在 T 开盘前抓取的快照；否则 (None, 原因)。快照只在 OI 结算落地后才落盘（cli 去重规则）。"""
    from undertow.collect.cboe_options import oi_change_total, snapshot_from_payload
    if T not in store.dates("options", sym):
        return None, "snapshot_missing", None
    payload, ident = store.load_with_identity("options", sym, T)
    if payload is None or not ident or not ident.get("captured_at"):
        return None, "snapshot_unreadable", None
    ca = datetime.fromtimestamp(ident["captured_at"], timezone.utc)
    if not _before_open(ca, T):
        return None, "snapshot_after_open", None
    snap = snapshot_from_payload(payload, None, sym)
    prev = [d for d in store.dates("options", sym) if d < T]
    oic = None
    if prev:
        pp = store.load("options", sym, prev[-1])
        oic = oi_change_total(snapshot_from_payload(pp, None, sym), snap) if pp else None
    d = payload.get("data") or {}
    ltt = str(d.get("last_trade_time") or "")[:10]
    spc = {"close": d.get("close") or d.get("prev_day_close"), "date": ltt or None}
    meta = {"file_date": T.isoformat(), "captured_at": ca.isoformat(), "sha256": ident.get("sha256"),
            "prev_file_date": prev[-1].isoformat() if prev else None, "oi_change_total": oic, "prev_close_field": spc}
    return snap, None, meta


REALTIME_TOL_S = 120


def _wall_now() -> datetime:
    return datetime.now(ET)


def _clock() -> datetime:
    """冻结判定用的时钟（测试可替换）。前瞻路径只认真实时钟：与墙钟相差 > REALTIME_TOL_S → 拒绝（离线回放不得进前瞻账）。"""
    return datetime.now(ET)


def _assert_realtime(t: datetime) -> None:
    if abs((t - _wall_now()).total_seconds()) > REALTIME_TOL_S:
        raise RuntimeError("离线回放不得写入前瞻账（时钟与墙钟不符）")


def _avail_times(inputs: dict, smeta: dict) -> list:
    """影响冻结决定的全部来源的可得时刻：所选序列、参与交叉核对的另一源、期权快照。缺任一 → 含 None。"""
    ts = [inputs.get("available_at"), smeta.get("captured_at")]
    ts += [c.get("available_at") for c in inputs.get("cross_check") or [] if c.get("against") in ("cboe_history", "yahoo_chart")]
    return ts


def capture_one(inst, T: date, *, store, cache=None, cas_put=None) -> tuple[str, dict]:
    """返回 (结果, 记录)。结果 ∈ inserted / exists / attempt_failed / late。

    时刻分开记（Codex 057 R2）：attempt_started_at（开始）→ inputs_consumed_at（输入取完）→ frozen_at（写入前再读时钟）。
    冻结条件：frozen_at 早于 T 开盘，且所有影响决定的来源可得时刻都已知、不晚于 frozen_at。开始于盘前不等于完成于盘前。
    """
    from undertow.collect import fresh_daily as fd
    sym = inst.options.symbol
    t0 = _clock(); _assert_realtime(t0)
    att = {"key": f"{T.isoformat()}|{inst.key}", "instrument": inst.key, "session": T.isoformat(),
           "tried_at": t0.isoformat(), "attempt_started_at": t0.isoformat(), "config_version": mw.CONFIG["version"]}
    if not _before_open(t0, T):
        att["status"] = "late"
        return "late", att
    snap, why, smeta = _snapshot_for(store, sym, T)
    if snap is None:
        att["status"] = why
        return "attempt_failed", att
    rec, ser = fd.resolve(inst, T, cache=cache, snapshot_prev_close=smeta["prev_close_field"], cas_put=cas_put)
    inputs = mw.derive_inputs(rec, ser, T)
    t1 = _clock()
    att["inputs_consumed_at"] = t1.isoformat()
    att["inputs"] = {k: inputs.get(k) for k in ("status", "source", "expected_base_date", "base_date", "attempts",
                                                "cross_check", "coverage", "incomplete_reason")}
    if inputs.get("status") != "fresh":
        att["status"] = f"input_{inputs.get('status')}"
        return "attempt_failed", att
    E = sh.target_expiry(snap, T, tuple(mw.CONFIG["dte"]))
    cprev = inputs["close"]
    pre = {"status": "ok", "inputs": inputs, "snapshot": smeta, "E": E.isoformat() if E else None,
           "attempt_started_at": t0.isoformat(), "inputs_consumed_at": t1.isoformat(), "code_sha": _code_sha()}
    if E is None:
        pre.update(status="no_expiry")
    else:
        pre["walls"] = {k: mw.wall_map(snap.contracts, T, E, cprev, k) for k in ("P", "C")}
        pre["strikes"] = {k: sh.listed_strikes(snap, k, E) for k in ("P", "C")}
        pre["widths"] = {k: sh.spread_width(pre["strikes"][k], cprev, mw.CONFIG["width"]["band"]) for k in ("P", "C")}
    t2 = _clock()
    avail = _avail_times(inputs, smeta)
    if not _before_open(t2, T):
        att.update(status="late_completion", frozen_check_at=t2.isoformat())
        return "attempt_failed", att
    if any(a is None for a in avail) or any(datetime.fromisoformat(a) > t2 for a in avail):
        att.update(status="input_availability_unproven", frozen_check_at=t2.isoformat(), available=avail)
        return "attempt_failed", att
    pre["frozen_at"] = pre["recorded_at"] = t2.isoformat()
    row = {"key": att["key"], "schema": 1, "instrument": inst.key, "symbol": sym, "session": T.isoformat(),
           "config_version": mw.CONFIG["version"], "config_hash": mw.config_hash(), "premarket": pre,
           "entry_decision": None, "legs": [], "windows": {}, "samples": [], "outcome": None}
    r = jl.insert_frozen(_path(inst.key), row, key_field=KEY, frozen=frozen_part)
    return r, row


def cmd_capture(args) -> int:
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    cfg = load_config(); store = SnapshotStore()
    now = _clock(); T = now.date()
    if mc.is_trading_day(T) is not True:
        print(f"  {T} 非交易日（或日历未覆盖），不冻结")
        _status(args, "capture", [], [], overall="unchanged")
        return 0 if mc.is_trading_day(T) is False else 3
    if not _before_open(now, T):
        # 逾时输入不追补成盘前决定；开盘窗会按最后一次失败尝试补机会母表行。这里不再追加尝试（免得每次唤醒刷一条）
        print(f"  {T} 已开盘，不再冻结盘前行")
        _status(args, "capture", [], [], overall="unchanged")
        return 0
    done, issues, pend = [], [], []
    for inst in _instruments(cfg, args.instruments):
        if any(r[KEY] == f"{T.isoformat()}|{inst.key}" for r in jl.load(_path(inst.key), KEY)):
            done.append(inst.key); print(f"  {inst.key:7s} {T} 已冻结"); continue
        try:
            from undertow.collect import cas as _cas
            r, rec = capture_one(inst, T, store=store, cas_put=_cas.put)
        except Exception as e:
            r, rec = "attempt_failed", {"key": f"{T.isoformat()}|{inst.key}", "instrument": inst.key,
                                        "session": T.isoformat(), "tried_at": now.isoformat(),
                                        "status": f"error:{type(e).__name__}", "error": str(e)[:200]}
        if r in ("inserted", "exists"):
            w = rec["premarket"].get("walls") or {}
            desc = " ".join(f"{k}墙 {w[k].get('strike')}({w[k].get('oi')})" for k in ("P", "C") if w.get(k, {}).get("status") == "ok")
            print(f"  {inst.key:7s} {T} 冻结 E={rec['premarket']['E']} 基准 {rec['premarket']['inputs']['base_date']}"
                  f" 收 {rec['premarket']['inputs']['close']}（{rec['premarket']['inputs']['source']}） {desc}")
            done.append(inst.key)
        else:
            try:
                _append_attempt(rec)
            except (AttemptsCorrupt, OSError) as e:
                issues.append({"instrument": inst.key, "error": f"attempts_log: {e}"[:200]})
            pend.append(inst.key)
            issues.append({"instrument": inst.key, "error": rec.get("status")})
    overall = "complete" if not pend else ("partial" if done else "failed")
    _status(args, "capture", done, issues, overall=overall)
    return 0 if overall == "complete" else 1


# ── 入场窗 / 标记窗 ───────────────────────────────────────────────────────

def _set_entry(path: Path, key: str, decision: dict, legs: list) -> str:
    """entry_decision 与 legs 只写一次（锁内读→判→写）；已存在 → exists，不改。"""
    with jl.locked(path):
        rows = jl.load_path(path, KEY)
        for r in rows:
            if r[KEY] == key:
                if r.get("entry_decision") is not None:
                    return "exists"
                r["entry_decision"], r["legs"] = decision, legs
                jl.atomic_write(path, rows, KEY)
                return "set"
    raise KeyError(key)


def _ensure_denominator_row(inst, T: date) -> None:
    """盘前没冻结成的品种：开盘后补一行，机会母表不能缺这一天（原因取最后一次失败尝试）。"""
    p = _path(inst.key)
    key = f"{T.isoformat()}|{inst.key}"
    if any(r[KEY] == key for r in jl.load(p, KEY)):
        return
    last, corrupt = None, None
    try:
        for a in _load_attempts():
            if a.get("key") == key:
                last = a
    except AttemptsCorrupt as e:
        corrupt = str(e)
    st = ("premarket_attempts_corrupt" if corrupt else
          f"premarket_{last['status']}" if last and last.get("status") else "premarket_not_run")
    row = {"key": key, "schema": 1, "instrument": inst.key, "symbol": inst.options.symbol, "session": T.isoformat(),
           "config_version": mw.CONFIG["version"], "config_hash": mw.config_hash(),
           "premarket": {"status": st, "last_attempt_at": (last or {}).get("tried_at"), "recorded_at": _now_iso(),
                         **({"error": corrupt} if corrupt else {})},
           "entry_decision": None, "legs": [], "windows": {}, "samples": [], "outcome": None}
    jl.insert_frozen(p, row, key_field=KEY, frozen=frozen_part)


def _decide(row: dict, sdec: float, sdec_meta: dict) -> tuple[dict, list]:
    pre = row["premarket"]
    atr = pre["inputs"]["atr14"]
    E = date.fromisoformat(pre["E"])
    choice = mw.choose_side(pre["walls"], sdec, atr)
    legs = mw.candidates(pre["strikes"], E, sdec, atr, pre["walls"], pre["widths"], choice)
    return {"sdec": sdec, **sdec_meta, "atr14": atr, "choice": choice, "frozen_at": _now_iso()}, legs


def _quote_legs(root: str, legs_by_row: list) -> tuple[dict, dict | None, str, str]:
    from undertow.collect.longbridge_options import _lb_symbol
    from undertow.collect.longbridge_quote import fetch_depth, fetch_stock_quotes
    need = {}
    for _, l in legs_by_row:
        for K in (l["sell"], l["buy"]):
            need[(sh.qkey(l["side"], K), l["expiry"])] = _lb_symbol(root, date.fromisoformat(l["expiry"]), l["side"], K)
    t0 = _now_iso()
    try:
        dep = fetch_depth(sorted(set(need.values())))
    except Exception as e:
        dep = {"__error__": f"{type(e).__name__}: {e}"[:200]}
    try:
        uq = fetch_stock_quotes([f"{root}.US"]).get(f"{root}.US")
        under = {"freshest": uq.freshest, "kind": uq.freshest_kind, "last": uq.last, "timestamp": uq.timestamp} if uq else None
    except Exception as e:
        under = {"error": type(e).__name__}
    return need, dep, under, (t0, _now_iso())


def _live_legs(r: dict, T: date) -> list:
    return [l for l in r.get("legs") or [] if l.get("status") == "candidate" and r["session"] <= T.isoformat() <= l["expiry"]]


def _leg_valid(r: dict, l: dict, wkey: str, window: str, T: date) -> bool:
    purpose = "entry" if (window == "open" and r["session"] == T.isoformat()) else "exit"
    view = mw.row_view_after(r, (r.get("entry_decision") or {}).get("frozen_at"))
    return sh.window_leg(view, l, wkey, purpose)["status"] == "valid"


def cmd_quote(args) -> int:
    from undertow.collect.longbridge_quote import fetch_stock_quotes
    from undertow.core.config import load_config
    from undertow.shadow_cli import _phase_now, _window_now
    window = args.window
    if _phase_now() != "rth" or not _window_now(window):
        print(f"不在 {window} 窗口或非盘中（ET）；主墙账不记窗外报价", file=sys.stderr)
        return 2
    cfg = load_config(); T = market_today(); wkey = f"{T.isoformat()}|{window}"
    counts = {"expected_legs": 0, "valid_legs": 0, "decided": 0}
    done, issues = [], []
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key); root = inst.options.symbol
        try:
            if window == "open":
                _ensure_denominator_row(inst, T)
                row = next(r for r in jl.load(p, KEY) if r["session"] == T.isoformat())
                if row["premarket"].get("status") not in ("ok", "no_expiry"):
                    # 盘前没冻结成 = 本该发生却没发生：作为 issue 让窗口保持未完成并在窗末提醒（不只写进账本）
                    issues.append({"instrument": inst.key, "error": row["premarket"].get("status")})
                if row["premarket"].get("status") == "ok" and row.get("entry_decision") is None \
                        and row["premarket"].get("walls"):
                    q0 = _now_iso()
                    uq = fetch_stock_quotes([f"{root}.US"]).get(f"{root}.US")
                    q1 = _now_iso()
                    if uq is None or uq.freshest_kind != "常规" or not (uq.last and uq.last > 0):
                        raise ValueError(f"标的常规时段价不可用（{getattr(uq, 'freshest_kind', None)}）")
                    if not sh.in_window(wkey, q0, q1):
                        # Codex 057 R2：取回跨过窗末（或窗前）不冻结成窗内 Sdec
                        raise ValueError(f"Sdec 取回时刻不在开盘窗内（{q0} → {q1}）")
                    dec, legs = _decide(row, float(uq.last), {"sdec_requested_at": q0, "sdec_at": q1,
                                                              "sdec_kind": uq.freshest_kind, "sdec_quote_time": uq.timestamp,
                                                              "sdec_source": "longbridge quote last"})
                    if _set_entry(p, row[KEY], dec, legs) == "set":
                        counts["decided"] += 1
                        print(f"  {inst.key:7s} Sdec {dec['sdec']} 近侧 {dec['choice']['near']}（{dec['choice']['reason']}）")
            todo = []
            for r in jl.load(p, KEY):
                for l in _live_legs(r, T):
                    counts["expected_legs"] += 1
                    if _leg_valid(r, l, wkey, window, T):
                        counts["valid_legs"] += 1
                    else:
                        todo.append((r[KEY], l))
            if not todo:
                done.append(inst.key); continue
            need, dep, under, (t0, t1) = _quote_legs(root, todo)
            err = dep.pop("__error__", None)
            if err:
                issues.append({"instrument": inst.key, "error": err})
            keys = {k for k, _ in todo}

            def fn(r):
                if r[KEY] not in keys:
                    return False
                quotes = {}
                for l in r.get("legs") or []:
                    for K in (l.get("sell"), l.get("buy")):
                        sym = need.get((sh.qkey(l["side"], K), l["expiry"])) if K is not None else None
                        if sym is None:
                            continue
                        d = dep.get(sym)
                        quotes[sh.qkey(l["side"], K)] = (
                            {"bid": d.bid, "ask": d.ask, "bid_size": d.bid_size, "ask_size": d.ask_size,
                             "error": d.error or None, "quote_time": None} if d is not None else
                            {"bid": None, "ask": None, "bid_size": 0, "ask_size": 0, "error": "not_returned", "quote_time": None})
                if not quotes:
                    return False
                r.setdefault("windows", {}).setdefault(wkey, {"attempts": []})["attempts"].append(
                    {"started_at": t0, "ended_at": t1, "phase": _phase_now(), "underlying": under, "quotes": quotes})
                return True
            jl.update(p, fn, key_field=KEY, frozen=frozen_part)
            ids = {(k, l["leg_id"]) for k, l in todo}
            counts["valid_legs"] += sum(1 for r in jl.load(p, KEY) for l in _live_legs(r, T)
                                        if (r[KEY], l["leg_id"]) in ids and _leg_valid(r, l, wkey, window, T))
            done.append(inst.key)
        except Exception as e:
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]})
    if window == "open":
        missing = [i.key for i in _instruments(cfg, args.instruments)
                   if not any(r["session"] == T.isoformat() and r.get("entry_decision") is not None
                              for r in jl.load(_path(i.key), KEY))
                   and any(r["session"] == T.isoformat() and (r.get("premarket") or {}).get("status") == "ok"
                           for r in jl.load(_path(i.key), KEY))]
        for m in missing:
            if not any(i["instrument"] == m for i in issues):
                issues.append({"instrument": m, "error": "sdec_not_frozen"})
    if counts["expected_legs"] == 0 and not issues:
        overall = "unchanged"
    elif counts["valid_legs"] >= counts["expected_legs"] and not issues:
        overall = "complete"
    else:
        overall = "partial" if counts["valid_legs"] > 0 else "failed"
    print(f"  主墙账 {wkey}：应有 {counts['expected_legs']} 个候选价差，有效 {counts['valid_legs']}，新冻结 Sdec {counts['decided']} → {overall}")
    _status(args, f"quote-{window}", done, issues, overall=overall, counts=counts)
    return 0 if overall in ("complete", "unchanged") else 1


# ── 盘中采样 ────────────────────────────────────────────────────────────

def _sample_members(pre: dict) -> tuple[list, dict]:
    """本行盘中采样的适用成员：两侧冻结墙的 A 腿两个合约。不适用的侧给原因（无墙 / 墙不挂牌 / 无保护腿 / 无宽度）。"""
    legs, na = [], {}
    for k in ("P", "C"):
        w, W = pre["walls"][k], pre["widths"].get(k)
        if w.get("status") != "ok":
            na[k] = "no_wall"
        elif not W:
            na[k] = "no_width"
        elif w["strike"] not in pre["strikes"][k]:
            na[k] = "wall_strike_not_listed_for_E"
        else:
            buy = sh._buy_leg(pre["strikes"][k], w["strike"], k, W)
            if buy is None:
                na[k] = "no_same_width_protective"
            else:
                legs.append({"side": k, "sell": w["strike"], "buy": buy, "expiry": pre["E"]})
    return legs, na


def _slot_bounds(T: date, slot: str, bd: tuple) -> tuple[datetime, datetime]:
    from undertow.shadow_cli import SAMPLE
    m0 = int(slot[:2]) * 60 + int(slot[3:])
    m1 = min(m0 + SAMPLE["step_min"], bd[1])
    mk = lambda m: datetime(T.year, T.month, T.day, m // 60, m % 60, tzinfo=ET)  # noqa: E731
    return mk(m0), mk(m1)


def _in_slot(iso: str | None, lo: datetime, hi: datetime) -> bool:
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return False
    return lo <= t < hi


def _slot_state(row: dict, slot: str, members: list, lo: datetime, hi: datetime) -> dict:
    """本桶各成员首个【桶内收到】的成功观测时刻（Codex 058 B）。资格按成员自己的 received_at；
    旧格式没有逐成员时刻的，保守要求整批 started_at 与 ended_at 都在桶内。跨桶返回的观测保留在原始尝试里，但不算本桶，
    也不倒填到下一桶。不同成员的时刻可能不同，不拼成同批。"""
    got = {"underlying": None, **{m: None for m in members}}
    for a in row.get("samples") or []:
        if a.get("slot") != slot:
            continue
        env = _in_slot(a.get("started_at"), lo, hi) and _in_slot(a.get("ended_at"), lo, hi)
        u = a.get("underlying") or {}
        if got["underlying"] is None and u.get("last") and (_in_slot(u["received_at"], lo, hi) if "received_at" in u else env):
            got["underlying"] = u.get("received_at") or a["started_at"]
        for m_, q in (a.get("quotes") or {}).items():
            ok_t = _in_slot(q["received_at"], lo, hi) if "received_at" in q else env
            if m_ in got and got[m_] is None and q.get("observed") and ok_t:
                got[m_] = q.get("received_at") or a["started_at"]
    return got


def cmd_sample(args) -> int:
    """盘中采样（Codex 057 R4 / 058 B）：标的与期权分开取、分开记、逐成员记请求与收到时刻。每个 15 分钟桶内：标的价 +
    到两侧冻结墙的有符号 ATR 距离，两侧 A 腿合约盘口。每次唤醒只补本桶尚无【桶内收到】成功观测的成员；每次尝试原样追加。
    没有适用期权腿时照样记标的与距离。成员齐全 → complete；部分 → partial（rc=1，下一次唤醒补）；不触发任何交易。
    一侧无挂单（bid/ask 为空但无错误）算已观测；取数失败 / 未返回 / 收到时已出桶（out_of_slot）不算。"""
    from undertow.collect.longbridge_options import _lb_symbol
    from undertow.collect.longbridge_quote import fetch_depth, fetch_stock_quotes
    from undertow.core.config import load_config
    from undertow.shadow_cli import _phase_now, sample_bounds, sample_slot
    t = datetime.now(ET); T = t.date()
    bd = sample_bounds(T)
    slot = sample_slot(t.hour * 60 + t.minute, bd) if bd else None
    if slot is None or _phase_now() != "rth":
        print("不在盘中采样时段", file=sys.stderr)
        return 2
    lo, hi = _slot_bounds(T, slot, bd)
    cfg = load_config(); done, issues = [], []
    counts = {"members": 0, "observed": 0}
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key); root = inst.options.symbol
        row = next((r for r in jl.load(p, KEY) if r["session"] == T.isoformat()), None)
        pre = (row or {}).get("premarket") or {}
        if not row or pre.get("status") != "ok" or not pre.get("walls"):
            continue                                          # 当天无冻结墙图：无采样对象（盘前缺口由覆盖核查报告）
        legs, na = _sample_members(pre)
        sym_of = {}
        for l in legs:
            for K in (l["sell"], l["buy"]):
                sym_of[sh.qkey(l["side"], K)] = _lb_symbol(root, date.fromisoformat(l["expiry"]), l["side"], K)
        got = _slot_state(row, slot, list(sym_of), lo, hi)
        need = [m_ for m_, v in got.items() if v is None]
        counts["members"] += len(got)
        if not need:
            counts["observed"] += len(got); done.append(inst.key); continue
        t0 = _now_iso()
        under = None
        if "underlying" in need:
            rq = _now_iso()
            try:
                uq = fetch_stock_quotes([f"{root}.US"]).get(f"{root}.US")
                under = ({"last": uq.last, "kind": uq.freshest_kind, "timestamp": uq.timestamp} if uq and uq.last
                         else {"error": "not_returned"})
            except Exception as e:
                under = {"error": type(e).__name__}
            under.update(requested_at=rq, received_at=_now_iso())
            under["out_of_slot"] = not _in_slot(under["received_at"], lo, hi)
        quotes = {}
        for m_ in [x for x in need if x != "underlying"]:
            rq = _now_iso()                                   # 逐合约单独请求（depth 本就一次一个代码），各记收到时刻
            try:
                d = fetch_depth([sym_of[m_]]).get(sym_of[m_]); err = None
            except Exception as e:
                d, err = None, type(e).__name__
            rv = _now_iso()
            ok = d is not None and not d.error
            quotes[m_] = ({"bid": d.bid, "ask": d.ask, "bid_size": d.bid_size, "ask_size": d.ask_size,
                           "error": d.error or None, "observed": ok} if d is not None
                          else {"error": err or "not_returned", "observed": False})
            quotes[m_].update(requested_at=rq, received_at=rv, out_of_slot=not _in_slot(rv, lo, hi))
        px = (under or {}).get("last") if under and not under.get("out_of_slot") else None
        atr = pre["inputs"]["atr14"]
        # signed_dist_atr：>0 = 墙仍在虚值侧（未越过），≤0 = 已到墙上或越过；标的收到时已出桶 → 不算距离
        dist = {k: (round((px - pre["walls"][k]["strike"]) / atr * (1 if k == "P" else -1), 6)
                    if px and pre["walls"][k].get("status") == "ok" else None) for k in ("P", "C")}
        rec = {"slot": slot, "started_at": t0, "ended_at": _now_iso(), "underlying": under, "signed_dist_atr": dist,
               "quotes": quotes, "not_applicable": na, "requested": need}

        def fn(r, rec=rec):
            if r["session"] != T.isoformat():
                return False
            r.setdefault("samples", []).append(rec)
            return True
        jl.update(p, fn, key_field=KEY, frozen=frozen_part)
        after = _slot_state(next(r for r in jl.load(p, KEY) if r["session"] == T.isoformat()), slot, list(sym_of), lo, hi)
        miss = [m_ for m_, v in after.items() if v is None]
        counts["observed"] += len(after) - len(miss)
        if miss:
            issues.append({"instrument": inst.key, "error": "missing:" + ",".join(miss)})
        else:
            done.append(inst.key)
    overall = ("unchanged" if counts["members"] == 0 else "complete" if not issues
               else "partial" if counts["observed"] else "failed")
    print(f"  主墙账盘中采样 {slot}：成员 {counts['members']}，已观测 {counts['observed']} → {overall}")
    _status(args, "sample", done, issues, overall=overall, counts=counts)
    return 0 if overall in ("complete", "unchanged") else 1


# ── 结算 / 报告 ─────────────────────────────────────────────────────────

def derive_row(row: dict, bars: list, now=None) -> dict:
    """一行的政策结果。确定弃权（无墙 / 已越墙 / 等距 / 无到期）→ A = B = A−B = 0；
    缺输入、缺报价、未成熟、残腿未知 → None（另给 bounds），不为填表补 0（Codex 057 R3）。"""
    outs = {l["leg_id"]: mw.leg_outcome(l, row, bars=bars, now=now) for l in row.get("legs") or []}
    legs = {l["leg_id"]: l for l in row.get("legs") or []}
    cat = mw.row_category(row, outs)
    ed = row.get("entry_decision") or {}
    near = (ed.get("choice") or {}).get("near")
    basis = mw.CONFIG["primary_basis"]

    def pb(o):
        if o is None:
            return None
        if o.get("policy_pnl") is not None:
            return [o["policy_pnl"], o["policy_pnl"]]
        return o.get("policy_bounds")
    if cat in mw.DETERMINED_ABSTAIN:
        a = b = 0.0; ab = bb = [0.0, 0.0]
    else:
        oa, ob = (outs.get(f"near-{near}-A"), outs.get(f"near-{near}-B")) if near else (None, None)
        a = (oa or {}).get("policy_pnl"); b = (ob or {}).get("policy_pnl")
        ab, bb = pb(oa), pb(ob)
    slim = {lid: {k: v for k, v in o.items() if k != "settle"} | {"pnl_bounds": (o.get("settle") or {}).get("pnl_bounds", {}).get(basis)}
            for lid, o in outs.items()}
    return {"category": cat, "legs": slim, "A": a, "B": b,
            "A_minus_B": (round(a - b, 4) if a is not None and b is not None else None),
            "A_bounds": ab, "B_bounds": bb, "A_minus_B_bounds": mw.interval_diff(ab, bb),
            "condor": mw.condor(outs, legs), "derived_at": _now_iso()}


def cmd_settle(args) -> int:
    from undertow.collect.cboe_history import CboeHistorySource
    from undertow.core.config import load_config
    cfg = load_config(); px = CboeHistorySource(); done, issues = [], []
    now = datetime.now(ET)
    for inst in _instruments(cfg, args.instruments):
        p = _path(inst.key)
        if not p.exists():
            continue
        try:
            ser = px.fetch_series(inst)
            bars = list(zip(ser.dates, ser.highs, ser.lows, ser.closes))

            def fn(r):
                if r["session"] > now.date().isoformat():
                    return False
                o = derive_row(r, bars, now)
                if r.get("outcome") and {k: v for k, v in r["outcome"].items() if k != "derived_at"} == \
                        {k: v for k, v in o.items() if k != "derived_at"}:
                    return False
                r["outcome"] = o; r["settled_at"] = _now_iso()
                return True
            n = jl.update(p, fn, key_field=KEY, frozen=frozen_part)
            done.append(inst.key); print(f"  {inst.key:7s} 更新 {n} 行")
        except Exception as e:
            issues.append({"instrument": inst.key, "error": f"{type(e).__name__}: {e}"[:200]})
    _status(args, "settle", done, issues)
    return 0 if not issues else 1


# ── 已到截止的应有覆盖（Codex 058 A）──────────────────────────────────────
# 不靠「正好在窗末醒来」：每次唤醒（session_hooks）与每日任务都把起点以来所有已过截止的应有项重算一遍，
# 新发现的缺口持久登记（coverage_gaps.jsonl）、先写兜底凭证再标已提醒。未到截止 → 不报；休市 / 无到期 / 无候选 → 不适用。
# 三个维度分开：盘前身份（冻结、Sdec）/ 主终点观测（入场窗、到期前一交易日收盘窗盘口）/ 描述采样（盘中 15 分钟桶）。
# 描述采样缺桶不抹掉合法的 A/B；缺行情不补 0。

def _gaps_path() -> Path:
    return _vdir() / "coverage_gaps.jsonl"


def _deadline(T: date, hhmm: tuple) -> datetime:
    return datetime(T.year, T.month, T.day, hhmm[0], hhmm[1], tzinfo=ET)


def _observed_in_window(row: dict, leg: dict, wkey: str, after: str | None) -> bool:
    """窗口内（含发出与返回）有一次 rth 尝试拿到了两腿报价（无取数错误）；质量是否合格另由结算判断，不算覆盖缺口。"""
    for a in ((row.get("windows") or {}).get(wkey) or {}).get("attempts") or []:
        if a.get("phase") != "rth" or not sh.in_window(wkey, a.get("started_at"), a.get("ended_at")):
            continue
        if after and not sh._after(a.get("started_at"), after):
            continue
        q = a.get("quotes") or {}
        sq, bq = q.get(sh.qkey(leg["side"], leg["sell"])), q.get(sh.qkey(leg["side"], leg["buy"]))
        if sq and bq and not sq.get("error") and not bq.get("error"):
            return True
    return False


def coverage_items(row: dict | None, T: date, inst: str, now: datetime) -> list[dict]:
    """(交易日, 品种) 已过截止的应有项。status ∈ complete / missing / partial / not_applicable。未到截止的不列。"""
    from undertow.shadow_cli import sample_bounds, sample_slots
    out = []

    def add(section, item, status, deadline, reason=None):
        out.append({"id": f"{T.isoformat()}|{inst}|{item}", "session": T.isoformat(), "instrument": inst,
                    "section": section, "item": item, "status": status, "reason": reason,
                    "deadline": deadline.isoformat()})
    d_cap = _deadline(T, MARKET_OPEN)
    if now < d_cap:
        return out
    pre = (row or {}).get("premarket") or {}
    if row is None:
        add("premarket", "capture", "missing", d_cap, "not_generated")
    elif pre.get("status") in ("ok", "no_expiry"):
        add("premarket", "capture", "complete", d_cap)
    else:
        add("premarket", "capture", "missing", d_cap, pre.get("status"))
    w_open = sh.window_end(f"{T.isoformat()}|{mw.CONFIG['entry_window']}")
    if w_open is None or now <= w_open:
        return out
    usable = row is not None and pre.get("status") == "ok" and pre.get("walls")
    if not usable:
        add("premarket", "sdec", "not_applicable", w_open, "no_frozen_wall_map")
    elif (row.get("entry_decision") or {}).get("sdec") is not None:
        add("premarket", "sdec", "complete", w_open)
    else:
        add("premarket", "sdec", "missing", w_open, "sdec_not_frozen")
    ed = (row or {}).get("entry_decision") or {}
    wkey = f"{T.isoformat()}|{mw.CONFIG['entry_window']}"
    for l in (row or {}).get("legs") or []:
        if l.get("status") != "candidate":
            continue
        ok = _observed_in_window(row, l, wkey, ed.get("frozen_at"))
        add("endpoint", f"entry:{l['leg_id']}", "complete" if ok else "missing", w_open, None if ok else "no_quote_in_window")
        X = mc.prev_trading_day(date.fromisoformat(l["expiry"]))
        if X is None or X < T:
            continue
        xk = f"{X.isoformat()}|close"
        xe = sh.window_end(xk)
        if xe is None or now <= xe:
            continue
        if not ok:
            add("endpoint", f"exit:{l['leg_id']}", "not_applicable", xe, "no_entry_quote")
            continue
        okx = _observed_in_window(row, l, xk, None)
        add("endpoint", f"exit:{l['leg_id']}", "complete" if okx else "missing", xe, None if okx else "no_quote_in_window")
    bd = sample_bounds(T)
    if bd and usable:
        legs, _na = _sample_members(pre)
        members = [sh.qkey(l["side"], K) for l in legs for K in (l["sell"], l["buy"])]
        for slot in sample_slots(bd):
            lo, hi = _slot_bounds(T, slot, bd)
            if now < hi:
                continue
            got = _slot_state(row, slot, members, lo, hi)
            miss = [m_ for m_, v in got.items() if v is None]
            st = "complete" if not miss else ("missing" if len(miss) == len(got) else "partial")
            add("sample", f"sample:{slot}", st, hi, None if not miss else "missing:" + ",".join(miss))
    return out


def coverage_all(insts: list, now: datetime) -> list[dict]:
    start = date.fromisoformat(mw.CONFIG["prospective_start"])
    if now.date() < start:
        return []
    days = mc.trading_days(start, now.date())
    if days is None:
        raise ValueError("交易日历不覆盖覆盖核查区间")
    out = []
    for k in insts:
        rows = {r["session"]: r for r in jl.load(_path(k), KEY)}
        for d in days:
            out += coverage_items(rows.get(d.isoformat()), d, k, now)
    return out


def _gap_frozen(g: dict) -> dict:
    return {k: v for k, v in g.items() if k not in ("detected_at", "alerted_at", "fallback_file")}


def cmd_coverage(args) -> int:
    """重算起点以来所有已过截止的应有项；新缺口持久登记，先写兜底凭证（data/reports/FAILURE_<ET日>.txt，只写状态）
    再标已提醒。输出最后一行 `NEW_GAPS n`（调度层据此弹通知；通知失败不影响兜底）。rc：0 无缺口 / 1 有缺口（新旧都算）。"""
    import os
    from undertow.core.config import load_config
    cfg = load_config()
    now = _clock()
    items = coverage_all([i.key for i in _instruments(cfg, args.instruments)], now)
    gaps = [g for g in items if g["status"] in ("missing", "partial")]
    gp = _gaps_path(); gp.parent.mkdir(parents=True, exist_ok=True)
    for g in gaps:
        g2 = {**g, "key": g["id"], "detected_at": now.isoformat(), "alerted_at": None}
        jl.insert_frozen(gp, g2, key_field="key", frozen=lambda x: _gap_frozen({k: v for k, v in x.items()}))
    pending = [r for r in (jl.load(gp, "key") if gp.exists() else []) if not r.get("alerted_at")]
    if pending:
        f = Path("data/reports") / f"FAILURE_{now.date().isoformat()}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            for r in pending:
                fh.write(f"{now:%Y-%m-%d %H:%M %z} | ⚠️ 主墙研究账覆盖缺口 {r['id']}（{r['section']}，{r['status']}，{r.get('reason')}），"
                         f"见 undertow mainwall audit\n")
            fh.flush(); os.fsync(fh.fileno())
        ids = {r["key"] for r in pending}

        def mark(r):
            if r["key"] in ids and not r.get("alerted_at"):
                r["alerted_at"] = now.isoformat(); r["fallback_file"] = str(f)
                return True
            return False
        jl.update(gp, mark, key_field="key", frozen=_gap_frozen)
    by = {}
    for g in items:
        by.setdefault(g["section"], {}).setdefault(g["status"], 0)
        by[g["section"]][g["status"]] += 1
    lab = {"premarket": "盘前身份", "endpoint": "主终点观测", "sample": "描述采样"}
    for sec in ("premarket", "endpoint", "sample"):
        print(f"  {lab[sec]}覆盖：" + ("，".join(f"{k} {v}" for k, v in sorted(by.get(sec, {}).items())) or "（尚无已到截止项）"))
    order = {"premarket": 0, "endpoint": 1, "sample": 2}
    for g in sorted(gaps, key=lambda x: (order[x["section"]], x["id"]))[:10]:     # 盘前/主终点缺口优先显示
        print(f"  ⚠️ {g['id']} {g['status']} {g.get('reason')}")
    print(f"NEW_GAPS {len(pending)}")
    _status(args, "coverage", ["coverage"], [{"instrument": g["instrument"], "error": g["id"]} for g in gaps][:20],
            overall="complete" if not gaps else "partial", counts={"gaps": len(gaps), "new": len(pending)})
    return 1 if gaps else 0


def _due_until(now: datetime) -> date | None:
    """已应发生的最后一个交易日：今天开盘窗结束之后算今天，否则上一个交易日。"""
    T = now.date()
    bd = sh.window_bounds(T, mw.CONFIG["entry_window"])
    if mc.is_trading_day(T) and bd and now.hour * 60 + now.minute > bd[1]:
        return T
    return mc.prev_trading_day(T)


def _table(args):
    from undertow.core.config import load_config
    cfg = load_config()
    insts = [i.key for i in _instruments(cfg, args.instruments)]
    rows = [r for k in insts for r in jl.load(_path(k), KEY)]
    return rows, mw.opportunity_table(rows, insts, _due_until(_clock()))


def _stat(vals: list, bounds: list) -> str:
    pts = [v for v in vals if v is not None]
    unk = sum(1 for v, bd in zip(vals, bounds) if v is None and not bd)
    bnd = sum(1 for v, bd in zip(vals, bounds) if v is None and bd)
    zero = sum(1 for v in pts if v == 0)
    s = f"点值 {len(pts)}（其中确定弃权/门槛拒绝 0 计 {zero}）、有界 {bnd}、未知 {unk}"
    return s + (f"；点值均值 {sum(pts) / len(pts):+.2f}$/组" if pts else "")


def cmd_report(args) -> int:
    from collections import Counter
    rows, tab = _table(args)
    print(f"主墙近侧研究账 {mw.CONFIG['version']}（hash {mw.config_hash()}）：前瞻起点 {mw.CONFIG['prospective_start']}，"
          f"应有 {len(tab)} 格（交易日 × 品种），账内 {len(rows)} 行")
    print(f"  身份：探索；{mw.CONFIG['stage_check']} 只是独立阶段检查，不继承 v5 确认资格；不是实盘放行。跨品种同日不独立，未做区间。")
    print("  机会类别：" + ("，".join(f"{k} {v}" for k, v in sorted(Counter(t["category"] for t in tab).items())) or "（尚无应有格）"))
    outs = [t["outcome"] or {} for t in tab]
    for lab, key in (("A 主墙（政策）", "A"), ("B 1·ATR（政策）", "B"), ("A−B 配对", "A_minus_B")):
        print(f"  {lab}：分母 {len(tab)} 格；" + _stat([o.get(key) for o in outs], [o.get(f"{key}_bounds") for o in outs]))
    return 0


def cmd_audit(args) -> int:
    """只读核对：应有格逐一列出身份与输入新鲜度（不看收益）。有 not_generated 或盘前失败 → rc=1（缺口要可见）。"""
    rows, tab = _table(args)
    by = {(r["session"], r["instrument"]): r for r in rows}
    bad = 0
    for t in tab:
        r = by.get((t["session"], t["instrument"]))
        if r is None:
            bad += 1
            print(f"  ⚠️ {t['session']}|{t['instrument']}  not_generated（整日没有任何记录）")
            continue
        pre = r.get("premarket") or {}
        inp = pre.get("inputs") or {}
        ed = r.get("entry_decision") or {}
        if pre.get("status") not in ("ok", "no_expiry"):
            bad += 1
        print(f"  {r[KEY]}  pre={pre.get('status')} 基准 {inp.get('base_date')}/{inp.get('expected_base_date')} "
              f"源 {inp.get('source')}  Sdec {ed.get('sdec')} 近侧 {(ed.get('choice') or {}).get('near')}  类别 {t['category']}")
    print(f"  应有 {len(tab)} 格，缺口/盘前失败 {bad}")
    cov = coverage_all(sorted({t["instrument"] for t in tab}) or [i for i in (args.instruments or mw.CONFIG["instruments"])], _clock())
    lab = {"premarket": "盘前身份", "endpoint": "主终点观测", "sample": "描述采样"}
    for sec in ("premarket", "endpoint", "sample"):
        xs = [c for c in cov if c["section"] == sec]
        g = [c for c in xs if c["status"] in ("missing", "partial")]
        print(f"  {lab[sec]}覆盖：已到截止 {len(xs)} 项，缺口 {len(g)}" + ("" if not g else "：" + "；".join(c["id"] for c in g[:6])))
        bad += len(g) if sec != "premarket" else sum(1 for c in g if c["item"] == "sdec")
    print("  注：rc=0 只说明已到截止的应有项没有缺口；未到截止的（如盘前时的当日开盘窗）不在其内。")
    return 1 if bad else 0


def register(sub):
    p = sub.add_parser("mainwall", help="主墙近侧卖方价差前瞻研究账（Codex 056 v0.1；只读，从不下单，不是实盘放行）")
    ss = p.add_subparsers(dest="mainwall_cmd", required=True)
    for name, fn, hl in (("capture", cmd_capture, "盘前冻结：新鲜 T−1 输入 + 两侧主墙图"),
                         ("sample", cmd_sample, "盘中采样：标的到两侧冻结墙的距离与 A 腿盘口"),
                         ("settle", cmd_settle, "收盘后派生政策结果与机会类别"),
                         ("report", cmd_report, "机会母表与 A、B、A−B 描述（探索）"),
                         ("audit", cmd_audit, "逐行身份与输入新鲜度 + 三维已到截止覆盖（不看收益）"),
                         ("coverage", cmd_coverage, "已到截止的应有覆盖核查：新缺口持久登记 + 兜底凭证（调度每次唤醒跑）")):
        c = ss.add_parser(name, help=hl); c.add_argument("instruments", nargs="*")
        c.add_argument("--status-file"); c.add_argument("--run-id")
        c.set_defaults(func=fn)
    q = ss.add_parser("quote", help="--window open：冻结 Sdec→选近侧→取盘口；close：收盘窗标记")
    q.add_argument("instruments", nargs="*"); q.add_argument("--window", choices=("open", "close"), required=True)
    q.add_argument("--status-file"); q.add_argument("--run-id")
    q.set_defaults(func=cmd_quote)
