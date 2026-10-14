"""ps-trigger-causal-v1（Codex 042 P1）：limit 挂单成交前生效、不看当日收盘/全日低点；候选只有实际成交才占位。全部合成。"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import ps_trigger_causal as pc  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402


def _bars(n=40, px=101.0):
    return [(i, px, px + 1, px - 2, px) for i in range(n)]


def _ep(b0=10, ll=90.0, key=100.0, d="long"):
    return NS(direction=d, break_at=b0, extreme=NS(price=ll), key=NS(price=key))


def test_limit_codex_three_paths_all_fill_at_100():
    """LL90、挂单 100、ATR2、tol .5：旧版①判失效 ②判超时 ③才成交；挂单已被穿越，三条都必须成交于 100。"""
    atr = [2.0] * 40
    for low, close, old in ((80, 85, "invalidated_close_beyond_extreme"), (90.5, 95, "timeout_no_entry"), (91, 95, "entered")):
        b = _bars(); b[10] = (10, 101, 104, 100.5, 103); b[11] = (11, 102, 103, low, close)
        if old == "timeout_no_entry":
            for t in range(12, 40):
                b[t] = (t, 101, 102, 100.5, 101)
        assert ps.fast_entry(b, atr, _ep(), [], "limit", "0.5", last_i=39)["status"] == old
        r = ps.fast_entry_causal(b, atr, _ep(), [], "limit", "0.5", last_i=39)
        assert r["status"] == "entered" and r["entry_i"] == 11 and r["entry"] == 100.0
        assert r["audit"]["order_live_from"] == 11 and r["audit"]["decided_at"] == 10


def test_limit_fill_then_same_bar_stop_is_loss_not_dropped():
    b = _bars(300); b[10] = (10, 101, 104, 100.5, 103); b[11] = (11, 102, 103, 80, 85)   # 250 根行政上限要在数据内
    atr = [2.0] * 300
    fe = ps.fast_entry_causal(b, atr, _ep(), [], "limit", "0.5", last_i=299)
    res = pc.exit_fast(b, atr, [], "0.5", 299, "limit")(_ep(), fe)
    assert res["exit_reason"] == "stop" and res["exit_i"] == 11 and res["exit_price"] == 90 - 0.2 and res["r_mult"] < 0


def test_limit_gap_below_stop_filled_through_stop():
    b = _bars(); b[10] = (10, 101, 104, 100.5, 103); b[11] = (11, 85, 86, 84, 85)   # 低开在止损 89.8 之下
    atr = [2.0] * 40
    fe = ps.fast_entry_causal(b, atr, _ep(), [], "limit", "0.5", last_i=39)
    assert fe["entry"] == 85 and fe["audit"]["gap_fill"] is True
    res = pc.exit_fast(b, atr, [], "0.5", 39, "limit")(_ep(), fe)
    assert res["status"] == "filled_through_stop" and res["exit_i"] == 11 and pc._atr_units(res, _ep(), atr) == 0


def test_limit_order_cancelled_after_prior_close_beyond_ll():
    b = _bars(); b[10] = (10, 101, 104, 100.5, 103); b[11] = (11, 101, 102, 100.2, 89)  # 未触及 100，收盘跌破 LL
    b[12] = (12, 95, 101, 94, 96)
    r = ps.fast_entry_causal(b, [2.0] * 40, _ep(), [], "limit", "0.5", last_i=39)
    assert r["status"] == "invalidated_close_beyond_extreme" and r["resolved_at"] == 11


def test_scheduler_codex_a_b_only_fills_occupy():
    """A：break2→entry8/exit10；B：break4→entry6/exit7。旧调度只输出 A；同时只持一笔仍应 B 先入、7 出，A 再于 8 入。"""
    A, B = NS(break_at=2, direction="long"), NS(break_at=4, direction="long")
    fe = {2: {"status": "entered", "entry_i": 8, "entry": 1.0}, 4: {"status": "entered", "entry_i": 6, "entry": 1.0}}
    ex = {2: 10, 4: 7}
    rows = pc.schedule([(A, fe[2]), (B, fe[4])], lambda ep, f: {"status": "entered", "exit_i": ex[ep.break_at]})
    assert [(r["ep"].break_at, r["sched"]) for r in rows] == [(2, "entered"), (4, "entered")]


def test_scheduler_busy_and_non_entries_kept_as_rows():
    C = [NS(break_at=i, direction="long") for i in (1, 2, 3)]
    fes = [{"status": "entered", "entry_i": 5, "entry": 1.0}, {"status": "entered", "entry_i": 6, "entry": 1.0},
           {"status": "timeout_no_entry", "resolved_at": 60}]
    rows = pc.schedule(list(zip(C, fes)), lambda ep, f: {"status": "entered", "exit_i": 9})
    assert [r["sched"] for r in rows] == ["entered", "busy_skipped", "timeout_no_entry"]
    assert rows[1]["blocked_by_break"] == 1
    # 同根衔接不允许：上一笔 exit_i = 6 时，entry_i = 6 的候选被占用（事前规定）
    rows = pc.schedule(list(zip(C[:2], fes[:2])), lambda ep, f: {"status": "entered", "exit_i": 6})
    assert [r["sched"] for r in rows] == ["entered", "busy_skipped"]
    # 未成交（越过止损而放弃）不占位
    rows = pc.schedule(list(zip(C[:2], fes[:2])), lambda ep, f: {"status": "open_beyond_stop"} if ep.break_at == 1 else {"status": "entered", "exit_i": 9})
    assert [r["sched"] for r in rows] == ["open_beyond_stop", "entered"]


def test_non_limit_methods_unchanged_and_audited():
    """bar 确认：确认根 t 收盘可知 → t+1 开盘，旧新相同；新版带触发点与决定/执行根。"""
    b = _bars(); b[10] = (10, 101, 104, 100.5, 103)
    b[12] = (12, 100, 101, 95, 96); b[13] = (13, 97, 102, 96, 101.5)
    atr = [2.0] * 40
    loc = [ps.Pivot("L", 12, 13, 95.0)]
    old = ps.fast_entry(b, atr, _ep(), loc, "bar", "0.5", last_i=39)
    new = ps.fast_entry_causal(b, atr, _ep(), loc, "bar", "0.5", last_i=39)
    assert (old["entry_i"], old["entry"]) == (new["entry_i"], new["entry"]) == (14, 101)
    assert new["audit"]["pivot"] == {"idx": 12, "conf": 13, "price": 95.0} and new["audit"]["execute_at"] == 14


def test_config_asserts_match_scripts():
    c = pc.load_cfg()
    assert c["version"].startswith("ps-trigger-causal-v1")
