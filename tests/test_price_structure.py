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
