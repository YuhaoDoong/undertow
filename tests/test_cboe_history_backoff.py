"""CBOE 日线：429 有界重试、跨进程重抓标记、重抓失败沿用近期缓存（Codex 032 O3；10/1 早上 429 → 影子账盘前捕获整批失败）。"""
from datetime import date
from types import SimpleNamespace as N

import pytest

from undertow.collect import cboe_history as ch
from undertow.collect.base import DataSourceError

INST = N(key="t", price=N(symbol="TST"))


def payload(last):
    return {"data": [{"date": last, "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1}]}


class Cache:
    def __init__(self, store=None, fresh=True):
        self.store, self.fresh, self.sets = dict(store or {}), fresh, []

    def get(self, key, ttl):
        if key not in self.store:
            return None
        return self.store[key] if (self.fresh or ttl is None) else None

    def set(self, key, data):
        self.store[key] = data; self.sets.append(key)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ch, "_expected_last_bar", lambda: date(2026, 9, 30))
    monkeypatch.setattr(ch, "RETRY_429_SLEEPS", (0.0, 0.0))
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)
    ch._STALE_REFETCHED.clear(); ch._STALE_WARNED.clear()


def test_429_retried_then_success(monkeypatch):
    calls = iter([DataSourceError("HTTP 429 调用失败"), payload("2026-09-30")])

    def get(url):
        x = next(calls)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(ch, "http_get_json", get)
    s = ch.CboeHistorySource(Cache()).fetch_series(INST)
    assert s.dates == [date(2026, 9, 30)]


def test_stale_cache_refetch_fails_falls_back_and_marks_cross_process(monkeypatch):
    n = {"calls": 0}

    def get(url):
        n["calls"] += 1
        raise DataSourceError("HTTP 429 调用失败")
    monkeypatch.setattr(ch, "http_get_json", get)
    c = Cache({"cboehist_TST": payload("2026-09-29")})
    s = ch.CboeHistorySource(c).fetch_series(INST)                      # 落后一天 → 重抓 → 429×3 → 沿用缓存
    assert s.dates == [date(2026, 9, 29)] and n["calls"] == 3 and "cboehist_refetch_TST" in c.store
    ch._STALE_REFETCHED.clear()                                          # 模拟另一个进程：标记仍在 → 不再重抓
    ch.CboeHistorySource(c).fetch_series(INST)
    assert n["calls"] == 3


def test_no_cache_or_week_old_cache_still_raises(monkeypatch):
    monkeypatch.setattr(ch, "http_get_json", lambda url: (_ for _ in ()).throw(DataSourceError("HTTP 429")))
    with pytest.raises(DataSourceError):
        ch.CboeHistorySource(Cache()).fetch_series(INST)
    old = Cache({"cboehist_TST": payload("2026-09-10")}, fresh=False)    # 过了 TTL 且落后 20 天
    with pytest.raises(DataSourceError):
        ch.CboeHistorySource(old).fetch_series(INST)


def test_non_429_error_not_retried(monkeypatch):
    n = {"calls": 0}

    def get(url):
        n["calls"] += 1
        raise DataSourceError("HTTP 500 调用失败")
    monkeypatch.setattr(ch, "http_get_json", get)
    with pytest.raises(DataSourceError):
        ch.CboeHistorySource(Cache()).fetch_series(INST)
    assert n["calls"] == 1


def test_premarket_gaps_and_hook_wiring():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from scripts.shadow_premarket_gaps import gaps
    rows = {"gold": [{"session": "2026-10-01", "identity": {"status": "certified"}}],
            "silver": [{"session": "2026-09-30", "identity": {"status": "certified"}}],
            "wti": [{"session": "2026-10-01", "identity": {"status": "provisional"}}], "qqq": []}
    assert gaps("2026-10-01", rows) == (["silver", "qqq"], ["wti"])
    h = (root / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert h.index("premarket_fill() {") < h.index("      premarket_fill\n")              # 定义先于调用
    assert "ET_MIN >= 565" in h and "shadow capture ${=MISS}" in h and "shadow settle ${=MISS} ${=PROV}" in h
