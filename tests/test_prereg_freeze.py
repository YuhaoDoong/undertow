"""预登记守卫：冻结的分析代码 / 信号算法 / 依赖函数 / 预登记文本被改动而没有升版本 → 测试失败。

改这些文件是允许的，但必须同时写新的预登记版本（新文件、新版本号、看结果前冻结），
并把本测试指向新版本 —— 否则「事前设计」会在不知不觉中被事后改掉。

现行：dir-analysis-v1.2（2026-09-26，Codex 012）。v1、v1.1 已被取代：它们的代码守卫退役，
但两版的文本与 JSON 必须保持原样（哈希记在 v1.2 JSON 的 supersedes 段），被取代的旧判断不静默覆盖。
"""
import hashlib
import inspect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs" / "prereg" / "2026-09-26_direction_v1.2.json"

_MSG = ("在预登记冻结后被改动。flow.py/依赖函数的无关改动：先确认分析结果在已记录样本上逐行不变，"
        "再追加修订记录并更新哈希；否则需要新的预登记版本（见 docs/prereg/ 修订规则）")


def _doc():
    return json.loads(PREREG.read_text("utf-8"))


def test_direction_prereg_frozen_files_unchanged():
    for rel, sha in _doc()["frozen_sha256"].items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == sha, f"{rel} {_MSG}"


def test_direction_prereg_dependency_fingerprints_unchanged():
    from undertow.analyze import shadow_direction as sd
    from undertow.analyze import signal_ledger as sl
    want = _doc()["dependency_fingerprints"]
    got = sd.dependency_fingerprints()
    got["signal_ledger.call_code_sha"] = hashlib.sha256(inspect.getsource(sl.call_code_sha).encode()).hexdigest()[:16]
    got["signal_ledger.CALL_CODE_FILES"] = list(sl.CALL_CODE_FILES)
    for k, v in want.items():
        assert got[k] == v, f"依赖 {k} {_MSG}"


def test_direction_prereg_matches_code_constants():
    from undertow.analyze import shadow_direction as sd
    doc = _doc()
    assert doc["analysis"] == json.loads(json.dumps(sd.ANALYSIS, ensure_ascii=False))
    assert doc["base"]["shadow_config_version"] == sd.ANALYSIS["base_config_version"]


def test_superseded_v1_documents_preserved():
    sup = _doc()["supersedes"]
    assert sup["version"] == "dir-analysis-v1.1-20260926" and sup["real_samples_seen"] == 0
    assert len(sup["sha256"]) == 4           # v1 与 v1.1 的文本和 JSON
    for rel, sha in sup["sha256"].items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == sha, f"被取代的 {rel} 不得改动"


def test_v1_and_current_share_design_constants():
    """v1.1/v1.2 不改设计常数：v1 冻结的常数必须原样保留（version/supersedes 以外）。"""
    v1 = json.loads((ROOT / "docs" / "prereg" / "2026-09-26_direction_v1.json").read_text("utf-8"))["analysis"]
    v11 = _doc()["analysis"]
    for k, v in v1.items():
        if k != "version":
            assert v11[k] == v, k
