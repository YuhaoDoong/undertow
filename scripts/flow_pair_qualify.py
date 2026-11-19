"""P0（Codex 071 工作包 P0-1/P0-3）：期权快照逐文件 / 逐对资格表 + 日度净压力（只算信号，不碰任何价格与收益）。

为什么有这个脚本：1119 讨论信里的翻转率/自相关是临时脚本算的，把「相邻两份文件」直接当「相邻交易日」，
周末文件、缺日、同一结算日的重抓都混在一起；其中「非相邻对」计数的条件写反了（相邻对反而被计入），
Codex 071 指出 TLT 56 恰为 58−2 的补数。本脚本替代那份临时脚本，逐项留证据。

口径（只读，零依赖；不写 data/，不登记 provenance，不隔离坏文件——只报告）：
- 逐文件：file_date、sha256（压缩字节）、captured_at、抓取时段、decision_session（clock.decision_session：最早可用于交易的那天）、
  quote_session = decision_session 的前一交易日（链里报价/IV 所属收盘）。
- oi_session 推断：与上一份【可读】文件比 settled_oi_change（剔除同键合约，与落盘闸门同一判据）：
    == 0 → 与上一份同一结算日（重抓/周末未结算，不是新的一天）；
    > 0  → 新结算日 = quote_session（若 ≤ 上一份 oi_session 则记 conflict）；
    第一份文件 → 假定 = quote_session（first_file_assumed）；无 captured_at → unknown。
- 结算日配对（session_pairs）：每个 oi_session 选一份代表文件——优先 quote_session == oi_session 的最早一份（报价与 OI 同属一日）；
  相邻结算日的代表文件配成一对，X = 新的 oi_session。资格（按优先级取第一个不合格原因）：
    load_error / unknown_session / conflict / stale_chain（sha 命中 11/16 旧链审计）/ gap（中间缺结算日）/
    quote_misaligned（任一侧代表文件报价日 ≠ OI 日）→ 否则 eligible。
  原始相邻文件对（file_pairs，生产 latest_two 的做法）另表列出，便于对照。
- 净压力：仅对 eligible 结算日对跑 analyze_flow(prev, curr, today=X, horizon_days=45)，记 up/down/net；不输出 spot。
- 统计：翻转率只在【相邻交易日且都 eligible】的成对上算；同时给 2p(1−p) 的独立基准；自相关只用两端都在的滞后对。
  A5 = 最近 5 个应有交易日全部 eligible 才给值，否则 None + 覆盖 k/5（warmup 同理）。

用法：python3 scripts/flow_pair_qualify.py --out DIR [--symbols GLD,SLV,USO,QQQ,TLT] [--until 2026-11-18]
输出：DIR/files_<SYM>.jsonl、DIR/session_pairs_<SYM>.jsonl、DIR/file_pairs_<SYM>.jsonl、DIR/summary.json
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.analyze.flow import analyze_flow  # noqa: E402
from undertow.cli import snapshot_from_payload  # noqa: E402
from undertow.collect.chain_dedup import settled_oi_change  # noqa: E402
from undertow.core import market_calendar as mc  # noqa: E402
from undertow.core.clock import capture_phase, decision_session  # noqa: E402

ET = ZoneInfo("America/New_York")
SNAP = ROOT / "data/snapshots/options"
STALE_AUDIT = ROOT / "data/history/snapshot_audit/stale_chain_dupkey_20261116.jsonl"
KEYS = {"GLD": "gold", "SLV": "silver", "USO": "wti", "QQQ": "qqq", "TLT": "tlt", "TQQQ": "tqqq",
        "SPY": "spy", "IWM": "iwm"}
HORIZON = 45
VERSION = "flow-pair-qualify-v1-20261119"


def _iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def stale_shas() -> set[str]:
    if not STALE_AUDIT.exists():
        return set()
    out = set()
    for line in STALE_AUDIT.read_text("utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("kind") == "stale_version" and r.get("sha256"):
                out.add(r["sha256"])
    return out


def read_file(path: Path):
    """(payload, sha256, captured_at, error)。只读，不隔离。"""
    comp = path.read_bytes()
    sha = hashlib.sha256(comp).hexdigest()
    try:
        rec = json.loads(gzip.decompress(comp).decode("utf-8"))
        if not isinstance(rec, dict) or "payload" not in rec:
            return None, sha, None, "missing_payload"
        ca = rec.get("captured_at")
        return rec["payload"], sha, (float(ca) if isinstance(ca, (int, float)) else None), None
    except Exception as e:  # noqa: BLE001 —— 审计脚本：任何解析失败都记原因，不吞
        return None, sha, None, f"{type(e).__name__}"


def qualify_files(sym: str, until: date, tdays: list[date], stale: set[str], snap_dir: Path | None = None):
    key = KEYS.get(sym, sym.lower())
    base = (snap_dir or SNAP) / sym
    files = sorted(p for p in base.glob("*.json.gz") if date.fromisoformat(p.name[:10]) <= until)
    rows, snaps = [], {}
    prev = None                                   # 上一份【可读】文件的行
    for p in files:
        fd = date.fromisoformat(p.name[:10])
        payload, sha, ca, err = read_file(p)
        r = {"file_date": fd.isoformat(), "path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p), "sha256": sha,
             "file_date_is_trading_day": mc.is_trading_day(fd), "captured_at": _iso(ca),
             "capture_phase": capture_phase(ca) if ca is not None else None,
             "decision_session": None, "quote_session": None, "oi_session": None,
             "oi_inference": None, "settled_change_vs_prev_readable": None,
             "stale_chain_audit_hit": sha in stale, "load_error": err}
        snap = None
        if err is None:
            try:
                snap = snapshot_from_payload(payload, key, sym)
            except Exception as e:  # noqa: BLE001
                r["load_error"] = f"snapshot_from_payload:{type(e).__name__}"
        r["oi_session_max_possible"] = None
        if ca is not None:
            ds = decision_session(ca, tdays)
            r["decision_session"] = ds and ds.isoformat()
            if ds is not None:
                r["quote_session"] = mc.prev_trading_day(ds).isoformat()
            # 交易日 S 的结算在 S 之后的日历日隔夜才落地 → 抓取日（美东日历日）之前最近的交易日是上限；
            # 收盘当晚抓、盘中抓都适用（盘中抓 decision_session=None，但 OI 结算日照样能界定）
            r["oi_session_max_possible"] = mc.prev_trading_day(datetime.fromtimestamp(ca, ET).date()).isoformat()
        mx = r["oi_session_max_possible"]
        if snap is None:
            r["oi_inference"] = "load_error"
        elif mx is None:
            r["oi_inference"] = "unknown_no_capture_time"
        elif prev is None:
            r["oi_session"], r["oi_inference"] = mx, "first_file_assumed_latest_possible"
        else:
            ch = settled_oi_change(snaps[prev["file_date"]], snap)
            r["settled_change_vs_prev_readable"] = ch
            r["compared_with"] = prev["file_date"]
            po = prev["oi_session"]
            if ch == 0:
                r["oi_session"], r["oi_inference"] = po, "same_oi_as_prev"
            else:
                nxt = mc.next_trading_day(date.fromisoformat(po)).isoformat()
                if nxt == mx:
                    r["oi_session"], r["oi_inference"] = mx, "new_settlement_adjacent"
                elif nxt < mx:      # 只观察到一次变化，可能跨了不止一个结算日（中间缺文件）——不猜
                    r["oi_session"], r["oi_inference"] = mx, "new_settlement_span_uncertain"
                else:
                    r["oi_inference"] = "conflict_changed_but_no_later_session_possible"
        if snap is not None:
            snaps[r["file_date"]] = snap
            if r["oi_session"] is not None:      # 比较基准只取结算日已知的文件，未知的不让链条断掉
                prev = r
        rows.append(r)
    return rows, snaps


def session_pairs(rows, snaps, tdays):
    reps: dict[str, dict] = {}
    for r in rows:                                          # 代表文件：报价与 OI 同日的最早一份；没有则取最早一份
        s = r["oi_session"]
        if s is None:
            continue
        cur = reps.get(s)
        aligned = r["quote_session"] == s
        if cur is None or (aligned and cur["quote_session"] != s):
            reps[s] = r
    sess = sorted(reps)
    out = []
    for a, b in zip(sess, sess[1:]):
        pa, pb = reps[a], reps[b]
        gap = mc.prev_trading_day(date.fromisoformat(b)).isoformat() != a
        reason = None
        uncertain = ("new_settlement_span_uncertain", "first_file_assumed_latest_possible")
        if pa["oi_inference"] in uncertain or pb["oi_inference"] in uncertain:
            reason = "session_uncertain"
        elif pa["stale_chain_audit_hit"] or pb["stale_chain_audit_hit"]:
            reason = "stale_chain"
        elif gap:
            reason = "gap_missing_sessions"
        elif pa["quote_session"] != a or pb["quote_session"] != b:
            reason = "quote_misaligned"
        rec = {"X": b, "prev_session": a, "prev_file": pa["file_date"], "curr_file": pb["file_date"],
               "prev_sha256": pa["sha256"], "curr_sha256": pb["sha256"],
               "status": "eligible" if reason is None else "ineligible", "reason": reason,
               "up": None, "down": None, "net": None, "flow_call": None}
        if reason is None:
            fa = analyze_flow(snaps[pa["file_date"]], snaps[pb["file_date"]], today=date.fromisoformat(b),
                              horizon_days=HORIZON, prev_date=pa["file_date"], curr_date=pb["file_date"])
            rec.update(up=round(fa.upside_pressure, 3), down=round(fa.downside_pressure, 3),
                       net=round(fa.upside_pressure - fa.downside_pressure, 3),
                       flow_call=("abstain" if fa.call.abstain else fa.call.direction))
        out.append(rec)
    return out


def file_pairs(rows):
    ok = [r for r in rows if r["load_error"] is None]
    return [{"prev_file": a["file_date"], "curr_file": b["file_date"],
             "filename_adjacent": mc.prev_trading_day(date.fromisoformat(b["file_date"])).isoformat() == a["file_date"],
             "prev_oi_session": a["oi_session"], "curr_oi_session": b["oi_session"],
             "same_oi_session": a["oi_session"] is not None and a["oi_session"] == b["oi_session"]}
            for a, b in zip(ok, ok[1:])]


def stats(sp, tdays):
    nets = {r["X"]: r["net"] for r in sp if r["status"] == "eligible"}
    idx = {d.isoformat(): i for i, d in enumerate(tdays)}
    xs = sorted(nets)
    adj = [(a, b) for a, b in zip(xs, xs[1:]) if idx.get(b, -9) - idx.get(a, -99) == 1]
    nz = [(a, b) for a, b in adj if nets[a] != 0 and nets[b] != 0]
    flips = sum((nets[a] > 0) != (nets[b] > 0) for a, b in nz)
    signs = [nets[x] > 0 for x in xs if nets[x] != 0]
    p = sum(signs) / len(signs) if signs else None

    def ac(k):
        prs = []
        for a in xs:
            j = idx[a] + k
            if j < len(tdays) and (b := tdays[j].isoformat()) in nets:
                prs.append((nets[a], nets[b]))
        if len(prs) < 3:
            return None, len(prs)
        return round(statistics.correlation([u for u, _ in prs], [v for _, v in prs]), 3), len(prs)

    a5 = []
    for x in xs:
        i = idx[x]
        win = [tdays[j].isoformat() for j in range(i - 4, i + 1) if j >= 0]       # 日历起点前的天数算缺（warmup）
        cov = sum(w in nets for w in win)
        a5.append({"X": x, "coverage": f"{cov}/5", "A5": round(sum(nets[w] for w in win), 3) if cov == 5 else None})
    return {"eligible_sessions": len(nets), "adjacent_eligible_pairs": len(adj), "nonzero_adjacent_pairs": len(nz),
            "flip_rate": round(flips / len(nz), 3) if nz else None, "flips": flips,
            "share_positive": round(p, 3) if p is not None else None,
            "independent_flip_baseline_2p(1-p)": round(2 * p * (1 - p), 3) if p is not None else None,
            "autocorr": {f"lag{k}": dict(zip(("r", "n_pairs"), ac(k))) for k in (1, 2, 5)},
            "A5_complete": sum(r["A5"] is not None for r in a5)}, a5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", default="GLD,SLV,USO,QQQ,TLT")
    ap.add_argument("--until", default=None)
    a = ap.parse_args()
    until = date.fromisoformat(a.until) if a.until else date.today()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tdays = mc.trading_days(date(2026, 6, 1), mc.next_trading_day(until) or until)
    if not tdays:
        sys.exit("交易日历未覆盖所需区间")
    stale = stale_shas()
    summary = {"version": VERSION, "until": until.isoformat(), "horizon_days": HORIZON,
               "calendar_hash": mc.calendar_hash(), "command": " ".join(sys.argv),
               "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "no_prices_or_returns": True, "symbols": {}}
    for sym in a.symbols.split(","):
        rows, snaps = qualify_files(sym, until, tdays, stale)
        sp = session_pairs(rows, snaps, tdays)
        fp = file_pairs(rows)
        st, a5 = stats(sp, tdays)
        for name, data in (("files", rows), ("session_pairs", sp), ("file_pairs", fp), ("a5", a5)):
            (out / f"{name}_{sym}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in data),
                                                     "utf-8")
        from collections import Counter
        summary["symbols"][sym] = {
            "files": len(rows),
            "file_inference": dict(Counter(r["oi_inference"] for r in rows)),
            "nontrading_file_dates": [r["file_date"] for r in rows if r["file_date_is_trading_day"] is False],
            "file_pairs": len(fp), "file_pairs_filename_nonadjacent": sum(not r["filename_adjacent"] for r in fp),
            "file_pairs_same_oi_session": sum(r["same_oi_session"] for r in fp),
            "session_pairs": len(sp), "session_pair_status": dict(Counter(r["reason"] or "eligible" for r in sp)),
            "flow_call_on_eligible": dict(Counter(r["flow_call"] for r in sp if r["status"] == "eligible")),
            "inputs_sha256": hashlib.sha256("".join(r["sha256"] for r in rows).encode()).hexdigest(),
            **st}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
