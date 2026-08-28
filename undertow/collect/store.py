"""快照仓库：把数据源的【原始 payload 全字段】按日落盘，永久留存。

为什么需要它（区别于 cache.py）:
  - cache.py 是带 TTL 的临时缓存，会被新数据覆盖，目的是少打 API。
  - store.py 是【永久档案】：CBOE/Yahoo 等不提供期权历史，今天的期权链
    一旦不存，明天就永远拿不回来。期权 OI/IV/volume 的日级演变正是 flow 层
    （近月大单异动、ΔOI/ΔIV）唯一的数据来源——必须自己每天落盘攒。
  - 我们存【原始 payload】而非解析后的子集：尽可能多留字段（bid/ask/theta/vega…），
    将来分析需要新字段时不必重新采集（也采集不到历史）。

落盘布局（每个文件 = 某标的某日的一份完整原始 payload）:
    data/snapshots/{kind}/{symbol}/{YYYY-MM-DD}.json
        kind:   数据种类，目前主要是 "options"（COT/price 可从官方 API 回填，非必需）
        record: {captured_at, kind, symbol, date, payload}

这些文件【纳入 git】——push 到远端即等于备份这份不可再生的历史。
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from undertow.core.config import DATA_DIR

SNAPSHOT_DIR = DATA_DIR / "snapshots"
# gzip 落盘：原始 payload 体量大（一条期权链 ~3MB），gzip 后 ~1/6，
# 无损（仍是完整原始全字段），便于纳入 git 备份而不撑爆仓库。
_SUFFIX = ".json.gz"


class SnapshotStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or SNAPSHOT_DIR

    def _dir(self, kind: str, symbol: str) -> Path:
        return self.root / kind / symbol

    def _path(self, kind: str, symbol: str, on_date: date) -> Path:
        return self._dir(kind, symbol) / f"{on_date.isoformat()}{_SUFFIX}"

    def save(self, kind: str, symbol: str, payload: Any, *, on_date: date,
             captured_at: float | None = None) -> Path:
        """落盘某标的某日的原始 payload（gzip 无损压缩）。同日重复保存会覆盖
        （保留当日最新/最全的一份，日内 volume 累积，临近收盘那次最完整）。返回文件路径。"""
        d = self._dir(kind, symbol)
        d.mkdir(parents=True, exist_ok=True)
        path = self._path(kind, symbol, on_date)
        record = {
            "captured_at": captured_at if captured_at is not None else time.time(),
            "kind": kind,
            "symbol": symbol,
            "date": on_date.isoformat(),
            "payload": payload,
        }
        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        # ⚠️ 原子写：先写临时文件 → 验证能完整读回 → 才 rename 覆盖目标。
        # 旧写法直接 gzip.open(path,"wb")，中途崩溃/磁盘满会留下**半截的坏文件**，
        # 而 load() 又把坏文件静默折成 None —— 一份不可再生的期权链就这样没了，
        # 且没有任何人知道（codex review 2026-08-28）。
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        try:
            with gzip.open(tmp, "wb") as f:
                f.write(blob)
            with gzip.open(tmp, "rb") as f:          # 回读验证，坏就不覆盖
                json.loads(f.read().decode("utf-8"))
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return path

    def dates(self, kind: str, symbol: str) -> list[date]:
        """该标的已落盘的所有日期（升序）。"""
        d = self._dir(kind, symbol)
        if not d.exists():
            return []
        out: list[date] = []
        for p in d.glob(f"*{_SUFFIX}"):
            stem = p.name[: -len(_SUFFIX)]
            try:
                out.append(date.fromisoformat(stem))
            except ValueError:
                continue
        return sorted(out)

    def load(self, kind: str, symbol: str, on_date: date, *,
             quarantine: bool = True) -> Any | None:
        """读回某日的原始 payload；文件不存在返回 None。

        ⚠️ **损坏 ≠ 不存在**。旧写法把二者都折成 None，后果：
          · 上层的"文件存在即算齐全"判据会把损坏文件当成有效快照；
          · 下一次 save 直接覆盖它，损坏的历史被静默抹掉；
          · 一份不可再生的期权链就这样没了，没有任何人知道。
        现在损坏文件会被【隔离】为 .corrupt-N 并在 stderr 显式报告，
        再返回 None —— 调用方看到的仍是"没有数据"，但证据留下了、不会被覆盖。
        """
        path = self._path(kind, symbol, on_date)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rb") as f:
                rec = json.loads(f.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, EOFError, UnicodeDecodeError) as e:
            if quarantine:
                n = 1
                while (q := path.with_suffix(path.suffix + f".corrupt-{n}")).exists():
                    n += 1
                try:
                    path.rename(q)
                    print(f"[严重] 快照损坏已隔离：{path.name} → {q.name}"
                          f"（{type(e).__name__}）—— 该日数据不可再生，请检查",
                          file=sys.stderr)
                except OSError:
                    print(f"[严重] 快照损坏且隔离失败：{path}（{type(e).__name__}）",
                          file=sys.stderr)
            return None
        if not isinstance(rec, dict) or "payload" not in rec:
            print(f"[严重] 快照结构异常（缺 payload 字段）：{path.name}", file=sys.stderr)
            return None
        return rec.get("payload")

    def latest(self, kind: str, symbol: str) -> tuple[date, Any] | None:
        ds = self.dates(kind, symbol)
        if not ds:
            return None
        return ds[-1], self.load(kind, symbol, ds[-1])

    def latest_two(self, kind: str, symbol: str) -> list[tuple[date, Any]]:
        """最近两日的 (日期, payload)，按日期升序。可能 0/1/2 个。"""
        ds = self.dates(kind, symbol)[-2:]
        return [(d, self.load(kind, symbol, d)) for d in ds]
