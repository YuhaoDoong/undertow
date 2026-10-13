"""影子账 v5 冻结时序的派生标签（Codex 033 B2）。原始行一字不改；派生结果写版本化旁注。

  python3 scripts/shadow_freeze_timing.py            # 写 data/history/shadow_derived/<版本>_freeze_timing.jsonl（整份重算覆盖）

每行三个原始时刻：snapshot_available_at（identity.captured_at，快照首次可得）、candidate_first_frozen_at（recorded_at，候选首次冻结）、
certified_at（identity.certified_at，认证完成）；计划截止 = 当日 ET 09:30（原协议硬截止）。派生：
- delay_vs_reference_min：候选冻结相对「描述参考时点」ET 06:45 的延迟。06:45 是事后选的描述参考（daily 正常末段），
  不是每个历史会话事先都有的计划，也不是门槛（Codex 034 §7）；
- late_frozen_before_cutoff：晚于参考时点但早于 09:30 截止 —— 按原协议仍在主集，另做时间分层/敏感性分析；
- frozen_after_cutoff：候选在截止后才首次生成 —— 不得回填为盘前样本（原协议本就排除）；
- certified_after_cutoff_only：候选盘前已冻结、只是认证晚于截止 —— 与 late_frozen 分开，不混为晚冻结；
  认证时刻缺失 → None（未知不是 False）；
- timestamp_order_ok：快照可得 ≤ 候选冻结 ≤ 认证 是否成立（任一缺失 → None；False 列出供核对，不改原始行）。
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
VERSION = "freeze-timing-v2-20261013"
REFERENCE_ET, CUTOFF_ET = time(6, 45), time(9, 30)


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
    ref = datetime.combine(d, REFERENCE_ET, tzinfo=ET)
    cutoff = datetime.combine(d, CUTOFF_ET, tzinfo=ET)
    out = {"key": row.get("key") or f"{row.get('instrument')}|{s}", "session": s, "instrument": row.get("instrument"),
           "derivation_version": VERSION, "snapshot_available_at": idt.get("captured_at"),
           "candidate_first_frozen_at": row.get("recorded_at"), "certified_at": idt.get("certified_at"),
           "reference_time_et": REFERENCE_ET.strftime("%H:%M"), "cutoff_et": CUTOFF_ET.strftime("%H:%M"),
           "identity_status": idt.get("status"), "mode": idt.get("mode")}
    if frozen is None:
        out.update(delay_vs_reference_min=None, late_frozen_before_cutoff=None, frozen_after_cutoff=None,
                   certified_after_cutoff_only=None, timestamp_order_ok=None)
        return out
    out["delay_vs_reference_min"] = round((frozen - ref).total_seconds() / 60, 1)
    out["frozen_after_cutoff"] = frozen >= cutoff
    out["late_frozen_before_cutoff"] = ref < frozen < cutoff
    out["certified_after_cutoff_only"] = None if cert is None else (frozen < cutoff and cert >= cutoff)
    out["timestamp_order_ok"] = None if (snap is None or cert is None) else (snap <= frozen <= cert)
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
    unk = sum(1 for r in rows if r["certified_after_cutoff_only"] is None)
    bad = sum(1 for r in rows if r["timestamp_order_ok"] is False)
    print(f"前瞻行 {n}：晚于参考时点但截止前冻结 {late}、截止后才冻结 {after}、仅认证晚于截止 {cert}、"
          f"认证时刻未知 {unk}、三时刻顺序异常 {bad} → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
