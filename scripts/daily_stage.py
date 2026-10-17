"""daily 的阶段状态与缺口检查（Codex 033 A2）。只读行情无关的本地文件；写的只有阶段哨兵与缺口台账。

  python3 scripts/daily_stage.py snapshots <ET日>        # 输出 MISSING / CORRUPT / IDENTITY 三行
  python3 scripts/daily_stage.py done-match <ET日>       # rc 0 = 当日下游阶段已在【同一快照身份】下完成
  python3 scripts/daily_stage.py mark-done <ET日>        # 发布成功后调用：必需阶段全部 ok 才记完成（附阶段表）
  python3 scripts/daily_stage.py stage-set <ET日> <阶段> <ok|partial|failed|pending|skipped> <rc>
  python3 scripts/daily_stage.py stage-ok <ET日> <阶段>  # rc 0 = 该阶段已在当前快照身份下 ok（续跑时跳过）
  python3 scripts/daily_stage.py record-complete <ET日> <记录类阶段>  # rc 0 = 当日所有注册品种都已落行
  python3 scripts/daily_stage.py trading-day <ET日>      # rc 0 交易日（或日历未覆盖）/ 3 确定非交易日
  python3 scripts/daily_stage.py lastrun <ET日>          # 窗口内每次运行调用：记最后一次运行的 ET 时刻
  python3 scripts/daily_stage.py gapcheck <ET日>         # 单日只读缺口检查：rc 0 无缺口 / 2 有缺口 / 3 非交易日
  python3 scripts/daily_stage.py gapcheck-due            # 补查所有截止(ET 09:30)已过、未查的交易日（水位，最多 5 天/次）
  python3 scripts/daily_stage.py gapcheck-pending        # rc 0 = 有待查会话

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


def is_non_trading(day: str) -> bool:
    """只有日历明确判为非交易日才返回 True；日历未覆盖或读不到 → False（未知不当作休市，不压告警）。"""
    try:
        from undertow.core import market_calendar as mc
        return mc.is_trading_day(date.fromisoformat(day)) is False
    except Exception:
        return False


# Codex 034 §3.1：发布成功 ≠ 分析完整。每个阶段在【当前快照身份】下记状态；只有 REQUIRED 全部 ok 才能 mark-done。
# 必需 = 开盘前须冻结或不可再生的产物 + 发布；可选 = 次日会自然补上的回填/私有报告（失败保留 pending，不借发布清空）。
REQUIRED_STAGES = ("report", "shadow_capture", "dirledger_record", "conviction_record", "expiry_profile",
                   "inputs_archive", "publish")
OPTIONAL_STAGES = ("report_v2", "paper_book", "signals_backfill", "shadow_settle", "dirledger_score",
                   "author_levels", "flow_side", "sample_check", "option_bars",
                   # Codex 056：v5 新鲜度侧车、主墙研究账。可选：研究账开盘后不能再冻结，列为必需会让 daily 整天卡在未完成；
                   # 开盘前另由 session_hooks ⑰ 每 5 分钟重试
                   "shadow_freshness", "mainwall_capture", "mainwall_settle")
STAGE_STATUSES = ("ok", "ok_ineligible", "partial", "failed", "pending", "skipped")
# ok_ineligible：记录类阶段所有注册品种都已落【终态】行，但有行不合格（晚于开盘、身份有问题或未知）。
# 冻结行不能重写来「补合格」，所以算阶段完成（政策：不可再生数据先封账），同时由缺口检查报质量缺口。
COMPLETE_STATUSES = ("ok", "ok_ineligible")


def _stages_path(day: str, logs: Path = LOGS) -> Path:
    return logs / f".daily_stages_{day}.json"


def load_stages(day: str, identity: str, logs: Path = LOGS) -> dict:
    """当前快照身份下的阶段表；身份变了（新快照落盘）→ 视为全新一轮，旧状态不沿用。文件损坏 → 隔离保留、按空表处理。"""
    f = _stages_path(day, logs)
    try:
        d = json.loads(f.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        try:
            f.replace(f.with_name(f.name + f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"))
        except OSError:
            pass
        return {}
    return d.get("stages", {}) if d.get("identity") == identity and identity else {}


def set_stage(day: str, identity: str, name: str, status: str, rc: int, logs: Path = LOGS) -> None:
    if name not in REQUIRED_STAGES + OPTIONAL_STAGES:
        raise ValueError(f"未登记的阶段：{name}")
    if status not in STAGE_STATUSES:
        raise ValueError(f"未知状态：{status}")
    if not identity:
        raise ValueError("快照不全或损坏，没有快照身份，不记阶段")
    st = load_stages(day, identity, logs)
    st[name] = {"status": status, "rc": rc, "at": datetime.now(timezone.utc).isoformat()}
    _atomic(_stages_path(day, logs), json.dumps({"identity": identity, "stages": st}, ensure_ascii=False, indent=1))


def stage_ok(day: str, identity: str, name: str, logs: Path = LOGS) -> bool:
    return (load_stages(day, identity, logs).get(name) or {}).get("status") == "ok"


def missing_required(day: str, identity: str, logs: Path = LOGS) -> list:
    st = load_stages(day, identity, logs)
    return [n for n in REQUIRED_STAGES if (st.get(n) or {}).get("status") not in COMPLETE_STATUSES]


def optional_incomplete(day: str, identity: str, logs: Path = LOGS) -> list:
    st = load_stages(day, identity, logs)
    return [n for n in OPTIONAL_STAGES if n in st and st[n].get("status") not in COMPLETE_STATUSES]


def _done_path(day: str, logs: Path = LOGS) -> Path:
    return logs / f".daily_done_{day}.json"


DONE_SCHEMA = 2


def done_match(day: str, identity: str, logs: Path = LOGS) -> bool:
    """完成凭证必须是当前 schema、同一快照身份，且凭证里记的必需阶段全部完成（Codex 036 §4：旧格式只有 identity
    的完成文件不再被接受 —— 降级为待核验，下次续跑只补未完成阶段）。"""
    try:
        d = json.loads(_done_path(day, logs).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not identity or d.get("schema") != DONE_SCHEMA or d.get("identity") != identity:
        return False
    st = d.get("stages") or {}
    return all((st.get(n) or {}).get("status") in COMPLETE_STATUSES for n in REQUIRED_STAGES)


def sym_to_key() -> dict:
    from undertow.core.config import load_config
    return {v.options.symbol: k for k, v in load_config().instruments.items() if v.options}


def classify_missing(day: str, sym: str, *, status: dict | None, last_run_et: str | None, holiday: bool,
                     key: str | None = None) -> str:
    """缺快照的原因。只有【明确证据】才归因（Codex 034 §3.3）：
    - 末班运行未发生 / 早于 08:45 → missed_window；
    - 状态文件缺失或损坏（status=None）→ unknown，不能推断「源未发布」；
    - 状态文件里该品种 failed → fetch_failed；明确 unchanged（与上一交易日逐行相同）→ not_published；
    - 其余（品种不在状态里、状态值不认识）→ unknown。"""
    if holiday:
        return "non_trading_day_expected"
    if not last_run_et or last_run_et < "08:45":
        return "missed_window（截止前末班运行未发生：睡眠/晚唤醒/前轮超时）"
    if status is None or status.get("schema") != 1:
        return "unknown（末班运行的状态文件缺失或损坏，无法归因）"
    sts = {i.get("status") for i in status.get("items", []) if i.get("instrument") == key} if key else set()
    if "failed" in sts:
        return "fetch_failed"
    if "unchanged" in sts:
        return "not_published（末班运行时源端仍为上一交易日数据）"
    return "unknown（状态文件里没有该品种的明确结果）"


MONITOR_START = "2026-10-07"     # 本协议起点：更早的会话不自动检查（此前已人工回填的记录保留）
MAX_SESSIONS_PER_RUN = 5         # 一次最多补查 5 个交易日，积压下次续（Codex 034 §3.2）


def _cutoff_dt(day: str) -> datetime:
    from datetime import time as _t
    h, m = (int(x) for x in CUTOFF_ET.split(":"))
    return datetime.combine(date.fromisoformat(day), _t(h, m), tzinfo=ET)


def _watermark_path(logs: Path = LOGS) -> Path:
    return logs / ".gapcheck_watermark.json"


def due_sessions(now: datetime, logs: Path = LOGS, *, start: str = MONITOR_START,
                 is_trading=None) -> tuple[list, str | None]:
    """截止（ET 09:30）已过、尚未检查的交易日（最多 MAX_SESSIONS_PER_RUN 个）；遇到日历未覆盖的日子停在那里。
    返回 (待检查日列表, 日历未知的那一天或 None)。非交易日直接跳过（由调用方推进水位）。"""
    from datetime import timedelta
    if is_trading is None:
        from undertow.core import market_calendar as mc
        is_trading = mc.is_trading_day
    try:
        wm = json.loads(_watermark_path(logs).read_text("utf-8"))["last_checked"]
        d = date.fromisoformat(wm) + timedelta(days=1)
    except (OSError, ValueError, KeyError):
        d = date.fromisoformat(start)
    out, unknown, n_trading = [], None, 0
    while n_trading < MAX_SESSIONS_PER_RUN and _cutoff_dt(d.isoformat()) <= now:   # 只按交易日计名额，周末不占
        t = is_trading(d)
        if t is None:
            unknown = d.isoformat(); break
        out.append((d.isoformat(), bool(t)))
        n_trading += bool(t)
        d += timedelta(days=1)
    return out, unknown


def set_watermark(day: str, logs: Path = LOGS) -> None:
    _atomic(_watermark_path(logs), json.dumps({"last_checked": day, "at": datetime.now(timezone.utc).isoformat()}))


def gapcheck(day: str, *, root: Path = ROOT, logs: Path = LOGS, gaps_dir: Path = GAPS, syms: list | None = None,
             holiday: bool | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    syms = syms if syms is not None else expected_symbols()
    if holiday is None:
        holiday = is_non_trading(day)
    if holiday:
        return {"day": day, "holiday": True, "gaps": [], "resolved": []}
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
    lg = _ledger_gaps(day, root)
    failed = [g for g in lg if g.get("check_failed")]                 # 机器字段，不 grep 原因文案
    gaps += [g for g in lg if not g.get("check_failed")]
    f = gaps_dir / f"{day[:7]}.jsonl"
    old_rows = [r for r in _read_gaps(f) if r.get("session") == day]
    state, count = {}, {}
    for r in old_rows:                                                # 每个缺口的最新状态：open / resolved / reopened
        b = r.get("resolves") or r.get("base_key") or r["key"]
        count[b] = count.get(b, 0) + 1
        state[b] = "closed" if r.get("status") == "resolved" else "open"
    new_rows, opened, resolved, reopened = [], [], [], []
    current = {}
    for g in gaps:
        b = f"{day}|{g['dataset']}|{g['instrument']}"
        current[b] = g
        base = {"session": day, **g, "base_key": b, "expected": True, "cutoff_et": CUTOFF_ET,
                "protocol_version": PROTOCOL, "last_daily_run_et": last_run}
        if b not in state:
            r = {"key": b, **base, "status": "open", "first_detected_at": now.isoformat()}
            new_rows.append(r); opened.append(r)
        elif state[b] == "closed":
            r = {"key": f"{b}|reopened|{count[b] + 1}", **base, "status": "reopened", "detected_at": now.isoformat()}
            new_rows.append(r); reopened.append(r)
    for b, stt in state.items():
        if stt == "open" and b not in current and not failed:          # 检查失败时不判「已补齐」
            r = {"key": f"{b}|resolved|{count[b] + 1}", "session": day, "base_key": b, "resolves": b, "status": "resolved",
                 "resolved_detected_at": now.isoformat(), "protocol_version": PROTOCOL}
            new_rows.append(r); resolved.append(r)
    _append_new(f, new_rows)
    still_open = [b for b in current]                                 # 本次检查时仍缺（含新开、重开、原本就开）
    return {"day": day, "holiday": False, "gaps": opened + reopened, "resolved": resolved, "reopened": reopened,
            "open_keys": still_open, "check_failed": [g["reason"] for g in failed]}


def _read_gaps(f: Path) -> list:
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()]


def _registry() -> list:
    """方向台账的期望品种来自【注册表】，不从已有文件枚举（整个目录丢失也必须报缺，Codex 034 §3.3）。"""
    from undertow.analyze import skew_reading as skr
    from undertow.dirledger_cli import CONVICTION_INSTRUMENTS
    return [("dirledger:conviction-h1-v1-20260928", "data/history/direction_ledger/conviction-h1-v1-20260928/prospective",
             tuple(CONVICTION_INSTRUMENTS)),
            (f"dirledger:{skr.RULE['version']}", f"data/history/direction_ledger/{skr.RULE['version']}/prospective",
             tuple(skr.RULE["instruments"]))]


def _ledger_gaps(day: str, root: Path, registry: list | None = None) -> list:
    """影子账 v5 与方向台账按各自注册的品种集核对当日行：缺行、截止后才生成、身份有问题分别记。"""
    out = []
    cutoff = _cutoff_dt(day)
    try:
        from undertow.analyze import shadow as sh
        for k in sh.CONFIG["instruments"]:
            f = root / "data/history/shadow" / sh.CONFIG["version"] / f"{k}.jsonl"
            rows = [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()] if f.exists() else []
            row = next((r for r in rows if r.get("session") == day), None)
            ds_ = f"shadow:{sh.CONFIG['version']}"
            if row is None:
                out.append({"dataset": ds_, "instrument": k, "missing_stage": "capture",
                            "reason": "no_row（截止前未生成盘前冻结行；不补造候选、收益不计 0）"})
            else:
                t = _parse_dt(row.get("recorded_at"))
                if t is None or t >= cutoff:
                    out.append({"dataset": ds_, "instrument": k, "missing_stage": "capture",
                                "reason": "late_row（冻结时刻缺失或晚于截止，不算盘前样本）"})
    except Exception as e:                                  # 研究配置读不到：如实报告，不当作无缺口
        out.append({"dataset": "shadow", "instrument": "*", "missing_stage": "check", "check_failed": True,
                    "reason": f"check_failed：{type(e).__name__}"})
    try:
        reg = registry if registry is not None else _registry()
    except Exception as e:
        return out + [{"dataset": "dirledger", "instrument": "*", "missing_stage": "check", "check_failed": True,
                       "reason": f"check_failed：{type(e).__name__}"}]
    for ds_, rel, insts in reg:
        for k in insts:
            f = root / rel / f"{k}.jsonl"
            rows = [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()] if f.exists() else []
            row = next((r for r in rows if r.get("session") == day), None)
            if row is None:
                out.append({"dataset": ds_, "instrument": k, "missing_stage": "record",
                            "reason": "no_row" + ("（台账文件不存在）" if not f.exists() else "")})
            elif row.get("before_open") is not True:
                out.append({"dataset": ds_, "instrument": k, "missing_stage": "record",
                            "reason": "late_row（before_open 不为 true：截止后才记录或未知）"})
            elif row.get("identity_ok") is False:
                out.append({"dataset": ds_, "instrument": k, "missing_stage": "record",
                            "reason": "identity_problem：" + ",".join(row.get("identity_problems") or [])})
            elif row.get("identity_ok") is None:          # 该 schema 有此字段却没给值：未知，不当作通过
                out.append({"dataset": ds_, "instrument": k, "missing_stage": "record", "reason": "identity_unknown"})
    return out


def record_stage_registry(stage: str) -> tuple[Path, tuple]:
    """记录类阶段 → (台账目录, 注册品种)。完成与否看当日冻结行是否落下，不看命令退出码：
    冻结文件 dirledger_cli 把「首份已冻结、新输入不同」也报 rc=1，退出码分不清「缺记录」与「记录在、拒绝改写」。"""
    from undertow.core.config import load_config
    if stage == "shadow_capture":
        from undertow.analyze import shadow as sh
        return ROOT / "data/history/shadow" / sh.CONFIG["version"], tuple(sh.CONFIG["instruments"])
    if stage == "dirledger_record":
        from undertow.analyze import skew_reading as skr
        return ROOT / f"data/history/direction_ledger/{skr.RULE['version']}/prospective", tuple(skr.RULE["instruments"])
    if stage == "conviction_record":
        from undertow.dirledger_cli import CONVICTION_INSTRUMENTS
        return ROOT / "data/history/direction_ledger/conviction-h1-v1-20260928/prospective", tuple(CONVICTION_INSTRUMENTS)
    if stage == "expiry_profile":
        return ROOT / "data/history/expiry_profile", tuple(k for k, v in load_config().instruments.items() if v.options)
    raise ValueError(f"不是记录类阶段：{stage}")


RECORD_STATES = ("completed_eligible", "completed_ineligible", "missing", "corrupt", "identity_conflict")


def _row_eligible(stage: str, row: dict, day: str) -> tuple[bool, str]:
    """终态行是否为合格的盘前样本。不重写冻结行；只分类（Codex 036 §4）。"""
    if stage == "shadow_capture":
        idt = row.get("identity") or {}
        t = _parse_dt(row.get("recorded_at"))
        if t is None or t >= _cutoff_dt(day):
            return False, "recorded_after_cutoff_or_unknown"
        if idt.get("status") != "certified":
            return False, f"identity_status={idt.get('status')}"
        return True, ""
    if row.get("before_open") is not True:
        return False, f"before_open={row.get('before_open')}"
    if stage in ("dirledger_record", "conviction_record"):
        io = row.get("identity_ok")
        if io is not True:                       # None = 未知（schema 有此字段却没给值），False = 有问题；都不算合格
            return False, f"identity_ok={io}"
    return True, ""


def record_status(stage: str, day: str, registry: tuple[Path, tuple] | None = None) -> dict:
    """每个注册品种当日的终态：completed_eligible / completed_ineligible / missing / corrupt / identity_conflict。"""
    d, insts = registry or record_stage_registry(stage)
    out = {}
    for k in insts:
        f = d / f"{k}.jsonl"
        if not f.exists():
            out[k] = {"state": "missing", "why": "file_missing"}; continue
        try:
            rows = [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()]
        except ValueError:
            out[k] = {"state": "corrupt", "why": "jsonl_unparseable"}; continue
        mine = [r for r in rows if r.get("session") == day]
        if not mine:
            out[k] = {"state": "missing", "why": "no_row"}
        elif len(mine) > 1 and any(json.dumps(m, sort_keys=True) != json.dumps(mine[0], sort_keys=True) for m in mine[1:]):
            out[k] = {"state": "identity_conflict", "why": f"{len(mine)} 行同一会话且内容不同"}
        else:
            ok, why = _row_eligible(stage, mine[0], day)
            out[k] = {"state": "completed_eligible" if ok else "completed_ineligible", "why": why}
    return out


def record_missing(stage: str, day: str, registry: tuple[Path, tuple] | None = None) -> list:
    return [k for k, v in record_status(stage, day, registry).items() if v["state"] == "missing"]


def _parse_dt(x):
    try:
        t = datetime.fromisoformat(str(x))
        return t if t.tzinfo else None
    except (TypeError, ValueError):
        return None


def _append_new(path: Path, rows: list) -> None:
    seen = {r["key"] for r in _read_gaps(path)}
    new = [r for r in rows if r["key"] not in seen]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())


QUEUE_FILE = ".gapcheck_queue.json"
RECHECK_EVERY_H = 24        # 开放缺口按固定节奏复查（未校准的运营节奏）
MAX_RECHECKS = 10           # 开放缺口最多复查 10 次，之后标 stale（记录仍开放，不再自动复查）
MAX_RECHECK_PER_RUN = 5


def _load_queue(logs: Path) -> dict:
    f = logs / QUEUE_FILE
    try:
        return json.loads(f.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        try:
            f.replace(f.with_name(f.name + f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"))
        except OSError:
            pass
        return {}


def _enqueue(q: dict, day: str, why: str, now: datetime, prev: dict | None = None) -> None:
    from datetime import timedelta
    n = (prev or {}).get("attempts", 0) + 1
    if why == "open_gaps" and n > MAX_RECHECKS:
        q[day] = {**(prev or {}), "why": why, "attempts": n - 1, "stale": True, "stale_at": now.isoformat()}
        return
    nxt = now if why == "check_failed" else now + timedelta(hours=RECHECK_EVERY_H)
    q[day] = {"why": why, "attempts": n, "next_at": nxt.isoformat(), "first_queued_at": (prev or {}).get("first_queued_at", now.isoformat())}


def gapcheck_due(now: datetime | None = None, logs: Path = LOGS, *, check=None) -> int:
    """首次检查（水位，最多 5 个交易日 / 次）+ 待重查队列（检查失败下次唤醒重试；开放缺口每 24 小时复查，记 resolved/reopened）。
    rc：0 正常 / 2 新发现或重开缺口 / 4 日历未覆盖 / 1 有检查失败（已入队重试）。无事可做 → 不输出。（Codex 036 §3）"""
    now = now or datetime.now(timezone.utc)
    check = check or (lambda d: gapcheck(d, logs=logs))
    q = _load_queue(logs)
    rc = 0

    def run_one(day: str, prev: dict | None) -> None:
        nonlocal rc
        try:
            r = check(day)
        except Exception as e:
            print(f"{day} 缺口检查失败：{type(e).__name__}: {e}"); _enqueue(q, day, "check_failed", now, prev); rc = rc or 1
            return
        for g in r.get("gaps", []):
            print(f"缺口 {day} {g['dataset']} {g['instrument']}：{g['reason']}" + ("（重开）" if g.get("status") == "reopened" else ""))
        for g in r.get("resolved", []):
            print(f"已补齐 {day} {g['base_key']}")
        if r.get("check_failed"):
            print(f"{day} 部分检查失败：{'；'.join(r['check_failed'])}（入队重试）")
            _enqueue(q, day, "check_failed", now, prev); rc = 1
        elif r.get("open_keys"):
            _enqueue(q, day, "open_gaps", now, prev)
        else:
            q.pop(day, None)
        print(f"{day} 新缺口 {len(r.get('gaps', []))} 项，仍开放 {len(r.get('open_keys', []))} 项，本次补齐 {len(r.get('resolved', []))} 项")
        if r.get("gaps") and rc == 0:
            rc = 2

    days, unknown = due_sessions(now, logs)
    for day, trading in days:
        if trading:
            run_one(day, None)
        set_watermark(day, logs)                     # 首次检查完成即推进；失败与开放缺口由队列负责重查，不阻塞新日期
    n = 0
    for day in sorted(q):
        e = q[day]
        if n >= MAX_RECHECK_PER_RUN or e.get("stale") or day in {d for d, _ in days}:
            continue
        if _parse_dt(e.get("next_at")) and _parse_dt(e["next_at"]) <= now:
            run_one(day, e); n += 1
    _atomic(logs / QUEUE_FILE, json.dumps(q, ensure_ascii=False, indent=1))
    if unknown:
        print(f"{unknown} 日历未覆盖：无法判定是否交易日，首次检查停在此日之前（需更新 market_calendar）")
        return 4 if rc == 0 else rc
    return rc


def gapcheck_pending(now: datetime, logs: Path = LOGS) -> bool:
    days, unknown = due_sessions(now, logs)
    if days or unknown:
        return True
    return any(not e.get("stale") and _parse_dt(e.get("next_at")) and _parse_dt(e["next_at"]) <= now
               for e in _load_queue(logs).values())


def main() -> int:
    cmd = sys.argv[1]
    day = sys.argv[2] if len(sys.argv) > 2 else ""
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
        miss = missing_required(day, st["identity"])
        if miss:                                            # 发布成功不能替代分析成功（Codex 034 §3.1）
            print("必需阶段未完成，不标记完成：" + " ".join(miss)); return 1
        opt = optional_incomplete(day, st["identity"])
        _atomic(_done_path(day), json.dumps({"schema": DONE_SCHEMA, "identity": st["identity"],
                                             "at": datetime.now(timezone.utc).isoformat(),
                                             "stages": load_stages(day, st["identity"]),
                                             # 可选阶段未完成 = 明确的终态（当日封账后不再重试），由 shell 告警一次
                                             "optional_terminal_incomplete": opt}, ensure_ascii=False))
        if opt:
            print("OPTIONAL_INCOMPLETE " + " ".join(opt))
        return 0
    if cmd == "record-complete":     # rc 0 全部合格终态行 / 4 全部有终态行但有不合格 / 1 有缺失、损坏或冲突（逐品种 JSON 输出）
        st = record_status(sys.argv[3], day)
        print(json.dumps(st, ensure_ascii=False))
        states = {v["state"] for v in st.values()}
        if states & {"missing", "corrupt", "identity_conflict"}:
            return 1
        return 4 if "completed_ineligible" in states else 0
    if cmd in ("stage-set", "stage-ok"):
        ident = snapshot_state(day, expected_symbols())["identity"]
        if cmd == "stage-ok":
            return 0 if ident and stage_ok(day, ident, sys.argv[3]) else 1
        if not ident:
            return 3                                          # 快照未齐：没有身份，本轮不记阶段（shell 静默）
        set_stage(day, ident, sys.argv[3], sys.argv[4], int(sys.argv[5]))
        return 0
    if cmd == "trading-day":                              # rc 0 交易日或日历未覆盖（未知按交易日处理，宁可多跑）/ 3 确定非交易日
        return 3 if is_non_trading(day) else 0
    if cmd == "lastrun":
        _atomic(LOGS / f".daily_lastrun_{day}", datetime.now(ET).strftime("%H:%M"))
        return 0
    if cmd == "gapcheck-pending":                         # rc 0 = 有待首次检查的交易日、到期的重查，或日历未知需报告
        return 0 if gapcheck_pending(datetime.now(timezone.utc)) else 1
    if cmd == "gapcheck-due":
        return gapcheck_due()
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
