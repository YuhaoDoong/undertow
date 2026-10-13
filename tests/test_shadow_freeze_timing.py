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
    assert late["delay_vs_reference_min"] > 140
    cert = derive(R("2026-10-01T10:04:11+00:00", "2026-10-01T14:38:00+00:00"))        # 06:04 冻结、10:38 才认证
    assert cert["certified_after_cutoff_only"] and not cert["late_frozen_before_cutoff"] and cert["delay_vs_reference_min"] < 0
    after = derive(R("2026-10-01T14:00:00+00:00", "2026-10-01T14:01:00+00:00"))       # 10:00 才首次冻结
    assert after["frozen_after_cutoff"] and not after["late_frozen_before_cutoff"]
    unk = derive(R(None, None))
    assert unk["late_frozen_before_cutoff"] is None and unk["frozen_after_cutoff"] is None


def test_missing_certification_is_unknown_and_order_checked():
    """Codex 034 §7：认证时刻缺失 → certified_after_cutoff_only=None（不是 False）；三时刻顺序异常单独标出。"""
    nocert = derive(R("2026-10-01T10:04:11+00:00", None))
    assert nocert["certified_after_cutoff_only"] is None and nocert["timestamp_order_ok"] is None
    ok = derive(R("2026-10-01T10:50:00+00:00", "2026-10-01T10:55:00+00:00"))
    assert ok["timestamp_order_ok"] is True and ok["reference_time_et"] == "06:45"
    bad = derive(R("2026-10-01T10:40:00+00:00", "2026-10-01T10:55:00+00:00"))          # 冻结早于快照可得
    assert bad["timestamp_order_ok"] is False
