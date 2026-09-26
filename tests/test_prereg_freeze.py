"""预登记守卫：冻结的分析代码 / 信号算法 / 预登记文本被改动而没有升版本 → 测试失败。

改这些文件是允许的，但必须同时写新的预登记版本（新文件、新版本号、看结果前冻结），
并把本测试指向新版本 —— 否则「事前设计」会在不知不觉中被事后改掉。
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs" / "prereg" / "2026-09-26_direction_v1.json"


def test_direction_prereg_frozen_files_unchanged():
    doc = json.loads(PREREG.read_text("utf-8"))
    for rel, sha in doc["frozen_sha256"].items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert got == sha, (f"{rel} 在预登记冻结后被改动。flow.py 的无关改动：先确认 call_direction 在已记录样本上逐行不变，"
                            f"再追加修订记录并更新哈希；否则需要新的预登记版本（见 docs/prereg/ 修订规则）")


def test_direction_prereg_matches_code_constants():
    from undertow.analyze import shadow_direction as sd
    doc = json.loads(PREREG.read_text("utf-8"))
    assert doc["analysis"] == json.loads(json.dumps(sd.ANALYSIS, ensure_ascii=False))
    assert doc["base"]["shadow_config_version"] == sd.ANALYSIS["base_config_version"]
