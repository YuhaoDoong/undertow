"""daily 的阶段状态与缺口检查（Codex 033 A2）。只读行情无关的本地文件；写的只有阶段哨兵与缺口台账。

  python3 scripts/daily_stage.py snapshots <ET日>        # 输出 MISSING / CORRUPT / IDENTITY 三行
  python3 scripts/daily_stage.py done-match <ET日>       # rc 0 = 当日下游阶段已在【同一快照身份】下完成
  python3 scripts/daily_stage.py mark-done <ET日>        # 发布成功后调用：记下完成时的快照身份
  python3 scripts/daily_stage.py lastrun <ET日>          # 窗口内每次运行调用：记最后一次运行的 ET 时刻
  python3 scripts/daily_stage.py gapcheck <ET日>         # 截止后只读缺口检查：rc 0 无缺口 / 2 有缺口 / 3 非交易日

为什么：旧的幂等守卫只看「当日快照文件都在」——保存快照后、研报/台账/发布前崩溃，剩余阶段会被永久跳过；
损坏的 gz 也算「齐」。末班车告警打在最后一次抓取【之前】、09:00 后直接退出，08:40 睡眠→09:05 醒来就一声不吭。
缺口按数据集分别记（快照 / 影子账 / 方向台账），记 expected、missing_stage、reason、first_detected_at、cutoff、protocol_version，
与原样本并行存，不改旧研究行、不补造候选、不补 0。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
LOGS = ROOT / "data/logs"
GAPS = ROOT / "data/history/coverage_gaps"
PROTOCOL = "coverage-gaps-v1-20261007"
CUTOFF_ET = "09:30"


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(name, path)


def expected_symbols() -> list:
    from undertow.core.config import load_config
    return [v.options.symbol for v in load_config().instruments.values() if v.options]


def snapshot_state(day: str, syms: list, root: Path = ROOT) -> dict:
    """缺失 / 损坏（gz 解不开或不是 JSON）/ 快照身份（各文件内容哈希的合并哈希，只在全部有效时给出）。"""
    missing, corrupt, hashes = [], [], []
    for s in syms:
        f = root / "data/snapshots/options" / s / f"{day}.json.gz"
        if not f.exists():
            missing.append(s); continue
        try:
            raw = f.read_bytes()
            json.loads(gzip.decompress(raw))
        except (OSError, ValueError, EOFError):
            corrupt.append(s); continue
        hashes.append(f"{s}:{hashlib.sha256(raw).hexdigest()}")
    ident = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()[:16] if not (missing or corrupt) else ""
    return {"missing": missing, "corrupt": corrupt, "identity": ident}


def _done_path(day: str, logs: Path = LOGS) -> Path:
    return logs / f".daily_done_{day}.json"


def done_match(day: str, identity: str, logs: Path = LOGS) -> bool:
    try:
        d = json.loads(_done_path(day, logs).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return bool(identity) and d.get("identity") == identity


def sym_to_key() -> dict:
    from undertow.core.config import load_config
    return {v.options.symbol: k for k, v in load_config().instruments.items() if v.options}


def classify_missing(day: str, sym: str, *, status: dict | None, last_run_et: str | None, holiday: bool,
                     key: str | None = None) -> str:
    """缺快照的原因：非交易日 / 抓取失败 / 源未发布（最后一次运行时仍与上一交易日相同）/ 错过窗口（截止前最后一次运行早于 08:45 或根本没跑）。"""
    if holiday:
        return "non_trading_day_expected"
    if not last_run_et or last_run_et < "08:45":
        return "missed_window（截止前末班运行未发生：睡眠/晚唤醒/前轮超时）"
    items = {i.get("instrument"): i for i in (status or {}).get("items", [])}
    st = (items.get(key) or {}).get("status") if key else None
    if st == "failed":
        return "fetch_failed"
    return "not_published（末班运行时源端仍为上一交易日数据）"


def gapcheck(day: str, *, root: Path = ROOT, logs: Path = LOGS, gaps_dir: Path = GAPS, syms: list | None = None,
             holiday: bool | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    syms = syms if syms is not None else expected_symbols()
    if holiday is None:
        try:
            from undertow.core import market_calendar as mc
            holiday = mc.is_trading_day(date.fromisoformat(day)) is False
        except Exception:
            holiday = False
    if holiday:
        return {"day": day, "holiday": True, "gaps": []}
    st = snapshot_state(day, syms, root)
    try:
        status = json.loads((logs / f".status_snapshot_{day}.json").read_text("utf-8"))
    except (OSError, ValueError):
        status = None
    try:
        last_run = (logs / f".daily_lastrun_{day}").read_text("utf-8").strip()
    except OSError:
        last_run = None
    gaps = []
    try:
        keys = sym_to_key()
    except Exception:
        keys = {}
    for s in st["missing"]:
        gaps.append({"dataset": "options_snapshot_premarket", "instrument": s, "missing_stage": "collect",
                     "reason": classify_missing(day, s, status=status, last_run_et=last_run, holiday=False, key=keys.get(s))})
    for s in st["corrupt"]:
        gaps.append({"dataset": "options_snapshot_premarket", "instrument": s, "missing_stage": "collect",
                     "reason": "corrupt_file（gz/JSON 无法解析）"})
    gaps += _ledger_gaps(day, root)
    rec = []
    for g in gaps:
        rec.append({"key": f"{day}|{g['dataset']}|{g['instrument']}", "session": day, **g, "expected": True,
                    "first_detected_at": now.isoformat(), "cutoff_et": CUTOFF_ET, "protocol_version": PROTOCOL,
                    "last_daily_run_et": last_run})
    _append_new(gaps_dir / f"{day[:7]}.jsonl", rec)
    return {"day": day, "holiday": False, "gaps": rec}


def _ledger_gaps(day: str, root: Path) -> list:
    """影子账 v5 与方向台账（conviction 前瞻）按各自注册的品种集核对当日行（不照搬 15 个到其它研究）。"""
    out = []
    try:
        from undertow.analyze import shadow as sh
        for k in sh.CONFIG["instruments"]:
            f = root / "data/history/shadow" / sh.CONFIG["version"] / f"{k}.jsonl"
            rows = [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()] if f.exists() else []
            if not any(r.get("session") == day for r in rows):
                out.append({"dataset": f"shadow:{sh.CONFIG['version']}", "instrument": k, "missing_stage": "capture",
                            "reason": "no_row（截止前未生成盘前冻结行；不补造候选、收益不计 0）"})
    except Exception as e:                                  # 研究配置读不到：如实报告，不当作无缺口
        out.append({"dataset": "shadow", "instrument": "*", "missing_stage": "check", "reason": f"check_failed：{type(e).__name__}"})
    d = root / "data/history/direction_ledger/conviction-h1-v1-20260928/prospective"
    for f in sorted(d.glob("*.jsonl")) if d.exists() else []:
        rows = [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()]
        if not any(r.get("session") == day for r in rows):
            out.append({"dataset": "dirledger:conviction-h1-v1-20260928", "instrument": f.stem, "missing_stage": "record",
                        "reason": "no_row"})
    return out


def _append_new(path: Path, rows: list) -> None:
    seen = set()
    if path.exists():
        for x in path.read_text("utf-8").splitlines():
            if x.strip():
                seen.add(json.loads(x)["key"])
    new = [r for r in rows if r["key"] not in seen]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())


def main() -> int:
    cmd, day = sys.argv[1], sys.argv[2]
    if cmd == "snapshots":
        st = snapshot_state(day, expected_symbols())
        print("MISSING " + " ".join(st["missing"])); print("CORRUPT " + " ".join(st["corrupt"])); print("IDENTITY " + st["identity"])
        return 0
    if cmd == "done-match":
        st = snapshot_state(day, expected_symbols())
        return 0 if done_match(day, st["identity"]) else 1
    if cmd == "mark-done":
        st = snapshot_state(day, expected_symbols())
        if not st["identity"]:
            print("快照不全或损坏，不标记完成"); return 1
        _atomic(_done_path(day), json.dumps({"identity": st["identity"], "at": datetime.now(timezone.utc).isoformat()}))
        return 0
    if cmd == "lastrun":
        _atomic(LOGS / f".daily_lastrun_{day}", datetime.now(ET).strftime("%H:%M"))
        return 0
    if cmd == "gapcheck":
        r = gapcheck(day)
        if r["holiday"]:
            print(f"{day} 非交易日：不期望数据"); return 3
        for g in r["gaps"]:
            print(f"缺口 {g['dataset']} {g['instrument']}：{g['reason']}")
        print(f"{day} 缺口 {len(r['gaps'])} 项")
        return 2 if r["gaps"] else 0
    print("未知命令"); return 1


if __name__ == "__main__":
    sys.exit(main())
