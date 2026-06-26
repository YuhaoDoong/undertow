"""期权资金流 / 持仓异动 + 快照仓库 单元测试（合成数据，不依赖网络）。

验证：单快照异常活跃扫描、两日 ΔOI/ΔIV diff、方向分类、净倾向、墙位叠加、
全新行标注、近价/近月过滤，以及 SnapshotStore 的落盘/读回。
运行: python tests/test_flow.py  或  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading_intel.models import OptionsSnapshot, OptionContract
from trading_intel.analysis.flow import analyze_flow, scan_unusual, _judge
from trading_intel.store import SnapshotStore

TODAY = date(2026, 6, 26)
EXP = date(2026, 7, 17)  # 21 天后，在 60 天近月窗口内


def _c(strike, kind, oi, vol=0, iv=0.20, expiry=EXP) -> OptionContract:
    return OptionContract(expiry=expiry, strike=strike, kind=kind,
                          open_interest=oi, volume=vol, gamma=0.0, delta=0.0, iv=iv)


def _snap(contracts, spot=100.0) -> OptionsSnapshot:
    return OptionsSnapshot(instrument="t", proxy_symbol="T", spot=spot,
                           asof="2026-06-26", contracts=contracts)


def test_scan_unusual_flags_high_vol_oi():
    snap = _snap([
        _c(95, "P", oi=2000, vol=5000),   # 量/OI=2.5 → 异常活跃
        _c(105, "C", oi=1000, vol=30),    # 量低于门槛 → 不计
        _c(50, "P", oi=10, vol=9999),     # 远 OTM（超 ±15%）→ 过滤
    ])
    out = scan_unusual(snap, today=TODAY)
    strikes = {(u.strike, u.kind) for u in out}
    assert (95.0, "P") in strikes
    assert (105.0, "C") not in strikes  # 成交量不足
    assert (50.0, "P") not in strikes   # 远 OTM 被近价带过滤
    u = next(u for u in out if u.strike == 95.0)
    assert abs(u.vol_oi_ratio - 2.5) < 1e-6


def test_flow_diff_classifies_and_tilts():
    prev = _snap([
        _c(95, "P", oi=200, iv=0.20),
        _c(105, "C", oi=100, iv=0.20),
        _c(110, "C", oi=500, iv=0.20),
    ])
    curr = _snap([
        _c(95, "P", oi=2000, vol=5000, iv=0.2113),  # +1800，IV +1.13pp，看跌增建
        _c(105, "C", oi=150, iv=0.20),              # +50，看涨增建
        _c(110, "C", oi=480, iv=0.20),              # -20，低于阈值被过滤
        _c(90, "P", oi=300, iv=0.20),               # 昨日无此行（全新看跌建仓）
        _c(50, "P", oi=9999, iv=0.20),              # 远 OTM 过滤
    ])
    fa = analyze_flow(prev, curr, today=TODAY, put_wall=95.0, call_wall=110.0,
                      prev_date="2026-06-25", curr_date="2026-06-26")
    by = {(c.strike, c.kind): c for c in fa.changes}
    # 看跌增建、墙位叠加、IV 升
    p95 = by[(95.0, "P")]
    assert p95.d_oi == 1800 and p95.bias == "bearish" and p95.on_wall == "put墙"
    assert abs(p95.d_iv_pp - 1.13) < 0.02
    # 全新行标注
    p90 = by[(90.0, "P")]
    assert "昨日无此行" in p90.note
    # 低于阈值 / 远 OTM 不出现
    assert (110.0, "C") not in by
    assert (50.0, "P") not in by
    # 净倾向：新增看跌 1800+300 > 看涨 50 → 偏空
    assert fa.net_put_doi == 2100 and fa.net_call_doi == 50
    assert fa.flow_tilt.startswith("偏空")


def test_flow_single_snapshot_only_unusual():
    curr = _snap([_c(95, "P", oi=1000, vol=3000)])
    fa = analyze_flow(None, curr, today=TODAY, curr_date="2026-06-26")
    assert fa.prev_date is None
    assert fa.changes == []
    assert any(u.strike == 95.0 for u in fa.unusual)
    assert "仅一份快照" in fa.flow_tilt


def test_judge_matches_author():
    # 用作者 WTI 6/22 表的代表行校验买卖方判定（IV 已是 Delta 修正后 pp）
    cases = [
        ("P", +318, -0.30, "卖方做支撑", "bullish"),
        ("P", +1232, +0.33, "买方保护", "bearish"),
        ("P", +140, +0.25, "买方轻微保护", "bearish"),   # 70P：<0.28 → 轻微
        ("P", -264, +0.34, "卖方撤退", "bearish"),
        ("C", +526, -1.32, "极强卖方压制", "bearish"),
        ("C", +1132, -0.01, "噪音", "neutral"),
        ("C", +496, +0.20, "轻微买方", "bullish"),
    ]
    for kind, doi, adj, exp_judg, exp_bias in cases:
        bias, judg, _w = _judge(kind, doi, adj, True)
        assert judg == exp_judg, f"{kind} {doi:+} {adj}: 得到 {judg}，期望 {exp_judg}"
        assert bias == exp_bias


def test_flow_buyer_seller_table():
    # 65P 加仓+IV升=买方保护(看空)；80C 加仓+IV大降=极强卖方压制(看空) → 偏空
    prev = _snap([_c(65, "P", oi=9000, iv=0.40), _c(80, "C", oi=6000, iv=0.35)], spot=70.0)
    curr = _snap([_c(65, "P", oi=10000, vol=2000, iv=0.41),
                  _c(80, "C", oi=7000, vol=2500, iv=0.32)], spot=70.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="d0", curr_date="d1")
    by = {(c.strike, c.kind): c for c in fa.changes}
    p, cc = by[(65.0, "P")], by[(80.0, "C")]
    assert p.judgment == "买方保护" and p.bias == "bearish"
    assert abs(p.adj_iv_pp - 1.0) < 0.01           # IV +1pp（单点偏斜=0，修正=原始）
    assert cc.judgment == "极强卖方压制" and cc.bias == "bearish"
    assert abs(cc.adj_iv_pp + 3.0) < 0.01
    assert fa.flow_tilt.startswith("偏空")


def test_snapshot_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(root=Path(tmp))
        d1, d2 = date(2026, 6, 25), date(2026, 6, 26)
        store.save("options", "GLD", {"data": {"x": 1}}, on_date=d1)
        store.save("options", "GLD", {"data": {"x": 2}}, on_date=d2)
        assert store.dates("options", "GLD") == [d1, d2]
        assert store.load("options", "GLD", d2) == {"data": {"x": 2}}
        two = store.latest_two("options", "GLD")
        assert [d for d, _ in two] == [d1, d2]
        # 同日覆盖
        store.save("options", "GLD", {"data": {"x": 3}}, on_date=d2)
        assert store.load("options", "GLD", d2) == {"data": {"x": 3}}
        assert store.dates("options", "GLD") == [d1, d2]  # 仍是两天


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
