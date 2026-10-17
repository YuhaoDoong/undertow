"""主墙近侧研究账（Codex 056 合同 v0.1）：选墙、近侧、候选、政策结果、机会类别、冻结与只写一次。全部合成数据。"""
import sys
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import mainwall as mw  # noqa: E402
from undertow.analyze import shadow as sh  # noqa: E402
from undertow.collect import jsonl_ledger as jl  # noqa: E402
from undertow.core.clock import ET  # noqa: E402

C = namedtuple("C", "kind strike expiry open_interest")
T = date(2026, 10, 16)
E = date(2026, 10, 19)


def test_wall_map_counts_only_expiry_ge_E_and_decomposes():
    cs = [C("P", 50.0, date(2026, 10, 16), 90000),       # 早于 E：不进主分子
          C("P", 50.0, E, 1000), C("P", 50.0, date(2026, 11, 20), 9000),
          C("P", 48.0, date(2026, 11, 20), 9500),
          C("P", 30.0, date(2026, 11, 20), 999999)]       # 价域 25% 外
    w = mw.wall_map(cs, T, E, 52.0, "P")
    assert w["status"] == "ok" and w["strike"] == 50.0 and w["oi"] == 10000
    assert w["decomp"] == {"ge_E": 10000, "all_live": 100000, "le14": 91000, "at_E": 1000, "lt_E": 90000}
    assert w["second"] == {"strike": 48.0, "oi": 9500} and w["ratio_top2"] == round(10000 / 9500, 4)


def test_wall_map_ties_nearer_then_lower_strike_and_otm_only():
    cs = [C("P", 48.0, E, 100), C("P", 49.0, E, 100), C("P", 53.0, E, 10**6)]   # 53 是实值，不算
    assert mw.wall_map(cs, T, E, 52.0, "P")["strike"] == 49.0
    cs = [C("C", 54.0, E, 100), C("C", 50.0, E, 100)]                          # 离 52 等距 → 数值小者
    assert mw.wall_map(cs, T, E, 52.0, "C")["strike"] == 54.0                   # 50 是实值 call，被排除
    assert mw.wall_map([C("C", 55.0, E, 0)], T, E, 52.0, "C")["status"] == "no_wall"


def _walls(p=50.0, c=60.0):
    return {"P": {"status": "ok", "strike": p} if p else {"status": "no_wall"},
            "C": {"status": "ok", "strike": c} if c else {"status": "no_wall"}}


def test_choose_side_nearest_single_tie_beyond():
    assert mw.choose_side(_walls(), 52.0, 1.5)["near"] == "P"
    assert mw.choose_side(_walls(p=None), 52.0, 1.5)["reason"] == "single_side"
    assert mw.choose_side(_walls(48.0, 56.0), 52.0, 1.5)["reason"] == "tie"
    ch = mw.choose_side(_walls(53.0, 60.0), 52.0, 1.5)        # put 墙已在价上 → 不合格、不换更远一层
    assert ch["sides"]["P"]["reason"] == "already_beyond" and ch["near"] == "C" and ch["far"] is None
    ch = mw.choose_side(_walls(52.0, None), 52.0, 1.5)        # 恰在墙上：d 必须严格 > 0
    assert ch["near"] is None and ch["reason"] == "already_beyond"


STR = {"P": [47.0, 48.0, 49.0, 50.0, 51.0], "C": [58.0, 59.0, 60.0, 61.0]}


def test_candidates_exact_wall_protective_and_b_distance():
    ch = mw.choose_side(_walls(), 52.0, 1.5)
    legs = {l["leg_id"]: l for l in mw.candidates(STR, E, 52.0, 1.5, _walls(), {"P": 1.0, "C": 1.0}, ch)}
    a, b = legs["near-P-A"], legs["near-P-B"]
    assert (a["sell"], a["buy"]) == (50.0, 49.0) and a["status"] == "candidate"
    assert (b["sell"], b["buy"]) == (50.0, 49.0) and b["same_as_A"]          # 1 ATR 落到同一档 → A=B 保留
    assert b["distance_residual_atr"] == round((52 - 50) / 1.5 - 1, 6)
    assert legs["far-C-A"]["sell"] == 60.0 and legs["far-C-A"]["buy"] == 61.0


def test_candidates_missing_wall_strike_or_protective_not_shifted():
    ch = mw.choose_side(_walls(p=49.5), 52.0, 1.5)
    legs = {l["leg_id"]: l for l in mw.candidates(STR, E, 52.0, 1.5, _walls(p=49.5), {"P": 1.0, "C": 1.0}, ch)}
    assert legs["near-P-A"]["status"] == "no_candidate" and legs["near-P-A"]["reason"] == "wall_strike_not_listed_for_E"
    ch = mw.choose_side(_walls(p=47.0, c=None), 52.0, 1.5)
    legs = {l["leg_id"]: l for l in mw.candidates(STR, E, 52.0, 1.5, _walls(p=47.0, c=None), {"P": 1.0}, ch)}
    assert legs["near-P-A"]["reason"] == "no_same_width_protective"         # 46 不挂牌 → 不挪腿


def _row(attempt_at: str, frozen_at: str, sell_bid=0.30, buy_ask=0.10, near="P"):
    legs = [{"leg_id": f"near-{near}-A", "role": "near", "side": near, "rule": "A", "expiry": E.isoformat(),
             "sell": 50.0, "buy": 49.0, "width_usd": 100.0, "status": "candidate"},
            {"leg_id": f"near-{near}-B", "role": "near", "side": near, "rule": "B", "expiry": E.isoformat(),
             "sell": 50.0, "buy": 49.0, "width_usd": 100.0, "status": "candidate"}]
    q = {"P|50": {"bid": sell_bid, "ask": sell_bid + 0.02, "bid_size": 10, "ask_size": 10, "error": None},
         "P|49": {"bid": buy_ask - 0.02, "ask": buy_ask, "bid_size": 10, "ask_size": 10, "error": None}}
    end = (datetime.fromisoformat(attempt_at) + timedelta(seconds=20)).isoformat()
    return {"key": f"{T}|silver", "session": T.isoformat(), "premarket": {"status": "ok"},
            "entry_decision": {"frozen_at": frozen_at, "choice": {"near": near, "reason": "nearest"}},
            "legs": legs, "windows": {f"{T}|open": {"attempts": [
                {"started_at": attempt_at, "ended_at": end, "phase": "rth", "quotes": q}]}}}


def _et(h, m):
    return datetime(2026, 10, 16, h, m, tzinfo=ET).isoformat()


def test_quotes_before_sdec_freeze_are_ignored():
    row = _row(_et(10, 1), _et(10, 2))
    o = mw.leg_outcome(row["legs"][0], row, bars=[], now=datetime(2026, 10, 16, 11, 0, tzinfo=ET))
    assert o["entry_status"] != "valid" and o["policy_pnl"] is None       # 冻结前的盘口不接受


def test_payoff_reject_is_zero_and_category():
    row = _row(_et(10, 5), _et(10, 2), sell_bid=0.06, buy_ask=0.02)       # 收 4 美元：远低于 10% 门槛
    now = datetime(2026, 10, 16, 11, 0, tzinfo=ET)
    outs = {l["leg_id"]: mw.leg_outcome(l, row, bars=[], now=now) for l in row["legs"]}
    assert outs["near-P-A"]["policy"] == "payoff_reject" and outs["near-P-A"]["policy_pnl"] == 0.0
    assert mw.row_category(row, outs) == "payoff_reject"


def test_entered_immature_not_zero():
    row = _row(_et(10, 5), _et(10, 2), sell_bid=0.40, buy_ask=0.10)
    now = datetime(2026, 10, 16, 11, 0, tzinfo=ET)
    o = mw.leg_outcome(row["legs"][0], row, bars=[], now=now)
    assert o["entry_status"] == "valid" and o["payoff"]["status"] == "pass"
    assert o["policy"] == "immature" and o["policy_pnl"] is None             # 未到退出窗：未知，不是 0


def test_row_category_priority():
    assert mw.row_category({"premarket": {"status": "premarket_input_stale"}}, {}) == "premarket_input_stale"
    assert mw.row_category({"premarket": {"status": "ok"}, "entry_decision": None}, {}) == "sdec_missing"
    assert mw.row_category({"premarket": {"status": "ok"},
                            "entry_decision": {"choice": {"near": None, "reason": "tie"}}}, {}) == "tie"


def test_condor_needs_same_attempt_both_sides():
    assert mw.condor({"near-P-A": {"entry_status": "valid"}}, {}) == {"status": "one_side_missing", "pnl": None}


# ── 编排层：冻结 / 只写一次 / 机会母表 ──────────────────────────────────────

@pytest.fixture
def mwcli(tmp_path, monkeypatch):
    import undertow.mainwall_cli as m
    monkeypatch.setattr(m, "DIR", tmp_path / "mw")
    return m


def _base_row(m, key=f"{T}|silver"):
    return {"key": key, "schema": 1, "instrument": "silver", "symbol": "SLV", "session": T.isoformat(),
            "config_version": mw.CONFIG["version"], "premarket": {"status": "ok", "walls": {"P": 1}},
            "entry_decision": None, "legs": [], "windows": {}, "samples": [], "outcome": None}


def _att(status, tried_at, key=f"{T}|silver"):
    return {"key": key, "instrument": "silver", "session": T.isoformat(), "status": status, "tried_at": tried_at}


def test_entry_set_once_and_frozen(mwcli):
    m = mwcli
    p = m._path("silver"); p.parent.mkdir(parents=True)
    jl.insert_frozen(p, _base_row(m), key_field="key", frozen=m.frozen_part)
    assert m._set_entry(p, f"{T}|silver", {"sdec": 51.8}, [{"leg_id": "x"}]) == "set"
    assert m._set_entry(p, f"{T}|silver", {"sdec": 49.0}, []) == "exists"      # 后续价格不重选
    assert jl.load(p, "key")[0]["entry_decision"]["sdec"] == 51.8

    def bad(r):
        r["legs"] = []
        return True
    with pytest.raises(jl.LedgerConflictError):
        jl.update(p, bad, key_field="key", frozen=m.frozen_part)


def test_premarket_conflict_not_overwritten(mwcli):
    m = mwcli
    p = m._path("silver"); p.parent.mkdir(parents=True)
    jl.insert_frozen(p, _base_row(m), key_field="key", frozen=m.frozen_part)
    r2 = _base_row(m); r2["premarket"]["walls"] = {"P": 2}                    # 之后的快照算出不同墙 → 不能改写
    with pytest.raises(jl.LedgerConflictError):
        jl.insert_frozen(p, r2, key_field="key", frozen=m.frozen_part)


def test_denominator_row_from_last_failed_attempt(mwcli):
    m = mwcli
    m._append_attempt(_att("input_stale", "a"))
    m._append_attempt(_att("snapshot_missing", "b"))

    class I:
        key = "silver"

        class options:
            symbol = "SLV"
    m._ensure_denominator_row(I, T)
    r = jl.load(m._path("silver"), "key")[0]
    assert r["premarket"]["status"] == "premarket_snapshot_missing" and r["premarket"]["last_attempt_at"] == "b"
    m._ensure_denominator_row(I, T)                                           # 幂等
    assert len(jl.load(m._path("silver"), "key")) == 1


class _I:
    key = "silver"

    class options:
        symbol = "SLV"


class _Store:
    def dates(self, kind, sym):
        return [T]

    def load_with_identity(self, kind, sym, d):
        return {"data": {}}, {"captured_at": datetime(2026, 10, 16, 6, 0, tzinfo=ET).timestamp(), "sha256": "x"}

    def load(self, *a):
        return None


def _clocks(monkeypatch, m, *times):
    it = iter(times)
    last = {}

    def clk():
        last["t"] = next(it, last.get("t"))
        return last["t"]
    monkeypatch.setattr(m, "_clock", clk)
    monkeypatch.setattr(m, "_wall_now", lambda: last.get("t") or times[0])


def _fresh_inputs(avail):
    return {"status": "fresh", "close": 51.92, "atr14": 1.5, "base_date": "2026-10-15", "expected_base_date": "2026-10-15",
            "available_at": avail, "cross_check": []}


def _setup_capture(monkeypatch, m, avail):
    import undertow.collect.cboe_options as co
    import undertow.collect.fresh_daily as fd
    snap = type("S", (), {"contracts": [C("P", 50.0, E, 100), C("C", 60.0, E, 100)]})()
    monkeypatch.setattr(co, "snapshot_from_payload", lambda *a: snap)
    monkeypatch.setattr(fd, "resolve", lambda *a, **k: (_fresh_inputs(avail), object()))
    monkeypatch.setattr(mw, "derive_inputs", lambda rec, ser, T: rec)
    monkeypatch.setattr(sh, "target_expiry", lambda *a: E)
    monkeypatch.setattr(sh, "listed_strikes", lambda snap, k, e: [49.0, 50.0] if k == "P" else [60.0, 61.0])


def _et_dt(h, m_, s=0):
    return datetime(2026, 10, 16, h, m_, s, tzinfo=ET)


def test_capture_late_or_stale_never_freezes(mwcli, monkeypatch):
    m = mwcli
    _clocks(monkeypatch, m, _et_dt(9, 31))
    assert m.capture_one(_I, T, store=_Store())[0] == "late"
    import undertow.collect.fresh_daily as fd
    import undertow.collect.cboe_options as co
    monkeypatch.setattr(co, "snapshot_from_payload", lambda *a: type("S", (), {"contracts": []})())
    monkeypatch.setattr(fd, "resolve", lambda *a, **k: ({"status": "stale"}, None))
    _clocks(monkeypatch, m, _et_dt(6, 30))
    r, att = m.capture_one(_I, T, store=_Store())
    assert r == "attempt_failed" and att["status"] == "input_stale"
    assert not m._path("silver").exists()                                   # 失败不占当天冻结键


def test_capture_started_premarket_but_completed_after_open_not_frozen(mwcli, monkeypatch):
    m = mwcli
    _setup_capture(monkeypatch, m, _et_dt(9, 28).isoformat())
    _clocks(monkeypatch, m, _et_dt(9, 29), _et_dt(9, 29, 50), _et_dt(9, 30, 5))
    r, att = m.capture_one(_I, T, store=_Store())
    assert r == "attempt_failed" and att["status"] == "late_completion" and not m._path("silver").exists()


def test_capture_input_available_after_freeze_or_unknown_not_frozen(mwcli, monkeypatch):
    m = mwcli
    for avail in (None, _et_dt(9, 31).isoformat()):
        _setup_capture(monkeypatch, m, avail)
        _clocks(monkeypatch, m, _et_dt(9, 0), _et_dt(9, 0, 10), _et_dt(9, 0, 20))
        r, att = m.capture_one(_I, T, store=_Store())
        assert r == "attempt_failed" and att["status"] == "input_availability_unproven"
    _setup_capture(monkeypatch, m, _et_dt(8, 59).isoformat())
    _clocks(monkeypatch, m, _et_dt(9, 0), _et_dt(9, 0, 10), _et_dt(9, 0, 20))
    r, row = m.capture_one(_I, T, store=_Store())
    assert r == "inserted" and row["premarket"]["frozen_at"] == _et_dt(9, 0, 20).isoformat()
    assert row["premarket"]["attempt_started_at"] < row["premarket"]["inputs_consumed_at"] < row["premarket"]["frozen_at"]


def test_offline_replay_refused(mwcli, monkeypatch):
    m = mwcli
    monkeypatch.setattr(m, "_clock", lambda: _et_dt(6, 30))
    monkeypatch.setattr(m, "_wall_now", lambda: datetime(2026, 10, 17, 10, 0, tzinfo=ET))
    with pytest.raises(RuntimeError):
        m.capture_one(_I, T, store=_Store())


# ── 端到端：开盘窗冻结 Sdec → 选近侧 → 取盘口 → 派生（全部替身，不联网）──────────

def _premarket_row(inst="silver"):
    return {"key": f"{T}|{inst}", "schema": 1, "instrument": inst, "symbol": "SLV", "session": T.isoformat(),
            "config_version": mw.CONFIG["version"], "config_hash": mw.config_hash(),
            "premarket": {"status": "ok", "E": E.isoformat(),
                          "inputs": {"status": "fresh", "close": 51.92, "atr14": 1.5, "base_date": "2026-10-15"},
                          "walls": {"P": {"status": "ok", "strike": 50.0}, "C": {"status": "ok", "strike": 60.0}},
                          "strikes": {"P": [48.0, 49.0, 50.0, 51.0], "C": [59.0, 60.0, 61.0]},
                          "widths": {"P": 1.0, "C": 1.0}},
            "entry_decision": None, "legs": [], "windows": {}, "samples": [], "outcome": None}


def _e2e(monkeypatch, mwcli, kind="常规", start=(10, 5, 0)):
    import itertools
    import undertow.collect.longbridge_quote as lq
    import undertow.shadow_cli as sc
    from undertow.collect.longbridge_quote import Depth
    m = mwcli
    p = m._path("silver"); p.parent.mkdir(parents=True)
    jl.insert_frozen(p, _premarket_row(), key_field="key", frozen=m.frozen_part)
    clock = (datetime(2026, 10, 16, *start, tzinfo=ET) + timedelta(seconds=i) for i in itertools.count())
    monkeypatch.setattr(m, "_now_iso", lambda: next(clock).isoformat())
    monkeypatch.setattr(m, "market_today", lambda: T)
    monkeypatch.setattr(sc, "_phase_now", lambda: "rth")
    monkeypatch.setattr(sc, "_window_now", lambda w: True)
    Q = type("Q", (), {"freshest_kind": kind, "last": 51.85, "freshest": 51.85, "timestamp": "t"})
    monkeypatch.setattr(lq, "fetch_stock_quotes", lambda syms: {s: Q() for s in syms})
    px = {"P50": (0.40, 0.42), "P49": (0.15, 0.17), "C59": (0.08, 0.09), "C60": (0.05, 0.06), "C61": (0.02, 0.03)}

    def depth(syms):
        out = {}
        for s in syms:
            side = s[-9]                                                      # SLV261019P50000.US
            k = side + str(int(s[-8:-3]) // 1000)
            b, a = px.get(k, (None, None))
            out[s] = Depth(s, b, 10, a, 10)
        return out
    monkeypatch.setattr(lq, "fetch_depth", depth)

    class A:
        instruments = ["silver"]; window = "open"; status_file = None; run_id = None
    return m, p, A


def test_open_window_freezes_sdec_then_quotes(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli)
    assert m.cmd_quote(A) == 0
    r = jl.load(p, "key")[0]
    ed = r["entry_decision"]
    assert ed["sdec"] == 51.85 and ed["choice"]["near"] == "P" and ed["choice"]["far"] == "C"
    att = r["windows"][f"{T}|open"]["attempts"][0]
    assert att["started_at"] > ed["frozen_at"]                                 # 先冻结 Sdec，后取盘口
    ids = {l["leg_id"] for l in r["legs"]}
    assert {"near-P-A", "near-P-B", "far-C-A", "far-C-B"} <= ids
    o = m.derive_row(r, bars=[], now=datetime(2026, 10, 16, 11, 0, tzinfo=ET))
    assert o["legs"]["near-P-A"]["entry_status"] == "valid" and o["category"] == "immature"
    assert m.cmd_quote(A) == 0                                                  # 再跑一次：不重选、不重抓已有效的腿
    r2 = jl.load(p, "key")[0]
    assert r2["entry_decision"] == ed and len(r2["windows"][f"{T}|open"]["attempts"]) == 1


def test_open_window_refuses_non_regular_sdec(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli, kind="盘前")
    assert m.cmd_quote(A) == 1
    assert jl.load(p, "key")[0]["entry_decision"] is None


def test_open_window_flags_failed_premarket(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli)

    class B(A):
        instruments = ["gold"]
    assert m.cmd_quote(B) == 1                                                  # gold 盘前没冻结 → 补分母行并报未完成
    g = jl.load(m._path("gold"), "key")[0]
    assert g["premarket"]["status"] == "premarket_not_run"


def test_sample_records_signed_distance_once_per_slot(monkeypatch, mwcli):
    import undertow.shadow_cli as sc
    m, p, A = _e2e(monkeypatch, mwcli)
    _sample_env(monkeypatch, m, depth_ok=True)
    assert m.cmd_sample(A) == 0
    r = jl.load(p, "key")[0]
    s = r["samples"][0]
    assert s["signed_dist_atr"]["P"] == round((51.85 - 50) / 1.5, 6) and s["signed_dist_atr"]["C"] > 0
    assert "P|50" in s["quotes"] and "C|60" in s["quotes"]
    assert m.cmd_sample(A) == 0 and len(jl.load(p, "key")[0]["samples"]) == 1     # 同一桶不重复记


def test_sdec_fetch_crossing_window_end_not_frozen(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli, start=(10, 20, 59))     # 10:20:59 发出、10:21:00 返回 → 跨窗末
    assert m.cmd_quote(A) == 1
    assert jl.load(p, "key")[0]["entry_decision"] is None


# ── R3：确定弃权 0 / 未知 None / 机会母表 / 损坏尝试账 ──────────────────────────

def test_determined_abstention_is_zero_unknown_stays_none(mwcli):
    m = mwcli
    tie = {"premarket": {"status": "ok"}, "entry_decision": {"choice": {"near": None, "reason": "tie"}}, "legs": []}
    o = m.derive_row(tie, bars=[])
    assert (o["A"], o["B"], o["A_minus_B"]) == (0.0, 0.0, 0.0) and o["category"] == "tie"
    o = m.derive_row({"premarket": {"status": "no_expiry"}, "legs": []}, bars=[])
    assert (o["A"], o["B"], o["A_minus_B"]) == (0.0, 0.0, 0.0)
    o = m.derive_row({"premarket": {"status": "premarket_input_stale"}, "legs": []}, bars=[])
    assert (o["A"], o["B"], o["A_minus_B"], o["A_bounds"]) == (None, None, None, None)     # 缺输入不折 0
    o = m.derive_row({"premarket": {"status": "ok"}, "entry_decision": None, "legs": []}, bars=[])
    assert o["A"] is None and o["category"] == "sdec_missing"


def test_opportunity_table_enumerates_missing_days():
    tab = mw.opportunity_table([{"session": "2026-10-19", "instrument": "gold", "outcome": {"category": "tie"}}],
                               ["gold", "silver"], date(2026, 10, 21))
    assert len(tab) == 6                                                     # 10/19–10/21 × 2
    assert [t["category"] for t in tab].count("not_generated") == 5
    assert mw.opportunity_table([], ["gold"], date(2026, 10, 16)) == []      # 起点之前不告警
    tab = mw.opportunity_table([], ["gold"], date(2026, 11, 30))
    assert "2026-11-26" not in {t["session"] for t in tab}                   # 感恩节休市不算应有格


def test_audit_flags_whole_day_never_run(mwcli, monkeypatch):
    m = mwcli
    monkeypatch.setattr(m, "_clock", lambda: datetime(2026, 10, 20, 17, 0, tzinfo=ET))

    class A:
        instruments = ["gold", "silver"]
    assert m.cmd_audit(A) == 1                                                # 空目录不能报「0 行、成功」
    assert m.cmd_report(A) == 0


def test_corrupt_attempts_log_is_reported_and_preserved(mwcli):
    m = mwcli
    m._append_attempt(_att("input_stale", "a"))
    ap = m._attempts_path()
    ap.write_bytes(ap.read_bytes() + b'{"key": "half\n')
    orig = ap.read_bytes()
    with pytest.raises(m.AttemptsCorrupt):
        m._append_attempt(_att("input_stale", "c"))
    assert ap.read_bytes() == orig and list(ap.parent.glob("capture_attempts.jsonl.corrupt-*"))

    m._ensure_denominator_row(_I, T)
    r = jl.load(m._path("silver"), "key")[0]
    assert r["premarket"]["status"] == "premarket_attempts_corrupt" and "不合法" in r["premarket"]["error"]


def _sample_env(monkeypatch, m, depth_ok: bool, row_mut=None, now=(11, 2, 0), step_s=1):
    import undertow.collect.longbridge_quote as lq
    import undertow.shadow_cli as sc
    from undertow.collect.longbridge_quote import Depth

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, *now, tzinfo=ET)
    monkeypatch.setattr(m, "datetime", FakeDT)
    monkeypatch.setattr(sc, "sample_bounds", lambda d: (585, 780))
    import itertools
    clk = (datetime(2026, 10, 16, *now, tzinfo=ET) + timedelta(seconds=i * step_s) for i in itertools.count())
    monkeypatch.setattr(m, "_now_iso", lambda: next(clk).isoformat())
    calls = {"depth": [], "quote": 0}

    def depth(syms):
        calls["depth"].append(list(syms))
        return {s: Depth(s, 0.3 if depth_ok else None, 5, 0.32 if depth_ok else None, 5, "" if depth_ok else "timeout")
                for s in syms}

    def quotes(syms):
        calls["quote"] += 1
        return {s: type("Q", (), {"last": 51.85, "freshest_kind": "常规", "timestamp": "t"})() for s in syms}
    monkeypatch.setattr(lq, "fetch_depth", depth)
    monkeypatch.setattr(lq, "fetch_stock_quotes", quotes)
    return calls


def test_sample_depth_failure_is_partial_then_retries_only_missing(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli)
    calls = _sample_env(monkeypatch, m, depth_ok=False)
    assert m.cmd_sample(A) == 1                                               # 标的成功不能掩盖盘口失败
    assert calls["quote"] == 1 and len(calls["depth"]) == 4                  # 逐合约请求，各记收到时刻
    calls = _sample_env(monkeypatch, m, depth_ok=True)
    assert m.cmd_sample(A) == 0
    assert calls["quote"] == 0 and len(calls["depth"]) == 4                   # 只补缺的 4 个合约，标的已有不重取
    smp = jl.load(p, "key")[0]["samples"]
    assert len(smp) == 2 and smp[1]["requested"] != smp[0]["requested"]     # 两次尝试都原样保留
    assert m.cmd_sample(A) == 0 and len(jl.load(p, "key")[0]["samples"]) == 2


def test_sample_without_applicable_legs_still_records_distance(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli)

    def drop(r):
        r["premarket"]["widths"] = {"P": None, "C": None}
        return True
    with jl.locked(p):
        rows = jl.load_path(p, "key"); drop(rows[0]); jl.atomic_write(p, rows, "key")
    calls = _sample_env(monkeypatch, m, depth_ok=False)
    assert m.cmd_sample(A) == 0 and calls["depth"] == []
    s = jl.load(p, "key")[0]["samples"][0]
    assert s["signed_dist_atr"]["P"] > 0 and s["not_applicable"] == {"P": "no_width", "C": "no_width"}


# ── 058：跨桶观测 / 尝试账严格校验 ──────────────────────────────────────────

@pytest.mark.parametrize("now", [(12, 59, 50), (10, 14, 50)])
def test_sample_members_received_after_slot_end_do_not_count(monkeypatch, mwcli, now):
    m, p, A = _e2e(monkeypatch, mwcli)
    _sample_env(monkeypatch, m, depth_ok=True, now=now, step_s=8)          # 第 2 次读钟起已越过桶末
    assert m.cmd_sample(A) == 1
    smp = jl.load(p, "key")[0]["samples"][0]
    assert smp["slot"] == ("12:45" if now[0] == 12 else "10:00")
    flags = [q["out_of_slot"] for q in smp["quotes"].values()] + [smp["underlying"]["out_of_slot"]]
    assert any(flags)                                                       # 原始尝试保留并标 out_of_slot
    if smp["underlying"]["out_of_slot"]:
        assert smp["signed_dist_atr"] == {"P": None, "C": None}


def test_sample_recovers_within_slot_after_failure(monkeypatch, mwcli):
    m, p, A = _e2e(monkeypatch, mwcli)
    _sample_env(monkeypatch, m, depth_ok=False, now=(11, 0, 0))
    assert m.cmd_sample(A) == 1
    _sample_env(monkeypatch, m, depth_ok=True, now=(11, 5, 0))
    assert m.cmd_sample(A) == 0
    smp = jl.load(p, "key")[0]["samples"]
    assert smp[0]["underlying"]["last"] and "underlying" not in smp[1]["requested"]   # 已合格成员不被覆盖、不重取


@pytest.mark.parametrize("bad", [b"null\n", b'{"key":"k","key":"k2","instrument":"silver","session":"s","status":"x","tried_at":"t"}\n',
                                 b'{"key":"k","instrument":"silver","session":"s","status":"x","tried_at":"t","v":NaN}\n',
                                 b"\xff\xfe\n", b'{"key":"k","instrument":"silver","session":"s"}\n'])
def test_attempts_log_rejects_bad_structure(mwcli, bad):
    m = mwcli
    m._append_attempt(_att("input_stale", "a"))
    ap = m._attempts_path()
    ap.write_bytes(ap.read_bytes() + bad)
    orig = ap.read_bytes()
    with pytest.raises(m.AttemptsCorrupt):
        m._load_attempts()
    assert ap.read_bytes() == orig and list(ap.parent.glob("capture_attempts.jsonl.corrupt-*"))
    m._ensure_denominator_row(_I, T)
    assert jl.load(m._path("silver"), "key")[0]["premarket"]["status"] == "premarket_attempts_corrupt"


def test_attempts_same_key_retries_allowed_and_write_rejects_nan(mwcli):
    m = mwcli
    for i in range(3):
        m._append_attempt(_att("input_stale", f"t{i}"))
    assert len(m._load_attempts()) == 3
    with pytest.raises(ValueError):
        m._append_attempt(_att("input_stale", "x") | {"v": float("nan")})
    with pytest.raises(ValueError):
        m._append_attempt({"key": "k"})
    assert len(m._load_attempts()) == 3


# ── 058 A：已到截止的应有覆盖（截止后第一次唤醒补查）──────────────────────────

D19 = date(2026, 10, 19)


def _row19(**kw):
    r = _premarket_row(); r["key"] = f"{D19}|silver"; r["session"] = D19.isoformat()
    r["premarket"]["E"] = "2026-10-21"
    r.update(kw)
    return r


def _cov(m, monkeypatch, now, rows=(), tmp=None):
    p = m._path("silver"); p.parent.mkdir(parents=True, exist_ok=True)
    for r in rows:
        jl.insert_frozen(p, r, key_field="key", frozen=m.frozen_part)
    monkeypatch.setattr(m, "_clock", lambda: now)

    class A:
        instruments = ["silver"]; status_file = None; run_id = None
    return A


def test_coverage_whole_open_window_missed_found_after_deadline(monkeypatch, mwcli, tmp_path, capsys):
    m = mwcli
    monkeypatch.chdir(tmp_path)
    A = _cov(m, monkeypatch, datetime(2026, 10, 19, 17, 0, tzinfo=ET), [_row19()])
    assert m.cmd_coverage(A) == 1
    out = capsys.readouterr().out
    assert "NEW_GAPS" in out and "sdec missing" in out
    fail = (tmp_path / "data/reports/FAILURE_2026-10-19.txt").read_text()
    assert "2026-10-19|silver|sdec" in fail and "2026-10-19|silver|sample:12:45" in fail
    assert m.cmd_coverage(A) == 1 and capsys.readouterr().out.strip().endswith("NEW_GAPS 0")    # 不重复提醒
    assert m.cmd_audit(A) == 1                                                                  # audit 不再 rc0


def test_coverage_before_deadline_and_next_day_catchup(monkeypatch, mwcli, tmp_path):
    m = mwcli
    monkeypatch.chdir(tmp_path)
    assert m.coverage_items(_row19(), D19, "silver", datetime(2026, 10, 19, 9, 0, tzinfo=ET)) == []
    items = m.coverage_items(_row19(), D19, "silver", datetime(2026, 10, 19, 10, 10, tzinfo=ET))
    assert [i["item"] for i in items] == ["capture"]                         # 开盘窗未结束：Sdec 未到截止
    A = _cov(m, monkeypatch, datetime(2026, 10, 20, 8, 0, tzinfo=ET))         # 10/19 一行都没有，10/20 早上补查
    assert m.cmd_coverage(A) == 1
    g = jl.load(m._gaps_path(), "key")
    assert any(x["id"] == "2026-10-19|silver|capture" and x["reason"] == "not_generated" for x in g)


def test_coverage_not_applicable_no_false_alarm(monkeypatch, mwcli, tmp_path):
    m = mwcli
    r = _row19(); r["premarket"] = {"status": "no_expiry"}
    items = m.coverage_items(r, D19, "silver", datetime(2026, 10, 19, 17, 0, tzinfo=ET))
    assert {i["item"]: i["status"] for i in items} == {"capture": "complete", "sdec": "not_applicable"}
    monkeypatch.setattr(m, "_clock", lambda: datetime(2026, 10, 18, 12, tzinfo=ET))       # 周末：起点前，无应有项
    assert m.coverage_all(["silver"], datetime(2026, 10, 18, 12, tzinfo=ET)) == []
    days = {i["session"] for i in m.coverage_all(["silver"], datetime(2026, 11, 30, 17, tzinfo=ET))}
    assert "2026-11-26" not in days and "2026-10-24" not in days                            # 休市日、周六不列


def test_coverage_half_day_sample_slots(mwcli):
    m = mwcli
    r = _row19(); r["session"] = "2026-11-27"
    items = m.coverage_items(r, date(2026, 11, 27), "silver", datetime(2026, 11, 27, 16, 0, tzinfo=ET))
    slots = [i["item"] for i in items if i["section"] == "sample"]
    assert slots and slots[-1] == "sample:12:30"                              # 13:00 收市日采样截到 12:45


def test_coverage_complete_day_reports_no_gap(monkeypatch, mwcli, tmp_path):
    m, p, A = _e2e(monkeypatch, mwcli)                                       # 10/16 有完整开盘窗
    assert m.coverage_items(None, T, "silver", datetime(2026, 10, 16, 9, 0, tzinfo=ET)) == []
    assert m.cmd_quote(A) == 0
    r = jl.load(p, "key")[0]
    items = m.coverage_items(r, T, "silver", datetime(2026, 10, 16, 10, 30, tzinfo=ET))
    st = {i["item"]: i["status"] for i in items}
    assert st["sdec"] == "complete" and all(v == "complete" for k, v in st.items() if k.startswith("entry:"))
