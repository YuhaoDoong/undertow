"""到期日磁吸探索的纯计算：pull 定义、墙定义 W0/W1/W3、同日同方向同距离安慰剂、日期簇 bootstrap。"""
from datetime import date

import pytest

from undertow.analyze import expiry_pin as ep
from undertow.core.models import OptionContract


def C(e, k, kind, oi, g=0.01):
    return OptionContract(expiry=e, strike=k, kind=kind, open_interest=oi, volume=0, gamma=g, delta=0.5, iv=0.2)


D = date(2026, 9, 14)                       # 周一
W = date(2026, 9, 16)                       # D 类（周三）
M = date(2026, 9, 18)                       # 9 月第三个周五 → M
FAR = date(2026, 10, 30)


def test_pull_sign():
    assert ep.pull(100, 102, 103, 2) == pytest.approx(1.0)                 # 靠近 → 正
    assert ep.pull(100, 98, 103, 2) == pytest.approx(-1.0)                 # 远离 → 负
    assert ep.pull(100, 106, 103, 2) == pytest.approx(0.0)                 # 越过 K 同样距离 → 0


def test_wall_definitions():
    cs = [C(W, 100, "P", 500), C(W, 105, "C", 300), C(M, 105, "C", 400), C(M, 95, "P", 450),
          C(FAR, 90, "P", 10_000), C(M, 150, "C", 99_999)]                # 150 在 ±10% 外；FAR 超出 14 天
    assert ep.wall_w0(cs, D, 100) == 105                                   # 300 + 400 = 700 > 500
    assert ep.wall_w1(cs, D, 100) == (95, M)                               # 最近 M/Q 到期自身
    assert ep.max_oi_strike(cs, {W}, 100) == 100
    g = [C(W, 100, "P", 100, g=0.10), C(W, 105, "C", 500, g=0.01)]
    assert ep.wall_w3(g, D, 100) == 100 and ep.wall_w3(g, D, 100, "C") == 105


def test_tie_break_is_deterministic():
    cs = [C(W, 98, "P", 100), C(W, 103, "C", 100)]
    assert ep.max_oi_strike(cs, {W}, 100) == 98                            # 并列 → 离参考价近


def test_placebo_same_side_same_bin_excludes_wall():
    strikes = [96, 98, 99, 100.5, 101, 101.5, 103, 106]
    pl = ep.placebos(strikes, 100, 101, 2)                                 # K* 距 0.5 ATR → 分箱 1（[0.5,1)）
    assert pl == [101.5] and 101 not in pl                                  # 100.5 距 0.25 → 分箱 0；103 距 1.5 → 分箱 2
    r = ep.wall_row(100, 101.2, 2, 101, strikes)                           # 墙 (1−0.2)/2=0.4；安慰剂 (1.5−0.3)/2=0.6
    assert r["pull_wall"] == pytest.approx(0.4) and r["pull_placebo"] == pytest.approx(0.6)
    assert r["diff"] == pytest.approx(-0.2) and r["n_placebo"] == 1
    assert ep.wall_row(100, 101, 2, 106, [106])["diff"] is None             # 不匹配


def test_cluster_bootstrap_counts_clusters():
    rows = [{"d": 1, "v": 1.0}, {"d": 1, "v": 1.0}, {"d": 2, "v": -1.0}, {"d": 3, "v": None}]
    s = ep.cluster_bootstrap(rows, key=lambda r: r["v"], cluster=lambda r: r["d"], B=200)
    assert s["n"] == 3 and s["clusters"] == 2 and s["mean"] == pytest.approx(1 / 3)
    assert s["lo"] is None and "not_estimable" in s["interval"]                       # 少簇不给区间（Codex 026）
    rows = [{"d": i, "v": float(i % 3)} for i in range(12)]
    s = ep.cluster_bootstrap(rows, key=lambda r: r["v"], cluster=lambda r: r["d"], B=200)
    assert s["lo"] <= s["mean"] <= s["hi"]


def test_block_bootstrap_keeps_date_blocks_and_min_clusters():
    rows = [{"d": i, "v": 1.0 if i < 10 else -1.0} for i in range(20)]
    s1 = ep.block_bootstrap(rows, key=lambda r: r["v"], date_of=lambda r: r["d"], block=1, B=400)
    s5 = ep.block_bootstrap(rows, key=lambda r: r["v"], date_of=lambda r: r["d"], block=5, B=400)
    assert s1["mean"] == pytest.approx(0) and s5["interval"] == "moving_block"
    assert (s5["hi"] - s5["lo"]) >= (s1["hi"] - s1["lo"])                             # 块长大 → 区间更宽（序列相关）
    few = ep.block_bootstrap(rows[:3], key=lambda r: r["v"], date_of=lambda r: r["d"], block=2)
    assert few["lo"] is None and "not_estimable" in few["interval"]


# —— v3 同品种事前匹配 ——
def _bars(n=120, start=date(2026, 1, 1)):
    from datetime import timedelta
    ds, o = [], {}
    d = start
    while len(ds) < n:
        if d.weekday() < 5:
            ds.append(d)
            c = 100 + 0.1 * len(ds)                          # 平稳上行、日振幅 2
            o[d] = (c, c + 1, c - 1, c)
        d += timedelta(days=1)
    return ds, o


def test_match_features_use_only_prior_days():
    ds, o = _bars()
    f = ep.match_features(ds, o, ds[100], k=115.0)
    assert f["c_prev"] == o[ds[99]][3] and f["direction"] == "above" and f["trend20"] == 1
    o2 = dict(o); o2[ds[100]] = (999, 999, 1, 500)                                  # D 当天数据不影响特征
    assert ep.match_features(ds, o2, ds[100], k=115.0) == f
    assert ep.match_features(ds, o, ds[50], k=115.0) is None                        # 历史不足 60+15 日


def test_match_pairs_nearest_same_cell_within_window_and_reuse():
    F = lambda i, key=("above", 1, 1, 1, "unknown"): {"direction": key[0], "dist_bin": key[1], "vol_tercile": key[2],
                                                       "trend20": key[3], "event": key[4], "day_index": i}
    rows = [{"sym": "X", "d": "e1", "is_expiry": True, "feat": F(100)},
            {"sym": "X", "d": "e2", "is_expiry": True, "feat": F(103)},
            {"sym": "X", "d": "e3", "is_expiry": True, "feat": F(200)},                  # 窗口内无对照
            {"sym": "X", "d": "e4", "is_expiry": True, "feat": F(100, ("below", 1, 1, 1, "unknown"))},
            {"sym": "X", "d": "e5", "is_expiry": True, "feat": None},
            {"sym": "X", "d": "n1", "is_expiry": False, "feat": F(98)},
            {"sym": "X", "d": "n2", "is_expiry": False, "feat": F(102)},
            {"sym": "Y", "d": "n3", "is_expiry": False, "feat": F(100, ("below", 1, 1, 1, "unknown"))}]  # 跨品种不配
    m = ep.match_pairs(rows)
    got = {p["expiry"]["d"]: (p["control"]["d"], p["control_reuse"]) for p in m["pairs"]}
    assert got == {"e1": ("n1", 1), "e2": ("n2", 1)}                                # e1 等距（2 天）取日期早者 n1
    why = {u["d"]: u["why"] for u in m["unmatched"]}
    assert why == {"e3": "no_control_in_window", "e4": "no_control_same_cell", "e5": "history_lt_60"}
