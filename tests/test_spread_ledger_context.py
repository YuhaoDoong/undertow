"""台账上下文：只记录不过滤，且不得前视。"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import spread_ledger as sl   # noqa: E402


def _series(n=320, seed=5):
    random.seed(seed)
    c = [100.0]
    for _ in range(n - 1):
        c.append(c[-1] * (1 + random.gauss(0, 0.01)))
    d0 = date(2025, 6, 1)
    dates = [d0 + timedelta(days=i) for i in range(n)]
    return [x * 1.01 for x in c], [x * 0.99 for x in c], c, dates


def test_decision_context_has_no_lookahead():
    h, l, c, dates = _series()
    session = dates[-1]
    base = sl.decision_context(h, l, c, dates, session)
    # 把 session 当天改成暴涨暴跌，上下文必须纹丝不动
    h2, l2, c2 = list(h), list(l), list(c)
    h2[-1] *= 1.5; l2[-1] *= 0.5; c2[-1] *= 1.4
    assert sl.decision_context(h2, l2, c2, dates, session) == base
    assert base["asof"] == dates[-2].isoformat(), "决策日是 session 的上一根"
    for k in ("atr14", "atr_expand_5", "atr_pct_250", "bb_width_20", "bb_expand_5"):
        assert base[k] is not None, k
    print("PASS test_decision_context_has_no_lookahead")


def test_decision_context_short_series_is_explicit():
    h, l, c, dates = _series(n=20)
    ctx = sl.decision_context(h, l, c, dates, dates[-1])
    assert ctx["asof"] is None and "不足" in ctx["note"], "数据不够要明说，不能给一堆 None 装作算过"
    print("PASS test_decision_context_short_series_is_explicit")


class _V:
    ok = False; reason = "test"; params = {}; all = []


def test_record_stores_context_and_old_rows_still_load(tmp_path):
    sl.record("silver", "SLV", date(2026, 9, 25), 58.0, _V(), root=tmp_path,
              context={"atr_expand_5": 1.31, "local_wall_P": {"strike": 57.0, "oi": 9000}})
    rows = sl.load("silver", root=tmp_path)
    assert rows[-1]["context"]["atr_expand_5"] == 1.31
    assert rows[-1]["context"]["local_wall_P"]["strike"] == 57.0
    # 旧行（无 context 字段）混在一起也要能 load / backfill
    p = tmp_path / "silver.jsonl"
    old = {"date": "2026-09-01", "inst": "silver", "sym": "SLV", "spot": 60.0, "ok": False,
           "reason": "x", "params": {}, "candidates": []}
    p.write_text(json.dumps(old) + "\n" + p.read_text("utf-8"), "utf-8")
    assert len(sl.load("silver", root=tmp_path)) == 2
    assert sl.backfill("silver", {}, root=tmp_path) == (0, 0)
    print("PASS test_record_stores_context_and_old_rows_still_load")
