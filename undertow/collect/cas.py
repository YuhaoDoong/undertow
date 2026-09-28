"""按内容寻址的分块存储（Codex 017 A01）：任何一版原始输入都能无损还原，重复内容只存一次。

为什么不直接每天存全文：研报输入（CBOE 日线、Yahoo、FRED、COT…）压缩后全量约 6.5MB/天，一年 1.6GB 进 git 太大；
而旧的「每日尾部 40 行 + 月初全量」丢掉了月内对早期行的修订（017 probe：改第 0 行后尾部相同、哈希不同、修订值没存下）。

做法（纯标准库）：
- 把原始字节在换行与逗号之后切成小片（片界只由内容决定），再把连续小片聚成块：
  某片的 crc32 低 9 位为 0 且块长 ≥ MIN，或块长达到 MAX，就在该片之后断块。
  块界只依赖局部内容 → 末尾追加一行、中间改一个数，只影响附近一两个块，其余块原样复用。
- 块按 sha256 存：objects/<前两位>/<sha>.gz；一版原文的块序列存 recipes/<前两位>/<原文sha>.json。
- 还原 = 按 recipe 拼接各块，再核对整份 sha256；任何一块缺失/损坏 → 明确报错（CasCorrupt），不返回半截。
- 写入：同目录临时文件 + fsync + 回读校验 → os.replace；同一对象并发写入内容相同，替换无害。
只读本地、不联网。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import zlib
from pathlib import Path

ROOT = Path("data/history/inputs/cas")
MIN_CHUNK = 4096
MAX_CHUNK = 65536
MASK = 0x1FF                     # 平均每 512 片断一次（分块粒度；只影响去重效率，不影响还原正确性）
_SPLIT = re.compile(rb"(?<=[\n,])")
RECIPE_SCHEMA = 1


class CasCorrupt(RuntimeError):
    pass


def chunks(raw: bytes) -> list[bytes]:
    out, cur, n = [], [], 0
    for piece in _SPLIT.split(raw):
        if not piece:
            continue
        cur.append(piece); n += len(piece)
        if (n >= MIN_CHUNK and (zlib.crc32(piece) & MASK) == 0) or n >= MAX_CHUNK:
            out.append(b"".join(cur)); cur, n = [], 0
    if cur:
        out.append(b"".join(cur))
    return out


def _obj_path(root: Path, sha: str) -> Path:
    return root / "objects" / sha[:2] / f"{sha}.gz"


def _recipe_path(root: Path, sha: str) -> Path:
    return root / "recipes" / sha[:2] / f"{sha}.json"


def _atomic(path: Path, data: bytes, check) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        if not check(Path(name).read_bytes()):
            raise CasCorrupt(f"{path} 临时文件回读校验失败")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_obj(root: Path, sha: str) -> bytes:
    p = _obj_path(root, sha)
    if not p.exists():
        raise CasCorrupt(f"缺块 {sha}")
    try:
        b = gzip.decompress(p.read_bytes())
    except (OSError, EOFError) as e:
        raise CasCorrupt(f"块 {sha} 无法解压：{e}") from e
    if hashlib.sha256(b).hexdigest() != sha:
        raise CasCorrupt(f"块 {sha} 内容与名字不符")
    return b


def put(raw: bytes, *, root: Path | None = None) -> dict:
    """存一版原文。返回 {sha256, size, n_chunks, new_chunks, new_recipe}。已存过（recipe 存在且可验证）→ 不重复切块。"""
    root = root or ROOT                     # 运行时取（默认参数在定义时求值，测试替换 ROOT 会失效）
    sha = hashlib.sha256(raw).hexdigest()
    rp = _recipe_path(root, sha)
    if rp.exists():
        try:
            rec = json.loads(rp.read_text("utf-8"))
            if rec.get("sha256") == sha and all(_obj_path(root, c).exists() for c in rec["chunks"]):
                return {"sha256": sha, "size": len(raw), "n_chunks": len(rec["chunks"]), "new_chunks": 0,
                        "new_recipe": False}
        except (ValueError, KeyError):
            pass                        # recipe 坏了 → 下面重建（原文就在手里）；旧坏文件由 verify 负责隔离报告
    new = 0
    ids = []
    for c in chunks(raw):
        cs = hashlib.sha256(c).hexdigest()
        ids.append(cs)
        op = _obj_path(root, cs)
        if op.exists():
            continue
        gz = gzip.compress(c, mtime=0)
        _atomic(op, gz, lambda b, c=c: gzip.decompress(b) == c)
        new += 1
    rec = {"schema": RECIPE_SCHEMA, "sha256": sha, "size": len(raw), "chunks": ids}
    body = json.dumps(rec, separators=(",", ":")).encode()
    _atomic(rp, body, lambda b: b == body)
    if get(sha, root=root) != raw:                      # 整份回读：拼回来必须逐字节相同
        raise CasCorrupt(f"{sha} 存入后还原不一致")
    return {"sha256": sha, "size": len(raw), "n_chunks": len(ids), "new_chunks": new, "new_recipe": True}


def get(sha: str, *, root: Path | None = None) -> bytes:
    root = root or ROOT
    rp = _recipe_path(root, sha)
    if not rp.exists():
        raise CasCorrupt(f"没有 {sha} 的 recipe")
    try:
        rec = json.loads(rp.read_text("utf-8"))
    except ValueError as e:
        raise CasCorrupt(f"recipe {sha} 无法解析：{e}") from e
    raw = b"".join(_read_obj(root, c) for c in rec["chunks"])
    if hashlib.sha256(raw).hexdigest() != sha or len(raw) != rec.get("size"):
        raise CasCorrupt(f"{sha} 拼接后哈希/长度不符")
    return raw


def verify(*, root: Path | None = None) -> dict:
    """逐个 recipe 还原核对。返回 {recipes, ok, bad:[(sha, 原因)]}。只报告、不删除。"""
    root = root or ROOT
    bad, n = [], 0
    for rp in sorted((root / "recipes").glob("*/*.json")):
        n += 1
        sha = rp.stem
        try:
            get(sha, root=root)
        except CasCorrupt as e:
            bad.append((sha, str(e)))
    return {"recipes": n, "ok": n - len(bad), "bad": bad}
