"""外部作者判断的结构化登记（Codex 066 合同 A1，`author-claim-v1-20261028`）。

代码公开、数据私有：登记文件在 `data/soul/author_claims.jsonl`（gitignored），原文与截图只在私有目录。
本模块只定义字段与校验，不读付费原文、不计分。

为什么要拆字段（066 A1）：作者一句话里常常同时有「状态描述」「方向」「条件」和「两头留口」的限定语。
旧做法只摘出一个方向去对结果，事后总能挑到对的那半句。这里把五类分开：
  state                   状态主张（如「恐慌情绪到位」）——可以和情绪面板对照一致性，但**不自动升级成方向预测**
  unconditional_direction 无条件方向
  conditional_direction   条件方向（「弹不上去就是跌」）：必须写条件；条件未触发前不计分
  revision                修订：只从它实际可读之后生效，不覆盖早先的判断（supersedes 指向旧 claim）
  ambiguous               歧义：待裁决，**永不计分**
有方向但没有可辨识期限 → horizon_state=undetermined，不可计分，不猜一个对作者最有利的窗口。
看过结果之后才编码的条目必须标 retrospective=True，不能改发布时间把它变成前瞻样本。
同一消息驱动的多品种走势放进同一 event_cluster：这只是依赖性处理，不认证新闻因果。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "author-claim-v1-20261028"
CLAIM_TYPES = ("state", "unconditional_direction", "conditional_direction", "revision", "ambiguous")
DIRECTIONS = ("up", "down")
HORIZON_STATES = ("given", "undetermined")
POLARITIES = ("fear_extreme", "greed_extreme", "neutral_uncertain", "other")
REQUIRED = ("claim_id", "schema_version", "claim_type", "author", "statement_id", "source_ref",
            "posted_at", "posted_tz_basis", "earliest_readable_at", "market", "qualifiers",
            "coded_at", "coded_before_outcome", "retrospective", "scorable", "event_cluster")


def _aware(ts) -> bool:
    if not isinstance(ts, str):
        return False
    try:
        return datetime.fromisoformat(ts).tzinfo is not None
    except ValueError:
        return False


def validate(c: dict) -> list[str]:
    """返回错误列表；空列表 = 合格。"""
    errs = [f"缺字段 {k}" for k in REQUIRED if k not in c]
    if errs:
        return errs
    t = c["claim_type"]
    if c["schema_version"] != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if t not in CLAIM_TYPES:
        errs.append(f"claim_type 非法：{t}")
    for k in ("earliest_readable_at", "coded_at"):
        if not _aware(c[k]):
            errs.append(f"{k} 必须是带时区的 ISO 时刻")
    if c["posted_at"] is not None and not _aware(c["posted_at"]):
        errs.append("posted_at 必须带时区或为 null（未知）")
    if not isinstance(c["qualifiers"], list):
        errs.append("qualifiers 必须是列表（保留全部限定语，不能只摘方向）")
    ref = c["source_ref"]
    if not (isinstance(ref, dict) and ref.get("path") and ref.get("sha256")):
        errs.append("source_ref 需要私有原文路径与 sha256")
    if t == "state" and c.get("polarity") not in POLARITIES:
        errs.append("state 需要 polarity")
    if t in ("unconditional_direction", "conditional_direction"):
        if c.get("direction") not in DIRECTIONS:
            errs.append("方向类 claim 需要 direction=up/down")
        if c.get("horizon_state") not in HORIZON_STATES:
            errs.append("方向类 claim 需要 horizon_state")
        if c.get("horizon_state") == "given" and not c.get("deadline"):
            errs.append("horizon_state=given 必须写 deadline")
    if t == "conditional_direction" and not (c.get("condition") or {}).get("text"):
        errs.append("条件方向必须写 condition.text")
    if t == "revision" and not c.get("supersedes"):
        errs.append("revision 必须写 supersedes")
    # 不变式：哪些情况一定不可计分
    if c["coded_before_outcome"] is False and c["retrospective"] is not True:
        errs.append("看过结果后才编码 → retrospective 必须为 True")
    if c["scorable"]:
        if t in ("state", "ambiguous"):
            errs.append(f"{t} 不可计分（state 只做一致性对照；ambiguous 待裁决）")
        if c.get("horizon_state") != "given":
            errs.append("期限未定不可计分")
        if c["retrospective"]:
            errs.append("retrospective 条目不能计入前瞻计分")
        if t == "conditional_direction" and not (c.get("condition") or {}).get("triggered_at"):
            errs.append("条件未触发不可计分")
    elif not c.get("unscorable_reason"):
        errs.append("scorable=False 需要 unscorable_reason")
    return errs


def validate_file(path: Path) -> dict:
    """逐行校验私有登记文件；坏行报行号，不改文件。返回 {n, errors: {行号: [..]}, chain_errors}。"""
    rows, errors = [], {}
    for i, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            errors[i] = [f"JSON 解析失败：{e}"]
            continue
        rows.append(r)
        e = validate(r)
        if e:
            errors[i] = e
    ids = [r.get("claim_id") for r in rows]
    chain = [f"claim_id 重复：{x}" for x in {x for x in ids if ids.count(x) > 1}]
    known = set(ids)
    chain += [f"{r['claim_id']} supersedes 未知 claim {r['supersedes']}" for r in rows
              if r.get("supersedes") and r["supersedes"] not in known]
    return {"n": len(rows), "errors": errors, "chain_errors": chain}


if __name__ == "__main__":       # python -m undertow.soul.author_claims [路径]
    import sys
    p = Path(sys.argv[1] if len(sys.argv) > 1 else "data/soul/author_claims.jsonl")
    rep = validate_file(p)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    sys.exit(1 if rep["errors"] or rep["chain_errors"] else 0)
