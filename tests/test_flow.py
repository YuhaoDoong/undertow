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
from undertow.analyze.flow import (analyze_flow, counter_signals, flip_driver_summary, scan_unusual,
                                   structural_moves, _judge)
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
    # 用一张 WTI 期权流表的代表行校验买卖方判定（IV 已是 Delta 修正后 pp）
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


def test_abs_iv_gate_neutralizes_relative_false_signals():
    """绝对 IV 闸门：相对判定与绝对 ΔIV 方向矛盾且绝对显著 → 存疑不投票。
    复刻黄金 8/14：事件后 IV 齐落，415C 绝对 -1.18pp 却因相对化(adj +0.30)被误判买方。"""
    # call 新建 + 相对偏买(adj>0)，但绝对 IV 大跌 → 假买方，闸门判 neutral
    bias, judg, w = _judge("C", +69887, +0.30, True, -1.18)
    assert bias == "neutral" and w == 0.0 and "存疑" in judg
    # 同一腿若绝对 IV 未随市回落(绝对 +0.2，闸门不触发) → 仍是真买方
    bias, judg, _w = _judge("C", +69887, +0.30, True, +0.20)
    assert bias == "bullish" and "买方" in judg
    # 对称：put 新建 + 相对偏卖(写权做支撑 adj<0)，但绝对 IV 却齐涨 → 假支撑，闸门 neutral
    bias, judg, w = _judge("P", +5000, -0.40, True, +0.90)
    assert bias == "neutral" and w == 0.0 and "存疑" in judg
    # call 卖方压制(adj<0)且绝对 IV 也在跌(方向一致) → 不该被闸门误伤，仍是卖方压制
    bias, judg, _w = _judge("C", +1434, -0.47, True, -1.95)
    assert bias == "bearish" and "卖方压制" in judg


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
    # 卖 72C(IV降=压制) + 买 75C(IV升=保护)，净看空（复刻一次 WTI 识破案例）。
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




def test_fingerprint_ignores_newly_listed_zero_oi_contracts():
    """新挂 OI=0 的合约不得改变持仓指纹。

    交易所每天新挂一批行权价/到期，全是 OI=0。若计入指纹，「持仓完全没变」
    也会因多了几百条空合约而哈希不同 → 放行落盘一份 OI 未结算的残缺快照
    （现价新、OI 旧），使次日 diff 把两天的 OI 变动记成一天。
    2026-08-27 实测：SPY 新增 388 条、IWM 新增 236 条全为 OI=0，
    而已有合约总 |ΔOI| 恰为 0，指纹却判为「新数据」。
    """
    from datetime import date as _d
    from undertow.collect.cboe_options import chain_fingerprint
    from undertow.core.models import OptionContract, OptionsSnapshot

    def _c(strike, oi):
        return OptionContract(expiry=_d(2026, 9, 18), strike=strike, kind="C",
                              open_interest=oi, volume=10, gamma=0.01,
                              delta=0.3, iv=0.2)

    def _snap(cs):
        return OptionsSnapshot(instrument="spy", proxy_symbol="SPY", asof="x",
                               spot=600.0, contracts=cs)

    base = [_c(600.0, 1000), _c(605.0, 500)]
    assert chain_fingerprint(_snap(base)) == chain_fingerprint(
        _snap(base + [_c(700.0, 0), _c(705.0, 0)])), "新挂空合约不应改变指纹"
    assert chain_fingerprint(_snap(base)) != chain_fingerprint(
        _snap([_c(600.0, 1001), _c(605.0, 500)])), "真实 OI 变动必须改变指纹"
    print("PASS test_fingerprint_ignores_newly_listed_zero_oi_contracts")



def test_oi_change_total_detects_unsettled_chain():
    """到期合约滚出不得被误判成"有新持仓数据"。

    指纹是单快照函数，判不了这种情形：存活合约的 OI 一张没动，但因为有合约
    到期消失，OI>0 的行集合变了 → 指纹不同 → 放行一份 OI 未结算的残缺快照
    （现价新、OI 旧），次日 diff 会把两天的 OI 变动记成一天。
    2026-08-27 实测：GLD 在指纹修好之后仍因到期滚出被放行，Σ|ΔOI| 恰为 0。
    """
    from datetime import date as _d
    from undertow.collect.cboe_options import chain_fingerprint, oi_change_total
    from undertow.core.models import OptionContract, OptionsSnapshot

    def _c(strike, oi, exp=(2026, 9, 18)):
        return OptionContract(expiry=_d(*exp), strike=strike, kind="C",
                              open_interest=oi, volume=10, gamma=0.01,
                              delta=0.3, iv=0.2)

    def _snap(cs):
        return OptionsSnapshot(instrument="gold", proxy_symbol="GLD", asof="x",
                               spot=400.0, contracts=cs)

    survivors = [_c(400.0, 1000), _c(405.0, 500)]
    prev = _snap(survivors + [_c(410.0, 300, exp=(2026, 8, 26))])   # 这条次日到期滚出
    curr = _snap(survivors)
    assert chain_fingerprint(prev) != chain_fingerprint(curr), "前提：指纹确实会不同"
    assert oi_change_total(prev, curr) == 0, "存活合约一张没动 → 判为未结算"
    # 真实持仓变动必须被认出来
    assert oi_change_total(prev, _snap([_c(400.0, 1200), _c(405.0, 500)])) == 200
    print("PASS test_oi_change_total_detects_unsettled_chain")



def test_agg_key_must_include_expiry():
    """同一行权价的不同到期【不得】合并成一条腿。

    这是 2026-08-27 codex review 查出的根 bug：旧版 _agg 按 (行权价, C/P) 把
    60 天窗口内所有月份合成一条，后果——
      · IV 跨期按 OI 加权平均后再做日差 → ΔIV 可能纯粹来自期限权重变化；
      · 换月（近月平、远月开）在同一个桶里互相抵消，ΔOI 失真；
      · 污染传导到 pressure / 强信号 / 结构读数 / 墙位增量 / 台账。
    实测影响：QQQ 8/26→8/27 腿数 154→520（13 个到期）、上行压力 +128%、
    压力比 4.89×→1.78×，那条「⚡强看跌」横幅随之消失。

    ⚠️ 当时 307 个测试全过也没抓到，因为所有 fixture 都是单到期。
    """
    from datetime import date as _d
    from undertow.analyze.flow import analyze_flow
    from undertow.core.models import OptionContract, OptionsSnapshot

    E1, E2 = _d(2026, 9, 18), _d(2026, 10, 16)

    def _c(exp, strike, oi, iv):
        return OptionContract(expiry=exp, strike=strike, kind="C", open_interest=oi,
                              volume=500, gamma=0.01, delta=0.30, iv=iv)

    def _snap(cs):
        return OptionsSnapshot(instrument="qqq", proxy_symbol="QQQ", asof="x",
                               spot=700.0, contracts=cs)

    # 同一行权价 720，两个到期：一个增仓一个减仓，量相当。
    # 混算会互相抵消成 ΔOI≈0（该腿凭空消失）；分月则是两条真实的腿。
    prev = _snap([_c(E1, 720.0, 10_000, 0.20), _c(E2, 720.0, 10_000, 0.22)])
    curr = _snap([_c(E1, 720.0, 18_000, 0.20), _c(E2, 720.0, 2_000, 0.22)])
    fa = analyze_flow(prev, curr, today=_d(2026, 8, 27))
    at720 = [c for c in fa.changes if abs(c.strike - 720.0) < 1e-6]
    assert len(at720) == 2, f"720 应拆成两条腿（两个到期），实得 {len(at720)}"
    assert {c.expiry for c in at720} == {E1, E2}
    assert {c.d_oi for c in at720} == {8000, -8000}, \
        "换月的一增一减必须各自保留，混算会抵消成 0、整条腿消失"
    print("PASS test_agg_key_must_include_expiry")


def test_same_strike_different_expiry_iv_not_blended():
    """不同到期的 IV 不得跨期加权平均后再做日差。

    两个到期 IV 各自纹丝不动，只是 OI 权重变了——混算版会算出一个假的 ΔIV
    （纯粹来自期限权重迁移），分月版必须是 0。
    """
    from datetime import date as _d
    from undertow.analyze.flow import analyze_flow
    from undertow.core.models import OptionContract, OptionsSnapshot

    E1, E2 = _d(2026, 9, 18), _d(2026, 10, 16)

    def _c(exp, oi, iv):
        return OptionContract(expiry=exp, strike=720.0, kind="C", open_interest=oi,
                              volume=500, gamma=0.01, delta=0.30, iv=iv)

    def _snap(cs):
        return OptionsSnapshot(instrument="qqq", proxy_symbol="QQQ", asof="x",
                               spot=700.0, contracts=cs)

    # 近月 IV 0.20、远月 0.30，两日都不变；仅权重由 9:1 变成 1:9。
    prev = _snap([_c(E1, 9_000, 0.20), _c(E2, 1_000, 0.30)])
    curr = _snap([_c(E1, 1_000, 0.20), _c(E2, 9_000, 0.30)])
    fa = analyze_flow(prev, curr, today=_d(2026, 8, 27))
    for c in fa.changes:
        assert abs(c.d_iv_pp) < 1e-6, \
            f"{c.expiry} 的 IV 未变，ΔIV 必须为 0，实得 {c.d_iv_pp:+.2f}pp（跨期混算的伪信号）"
    print("PASS test_same_strike_different_expiry_iv_not_blended")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


# ==== 速读用结构性异动（structural_moves）====

def test_structural_moves_detects_put_wall_roll():
    # 复刻 2026-07-08 黄金实况：put 墙 360 卖方撤退 / 370 买方保护进场，
    # 双腿 IV 齐升 → 双 bearish → 结论必须点明"资本更看跌"（机构口径）
    prev = _snap([
        _c(360, "P", oi=106_000, iv=0.200), _c(370, "P", oi=6_600, iv=0.200),
        _c(400, "C", oi=40_000, iv=0.20),
    ], spot=374.0)
    curr = _snap([
        _c(360, "P", oi=103_725, iv=0.205),   # OI↓ + IV↑ = 卖方撤退（支撑减弱）
        _c(370, "P", oi=9_467, iv=0.205),     # OI↑ + IV↑ = 买方保护进场
        _c(400, "C", oi=40_000, iv=0.20),
    ], spot=374.0)
    fa = analyze_flow(prev, curr, today=TODAY, put_wall=360.0, call_wall=400.0,
                      prev_date="a", curr_date="b")
    moves = structural_moves(fa, conv=lambda v: v * 10.9)
    assert moves, "应识别出 put 墙滚动"
    assert "卖方撤退" in moves[0] and "买方保护" in moves[0]
    assert "资本更看跌" in moves[0]
    assert "3,924" in moves[0] and "4,033" in moves[0]   # 360/370 × 10.9 商品口径
    assert "-2,275" in moves[0] and "+2,867" in moves[0]


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
    assert "资金流净倾向偏空" in txt
    assert "支撑防线后撤" in txt
    # 不传就不出现该段
    assert "今日持仓异动" not in plain_summary(o)


def test_counter_signals_picks_opposing_moves():
    # 偏空研判下，call 端买方大单是最强对手盘；观望不出
    prev = _snap([_c(62, "C", oi=4_000, iv=0.200), _c(55, "P", oi=9_000, iv=0.200)],
                 spot=58.0)
    curr = _snap([
        _c(62, "C", oi=9_181, iv=0.206),   # OI↑+IV↑ = 买方（bullish）
        _c(55, "P", oi=13_500, iv=0.206),  # OI↑+IV↑ = 买方保护（bearish）
    ], spot=58.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    against_short = counter_signals(fa, "做空")
    assert against_short and "62.0" in against_short[0] and "买方" in against_short[0]
    assert all("买方保护" not in s for s in against_short)   # 同向的不算对手盘
    against_long = counter_signals(fa, "做多")
    assert against_long and "55.0" in against_long[0]
    assert counter_signals(fa, "观望") == []


def test_plain_summary_blocks_structure_and_counter():
    from undertow.analyze.outlook import KeyLevel, Outlook, plain_summary_blocks
    o = Outlook(instrument="t", display_name="T", asof="x", spot=100.0,
                commodity_spot=None, proxy_symbol="T", bias="偏空",
                bias_score=-3.0, confidence="高", regime="负Gamma",
                key_levels=[KeyLevel("看跌墙 / 支撑", 95.0, None, "support", "")])
    blocks = plain_summary_blocks(
        o, flow_tilt="偏空（下行 100 > 上行 10）",
        flow_moves=["put 墙 95.0 增厚（+2,000 手）"],
        counter_notes=["call 端 105.0 买方（+5,181 手）"])
    titles = [t for t, _ in blocks]
    assert titles == ["方向", "关键位/路径", "持仓异动", "对手盘警示"]
    d = dict(blocks)
    assert "负伽马" in d["方向"] and "偏空·可信度高" in d["方向"]
    assert "与研判方向相反的最强信号" in d["对手盘警示"]
    assert "置信应相应下调" in d["对手盘警示"]
    # 有方向、有 diff、但无反向信号 → 显式说"暂无"
    blocks2 = plain_summary_blocks(o, flow_tilt="偏空（下行 100 > 上行 10）")
    assert dict(blocks2)["对手盘警示"] == "今日 ΔOI 中暂无结构级的反向信号。"
    # 中性 → 无对手盘块
    o3 = Outlook(instrument="t", display_name="T", asof="x", spot=100.0,
                 commodity_spot=None, proxy_symbol="T", bias="中性",
                 bias_score=0.0, confidence="低", regime="正Gamma",
                 key_levels=[])
    assert "对手盘警示" not in dict(plain_summary_blocks(o3, flow_tilt="分歧（…）"))


def test_flip_driver_summary_panic_receding():
    # 现价上方 put 减仓+IV降、近价 call 增仓+IV升 → 恐慌退潮、多头谨慎入场
    prev = _snap([_c(102, "P", oi=9_000, iv=0.50), _c(101, "C", oi=5_000, iv=0.44)],
                 spot=100.0)
    curr = _snap([_c(102, "P", oi=6_500, iv=0.44), _c(101, "C", oi=8_200, iv=0.47)],
                 spot=100.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    txt = flip_driver_summary(fa)
    assert "现价上方 put 减仓 2,500 手且相对 IV 回落" in txt
    assert "近价 call 增仓 3,200 手且相对 IV 走强" in txt
    assert "恐慌保护退潮" in txt and "谨慎入场" in txt
    # 反向模式：put 增仓+IV升 → 风险重新加价；call 撤退
    fa2 = analyze_flow(curr, prev, today=TODAY, prev_date="a", curr_date="b")
    t2 = flip_driver_summary(fa2)
    assert "下方风险重新加价" in t2 and "看涨需求退潮" in t2


# ==== Codex review P0 回归（2026-07-10）====

def test_delta_correction_sign_sticky_moneyness():
    # P0-1：现价 +1、IV 沿偏斜机械上移（sticky-moneyness），残差应≈0 判噪音；
    # 旧实现（d_iv - slope*d_spot）会把机械项翻倍成 +0.4pp 误判买方保护
    prev = _snap([_c(95, "P", oi=2_000, iv=0.300), _c(100, "P", oi=2_000, iv=0.290)],
                 spot=100.0)
    curr = _snap([_c(95, "P", oi=2_500, iv=0.302), _c(100, "P", oi=2_000, iv=0.292)],
                 spot=101.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    p95 = next(c for c in fa.changes if c.strike == 95.0)
    assert abs(p95.adj_iv_pp) < 0.05, f"机械 IV 变化未被去除: {p95.adj_iv_pp}pp"
    assert p95.judgment == "噪音"


def test_spread_requires_same_expiry():
    # P0-2：跨到期的"卖低买高"不得配成垂直价差（7月卖方 + 8月买方 ≠ Bear Call）
    e2 = EXP + timedelta(days=35)
    prev = _snap([_c(100, "C", oi=2_000, iv=0.30), _c(105, "C", oi=1_500, iv=0.34, expiry=e2)],
                 spot=100.0)
    curr = _snap([_c(100, "C", oi=2_300, vol=900, iv=0.28),
                  _c(105, "C", oi=1_800, vol=900, iv=0.36, expiry=e2)], spot=100.0)
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    assert fa.spreads == [], "跨到期两腿被误配为垂直价差"
    # 同到期版本必须仍能识别
    prev2 = _snap([_c(100, "C", oi=2_000, iv=0.30), _c(105, "C", oi=1_500, iv=0.34)], spot=100.0)
    curr2 = _snap([_c(100, "C", oi=2_300, vol=900, iv=0.28),
                   _c(105, "C", oi=1_800, vol=900, iv=0.36)], spot=100.0)
    fa2 = analyze_flow(prev2, curr2, today=TODAY, prev_date="a", curr_date="b")
    assert len(fa2.spreads) == 1


def test_unequal_spread_deducts_matched_size_only():
    # P0-3：腿不等量时只扣匹配数量——剩余 150 张买方 call 仍是方向仓
    prev = _snap([_c(72, "C", oi=2_000, iv=0.30), _c(75, "C", oi=1_500, iv=0.34)], spot=70.0)
    curr = _snap([_c(72, "C", oi=2_100, vol=900, iv=0.28),     # 卖方 +100
                  _c(75, "C", oi=1_750, vol=900, iv=0.36)], spot=70.0)  # 买方 +250
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    assert len(fa.spreads) == 1 and fa.spreads[0].size == 100
    assert fa.upside_pressure == 150.0, f"应剩 150 未配对买方压力，实得 {fa.upside_pressure}"


def test_new_row_without_prev_iv_is_downweighted():
    # P1-8：昨日无 IV 的全新行 → 主动方未知，方向权重减半而非满权
    prev = _snap([_c(100, "C", oi=1_000, iv=0.20)])
    curr = _snap([_c(100, "C", oi=1_000, iv=0.20), _c(95, "P", oi=800, iv=0.20)])
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    p95 = next(c for c in fa.changes if c.strike == 95.0)
    assert "主动方未知" in p95.judgment and p95.weight == 0.5


def test_band_crossing_strike_uses_prev_oi_baseline():
    # P0（2026-07-15 实况）：现价上涨后，行权价 113 刚进入今日近价带，
    # 但按昨日 spot 锚定的带宽在带外——旧实现昨日基线缺失，把 134,701 手
    # 存量 OI 整体误判成"单日新建买方"（SLV 60C 实例）。
    # 修复：昨日链按【今日】spot 锚定近价带（band_spot）。
    prev = _snap([_c(100, "C", oi=5_000, iv=0.20), _c(113, "C", oi=134_701, iv=0.30)],
                 spot=98.0)   # 昨日 spot=98 → 旧带上界在 113 之下，该行落带外
    curr = _snap([_c(100, "C", oi=5_000, iv=0.20), _c(113, "C", oi=134_951, iv=0.30)],
                 spot=101.0)  # 今日 spot=101 → 113 进带
    fa = analyze_flow(prev, curr, today=TODAY, prev_date="a", curr_date="b")
    # 关键断言：不得出现 13 万手级别的伪"新建"行
    assert all(abs(c.d_oi) < 10_000 for c in fa.changes), \
        [f"{c.strike}{c.kind} {c.d_oi:+,}" for c in fa.changes]
    row = next((c for c in fa.changes if c.strike == 113.0), None)
    if row is not None:   # ΔOI=+250 若低于门槛整行被滤掉，同样正确
        assert row.d_oi == 250 and "新建" not in row.judgment
