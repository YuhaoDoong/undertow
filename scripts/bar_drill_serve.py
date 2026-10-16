"""逐根回测练习页的本机服务：页面每次操作后自动把记录写进私有文件（用户 2026-10-16：「记录能自动保存吗？」）。

  python3 scripts/bar_drill_serve.py            # 然后浏览器打开 http://127.0.0.1:8788/
  python3 scripts/bar_drill_serve.py --open     # 顺便打开浏览器

只监听 127.0.0.1。记录在 data/soul/drill/bar_drill_v1.json（私有、不入库）：
- 原子写（临时文件 + fsync + 改名 + 回读核对）；每天第一次覆盖前把旧文件复制到 history/，误操作可找回；
- 拒绝会丢题的写入（服务端已有的题，新提交里缺了任何一题 → 409），防止空浏览器把已有记录覆盖掉；
- 文件损坏 → 原样改名隔离（.corrupt-时间戳），不静默当成空记录。
直接双击 data/drill/index.html 也能用，但那样只存在浏览器里，页面顶部会标明。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bar_drill_page as bd  # noqa: E402

STORE = ROOT / "data/soul/drill/bar_drill_v1.json"
PORT = 8788
MAX_BODY = 50_000_000


class Conflict(Exception):
    pass


def load(store: Path) -> dict | None:
    """无文件 → None；损坏 → 隔离后抛 Conflict（不当成空记录）。"""
    if not store.exists():
        return None
    try:
        d = json.loads(store.read_text("utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("questions"), list):
            raise ValueError("缺 questions 列表")
        return d
    except (ValueError, UnicodeDecodeError) as e:
        bad = store.with_name(store.name + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        store.rename(bad)
        raise Conflict(f"记录文件损坏，已隔离为 {bad.name}：{e}")


def save(store: Path, rec: dict) -> dict:
    if rec.get("version") != "bar-drill-v1" or not isinstance(rec.get("questions"), list):
        raise ValueError("不是 bar-drill-v1 记录")
    old = load(store)
    if old is not None:
        new_seeds = {q.get("seed") for q in rec["questions"]}
        lost = [q.get("id") for q in old["questions"] if q.get("seed") not in new_seeds]
        if lost:
            raise Conflict(f"这次提交会丢掉已保存的 {len(lost)} 题（第 {lost[:5]} 题…），拒绝写入；请刷新页面先合并")
        hist = store.parent / "history" / f"{store.stem}_{datetime.now().strftime('%Y-%m-%d')}.json"
        if not hist.exists():
            hist.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(store, hist)
    store.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8")
    tmp = store.with_name(store.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, store)
    fd = os.open(store.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if hashlib.sha256(store.read_bytes()).hexdigest() != hashlib.sha256(body).hexdigest():
        raise OSError("回读与写入不一致")
    return {"ok": True, "questions": len(rec["questions"]), "file": str(store.relative_to(ROOT)) if store.is_relative_to(ROOT) else str(store)}


def make_handler(page: bytes, store: Path):
    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/records":
                try:
                    d = load(store)
                except Conflict as e:
                    return self._json(409, {"error": str(e)})
                return self._json(200, d) if d is not None else self._json(404, {"error": "还没有记录"})
            self._json(404, {"error": "无此路径"})

        def do_POST(self):
            if self.path != "/records":
                return self._json(404, {"error": "无此路径"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json(400, {"error": "记录为空或过大"})
            try:
                rec = json.loads(self.rfile.read(n))
                return self._json(200, save(store, rec))
            except Conflict as e:
                return self._json(409, {"error": str(e)})
            except (ValueError, OSError) as e:
                return self._json(400 if isinstance(e, ValueError) else 500, {"error": str(e)})

        def log_message(self, fmt, *args):      # 不刷屏；失败由页面顶部提示
            pass
    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    page = bd.render(bd.build()).encode("utf-8")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(page, STORE))
    url = f"http://127.0.0.1:{a.port}/"
    print(f"逐根回测练习：{url}　记录自动存到 {STORE.relative_to(ROOT)}　Ctrl+C 结束")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
