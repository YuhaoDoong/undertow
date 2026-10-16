"""spread_pricing：净价三档、费用、盈亏平衡、挂单区间、预算边界、换算、缺盘口（2026-10-15 手算固化）。"""
import pytest

from undertow.analyze import spread_pricing as sp
from undertow.analyze import risk_policy as rp
from undertow.analyze.healthcheck import FEE_PER_CONTRACT


def L(k, side, bid, ask, kind="C", delta=None):
    return sp.LegQuote(strike=k, kind=kind, side=side, bid=bid, ask=ask, bid_size=10, ask_size=10, delta=delta)


# 2026-10-15 合成盘口：USO 11/20 158C 3.95/4.20、162C 2.88/2.89
USO_158_162 = [L(158, 1, 3.95, 4.20), L(162, -1, 2.88, 2.89)]


def test_net_prices_three_tiers():
    px = sp.net_prices(USO_158_162)
    assert px["worst"] == pytest.approx(4.20 - 2.88)          # 买付 ask、卖收 bid
    assert px["mid"] == pytest.approx(4.075 - 2.885)
    assert px["best"] == pytest.approx(3.95 - 2.89)
    assert px["missing"] == []


def test_missing_quote_is_none_not_zero():
    px = sp.net_prices([L(158, 1, 3.95, None), L(162, -1, 2.88, 2.89)])
    assert px["worst"] is None and px["mid"] is None
    assert px["best"] == pytest.approx(3.95 - 2.89)            # 最好档不需要买腿 ask
    assert any("缺 ask" in m for m in px["missing"])
    assert sp.limit_range(px) is None
    assert sp.economics(sp.vertical(USO_158_162), px["worst"]) is None


def test_zero_bid_on_short_leg_is_missing():
    px = sp.net_prices([L(158, 1, 3.95, 4.20), L(162, -1, 0.0, 2.89)])
    assert px["worst"] is None and px["mid"] is None


def test_vertical_classification():
    assert sp.vertical(USO_158_162)["debit"] is True
    assert sp.vertical([L(160, 1, 1, 1.1), L(150, -1, 2, 2.1)])["debit"] is False          # 熊市 call = 贷记
    assert sp.vertical([L(147, 1, 1, 1.1, "P"), L(148, -1, 1, 1.1, "P")])["debit"] is False  # 牛市 put = 贷记
    assert sp.vertical([L(50, 1, 1, 1.1, "P"), L(46, -1, 1, 1.1, "P")])["debit"] is True
    with pytest.raises(ValueError):
        sp.vertical([L(1, 1, 1, 1), L(2, 1, 1, 1)])
    with pytest.raises(ValueError):
        sp.vertical([L(1, 1, 1, 1), L(2, -1, 1, 1, "P")])


def test_vertical_four_structures_direction_prefix_mirror():
    """四种结构的方向前缀与括号一致（Codex 052：call 贷记、put 贷记原先前缀写反）。"""
    cases = {("C", True): ([L(100, 1, 1, 1.1), L(105, -1, 1, 1.1)], "看涨 call 借记", "牛市 call"),
             ("C", False): ([L(105, 1, 1, 1.1), L(100, -1, 1, 1.1)], "看跌 call 贷记", "熊市 call"),
             ("P", True): ([L(105, 1, 1, 1.1, "P"), L(100, -1, 1, 1.1, "P")], "看跌 put 借记", "熊市 put"),
             ("P", False): ([L(100, 1, 1, 1.1, "P"), L(105, -1, 1, 1.1, "P")], "看涨 put 贷记", "牛市 put")}
    for (kind, debit), (legs, prefix, paren) in cases.items():
        v = sp.vertical(legs)
        assert v["kind"] == kind and v["debit"] is debit
        assert v["name"].startswith(prefix) and paren in v["name"], v["name"]
        bull = prefix.startswith("看涨")
        assert bull == ("牛市" in v["name"])                    # 前缀方向与括号牛熊一致


def test_bad_quotes_degrade_not_price():
    """交叉 / 取数错误 / 陈旧 / 非有限：不出价（Codex 052 合成：买腿 2/1 交叉原先仍给 worst 0.5、best 1.4）。"""
    good_short = L(105, -1, .5, .6)
    crossed = sp.net_prices([L(100, 1, 2, 1), good_short])
    assert crossed["worst"] is None and crossed["mid"] is None and crossed["best"] is None
    assert any("交叉" in m for m in crossed["problems"])
    err = sp.net_prices([sp.LegQuote(100, "C", 1, 1.0, 1.1, error="timeout"), good_short])
    assert err["worst"] is None and any("取数报错" in m for m in err["problems"])
    stale = sp.net_prices([sp.LegQuote(100, "C", 1, 1.0, 1.1, stale=True), good_short])
    assert stale["mid"] is None and any("陈旧" in m for m in stale["problems"])
    for bad in (float("nan"), float("inf"), -1.0):
        px = sp.net_prices([L(100, 1, 1.0, bad), good_short])
        assert px["worst"] is None and px["mid"] is None
    assert sp.net_prices([L(100, 1, 1.0, 1.1), good_short])["worst"] == pytest.approx(0.6)


def test_debit_economics_matches_live_fill():
    """160/165C 净付 1.20：往返费 3.20（spread_econ 唯一口径）→ 最大亏损 123.2、最大收益 376.8、平衡含每股费 161.232。"""
    spec = sp.vertical([L(160, 1, 3.4, 3.5), L(165, -1, 2.1, 2.2)])
    e = sp.economics(spec, 1.20)
    rt = 4 * FEE_PER_CONTRACT
    assert e["unit_max_loss"] == e["total_max_loss"] == pytest.approx(120 + rt)
    assert e["unit_max_gain"] == e["total_max_gain"] == pytest.approx(500 - 120 - rt)
    assert e["breakeven"] == pytest.approx(161.20 + rt / 100)
    assert e["payoff_floor"]["status"] == "pass"


def test_credit_economics_and_direction_guard():
    spec = sp.vertical([L(147, 1, 0.85, 0.90, "P"), L(148, -1, 1.01, 1.08, "P")])
    e = sp.economics(spec, -0.15)
    rt = 4 * FEE_PER_CONTRACT
    assert e["unit_max_gain"] == pytest.approx(15 - rt)
    assert e["unit_max_loss"] == pytest.approx(85 + rt)
    assert e["breakeven"] == pytest.approx(148 - 0.15 + rt / 100)
    assert sp.economics(spec, 0.15) is None                    # 方向与结构不符 → 不可计算
    assert sp.economics(spec, -1.5) is None                    # 收入 ≥ 宽度 → 不可计算


def test_limit_range_uncalibrated_rule():
    lr = sp.limit_range(sp.net_prices(USO_158_162))
    m, w = 4.075 - 2.885, 4.20 - 2.88
    assert lr["start"] == pytest.approx(int(m * 100) / 100)
    assert lr["cap"] == pytest.approx(int((m + sp.LIMIT_CAP_FRAC * (w - m)) * 100) / 100)
    assert "未校准" in lr["meta"]["status"]
    # 贷记：负数，上限 = 最少收入（比中价收得少）
    lr2 = sp.limit_range(sp.net_prices([L(147, 1, 0.85, 0.90, "P"), L(148, -1, 1.01, 1.08, "P")]))
    assert lr2["cap"] >= lr2["start"] and lr2["cap"] < 0


def test_price_bound_under_policy_b():
    """R=529：其它类上限 $150 → 5 宽借记最高可付使 净付×100 + 3.2 ≤ 150 → 1.46。"""
    spec = sp.vertical([L(160, 1, 3.4, 3.5), L(165, -1, 2.1, 2.2)])
    cap = {"equity": 500.00, "asof": "2026-10-15T10:00:00-04:00"}

    def ok(e):
        b = rp.evaluate_budget(candidate=sp.budget_candidate(spec, e, "USO", "x"), open_positions=[],
                               capital=cap, pause={"latched": False})
        return b["allowed_units"] >= 1
    assert sp.price_bound_under(spec, ok, qty=1) == pytest.approx(1.46)
    fb = sp.price_bound_under(spec, lambda e: e["payoff_floor"]["status"] == "pass", qty=1)
    assert fb is not None and fb > 4.0                          # 下限只挡收益风险比 < 10% 的价


def test_price_bound_credit_min_receipt():
    spec = sp.vertical([L(143, 1, 1, 1.1, "P"), L(145, -1, 1, 1.1, "P")])     # 宽 2 的牛市 put
    b = sp.price_bound_under(spec, lambda e: e["total_max_loss"] <= 150, qty=1)
    # (2 − c)×100 + 3.2 ≤ 150 → c ≥ 0.532 → 最少收 0.54
    assert b == pytest.approx(-0.54)


def test_itm_proxy_range():
    deb = sp.vertical([L(160, 1, 1, 1), L(165, -1, 1, 1)])
    ip = sp.itm_proxy(deb, 0.35, 0.25, sp.economics(deb, 1.0))
    assert ip["status"] == "ok" and (ip["lo"], ip["hi"]) == (0.25, 0.35)
    assert "价内概率粗略代理" in ip["text"] and "非含费盈利概率" in ip["text"] and "盈利概率" not in ip["text"].replace("非含费盈利概率", "")
    cred = sp.vertical([L(147, 1, 1, 1, "P"), L(148, -1, 1, 1, "P")])
    ip = sp.itm_proxy(cred, -0.21, -0.25, sp.economics(cred, -0.5))
    assert (ip["lo"], ip["hi"]) == (0.75, 0.79)
    assert sp.itm_proxy(cred, None, -0.25, sp.economics(cred, -0.5)) is None
    assert sp.itm_proxy(cred, float("nan"), -0.25, sp.economics(cred, -0.5)) is None


def test_roll_plan_and_conversion():
    assert sp.roll_plan(6, 15) == 2 and sp.roll_plan(0, 15) == 0
    r = sp.realtime_ratio(105.09, 154.61)
    assert r == pytest.approx(105.09 / 154.61)
    assert sp.convert_level(154.61, r, to_commodity=True) == pytest.approx(105.09)
    assert sp.convert_level(101.50, r, to_commodity=False) == pytest.approx(101.50 / r)
    assert sp.realtime_ratio(None, 154.61) is None and sp.convert_level(150, None, to_commodity=True) is None
    assert sp.realtime_ratio(100, 0) is None


def test_broker_note_records_user_correction():
    assert "先平卖腿" in sp.BROKER_NOTES["combo_leg_exit"]


def test_economics_is_spread_econ():
    """同一个量不在两处各算一遍：经济量与 spread_econ.vertical 逐字段一致。"""
    from undertow.analyze import spread_econ as se
    spec = sp.vertical([L(160, 1, 3.4, 3.5), L(165, -1, 2.1, 2.2)])
    e = sp.economics(spec, 1.20, qty=3)
    ref = se.vertical(side="C", k_sell=165, k_buy=160, entry_credit=-1.20, fee_round_trip=4 * FEE_PER_CONTRACT)
    ref3 = se.vertical(side="C", k_sell=165, k_buy=160, entry_credit=-1.20, qty=3, fee_round_trip=4 * FEE_PER_CONTRACT)
    assert (e["unit_max_loss"], e["unit_max_gain"], e["breakeven"]) == (ref["entry_net_payoff_max_loss"], ref["entry_net_max_gain"], ref["breakeven"])
    assert (e["total_max_loss"], e["total_max_gain"], e["total_fee_total"]) == (
        ref3["entry_net_payoff_max_loss"], ref3["entry_net_max_gain"], ref3["fee_total_usd"])


# ───── Codex 052 I3：定价单位、价格边界、盘口合同、概率标签（全部合成数据；R=500 是合成数，不是账户读数） ─────

SYN_CAP = {"equity": 500.0, "asof": "2026-01-02T10:00:00-05:00"}
NOT_PAUSED = {"latched": False}


def _direct_allowed(unit_loss, qty):
    """直接用 risk_policy 的每组接口（不经 spread_pricing.budget_candidate），作为独立对照。"""
    return rp.evaluate_budget(candidate={"id": "syn", "underlying": "USO", "unit_stop_loss": unit_loss,
                                         "unit_payoff_max_loss": unit_loss, "qty_requested": qty},
                              open_positions=[], capital=SYN_CAP, pause=NOT_PAUSED)["allowed_units"]


def _via_candidate(spec, e):
    return rp.evaluate_budget(candidate=sp.budget_candidate(spec, e, "USO", "syn"), open_positions=[],
                              capital=SYN_CAP, pause=NOT_PAUSED)["allowed_units"]


def test_unit_and_total_separate_budget_multiplies_once():
    """宽 5、每股净付 0.50：每组含费最大亏损 50+3.20=53.20，两组整笔 106.40；budget 只乘一次组数。
    旧版把整笔 106.40 当每组又请求 2 组（=212.80 > 150）→ 只给 1 组。"""
    spec = sp.vertical([L(100, 1, 1.0, 1.1), L(105, -1, 0.5, 0.6)])
    one, two, three = (sp.economics(spec, 0.50, qty=q) for q in (1, 2, 3))
    assert one["unit_max_loss"] == two["unit_max_loss"] == three["unit_max_loss"] == pytest.approx(53.20)
    assert two["total_max_loss"] == pytest.approx(106.40) and three["total_max_loss"] == pytest.approx(159.60)
    cand = sp.budget_candidate(spec, two, "USO", "syn")
    assert cand["unit_payoff_max_loss"] == pytest.approx(53.20) and cand["qty_requested"] == 2
    assert _via_candidate(spec, two) == _direct_allowed(53.20, 2) == 2
    # 3 组：159.60 > 其它类 max(20%·500, 150)=150 → 政策只给 2 组；两条路径一致
    assert _via_candidate(spec, three) == _direct_allowed(53.20, 3) == 2
    assert _via_candidate(spec, one) == _direct_allowed(53.20, 1) == 1


@pytest.mark.parametrize("qty,expected", [(1, 1.46), (2, 0.71), (3, 0.46)])
def test_price_bound_uses_actual_qty_debit(qty, expected):
    """借记宽 5、其它类上限 $150：qty×(100x+3.2) ≤ 150 → 1 组 1.46、2 组 0.71、3 组 0.46。
    旧版扫描永远按 1 组，要求 ≥2 组时边界恒 None。边界前后一档用 risk_policy 每组接口独立复核。"""
    spec = sp.vertical([L(100, 1, 1.0, 1.1), L(105, -1, 0.5, 0.6)])

    def ok(e):
        assert e["qty"] == qty
        return _via_candidate(spec, e) >= qty
    b = sp.price_bound_under(spec, ok, qty=qty)
    assert b == pytest.approx(expected)
    at, beyond = sp.economics(spec, b, qty=1), sp.economics(spec, round(b + 0.01, 2), qty=1)
    assert _direct_allowed(at["unit_max_loss"], qty) >= qty                 # 边界这一档能开 qty 组
    assert _direct_allowed(beyond["unit_max_loss"], qty) < qty              # 再多付 0.01 就开不了 qty 组


@pytest.mark.parametrize("qty,expected", [(1, -3.54), (2, -4.29), (3, -4.54)])
def test_price_bound_uses_actual_qty_credit_mirror(qty, expected):
    """贷记镜像（卖 100P 买 95P，宽 5）：qty×((5−c)×100+3.2) ≤ 150 → 1 组最少收 3.54、2 组 4.29、3 组 4.54。"""
    spec = sp.vertical([L(95, 1, 1.0, 1.1, "P"), L(100, -1, 2.0, 2.1, "P")])
    assert spec["debit"] is False
    b = sp.price_bound_under(spec, lambda e: _via_candidate(spec, e) >= qty, qty=qty)
    assert b == pytest.approx(expected)
    at, worse = sp.economics(spec, b, qty=1), sp.economics(spec, round(b + 0.01, 2), qty=1)   # 少收 0.01
    assert _direct_allowed(at["unit_max_loss"], qty) >= qty
    assert _direct_allowed(worse["unit_max_loss"], qty) < qty


def test_payoff_floor_bound_is_qty_invariant():
    """收益风险比是比值，组数不改变它；按实际组数扫描与按 1 组扫描得同一边界。"""
    spec = sp.vertical([L(95, 1, 1.0, 1.1, "P"), L(100, -1, 2.0, 2.1, "P")])
    f = lambda e: e["payoff_floor"]["status"] == "pass"
    assert sp.price_bound_under(spec, f, qty=1) == sp.price_bound_under(spec, f, qty=3) is not None


def test_economics_rejects_bad_qty():
    spec = sp.vertical([L(100, 1, 1.0, 1.1), L(105, -1, 0.5, 0.6)])
    for q in (0, -1, 1.5, True):
        assert sp.economics(spec, 0.5, qty=q) is None


def test_four_structures_names_and_mirror():
    """方向前缀与括号一致（旧版熊市 call 贷记叫「看涨贷记」、牛市 put 贷记叫「看跌贷记」）；借记/贷记镜像经济量对称。"""
    cd = sp.vertical([L(100, 1, 1, 1.1), L(105, -1, 1, 1.1)])                 # 买低卖高 call
    cc = sp.vertical([L(105, 1, 1, 1.1), L(100, -1, 1, 1.1)])                 # 卖低买高 call
    pd = sp.vertical([L(105, 1, 1, 1.1, "P"), L(100, -1, 1, 1.1, "P")])       # 买高卖低 put
    pc = sp.vertical([L(100, 1, 1, 1.1, "P"), L(105, -1, 1, 1.1, "P")])       # 卖高买低 put
    assert (cd["debit"], cd["bias"], cd["name"].startswith("看涨"), "牛市" in cd["name"]) == (True, "bull", True, True)
    assert (cc["debit"], cc["bias"], cc["name"].startswith("看跌"), "熊市" in cc["name"]) == (False, "bear", True, True)
    assert (pd["debit"], pd["bias"], pd["name"].startswith("看跌"), "熊市" in pd["name"]) == (True, "bear", True, True)
    assert (pc["debit"], pc["bias"], pc["name"].startswith("看涨"), "牛市" in pc["name"]) == (False, "bull", True, True)
    # 买入价差付 1.20 ↔ 卖出同一价差收 1.20：毛盈亏互为镜像（费用两边都扣同样的 3.20）
    for deb, cre in ((cd, cc), (pd, pc)):
        d, c = sp.economics(deb, 1.20), sp.economics(cre, -1.20)
        assert d["unit_max_loss"] - 3.20 == pytest.approx(c["unit_max_gain"] + 3.20)
        assert d["unit_max_gain"] + 3.20 == pytest.approx(c["unit_max_loss"] - 3.20)


def test_no_fee_profit_means_no_probability():
    """Codex 052 例：净收 0.01、往返费 3.20 → 含费最大收益 −2.20；delta 代理不得再给 80%–90%，打平胜率不给。"""
    spec = sp.vertical([L(100, 1, 1, 1.01), L(99, -1, 1.02, 1.03)])
    e = sp.economics(spec, -0.01)
    assert e["unit_max_gain"] == pytest.approx(-2.20)
    assert e["fee_profit_possible"] is False and e["breakeven_win_rate"] is None
    ip = sp.itm_proxy(spec, 0.1, 0.2, e)
    assert ip["status"] == "no_fee_profit" and ip["lo"] is None and ip["hi"] is None
    assert sp.itm_proxy(spec, 0.1, 0.2, None)["status"] == "no_fee_profit"   # 经济量不可计算 → 同样不给


def test_crossed_quote_not_priced():
    """Codex 052 例：买腿 bid 2 / ask 1（交叉）→ 旧版给 worst 0.5 / mid 0.95 / best 1.4、missing=[]（优劣顺序反了）。"""
    legs = [L(100, 1, 2.0, 1.0), L(105, -1, 0.5, 0.6)]
    px = sp.net_prices(legs)
    assert (px["worst"], px["mid"], px["best"]) == (None, None, None)
    assert any("交叉" in m for m in px["problems"])
    assert sp.limit_range(px) is None and sp.economics(sp.vertical(legs), px["worst"]) is None


@pytest.mark.parametrize("bid,ask", [(float("nan"), 1.1), (1.0, float("inf")), (-0.1, 1.1), (1.0, "1.1"), (True, 1.1)])
def test_nonfinite_or_garbage_quote_not_priced(bid, ask):
    px = sp.net_prices([L(100, 1, bid, ask), L(105, -1, 0.5, 0.6)])
    assert (px["worst"], px["mid"], px["best"]) == (None, None, None) and px["problems"]


def test_leg_error_not_priced_and_zero_size_side_missing():
    err = sp.LegQuote(strike=100, kind="C", side=1, bid=1.0, ask=1.1, error="超时")
    px = sp.net_prices([err, L(105, -1, 0.5, 0.6)])
    assert px["worst"] is None and px["best"] is None and any("超时" in m for m in px["problems"])
    # 卖腿 bid 有价但挂单量明确为 0 → 只有用到卖腿 bid 的档（逐腿对手价、中价）不可计算
    z = sp.LegQuote(strike=105, kind="C", side=-1, bid=0.5, ask=0.6, bid_size=0, ask_size=3)
    px = sp.net_prices([L(100, 1, 1.0, 1.1), z])
    assert px["worst"] is None and px["mid"] is None and px["best"] == pytest.approx(1.0 - 0.6)
    assert not px["problems"] and any("缺 bid" in m for m in px["missing"])


def test_stale_quote_needs_explicit_permission_and_never_gives_limit():
    stale = [sp.LegQuote(strike=100, kind="C", side=1, bid=1.0, ask=1.1, stale=True), L(105, -1, 0.5, 0.6)]
    px = sp.net_prices(stale)
    assert px["mid"] is None and any("陈旧" in m for m in px["problems"])
    px = sp.net_prices(stale, allow_stale=True)
    assert px["stale"] is True and px["mid"] == pytest.approx(1.05 - 0.55)
    assert sp.limit_range(px) is None                                       # 陈旧价只作参考，不给挂单区间


def test_assignment_note_separates_theoretical_loss_from_disposal():
    assert "提前指派" in sp.ASSIGNMENT_NOTE and "未核实" in sp.ASSIGNMENT_NOTE
    assert "不会被强平" not in sp.ASSIGNMENT_NOTE
