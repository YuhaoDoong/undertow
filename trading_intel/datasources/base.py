"""数据源抽象基类 + 轻量 HTTP 工具（仅标准库）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any


class DataSourceError(RuntimeError):
    """数据源层统一异常，便于上层捕获区分网络/解析错误。"""


def http_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET 一个 JSON 接口。封装编码/超时/错误，返回已解析对象。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "trading_intel/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise DataSourceError(f"HTTP {e.code} 调用失败: {url}\n{e.read()[:500]!r}") from e
    except urllib.error.URLError as e:
        raise DataSourceError(f"网络错误: {e.reason} ({url})") from e
    except json.JSONDecodeError as e:
        raise DataSourceError(f"返回非合法 JSON: {url}") from e


class DataSource(ABC):
    """所有数据源的统一接口。"""

    name: str = "base"

    @abstractmethod
    def fetch_history(self, instrument, *, lookback: int, use_cache: bool = True):
        """拉取某品种的历史序列，返回按时间升序排列的语义模型列表。"""
        raise NotImplementedError
