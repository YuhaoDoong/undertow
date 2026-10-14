"""实盘试行状态的持久化（Codex 043 WP3；Codex 044 包 B 改为事务）：私有 data/account/live_trial/state.json（gitignore）。

Codex 044 P1-2：旧版 save 只锁写入，调用方在锁外 load → evaluate → save。两个写者读到同一旧版本时，
A 写入 60% 亏损暂停后，B 拿旧快照（权益 500）照样写入，磁盘 pause_latched true→false，没有任何 resume。
原子改名只防半截文件，不防旧版本覆盖新版本。

现在只有一个写入口 update(fn)：在同一把锁里 读 → 深校验 → fn(最新状态) → 不变量检查 → 原子写 → 回读。
- 计算在锁内对【最新】状态做，旧快照无从写回；rev 每次写入 +1。
- 不变量：普通更新不得把 pause_latched 从 true 改成 false；只有 resume 事件（resume_log 恰好多一条）才可以。
- 读到损坏/结构不符 → 隔离原文件（保留字节）并抛 TrialStateError，调用方必须当场告知、冻结新增；绝不用新状态覆盖。
- 网络读数（权益、成交）在锁外取，传进 fn；fn 内按最新状态计算，旧观察不会覆盖新观察，也不作新的暂停决策；现金流按观察时点截面计（trial_capital.evaluate，Codex 045）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from undertow.analyze import trial_capital as tc
from undertow.collect.asof_history import atomic_write_json, locked
from undertow.collect.cas import quarantine

STATE = Path("data/account/live_trial/state.json")


class TrialStateError(RuntimeError):
    """状态文件损坏/不合格（已隔离）或写入违反不变量。调用方当场告知、冻结新增。"""


def _load_unlocked(path: Path) -> tuple[dict | None, str | None]:
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


def load(path: Path = STATE) -> tuple[dict | None, str | None]:
    """只读快照 → (state, problem)。不存在 → (None, None)：试行未建立。损坏 → (None, 问题说明)，原文件已隔离。
    快照只用于显示；任何写入都必须走 update，不能把这里读到的状态改完再写回。"""
    with locked(path):
        return _load_unlocked(path)


def update(fn: Callable[[dict | None], dict | None], path: Path = STATE, *, create: bool = False) -> dict | None:
    """锁内 读 → 校验 → new = fn(当前状态) → 不变量 → 原子写 → 回读，返回写入后的状态。
    fn 返回 None 或与当前相同 → 不写。create=False 时状态不存在 → 抛错；create=True 时状态已存在 → 抛错（init 不覆盖）。"""
    with locked(path):
        cur, prob = _load_unlocked(path)
        if prob:
            raise TrialStateError(prob)
        if cur is None and not create:
            raise TrialStateError("试行未建立（undertow trial init）")
        if cur is not None and create:
            raise TrialStateError(f"试行 {cur['trial_id']} 已存在，不覆盖")
        new = fn(cur)
        if new is None or new == cur:
            return cur
        bad = tc.validate({**new, "rev": (cur or {"rev": -1})["rev"] + 1})
        if bad:
            raise TrialStateError(f"拒绝写入不合格的试行状态：{bad}")
        if cur is not None and cur["pause_latched"] and not new["pause_latched"] \
                and len(new["resume_log"]) != len(cur["resume_log"]) + 1:
            raise TrialStateError("拒绝写入：暂停只能由用户明确的 resume 解除，普通更新不得清掉 latch")
        if cur is not None and len(new["resume_log"]) < len(cur["resume_log"]):
            raise TrialStateError("拒绝写入：解除记录不能减少")
        new = {**new, "rev": (cur or {"rev": -1})["rev"] + 1}
        atomic_write_json(path, new)
        back, prob = _load_unlocked(path)
        if prob or back != json.loads(json.dumps(new, default=str)):
            raise TrialStateError(f"试行状态写入后回读不一致：{prob}")
        return back
