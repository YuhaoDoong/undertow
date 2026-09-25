"""主源停更 → 自动切长桥备份源 —— 回归测试。

2026-09-22~24：CBOE 停更 34 小时。上一轮只加了【告警】，结果 **9/22 收盘那份
OI 永久丢失** —— 等人看到告警、手动补抓时，源已经跳到 9/23 了。
期权链不可再生，所以告警不够，必须当场换源。

本文件锚住降级的两个前提（缺一不可）与四个分支的可区分性：

    ① 正常 unchanged（今天还没结算）→ **不得**降级。否则每天凌晨早时点都白跑
       2~4 分钟长桥全链，且告警变狼来了。
    ② 跨 ≥STALE_SESSIONS 个交易日 → 降级，落盘，status 标明来源。
    ③ 备份源也说没有新 OI → 两个独立源一致，判定确实未结算，**不算失败**。
    ④ 备份源抓取失败 → 必须记 failed + 进 stale_unresolved，不能静默。
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow import cli                                      # noqa: E402
from undertow.collect.base import DataSourceError             # noqa: E402
from undertow.core.clock import STALE_SESSIONS                # noqa: E402

TODAY = date(2026, 9, 24)          # 周四，实测停更那天
PREV = date(2026, 9, 22)           # 周二，源卡住的时点


def _payload(*, asof: date, oi: int, strike: float = 400.0) -> dict:
    """最小可用的 CBOE 形态 payload。asof 就是源自报的数据时点。"""
    return {
        "timestamp": f"{asof.isoformat()}T20:00:00+00:00",
        "data": {
            "current_price": 410.0,
            "last_trade_time": f"{asof.isoformat()}T15:59:59",
            "options": [{
                "option": f"GLD{asof:%y%m%d}C{int(strike * 1000):08d}",
                "open_interest": oi, "volume": 100, "iv": 0.2,
                "delta": 0.5, "gamma": 0.01, "bid": 1.0, "ask": 1.1,
                "bid_size": 1, "ask_size": 1, "theta": 0.0, "vega": 0.0,
                "rho": 0.0, "last_trade_price": 1.05, "prev_day_close": 1.0,
            }],
        },
    }


# 到期日必须固定，否则 OCC 里的到期变了就成了「不同合约」，
# oi_change_total 看不到 ΔOI=0（存活合约集合为空）——去重判据会失效。
def _fixed(asof: date, oi: int) -> dict:
    p = _payload(asof=asof, oi=oi)
    p["data"]["options"][0]["option"] = "GLD261016C00400000"
    return p


class _Args:
    def __init__(self, **kw):
        self.instruments = ["gold"]
        self.no_cache = True
        self.status_file = None
        self.no_fallback = False
        self.fallback_expiries = None
        self.__dict__.update(kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的快照仓库 + 冻结的今天 + 可控的主源。预置一份 PREV 快照。"""
    # root=tmp_path 是硬要求：写进真实 data/snapshots 会污染不可再生的历史数据。
    # 原先这里写成「若不支持 root 就退回默认」—— 那正是静默失败：
    # 签名一变，测试就悄悄往实盘数据里写。
    store = cli.SnapshotStore(root=tmp_path)
    monkeypatch.setattr(cli, "market_today", lambda: TODAY)
    monkeypatch.setattr(cli, "SnapshotStore", lambda *a, **k: store)
    store.save("options", "GLD", _fixed(PREV, 1000), on_date=PREV)

    state = {"fallback_calls": 0}

    def set_main(asof, oi):
        class _Main:
            name = "cboe_etf"
            def fetch_raw(self, inst, *, use_cache=True):
                return _fixed(asof, oi)
        monkeypatch.setattr(cli, "CboeOptionsSource", _Main)

    def set_fallback(behavior):
        import undertow.collect.longbridge_options as lo

        class _LB:
            name = "longbridge_options"
            def fetch_raw(self, inst, *, use_cache=True, max_expiries=None):
                state["fallback_calls"] += 1
                if behavior == "boom":
                    raise DataSourceError("longbridge CLI 不可用")
                p = _fixed(asof=TODAY, oi=1000 if behavior == "same" else 7777)
                p["_source"] = "longbridge_options"
                p["_greeks"] = "bs_computed"
                p["_missing_quote"] = []
                return p
        monkeypatch.setattr(lo, "LongbridgeOptionsSource", _LB)

    def run(**kw):
        args = _Args(**kw)
        args.status_file = str(tmp_path / "st.json")
        rc = cli.cmd_snapshot(args)
        return rc, json.loads((tmp_path / "st.json").read_text("utf-8"))

    env = type("E", (), {})()
    env.store, env.state, env.run = store, state, run
    env.set_main, env.set_fallback = set_main, set_fallback
    return env


def _item(st):
    return next(i for i in st["items"] if i["instrument"] == "gold")


def test_normal_unchanged_does_not_fall_back(env, monkeypatch):
    """① 源时点就是上一交易日 = 今天还没结算，正常。绝不能降级。

    这是每天凌晨早时点的常态。若这里降级，等于每天白跑几十分钟长桥全链。
    """
    env.set_main(PREV, 1000)                     # 与已存快照逐行相同
    env.set_fallback("new")
    wed = PREV + timedelta(days=1)               # 周三：只跨 1 个交易日
    monkeypatch.setattr(cli, "market_today", lambda: wed)
    rc, st = env.run()
    assert env.state["fallback_calls"] == 0, "正常 unchanged 触发了降级"
    assert _item(st)["status"] == "unchanged"
    assert _item(st)["stale_sessions"] == 1
    assert st["stale_unresolved"] == [], "1 个交易日不算停更，不该进告警名单"
    print("PASS test_normal_unchanged_does_not_fall_back")


def test_stale_source_falls_back_and_saves(env):
    """② 跨 2 个交易日 → 降级抓到新数据并落盘，status 必须标明来源。"""
    env.set_main(PREV, 1000)
    env.set_fallback("new")
    rc, st = env.run()
    assert rc == 0 and env.state["fallback_calls"] == 1
    it = _item(st)
    assert it["status"] == "saved" and it["fallback"] is True
    assert it["source"] == "longbridge_options"
    assert it["stale_sessions"] >= STALE_SESSIONS
    assert st["fallback_used"] == ["gold"]
    assert st["overall"] == "complete", "降级成功仍是 complete —— 研报该照常出"
    # 真的落盘了，且落的是备份源的数据
    saved = env.store.load("options", "GLD", TODAY)
    assert saved is not None and saved["_source"] == "longbridge_options"
    assert saved["data"]["options"][0]["open_interest"] == 7777
    print("PASS test_stale_source_falls_back_and_saves")


def test_both_sources_agree_no_new_oi_is_not_a_failure(env):
    """③ 两个独立源都说没有新 OI → 那就是真没结算，不该记 failed。

    这是降级最有价值的副产品：它把「源挂了」和「确实还没结算」区分开了 ——
    单源时代这两者完全无法分辨。
    """
    env.set_main(PREV, 1000)
    env.set_fallback("same")
    rc, st = env.run()
    assert env.state["fallback_calls"] == 1
    it = _item(st)
    assert it["status"] == "unchanged", f"两源一致不该记 failed：{it}"
    assert st["stale_unresolved"] == [], "两源一致 → 不是「兜不住」"
    assert st["n_failed"] == 0
    print("PASS test_both_sources_agree_no_new_oi_is_not_a_failure")


def test_fallback_failure_is_loud(env):
    """④ 备份源也挂 → 必须 failed + 进 stale_unresolved。静默是最严重的 bug。"""
    env.set_main(PREV, 1000)
    env.set_fallback("boom")
    rc, st = env.run()
    it = _item(st)
    assert it["status"] == "failed", f"备份源失败被吞了：{it}"
    assert it["fallback"] is True and it["error"], "必须留下失败原因供审计"
    assert st["stale_unresolved"] == ["gold"]
    assert rc != 0, "一份都没落盘必须非零退出，否则调度层以为跑成功了"
    print("PASS test_fallback_failure_is_loud")


def test_no_fallback_flag_still_reports_stale(env):
    """--no-fallback 只关降级，**不关告警** —— 否则关掉开关就等于关掉眼睛。"""
    env.set_main(PREV, 1000)
    env.set_fallback("new")
    rc, st = env.run(no_fallback=True)
    assert env.state["fallback_calls"] == 0
    assert _item(st)["status"] == "unchanged"
    assert st["stale_unresolved"] == ["gold"], "禁用降级后仍必须报停更"
    print("PASS test_no_fallback_flag_still_reports_stale")


def test_source_asof_prefers_source_own_timestamp():
    """停更判据优先用【源自报的时点】，而不是我们自己的落盘状态。

    区别很实在：源侧 ts 说的是「源没动」，落盘状态只说「我们没拿到新的」——
    后者也可能是我们自己的落盘环节坏了，那不该去切源。
    """
    p = _payload(asof=PREV, oi=1)
    assert cli._source_asof(p) == PREV
    p["data"]["last_trade_time"] = None          # 退回顶层 timestamp
    assert cli._source_asof(p) == PREV
    assert cli._source_asof({"data": {}}) is None, "取不到必须返回 None，不能猜今天"
    print("PASS test_source_asof_prefers_source_own_timestamp")
