"""主张权限登记表（Codex 015）：未登记无权限、T2/T3 与观测只能展示、T1 需完整依据、证据引用可解析。"""
import re
from pathlib import Path

import pytest

from undertow.analyze import claims as cl
from undertow.analyze import validation as vd

ROOT = Path(__file__).resolve().parents[1]


def test_roles_tiers_reasons_are_valid():
    for c in cl.CLAIMS.values():
        assert c.role in cl.ROLES, c.claim_id
        assert set(c.allowed_uses) <= set(cl.USES), c.claim_id
        assert c.reason in cl.REASONS, c.claim_id
        if c.role == "prediction":
            assert c.tier in cl.TIERS, c.claim_id
            assert c.tier != "T3" or c.reason, f"{c.claim_id}：T3 必须写明原因，不统称无效"
        else:
            assert c.tier is None, f"{c.claim_id}：只有预测主张分 T 级"
        if c.role == "policy":
            assert c.policy_ref, f"{c.claim_id}：政策必须标来源"


def test_unregistered_claim_has_no_decision_permission():
    assert cl.decision_allowed("no.such.claim", "direction") is False
    assert cl.decision_allowed("no.such.claim", "display") is False


@pytest.mark.parametrize("use", ["direction", "filter", "confidence", "ranking", "sizing", "holding"])
def test_t2_t3_and_observations_are_display_only(use):
    for c in cl.CLAIMS.values():
        if (c.role == "prediction" and c.tier in ("T2", "T3")) or c.role == "observation":
            assert cl.decision_allowed(c.claim_id, use) is False, (c.claim_id, use)
            assert cl.decision_allowed(c.claim_id, "display") is True


def test_t1_requires_full_basis():
    """T1 升级必须有事前方案、证据引用与审查记录；当前应为空（Codex 015 裁定）。"""
    for c in cl.t1_claims():
        assert c.prereg_ref and c.evidence_refs and c.review_record and c.scope, c.claim_id
    assert cl.t1_claims() == [], "Codex 015：本轮审到的主张暂不授予 T1；升级需新的审查记录"


def test_feasibility_and_policy_keep_protective_uses():
    assert cl.decision_allowed("feas.quote_quality", "filter")
    assert cl.decision_allowed("policy.risk_limits", "sizing")
    assert not cl.decision_allowed("risk_reward.auto_targets", "filter")


def test_evidence_refs_resolve():
    for c in cl.CLAIMS.values():
        for ref in c.evidence_refs:
            if " " not in ref and "/" not in ref and "." not in ref:
                assert ref in vd.REGISTRY, f"{c.claim_id}：validation 键 {ref} 不存在"
            else:
                path = re.split(r"[\s「（]", ref)[0]
                assert (ROOT / path).exists(), f"{c.claim_id}：证据文件 {path} 不存在"


def test_key_consumers_mapped():
    """会改变结论的现存消费者都要有映射（迁移前盘点）。"""
    joined = " ".join(" ".join(c.consumers) for c in cl.CLAIMS.values())
    for needle in ("verdict.build_verdict", "strategy.build_strategy", "credit_spread", "condor",
                   "strategy: 2 条以上否决", "cli: 解锁 credit-wall", "verdict: 别追"):
        assert needle in joined, needle


def test_render_md_lists_every_claim():
    md = cl.render_md()
    assert "当前为空" in md
    for cid in cl.CLAIMS:
        assert f"| {cid} |" in md
