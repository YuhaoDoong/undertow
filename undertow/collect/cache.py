"""极简文件缓存：把原始 API 响应按 key 落盘，带 TTL。

目的：COT 周报每周才更新一次，没必要每次分析都打 API。
也方便离线复跑 / 调试分析逻辑时不依赖网络。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from undertow.core.config import CACHE_DIR


class FileCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or CACHE_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.root / f"{safe}.json"

    def get(self, key: str, ttl_seconds: float | None) -> Any | None:
        """命中且未过期返回数据，否则 None。ttl_seconds=None 表示永不过期。"""
        path = self._path(key)
        if not path.exists():
            return None
        if ttl_seconds is not None and (time.time() - path.stat().st_mtime) > ttl_seconds:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload["data"]
        except (json.JSONDecodeError, KeyError):
            return None

    def set(self, key: str, data: Any) -> None:
        path = self._path(key)
        path.write_text(
            json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
