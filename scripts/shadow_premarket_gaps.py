"""影子账盘前缺口（Codex 032 O3）：今天应有的机会行哪些还没有、哪些仍是 provisional。

  python3 scripts/shadow_premarket_gaps.py        # 输出两行：missing <品种…> / provisional <品种…>；rc 0

session 在 ET 09:00–09:25 每次唤醒据此有界重试：missing → `shadow capture <品种>`（只补缺的，已冻结的行不重跑、
不触发「首份已冻结」冲突）；provisional → `shadow settle <品种>`（认证只由预存交易日历判断，取不到就保持 provisional，
不伪造 certified）。09:30 之后补的行不是前瞻样本，故重试只在 09:25 前。

三个时刻各自保留在行里、互不改写：identity.captured_at（快照首次可得）、recorded_at（候选首次冻结）、
identity.certified_at（认证完成）。只读，不写任何文件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def gaps(today: str, rows_by_inst: dict) -> tuple[list, list]:
    """纯函数：rows_by_inst = {品种: 该品种账本里所有行}；返回 (缺今天行的, 今天行仍未认证的)。"""
    missing, prov = [], []
    for k, rows in rows_by_inst.items():
        r = next((x for x in reversed(rows) if x.get("session") == today), None)
        if r is None:
            missing.append(k)
        elif (r.get("identity") or {}).get("status") == "provisional":
            prov.append(k)
    return missing, prov


def main() -> int:
    from undertow.analyze import shadow as sh
    from undertow.core.clock import market_today
    from undertow.shadow_cli import _path
    today = market_today().isoformat()
    by = {}
    for k in sh.CONFIG["instruments"]:
        p = _path(k, False)
        by[k] = [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()] if p.exists() else []
    missing, prov = gaps(today, by)
    print("missing " + " ".join(missing))
    print("provisional " + " ".join(prov))
    return 0


if __name__ == "__main__":
    sys.exit(main())
