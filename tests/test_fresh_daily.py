"""新鲜 T−1 日线（Codex 056 F0）：expected 只来自日历、按优先顺序取源不拼接、缺 H/L 不顶替、冲突拒用、ATR/语境同日。全部合成数据。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import mainwall as mw  # noqa: E402
from undertow.collect import fresh_daily as fd  # noqa: E402
from undertow.collect.base import DataSourceError  # noqa: E402
from undertow.core import market_calendar as mc  # noqa: E402
from undertow.core.models import PriceSeries  # noqa: E402


class _Inst:
    key = "silver"

    class price:
        symbol = "SLV"


def _series(last: date, n: int = 300, px: float = 50.0) -> PriceSeries:
    ds, d = [], last
    while len(ds) < n:
        if mc.is_trading_day(d) is not False and d.weekday() < 5:
            ds.append(d)
        d -= timedelta(days=1)
    ds.reverse()
    c = [px + 0.01 * i for i in range(n)]
    return PriceSeries("SLV", ds, c, [x + 0.5 for x in c], [x - 0.5 for x in c])


def _fetchers(cboe=None, yahoo=None):
    def mk(x):
        def f(inst, cache):
            if x is None:
                raise DataSourceError("未提供")
            if isinstance(x, Exception):
                raise x
            return x, {"fetched_at": 1_760_000_000.0, "raw_sha": "s"}
        return f
    return {"cboe_history": mk(cboe), "yahoo_chart": mk(yahoo)}


T = date(2026, 10, 16)          # 周五；expected = 10/15


def test_expected_from_calendar_and_primary_source_wins():
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(_series(date(2026, 10, 15)), _series(date(2026, 10, 15))))
    assert rec["status"] == "fresh" and rec["source"] == "cboe_history"
    assert rec["expected_base_date"] == rec["base_date"] == "2026-10-15"
    assert ser.dates[-1] == date(2026, 10, 15)
    assert rec["cross_check"][0]["against"] == "yahoo_chart" and rec["cross_check"][0]["ok"]


def test_monday_and_holiday_expected():
    assert fd.resolve(_Inst, date(2026, 10, 19), fetchers=_fetchers(_series(date(2026, 10, 16))))[0]["base_date"] == "2026-10-16"
    # 11/27 感恩节后（11/26 休市）→ expected 11/25
    r, _ = fd.resolve(_Inst, date(2026, 11, 27), fetchers=_fetchers(_series(date(2026, 11, 25))))
    assert r["expected_base_date"] == "2026-11-25" and r["status"] == "fresh"


def test_cboe_stale_falls_back_to_yahoo_whole_series():
    y = _series(date(2026, 10, 15), px=60.0)
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(_series(date(2026, 10, 14)), y))
    assert rec["status"] == "fresh" and rec["source"] == "yahoo_chart"
    assert ser.closes == y.closes                       # 整条序列来自同一源，不拼接
    assert rec["attempts"][0]["status"] == "stale" and rec["attempts"][0]["last_date"] == "2026-10-14"


def test_both_stale_or_error_is_typed_not_raised():
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(_series(date(2026, 10, 14)), DataSourceError("HTTP 429")))
    assert rec["status"] == "stale" and ser is None
    assert [a["status"] for a in rec["attempts"]] == ["stale", "error"]


def test_session_day_bar_is_cut_off():
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(_series(date(2026, 10, 16)), None))
    assert rec["status"] == "fresh" and rec["base_date"] == "2026-10-15"      # T 当天的半根不算


def test_conflict_between_sources_rejected():
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(_series(date(2026, 10, 15), px=50), _series(date(2026, 10, 15), px=51)))
    assert rec["status"] == "conflict" and ser is None


def test_snapshot_prev_close_only_checked_when_dated():
    s = _series(date(2026, 10, 15))
    bad = {"close": s.closes[-1] * 1.02, "date": "2026-10-15"}
    assert fd.resolve(_Inst, T, fetchers=_fetchers(s, None), snapshot_prev_close=bad)[0]["status"] == "conflict"
    undated = {"close": s.closes[-1] * 1.02, "date": "2026-10-14"}
    r, _ = fd.resolve(_Inst, T, fetchers=_fetchers(s, None), snapshot_prev_close=undated)
    assert r["status"] == "fresh" and "skipped" in r["cross_check"][-1]


def test_non_trading_day_calendar_unknown():
    assert fd.resolve(_Inst, date(2026, 10, 17), fetchers=_fetchers(None, None))[0]["status"] == "calendar_unknown"


def test_yahoo_parse_et_date_and_drops_missing_hl():
    # 10/15 13:30 UTC = 09:30 ET；10/16 00:30 UTC 落在 ET 10/15 晚 → 仍归 10/15，不会被当成 10/16
    t1 = int(datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc).timestamp())
    t2 = int(datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc).timestamp())
    payload = {"chart": {"result": [{"timestamp": [t1, t2], "indicators": {"quote": [
        {"close": [50.0, 51.0], "high": [50.5, None], "low": [49.5, 50.5]}]}}]}}
    ser = fd.parse_yahoo(payload, "SLV")
    assert ser.dates == [date(2026, 10, 14)]          # 缺 high 的 10/15 整根丢，不拿收盘顶替


def test_derive_inputs_same_asof_or_incomplete():
    s = _series(date(2026, 10, 15))
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(s, None))
    out = mw.derive_inputs(rec, ser, T)
    assert out["status"] == "fresh" and out["atr14"] > 0
    assert set(out["asof"].values()) == {"2026-10-15"}
    short = _series(date(2026, 10, 15), n=10)
    rec2, ser2 = fd.resolve(_Inst, T, fetchers=_fetchers(short, None))
    assert mw.derive_inputs(rec2, ser2, T)["status"] == "incomplete"     # 语境不足 30 根 → 不冻结


def test_identity_bound_to_same_bytes_even_if_cache_rewritten(tmp_path, monkeypatch):
    """057 R2：读到 A 之后另一个写者把缓存改成 B —— 返回的序列与 hash 必须都属于 A。"""
    import hashlib
    import json
    import time as _t
    from undertow.collect.cache import FileCache
    cache = FileCache(tmp_path)
    t1 = int(datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc).timestamp())

    def payload(c):
        return {"chart": {"result": [{"timestamp": [t1], "indicators": {"quote": [{"close": [c], "high": [c + 1], "low": [c - 1]}]}}]}}
    key = "yahoo_etf_SLV_2y"
    raw_a = json.dumps({"fetched_at": _t.time(), "data": payload(50.0)}, ensure_ascii=False).encode("utf-8")
    cache._path(key).write_bytes(raw_a)
    orig = fd.parse_yahoo

    def racing_parse(p, sym):
        cache.set(key, payload(70.0))                     # 解析期间被改写
        return orig(p, sym)
    monkeypatch.setattr(fd, "parse_yahoo", racing_parse)
    ser, meta = fd.fetch_yahoo(_Inst, cache)
    assert ser.closes == [50.0] and meta["raw_sha"] == hashlib.sha256(raw_a).hexdigest()


def test_identity_missing_is_not_fresh():
    def f(inst, cache):
        return _series(date(2026, 10, 15)), {"fetched_at": None, "raw_sha": None}
    rec, ser = fd.resolve(_Inst, T, fetchers={"cboe_history": f, "yahoo_chart": f})
    assert rec["status"] == "stale" and ser is None
    assert {a["status"] for a in rec["attempts"]} == {"identity_missing"}


def test_internal_missing_bar_in_atr_window_is_incomplete():
    s = _series(date(2026, 10, 15))
    i = s.dates.index(date(2026, 10, 13))
    gap = PriceSeries("SLV", s.dates[:i] + s.dates[i + 1:], s.closes[:i] + s.closes[i + 1:],
                      s.highs[:i] + s.highs[i + 1:], s.lows[:i] + s.lows[i + 1:])
    rec, ser = fd.resolve(_Inst, T, fetchers=_fetchers(gap, None))
    assert rec["status"] == "fresh"                       # 最后一根到了
    out = mw.derive_inputs(rec, ser, T)
    assert out["status"] == "incomplete" and "2026-10-13" in out["incomplete_reason"]


def test_context_gap_outside_atr_window_only_blanks_context():
    s = _series(date(2026, 10, 15))
    i = s.dates.index(date(2026, 8, 3))                    # ATR 窗口（最近 15 根）之外、语境窗口之内
    gap = PriceSeries("SLV", s.dates[:i] + s.dates[i + 1:], s.closes[:i] + s.closes[i + 1:],
                      s.highs[:i] + s.highs[i + 1:], s.lows[:i] + s.lows[i + 1:])
    out = mw.derive_inputs(*fd.resolve(_Inst, T, fetchers=_fetchers(gap, None)), T)
    assert out["status"] == "fresh" and out["context_status"] == "incomplete" and "atr14" not in out["context"]
