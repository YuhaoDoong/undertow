"""Codex 008 G07：风控政策唯一来源；仓位组数受 Kelly、购买力、政策三者共同约束。"""
import math

import pytest

from undertow.analyze import risk_policy as rp
from undertow.analyze.sizing import kelly, size


def test_codex_repro_total_buying_power_now_binds():
    """复现：净资产 1000、每组 100、Kelly .5、购买力 100 → 旧版 ok、5 组、占用 500。"""
    k = kelly(0.9, 40.0, 50.0)
    assert k.kelly >= 0.5
    v = size(1000.0, 100.0, k, buying_power=100.0, unit_stop_loss=50.0)
    assert v.n_units * 100.0 <= 100.0, v


@pytest.mark.parametrize("kw,frag", [
    (dict(net_assets=float("nan"), unit_max_loss=10), "非有限"),
    (dict(net_assets=0.0, unit_max_loss=10), "≤ 0"),
    (dict(net_assets=1000.0, unit_max_loss=None), "未知"),
    (dict(net_assets=1000.0, unit_max_loss=float("inf")), "未知"),
    (dict(net_assets=1000.0, unit_max_loss=50, buying_power=float("nan"), unit_occupancy=50), "购买力"),
])
def test_unknown_inputs_give_zero(kw, frag):
    n, notes = rp.max_units(**kw)
    assert n == 0 and any(frag in x for x in notes)


def test_binding_constraints_reported():
    n, notes = rp.max_units(net_assets=1000.0, unit_max_loss=90.0, unit_stop_loss=30.0)
    assert n == 2 and "最大亏损" in notes[0]                  # 200//90=2，100//30=3，同簇 200//90=2
    n2, _ = rp.max_units(net_assets=1000.0, unit_max_loss=90.0, unit_stop_loss=30.0, cluster_open_max_loss=150.0)
    assert n2 == 0, "同簇已占 150，剩 50 < 90"
    assert any("未设定" in x for x in notes), "全账户上限未设定必须明说"


def test_current_account_cannot_open_anything():
    assert rp.max_units(net_assets=0.05, unit_max_loss=60.0)[0] == 0


def test_shadow_budget_delegates_to_policy():
    from undertow.analyze import shadow as sh
    assert sh.contracts_allowed(90.0, 1000.0) == 2
    assert sh.contracts_allowed(90.0, 1000.0, cluster_used=150.0) == 0
    src = open(sh.__file__, encoding="utf-8").read()
    assert "risk_policy" in src[src.index("def contracts_allowed"):]


def test_kelly_cannot_loosen_policy():
    k = kelly(0.95, 30.0, 35.0)                              # Kelly 很大
    v = size(1000.0, 300.0, k, buying_power=1000.0, unit_stop_loss=100.0, allow_over=True)
    assert not v.ok and "风控政策" in v.reason                # 最大亏 300 > 200
