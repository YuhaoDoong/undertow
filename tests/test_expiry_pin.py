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
    assert s["lo"] <= s["mean"] <= s["hi"]
