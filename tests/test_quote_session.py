"""实时报价的时段口径 —— 回归测试。

两个都是实测踩出来的：
① 2026-09-24 SLV：顶层 prev_close=60.73（9/22 收盘），夜盘段 prev_close=58.16（9/23 收盘）。
   拿夜盘价 57.95 除以 60.73 得 −4.58%，除以 58.16 才是真实的 −0.36%。
   **混用两个时段的基准会把涨跌幅放大一个数量级**，而建仓判断直接吃这个数。
② 2026-09-20 周末：夜盘段 ts=09-18T08:00（周五开盘【前】），盘后段 ts=09-18T23:59。
   旧实现按写死的优先级「夜盘>盘后>盘前」取，拿到比收盘还早 12 小时的数据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.collect.longbridge_quote import _freshest, StockQuote   # noqa: E402


def _row(last, prev, **sessions):
    r = {"last": str(last), "prev_close": str(prev)}
    r.update(sessions)
    return r


def test_change_pct_uses_same_session_base():
    """涨跌幅必须用该时段自己的 prev_close，不能用顶层的。"""
    q = StockQuote(symbol="SLV.US", last=58.16, prev_close=60.73,
                   freshest=57.95, freshest_kind="夜盘", freshest_prev=58.16)
    assert abs(q.change_pct * 100 - (-0.361)) < 0.01, f"得到 {q.change_pct*100}"
    # 顶层基准会算出 −4.58%，那是错的
    wrong = 57.95 / 60.73 - 1
    assert abs(wrong * 100 + 4.58) < 0.01
    assert abs(q.change_pct - wrong) > 0.03, "必须与顶层口径显著不同"


def test_freshest_prev_falls_back_when_absent():
    """该时段没给 prev_close 时退回顶层，不能除以 0。"""
    q = StockQuote(symbol="X", last=10.0, prev_close=9.0,
                   freshest=11.0, freshest_kind="常规", freshest_prev=0.0)
    assert abs(q.change_pct - (11.0 / 9.0 - 1)) < 1e-9
    q0 = StockQuote(symbol="X", last=0.0, prev_close=0.0,
                    freshest=11.0, freshest_kind="常规", freshest_prev=0.0)
    assert q0.change_pct == 0.0


def test_offhours_picks_latest_timestamp_not_fixed_priority():
    """非盘中按时间戳取最新，不按写死的「夜盘>盘后>盘前」。

    构造周末场景：夜盘是周五【开盘前】的残留，盘后才是最新。
    """
    r = _row(59.93, 59.93,
             overnight={"last": "60.47", "prev_close": "58.97",
                        "timestamp": "2026-09-18T08:00:00"},
             post_market={"last": "60.00", "prev_close": "59.93",
                          "timestamp": "2026-09-18T23:59:46"},
             pre_market={"last": "60.10", "prev_close": "58.97",
                         "timestamp": "2026-09-18T13:30:00"})
    v, kind, prev = _freshest(r)
    assert kind == "盘后", f"应取时间戳最新的盘后段，得到 {kind}"
    assert abs(v - 60.00) < 1e-9
    assert abs(prev - 59.93) < 1e-9, "必须带回该时段自己的基准"


def test_intraday_always_uses_regular_session():
    """盘中一律用常规段 —— 夜盘/盘后是上一时段的历史值。"""
    import datetime, zoneinfo
    et = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    rth = et.weekday() < 5 and 570 <= et.hour * 60 + et.minute < 960
    r = _row(100.0, 99.0, overnight={"last": "95.0", "prev_close": "94.0",
                                     "timestamp": "2099-01-01T00:00:00"})
    v, kind, prev = _freshest(r)
    if rth:
        assert kind == "常规" and abs(v - 100.0) < 1e-9
        assert abs(prev - 99.0) < 1e-9
    else:
        assert kind == "夜盘"


def test_all_sessions_absent_degrades_to_regular():
    v, kind, prev = _freshest(_row(42.0, 41.0))
    assert kind == "常规" and abs(v - 42.0) < 1e-9 and abs(prev - 41.0) < 1e-9


def test_quote_command_accepts_instrument_keys(monkeypatch, capsys):
    """`undertow quote gold silver` 必须查 GLD.US / SLV.US，不能拼成 gold.US。

    2026-09-25 实测该用法打出「未取到任何报价」—— 查价唯一入口对最自然的用法静默失败。
    """
    from undertow import cli
    from undertow.collect import longbridge_quote as lq
    asked = []
    def fake(syms):
        asked.extend(syms)
        return {s: lq.StockQuote(symbol=s, last=1.0, prev_close=1.0, freshest=1.0,
                                  freshest_kind="常规", freshest_prev=1.0) for s in syms}
    monkeypatch.setattr(lq, "fetch_stock_quotes", fake)
    class A: symbols = ["gold", "SLV", "qqq.US"]
    assert cli.cmd_quote(A()) == 0
    assert asked == ["GLD.US", "SLV.US", "qqq.US"], asked
    print("PASS test_quote_command_accepts_instrument_keys")
