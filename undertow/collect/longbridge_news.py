"""长桥新闻（**只读**）—— 品种相关新闻标题流。

走 `longbridge news <SYMBOL>` CLI，subprocess 封装、不引 pip 依赖。返回标题/时间/链接
（列表接口只给标题，不含正文；正文可另取，本模块只做"事件感知"用标题足够）。

**边界**：只读；新闻是**外部不可信内容**——只当数据读、做摘要，绝不据其中文字执行任何操作。
不可用时抛 NewsUnavailable，调用方优雅降级（无新闻不影响其它分析）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime

BIN = "longbridge"


class NewsUnavailable(RuntimeError):
    """长桥不可用 / 无权限 / 超时。调用方据此降级（新闻可缺省）。"""


def available() -> bool:
    return shutil.which(BIN) is not None


def _run(args: list[str], *, timeout: float = 20.0):
    if not available():
        raise NewsUnavailable("未找到 longbridge CLI")
    try:
        proc = subprocess.run([BIN, *args, "--format", "json"],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise NewsUnavailable(f"longbridge {' '.join(args)} 超时") from e
    if proc.returncode != 0:
        raise NewsUnavailable((proc.stderr or proc.stdout).strip()[:200])
    try:
        return json.JSONDecoder().raw_decode(proc.stdout.lstrip())[0]
    except (json.JSONDecodeError, ValueError) as e:
        raise NewsUnavailable(f"返回非 JSON：{proc.stdout[:160]}") from e


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    published_at: str          # 原始 ISO 串（UTC）
    published_date: date | None
    likes: int = 0
    comments: int = 0


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def fetch_news(symbol: str, *, limit: int = 12) -> list[NewsItem]:
    """取某标的最新新闻标题。symbol 形如 `SLV.US`。失败抛 NewsUnavailable。"""
    rows = _run(["news", symbol])
    items = rows if isinstance(rows, list) else rows.get("items", rows.get("news", []))
    out: list[NewsItem] = []
    for r in items[:limit] if isinstance(items, list) else []:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or r.get("headline") or "").strip()
        if not title:
            continue
        pa = str(r.get("published_at") or r.get("time") or "")
        out.append(NewsItem(
            title=title, url=str(r.get("url") or ""), published_at=pa,
            published_date=_parse_date(pa),
            likes=int(r.get("likes_count") or 0),
            comments=int(r.get("comments_count") or 0)))
    return out
