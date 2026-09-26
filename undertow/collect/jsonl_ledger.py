"""通用 JSONL 前瞻台账存储：一行一个 (品种, 决策日)，事前字段冻结，事后字段可追加。

从 analyze/spread_ledger.py 的完整性补丁（Codex A02）抽象而来，供 W05 影子账使用：
  · 读改写全程 flock（锁文件独立于被原子替换的数据 inode）
  · 严格解析：坏行、重复字段、非有限数值即报错；原件不动 + 另存隔离副本 → 绝不当空表继续写
  · 同目录临时文件 + fsync + 回读校验 → os.replace
  · 事前字段冻结：同 key 再写时比较 frozen 部分，不同则 LedgerConflictError，不覆盖
  · 事后更新（报价、监控、结算）只能改 mutable 字段；更新函数若改了 frozen 部分，整次写入拒绝

⚠️ spread_ledger 仍保留它自己的一份实现（已提交、已测试），与本模块语义一致但尚未合并 ——
   这是已知重复，合并留作后续工作（见 GPTcom claude_logs）。
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


class LedgerCorruptError(ValueError):
    pass


class LedgerConflictError(ValueError):
    pass


def _reject_constant(v):
    raise ValueError(f"非有限 JSON 数值 {v}")


def _unique(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError(f"重复 JSON 字段 {k}")
        obj[k] = v
    return obj


def _check_finite(v):
    if isinstance(v, dict):
        for x in v.values():
            _check_finite(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _check_finite(x)
    elif isinstance(v, float) and not math.isfinite(v):
        raise ValueError("含非有限数值")


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def _decode(raw: bytes, key_field: str) -> list[dict]:
    rows = [json.loads(line, parse_constant=_reject_constant, object_pairs_hook=_unique)
            for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("已有台账为空，可能被截断；不能当作未建账")
    seen = set()
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict) or not isinstance(r.get(key_field), str):
            raise ValueError(f"第 {i} 行缺少 {key_field}")
        if r[key_field] in seen:
            raise ValueError(f"第 {i} 行 {key_field} 重复：{r[key_field]}")
        seen.add(r[key_field])
    _check_finite(rows)
    return rows


def load_path(path: Path, key_field: str) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    try:
        return _decode(raw, key_field)
    except (ValueError, UnicodeError) as exc:
        fd, backup = tempfile.mkstemp(prefix=path.name + ".corrupt-", dir=path.parent)
        with os.fdopen(fd, "wb") as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        raise LedgerCorruptError(f"{path} 损坏：{exc}；原文件未改，隔离副本 {backup}") from exc


def atomic_write(path: Path, rows: list[dict], key_field: str) -> None:
    raw = ("\n".join(json.dumps(r, ensure_ascii=False, allow_nan=False) for r in rows) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        if tmp.read_bytes() != raw or _decode(raw, key_field) != rows:
            raise ValueError(f"{path} 临时文件回读验证失败；原文件未改")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load(path: Path, key_field: str) -> list[dict]:
    with locked(path):
        return load_path(path, key_field)


def insert_frozen(path: Path, row: dict, *, key_field: str,
                  frozen: Callable[[dict], dict]) -> str:
    """插入新行；同 key 已存在时：frozen 部分相同 → 幂等返回 "exists"；不同 → 冲突。"""
    _check_finite(row)
    with locked(path):
        rows = load_path(path, key_field)
        for old in rows:
            if old[key_field] == row[key_field]:
                if frozen(old) != frozen(row):
                    raise LedgerConflictError(f"{row[key_field]} 首份事前记录已冻结，新输入不同；未改写")
                return "exists"
        rows.append(row)
        rows.sort(key=lambda r: r[key_field])
        atomic_write(path, rows, key_field)
        return "inserted"


def update(path: Path, fn: Callable[[dict], bool], *, key_field: str,
           frozen: Callable[[dict], dict]) -> int:
    """对每行调用 fn（原地修改事后字段，返回是否改动）。改到 frozen 部分即拒绝整次写入。"""
    with locked(path):
        rows = load_path(path, key_field)
        before = [copy.deepcopy(frozen(r)) for r in rows]
        n = sum(1 for r in rows if fn(r))
        for b, r in zip(before, rows):
            if frozen(r) != b:
                raise LedgerConflictError(f"{r[key_field]} 的事前字段在更新中被改动；整次写入拒绝")
        if n:
            _check_finite(rows)
            atomic_write(path, rows, key_field)
        return n
