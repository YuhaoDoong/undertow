"""价格结构状态机（ps-explore-v0.3）：前缀不变性、道氏示例、跳空/双触及/开盘越止损、H2 含从未突破的 episode。全部合成数据。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import price_structure as ps  # noqa: E402


def mk(closes, spread=0.2, start=date(2000, 1, 3)):
    out, d, prev = [], start, closes[0]
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c; d += timedelta(days=1)
    return out


def path(*legs, n=4, flat=20, base=100.0):
    """按节点线性插值的收盘路径；前置 flat 根平盘用于预热。"""
    c = [base] * flat
    for a, b in zip(legs[:-1], legs[1:]):
        c += [a + (b - a) * k / n for k in range(1, n + 1)]
    return c


def test_prefix_invariance():
    closes = path(100, 90, 95, 82, 88, 76, 84, 79, 98, 90, 104, 96, 110, 99, 92, 101)
    bars = mk(closes)
    full_p, full_e = ps.detect(bars)
    for cut in range(30, len(bars)):
        pp, ee = ps.detect(bars[:cut])
        assert [(p.kind, p.idx, p.conf) for p in pp] == [(p.kind, p.idx, p.conf) for p in full_p if p.conf < cut]
        # 截至 cut 已产生的信号与完整数据一致
        sig = lambda es: sorted((e.kind, e.direction, e.start, e.signal_at) for e in es if e.signal_at is not None and e.signal_at < cut)
        assert sig(ee) == sig(full_e)


def test_dow_example_type_a_signal():
    """示例一：下降结构 → 突破关键 LH → 回踩形成高于 LL 的 HL₁ → 信号（A 型）。"""
    closes = path(100, 90, 96, 84, 90, 80, 112, 96, 118, 105, n=6)
    bars = mk(closes, spread=0.1)
    piv, eps = ps.detect(bars)
    a = [e for e in eps if e.kind == "A" and e.direction == "long"]
    assert a and a[0].status == "signal" and a[0].hl1.price > a[0].extreme.price
    assert a[0].hl1.idx > a[0].break_at                                         # 回踩低点晚于突破
    assert abs(a[0].key.price - 90) < 1e-9 and abs(a[0].extreme.price - 80) < 1e-9   # 关键 LH = LL 之前的更低高点


def test_dow_example_type_b_h2_registered_at_hl1():
    """示例二：先出 HL₁（88）再突破关键 LH → A 型不触发；H2 在 HL₁ 确认当日就登记；等待政策在 HL₂ 给信号。"""
    closes = path(100, 90, 110, 84, 103, 80, 95, 88, 108, 99, 115, n=6)
    bars = mk(closes, spread=0.1)
    piv, eps = ps.detect(bars)
    h2 = [e for e in eps if e.kind == "H2" and e.direction == "long"]
    assert h2 and abs(h2[0].hl1.price - 88) < 1e-9 and h2[0].start == h2[0].hl1.conf
    assert h2[0].policy_w_status == "signal" and h2[0].hl2.price > 88
    assert not [e for e in eps if e.kind == "A" and e.direction == "long" and e.start == h2[0].break_at]


def test_h2_kept_when_never_breaks():
    """HL₁ᴮ 之后从未突破、再创新低 → episode 仍在，等待政策记失效，不从分母删除。"""
    closes = path(100, 90, 110, 84, 103, 80, 95, 88, 94, 70, n=6)
    piv, eps = ps.detect(mk(closes, spread=0.1))
    h2 = [e for e in eps if e.kind == "H2" and e.direction == "long"]
    assert h2 and h2[0].policy_w_status == "invalidated_close_beyond_extreme" and h2[0].break_at is None


def test_short_side_mirror():
    closes = path(100, 110, 104, 116, 110, 120, 88, 104, 82, 95, n=6)
    piv, eps = ps.detect(mk(closes, spread=0.1))
    s = [e for e in eps if e.kind == "A" and e.direction == "short"]
    assert s and s[0].status == "signal" and s[0].hl1.price < s[0].extreme.price


def B(o, h, l, c, d=date(2001, 1, 1)):
    return (d, o, h, l, c)


def test_simulate_gap_double_touch_open_beyond_stop_target_gap():
    bars = [B(100, 101, 99, 100), B(100, 101, 99, 100), B(95, 96, 94, 95)]
    r = ps.simulate(bars, 1, 98.0, "long", last_i=2, hold=2)
    assert r["exit_reason"] == "gap_stop" and r["exit_price"] == 95 and r["r_mult"] < -1     # 跳空穿止损按开盘
    bars = [B(100, 101, 99, 100), B(100, 105, 97, 100)]
    assert ps.simulate(bars, 1, 98.0, "long", last_i=1, hold=1)["exit_reason"] == "stop_both_touched"
    assert ps.simulate(bars, 1, 101.0, "long", last_i=1, hold=1)["status"] == "open_beyond_stop"
    bars = [B(100, 101, 99, 100), B(100, 101, 99.5, 100), B(110, 111, 109, 110)]
    r = ps.simulate(bars, 1, 98.0, "long", last_i=2, hold=2)
    assert r["exit_reason"] == "target" and r["exit_price"] == 104.0 and r["r_mult"] == 2.0     # 开盘跳过目标按限价目标
    assert ps.simulate(bars, 1, 98.0, "long", last_i=1, hold=2)["status"] == "boundary_excluded"


def test_barrier_survival_equality_and_incomplete():
    bars = [B(100, 101, 99, 100), B(100, 101, 98, 98.0), B(99, 100, 97, 99)]
    assert ps.barrier_survival(bars, 1, 98.0, "long", 2) is True                 # 收盘等于止损不算越过
    assert ps.barrier_survival(bars, 1, 98.5, "long", 2) is False
    assert ps.barrier_survival(bars, 1, 98.0, "long", 5) is None


def test_ps_explore_refuses_existing_run_dir(tmp_path, monkeypatch):
    """Codex 038：run-id 必填，目录已存在拒绝运行（不覆盖已看过的结果）。"""
    import pytest
    from scripts import ps_explore as pe
    monkeypatch.setattr(pe, "BASE", tmp_path)
    (tmp_path / "runX").mkdir()
    monkeypatch.setattr(sys, "argv", ["ps_explore.py", "--run-id", "runX"])
    with pytest.raises(SystemExit) as e:
        pe.main()
    assert "已存在" in str(e.value)


def test_h2_policy_value_common_denominator():
    """规则拒绝入场 → 0R；缺数据/窗口不完整 → None（不折零）。"""
    from scripts import ps_explore as pe
    assert pe._policy_value("entered", 1.0) == 1.0
    assert pe._policy_value("open_beyond_stop", None) == 0.0 and pe._policy_value("timeout_no_entry", None) == 0.0
    assert pe._policy_value("boundary_excluded", None) is None and pe._policy_value(None, None) is None


def test_fractal_pivots_confirmation_plateau_and_ambiguity():
    """structure-v0：左右各 2 根；极值 i 在 i+2 才确认；平台相等取最早；同根高低都满足标 ambiguous_order 且不进交替投影。"""
    hl = [(10, 9), (11, 10), (13, 11), (13, 11.5), (12, 10.5), (11, 9.5), (12, 10), (14, 9), (12, 8.8), (11, 9.6), (13, 10)]
    bars = [(date(2001, 1, 1) + timedelta(days=i), (h + l) / 2, h, l, (h + l) / 2) for i, (h, l) in enumerate(hl)]
    piv = ps.fractal_pivots(bars)
    H = [p for p in piv if p.kind == "H"]
    assert H[0].idx == 2 and H[0].conf == 4 and H[0].price == 13            # 平台 13、13 取最早一根
    seq = ps.alternating(piv)
    assert all(a.kind != b.kind for a, b in zip(seq, seq[1:]))             # 交替
    # 前缀不变性：截到第 t 根只看得到 conf ≤ t−1 的点
    for cut in range(3, len(bars) + 1):
        pp = ps.fractal_pivots(bars[:cut])
        assert [(p.kind, p.idx) for p in pp] == [(p.kind, p.idx) for p in piv if p.conf < cut]


def test_fractal_same_bar_high_and_low_is_ambiguous():
    hl = [(10, 9), (10.5, 9.2), (12, 8), (11, 9), (10.8, 9.1)]                 # 第 2 根同时最高、最低（外包线）
    bars = [(date(2001, 1, 1) + timedelta(days=i), (h + l) / 2, h, l, (h + l) / 2) for i, (h, l) in enumerate(hl)]
    piv = ps.fractal_pivots(bars)
    assert {(p.kind, p.idx, p.ambiguous_order) for p in piv} == {("H", 2, True), ("L", 2, True)}
    assert ps.alternating(piv) == []


# ---------------- 出场政策 P0–P4（ps-exit-v1，Codex 040 合成验收） ----------------
P = ps.Pivot


def flat(n, px=100.0):
    return [B(px, px + 0.5, px - 0.5, px, date(2001, 1, 1) + timedelta(days=k)) for k in range(n)]


def test_old_point_confirmed_after_entry_does_not_trigger_exit():
    bars = flat(80)
    piv = [P("H", 2, 3, 105.0), P("H", 4, 12, 103.0)]          # 第二个高点极值在入场(5)之前，入场后才确认
    r = ps.simulate_policy(bars, piv, 5, 98.0, "long", ps.POLICIES["P2"], last_i=79)
    assert r["exit_reason"] == "time_exit" or r["exit_i"] != 13


def test_lh_confirmation_exits_next_open_and_stop_wins_same_bar():
    bars = flat(80)
    piv = [P("H", 2, 3, 105.0), P("H", 8, 10, 103.0)]          # 入场后形成、第 10 根确认的 LH
    r = ps.simulate_policy(bars, piv, 5, 98.0, "long", ps.POLICIES["P2"], last_i=79)
    assert r["exit_reason"] == "struct_exit" and r["exit_i"] == 11 and r["exit_price"] == bars[11][1]
    assert r["audit"]["struct_point"]["ref_idx"] == 2              # 前一个同类点在入场前，有审计引用
    bars2 = list(bars); bars2[10] = B(100, 100.5, 97.0, 99, bars[10][0])   # 确认当根先触及止损
    r2 = ps.simulate_policy(bars2, piv, 5, 98.0, "long", ps.POLICIES["P2"], last_i=79)
    assert r2["exit_reason"] == "stop" and r2["exit_i"] == 10


def test_hh_breakeven_takes_effect_next_bar_not_same_bar():
    bars = flat(80)
    piv = [P("H", 2, 3, 99.0), P("H", 8, 10, 101.0)]           # HH 第 10 根确认 → 第 11 根起止损 = 入场价
    bars[10] = B(100, 100.5, 99.6, 100, bars[10][0])              # 确认当根低点 99.6 < 入场 100：当根不得按成本止损
    r = ps.simulate_policy(bars, piv, 5, 98.0, "long", ps.POLICIES["P3"], last_i=79)
    # 第 10 根（确认当根）低点 99.6 < 入场价不触发；第 11 根起成本止损生效，平盘低点 99.5 → 第 11 根按成本价出
    assert r["exit_i"] == 11 and r["exit_reason"] in ("breakeven_stop", "breakeven_gap_stop") and r["exit_price"] == 100.0
    assert r["breakeven_from_i"] == 11


def test_gap_open_struct_and_stop_single_fill_and_reject_and_boundary():
    bars = flat(80)
    piv = [P("H", 2, 3, 105.0), P("H", 8, 10, 103.0)]
    bars[11] = B(96, 96.5, 95.5, 96, bars[11][0])                 # 结构离场意图 + 开盘跳空穿止损
    r = ps.simulate_policy(bars, piv, 5, 98.0, "long", ps.POLICIES["P2"], last_i=79)
    assert r["exit_reason"] == "struct_exit+gap_stop" and r["exit_price"] == 96 and r["r_mult"] < -1
    assert ps.simulate_policy(bars, piv, 5, 100.5, "long", ps.POLICIES["P2"], last_i=79)["status"] == "open_beyond_stop"
    assert ps.simulate_policy(bars, piv, 5, 98.0, "long", ps.POLICIES["P4"], last_i=79)["status"] == "boundary_excluded"


def test_p0_matches_original_simulate():
    import random
    rng = random.Random(7)
    px, bars = 100.0, []
    for k in range(200):
        o = px; px *= 1 + rng.gauss(0, 0.01)
        bars.append(B(o, max(o, px) * 1.003, min(o, px) * 0.997, px, date(2001, 1, 1) + timedelta(days=k)))
    atr = ps.wilder_atr(bars)
    piv = ps.zigzag(bars, atr)
    for e in range(30, 120, 7):
        stop = bars[e][1] * 0.97
        a = ps.simulate(bars, e, stop, "long", last_i=199)
        b = ps.simulate_policy(bars, piv, e, stop, "long", ps.POLICIES["P0"], last_i=199)
        assert (a["exit_i"], a["exit_reason"], a["r_mult"]) == (b["exit_i"], b["exit_reason"], b["r_mult"])


def test_policy_no_open_and_short_mirror_struct_exit():
    """Codex 041：缺开盘价显式 no_open；做空镜像：持仓后新形成并确认的更高低点（HL）→ 下一根开盘离场；审计保留参照点。"""
    bars = flat(80)
    bars[5] = (bars[5][0], None, 100.5, 99.5, 100)
    assert ps.simulate_policy(bars, [], 5, 103.0, "short", ps.POLICIES["U1"], last_i=79)["status"] == "no_open"
    bars = flat(80)
    piv = [P("L", 2, 3, 95.0), P("L", 8, 10, 97.0)]              # 第二个低点更高 = HL（做空离场信号）
    r = ps.simulate_policy(bars, piv, 5, 103.0, "short", ps.POLICIES["U1"], last_i=79)
    assert r["exit_reason"] == "struct_exit" and r["exit_i"] == 11
    a = r["audit"]["struct_point"]
    assert (a["ref_idx"], a["decided_at"], a["execute_at"]) == (2, 10, 11)
    assert ps.simulate_policy(bars, piv, 5, 103.0, "short", ps.POLICIES["N60"], last_i=79)["exit_reason"] == "time_exit"


def test_armed_lh_exit_waits_for_new_hh_after_entry():
    """ps-exit-v4（用户：要等入场后的新 HH）：入场后第一个反弹没过旧高（103）不离场；创出新 HH(106) 后，低于 106 的高点(104)才离场；
    HH 继续抬高时参照跟着抬高。"""
    bars = flat(80)
    piv = [P("H", 2, 3, 103.0), P("H", 8, 10, 101.0), P("H", 14, 16, 106.0), P("H", 20, 22, 104.0)]
    r = ps.simulate_policy(bars, piv, 5, 95.0, "long", ps.POLICIES["U5"], last_i=79)
    assert r["exit_reason"] == "struct_exit" and r["exit_i"] == 23
    assert r["audit"]["armed_at"]["price"] == 106.0 and r["audit"]["struct_point"]["ref_price"] == 106.0
    piv2 = [P("H", 2, 3, 103.0), P("H", 14, 16, 106.0), P("H", 20, 22, 108.0), P("H", 26, 28, 107.0)]
    r2 = ps.simulate_policy(bars, piv2, 5, 95.0, "long", ps.POLICIES["U5"], last_i=79)
    assert r2["exit_i"] == 29 and r2["audit"]["struct_point"]["ref_price"] == 108.0          # 参照随 HH 抬高
    r3 = ps.simulate_policy(bars, piv[:2], 5, 95.0, "long", ps.POLICIES["U5"], last_i=79)
    assert r3["exit_reason"] == "time_exit"                                                    # 没有新 HH：不上膛、只靠 LL 止损
