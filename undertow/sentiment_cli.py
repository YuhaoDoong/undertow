"""`undertow sentiment ...` 编排：情绪面板 v0（Codex 066 合同 sentiment-panel-v0-20261028；只读）。

  forward  前瞻：现在抓取各分量（不用缓存），写一行「截至此刻可见的状态」到 forward 台账
  replay   回补：对日历内每个交易日 S，只用 S 开盘前可得的输入重建一行，写到 replay 台账
  show     打印最新一行（平常表格；原值、数据日、分位、质量逐列）
  audit    可得性与版本覆盖审计（各分量历史覆盖、VXSLV 回算段、COT 版本未知段）；不算任何收益

定位：**描述层**，claims 角色=背景。三组分量分列，不合成恐慌分，不给方向，不进研判、不做开仓过滤。

台账：data/history/sentiment/{forward,replay}/<品种>.jsonl。一行身份 =
(effective_session, instrument, rule_version, vintage_id)。同一输入重跑 → 幂等跳过；输入变了
（新数据或源修订）→ 新 vintage 另起一行并指向上一版（supersedes），旧行不改。
replay 与 forward 分目录，回补永远不会冒充当日记录。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from undertow.analyze import sentiment as S
from undertow.collect import jsonl_ledger as jl
from undertow.core import market_calendar as mc
from undertow.core.clock import ET

DIR = Path("data/history/sentiment")
KEY = "key"
INSTRUMENTS = ("gold", "silver", "wti", "qqq")
BACKGROUND_VOL = "VIX"            # 美股共享背景：只放在 qqq 行，且标明不是独立样本
FROZEN_EXCLUDE = ("recorded_at",)
COT_LOOKBACK_FETCH = 420          # 回补 6/25 以来每期都要前 156 期，留足余量


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


def _open_dt(session: date) -> datetime:
    return datetime.combine(session, dtime(9, 30), ET)


# ═══════════════════════ 期权快照 → 合约列表 ═══════════════════════

def parse_chain(payload: dict) -> tuple[list[dict], dict]:
    """CBOE 快照 payload → (合约列表, 时间元数据)。缺字段保持 None。"""
    import re
    pat = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
    data = (payload or {}).get("data") or {}
    out = []
    for o in data.get("options") or []:
        m = pat.match(str(o.get("option", "")))
        if not m:
            continue
        _, ex, cp, k = m.groups()
        try:
            exd = date(2000 + int(ex[:2]), int(ex[2:4]), int(ex[4:6]))
        except ValueError:
            continue

        def num(f):
            v = o.get(f)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append({"expiry": exd, "kind": cp, "strike": int(k) / 1000, "iv": num("iv"),
                    "delta": num("delta"), "bid": num("bid"), "ask": num("ask"),
                    "volume": num("volume"), "open_interest": num("open_interest")})
    ltt = data.get("last_trade_time")
    val = None
    if isinstance(ltt, str) and len(ltt) >= 10:
        try:
            val = date.fromisoformat(ltt[:10])
        except ValueError:
            val = None
    meta = {"payload_timestamp_raw": payload.get("timestamp") if payload else None,
            "underlying_last_trade_time_raw": ltt, "valuation_date": val,
            "time_basis": "underlying_last_trade_time 的日期部分（源无时区，未核实；估计）"}
    return out, meta


def options_features(payload: dict) -> dict:
    cs, meta = parse_chain(payload)
    val = meta["valuation_date"]
    return {"meta": meta, "skew": S.skew_features(cs, val), "pc": S.pc_features(cs, val)}


# ═══════════════════════ 组装一行（纯：吃已取到的输入） ═══════════════════════

def vol_component(sym: str, src: dict | None, decision_date: date, trading_days, *, mode: str) -> dict:
    base = {"source_id": f"cboe:{sym}", "proxy": sym,
            "methodology_vintage": S.VOL_METHOD_NOTES.get(sym, {}).get("methodology_vintage", "cboe-current-csv"),
            "source_timezone_basis": "CSV 日期为指数数据日（无时刻）",
            "available_at_basis": ("actual_capture" if mode == "forward"
                                   else "nominal：数据日收盘后发布、次一交易日开盘前可得（未逐日核实）"),
            "unit": "index_points"}
    if not src:
        return {**base, "value": None, "quality_state": S.MISSING, "missing_reason": "source_unavailable"}
    f = S.vol_features(src["series"], decision_date, sym)
    fresh = S.MISSING if f["value"] is None else \
        S.vol_quality(date.fromisoformat(f["event_date"]), decision_date, trading_days)
    # 前瞻：新鲜度即质量；回补：今天下载的历史值只能是 reconstructed/unknown_vintage
    q = fresh if (mode == "forward" or f["value"] is None) else f["point_vintage"]
    age = None
    if f["event_date"] and trading_days:
        ed = date.fromisoformat(f["event_date"])
        age = sum(1 for t in trading_days if ed < t < decision_date)
    return {**base, **f, "source_hash": src["sha256"], "source_path_or_url": src["url"],
            "captured_at": _iso(src.get("fetched_at")),
            "first_available_at": _iso(src.get("fetched_at")) if mode == "forward" else None,
            "source_asof": f["event_date"], "quality_state": q, "freshness_state": fresh,
            "age_sessions": age}


def cot_component(inst_key: str, series: list[dict] | None, src_hash: str | None,
                  decision_at: datetime, *, mode: str, fetched_at: float | None) -> dict:
    """只用 nominal 发布时刻早于决策时刻的报告期。前瞻时以实际抓取时刻为保守可得依据。"""
    base = {"source_id": "cftc:socrata", "source_timezone_basis": "report_date=周二截止日",
            "methodology_vintage": "cftc-current-revision",
            "available_at_basis": ("actual_capture" if mode == "forward" else "nominal_schedule_unverified"),
            "release_rule": "周二截止 → 当周五 15:30 ET（常规；例外未逐期核实）"}
    if not series:
        return {**base, "value": None, "quality_state": S.MISSING, "missing_reason": "source_unavailable"}

    def nominal_release(d: date) -> datetime:
        return datetime.combine(d + timedelta(days=3), dtime(15, 30), ET)
    usable = [r for r in series if nominal_release(r["report_date"]) < decision_at]
    if not usable:
        return {**base, "value": None, "quality_state": S.MISSING, "missing_reason": "no_report_before_decision"}
    f = S.cot_features(usable)
    cur = usable[-1]
    # 预期最新一期：nominal 发布早于决策的最近一个周二
    expected = None
    d = decision_at.astimezone(ET).date()
    while expected is None:
        if d.weekday() == 1 and nominal_release(d) < decision_at:
            expected = d
        d -= timedelta(days=1)
    if mode == "replay":
        q = S.RECONSTRUCTED
    else:
        lag_weeks = (expected - cur["report_date"]).days // 7
        if lag_weeks <= 0:
            # 发布后的第一个交易日算 current；之后到下一期发布前都是正常沿用
            q = S.CURRENT if decision_at.astimezone(ET).date() <= _next_session(
                nominal_release(cur["report_date"]).astimezone(ET).date()) else S.EXPECTED_CARRY
        elif lag_weeks == 1:
            q = S.PENDING
        else:
            q = S.STALE
    return {**base, **f, "instrument": inst_key, "report_type": cur["report_type"],
            "category_actual": cur["category_actual"], "value": f["net_over_oi"],
            "event_date": f["report_date"], "source_asof": f["report_date"],
            "nominal_release_at": nominal_release(cur["report_date"]).isoformat(),
            "source_hash": src_hash, "captured_at": _iso(fetched_at),
            "first_available_at": _iso(fetched_at) if mode == "forward" else None,
            "quality_state": q, "missing_reason": None}


def _next_session(d: date) -> date:
    """严格晚于 d 的第一个交易日；日历未覆盖时按工作日。"""
    x = d + timedelta(days=1)
    for _ in range(10):
        t = mc.is_trading_day(x)
        if t or (t is None and x.weekday() < 5):
            return x
        x += timedelta(days=1)
    return x


def options_component(snap: dict | None, history: list[tuple[date, dict]], *, mode: str) -> dict:
    """snap = {payload, ident, file_date, session}；history = 决策前可得的更早快照特征 [(session, feats)]。"""
    base = {"source_id": "cboe:delayed_quotes_chain", "available_at_basis": "actual_capture",
            "source_timezone_basis": "payload.timestamp 无时区；captured_at 为 UTC 实测",
            "methodology_vintage": S.RULE_VERSION,
            "oi_session_basis": "OCC 隔夜结算的上一 session（项目时序约定，未逐腿核实）",
            "volume_session_basis": "估值日 session（估计）",
            "quote_time_basis": "源未给每腿时刻；以 payload.timestamp 原文记录"}
    if not snap:
        return {**base, "quality_state": S.MISSING, "missing_reason": "no_snapshot_for_session",
                "skew25_30d_pp": None}
    feats = snap["features"]
    sk, pc, meta = feats["skew"], feats["pc"], feats["meta"]

    def hist(get):
        return [(d, get(f)) for d, f in history if get(f) is not None]
    skew_rank = S.rank_block(hist(lambda f: f["skew"]["skew25_30d_pp"]), sk["skew25_30d_pp"], S.VOL_LOOKBACK)
    atm_rank = S.rank_block(hist(lambda f: f["skew"]["atm_mid50_30d"]), sk["atm_mid50_30d"], S.VOL_LOOKBACK)
    pcv = (pc.get("window") or {}).get("volume") or {}
    pc_rank = S.rank_block(hist(lambda f: ((f["pc"].get("window") or {}).get("volume") or {}).get("value")),
                           pcv.get("value"), S.VOL_LOOKBACK)
    q = S.CURRENT          # 快照原文就是当时实际抓到的字节（两种模式都是真实时点版本）
    if sk["skew25_30d_pp"] is None and pcv.get("value") is None:
        q = S.INVALID
    return {**base, "source_hash": snap["ident"]["sha256"],
            "source_path_or_url": snap["ident"]["path"], "file_date": snap["file_date"].isoformat(),
            "captured_at": _iso(snap["ident"]["captured_at"]),
            "first_available_at": _iso(snap["ident"]["captured_at"]),
            "valuation_date": meta["valuation_date"].isoformat() if meta["valuation_date"] else None,
            "payload_timestamp_raw": meta["payload_timestamp_raw"],
            "underlying_last_trade_time_raw": meta["underlying_last_trade_time_raw"],
            "time_basis": meta["time_basis"], "event_date": meta["valuation_date"].isoformat()
            if meta["valuation_date"] else None,
            "skew": {k: v for k, v in sk.items()}, "pc": pc,
            "skew25_30d_pp": sk["skew25_30d_pp"], "rank_skew25_30d": skew_rank,
            "rank_atm_mid50_30d": atm_rank, "rank_pc_volume_window": pc_rank,
            "quality_state": q, "missing_reason": sk["missing_reason"]}


def build_row(*, instrument: str, session: date, session_status: str, mode: str,
              decision_at: datetime, components: dict) -> dict:
    hashes = {k: c.get("source_hash") or f"missing:{c.get('missing_reason')}" for k, c in components.items()}
    vintage = _sha({"rule": S.RULE_VERSION, "sources": hashes})[:16]
    return {KEY: f"{session.isoformat()}|{instrument}|{S.RULE_VERSION}|{vintage}",
            "effective_session": session.isoformat(), "session_status": session_status,
            "instrument": instrument, "rule_version": S.RULE_VERSION, "vintage_id": vintage,
            "mode": mode, "decision_asof": decision_at.isoformat(),
            "claims_role": "background", "components": components}


def write_row(row: dict, *, root: Path | None = None) -> str:
    """幂等写入；同 (session, 品种, 规则) 已有别的 vintage → 新行 supersedes 最新旧行，旧行不改。"""
    path = (root or DIR) / row["mode"] / f"{row['instrument']}.jsonl"
    with jl.locked(path):
        rows = jl.load_path(path, KEY)
        if any(r[KEY] == row[KEY] for r in rows):
            return "exists"
        same = [r for r in rows if r["effective_session"] == row["effective_session"]
                and r["rule_version"] == row["rule_version"]]
        row = dict(row)
        row["supersedes"] = same[-1][KEY] if same else None
        row["revision"] = len(same) + 1
        row["recorded_at"] = datetime.now(timezone.utc).isoformat()
        jl._check_finite(row)
        rows.append(row)
        rows.sort(key=lambda r: (r["effective_session"], r.get("revision", 1)))
        jl.atomic_write(path, rows, KEY)
    return "inserted"


# ═══════════════════════ I/O 编排 ═══════════════════════

def _instruments(keys):
    from undertow.core.config import load_config
    cfg = load_config()
    return [cfg.instruments[k] for k in (keys or INSTRUMENTS)]


def _vol_sources(insts, *, use_cache: bool) -> dict:
    from undertow.collect.cboe_vol import CboeVolSource
    src = CboeVolSource()
    out = {}
    for sym in {i.vol_index for i in insts if i.vol_index} | {BACKGROUND_VOL}:
        try:
            out[sym] = src.fetch_with_identity(sym, use_cache=use_cache)
        except Exception as e:                              # 源失败 → 分量 missing，行照写
            print(f"[sentiment] {sym} 波动指数取数失败：{type(e).__name__}: {e}", file=sys.stderr)
            out[sym] = None
    return out


def _cot_sources(insts, *, use_cache: bool) -> dict:
    import time as _t
    from undertow.collect.cftc_cot import CftcCotSource, raw_hash, strict_category_series
    src = CftcCotSource()
    out = {}
    for i in insts:
        if not i.cot:
            out[i.key] = None
            continue
        try:
            reps = src.fetch_history(i, lookback=COT_LOOKBACK_FETCH, use_cache=use_cache)
            out[i.key] = {"series": strict_category_series(reps, i.cot.report), "hash": raw_hash(reps),
                          "fetched_at": _t.time()}
        except Exception as e:
            print(f"[sentiment] {i.key} COT 取数失败：{type(e).__name__}: {e}", file=sys.stderr)
            out[i.key] = None
    return out


def _snapshot_index(sym: str, trading_days: list[date]) -> list[dict]:
    """该标的全部期权快照：认证 session、captured_at 与特征（按 session 升序；同 session 取最早抓取）。"""
    from undertow.collect.store import SnapshotStore
    from undertow.core.clock import certify_session
    st = SnapshotStore()
    out, seen = [], set()
    for fd in st.dates("options", sym):
        payload, ident = st.load_with_identity("options", sym, fd)
        if payload is None or ident is None:
            continue
        cert = certify_session(ident["captured_at"], trading_days)
        if cert["session"] is None or cert["session"] in seen:
            continue
        seen.add(cert["session"])
        ident = {k: v for k, v in ident.items() if k != "raw"}
        out.append({"file_date": fd, "session": cert["session"], "session_status": cert["status"],
                    "ident": ident, "features": options_features(payload)})
    return sorted(out, key=lambda x: x["session"])


def _rows_for_session(insts, session: date, session_status: str, decision_at: datetime, *, mode: str,
                      vols: dict, cots: dict, snaps: dict, trading_days) -> list[dict]:
    rows = []
    for i in insts:
        sym = i.options.symbol if i.options else None
        idx = snaps.get(sym) or []
        snap = next((s for s in idx if s["session"] == session
                     and s["ident"]["captured_at"] is not None
                     and datetime.fromtimestamp(s["ident"]["captured_at"], timezone.utc) < decision_at), None)
        hist = [(s["session"], s["features"]) for s in idx if s["session"] < session]
        comps = {
            "vol": vol_component(i.vol_index, vols.get(i.vol_index), session, trading_days, mode=mode),
            "options": options_component(snap, hist, mode=mode),
            "cot": cot_component(i.key, (cots.get(i.key) or {}).get("series"),
                                 (cots.get(i.key) or {}).get("hash"), decision_at, mode=mode,
                                 fetched_at=(cots.get(i.key) or {}).get("fetched_at")),
        }
        if i.key == "qqq":
            bg = vol_component(BACKGROUND_VOL, vols.get(BACKGROUND_VOL), session, trading_days, mode=mode)
            bg["note"] = "美股共享背景；预测检验时不另计一份纳指样本"
            comps["background_vix"] = bg
        rows.append(build_row(instrument=i.key, session=session, session_status=session_status,
                              mode=mode, decision_at=decision_at, components=comps))
    return rows


def _forward_session(now: datetime) -> tuple[date, str]:
    d = now.astimezone(ET).date()
    if mc.is_trading_day(d) and now < _open_dt(d):
        return d, "certified"
    x = d + timedelta(days=1)
    for _ in range(10):
        t = mc.is_trading_day(x)
        if t is None:
            while x.weekday() >= 5:
                x += timedelta(days=1)
            return x, "provisional"
        if t:
            return x, "certified"
        x += timedelta(days=1)
    return x, "provisional"


def cmd_forward(args) -> int:
    insts = _instruments(args.instruments)
    now = datetime.now(timezone.utc)
    session, status = _forward_session(now)
    td = mc.trading_days(date(2026, 1, 1), session) or []
    vols = _vol_sources(insts, use_cache=False)
    cots = _cot_sources(insts, use_cache=False)
    snaps = {i.options.symbol: _snapshot_index(i.options.symbol, td) for i in insts if i.options}
    rows = _rows_for_session(insts, session, status, now, mode="forward", vols=vols, cots=cots,
                             snaps=snaps, trading_days=td)
    for r in rows:
        print(f"{r['instrument']:7s} {r['effective_session']} {write_row(r)}")
    _print_rows(rows)
    return 0


def cmd_replay(args) -> int:
    insts = _instruments(args.instruments)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else datetime.now(ET).date()
    td = mc.trading_days(max(start, mc.COVERAGE[0]), end) or []
    vols = _vol_sources(insts, use_cache=True)
    cots = _cot_sources(insts, use_cache=True)
    all_td = mc.trading_days(mc.COVERAGE[0], end) or []
    snaps = {i.options.symbol: _snapshot_index(i.options.symbol, all_td) for i in insts if i.options}
    n = {"inserted": 0, "exists": 0}
    for s in td:
        if s < start:
            continue
        for r in _rows_for_session(insts, s, "certified", _open_dt(s) - timedelta(seconds=1), mode="replay",
                                   vols=vols, cots=cots, snaps=snaps, trading_days=all_td):
            n[write_row(r)] += 1
    print(f"replay {start}~{end}：新写 {n['inserted']} 行，已存在 {n['exists']} 行（data/history/sentiment/replay/）")
    return 0


def _fmt(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def _sg(v):
    return "—" if v is None else f"{v:+d}"


def _pct(rb):
    if not rb or rb.get("percentile") is None:
        return "—"
    tag = "（partial）" if rb.get("partial_window") else ""
    return f"{100 * rb['percentile']:.0f}% n={rb['n_prior']}{tag}"


def _print_rows(rows):
    print("\n| 品种 | 分量 | 原值 | 数据日 | 分位（prior-only） | 质量 |")
    print("|---|---|---:|---|---|---|")
    for r in rows:
        c = r["components"]
        v = c["vol"]
        print(f"| {r['instrument']} | {v.get('proxy')} | {_fmt(v.get('value'))} | {v.get('event_date') or '—'} "
              f"| {_pct(v)} | {v.get('quality_state')} |")
        if "background_vix" in c:
            b = c["background_vix"]
            print(f"| {r['instrument']} | VIX（背景） | {_fmt(b.get('value'))} | {b.get('event_date') or '—'} "
                  f"| {_pct(b)} | {b.get('quality_state')} |")
        o = c["options"]
        print(f"| {r['instrument']} | 25Δ 恒30 偏斜 pp（P−C） | {_fmt(o.get('skew25_30d_pp'))} "
              f"| {o.get('valuation_date') or '—'} | {_pct(o.get('rank_skew25_30d'))} | {o.get('quality_state')} |")
        pcv = (((o.get("pc") or {}).get("window") or {}).get("volume") or {})
        print(f"| {r['instrument']} | 源链 P/C 成交（0–45D） | {_fmt(pcv.get('value'))} "
              f"| {o.get('valuation_date') or '—'} | {_pct(o.get('rank_pc_volume_window'))} | {pcv.get('state', '—')} |")
        k = c["cot"]
        print(f"| {r['instrument']} | COT {k.get('category_actual', '?')} 净/OI | {_fmt(k.get('value'), 3)} "
              f"（多 {k.get('long')} 空 {k.get('short')}，周变 多{_sg(k.get('change_long'))}/空{_sg(k.get('change_short'))}） "
              f"| {k.get('event_date') or '—'} | {_pct(k.get('rank_net_over_oi'))} | {k.get('quality_state')} |")
    print("\n分位只是该变量在自身历史里的位置，不是反弹概率；三组分量不合成分数，不给方向。")


def cmd_show(args) -> int:
    rows = []
    for k in (args.instruments or INSTRUMENTS):
        p = DIR / args.mode / f"{k}.jsonl"
        try:
            rs = jl.load(p, KEY)
        except jl.LedgerCorruptError as e:
            print(f"[严重] {e}", file=sys.stderr)
            return 2
        if rs:
            rows.append(rs[-1])
    if not rows:
        print(f"{DIR / args.mode} 下还没有面板行")
        return 1
    _print_rows(rows)
    return 0


def coverage_audit(vols: dict, cots: dict, snaps: dict) -> dict:
    """可得性与版本覆盖审计（A2 前置）：不计算任何收益。"""
    out = {"rule_version": S.RULE_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
           "vol": {}, "cot": {}, "options": {}}
    for sym, src in vols.items():
        if not src:
            out["vol"][sym] = {"state": "missing"}
            continue
        ser = src["series"]
        n_rec = sum(1 for d, _ in ser if S.vol_point_vintage(sym, d) == S.RECONSTRUCTED)
        gaps = [[a[0].isoformat(), b[0].isoformat()] for a, b in zip(ser, ser[1:]) if (b[0] - a[0]).days > 10]
        out["vol"][sym] = {"first": ser[0][0].isoformat(), "last": ser[-1][0].isoformat(), "n": len(ser),
                           "n_reconstructed": n_rec, "n_unknown_vintage": len(ser) - n_rec,
                           "gaps_over_10d": gaps,
                           "source_hash": src["sha256"],
                           "note": "今天下载的历史；除 VXSLV 回算段外均为 unknown_vintage（未证明与当时公开值一致）"}
    for k, c in cots.items():
        if not c:
            out["cot"][k] = {"state": "missing"}
            continue
        ser = c["series"]
        out["cot"][k] = {"first": ser[0]["report_date"].isoformat(), "last": ser[-1]["report_date"].isoformat(),
                         "n_reports": len(ser), "category_actual": ser[-1]["category_actual"],
                         "report_type": ser[-1]["report_type"], "vintage": "today_revision_only（全部 reconstructed）",
                         "n_missing_long_or_short": sum(1 for r in ser if r["long"] is None or r["short"] is None)}
    for sym, idx in snaps.items():
        sk = [s for s in idx if s["features"]["skew"]["skew25_30d_pp"] is not None]
        out["options"][sym] = {"n_sessions": len(idx), "first": idx[0]["session"].isoformat() if idx else None,
                               "last": idx[-1]["session"].isoformat() if idx else None,
                               "n_skew30_valid": len(sk),
                               "n_valuation_unknown": sum(1 for s in idx
                                                          if s["features"]["meta"]["valuation_date"] is None),
                               "missing_reasons": sorted({s["features"]["skew"]["missing_reason"] for s in idx
                                                          if s["features"]["skew"]["missing_reason"]})}
    return out


def cmd_audit(args) -> int:
    insts = _instruments(args.instruments)
    end = datetime.now(ET).date()
    all_td = mc.trading_days(mc.COVERAGE[0], end) or []
    vols = _vol_sources(insts, use_cache=True)
    cots = _cot_sources(insts, use_cache=True)
    snaps = {i.options.symbol: _snapshot_index(i.options.symbol, all_td) for i in insts if i.options}
    rep = coverage_audit(vols, cots, snaps)
    path = DIR / "coverage_audit.json"
    from undertow.collect.asof_history import atomic_write_json
    atomic_write_json(path, rep)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"\n已写 {path}")
    return 0


def register(sub):
    p = sub.add_parser("sentiment", help="情绪面板 v0（Codex 066）：波动/期权不对称/COT 分列描述；只读，不给方向")
    ss = p.add_subparsers(dest="sentiment_cmd", required=True)
    f = ss.add_parser("forward", help="前瞻：现在抓取并写一行（不用缓存）")
    f.add_argument("instruments", nargs="*")
    f.set_defaults(func=cmd_forward)
    r = ss.add_parser("replay", help="回补：每个交易日只用开盘前可得输入重建（写 replay 台账）")
    r.add_argument("instruments", nargs="*")
    r.add_argument("--start", required=True); r.add_argument("--end")
    r.set_defaults(func=cmd_replay)
    s = ss.add_parser("show", help="打印最新一行")
    s.add_argument("instruments", nargs="*"); s.add_argument("--mode", choices=("forward", "replay"),
                                                              default="forward")
    s.set_defaults(func=cmd_show)
    a = ss.add_parser("audit", help="可得性与版本覆盖审计（不算收益）")
    a.add_argument("instruments", nargs="*")
    a.set_defaults(func=cmd_audit)
