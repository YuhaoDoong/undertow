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
    assert mw.condor({"near-P-A": {"entry_status": "valid"}}, {}) is None


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
    m._append_attempt({"key": f"{T}|silver", "status": "input_stale", "tried_at": "a"})
    m._append_attempt({"key": f"{T}|silver", "status": "snapshot_missing", "tried_at": "b"})

    class I:
        key = "silver"

        class options:
            symbol = "SLV"
    m._ensure_denominator_row(I, T)
    r = jl.load(m._path("silver"), "key")[0]
    assert r["premarket"]["status"] == "premarket_snapshot_missing" and r["premarket"]["last_attempt_at"] == "b"
    m._ensure_denominator_row(I, T)                                           # 幂等
    assert len(jl.load(m._path("silver"), "key")) == 1


def test_capture_late_or_stale_never_freezes(mwcli, monkeypatch):
    m = mwcli

    class I:
        key = "silver"

        class options:
            symbol = "SLV"

    class Store:
        def dates(self, kind, sym):
            return [T]

        def load_with_identity(self, kind, sym, d):
            return {"data": {}}, {"captured_at": datetime(2026, 10, 16, 6, 0, tzinfo=ET).timestamp(), "sha256": "x"}

        def load(self, *a):
            return None
    import undertow.collect.cboe_options as co
    monkeypatch.setattr(co, "snapshot_from_payload", lambda *a: type("S", (), {"contracts": []})())
    r, att = m.capture_one(I, T, datetime(2026, 10, 16, 9, 31, tzinfo=ET), store=Store())
    assert r == "late"
    import undertow.collect.fresh_daily as fd
    monkeypatch.setattr(fd, "resolve", lambda *a, **k: ({"status": "stale"}, None))
    r, att = m.capture_one(I, T, datetime(2026, 10, 16, 6, 30, tzinfo=ET), store=Store())
    assert r == "attempt_failed" and att["status"] == "input_stale"
    assert not m._path("silver").exists()                                   # 失败不占当天冻结键


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


def _e2e(monkeypatch, mwcli, kind="常规"):
    import itertools
    import undertow.collect.longbridge_quote as lq
    import undertow.shadow_cli as sc
    from undertow.collect.longbridge_quote import Depth
    m = mwcli
    p = m._path("silver"); p.parent.mkdir(parents=True)
    jl.insert_frozen(p, _premarket_row(), key_field="key", frozen=m.frozen_part)
    clock = (datetime(2026, 10, 16, 10, 5, tzinfo=ET) + timedelta(seconds=i) for i in itertools.count())
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

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, 11, 2, tzinfo=ET)
    monkeypatch.setattr(m, "datetime", FakeDT)
    monkeypatch.setattr(sc, "sample_bounds", lambda d: (585, 780))
    assert m.cmd_sample(A) == 0
    r = jl.load(p, "key")[0]
    s = r["samples"][0]
    assert s["signed_dist_atr"]["P"] == round((51.85 - 50) / 1.5, 6) and s["signed_dist_atr"]["C"] > 0
    assert "P|50" in s["quotes"] and "C|60" in s["quotes"]
    assert m.cmd_sample(A) == 0 and len(jl.load(p, "key")[0]["samples"]) == 1     # 同一桶不重复记
