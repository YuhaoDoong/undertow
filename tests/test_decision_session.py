"""快照捕获时刻 → 可交易日 的时序锁（codex 2026-09-01 P0-1）。

此前所有回测与台账都把【文件名日期】当成「D 开盘前已知」，
完全丢弃 captured_at。实测 193 份快照里 21 份不是盘前抓的
（18 份收盘后、3 份盘中），那部分是前视。
"""
from datetime import date, datetime

import pytest

from undertow.core.clock import (ET, INTRADAY, POST, PRE, capture_phase,
                                 decision_session)

TD = [date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31),
      date(2026, 9, 1), date(2026, 9, 2)]


def ts(y, m, d, H, M):
    return datetime(y, m, d, H, M, tzinfo=ET).timestamp()


# ── capture_phase ────────────────────────────────────────────────────
@pytest.mark.parametrize("H,M,want", [
    (0, 1, PRE), (6, 0, PRE), (9, 29, PRE),
    (9, 30, INTRADAY), (12, 0, INTRADAY), (15, 59, INTRADAY),
    (16, 0, POST), (23, 59, POST),
])
def test_capture_phase边界(H, M, want):
    assert capture_phase(ts(2026, 9, 1, H, M)) == want


def test_周末一律算盘后():
    # 2026-08-29 周六、08-30 周日
    assert capture_phase(ts(2026, 8, 29, 10, 0)) == POST
    assert capture_phase(ts(2026, 8, 30, 6, 0)) == POST


# ── decision_session ─────────────────────────────────────────────────
def test_盘前抓的用于当天():
    assert decision_session(ts(2026, 9, 1, 6, 0), TD) == date(2026, 9, 1)


def test_盘后抓的顺延到下一交易日():
    """2026-06-25~07-02 的快照全是当晚 21:49~23:27 ET 抓的，
    信息在收盘【之后】才存在，不能当成当日开盘前可用。"""
    assert decision_session(ts(2026, 8, 31, 23, 25), TD) == date(2026, 9, 1)


def test_盘中抓的必须剔除而不是退回文件名日期():
    """2026-07-21 GLD 在 09:58 ET 抓 —— 既非开盘前信息，
    也无法确定当天按什么价成交。返回 None，调用方不得回退。"""
    assert decision_session(ts(2026, 7, 21, 9, 58), TD) is None


def test_周末抓的落到下周一():
    assert decision_session(ts(2026, 8, 29, 12, 0), TD) == date(2026, 8, 31)
    assert decision_session(ts(2026, 8, 30, 3, 0), TD) == date(2026, 8, 31)


def test_盘前但当天非交易日则顺延():
    # 2026-08-29 是周六；即便"盘前"时刻也没有当天可交易
    assert decision_session(ts(2026, 8, 29, 6, 0), TD) == date(2026, 8, 31)


def test_日历没覆盖到就返回None而不是猜():
    assert decision_session(ts(2026, 12, 25, 20, 0), TD) is None


def test_冬令时也按美东判定():
    """1 月是 EST(-5)。用固定 UTC 偏移会在换季时错一小时，
    正好可能把盘前判成盘中。"""
    td = [date(2026, 1, 5), date(2026, 1, 6)]
    assert capture_phase(ts(2026, 1, 5, 6, 0)) == PRE
    assert decision_session(ts(2026, 1, 5, 6, 0), td) == date(2026, 1, 5)
    assert capture_phase(ts(2026, 1, 5, 20, 0)) == POST
    assert decision_session(ts(2026, 1, 5, 20, 0), td) == date(2026, 1, 6)


# ── SnapshotStore 接口 ───────────────────────────────────────────────
def test_store_暴露captured_at(tmp_path):
    from undertow.collect.store import SnapshotStore
    st = SnapshotStore(tmp_path)
    t = ts(2026, 9, 1, 6, 0)
    st.save("options", "ZZZ", {"x": 1}, on_date=date(2026, 9, 1), captured_at=t)
    assert st.captured_at("options", "ZZZ", date(2026, 9, 1)) == pytest.approx(t)
    assert st.load("options", "ZZZ", date(2026, 9, 1)) == {"x": 1}


def test_store_decision_session(tmp_path):
    from undertow.collect.store import SnapshotStore
    st = SnapshotStore(tmp_path)
    # 盘后抓，文件名写成 8/31 —— 可交易日应是 9/1，不是 8/31
    st.save("options", "ZZZ", {"x": 1}, on_date=date(2026, 8, 31),
            captured_at=ts(2026, 8, 31, 22, 0))
    got = st.decision_session("options", "ZZZ", date(2026, 8, 31), TD)
    assert got == date(2026, 9, 1), "盘后快照被当成了当日开盘前信息"


def test_store_缺文件返回None(tmp_path):
    from undertow.collect.store import SnapshotStore
    st = SnapshotStore(tmp_path)
    assert st.captured_at("options", "ZZZ", date(2026, 9, 1)) is None
    assert st.decision_session("options", "ZZZ", date(2026, 9, 1), TD) is None
