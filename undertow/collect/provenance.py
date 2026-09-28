"""研报实际消费输入的留痕（Codex 017 A02）：在取数边界记录「这次运行真正用了哪一版输入」。

旧做法是研报跑完后扫描 data/cache —— 缓存可能已被别的进程改写，也可能含没用到的文件，
长桥 K 线根本不经过缓存。事后扫描证明不了研报用的是哪一版。

现在：
- FileCache.get/set、长桥 kline、快照读取在返回数据的那一刻调用 consume_*()：
  原始字节立即存进 cas（按内容去重），同时在本进程的运行清单里记一条（来源、key、sha256、大小、
  fresh_fetch / cache_hit / stale_cache、抓取时刻、缓存年龄、TTL）。快照已按日入库，只记路径与 sha，不重复存。
- cli.main 在命令结束时写清单 data/history/inputs/manifests/<ET日>/<时刻>_<命令>_<pid>.json（含 argv、git HEAD、返回码）。
- 只在 begin() 之后生效（cli 对白名单命令开启；pytest 下不开启），库函数被单独调用时不写任何东西。
- 留痕失败不能让研报静默缺一块：失败记进清单的 errors 并打印到 stderr，清单照写。
不含账户、凭证或付费内容：只登记公开行情源（缓存 key 白名单前缀 + 长桥行情 K 线 + 期权快照）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from undertow.collect import cas

MANIFEST_DIR = Path("data/history/inputs/manifests")
PUBLIC_CACHE_PREFIX = ("cboehist_", "cboevol_", "cot_", "fred_", "yahoo_", "ffcal_")
SCHEMA = 1

_run: dict | None = None


def active() -> bool:
    return _run is not None


def begin(command: str, argv: list[str]) -> None:
    global _run
    _run = {"schema": SCHEMA, "command": command, "argv": list(argv), "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(), "items": [], "errors": [], "_seen": set()}


def _add(item: dict) -> None:
    """同一 (来源, key, 版本) 只记一次；同 key 不同版本各记一条，按消费顺序编号 seq（Codex 019-02）。"""
    sig = (item["kind"], item["key"], item.get("sha256"))
    if sig in _run["_seen"]:
        return
    _run["_seen"].add(sig)
    item["seq"] = len(_run["items"])
    _run["items"].append(item)


def _err(msg: str) -> None:
    _run["errors"].append(msg)
    print(f"[留痕] ⚠️ {msg}", file=sys.stderr)


def consume_bytes(kind: str, key: str, raw: bytes, *, status: str, fetched_at: float | None = None,
                  age_s: float | None = None, ttl_s: float | None = None) -> None:
    if _run is None:
        return
    item = {"kind": kind, "key": key, "status": status, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "fetched_at": datetime.fromtimestamp(fetched_at, timezone.utc).isoformat() if fetched_at else None,
            "age_s": None if age_s is None else round(age_s, 1), "ttl_s": ttl_s, "stored": False}
    try:
        res = cas.put(raw)                         # put 已验证可逐字节还原才返回（019-01）
        item["stored"] = True
        if res.get("repairs"):
            item["cas_repairs"] = res["repairs"]
            _err(f"{kind}:{key} 存入时发现并修复了 cas 损坏：{len(res['repairs'])} 处（已隔离保留）")
    except Exception as e:                         # 存不下也要在清单里显式可见
        _err(f"{kind}:{key} 存入 cas 失败：{type(e).__name__}: {e}")
    _add(item)


def consume_cache_raw(key: str, raw: bytes, *, status: str, ttl_s: float | None) -> None:
    """登记调用方【已经读到手】的那份原文 —— 绝不再按路径重读（重读之间可能被别的进程替换，Codex 019-02）。"""
    if _run is None or not key.startswith(PUBLIC_CACHE_PREFIX):
        return
    try:
        fetched = json.loads(raw.decode("utf-8")).get("fetched_at")
    except (ValueError, UnicodeError, AttributeError) as e:
        _err(f"cache:{key} 原文无法解析 fetched_at：{type(e).__name__}")
        fetched = None
    age = (time.time() - fetched) if isinstance(fetched, (int, float)) else None
    if status == "cache_hit" and ttl_s is not None and age is not None and age > ttl_s:
        status = "stale_cache"
    consume_bytes("cache", key, raw, status=status, fetched_at=fetched, age_s=age, ttl_s=ttl_s)


def reference_bytes(kind: str, key: str, path: Path, raw: bytes, *, captured_at: float | None = None) -> None:
    """已按日入库的文件（期权快照）：用调用方已读到的同一份字节算 sha，只记路径与 sha，不重复存。
    ⚠️ 快照文件同日可被覆盖（store.save os.replace）；被覆盖的旧版本只能在它被 git 提交过时找回 ——
    清单能检出「后来变了」，但不保证能还原（Codex 019-02 已知局限）。"""
    if _run is None:
        return
    _add({"kind": kind, "key": key, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
          "captured_at": captured_at, "status": "stored_file", "stored": False})


def _git_head() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def finish(rc: int | None, *, out_dir: Path = MANIFEST_DIR) -> Path | None:
    """写清单（没有消费任何输入 → 不写）。返回路径。"""
    global _run
    run, _run = _run, None
    if run is None or not (run["items"] or run["errors"]):
        return None
    from undertow.core.clock import market_today
    run.pop("_seen", None)
    run.update({"finished_at": datetime.now(timezone.utc).isoformat(), "rc": rc, "git_head": _git_head()})
    body = json.dumps(run, ensure_ascii=False, indent=1).encode()
    d = out_dir / market_today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    path = d / f"{stamp}_{run['command']}_{run['pid']}.json"
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body); f.flush(); os.fsync(f.fileno())
        if Path(name).read_bytes() != body:
            raise OSError("清单回读校验失败")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def restore_inputs(manifest: Path) -> dict:
    """按清单逐条还原（按消费顺序 seq；同 key 多版本各自保留，Codex 019-02）。
    返回 {complete, problems, items:[{seq, kind, key, sha256, raw|None}]}。清单含 errors、某项未存入 cas、
    或快照已与清单 sha 不符 → complete=False 并列出原因（不再笼统称可回放）。"""
    run = json.loads(manifest.read_text("utf-8"))
    problems = [f"运行时留痕错误：{e}" for e in run.get("errors", [])]
    items = []
    for it in sorted(run["items"], key=lambda x: x.get("seq", 0)):
        raw = None
        try:
            if it["kind"] == "snapshot":
                b = Path(it["path"]).read_bytes()
                if hashlib.sha256(b).hexdigest() == it["sha256"]:
                    raw = b
                else:
                    problems.append(f"快照 {it['path']} 已被覆盖（与清单 sha 不符），需到 git 历史找回")
            elif not it.get("stored"):
                problems.append(f"{it['kind']}:{it['key']} 当时未存入 cas")
            else:
                raw = cas.get(it["sha256"])
        except (OSError, cas.CasCorrupt) as e:
            problems.append(f"{it['kind']}:{it['key']} 还原失败：{type(e).__name__}: {e}")
        items.append({"seq": it.get("seq"), "kind": it["kind"], "key": it["key"], "sha256": it["sha256"], "raw": raw})
    return {"complete": not problems, "problems": problems, "items": items}
