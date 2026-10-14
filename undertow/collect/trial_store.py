"""实盘试行状态的持久化（Codex 043 WP3）：私有 data/account/live_trial/state.json（gitignore）。

原子写 + 回读校验（asof_history.atomic_write_json）；读到损坏/结构不符 → 隔离原文件（保留字节）并返回问题，
调用方必须当场告知、冻结新增，绝不当作「没有暂停」或用新状态覆盖。暂停 latch 存在这里，重启/刷新不会丢。
"""
from __future__ import annotations

import json
from pathlib import Path

from undertow.analyze import trial_capital as tc
from undertow.collect.asof_history import atomic_write_json, locked
from undertow.collect.cas import quarantine

STATE = Path("data/account/live_trial/state.json")


def load(path: Path = STATE) -> tuple[dict | None, str | None]:
    """→ (state, problem)。文件不存在 → (None, None)：试行未建立。损坏 → (None, 问题说明)，原文件已隔离。"""
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as e:
        q = quarantine(path, raw)
        return None, f"试行状态文件无法解析，已隔离到 {q}：{e}"
    bad = tc.validate(obj)
    if bad:
        q = quarantine(path, raw)
        return None, f"试行状态文件结构不符（{bad}），已隔离到 {q}"
    return obj, None


def save(state: dict, path: Path = STATE) -> None:
    bad = tc.validate(state)
    if bad:
        raise ValueError(f"拒绝写入不合格的试行状态：{bad}")
    with locked(path):
        atomic_write_json(path, state)
    back, prob = load(path)
    if prob or back != json.loads(json.dumps(state, default=str)):
        raise ValueError(f"试行状态写入后回读不一致：{prob}")
