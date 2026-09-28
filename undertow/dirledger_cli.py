"""方向判断台账（用户 2026-09-28：「[外部作者]的方向性判断……你能否也给出这样类似的方向性判断」）。

两条独立记录、同一套价格计分：
- 我们的偏度读数（analyze/skew_reading.py，预登记 docs/prereg/2026-09-28_skew_reading_v1.md）。
- 外部作者的判断：用户给帖子后手动登记，按【发布时刻】对应到第一个在其后开盘的交易日，
  写 data/soul/author_calls.jsonl（私有、gitignore —— 付费内容，不入库；只存概括，不存原文）。

台账 v2（Codex 017 D17-01～04）：
- 目录按规则版本分开：data/history/direction_ledger/<rule_version>/{prospective,attempts,replay}/<inst>.jsonl，
  v2 规则不会与 v1 冲突或覆盖。旧的无版本文件归档在 _superseded_v0_ledger/。
- attempts/ 只追加：每次运行（含数据不足、晚到、冻结后输入变化）都留一行，不删不改。
- prospective/ 每个 (品种, 交易日) 至多一条正式记录，**截止 = 该交易日 09:30 ET 开盘**：
  · 截止前第一次【身份合格】的计算冻结为正式预测（status=eligible）。合格 = 当日与前一交易日两份快照都存在、
    抓取时刻都已知且 ≤ 记录时刻 < 开盘、两份都经 captured_at 认证到对应交易日、读数已按规则算出。
  · 截止前输入不齐 → 只记 attempts，不占正式 key（旧版把凌晨第一次「数据不足」冻结，快照到齐后反而冲突）。
  · 截止后仍无正式记录 → 写一条 status=missing_at_cutoff（缺失本身入账，不能事后补成预测）；截止后算出的读数只进 attempts（late）。
  · 已有正式记录时重复运行：输入相同 → exists（保留首次记录时刻）；输入变了 → attempts 记 changed_after_freeze，正式记录不动。
- 计分：终点按交易日历（skew_reading.forward_returns），缺行情不顺延、未收市不计；只有 eligible 行进前瞻汇总，
  其余按原因列分母。结果变动时旧结果留在 outcome_history。汇总只是描述，不是预登记检验。
只读行情，从不下单。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path

from undertow.analyze import skew_reading as skr
from undertow.collect import jsonl_ledger as jl
from undertow.core import market_calendar as mc
from undertow.core.clock import ET, market_today

DIR = Path("data/history/direction_ledger")
AUTHOR = Path("data/soul/author_calls.jsonl")
KEY = "key"
LEDGER_SCHEMA = 2
POST_FIELDS = ("outcome", "scored_at", "outcome_history")
SESSION_MAP = "captured_at→certify_session（NYSE 日历）"
OPEN = dtime(9, 30)
CLOSE_BUFFER_MIN = 15          # 收市后 15 分钟才认为日线收盘价定格（运营缓冲，非统计阈值）


def _frozen(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in POST_FIELDS}


def vdir(rule_version: str | None = None) -> Path:
    return DIR / (rule_version or skr.RULE["version"])


def _path(inst: str, kind: str, rule_version: str | None = None) -> Path:
    """kind ∈ prospective / attempts / replay。"""
    return vdir(rule_version) / kind / f"{inst}.jsonl"


def _now():
    return datetime.now(timezone.utc)


def open_time(session: date) -> datetime:
    return datetime.combine(session, OPEN, tzinfo=ET)


def last_closed_session(now: datetime) -> date | None:
    """now 时刻已收市（含缓冲）的最后一个交易日。"""
    t = now.astimezone(ET)
    d = t.date()
    ct = mc.close_time(d)
    if ct:
        hh, mm = map(int, ct.split(":"))
        if (t.hour * 60 + t.minute) >= hh * 60 + mm + CLOSE_BUFFER_MIN:
            return d
    return mc.prev_trading_day(d)


def session_index(store, sym: str) -> dict:
    """{交易日: 快照文件日}：按【实际抓取时刻】认证每份快照可用于哪个交易日（盘前→当日；盘后/周末→下一交易日；
    盘中→剔除）。早期快照文件日与数据日大量错位（记忆 snapshot-date-alignment-p0），不能用文件名日期。
    同一交易日有多份时取抓取最晚的一份（报价最新）。
    ⚠️ 这只认证「可用于哪个交易日」，不认证「在某次决策时已经可得」—— 前瞻记录另查 captured_at ≤ recorded_at；
    历史重放用它时只能称「盘前最后版本重放」（Codex 018 #1）。"""
    from undertow.core.clock import certify_session
    tdays = mc.trading_days(date(2026, 1, 1), market_today()) or []
    best: dict = {}
    for d in store.dates("options", sym):
        ca = store.captured_at("options", sym, d)
        cert = certify_session(ca, tdays)
        if cert["status"] != "certified" or cert["session"] is None:
            continue
        s = cert["session"]
        if s not in best or ca > best[s][1]:
            best[s] = (d, ca)
    return {s: d for s, (d, _) in best.items()}


def _ts(x) -> datetime | None:
    return datetime.fromtimestamp(x, timezone.utc) if x is not None else None


def skew_reader(prev_snap, curr_snap, quote_day, inst) -> dict:
    res = skr.read(prev_snap.contracts, curr_snap.contracts, asof=quote_day)
    res["complete"] = res.get("reading") != "数据不足"          # 规则所需分量是否齐全（019-03）
    return res


def _load_ident(store, sym: str, d, problems: list, name: str):
    """一次读取得到 (payload, ident)（019-02）。旧式 store 桩（无 load_with_identity）→ 多次读取，标身份问题。"""
    if d is None:
        return None, None
    loader = getattr(store, "load_with_identity", None)
    if loader is not None:
        return loader("options", sym, d)
    problems.append(f"{name}_multi_read_identity")
    p = store.path_of("options", sym, d)
    ident = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest() if (p and p.exists()) else None,
             "captured_at": store.captured_at("options", sym, d)}
    return store.load("options", sym, d), ident


def _certified(ca: float | None, session: date) -> bool:
    from undertow.core.clock import certify_session
    if ca is None:
        return False
    cert = certify_session(ca, mc.trading_days(date(2026, 1, 1), session) or [])
    return cert.get("status") == "certified" and cert.get("session") == session


def build_row(inst: str, sym: str, session: date, store, *, now: datetime, replay: bool, index=None,
              reader=None, rule_version: str | None = None, level_fn=None) -> dict:
    """某品种某 session 的读数行（纯组装；读快照由 store 提供）。

    身份（identity）与质量（quality）分开判（Codex 019-03）：
      identity —— 两份快照都在、同一次读取得到 payload/sha/抓取时刻、抓取时刻已知且 ≤ 记录时刻 < 开盘、
                  各自抓取时刻重新认证到对应交易日（索引建好后文件被替换也能发现）；
      quality  —— reader 返回 complete=True（规则所需分量齐全）。合法的「中性」/H1=0 属于 complete。
    两者都过才可能冻结。H3 用的当日水平另有资格（record_one 里单独判），不借 H1 的正式记录。"""
    from undertow.collect.cboe_options import snapshot_from_payload
    row = {KEY: f"{inst}|{session.isoformat()}", "schema": LEDGER_SCHEMA, "instrument": inst,
           "session": session.isoformat(), "rule_version": rule_version or skr.RULE["version"],
           "recorded_at": now.astimezone(timezone.utc).isoformat(),
           "mode": "replay" if replay else "prospective", "session_map": SESSION_MAP,
           "decision_cutoff": open_time(session).isoformat()}
    idx = index if index is not None else session_index(store, sym)
    prev_td = mc.prev_trading_day(session)
    quote_day = prev_td                       # 认证到 session 的快照，其报价 ≈ session 前一交易日收盘
    cur_file, prev_file = idx.get(session), (idx.get(prev_td) if prev_td else None)
    row.update({"curr_file": cur_file.isoformat() if cur_file else None,
                "prev_file": prev_file.isoformat() if prev_file else None,
                "quote_day": quote_day.isoformat() if quote_day else None})
    problems: list = []
    curr_problems: list = []
    loaded = {}
    for name, d, want in (("curr", cur_file, session), ("prev", prev_file, prev_td)):
        payload, ident = _load_ident(store, sym, d, problems, name)
        loaded[name] = payload
        # 020-02：前瞻记录把本次读到的快照原字节整份存 cas，正式行引用其 sha256 —— 同日被覆盖也能恢复
        if not replay and payload is not None and ident and ident.get("raw") is not None:
            try:
                from undertow.collect import cas
                reps: list = []
                row[f"{name}_blob"] = cas.put_blob(ident["raw"], repairs=reps)
                if reps:
                    row[f"{name}_blob_repairs"] = reps
            except Exception as e:
                row[f"{name}_blob"] = None
                problems.append(f"{name}_blob_store_failed")
                if name == "curr":
                    curr_problems.append(f"{name}_blob_store_failed")
                print(f"  ⚠️ {inst} {name} 快照原文存档失败：{type(e).__name__}: {e}", file=sys.stderr)
        elif not replay and payload is not None:
            row[f"{name}_blob"] = None
        ca = _ts(ident.get("captured_at")) if ident else None
        row[f"{name}_sha"] = (ident.get("sha256") or "")[:16] or None if ident else None
        row[f"{name}_captured_at"] = ca.isoformat() if ca else None
        mine = []
        if d is None:
            mine.append(f"{name}_file_missing")
        elif payload is None:
            mine.append(f"{name}_payload_unloadable")
        elif ca is None:
            mine.append(f"{name}_captured_at_unknown")
        else:
            if ca > now:
                mine.append(f"{name}_captured_after_record")
            if want is not None and not replay and not _certified(ident.get("captured_at"), want):
                mine.append(f"{name}_certification_changed")
        problems += mine
        if name == "curr":
            curr_problems += mine
    if now >= open_time(session):
        problems.append("recorded_after_open"); curr_problems.append("recorded_after_open")
    row["before_open"] = now < open_time(session)
    cur_p, prev_p = loaded["curr"], loaded["prev"]
    if cur_p is not None and level_fn is not None:
        try:
            lv = level_fn(snapshot_from_payload(cur_p, inst, sym), quote_day)
        except Exception as e:
            lv = {"error": f"{type(e).__name__}: {e}"[:160]}
        row["curr_level"] = lv
        row["level_identity_problems"] = [x for x in curr_problems if not x.startswith("curr_multi")] + \
            [x for x in problems if x == "curr_multi_read_identity"]
    if cur_p is None or prev_p is None:
        row.update({"reading": "数据不足", "reason": "认证到当日或前一交易日的快照缺失（不以更早快照顶替）",
                    "inputs_complete": False, "quality_ok": False})
    else:
        cur = snapshot_from_payload(cur_p, inst, sym)
        prv = snapshot_from_payload(prev_p, inst, sym)
        res = (reader or skew_reader)(prv, cur, quote_day, inst)
        complete = res.get("complete")
        if complete is None:
            complete = res.get("reading") != "数据不足"
        row.update({"reading": res["reading"], "reason": res.get("reason", ""), "features": res.get("features"),
                    "inputs_complete": True, "quality_ok": bool(complete)})
    row["identity_problems"] = problems
    row["identity_ok"] = not problems
    return row


def restore_row_inputs(row: dict) -> dict:
    """按正式记录引用的整份原字节恢复两份快照 payload 与 captured_at（不读 data/snapshots，不联网）。"""
    import gzip as _gz
    from undertow.collect import cas
    out = {}
    for name in ("prev", "curr"):
        sha = row.get(f"{name}_blob")
        if not sha and row.get(f"{name}_sha"):
            # 020-02 上线前冻结的行只有 16 位 sha 前缀：按前缀找整份原文（找不到就是找不到，不补造）
            hits = sorted((cas.ROOT / "blobs").glob(f"{row[f'{name}_sha'][:2]}/{row[f'{name}_sha']}*.bin"))
            sha = hits[0].stem if len(hits) == 1 else None
        if not sha:
            out[name] = None
            continue
        rec = json.loads(_gz.decompress(cas.get_blob(sha)).decode("utf-8"))
        out[name] = {"payload": rec.get("payload"), "captured_at": rec.get("captured_at"), "sha256": sha}
    return out


def _append_attempt(inst: str, row: dict, status: str, note: str = "", rule_version: str | None = None) -> None:
    p = _path(inst, "attempts", rule_version)
    rec = dict(row, attempt_status=status, attempt_note=note)
    rec.pop(KEY, None)
    jl._check_finite(rec)
    with jl.locked(p):
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
            fh.flush(); os.fsync(fh.fileno())


def _input_sig(r: dict) -> tuple:
    return (r.get("curr_sha"), r.get("prev_sha"), r.get("rule_version"))


def decide(existing: dict | None, row: dict, now: datetime) -> tuple[str, dict | None]:
    """正式记录政策（纯函数，便于测试）。返回 (状态, 要插入的正式行或 None)。"""
    after = now >= open_time(date.fromisoformat(row["session"]))
    if existing is not None:
        if existing.get("status") == "eligible" and _input_sig(existing) != _input_sig(row) and row.get("quality_ok"):
            return "changed_after_freeze", None
        return "exists", None
    if not after:
        if row["identity_ok"] and row.get("quality_ok", False):
            return "eligible", dict(row, status="eligible")
        return "not_ready", None
    miss = {k: row.get(k) for k in (KEY, "schema", "instrument", "session", "rule_version", "recorded_at", "mode",
                                     "decision_cutoff", "curr_file", "prev_file", "identity_problems", "quality_ok")}
    return "missing_at_cutoff", dict(miss, status="missing_at_cutoff", reading=None,
                                     reason="截止（开盘）前没有身份合格的记录；截止后的读数只进 attempts，不补成预测")


def record_one(inst: str, sym: str, session: date, store, now: datetime, *, reader=None,
               rule_version: str | None = None, level_fn=None) -> tuple[str, dict]:
    row = build_row(inst, sym, session, store, now=now, replay=False, reader=reader, rule_version=rule_version,
                    level_fn=level_fn)
    p = _path(inst, "prospective", rule_version)
    existing = next((r for r in jl.load(p, KEY) if r[KEY] == row[KEY]), None)
    status, formal = decide(existing, row, now)
    if formal is not None:
        st = jl.insert_frozen(p, formal, key_field=KEY, frozen=_frozen)
        status = status if st == "inserted" else "exists"
    note = {"not_ready": "截止前输入/身份未齐，等下一次运行", "changed_after_freeze": "正式记录已冻结，本次输入不同，只留痕",
            "missing_at_cutoff": "截止后首次运行，缺失入账", "exists": "", "eligible": "冻结为正式预测"}.get(status, "")
    if status == "exists" and now >= open_time(session):
        status_for_attempt = "late"
    else:
        status_for_attempt = status
    _append_attempt(inst, row, status_for_attempt, note, rule_version)
    if level_fn is not None:
        row["level_status"] = _record_level(inst, row, now, rule_version)
    return status, row


def _record_level(inst: str, row: dict, now: datetime, rule_version: str | None) -> str:
    """H3 用的当日偏斜水平：独立资格（只看当日快照身份 + 水平已算出），独立正式记录（019-03）。
    与 H1 共享截止政策：截止前首份合格冻结；截止后无记录 → missing_at_cutoff。"""
    lv = row.get("curr_level")
    lrow = {k: row.get(k) for k in (KEY, "schema", "instrument", "session", "rule_version", "recorded_at", "mode",
                                     "decision_cutoff", "curr_file", "curr_sha", "curr_blob", "curr_captured_at")}
    probs = list(row["level_identity_problems"]) if "level_identity_problems" in row else ["curr_file_missing"]
    lrow.update({"level": lv, "identity_problems": probs, "identity_ok": not probs,
                 "quality_ok": isinstance(lv, dict) and "error" not in lv and lv.get("skew25_pp") is not None
                 and lv.get("skew10_pp") is not None,
                 "prev_sha": None, "inputs_complete": True})
    p = _path(inst, "levels", rule_version)
    existing = next((r for r in jl.load(p, KEY) if r[KEY] == lrow[KEY]), None)
    status, formal = decide(existing, lrow, now)
    if formal is not None:
        formal = {k: v for k, v in formal.items() if k not in ("prev_sha", "inputs_complete")}
        st = jl.insert_frozen(p, formal, key_field=KEY, frozen=_frozen)
        status = status if st == "inserted" else "exists"
    return status


def cmd_record(args) -> int:
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    cfg, store = load_config(), SnapshotStore()
    if args.as_of:
        return _record_replay(cfg, store, date.fromisoformat(args.as_of))
    session = market_today()
    if mc.is_trading_day(session) is not True:
        print(f"{session} 非交易日（或日历未覆盖）：不记录。")
        return 0 if mc.is_trading_day(session) is False else 1
    rc = 0
    now = _now()
    for inst in skr.RULE["instruments"]:
        sym = cfg.get(inst).options.symbol
        try:
            status, row = record_one(inst, sym, session, store, now)
        except (jl.LedgerConflictError, jl.LedgerCorruptError) as e:
            print(f"  ⚠️ {inst}：{e}", file=sys.stderr); rc = 1; continue
        except Exception as e:
            print(f"  ⚠️ {inst}：{type(e).__name__}: {e}", file=sys.stderr); rc = 1; continue
        f = row.get("features") or {}
        extra = (f" Δskew25 {f.get('d_skew25_pp'):+.2f}pp，put 更贵档 {f.get('rungs_put_richer')}/6，"
                 f"skew25 {f.get('skew25_curr_pp'):+.2f}，到期 {f.get('expiry')}" if f.get("d_skew25_pp") is not None
                 else f" {row.get('reason', '')}")
        probs = "；身份问题：" + ",".join(row["identity_problems"]) if row["identity_problems"] else ""
        print(f"  {inst:6s} {session} {row['reading']}（{status}）{extra}{probs}")
        if status == "changed_after_freeze":
            print(f"  ⚠️ {inst}：正式记录冻结后输入发生变化 —— 已留痕于 attempts，正式记录不改。", file=sys.stderr)
    return rc


def _record_replay(cfg, store, session: date) -> int:
    if mc.is_trading_day(session) is not True:
        print(f"{session} 非交易日：不回放。")
        return 0
    rc = 0
    for inst in skr.RULE["instruments"]:
        sym = cfg.get(inst).options.symbol
        try:
            row = build_row(inst, sym, session, store, now=_now(), replay=True)
            row["status"] = "replay"
            st = jl.insert_frozen(_path(inst, "replay"), row, key_field=KEY,
                                  frozen=lambda r: {k: v for k, v in _frozen(r).items() if k != "recorded_at"})
        except Exception as e:
            print(f"  ⚠️ {inst}：{type(e).__name__}: {e}", file=sys.stderr); rc = 1; continue
        print(f"  {inst:6s} {session} {row['reading']}（回放 {st}，盘前最后版本重放，非前瞻）")
    return rc


def _bars(sym: str) -> list[tuple[date, float, float]]:
    from undertow.collect.longbridge_kline import fetch_bars
    out = []
    for b in fetch_bars(f"{sym}.US", period="day", count=400):
        out.append((b["ts"].astimezone(ET).date() if b["ts"].tzinfo else b["ts"].date(), b["open"], b["close"]))
    return sorted(out)


def _bars_sha(bars) -> str:
    return hashlib.sha256(json.dumps([[b[0].isoformat(), b[1], b[2]] for b in bars]).encode()).hexdigest()[:16]


def session_after(posted: datetime) -> date | None:
    """发布时刻之后第一个开盘的交易日（09:30 ET 前发布 → 当日；之后 → 下一交易日）。"""
    t = posted.astimezone(ET)
    d = t.date()
    if mc.is_trading_day(d) and (t.hour, t.minute) < (9, 30):
        return d
    return mc.next_trading_day(d)


RULE_DIRECTION = {"防守化": -1, "进攻化": 1}             # skew-reading-v1 预登记的方向假设（机器标签）
#: 作者自然语言标签：只有明确的偏多/偏空计方向；「防守 / 放弃做多 / 区间 / 中性」是风险姿态或区间主张，不计方向。
#: 这是【回溯方法修订】（Codex 019：旧帖已按旧口径看过结果）——旧口径（防守→看跌）的输出另存私有文件，不覆盖。
AUTHOR_DIRECTION = {"偏空": -1, "偏多": 1}
AUTHOR_DIRECTION_OLD = {"偏空": -1, "防守": -1, "偏多": 1}


def _hit_with(mapping: dict, label: str, ret):
    if ret is None or label not in mapping:
        return None
    return (ret > 0) if mapping[label] > 0 else (ret < 0)


def _hit(label: str, ret):
    """机器读数与作者标签分属两个命名空间（019：作者「防守」≠ 机器「防守化」），各查各的映射。"""
    return _hit_with(RULE_DIRECTION if label in RULE_DIRECTION else AUTHOR_DIRECTION, label, ret)


def score_rows(rows: list[dict], bars, closed_through: date, source_sha: str, now: datetime) -> int:
    """原地回填 outcome；变化时把旧结果推入 outcome_history。返回改动行数。"""
    n = 0
    for r in rows:
        if r.get("status") not in ("eligible", "replay"):
            continue
        new = skr.forward_returns(bars, date.fromisoformat(r["session"]), closed_through=closed_through)
        new["price_source_sha"] = source_sha
        old = r.get("outcome")
        if old is not None and {k: v for k, v in old.items() if k != "price_source_sha"} == \
                {k: v for k, v in new.items() if k != "price_source_sha"}:
            continue
        if old is not None:
            r.setdefault("outcome_history", []).append({"outcome": old, "scored_at": r.get("scored_at")})
        r["outcome"], r["scored_at"] = new, now.isoformat()
        n += 1
    return n


def cmd_score(args) -> int:
    from undertow.core.config import load_config
    cfg = load_config()
    now = _now()
    closed = last_closed_session(now)
    lines = []
    for inst in skr.RULE["instruments"]:
        sym = cfg.get(inst).options.symbol
        try:
            bars = _bars(sym)
        except Exception as e:
            print(f"  ⚠️ {inst} 取日线失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        sha = _bars_sha(bars)
        for kind in ("prospective", "replay"):
            p = _path(inst, kind)
            if not p.exists():
                continue
            jl.update(p, lambda r: score_rows([r], bars, closed, sha, now) > 0, key_field=KEY, frozen=_frozen)
            rows = jl.load(p, KEY)
            title = f"{inst}（前瞻，只计 eligible）" if kind == "prospective" else f"{inst}（盘前最后版本重放，探索）"
            lines.append(_summary(title, rows, key="reading"))
        if AUTHOR.exists():
            calls = [json.loads(x) for x in AUTHOR.read_text("utf-8").splitlines() if x.strip()]
            mine = [c for c in calls if c.get("instrument") == inst]
            for c in mine:
                s = session_after(datetime.fromisoformat(c["posted_at"]))
                c["session"] = s.isoformat() if s else None
                c["status"] = "eligible"
                c["outcome"] = skr.forward_returns(bars, s, closed_through=closed) if s else None
                c["reading"] = c["call"]
            if mine:
                lines.append(_summary(f"{inst} 作者判断（私有，按发布时刻；回溯登记，非事前冻结）", mine, key="reading"))
                _save_author_revision(inst, mine)
    print("\n".join(l for l in lines if l))
    print("注：以上是描述性汇总，不是预登记检验 —— 5/10 日窗口相互重叠、未与基准比较、未做多重比较校正；"
          "n 达到 50 只是一次评估的触发条件，不代表可靠（Codex 017 D17-04）。")
    return 0


def _save_author_revision(inst: str, calls: list[dict]) -> None:
    """同一批作者判断按新旧两种口径的命中各存一份（私有，data/soul/），旧口径结果不被新口径覆盖。"""
    out = {"generated_at": _now().isoformat(), "instrument": inst,
           "note": "回溯方法修订：旧帖在改口径前已看过结果；仅未来未看的作者登记可称运行前采用新语义",
           "old_mapping": AUTHOR_DIRECTION_OLD, "new_mapping": AUTHOR_DIRECTION, "rows": []}
    for c in calls:
        oc = c.get("outcome") or {}
        out["rows"].append({"posted_at": c.get("posted_at"), "session": c.get("session"), "call": c.get("call"),
                            **{f"{tag}_{h}d": _hit_with(m, c.get("call"), oc.get(f"ret_{h}d"))
                               for tag, m in (("old", AUTHOR_DIRECTION_OLD), ("new", AUTHOR_DIRECTION))
                               for h in (1, 5, 10)}})
    p = AUTHOR.parent / f"author_scoring_revision_{inst}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def _summary(title: str, rows: list[dict], *, key: str) -> str:
    out = [f"【{title}】"]
    elig = [r for r in rows if r.get("status") in ("eligible", "replay")]
    rej: dict = {}
    for r in rows:
        if r not in elig:
            rej[r.get("status") or "unknown"] = rej.get(r.get("status") or "unknown", 0) + 1
    if rej:
        out.append("  不计入：" + "，".join(f"{k} {v}" for k, v in sorted(rej.items())))
    groups: dict = {}
    for r in elig:
        groups.setdefault(r.get(key), []).append(r)
    for lab, rs in sorted(groups.items(), key=lambda x: str(x[0])):
        cells = []
        for h in (1, 5, 10):
            oc = [(r.get("outcome") or {}) for r in rs]
            rets = [o.get(f"ret_{h}d") for o in oc if o.get(f"ret_{h}d") is not None]
            miss = sum(1 for o in oc if o.get(f"status_{h}d") == "missing_price")
            hits = [x for x in (_hit(lab, v) for v in rets) if x is not None]
            cells.append(f"{h}日 n={len(rets)}" + (f" 均 {sum(rets) / len(rets) * 100:+.2f}%" if rets else "")
                         + (f" 命中 {sum(hits)}/{len(hits)}" if hits else "") + (f" 缺价 {miss}" if miss else ""))
        out.append(f"  {lab}：{len(rs)} 条；" + "；".join(cells))
    return "\n".join(out)


def migrate_v0() -> list[str]:
    """把无版本旧文件移到 _superseded_v0_ledger/（保留，不删）。幂等。"""
    moved = []
    dst = DIR / "_superseded_v0_ledger"
    for p in list(DIR.glob("skew_*.jsonl")) + ([DIR / "replay"] if (DIR / "replay").exists() else []):
        dst.mkdir(parents=True, exist_ok=True)
        target = dst / p.name
        if target.exists():
            raise FileExistsError(f"{target} 已存在，拒绝覆盖")
        shutil.move(str(p), str(target))
        lk = p.with_suffix(p.suffix + ".lock")
        if lk.exists():
            lk.unlink()
        moved.append(f"{p} → {target}")
    return moved


def cmd_author_add(args) -> int:
    """登记外部作者的一条判断（只存概括；私有文件，不入库）。"""
    posted = datetime.fromisoformat(args.posted)
    if posted.tzinfo is None:
        print("posted 必须带时区（如 2026-09-25T19:33+08:00）", file=sys.stderr)
        return 2
    rec = {"posted_at": posted.isoformat(), "instrument": args.inst, "call": args.call,
           "horizon": args.horizon or "", "levels": args.levels or "", "summary": args.summary or "",
           "source": args.source or "", "added_at": _now().isoformat(), "retrospective": bool(args.retrospective)}
    AUTHOR.parent.mkdir(parents=True, exist_ok=True)
    existing = AUTHOR.read_text("utf-8").splitlines() if AUTHOR.exists() else []
    if any(json.loads(x).get("posted_at") == rec["posted_at"] and json.loads(x).get("instrument") == rec["instrument"]
           for x in existing if x.strip()):
        print("已登记过（同一发布时刻、同一品种），未重复写入。")
        return 0
    with open(AUTHOR, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    s = session_after(posted)
    print(f"已登记：{args.inst} {args.call}（发布 {posted.astimezone(ET):%m-%d %H:%M} ET → 计分起点 {s} 开盘）")
    return 0


CONVICTION_INSTRUMENTS = ("gold", "silver", "wti", "qqq", "tqqq", "tlt", "spy", "iwm")
_CONV_LABEL = {1: "多层看涨", -1: "多层看跌", 0: "无", None: "未知"}


def conviction_reader(prev_snap, curr_snap, quote_day, inst) -> dict:
    """期权多层同向（开发期规则，analyze/conviction.py）。只记录原始分量与判定，不计分、不进研报。"""
    from undertow.analyze import conviction as cv
    from undertow.analyze import structure_read as sr
    from undertow.analyze.flow import _live, analyze_flow
    fa = analyze_flow(prev_snap, curr_snap, today=quote_day, prev_date="prev", curr_date="curr")
    read = sr.analyze_structure(fa, _live(prev_snap, quote_day, 60), _live(curr_snap, quote_day, 60))
    feats = cv.layers(fa, read, prev_snap.spot, curr_snap.spot)
    return {"reading": _CONV_LABEL[feats["H1"]], "reason": "" if read.ok else (read.reason or ""), "features": feats,
            "complete": feats["H1"] is not None}             # H1 未知 → 不得冻结（019-03）；H1=0 是合法读数


def conviction_level(curr_snap, quote_day) -> dict | None:
    """H3 用的单快照偏斜水平（read_vol 主力到期口径）。"""
    from undertow.analyze.flow import read_vol
    v = read_vol(curr_snap, today=quote_day)
    if v is None:
        return None
    return {"skew25_pp": round(v.skew25_pp, 3), "skew10_pp": round(v.skew10_pp, 3), "atm_iv_pp": round(v.atm_iv_pp, 3),
            "expiry": v.expiry.isoformat(), "dte": v.days_out}


def cmd_conviction_record(args) -> int:
    """每个交易日盘前记录多层读数（开发期，status 仍按 v2 截止政策；不得称确认样本）。"""
    from undertow.analyze import conviction as cv
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    cfg, store = load_config(), SnapshotStore()
    session = market_today()
    if mc.is_trading_day(session) is not True:
        print(f"{session} 非交易日（或日历未覆盖）：不记录。")
        return 0 if mc.is_trading_day(session) is False else 1
    from undertow.analyze import direction_stats as dst
    if session < dst.FAMILY_D_START:
        print(f"{session} 早于方向台账族 D 正式起点 {dst.FAMILY_D_START}：冻结版本 {cv.RULE['version']} 不记录"
              "（开发期目录保留原样，不回填）。")
        return 0
    rc, now = 0, _now()
    for inst in CONVICTION_INSTRUMENTS:
        try:
            sym = cfg.get(inst).options.symbol
            status, row = record_one(inst, sym, session, store, now, reader=conviction_reader,
                                     rule_version=cv.RULE["version"], level_fn=conviction_level)
        except Exception as e:
            print(f"  ⚠️ {inst}：{type(e).__name__}: {e}", file=sys.stderr); rc = 1; continue
        f = row.get("features") or {}
        print(f"  {inst:6s} {session} {row['reading']}（{status}；水平 {row.get('level_status')}）"
              f"S={f.get('S')} F={f.get('F')} V={f.get('V')}"
              + (f"；身份问题：{','.join(row['identity_problems'])}" if row["identity_problems"] else ""))
    print(f"  规则 {cv.RULE['version']}：{cv.RULE['status']}")
    return rc


def cmd_migrate(args) -> int:
    for m in migrate_v0():
        print("  归档", m)
    return 0


def register(sub):
    p = sub.add_parser("dirledger", help="方向判断台账：偏度读数（事前冻结）+ 外部作者判断（私有）+ 价格计分")
    ss = p.add_subparsers(dest="dir_cmd", required=True)
    r = ss.add_parser("record", help="记录当日（开盘前）金银偏度读数；--as-of 回放写 replay/（探索，不进前瞻样本）")
    r.add_argument("--as-of"); r.set_defaults(func=cmd_record)
    s = ss.add_parser("score", help="回填 1/5/10 日走势并按读数分组汇总（含作者判断，私有）")
    s.set_defaults(func=cmd_score)
    c = ss.add_parser("conviction-record", help="盘前记录期权多层同向读数（开发期规则，只记录不计分）")
    c.set_defaults(func=cmd_conviction_record)
    m = ss.add_parser("migrate-v0", help="把无版本旧台账移入 _superseded_v0_ledger/（保留）")
    m.set_defaults(func=cmd_migrate)
    a = ss.add_parser("author-add", help="登记外部作者的一条判断（按发布时刻；私有、不入库）")
    a.add_argument("--inst", required=True, choices=list(skr.RULE["instruments"]))
    a.add_argument("--posted", required=True, help="发布时刻，带时区，如 2026-09-25T19:33+08:00")
    a.add_argument("--call", required=True, choices=("防守", "偏空", "偏多", "中性", "区间"))
    a.add_argument("--horizon"); a.add_argument("--levels"); a.add_argument("--summary"); a.add_argument("--source")
    a.add_argument("--retrospective", action="store_true", help="事后才读到的旧帖（回溯登记，不与事前冻结同列）")
    a.set_defaults(func=cmd_author_add)
