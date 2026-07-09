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

from undertow.core.models import OptionsSnapshot, OptionContract
from undertow.analyze.flow import analyze_flow, scan_unusual, structural_moves, _judge
from undertow.collect.store import SnapshotStore

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


def test_detect_bear_call_spread_strips_protective_leg():
    # 表面"上方大量买 75C"看似看涨，实为 Bear Call Spread 的封顶腿：
    # 卖 72C(IV降=压制) + 买 75C(IV升=保护)，净看空（复刻作者 6/25 WTI 识破）。
    prev = _snap([_c(72, "C", oi=2000, iv=0.30), _c(75, "C", oi=1500, iv=0.34)], spot=70.0)
    curr = _snap([_c(72, "C", oi=2300, vol=900, iv=0.28),
                  _c(75, "C", oi=1800, vol=900, iv=0.36)], spot=70.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="d0", curr_date="d1")
    assert len(fa.spreads) == 1
    sp = fa.spreads[0]
    assert sp.net_bias == "bearish" and sp.short_strike == 72.0 and sp.long_strike == 75.0
    assert "熊市看涨" in sp.name
    by = {(c.strike, c.kind): c for c in fa.changes}
    # 短腿=方向，长腿=保护（与净向相反，不计方向）
    assert "短腿" in by[(72.0, "C")].spread_note
    assert "保护" in by[(75.0, "C")].spread_note
    # 75C 的看涨买盘被扣除 → 上行压力清零，净倾向偏空（而非被表象误导成看涨）
    assert fa.upside_pressure == 0.0
    assert fa.downside_pressure > 0
    assert fa.flow_tilt.startswith("偏空")


def test_spread_rejects_far_and_small_legs():
    # 行权价相距过远（>8%）或某腿规模太小，都不应被配成价差。
    prev = _snap([_c(72, "C", oi=2000, iv=0.30), _c(90, "C", oi=1500, iv=0.34),
                  _c(73, "C", oi=2000, iv=0.30), _c(74, "C", oi=1000, iv=0.34)], spot=70.0)
    curr = _snap([_c(72, "C", oi=2300, vol=900, iv=0.28),   # 卖方压制
                  _c(90, "C", oi=1800, vol=900, iv=0.36),   # 买方，但距 72 太远(25%>8%)
                  _c(73, "C", oi=2330, vol=900, iv=0.28),   # 卖方压制
                  _c(74, "C", oi=1010, vol=900, iv=0.36)],  # 买方，但 ΔOI=10 < 噪音门槛
                 spot=70.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="d0", curr_date="d1")
    assert fa.spreads == []   # 既无相近又量级达标的买卖对


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


# ==== 速读用结构性异动（structural_moves）====

def test_structural_moves_detects_put_wall_roll():
    # 复刻 2026-07-08 黄金实况：put 墙 360 削 2,275 / 355 增 2,119 = 防线后撤
    prev = _snap([
        _c(360, "P", oi=106_000, iv=0.20), _c(355, "P", oi=6_600, iv=0.20),
        _c(400, "C", oi=40_000, iv=0.20),
    ], spot=375.0)
    curr = _snap([
        _c(360, "P", oi=103_725, iv=0.20), _c(355, "P", oi=8_719, iv=0.20),
        _c(400, "C", oi=40_000, iv=0.20),
    ], spot=374.0)
    fa = analyze_flow(prev, curr, today=TODAY, put_wall=360.0, call_wall=400.0,
                      prev_date="a", curr_date="b")
    moves = structural_moves(fa, conv=lambda v: v * 10.9)
    assert moves, "应识别出 put 墙滚动"
    assert "移至" in moves[0] and "支撑防线后撤" in moves[0]
    assert "3,924" in moves[0] and "3,870" in moves[0]   # 360/355 × 10.9 商品口径
    assert "-2,275" in moves[0] and "+2,119" in moves[0]


def test_structural_moves_wall_thicken_and_top_build():
    prev = _snap([
        _c(105, "C", oi=20_000, iv=0.20), _c(95, "P", oi=8_000, iv=0.20),
    ])
    curr = _snap([
        _c(105, "C", oi=31_000, iv=0.208),   # call 墙 +11,000 增厚
        _c(95, "P", oi=13_000, vol=9_000, iv=0.212),  # +5,000 买方保护
    ])
    fa = analyze_flow(prev, curr, today=TODAY, call_wall=105.0, put_wall=None,
                      prev_date="a", curr_date="b")
    moves = structural_moves(fa)
    assert any("call墙" in m and "增厚" in m and "+11,000" in m for m in moves)
    assert any("95.0 put 新增 5,000 手" in m for m in moves)


def test_structural_moves_ignores_small_and_single_snapshot():
    prev = _snap([_c(100, "C", oi=1_000, iv=0.20)])
    curr = _snap([_c(100, "C", oi=1_500, iv=0.20)])   # +500 < 结构级门槛
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    assert structural_moves(fa) == []
    fa_single = analyze_flow(None, curr, today=TODAY)
    assert structural_moves(fa_single) == []


def test_plain_summary_carries_flow_signals():
    from undertow.analyze.outlook import KeyLevel, Outlook, plain_summary
    o = Outlook(instrument="t", display_name="T", asof="x", spot=100.0,
                commodity_spot=None, proxy_symbol="T", bias="偏空",
                bias_score=-3.0, confidence="高", regime="负Gamma",
                key_levels=[KeyLevel("看跌墙 / 支撑", 95.0, None, "support", "")])
    txt = plain_summary(o, flow_tilt="偏空（下行 100 > 上行 10）",
                        flow_moves=["put 仓从 95.0 移至 94.0（-2,000 / +1,900 手，支撑防线后撤）"])
    assert "今日持仓异动：期权资金流净倾向偏空" in txt
    assert "支撑防线后撤" in txt
    # 不传就不出现该段
    assert "今日持仓异动" not in plain_summary(o)
