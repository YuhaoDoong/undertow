"""备份恢复演练（Codex 024-6：一次只读恢复演练比「提交成功」更有价值）。

  python3 scripts/restore_drill.py a2309a5            # 从该提交抽样还原并核对
  python3 scripts/restore_drill.py a2309a5 --n 8 --seed 7

做法：列出该提交改动的 data/ 文件 → 固定种子抽样 → `git show <提交>:<路径>` 取出字节写入临时目录（不碰工作区）→
核对：①字节 sha256 与 git 记录的 blob 一致（git hash-object 复算）②能解析（.json.gz 解压 + JSON；.jsonl 逐行 JSON；
.json JSON）③行数/记录数、顶层字段 ④若含来源字段（basis / source / captured_at / fetched_at）则列出。只读，不联网。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*a, binary=False):
    r = subprocess.run(["git", *a], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[:200])
    return r.stdout if binary else r.stdout.decode()


def check(raw: bytes, path: str) -> dict:
    out = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16]}
    try:
        if path.endswith(".json.gz"):
            obj = json.loads(gzip.decompress(raw).decode("utf-8"))
            out.update(kind="json.gz", keys=sorted(obj)[:8] if isinstance(obj, dict) else None,
                       n=len(obj.get("contracts") or obj.get("payload") or []) if isinstance(obj, dict) else len(obj))
            src = {k: obj.get(k) for k in ("basis", "source", "captured_at", "date", "symbol") if isinstance(obj, dict) and k in obj}
        elif path.endswith(".jsonl"):
            rows = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
            out.update(kind="jsonl", n=len(rows), keys=sorted(rows[-1])[:8] if rows else None)
            src = {k: rows[-1].get(k) for k in ("recorded_at", "captured_at", "fetched_at", "started_at") if rows and k in rows[-1]}
        elif path.endswith(".json"):
            obj = json.loads(raw.decode("utf-8"))
            out.update(kind="json", keys=sorted(obj)[:8] if isinstance(obj, dict) else None)
            src = {}
        else:
            out.update(kind="other"); src = {}
        out["source"] = {k: (str(v)[:60] if v is not None else None) for k, v in src.items()}
        out["parse"] = "ok"
    except Exception as e:
        out["parse"] = f"FAIL {type(e).__name__}: {e}"[:160]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("commit"); ap.add_argument("--n", type=int, default=6); ap.add_argument("--seed", type=int, default=20260929)
    a = ap.parse_args()
    files = [f for f in git("show", "--name-only", "--format=", a.commit).splitlines()
             if f.startswith("data/") and not f.endswith(".lock")]
    exist = [f for f in files if git("cat-file", "-t", f"{a.commit}:{f}").strip() == "blob"] if files else []
    pick = sorted(random.Random(a.seed).sample(exist, min(a.n, len(exist))))
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        for f in pick:
            raw = git("show", f"{a.commit}:{f}", binary=True)
            dst = Path(td) / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(raw)
            blob = git("rev-parse", f"{a.commit}:{f}").strip()
            re_blob = git("hash-object", str(dst)).strip()
            r = check(dst.read_bytes(), f)
            ok = blob == re_blob and r["parse"] == "ok"
            bad += not ok
            print(f"{'✅' if ok else '❌'} {f}\n    blob {blob[:12]} 复算 {re_blob[:12]}；{r}")
    print(f"演练 {a.commit}：该提交 data/ 文件 {len(exist)} 个，抽样 {len(pick)}，失败 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
