"""R 日初基准观察的私有存储（Codex 065 WP2）：data/account/r_budget/anchors.jsonl（gitignore）。

沿用 jsonl_ledger：读改写全程 flock、坏文件隔离不覆盖、原子写 + 回读。基准观察不可变：
同一交易日的日终观察只保留【首份】（再次采集返回 exists，不覆盖、不报冲突）；bootstrap 按采集时刻各成一行。
"""
from __future__ import annotations

from pathlib import Path

from undertow.collect import jsonl_ledger as jl

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "data" / "account" / "r_budget" / "anchors.jsonl"
KEY = "id"


def load(path: Path | None = None) -> list[dict]:
    """损坏 → jl.LedgerCorruptError（原件不动、另存隔离副本）；调用方据此报 incomplete，不当空表。"""
    return jl.load(path or PATH, KEY)


def record(row: dict, path: Path | None = None) -> str:
    """→ "inserted" / "exists"。"""
    p = path or PATH
    with jl.locked(p):
        rows = jl.load_path(p, KEY)
        if any(r[KEY] == row[KEY] for r in rows):
            return "exists"
        rows.append(row)
        rows.sort(key=lambda r: r["captured_at"])
        jl.atomic_write(p, rows, KEY)
        return "inserted"
