"""桌面通知去重：同一美东日、同一内容只弹一次（用户 2026-11-06：「电脑上经常弹出通知」）。

起因：daily 每 15 分钟唤醒一次、未完成就重跑，研报/台账阶段每轮都把同一条告警再弹一遍（实测一小时十几条，
「⚡极强看跌」「前瞻台账未写入」「影子账采集失败」反复出现）。AGENTS 第四节的规矩不变：
- 失败照常【每次】追加兜底文件（data/reports/FAILURE_*.txt），去重只作用于弹窗；
- 内容变了（新的失败原因、新的品种、新的信号）照常弹；第二天照常再弹一次；
- 去重状态读写出错 → 照弹（宁可重复，不能漏）。

去重键 = 标题 + 正文，去掉形如「(ET 2026-11-06 01:28)」「ET 01:28」的运行时刻（标题里带时刻，每轮都不同）。
状态放 data/cache/notify_once/（已 gitignore，不进发布流程）。

用法（shell）：python3 -m undertow.notify_once <标题> <正文> [声音] [副标题]
  退出码 0 = 已弹或今天弹过（都正常）；非 0 = 本模块自身出错 → 调用方应直接用 osascript 兜底弹一次。
测试钩子：NOTIFY_ONCE_DIR（状态目录）、NOTIFY_ONCE_OSASCRIPT（替换 osascript，可指向记录脚本）。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_TIME = re.compile(r"[（(]?\s*ET\s*\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s*[)）]?|ET\s*\d{1,2}:\d{2}")


def key_of(title: str, body: str) -> str:
    norm = lambda s: re.sub(r"\s+", " ", _TIME.sub("", s or "")).strip()
    return hashlib.sha256(f"{norm(title)}\n{norm(body)}".encode()).hexdigest()


def _state_dir() -> Path:
    return Path(os.environ.get("NOTIFY_ONCE_DIR") or Path(__file__).resolve().parent.parent / "data" / "cache" / "notify_once")


def claim(title: str, body: str, *, et_date: str | None = None) -> bool:
    """今天第一次见到这条内容 → 登记并返回 True（该弹）；今天已弹过 → False。读写出错抛出（调用方照弹）。"""
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    day = et_date or datetime.now(_ET).date().isoformat()
    k = key_of(title, body)
    with open(d / f"{day}.txt", "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        if k in {ln.strip() for ln in f}:
            return False
        f.write(k + "\n")
        f.flush()
        return True


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def show(title: str, body: str, *, sound: str = "Glass", subtitle: str = "") -> None:
    script = f'display notification "{_esc(body)}" with title "{_esc(title)}"'
    if subtitle:
        script += f' subtitle "{_esc(subtitle)}"'
    script += f' sound name "{_esc(sound)}"'
    exe = os.environ.get("NOTIFY_ONCE_OSASCRIPT") or "/usr/bin/osascript"
    subprocess.run([exe, "-e", script], check=False, capture_output=True, timeout=10)


def push(title: str, body: str, *, sound: str = "Glass", subtitle: str = "") -> bool:
    """去重后弹窗。→ 是否弹了。去重状态出错 → 照弹（不漏）。弹窗本身失败不抛（与原 osascript 调用同）。"""
    try:
        first = claim(title, body)
    except Exception:                                  # noqa: BLE001 —— 去重坏了也要弹
        first = True
    if first:
        try:
            show(title, body, sound=sound, subtitle=subtitle)
        except Exception:                              # noqa: BLE001
            pass
    return first


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python3 -m undertow.notify_once <标题> <正文> [声音] [副标题]", file=sys.stderr)
        return 2
    title, body = argv[0], argv[1]
    first = claim(title, body)                         # 出错 → 异常 → 非 0 退出，调用方兜底直接弹
    if first:
        show(title, body, sound=argv[2] if len(argv) > 2 else "Glass", subtitle=argv[3] if len(argv) > 3 else "")
    else:
        print(f"[通知去重] 今天已弹过：{title}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
