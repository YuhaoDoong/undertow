"""研报输入存档（用户 2026-09-28：「数据永远是最主要的，研报什么的都可以再改」）。

期权链快照早已按日入库；但研报还用到的日线价格（CBOE/Yahoo）、波动率指数、FRED 宏观、COT 原始下载
只存在 data/cache/（gitignore、每次被最新数据覆盖、无版本）。这意味着「某天研报当时看到的是什么」
事后无法还原：FRED 会修订历史，数据源也可能改版或下线。

存法（控制仓库体积）：
- 每日：data/history/inputs/daily/<ET日>.json.gz —— 每个输入序列的【最近 TAIL 行】+ 全文 sha256 + 行数 + 抓取时刻。
  同一天多次运行：只有内容（sha）变了才追加一个版本，不重复存。
- 每月：data/history/inputs/monthly/<YYYY-MM>/<名>.json.gz —— 当月第一次运行时存完整原文（保证能完整重算）。
- 不存 cboe_*（期权链，已在 data/snapshots/）。认不出的格式整份存进每日文件并标 kind=unknown_full，不静默跳过。
原子写 + 回读校验；已有文件损坏 → 隔离保留，不覆盖。只读 data/cache，不联网。

⚠️ Codex 017 A01/A02（2026-09-28）：尾部 + 月初全量**不能**还原月内对早期行的修订；事后扫描缓存也**不能**证明
研报用了哪一版。所以现在：
- 每个缓存文件的完整原文另存进 collect/cas（分块、按内容去重、可逐字节还原），daily 索引记 cas_sha256；
  尾部只作便于浏览的索引，不再是唯一真值。
- 研报「实际消费了哪一版」由 collect/provenance 在取数边界记录（manifests/），本扫描只是补充的「存档时刻缓存状态」。
- 已存在的月度文件也要能解开，否则隔离并报告（以前只在不存在时写，坏了也当齐全）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from undertow.collect import cas

CACHE_DIR = Path("data/cache")
OUT_DIR = Path("data/history/inputs")
TAIL = 40
SCHEMA = 1
SKIP_PREFIX = ("cboe_",)            # 期权链：已由快照存档


class ArchiveCorrupt(RuntimeError):
    pass


def _tail(name: str, obj):
    """按格式取尾部。返回 (kind, n_rows, tail)。认不出 → ("unknown_full", None, 原对象)。"""
    d = obj.get("data") if isinstance(obj, dict) else None
    if name.startswith("cboehist_") and isinstance(d, dict) and isinstance(d.get("data"), list):
        rows = d["data"]
        return "rows", len(rows), rows[-TAIL:]
    if name.startswith(("cboevol_", "fred_")) and isinstance(d, str):
        lines = d.splitlines()
        return "csv", max(0, len(lines) - 1), lines[:1] + lines[1:][-TAIL:]
    if name.startswith("cot_") and isinstance(d, list):
        rows = sorted(d, key=lambda r: str(r.get("report_date_as_yyyy_mm_dd", "")))
        return "rows", len(rows), rows[-TAIL:]
    if name.startswith("yahoo_") and isinstance(d, dict):
        try:
            res = d["chart"]["result"][0]
            ts = res.get("timestamp") or []
            ind = res.get("indicators") or {}
            cut = lambda arr: arr[-TAIL:] if isinstance(arr, list) else arr
            tail = {"meta": res.get("meta"), "timestamp": cut(ts),
                    "indicators": {k: [{kk: cut(vv) for kk, vv in blk.items()} for blk in v]
                                   for k, v in ind.items() if isinstance(v, list)}}
            return "yahoo", len(ts), tail
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
    if name.startswith("ffcal_"):
        return "calendar_full", None, obj          # 本周经济日历，体积小，整份存
    return "unknown_full", None, obj


def _read_gz(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, EOFError, ValueError, UnicodeError) as e:
        raise ArchiveCorrupt(f"{path} 无法解析：{e}") from e


def _write_gz(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        if json.loads(gzip.decompress(Path(name).read_bytes())) != obj:
            raise ValueError(f"{path} 回读校验失败；原文件未改")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _quarantine(path: Path) -> Path:
    q = path.with_name(path.name + f".corrupt-{int(time.time())}")
    path.rename(q)
    return q


def archive(et_day: str, *, cache_dir: Path = CACHE_DIR, out_dir: Path = OUT_DIR, cas_root: Path | None = None) -> dict:
    """存一次。返回统计与问题清单（issues 非空 → 调用方告警）。"""
    cas_root = cas_root or (out_dir / "cas")          # 默认 = data/history/inputs/cas（与 cas.ROOT 相同）
    now = datetime.now(timezone.utc).isoformat()
    stats = {"files": 0, "new_versions": 0, "unchanged": 0, "monthly_new": 0, "unknown_full": 0, "skipped_options": 0,
             "cas_new_chunks": 0, "cas_new_recipes": 0}
    issues: list[str] = []
    daily_path = out_dir / "daily" / f"{et_day}.json.gz"
    try:
        daily = _read_gz(daily_path)
    except ArchiveCorrupt as e:
        q = _quarantine(daily_path)
        issues.append(f"{e}；已隔离为 {q.name}，今日重新开始（旧文件保留）")
        daily = None
    daily = daily or {"schema": SCHEMA, "et_day": et_day, "tail_rows": TAIL, "files": {}}
    month = et_day[:7]
    if not cache_dir.exists():
        return {"stats": stats, "issues": [f"缓存目录不存在：{cache_dir}"]}
    for p in sorted(cache_dir.glob("*.json")):
        name = p.stem
        if name.startswith(SKIP_PREFIX):
            stats["skipped_options"] += 1
            continue
        stats["files"] += 1
        try:
            raw = p.read_bytes()
            obj = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as e:
            issues.append(f"{p.name} 读取失败：{type(e).__name__}")
            continue
        sha = hashlib.sha256(raw).hexdigest()
        try:
            put = cas.put(raw, root=cas_root)
            stats["cas_new_chunks"] += put["new_chunks"]; stats["cas_new_recipes"] += put["new_recipe"]
        except Exception as e:
            issues.append(f"{p.name} 完整原文存入 cas 失败：{type(e).__name__}: {e}")
        versions = daily["files"].setdefault(name, [])
        if versions and versions[-1]["sha256"] == sha:
            stats["unchanged"] += 1
        else:
            kind, n, tail = _tail(name, obj)
            stats["unknown_full"] += kind == "unknown_full"
            versions.append({"archived_at": now, "fetched_at": obj.get("fetched_at") if isinstance(obj, dict) else None,
                             "sha256": sha, "cas_sha256": sha, "kind": kind, "n_rows": n, "tail": tail})
            stats["new_versions"] += 1
        mpath = out_dir / "monthly" / month / f"{name}.json.gz"
        if mpath.exists():
            try:
                _read_gz(mpath)
            except ArchiveCorrupt as e:
                q = _quarantine(mpath)
                issues.append(f"{e}；已隔离为 {q.name}（旧文件保留），本月全量用当前缓存重建 —— 不是原月初版本")
        if not mpath.exists():
            _write_gz(mpath, {"schema": SCHEMA, "month": month, "archived_at": now, "sha256": sha, "raw": obj})
            stats["monthly_new"] += 1
    _write_gz(daily_path, daily)
    return {"stats": stats, "issues": issues, "daily": str(daily_path)}
