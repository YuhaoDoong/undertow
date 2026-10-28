"""作者判断结构化登记的校验（合成条目，不含任何真实作者原文）。"""
from __future__ import annotations

import json

from undertow.soul import author_claims as A


def _base(**kw):
    c = {"claim_id": "x1", "schema_version": A.SCHEMA_VERSION, "claim_type": "unconditional_direction",
         "author": "外部作者X", "statement_id": "s1", "source_ref": {"path": "private/x.png", "sha256": "ab"},
         "posted_at": "2026-10-27T15:09:00+08:00", "posted_tz_basis": "群聊时间戳（北京时间）",
         "earliest_readable_at": "2026-10-27T15:09:00+08:00", "market": "XAU", "qualifiers": ["合成限定语"],
         "coded_at": "2026-10-27T15:10:00+08:00", "coded_before_outcome": True, "retrospective": False,
         "scorable": True, "event_cluster": "evt-1", "direction": "up", "horizon_state": "given",
         "deadline": "2026-10-28T23:59:00+08:00"}
    c.update(kw)
    return c


def test_valid_prospective_direction():
    assert A.validate(_base()) == []


def test_state_never_scorable_and_needs_polarity():
    e = A.validate(_base(claim_type="state", polarity="fear_extreme"))
    assert any("不可计分" in x for x in e)
    assert A.validate(_base(claim_type="state", polarity="fear_extreme", scorable=False,
                            unscorable_reason="状态主张只做一致性对照")) == []
    assert any("polarity" in x for x in A.validate(_base(claim_type="state", scorable=False,
                                                         unscorable_reason="r")))


def test_undetermined_horizon_not_scorable():
    e = A.validate(_base(horizon_state="undetermined", deadline=None))
    assert any("期限未定" in x for x in e)


def test_retrospective_required_when_coded_after_outcome():
    e = A.validate(_base(coded_before_outcome=False, retrospective=False))
    assert any("retrospective" in x for x in e)
    e = A.validate(_base(coded_before_outcome=False, retrospective=True))
    assert any("retrospective 条目不能计入" in x for x in e)


def test_conditional_needs_condition_and_trigger():
    e = A.validate(_base(claim_type="conditional_direction"))
    assert any("condition.text" in x for x in e)
    e = A.validate(_base(claim_type="conditional_direction", condition={"text": "反弹失败"}))
    assert any("条件未触发" in x for x in e)


def test_ambiguous_never_scorable_and_revision_needs_supersedes():
    assert any("不可计分" in x for x in A.validate(_base(claim_type="ambiguous")))
    assert any("supersedes" in x for x in A.validate(_base(claim_type="revision")))


def test_naive_timestamp_rejected():
    assert any("时区" in x for x in A.validate(_base(earliest_readable_at="2026-10-27T15:09:00")))


def test_validate_file_chain(tmp_path):
    p = tmp_path / "c.jsonl"
    rows = [_base(), _base(claim_id="x2", claim_type="revision", supersedes="nope")]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{bad\n")
    rep = A.validate_file(p)
    assert rep["n"] == 2 and 3 in rep["errors"]
    assert any("未知 claim" in x for x in rep["chain_errors"])
