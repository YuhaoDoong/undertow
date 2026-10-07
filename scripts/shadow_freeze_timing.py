"""影子账 v5 冻结时序的派生标签（Codex 033 B2）。原始行一字不改；派生结果写版本化旁注。

  python3 scripts/shadow_freeze_timing.py            # 写 data/history/shadow_derived/<版本>_freeze_timing.jsonl（整份重算覆盖）

每行三个原始时刻：snapshot_available_at（identity.captured_at，快照首次可得）、candidate_first_frozen_at（recorded_at，候选首次冻结）、
certified_at（identity.certified_at，认证完成）；计划截止 = 当日 ET 09:30（原协议硬截止）。派生：
- delay_vs_plan_min：候选冻结相对「计划冻结时刻」的延迟（计划时刻取 ET 06:45，即 daily 正常末段；只作描述，不是新门槛）；
- late_frozen_before_cutoff：晚于计划时刻但早于 09:30 截止 —— 按原协议仍在主集，另做时间分层/敏感性分析；
- frozen_after_cutoff：候选在截止后才首次生成 —— 不得回填为盘前样本（原协议本就排除）；
- certified_after_cutoff_only：候选盘前已冻结、只是认证晚于截止 —— 与 late_frozen 分开，不混为晚冻结。
不追加 06:xx 硬门槛去事后剔除样本。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
VERSION = "freeze-timing-v1-20261007"
PLAN_ET, CUTOFF_ET = time(6, 45), time(9, 30)


def _t(x):
    try:
        t = datetime.fromisoformat(str(x))
        return t if t.tzinfo else None
    except (TypeError, ValueError):
        return None


def derive(row: dict) -> dict:
    """纯函数：一行影子账 → 冻结时序标签。时刻缺失或无时区 → 对应标签为 None（未知不是通过）。"""
    idt = row.get("identity") or {}
    s = row["session"]
    snap, frozen, cert = _t(idt.get("captured_at")), _t(row.get("recorded_at")), _t(idt.get("certified_at"))
    d = datetime.fromisoformat(s).date()
    plan = datetime.combine(d, PLAN_ET, tzinfo=ET)
    cutoff = datetime.combine(d, CUTOFF_ET, tzinfo=ET)
    out = {"key": row.get("key") or f"{row.get('instrument')}|{s}", "session": s, "instrument": row.get("instrument"),
           "derivation_version": VERSION, "snapshot_available_at": idt.get("captured_at"),
           "candidate_first_frozen_at": row.get("recorded_at"), "certified_at": idt.get("certified_at"),
           "plan_et": PLAN_ET.strftime("%H:%M"), "cutoff_et": CUTOFF_ET.strftime("%H:%M"),
           "identity_status": idt.get("status"), "mode": idt.get("mode")}
    if frozen is None:
        out.update(delay_vs_plan_min=None, late_frozen_before_cutoff=None, frozen_after_cutoff=None, certified_after_cutoff_only=None)
        return out
    out["delay_vs_plan_min"] = round((frozen - plan).total_seconds() / 60, 1)
    out["frozen_after_cutoff"] = frozen >= cutoff
    out["late_frozen_before_cutoff"] = plan < frozen < cutoff
    out["certified_after_cutoff_only"] = (frozen < cutoff and cert is not None and cert >= cutoff)
    return out


def main() -> int:
    from undertow.analyze import shadow as sh
    d = ROOT / "data/history/shadow" / sh.CONFIG["version"]
    rows = []
    for f in sorted(d.glob("*.jsonl")):
        if f.name.startswith("_"):
            continue
        for x in f.read_text("utf-8").splitlines():
            if x.strip():
                r = json.loads(x)
                if (r.get("identity") or {}).get("mode") == "prospective":
                    rows.append(derive(r))
    # 不放进版本目录：shadow_cli 会把该目录下所有 *.jsonl 当成品种台账读取
    out = ROOT / "data/history/shadow_derived" / f"{sh.CONFIG['version']}_freeze_timing.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    tmp.replace(out)
    n = len(rows)
    late = sum(1 for r in rows if r["late_frozen_before_cutoff"])
    after = sum(1 for r in rows if r["frozen_after_cutoff"])
    cert = sum(1 for r in rows if r["certified_after_cutoff_only"])
    print(f"前瞻行 {n}：晚于计划但截止前冻结 {late}、截止后才冻结 {after}、仅认证晚于截止 {cert} → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
