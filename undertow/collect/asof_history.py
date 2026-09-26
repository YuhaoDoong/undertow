"""日度历史的「首份发布冻结」（Codex 005 S04 / Claude 新发现 3）。

原错：outlook_scores / resonance / ratio_watch / signal_ledger 都是「同一天重跑就覆盖」。
2026-09-26 一次手动 `report silver` 把 9/25 盘前记录（SLV 64.02）覆盖成盘后值（64.71），
ratio_watch 还因只跑白银而把黄金侧写成 null —— 不可再生的「当时发布了什么」被静默改写。

规则（as-published）：
  · 某 key（如 品种+日期）第一次写入的内容 = 发布版本，永久不变；
  · 之后同 key 的新内容若不同，**不覆盖**，追加到旁边的 `<文件>.revisions.jsonl`
    （含首发时刻、修订时刻、内容 sha256、完整新内容）；
  · 相同内容幂等，不产生修订；
  · 调用方可声明「易变字段」（如只作展示的实时价），它们的差异不算修订；
  · 显式重建（如 signals --rebuild）由调用方先删旧行再写，不经过本冻结。
写入一律同目录临时文件 + fsync + os.replace；读坏了抛错，不当空表重写。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def atomic_write_json(path: Path, obj, *, indent=1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, indent=indent, default=str).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        if Path(name).read_bytes() != raw:
            raise ValueError(f"{path} 回读校验失败；原文件未改")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(path: Path):
    """对 `<path>.lock` 加排他 flock，包住整段「读 → 合并 → 写主文件 → 追加修订」。

    Codex 006 C05：原子替换只保证单次写入不出半截文件，不保证两个进程（定时研报 + 手动研报）
    各自读到「还没有这个 key」、各自写入，后写者把先写的首发内容整体换掉且不留修订。
    锁放在存储层（本模块），调用方不必各自实现。
    """
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    lp = path.with_name(path.name + ".lock")
    with open(lp, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_revisions(path: Path, revisions: list[dict]) -> None:
    if not revisions:
        return
    rp = path.with_name(path.name + ".revisions.jsonl")
    # 调用方应已持有 locked(path)；这里整批一次 write，避免多行交错
    blob = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in revisions)
    with rp.open("a", encoding="utf-8") as f:
        f.write(blob); f.flush(); os.fsync(f.fileno())


def freeze_merge(old_rows: list[dict], new_rows: list[dict], key, *, volatile=()) -> tuple[list[dict], list[dict]]:
    """纯函数：返回 (合并后的行, 修订记录)。key(row) → 可哈希键。"""
    def core(r):
        return {k: v for k, v in r.items() if k not in volatile and k != "published_at"}
    by = {key(r): r for r in old_rows}
    revs = []
    for r in new_rows:
        k = key(r)
        if k not in by:
            by[k] = {**r, "published_at": r.get("published_at") or _now()}
        elif core(by[k]) != core(r):
            revs.append({"key": list(k) if isinstance(k, tuple) else k,
                         "first_published_at": by[k].get("published_at"), "revised_at": _now(),
                         "sha256_published": _sha(core(by[k])), "sha256_revision": _sha(core(r)),
                         "revision": r})
    merged = list(by.values())
    return merged, revs


def load_json(path: Path, default):
    """读 JSON；不存在 → default。解析失败或顶层类型与 default 不同（如期望 list 却读到 dict）→ 抛错，
    不当空表覆盖（C05：合法 JSON 不等于结构正确）。"""
    if not path.exists():
        return default
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as e:
        raise ValueError(f"{path} 无法解析（{e}）；原文件未改，拒绝当作空表覆盖") from e
    if not isinstance(obj, type(default)):
        raise ValueError(f"{path} 顶层类型为 {type(obj).__name__}，期望 {type(default).__name__}；原文件未改")
    return obj
