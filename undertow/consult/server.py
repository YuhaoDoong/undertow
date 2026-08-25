"""本地只读咨询 HTTP API（标准库 http.server，零依赖）。

把 undertow 的"咨询上下文包"暴露成一个**仅绑定本机、只读**的 HTTP 端点，方便用户把
自己的 AI（GPT/Gemini/本地模型…）接进来：AI 拿确定性上下文包 → 喂给它自己的 LLM 给意见。

端点（全部 GET，只读）：
  GET /                    → 使用说明 + 端点清单（JSON）
  GET /health             → {"ok": true}
  GET /positions          → 当前持仓评价（PortfolioReview 简报 JSON）
  GET /consult?q=...       → 完整咨询包（含 prompt 字段）
  GET /consult?q=...&pre_trade=SPEC → 附带开仓前问诊
  GET /prompt?q=...        → 只返回可投喂 LLM 的 prompt 文本

**安全**：默认只绑 127.0.0.1；**没有任何写/下单端点**；账户数据只在本机内存流转、不落公开仓库。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_INDEX = {
    "service": "undertow.consult",
    "readonly": True,
    "note": "只读咨询 API；无下单端点。数字均由 undertow 确定性引擎算好，接入的 AI 只解读、不臆算。",
    "endpoints": {
        "GET /health": "存活检查",
        "GET /positions": "当前持仓评价简报",
        "GET /consult?q=问题[&pre_trade=SPEC]": "完整咨询包（含可投喂 LLM 的 prompt）",
        "GET /prompt?q=问题": "只取 prompt 文本",
    },
}


def make_handler(build_packet, build_positions):
    """build_packet(question, pre_trade)->dict; build_positions()->dict。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "undertow-consult/1.0"

        def _send(self, code, obj, *, text=False):
            body = (obj if text else json.dumps(obj, ensure_ascii=False, indent=2)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",
                             "text/plain; charset=utf-8" if text else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            question = (q.get("q", [""])[0] or "").strip()
            pre_trade = (q.get("pre_trade", [None])[0] or None)
            try:
                if u.path in ("/", "/index"):
                    return self._send(200, _INDEX)
                if u.path == "/health":
                    return self._send(200, {"ok": True})
                if u.path == "/positions":
                    return self._send(200, build_positions())
                if u.path == "/consult":
                    return self._send(200, build_packet(question, pre_trade))
                if u.path == "/prompt":
                    return self._send(200, build_packet(question, pre_trade)["prompt"], text=True)
                return self._send(404, {"error": "unknown endpoint", "see": "/"})
            except Exception as e:  # noqa: BLE001 —— 不让单次请求崩掉服务
                return self._send(500, {"error": str(e)[:300]})

        def do_POST(self):
            # 只读服务：拒绝所有写方法（防误用成下单通道）
            return self._send(405, {"error": "只读服务，不接受 POST/写操作；下单请在券商端自行完成。"})

        def log_message(self, fmt, *a):
            pass  # 静默，避免把账户查询打进终端日志

    return Handler


def serve(build_packet, build_positions, *, host="127.0.0.1", port=8787):
    handler = make_handler(build_packet, build_positions)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
