"""方向判断台账（用户 2026-09-28：「[外部作者]的方向性判断……你能否也给出这样类似的方向性判断」）。

两条独立记录、同一套价格计分：
- 我们的偏度读数（analyze/skew_reading.py，预登记 docs/prereg/2026-09-28_skew_reading_v1.md）：
  每个交易日开盘前自动记录，首份记录冻结（jsonl_ledger.insert_frozen），写 data/history/direction_ledger/（入库）。
- 外部作者的判断：用户给帖子后手动登记，按【发布时刻】对应到第一个在其后开盘的交易日，
  写 data/soul/author_calls.jsonl（私有、gitignore —— 付费内容，不入库；只存概括，不存原文）。
计分：基准 = 该交易日开盘价（信号开盘前可得、最早可执行），结果 = 第 1/5/10 个交易日收盘（含当日）。
只读行情，从不下单。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from undertow.analyze import skew_reading as skr
from undertow.collect import jsonl_ledger as jl
from undertow.core import market_calendar as mc
from undertow.core.clock import ET, market_today

DIR = Path("data/history/direction_ledger")
AUTHOR = Path("data/soul/author_calls.jsonl")
KEY = "key"
POST_FIELDS = ("outcome", "scored_at")


def _frozen(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in POST_FIELDS}


def _path(inst: str, replay: bool) -> Path:
    return (DIR / "replay" if replay else DIR) / f"skew_{inst}.jsonl"


def _now():
    return datetime.now(timezone.utc)


SESSION_MAP = "captured_at→certify_session（NYSE 日历）"


def session_index(store, sym: str) -> dict:
    """{交易日: 快照文件日}：按【实际抓取时刻】认证每份快照可用于哪个交易日（盘前→当日；盘后/周末→下一交易日；
    盘中→剔除）。早期快照文件日与数据日大量错位（记忆 snapshot-date-alignment-p0），不能用文件名日期。
    同一交易日有多份时取抓取最晚的一份（报价最新）。"""
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


def build_row(inst: str, sym: str, session: date, store, *, now: datetime, replay: bool, index=None) -> dict:
    """某品种某 session 的读数行（纯组装；读快照由 store 提供）。

    当日快照 = 认证到 session 的那份；前一份 = 认证到 session 前一交易日的那份。缺任一 → 数据不足（不顶替）。"""
    from undertow.collect.cboe_options import snapshot_from_payload
    row = {KEY: f"{inst}|{session.isoformat()}", "instrument": inst, "session": session.isoformat(),
           "rule_version": skr.RULE["version"], "recorded_at": now.isoformat(),
           "mode": "replay" if replay else "prospective", "session_map": SESSION_MAP}
    idx = index if index is not None else session_index(store, sym)
    prev_td = mc.prev_trading_day(session)
    quote_day = prev_td                       # 认证到 session 的快照，其报价 ≈ session 前一交易日收盘
    cur_file, prev_file = idx.get(session), (idx.get(prev_td) if prev_td else None)
    cur_p = store.load("options", sym, cur_file) if cur_file else None
    prev_p = store.load("options", sym, prev_file) if prev_file else None
    ca = store.captured_at("options", sym, cur_file) if cur_file else None
    row.update({"curr_file": cur_file.isoformat() if cur_file else None,
                "prev_file": prev_file.isoformat() if prev_file else None,
                "quote_day": quote_day.isoformat() if quote_day else None,
                "curr_captured_at": datetime.fromtimestamp(ca, timezone.utc).isoformat() if ca else None})
    for name, d in (("curr", cur_file), ("prev", prev_file)):
        p = store.path_of("options", sym, d) if d else None
        row[f"{name}_sha"] = hashlib.sha256(p.read_bytes()).hexdigest()[:16] if (p and p.exists()) else None
    open_t = datetime(session.year, session.month, session.day, 9, 30, tzinfo=ET)
    row["before_open"] = now < open_t and (ca is None or datetime.fromtimestamp(ca, timezone.utc) < open_t)
    if cur_p is None or prev_p is None:
        row.update({"reading": "数据不足", "reason": "认证到当日或前一交易日的快照缺失（不以更早快照顶替）"})
        return row
    cur = snapshot_from_payload(cur_p, inst, sym).contracts
    prv = snapshot_from_payload(prev_p, inst, sym).contracts
    res = skr.read(prv, cur, asof=quote_day)
    row.update({"reading": res["reading"], "reason": res.get("reason", ""), "features": res.get("features")})
    return row


def cmd_record(args) -> int:
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    cfg, store = load_config(), SnapshotStore()
    replay = bool(args.as_of)
    session = date.fromisoformat(args.as_of) if replay else market_today()
    if mc.is_trading_day(session) is not True:
        print(f"{session} 非交易日（或日历未覆盖）：不记录。")
        return 0 if mc.is_trading_day(session) is False else 1
    rc = 0
    for inst in skr.RULE["instruments"]:
        sym = cfg.get(inst).options.symbol
        try:
            row = build_row(inst, sym, session, store, now=_now(), replay=replay)
            st = jl.insert_frozen(_path(inst, replay), row, key_field=KEY, frozen=_frozen)
        except jl.LedgerConflictError as e:
            print(f"  ⚠️ {inst}：{e}", file=sys.stderr); rc = 1; continue
        except Exception as e:
            print(f"  ⚠️ {inst}：{type(e).__name__}: {e}", file=sys.stderr); rc = 1; continue
        f = row.get("features") or {}
        print(f"  {inst:6s} {session} {row['reading']}（{st}）"
              + (f" Δskew25 {f.get('d_skew25_pp'):+.2f}pp，put 更贵档 {f.get('rungs_put_richer')}/6，"
                 f"skew25 {f.get('skew25_curr_pp'):+.2f}，到期 {f.get('expiry')}" if f.get("d_skew25_pp") is not None
                 else f" {row.get('reason', '')}")
              + ("" if row["before_open"] or replay else "　⚠️ 开盘后才记录：不计入前瞻样本"))
    return rc


def _bars(sym: str) -> list[tuple[date, float, float]]:
    from undertow.collect.longbridge_kline import fetch_bars
    out = []
    for b in fetch_bars(f"{sym}.US", period="day", count=400):
        out.append((b["ts"].astimezone(ET).date() if b["ts"].tzinfo else b["ts"].date(), b["open"], b["close"]))
    return sorted(out)


def session_after(posted: datetime) -> date | None:
    """发布时刻之后第一个开盘的交易日（09:30 ET 前发布 → 当日；之后 → 下一交易日）。"""
    t = posted.astimezone(ET)
    d = t.date()
    if mc.is_trading_day(d) and (t.hour, t.minute) < (9, 30):
        return d
    return mc.next_trading_day(d)


def _hit(call: str, ret):
    if ret is None:
        return None
    if call in ("防守化", "偏空", "防守"):
        return ret < 0
    if call in ("进攻化", "偏多"):
        return ret > 0
    return None


def cmd_score(args) -> int:
    from undertow.core.config import load_config
    cfg = load_config()
    lines = []
    for inst in skr.RULE["instruments"]:
        sym = cfg.get(inst).options.symbol
        try:
            bars = _bars(sym)
        except Exception as e:
            print(f"  ⚠️ {inst} 取日线失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        for replay in (False, True):
            p = _path(inst, replay)
            if not p.exists():
                continue

            def fn(r):
                new = skr.forward_returns(bars, date.fromisoformat(r["session"]))
                if new != r.get("outcome"):
                    r["outcome"], r["scored_at"] = new, _now().isoformat()
                    return True
                return False
            jl.update(p, fn, key_field=KEY, frozen=_frozen)
            rows = jl.load(p, KEY)
            lines.append(_summary(f"{inst}{'（回放，探索）' if replay else '（前瞻）'}", rows, key="reading"))
        if AUTHOR.exists():
            calls = [json.loads(x) for x in AUTHOR.read_text("utf-8").splitlines() if x.strip()]
            mine = [c for c in calls if c.get("instrument") == inst]
            for c in mine:
                s = session_after(datetime.fromisoformat(c["posted_at"]))
                c["session"] = s.isoformat() if s else None
                c["outcome"] = skr.forward_returns(bars, s) if s else None
                c["reading"] = c["call"]
            if mine:
                lines.append(_summary(f"{inst} 作者判断（私有，按发布时刻）", mine, key="reading"))
    print("\n".join(l for l in lines if l))
    print("注：样本很少时命中率没有统计意义；按预登记，前瞻 n ≥ 50 且检验通过之前，所有读数都是未验证的（T3）。")
    return 0


def _summary(title: str, rows: list[dict], *, key: str) -> str:
    out = [f"【{title}】"]
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get(key), []).append(r)
    for lab, rs in sorted(groups.items(), key=lambda x: str(x[0])):
        cells = []
        for h in (1, 5, 10):
            rets = [(r.get("outcome") or {}).get(f"ret_{h}d") for r in rs]
            rets = [x for x in rets if x is not None]
            hits = [_hit(lab, x) for x in rets]
            hits = [x for x in hits if x is not None]
            cells.append(f"{h}日 n={len(rets)}" + (f" 均 {sum(rets) / len(rets) * 100:+.2f}%" if rets else "")
                         + (f" 命中 {sum(hits)}/{len(hits)}" if hits else ""))
        out.append(f"  {lab}：{len(rs)} 条；" + "；".join(cells))
    return "\n".join(out)


def cmd_author_add(args) -> int:
    """登记外部作者的一条判断（只存概括；私有文件，不入库）。"""
    posted = datetime.fromisoformat(args.posted)
    if posted.tzinfo is None:
        print("posted 必须带时区（如 2026-09-25T19:33+08:00）", file=sys.stderr)
        return 2
    rec = {"posted_at": posted.isoformat(), "instrument": args.inst, "call": args.call,
           "horizon": args.horizon or "", "levels": args.levels or "", "summary": args.summary or "",
           "source": args.source or "", "added_at": _now().isoformat()}
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


def register(sub):
    p = sub.add_parser("dirledger", help="方向判断台账：偏度读数（事前冻结）+ 外部作者判断（私有）+ 价格计分")
    ss = p.add_subparsers(dest="dir_cmd", required=True)
    r = ss.add_parser("record", help="记录当日（开盘前）金银偏度读数；--as-of 回放写 replay/（探索，不进前瞻样本）")
    r.add_argument("--as-of"); r.set_defaults(func=cmd_record)
    s = ss.add_parser("score", help="回填 1/5/10 日走势并按读数分组汇总（含作者判断，私有）")
    s.set_defaults(func=cmd_score)
    a = ss.add_parser("author-add", help="登记外部作者的一条判断（按发布时刻；私有、不入库）")
    a.add_argument("--inst", required=True, choices=list(skr.RULE["instruments"]))
    a.add_argument("--posted", required=True, help="发布时刻，带时区，如 2026-09-25T19:33+08:00")
    a.add_argument("--call", required=True, choices=("防守", "偏空", "偏多", "中性", "区间"))
    a.add_argument("--horizon"); a.add_argument("--levels"); a.add_argument("--summary"); a.add_argument("--source")
    a.set_defaults(func=cmd_author_add)
