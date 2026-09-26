"""第四步脚本的行为锁：技术指标能否提示破墙（scripts/step4_filters.py）。

它不进模块，但它的结论会被引用（docs/wall_spread_3steps.md 第四步），
所以口径必须锁住：检验族大小、无前视、统计件不会静默退化。
"""
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("step4_filters", ROOT / "scripts" / "step4_filters.py")
s4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s4)


def test_family_matches_tested_cells():
    """Bonferroni 的族大小必须等于实际检验的格数 —— 少算就是偷显著性。"""
    cells = sum(len(tails) for _, tails in s4.INDICATORS.values()) * 2   # × 两侧
    assert s4.FAMILY == cells == 36
    assert abs(s4.ALPHA_BONF - 0.05 / 36) < 1e-12
    for name, (_, tails) in s4.INDICATORS.items():
        assert len(tails) == 2, f"{name} 必须恰好两端尾部状态"
    print("PASS test_family_matches_tested_cells")


def test_state_functions_map_tails():
    f = s4.INDICATORS["RSI14"][0]
    assert f(25) == "超卖≤30" and f(75) == "超买≥70" and f(50) == "中"
    f = s4.INDICATORS["ATR扩张比"][0]
    assert f(1.3) == "扩张≥1.3" and f(0.8) == "收缩≤0.8" and f(1.0) == "中"
    f = s4.INDICATORS["超买超卖"][0]
    assert f(0.01) == "极超卖" and f(0.99) == "极超买" and f(0.5) == "中性"
    print("PASS test_state_functions_map_tails")


def test_two_prop_sanity():
    z, p = s4.two_prop(50, 1000, 50, 1000)
    assert abs(z) < 1e-9 and p > 0.99
    z, p = s4.two_prop(200, 1000, 50, 1000)
    assert z > 5 and p < 1e-6
    assert s4.two_prop(1, 3, 1, 3) == (0.0, 1.0), "样本不足必须退化为不显著，不能抛错或给 0"
    print("PASS test_two_prop_sanity")


def test_ratio_series():
    assert s4._ratio_series([1, 2, 4, 8], 1) == [None, 2.0, 2.0, 2.0]
    assert s4._ratio_series([None, 2, 4], 1) == [None, None, 2.0]
    assert s4._ratio_series([0, 2], 1) == [None, None], "分母为 0 必须给 None"
    print("PASS test_ratio_series")


def test_effect_and_test_use_same_observations_and_comparator():
    """全样本给正效应、固定抽样却给负效应时，主比值必须跟检验走。"""
    keys = [(side, k, b) for side in ("down", "up") for k in s4.KS
            for b in ("3%", "5%", "7%", "1.5ATR", "2ATR", "3ATR")]
    rows = []
    for i in range(60):
        state = i % 6 < 3
        breach = (i % 6 in (1, 2, 3)) or i == 0
        rows.append({"i": i, "expand": float(i),
                     "states": {n: tails[0 if state else 1] for n, (_, tails) in s4.INDICATORS.items()},
                     "out": {key: breach for key in keys}})
    _, tests, _, n = s4.analyse(rows)
    t = tests[("RSI14", "超卖≤30", "down")]
    assert n == 20 and t["n_state"] == t["n_rest"] == 10
    assert t["events_state"] == 1 and t["events_rest"] == 10
    assert t["raw_ratio"] > 1, "原始描述必须保留，供审计差异"
    assert t["ratio"] == pytest.approx(0.1) and t["z"] < 0
    assert t["ratio"] == t["ratios_all"]["down|k3|2ATR"]
    assert t["ratio"] == t["rate_state"] / t["rate_rest"]


def test_unexamined_buffer_cannot_borrow_primary_p():
    t = {"bonf": True, "p": 0.000001}
    assert s4.test_mark(t, "2ATR") == "**"
    assert s4.test_mark(t, "5%") == ""
    assert s4.test_mark(t, "3ATR") == ""


class _Ser:
    def __init__(self, closes):
        self.symbol = "SYN"; self.closes = closes
        self.highs = [c * 1.001 for c in closes]; self.lows = [c * 0.999 for c in closes]
        import datetime as dt
        d0 = dt.date(2020, 1, 1)
        self.dates = [d0 + dt.timedelta(days=i) for i in range(len(closes))]


def test_no_lookahead_in_outcomes():
    """第 i 根的结果只能看 i+1..i+k，绝不能含第 i 根自己 —— 含了就是前视。

    造一根 +50% 的尖峰在第 600 根：k=3 时只有 i∈{597,598,599} 该标「上破」，
    i=600 自己（尖峰当天）以及 i≤596 都不该。
    """
    random.seed(3)
    c = [100.0]
    for _ in range(699):
        c.append(c[-1] * (1 + random.gauss(0, 0.0005)))      # 噪音极小，3 天动不了 5%
    c[600] = c[599] * 1.5
    for j in range(601, 700):
        c[j] = c[600] * (1 + random.gauss(0, 0.0005))
    rows = s4.build(_Ser(c))
    by_i = {r["i"]: r for r in rows}
    assert 599 in by_i and 600 in by_i and 596 in by_i, "样本宇宙应覆盖尖峰前后"
    for i in (597, 598, 599):
        assert by_i[i]["out"][("up", 3, "5%")] is True, i
    assert by_i[600]["out"][("up", 3, "5%")] is False, "尖峰当天自身不得算作前瞻结果（前视）"
    assert by_i[596]["out"][("up", 3, "5%")] is False, "k=3 看不到第 600 根"
    assert by_i[596]["out"][("up", 4, "5%")] is True, "k=4 才看得到"
    print("PASS test_no_lookahead_in_outcomes")


def test_emitted_artifact_is_consistent():
    """落盘产物是文档引用的唯一来源，schema 与族大小必须与脚本一致。"""
    p = ROOT / "data" / "history" / "wall_spread" / "filter_test.json"
    if not p.exists():
        pytest.skip("尚未 --emit")
    d = json.loads(p.read_text("utf-8"))
    # schema 2 是保留的历史产物，修正脚本不会自动覆盖旧研究证据。
    assert d["schema"] in (2, 3) and d["family"] == s4.FAMILY
    assert set(d["summary_by_buffer"]) == {"2ATR", "5%"}, "两套口径都必须在，缺一套就没法识别缩放效应"
    for bb, rows in d["summary_by_buffer"].items():
        assert len(rows) == s4.FAMILY, (bb, len(rows))
    assert len(d["instruments"]) >= 15
    if d["schema"] == 3:
        for inst in d["instruments"].values():
            for t in inst["tests"].values():
                if t["rate_rest"] > 0:
                    assert t["ratio"] == pytest.approx(t["rate_state"] / t["rate_rest"])
    print("PASS test_emitted_artifact_is_consistent")
