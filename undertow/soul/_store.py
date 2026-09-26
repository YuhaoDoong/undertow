"""私有档案（profile / journal / plan）的严格存储（Codex 008 G02）。

原错：三个档案的 load 在 JSON 损坏或读失败时都返回「空」，save 用普通 write_text。
journal --capture 读到空 → 加入当天 → 保存，就把整本损坏的历史日记覆盖成只剩一天；
save_journal 默认从同一文件读 theses，坏档时 theses 也被写成空。profile 坏档返回 None，
纪律检查随之返回 []，咨询上下文直接省略纪律层 —— 看起来像「没有违规」。

规则：
- 不存在 → None（正常：尚未建档）。
- 读不了（权限等）/ 解析失败 / 顶层类型不对 / 字段结构不符 → PrivateStoreError，调用方必须让用户看见。
- 损坏文件原字节保留在原处，并在同目录另存一份带时间戳的隔离副本（同目录 = 同样 gitignore，
  私人内容不会因隔离而进入公开仓库）。原件不动，所以之后任何保存都会再次撞上错误，而不是覆盖它。
- 写入：同目录临时文件 + fsync + 回读 + 原子替换；读改写整段用 locked()。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from undertow.collect.asof_history import atomic_write_json, locked  # noqa: F401  (re-export)


class PrivateStoreError(RuntimeError):
    """私有档案存在但不可用（损坏/不可读/结构不符）。绝不当作空档继续写。"""


def quarantine(path: Path) -> Path:
    q = path.with_name(f"{path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(path, q)
    return q


def read_json(path: Path, expect: type = dict):
    """不存在 → None；其它一切异常 → PrivateStoreError（损坏时先隔离副本）。"""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise PrivateStoreError(f"{path} 无法读取（{type(e).__name__}: {e}）；未作任何修改") from e
    try:
        obj = json.loads(text)
    except ValueError as e:
        q = quarantine(path)
        raise PrivateStoreError(f"{path} 已损坏（{e}）；原件保留，另存隔离副本 {q.name}；"
                                f"拒绝当作空档继续写入") from e
    if not isinstance(obj, expect):
        q = quarantine(path)
        raise PrivateStoreError(f"{path} 顶层类型为 {type(obj).__name__}，期望 {expect.__name__}；"
                                f"原件保留，隔离副本 {q.name}")
    return obj


def build(path: Path, fn):
    """把「原始 dict → 数据类」的构造包起来：字段结构不符（多余/缺失键、类型错）→ PrivateStoreError。"""
    try:
        return fn()
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise PrivateStoreError(f"{path} 结构不符（{type(e).__name__}: {e}）；原件未改") from e
