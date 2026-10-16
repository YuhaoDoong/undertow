"""按来向区分的压力/支撑区间（dz-explore-v1）：R/S 区识别、启用/触及/首次通过、安慰剂方向、去重、前缀不变性。全部合成数据。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import directional_zones as dz  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402


def bars_from(rows, start=date(2000, 1, 3)):
    """rows: [(o, h, l, c)] → 带日期的 bars。"""
    out, d = [], start
    for r in rows:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, *r))
        d += timedelta(days=1)
    return out


def flat(c, n, w=0.3):
    return [(c, c + w, c - w, c)] * n


ATR1 = lambda n: [1.0] * n  # noqa: E731


def test_r_zone_first_mid_decline_box():
    rows = flat(110, 3) + [(110, 110.2, 104, 104), (104, 104.2, 101, 101)] + flat(100, 5) + [(100, 100.2, 96, 96), (96, 96.2, 95, 95)] + flat(95, 3)
    b = bars_from(rows)
    piv = [ps.Pivot("H", 2, 4, 110.0), ps.Pivot("L", 11, 14, 95.0)]
    z = dz.find_zones(b, ATR1(len(b)), piv)
    assert len(z) == 1 and z[0].kind == "R"
    assert (z[0].lo, z[0].hi) == (99.7, 100.3)
    assert z[0].active == 14 and z[0].formed == 9


def test_r_zone_rejects_bottom_box():
    # 横盘后只再跌 0.5（< 1·ATR）→ 底部横盘，不算「途中」
    rows = flat(110, 3) + [(110, 110.2, 104, 104), (104, 104.2, 100, 100)] + flat(99.5, 5) + [(99.5, 99.6, 99, 99)] + flat(99, 3)
    b = bars_from(rows)
    piv = [ps.Pivot("H", 2, 4, 110.0), ps.Pivot("L", 10, 13, 99.0)]
    assert dz.find_zones(b, ATR1(len(b)), piv) == []


def test_s_zone_is_middle_of_first_big_bull_body():
    rows = flat(100, 3) + [(100, 100.8, 99.9, 100.6), (100.6, 102.7, 100.5, 102.6), (102.6, 106, 102.5, 105.9)] + flat(106, 3)
    b = bars_from(rows)
    piv = [ps.Pivot("L", 2, 4, 100.0), ps.Pivot("H", 5, 8, 105.9)]
    z = dz.find_zones(b, ATR1(len(b)), piv)
    assert len(z) == 1 and z[0].kind == "S"
    # 第一根实体 ≥1 ATR 的是 100.6→102.6（第 4 根 0.6 不够），中段 = [101.1, 102.1]
    assert abs(z[0].lo - 101.1) < 1e-9 and abs(z[0].hi - 102.1) < 1e-9 and z[0].formed == 4


def _zone_r(active=0):
    return dz.Zone("R", 100.0, 101.0, 0, active, 1.0, (0, 0))


def test_evaluate_r_reversed_broken_crossed():
    pre = flat(97, 3)
    up = [(97, 98, 96.8, 98), (98, 100.2, 97.9, 99.8)]          # 第 4 根触及下沿
    down = [(99.8, 99.9, 98, 98.2)]                            # 收盘 98.2 ≤ 100 − 1.5
    b = bars_from(pre + up + down + flat(98, 30))
    r = dz.evaluate(b, ATR1(len(b)), _zone_r(active=2))
    assert r["status"] == "event" and r["touch"] == 4 and r["outcome"] == "reversed"
    assert abs(r["ret10_atr"] - 2.0) < 1e-9                   # −(98 − 100)/1

    brk = [(99.8, 101.8, 99.7, 101.6)]                         # 收盘 101.6 ≥ 101 + 0.5
    b2 = bars_from(pre + up + brk + flat(101.6, 30))
    assert dz.evaluate(b2, ATR1(len(b2)), _zone_r(active=2))["outcome"] == "broken"

    b3 = bars_from(flat(100.5, 40))
    assert dz.evaluate(b3, ATR1(len(b3)), _zone_r(active=2))["status"] == "crossed_before"


def test_evaluate_none_and_incomplete():
    b = bars_from(flat(97, 3) + [(97, 100.1, 96.9, 99.5)] + flat(99.5, 25))
    assert dz.evaluate(b, ATR1(len(b)), _zone_r(active=2))["outcome"] == "none"
    short = bars_from(flat(97, 3) + [(97, 100.1, 96.9, 99.5)] + flat(99.5, 5))
    assert dz.evaluate(short, ATR1(len(short)), _zone_r(active=2))["status"] == "incomplete"


def test_evaluate_s_mirror():
    z = dz.Zone("S", 100.0, 101.0, 0, 2, 1.0, (0, 0))
    b = bars_from(flat(104, 3) + [(104, 104, 100.8, 101.2), (101.2, 103, 101, 102.6)] + flat(102.6, 30))
    r = dz.evaluate(b, ATR1(len(b)), z)
    assert r["touch"] == 3 and r["outcome"] == "reversed"


def test_placebo_shift_direction():
    r = _zone_r()
    assert dz.shifted(r, 1).lo == 102.0 and dz.shifted(r, -1).lo == 98.0
    s = dz.Zone("S", 100.0, 101.0, 0, 0, 1.0, (0, 0))
    assert dz.shifted(s, 1).lo == 98.0                         # S 区「更远」= 更低


def test_dedupe_same_kind_only():
    mk = lambda k: dz.Zone(k, 0, 1, 0, 0, 1.0, (0, 0))         # noqa: E731
    ev = [(mk("R"), {"touch": 10}), (mk("R"), {"touch": 13}), (mk("S"), {"touch": 12}), (mk("R"), {"touch": 15})]
    kept = dz.dedupe(ev)
    assert [(z.kind, r["touch"]) for z, r in kept] == [("R", 10), ("S", 12), ("R", 15)]


def test_zones_prefix_invariant():
    import math
    closes = [100 + 8 * math.sin(i / 6) + 3 * math.sin(i / 2.3) for i in range(260)]
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + 0.3, min(prev, c) - 0.3, c))
        prev = c
    b = bars_from(rows)
    atr = ps.wilder_atr(b)
    full = dz.find_zones(b, atr, ps.zigzag(b, atr))
    assert full, "合成路径应产生区间"
    for cut in range(60, len(b), 7):
        bc = b[:cut]
        ac = ps.wilder_atr(bc)
        part = dz.find_zones(bc, ac, ps.zigzag(bc, ac))
        key = lambda zs: [(z.kind, round(z.lo, 9), round(z.hi, 9), z.active) for z in zs]  # noqa: E731
        assert key(part) == key([z for z in full if z.active < cut])


def test_pb_z_null_is_placebo_not_half():
    from scripts.dz_explore import pb_z
    # 真实成功率正好等于安慰剂概率 0.7 → z≈0；旧符号检验会把 70% 当成显著高于 50%
    z, p = pb_z([1] * 70 + [0] * 30, [0.7] * 100)
    assert abs(z) < 1e-9 and p > 0.99
    z, p = pb_z([1] * 90 + [0] * 10, [0.7] * 100)
    assert z > 4 and p < 1e-4
