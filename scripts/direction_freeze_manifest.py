"""方向台账族 D 冻结清单（Codex 020：提交唯一版本清单——规则/协议/计算器/日历/输入存档标识）。

  python3 scripts/direction_freeze_manifest.py            # 打印当前哈希
  python3 scripts/direction_freeze_manifest.py --write    # 写 docs/prereg/2026-09-28_direction_freeze_manifest.json

只读、纯标准库。清单是【草案】直到 Codex 验收并提交冻结；冻结后任一哈希变化 = 新版本，不得沿用旧起点。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FILES = ["docs/prereg/2026-09-28_skew_reading_v1.md", "docs/prereg/2026-09-28_skew_reading_v1_addendum.md",
         "docs/prereg/2026-09-28_skew_reading_v1_addendum2.md", "docs/prereg/2026-09-28_conviction_v1.1.md",
         "docs/prereg/2026-09-28_skew_reading_v1_addendum3.md",
         "docs/prereg/2026-09-28_conviction_v1.2.md", "docs/prereg/2026-09-28_conviction_v1.3.md",
         "docs/prereg/2026-09-28_conviction_v1.4.md",
         "undertow/analyze/skew_reading.py", "undertow/analyze/conviction.py", "undertow/analyze/direction_stats.py",
         "undertow/analyze/structure_read.py", "undertow/analyze/flow.py", "undertow/dirledger_cli.py",
         "undertow/core/market_calendar.py", "undertow/core/clock.py", "undertow/collect/store.py",
         "undertow/collect/cas.py", "undertow/collect/provenance.py", "undertow/collect/longbridge_kline.py",
         "undertow/collect/cboe_options.py"]


def build() -> dict:
    from undertow.analyze import conviction as cv
    from undertow.analyze import direction_stats as ds
    from undertow.analyze import shadow
    from undertow.analyze import skew_reading as skr
    from undertow.core import market_calendar as mc
    rules = {"skew_reading.RULE": skr.RULE, "conviction.RULE": cv.RULE, "conviction.H3_RULE": cv.H3_RULE,
             "direction_stats": {k: getattr(ds, k) for k in ("FAMILY_SIZE", "ALPHA", "QUANTILE_METHOD", "BLOCK_DAYS",
                                                            "SENSITIVITY_BLOCKS", "ITERS", "SEED", "MIN_NONEVENT_DAYS",
                                                            "MAX_INVALID_FRAC", "SMA_DAYS")}}
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    return {"status": "draft（待 Codex 验收；验收后冻结，正式起点=冻结提交后的下一个交易日）", "git_head_at_build": head,
            "files_sha256": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FILES},
            "rules": json.loads(json.dumps(rules, default=str)),
            "rules_sha16": {k: hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:16]
                            for k, v in rules.items()},
            "calendar_hash": mc.calendar_hash(), "shadow_v5_config_hash": shadow.config_hash(),
            "input_archive": {"scheme": "cas 分块(recipes/objects) + 整份 blobs（被正式预测消费的快照）",
                              "root": "data/history/inputs/cas"},
            "freeze_step": "conviction.RULE.version 由 conviction-dev-20260928 改为 conviction-h1-v1-<冻结日>、status=frozen；"
                           "新目录记录；开发期目录保留、不回填"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    m = build()
    txt = json.dumps(m, ensure_ascii=False, indent=1)
    if a.write:
        (ROOT / "docs/prereg/2026-09-28_direction_freeze_manifest.json").write_text(txt + "\n", "utf-8")
    print(txt)


if __name__ == "__main__":
    main()
