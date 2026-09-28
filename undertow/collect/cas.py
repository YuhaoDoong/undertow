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
import sys
import tempfile
import time
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


_HEX = set("0123456789abcdef")


def _is_sha(x) -> bool:
    return isinstance(x, str) and len(x) == 64 and set(x) <= _HEX


def quarantine(path: Path, bad: bytes | None = None) -> Path | None:
    """把坏文件改名隔离（唯一名，原字节保留）。若改名后发现拿到的不是我们判定的坏字节（别的进程刚修好），换回去。"""
    if not path.exists():
        return None
    q = path.with_name(f"{path.name}.corrupt-{time.time_ns()}-{os.getpid()}")
    try:
        os.rename(path, q)
    except FileNotFoundError:
        return None
    if bad is not None and q.read_bytes() != bad:
        if not path.exists():
            os.rename(q, path)          # 移走的是别人刚写好的版本：放回去
        return None
    return q


def _read_obj(root: Path, sha: str) -> bytes:
    p = _obj_path(root, sha)
    if not p.exists():
        raise CasCorrupt(f"缺块 {sha}")
    try:
        b = gzip.decompress(p.read_bytes())
    except (OSError, EOFError, zlib.error) as e:
        raise CasCorrupt(f"块 {sha} 无法解压：{e}") from e
    if hashlib.sha256(b).hexdigest() != sha:
        raise CasCorrupt(f"块 {sha} 内容与名字不符")
    return b


def _load_recipe(root: Path, sha: str) -> dict:
    """读并做完整结构校验；任何结构错误统一为 CasCorrupt（Codex 019-01）。"""
    rp = _recipe_path(root, sha)
    if not rp.exists():
        raise CasCorrupt(f"没有 {sha} 的 recipe")
    try:
        rec = json.loads(rp.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeError) as e:
        raise CasCorrupt(f"recipe {sha} 无法解析：{e}") from e
    if not isinstance(rec, dict) or rec.get("schema") != RECIPE_SCHEMA or rec.get("sha256") != sha \
            or not isinstance(rec.get("size"), int) or not isinstance(rec.get("chunks"), list) \
            or not all(_is_sha(c) for c in rec["chunks"]):
        raise CasCorrupt(f"recipe {sha} 结构不合法")
    return rec


def _put_chunk(root: Path, c: bytes, repairs: list) -> tuple[str, bool]:
    cs = hashlib.sha256(c).hexdigest()
    op = _obj_path(root, cs)
    if op.exists():
        try:
            _read_obj(root, cs)
            return cs, False                       # 已有且验证通过
        except CasCorrupt as e:
            q = quarantine(op, op.read_bytes() if op.exists() else None)
            repairs.append({"object": cs, "why": str(e), "quarantined_as": q.name if q else None})
    gz = gzip.compress(c, mtime=0)
    _atomic(op, gz, lambda b, c=c: gzip.decompress(b) == c)
    return cs, True


def put(raw: bytes, *, root: Path | None = None) -> dict:
    """存一版原文并【验证可还原】后才返回成功。已有 recipe → 完整还原核对；坏块/坏 recipe 先隔离保留、留痕，
    再用手里的原文修复（Codex 019-01：以前只查文件存在就快速成功，坏 recipe 被静默覆盖）。
    返回 {sha256, size, n_chunks, new_chunks, new_recipe, repairs}。"""
    root = root or ROOT                     # 运行时取（默认参数在定义时求值，测试替换 ROOT 会失效）
    sha = hashlib.sha256(raw).hexdigest()
    repairs: list = []
    rp = _recipe_path(root, sha)
    if rp.exists():
        try:
            rec = _load_recipe(root, sha)
            if get(sha, root=root) == raw:
                return {"sha256": sha, "size": len(raw), "n_chunks": len(rec["chunks"]), "new_chunks": 0,
                        "new_recipe": False, "repairs": []}
        except CasCorrupt as e:
            bad_recipe = False
            try:
                _load_recipe(root, sha)
            except CasCorrupt:
                bad_recipe = True
            if bad_recipe:
                q = quarantine(rp, rp.read_bytes() if rp.exists() else None)
                repairs.append({"recipe": sha, "why": str(e), "quarantined_as": q.name if q else None})
            else:
                repairs.append({"recipe": sha, "why": str(e), "action": "块损坏，逐块验证后修复"})
    new, ids = 0, []
    for c in chunks(raw):
        cs, wrote = _put_chunk(root, c, repairs)
        ids.append(cs); new += wrote
    rec = {"schema": RECIPE_SCHEMA, "sha256": sha, "size": len(raw), "chunks": ids}
    body = json.dumps(rec, separators=(",", ":")).encode()
    if rp.exists():
        try:
            same = rp.read_bytes() == body
        except OSError:
            same = False
        if not same:
            q = quarantine(rp, rp.read_bytes())
            repairs.append({"recipe": sha, "why": "recipe 与重建内容不同", "quarantined_as": q.name if q else None})
    if not rp.exists():
        _atomic(rp, body, lambda b: b == body)
    if get(sha, root=root) != raw:                      # 整份回读：拼回来必须逐字节相同
        raise CasCorrupt(f"{sha} 存入后还原不一致")
    return {"sha256": sha, "size": len(raw), "n_chunks": len(ids), "new_chunks": new, "new_recipe": True,
            "repairs": repairs}


def get(sha: str, *, root: Path | None = None) -> bytes:
    root = root or ROOT
    rec = _load_recipe(root, sha)
    raw = b"".join(_read_obj(root, c) for c in rec["chunks"])
    if hashlib.sha256(raw).hexdigest() != sha or len(raw) != rec["size"]:
        raise CasCorrupt(f"{sha} 拼接后哈希/长度不符")
    return raw


def verify(*, root: Path | None = None) -> dict:
    """逐个 recipe 还原核对 + 逐个整份对象（blobs）核对哈希（Codex 021：巡检范围覆盖 blobs）。
    任何异常都记为该项 bad 并继续（不因一项崩溃中断整批）。只报告、不删除。"""
    root = root or ROOT
    bad, n = [], 0
    for rp in sorted((root / "recipes").glob("*/*.json")):
        n += 1
        sha = rp.stem
        try:
            get(sha, root=root)
        except Exception as e:
            bad.append((sha, f"{type(e).__name__}: {e}"))
    bbad, nb = [], 0
    for bp in sorted((root / "blobs").glob("*/*.bin")):
        nb += 1
        try:
            get_blob(bp.stem, root=root)
        except Exception as e:
            bbad.append((bp.stem, f"{type(e).__name__}: {e}"))
    return {"recipes": n, "ok": n - len(bad), "bad": bad, "blobs": nb, "blobs_ok": nb - len(bbad), "blobs_bad": bbad}


# —— 整份原样存储（Codex 020-02：被正式预测消费的快照版本必须可恢复）——
# 快照是已压缩的 gzip，分块去重几乎无效；这里按原字节整份存，文件名即 sha256。与 data/snapshots 下同一版本
# 字节完全相同 → 该版本也被 git 提交时，git 按内容只存一份 blob，几乎不增加仓库体积；
# 若同日被覆盖、中间版本从未提交，这里的副本就是唯一证据。


def _blob_path(root: Path, sha: str) -> Path:
    return root / "blobs" / sha[:2] / f"{sha}.bin"


def put_blob(raw: bytes, *, root: Path | None = None, repairs: list | None = None) -> str:
    """原样存入并回读核对；已有且完好 → 直接返回；已有但损坏 → 隔离保留后重写，并把修复记进 repairs、
    打印到 stderr（Codex 021：修复必须可见）。返回 sha256。"""
    root = root or ROOT
    sha = hashlib.sha256(raw).hexdigest()
    p = _blob_path(root, sha)
    if p.exists():
        old = p.read_bytes()
        if hashlib.sha256(old).hexdigest() == sha:
            return sha
        q = quarantine(p, old)
        info = {"blob": sha, "why": "内容与名字不符", "quarantined_as": q.name if q else None}
        if repairs is not None:
            repairs.append(info)
        print(f"[存档] ⚠️ 整份对象 {sha[:12]} 损坏，已隔离为 {info['quarantined_as']} 并重写", file=sys.stderr)
    _atomic(p, raw, lambda b: b == raw)
    return sha


def get_blob(sha: str, *, root: Path | None = None) -> bytes:
    root = root or ROOT
    p = _blob_path(root, sha)
    if not p.exists():
        raise CasCorrupt(f"缺整份对象 {sha}")
    b = p.read_bytes()
    if hashlib.sha256(b).hexdigest() != sha:
        raise CasCorrupt(f"整份对象 {sha} 内容与名字不符")
    return b
