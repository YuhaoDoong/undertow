"""影子账冻结时序派生标签（Codex 033 B2）：晚冻结但截止前 / 截止后才冻结 / 只是认证晚于截止，三者分开；缺时刻 → None。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.shadow_freeze_timing import derive  # noqa: E402


def R(rec, cert, cap="2026-10-01T10:45:10+00:00"):
    return {"key": "x", "instrument": "silver", "session": "2026-10-01", "recorded_at": rec,
            "identity": {"mode": "prospective", "captured_at": cap, "certified_at": cert, "status": "certified"}}


def test_labels():
    late = derive(R("2026-10-01T13:13:52+00:00", "2026-10-01T13:13:58+00:00"))        # ET 09:13 冻结
    assert late["late_frozen_before_cutoff"] and not late["frozen_after_cutoff"] and not late["certified_after_cutoff_only"]
    assert late["delay_vs_plan_min"] > 140
    cert = derive(R("2026-10-01T10:04:11+00:00", "2026-10-01T14:38:00+00:00"))        # 06:04 冻结、10:38 才认证
    assert cert["certified_after_cutoff_only"] and not cert["late_frozen_before_cutoff"] and cert["delay_vs_plan_min"] < 0
    after = derive(R("2026-10-01T14:00:00+00:00", "2026-10-01T14:01:00+00:00"))       # 10:00 才首次冻结
    assert after["frozen_after_cutoff"] and not after["late_frozen_before_cutoff"]
    unk = derive(R(None, None))
    assert unk["late_frozen_before_cutoff"] is None and unk["frozen_after_cutoff"] is None
