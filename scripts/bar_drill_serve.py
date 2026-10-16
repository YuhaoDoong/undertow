"""逐根回测练习页的本机服务：页面每次操作后自动把记录写进私有文件（用户 2026-10-16：「记录能自动保存吗？」）。

  python3 scripts/bar_drill_serve.py            # 然后浏览器打开 http://127.0.0.1:8788/
  python3 scripts/bar_drill_serve.py --open     # 顺便打开浏览器
  python3 scripts/bar_drill_serve.py --recovery-status          # 记录损坏后：看隔离件与恢复状态
  python3 scripts/bar_drill_serve.py --recover-from <备份文件>   # 用备份（history/ 下按天副本或导出件）恢复
  python3 scripts/bar_drill_serve.py --recovery-ack             # 确认不恢复，下次保存从空记录开始

记录在 data/soul/drill/bar_drill_v1.json（私有、不入库）。Codex 053 审查后的保护（原版只按题目种子防丢题）：
- **同题不许倒退**：服务端记录带 revision，页面提交时带 base_revision（CAS），落后就 409；即使版本号对上，
  只要任何一题的进度（cursor/max_cursor）变小、done 被撤销、动作/成交/标注不是旧记录的延续，也 409、原件不动。
  原版的漏洞：同 seed 的旧快照（cursor 3、空成交）能覆盖已存的新进度（cursor 30、1 笔成交）——种子还在，所以没被拦。
- **整个「读→校验→备份→写」在锁内**（进程内线程锁 + 文件锁），临时文件名唯一（mkstemp），原子改名 + 回读核对。
  原版 ThreadingHTTPServer 下多个请求共用固定的 .tmp 且无锁。
- **损坏 → 隔离 + 恢复未决**：原样改名隔离（.corrupt-时间戳），同时落一个 .recovery-pending.json 标记；标记在时
  读写一律 409，直到用户用上面两条命令之一明确处理。原版隔离后原路径空了，下一次空提交就被当成首次记录建了空账。
- **页面版本绑定**：提交必须带页面的撮合引擎 sha256 与输入数据 sha256，与本服务启动时生成的页面不一致 → 409
  （旧标签页刷新后才能写）。每题创建时另记 engine/data_sha256，旧题原样保留、不按新引擎重判。
- **127.0.0.1 不是鉴权**：Host 头必须是本机名（防 DNS 重绑定，与 consult 服务共用判定）；/records 读写都要页面里
  注入的随机写入凭据（X-Drill-Token）；写入要求 Origin 与 Host 同源、Content-Type 为 application/json、
  有 Content-Length 且不超上限；JSON 结构逐项校验；连接超时 30 秒。
直接双击 data/drill/index.html 也能用，但那样只存在浏览器里，页面顶部会标明。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bar_drill_page as bd  # noqa: E402
from undertow.consult.server import host_header_is_loopback  # noqa: E402

STORE = ROOT / "data/soul/drill/bar_drill_v1.json"
PORT = 8788
VERSION = "bar-drill-v1"
MAX_BODY = 20_000_000
MAX_QUESTIONS = 20_000
MAX_NOTE = 20_000
REQUEST_TIMEOUT = 30
SERVER_KEYS = {"revision", "saved_at"}                      # 服务端写，页面带回来也会被覆盖
CLIENT_KEYS = {"version", "questions", "cur", "settings", "updated_at", "base_revision",
               "page_engine_sha256", "data_sha256", "conflict_copies"}
APPEND_ONLY = ("actions", "trades", "marks")                 # 只许在末尾追加，不许删改
IDENTITY = ("sym", "i0", "mirror", "left", "right", "engine", "data_sha256")   # 题目身份，存下后不许改
TOKEN_HEADER = "X-Drill-Token"


class Conflict(Exception):
    """409：拒绝写入（版本落后 / 进度倒退 / 页面过期 / 恢复未决），原件不动。"""

    def __init__(self, msg: str, **extra):
        super().__init__(msg)
        self.extra = extra


class RecoveryPending(Conflict):
    pass


_TLOCK = threading.Lock()


def _marker(store: Path) -> Path:
    return store.with_name(store.name + ".recovery-pending.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def locked(store: Path):
    """整笔事务的锁：线程锁挡同进程并发，flock 挡另一个服务进程。"""
    store.parent.mkdir(parents=True, exist_ok=True)
    with _TLOCK, open(store.with_name(store.name + ".lock"), "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def write_atomic(path: Path, body: bytes) -> None:
    """唯一临时文件 + fsync + 改名 + 目录 fsync + 回读核对。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(body).hexdigest():
        raise OSError(f"{path.name} 回读与写入不一致")


def _recovery_msg(info: dict) -> str:
    return (f"记录文件损坏，已原样隔离为 {info.get('quarantined', '?')}（{info.get('error', '')}）。为免空记录静默顶替历史，"
            "自动保存已暂停，在你明确处理前不会写入。处理办法（在终端里运行，都不会删除隔离件）："
            "① 有备份（data/soul/drill/history/ 下的按天副本或导出件）：python3 scripts/bar_drill_serve.py --recover-from <文件>；"
            "② 确认不恢复、从空记录重新开始：python3 scripts/bar_drill_serve.py --recovery-ack。处理后在页面点「重新读取」（服务不用重启）。")


def _load_unlocked(store: Path) -> dict | None:
    """无文件 → None；恢复未决或损坏 → RecoveryPending（损坏时先落标记再隔离，不当成空记录）。"""
    mk = _marker(store)
    if mk.exists():
        try:
            info = json.loads(mk.read_text("utf-8"))
        except (ValueError, UnicodeDecodeError):
            info = {"error": "恢复标记本身不可读"}
        raise RecoveryPending(_recovery_msg(info), recovery_pending=True)
    if not store.exists():
        return None
    try:
        d = json.loads(store.read_text("utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("questions"), list):
            raise ValueError("缺 questions 列表")
        return d
    except (ValueError, UnicodeDecodeError) as e:
        bad = store.with_name(store.name + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        info = {"quarantined": bad.name, "error": str(e)[:300], "at": _now()}
        write_atomic(mk, json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"))   # 标记先落盘，再隔离
        store.rename(bad)
        raise RecoveryPending(_recovery_msg(info), recovery_pending=True)


def load(store: Path) -> dict | None:
    with locked(store):
        return _load_unlocked(store)


def _int(x) -> bool:
    return type(x) is int


def validate(rec) -> None:
    """页面提交的结构校验（宽松接受旧版题目的多余字段，只卡住会影响防回退判断的那些）。"""
    if not isinstance(rec, dict):
        raise ValueError("记录不是 JSON 对象")
    extra = set(rec) - CLIENT_KEYS - SERVER_KEYS
    if extra:
        raise ValueError(f"记录含未知字段 {sorted(extra)[:5]}")
    if rec.get("version") != VERSION:
        raise ValueError(f"不是 {VERSION} 记录")
    if not (_int(rec.get("base_revision")) and rec["base_revision"] >= 0):
        raise ValueError("缺 base_revision（页面过旧？请刷新）")
    qs = rec.get("questions")
    if not isinstance(qs, list) or len(qs) > MAX_QUESTIONS:
        raise ValueError("questions 不是列表或过多")
    seeds = set()
    for k, q in enumerate(qs):
        if not isinstance(q, dict):
            raise ValueError(f"第 {k} 项题目不是对象")
        if not (_int(q.get("seed")) and 0 <= q["seed"] < 2 ** 32) or not _int(q.get("id")) or not _int(q.get("cursor")):
            raise ValueError(f"第 {k} 项题目缺 seed/id/cursor 或类型不对")
        if q["seed"] in seeds:
            raise ValueError(f"种子 {q['seed']} 重复")
        seeds.add(q["seed"])
        for f in APPEND_ONLY:
            if not isinstance(q.get(f, []), list):
                raise ValueError(f"第 {q['id']} 题的 {f} 不是列表")
        if "done" in q and not isinstance(q["done"], bool):
            raise ValueError(f"第 {q['id']} 题的 done 不是布尔")
        if "note" in q and not (isinstance(q["note"], str) and len(q["note"]) <= MAX_NOTE):
            raise ValueError(f"第 {q['id']} 题的备注不是文本或过长")
    if not isinstance(rec.get("conflict_copies", []), list):
        raise ValueError("conflict_copies 不是列表")
    if not isinstance(rec.get("settings", {}), dict):
        raise ValueError("settings 不是对象")


def check_progress(old: dict, new: dict) -> None:
    """同题不许倒退：题不能少、进度不减、done 不撤、动作/成交/标注只许追加、题目身份不变；冲突副本只许追加。"""
    by_seed = {q["seed"]: q for q in new["questions"]}
    bad = []
    for oq in old.get("questions", []):
        if not isinstance(oq, dict):
            continue
        tag = f"第 {oq.get('id')} 题"
        nq = by_seed.get(oq.get("seed"))
        if nq is None:
            bad.append(f"{tag}被删掉")
            continue
        for k in ("cursor", "max_cursor"):
            if _int(oq.get(k)) and not (_int(nq.get(k)) and nq[k] >= oq[k]):
                bad.append(f"{tag}的 {k} 从 {oq[k]} 退到 {nq.get(k)}")
        if oq.get("done") is True and nq.get("done") is not True:
            bad.append(f"{tag}已结束却被改回未结束")
        for k in APPEND_ONLY:
            a, b = oq.get(k) or [], nq.get(k) or []
            if len(b) < len(a) or b[:len(a)] != a:
                bad.append(f"{tag}的 {k} 不是已存记录的延续（{len(a)} → {len(b)} 条）")
        for k in IDENTITY:
            if k in oq and oq[k] != nq.get(k):
                bad.append(f"{tag}的 {k} 被改")
    a, b = old.get("conflict_copies") or [], new.get("conflict_copies") or []
    if len(b) < len(a) or b[:len(a)] != a:
        bad.append("冲突副本被删改")
    if bad:
        raise Conflict("这次提交会让已保存的记录倒退，拒绝写入（原件未动）：" + "；".join(bad[:5]) +
                       (f" 等 {len(bad)} 处" if len(bad) > 5 else "") + "。请点「重新载入并合并」",
                       conflict=True, regress=bad[:20])


def save(store: Path, rec: dict, page_meta: dict | None = None) -> dict:
    validate(rec)
    if page_meta is not None and (rec.get("page_engine_sha256") != page_meta["engine_sha256"]
                                  or rec.get("data_sha256") != page_meta["data_sha256"]):
        raise Conflict("页面版本与本机服务不一致（撮合引擎或数据已更新），这个标签页不能再写；请刷新页面", stale_page=True)
    with locked(store):
        old = _load_unlocked(store)
        cur = 0 if old is None else old.get("revision", 0)
        if not _int(cur):
            raise OSError("服务端记录的 revision 不是整数")
        if rec["base_revision"] != cur:
            raise Conflict(f"记录已被别的页面更新（服务端第 {cur} 版，本页基于第 {rec['base_revision']} 版），拒绝覆盖；"
                           "请点「重新载入并合并」", conflict=True, revision=cur)
        if old is not None:
            check_progress(old, rec)
            hist = store.parent / "history" / f"{store.stem}_{datetime.now().strftime('%Y-%m-%d')}.json"
            if not hist.exists():
                hist.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(store, hist)
        out = {k: v for k, v in rec.items() if k != "base_revision"}
        out["revision"], out["saved_at"] = cur + 1, _now()
        write_atomic(store, json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"))
    return {"ok": True, "revision": cur + 1, "questions": len(rec["questions"]),
            "file": str(store.relative_to(ROOT)) if store.is_relative_to(ROOT) else str(store)}


def _log_recovery(store: Path, entry: dict) -> None:
    with open(store.parent / "recovery_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **entry}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def recovery_status(store: Path) -> dict:
    mk = _marker(store)
    return {"pending": mk.exists(), "marker": mk.read_text("utf-8") if mk.exists() else None,
            "store_exists": store.exists(), "quarantined": sorted(p.name for p in store.parent.glob(store.name + ".corrupt-*")),
            "history": sorted(p.name for p in (store.parent / "history").glob("*.json"))}


def recovery_ack(store: Path) -> str:
    """用户明确确认不恢复：去掉标记（隔离件不动）。若此时原路径已有文件，它必须可读。"""
    with locked(store):
        mk = _marker(store)
        if not mk.exists():
            return "没有未决的恢复，什么也没做"
        if store.exists():
            d = json.loads(store.read_text("utf-8"))
            if not isinstance(d, dict) or not isinstance(d.get("questions"), list):
                raise ValueError(f"{store.name} 存在但不是有效记录，先移走它")
        _log_recovery(store, {"action": "ack", "marker": mk.read_text("utf-8"), "store_exists": store.exists()})
        mk.unlink()
    return "已确认：恢复标记已清除，隔离件原样保留；" + ("沿用现有记录文件" if store.exists() else "下次保存从空记录开始")


def recover_from(store: Path, src: Path) -> str:
    """用备份恢复：只在原路径没有文件时写入（不覆盖任何东西），写完清标记。"""
    raw = src.read_bytes()
    d = json.loads(raw)
    if not isinstance(d, dict) or d.get("version") != VERSION or not isinstance(d.get("questions"), list):
        raise ValueError(f"{src} 不是 {VERSION} 记录")
    with locked(store):
        if store.exists():
            raise ValueError(f"{store} 已存在，不覆盖；如确要用备份替换，先手动把它移走")
        rec = {k: v for k, v in d.items() if k in CLIENT_KEYS | SERVER_KEYS and k != "base_revision"}
        rec["revision"] = (d.get("revision") if _int(d.get("revision")) else 0) + 1
        rec["saved_at"] = _now()
        write_atomic(store, json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))
        mk = _marker(store)
        _log_recovery(store, {"action": "recover_from", "src": str(src), "src_sha256": hashlib.sha256(raw).hexdigest(),
                              "questions": len(rec["questions"]), "had_marker": mk.exists()})
        mk.unlink(missing_ok=True)
    return f"已用 {src.name} 恢复 {len(rec['questions'])} 题（第 {rec['revision']} 版）；隔离件原样保留"


def make_handler(page: bytes, store: Path, *, token: str, page_meta: dict):
    """page 必须恰好含一个 <head>：在其后注入本次启动的写入凭据。"""
    assert page.count(b"<head>") == 1, "页面模板应恰好含一个 <head>"
    assert token and all(c.isalnum() or c in "-_" for c in token)
    page = page.replace(b"<head>", b'<head><meta name="drill-token" content="' + token.encode() + b'">', 1)

    class H(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, write: bool) -> tuple[int, str] | None:
            """/records 读写的前置检查；返回 (状态码, 原因) 表示拒绝。"""
            host = (self.headers.get("Host") or "").strip()
            if not host_header_is_loopback(host):
                return 403, "Host 头不是本机地址（防 DNS 重绑定）"
            sfs = self.headers.get("Sec-Fetch-Site")
            if sfs is not None and sfs != "same-origin":
                return 403, f"不是同源请求（Sec-Fetch-Site={sfs}）"
            origin = self.headers.get("Origin")
            if (write or origin is not None) and (origin or "").lower() != "http://" + host.lower():
                return 403, "写入必须来自本页（Origin 与 Host 不同源或缺失）"
            if not secrets.compare_digest((self.headers.get(TOKEN_HEADER) or "").encode(), token.encode()):
                return 403, "缺少或错误的写入凭据（请从 bar_drill_serve.py 打开的页面操作；服务重启过就刷新页面）"
            if write and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return 415, "Content-Type 必须是 application/json"
            return None

        def do_GET(self):
            if not host_header_is_loopback(self.headers.get("Host")):
                return self._json(403, {"error": "Host 头不是本机地址（防 DNS 重绑定）"})
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/records":
                if (g := self._guard(write=False)):
                    return self._json(g[0], {"error": g[1]})
                try:
                    d = load(store)
                except Conflict as e:
                    return self._json(409, {"error": str(e), **e.extra})
                return self._json(200, d) if d is not None else self._json(404, {"error": "还没有记录", "revision": 0})
            self._json(404, {"error": "无此路径"})

        def do_POST(self):
            if self.path != "/records":
                return self._json(404, {"error": "无此路径"})
            if (g := self._guard(write=True)):
                self.close_connection = True
                return self._json(g[0], {"error": g[1]})
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self._json(411, {"error": "不接受分块传输，请带 Content-Length"})
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self.close_connection = True
                return self._json(411, {"error": "缺 Content-Length"})
            if n <= 0:
                return self._json(400, {"error": "记录为空"})
            if n > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": f"记录超过 {MAX_BODY} 字节"})
            try:
                rec = json.loads(self.rfile.read(n).decode("utf-8"))
                return self._json(200, save(store, rec, page_meta))
            except Conflict as e:
                return self._json(409, {"error": str(e), **e.extra})
            except (ValueError, UnicodeDecodeError) as e:      # JSONDecodeError 是 ValueError
                return self._json(400, {"error": str(e)[:500]})
            except OSError as e:
                return self._json(500, {"error": f"写文件失败：{e}"})

        def log_message(self, fmt, *args):      # 不刷屏；失败由页面顶部提示
            pass
    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--store", default=str(STORE), help="记录文件（测试用；默认 data/soul/drill/bar_drill_v1.json）")
    ap.add_argument("--recovery-status", action="store_true")
    ap.add_argument("--recover-from", metavar="FILE")
    ap.add_argument("--recovery-ack", action="store_true")
    a = ap.parse_args()
    store = Path(a.store).resolve()
    if a.recovery_status:
        print(json.dumps(recovery_status(store), ensure_ascii=False, indent=1))
        return 0
    if a.recover_from or a.recovery_ack:
        try:
            print(recover_from(store, Path(a.recover_from)) if a.recover_from else recovery_ack(store))
        except (ValueError, OSError) as e:
            print(f"恢复失败：{e}", file=sys.stderr)
            return 2
        return 0
    pool = bd.build()
    page = bd.render(pool).encode("utf-8")          # render 把撮合引擎 sha256 写进 pool["meta"]
    meta = {"engine_sha256": pool["meta"]["engine_sha256"], "data_sha256": pool["meta"]["sha256"]}
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(page, store, token=secrets.token_urlsafe(24), page_meta=meta))
    url = f"http://127.0.0.1:{a.port}/"
    shown = store.relative_to(ROOT) if store.is_relative_to(ROOT) else store
    print(f"逐根回测练习：{url}　记录自动存到 {shown}　Ctrl+C 结束（服务重启后已打开的页面要刷新）")
    if _marker(store).exists():
        print("⚠ 记录处于「恢复未决」状态，页面不会写入；先运行 --recovery-status 查看", file=sys.stderr)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
