"""持仓 × 信号冲突告警 —— 回归测试。

全部用例都锚定 2026-08-28 那次失职：黄金亮 ⚡极强看跌（53.5×），
用户持白银多头，金银相关 0.89 —— 没提信号、没质疑白银的相反结论、没预警持仓。
次日 SLV -4.38%。
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.position_alert import (  # noqa: E402
    parse_symbol, leg_bias, position_bias, check_conflicts, render, unparsed)


@dataclass
class SS:
    direction: str
    level: str
    pressure_ratio: float
    reasons: list


# 长桥真实返回的格式：带 .US 交易所后缀
REAL = {"SLV260918C70000.US": 1, "SLV260918C73000.US": -1,
        "TQQQ260918C76000.US": 1, "TQQQ260918C80000.US": -1}


def test_parses_exchange_suffix():
    """真实代码带 .US —— 早先正则以 $ 收尾，四条腿一条都解析不出来，
    持仓被当成不存在，告警静默消失。"""
    assert parse_symbol("SLV260918C70000.US") == ("SLV", "C")
    assert parse_symbol("SLV260918C70000") == ("SLV", "C")
    assert unparsed(REAL) == [], "真实持仓必须全部可解析"
    print("PASS test_parses_exchange_suffix")


def test_unparsed_is_reported_not_swallowed():
    """解析不了必须能被报出来 —— 静默吞掉等于没有告警。"""
    assert unparsed({"NOT_AN_OPTION": 1}) == ["NOT_AN_OPTION"]
    print("PASS test_unparsed_is_reported_not_swallowed")


def test_spread_direction_from_strikes():
    """价差两腿张数抵消，方向由【买的腿在哪一侧】唯一决定 —— 这是定义不是推测。"""
    assert leg_bias("SLV260918C70000.US", 1) == 1      # 买 call
    assert leg_bias("SLV260918C73000.US", -1) == -1    # 卖 call
    # 买低卖高的 call 价差 = 牛市看涨
    assert position_bias(REAL) == {"SLV": 1, "TQQQ": 1}
    # 买高卖低的 call 价差 = 熊市看涨（净看跌）
    bear = {"AAA260918C80000": 1, "AAA260918C76000": -1}
    assert position_bias(bear)["AAA"] == -1
    # 买高卖低的 put 价差 = 熊市看跌
    bear_put = {"BBB260918P80000": 1, "BBB260918P76000": -1}
    assert position_bias(bear_put)["BBB"] == -1
    print("PASS test_spread_direction_from_strikes")


def test_gold_signal_wakes_silver_position():
    """8/28 原样重演：黄金的极强看跌必须惊动白银的持仓。"""
    signals = {"GLD": SS("看跌", "极强", 53.5, ["看跌加权增仓 112,789 ≫ 看涨 563"])}
    cf = check_conflicts(REAL, signals)
    assert cf, "金银相关 0.89，黄金极强看跌必须对白银多头告警"
    c = cf[0]
    assert c.underlying == "SLV" and c.holding == "看涨"
    assert c.source == "GLD" and c.cross is True and c.corr == 0.89
    assert "SLV" in c.headline() and "GLD" in c.headline()
    assert "不是平仓指令" in render(cf), "必须声明未经校准，不是指令"
    print("PASS test_gold_signal_wakes_silver_position")


def test_same_direction_does_not_alert():
    """信号与持仓同向不该告警 —— 告警滥发等于没有告警。"""
    assert check_conflicts(REAL, {"GLD": SS("看涨", "强", 5.0, [])}) == []
    assert check_conflicts(REAL, {"SLV": SS("看涨", "强", 5.0, [])}) == []
    print("PASS test_same_direction_does_not_alert")


def test_same_instrument_ranks_before_cross():
    """同品种信号比交叉信号更直接，必须排在前面。"""
    cf = check_conflicts(REAL, {"GLD": SS("看跌", "极强", 53.5, []),
                                "SLV": SS("看跌", "强", 4.0, [])})
    assert cf[0].source == "SLV" and cf[0].cross is False
    print("PASS test_same_instrument_ranks_before_cross")


def test_family_consistency_flags_gold_silver_split():
    """金银同向、QQQ/TQQQ 同向 —— 结论不一致必须被摆出来（用户 2026-08-29 要求）。

    8/28 原样重演：黄金 ⚡极强看跌、白银近端中性，两者相关 0.89，
    当时没有任何地方指出这个矛盾。次日 GLD -3.24%、SLV -4.38%。
    """
    from undertow.analyze.family import check

    views = {
        "gold": {"near": "偏空(弱)", "signal_dir": "看跌", "signal_level": "极强"},
        "silver": {"near": "中性", "signal_dir": "", "signal_level": ""},
        "qqq": {"near": "偏空(弱)", "signal_dir": "", "signal_level": ""},
        "spy": {"near": "偏多(弱)", "signal_dir": "", "signal_level": ""},
        "tqqq": {"near": "偏空(弱)", "signal_dir": "", "signal_level": ""},
    }
    notes = check(views)
    pairs = {(n.a, n.b): n for n in notes}
    assert ("gold", "silver") in pairs, "黄金强信号落单必须惊动白银"
    assert pairs[("gold", "silver")].kind == "强信号落单"
    assert ("qqq", "spy") in pairs, "QQQ 与 SPY 近端相反必须报"
    assert pairs[("qqq", "spy")].kind == "近端方向相反"
    # QQQ 与 TQQQ 同向 → 不该报
    assert ("qqq", "tqqq") not in pairs, "同向不得误报，滥发等于没有告警"
    # 相关性越高、等级越强的排前面
    assert notes[0].severity >= notes[-1].severity
    print("PASS test_family_consistency_flags_gold_silver_split")


def test_family_same_direction_is_silent():
    """同族同向时必须完全沉默 —— 每天都报的提示等于没有提示。"""
    from undertow.analyze.family import check
    assert check({"gold": {"near": "偏多"}, "silver": {"near": "偏多(弱)"}}) == []
    assert check({"qqq": {"near": "偏空"}, "tqqq": {"near": "偏空(弱)"}}) == []
    print("PASS test_family_same_direction_is_silent")


def test_report_filename_uses_tradable_day_not_generation_day():
    """研报文件名必须回答「这份东西哪天能用」，不是「哪天生成的」。

    2026-08-29（周六）生成的报告装着描述 8/27 交易日的数据、可交易日是 8/28，
    却被命名成 gold_2026-08-29.html —— 工作日两者相同看不出来，周末就错位。
    """
    src = (Path(__file__).resolve().parents[1] / "undertow" / "cli.py").read_text("utf-8")
    assert 'fn = f"{inst.key}_{curr_date_s or today.isoformat()}.html"' in src, \
        "研报文件名须用快照日期（可交易日），不得用 today"
    assert 'index_path = reports_dir / f"index_{_idx_day}.html"' in src, \
        "索引页同理"
    assert "今天（" in src and "没有新数据" in src, \
        "数据非当日时必须明说，否则看着像当日研报"
    print("PASS test_report_filename_uses_tradable_day_not_generation_day")


def test_replay_truncates_future_prices():
    """回放必须掐断未来价格 —— 否则整份重放不可信。

    2026-08-29 实测：回放 8/28 的黄金，吃进了 8/28 当天 -3.24% 的收盘价，
    超买超卖从当时真实的「偏超买 78%」变成「中性 57%」，直接改写了结论。
    未来数据从两个入口渗入：real_series，以及"谁更新用谁"的长桥 K 线。
    """
    src = (Path(__file__).resolve().parents[1] / "undertow" / "cli.py").read_text("utf-8")
    assert "def _truncate_before(" in src, "缺少按 as-of 截断价格序列的函数"
    # 出口处必须统一再截一次：长桥 K 线那一支会把未来数据接回来
    assert "tech_series = _truncate_before(tech_series, today)" in src, \
        "技术面序列必须在【所有来源汇合之后】统一截断"
    assert "series_done = _truncate_before(series_done, today)" in src
    print("PASS test_replay_truncates_future_prices")


def test_indicator_families_are_non_overlapping_sources():
    """六组指标必须按【数据是否同源】分，同源的不得算两票。"""
    from undertow.analyze.indicators import FAMILIES, Label, render_pills, render_section
    assert set(FAMILIES) == {"struct", "flow", "vol", "price", "cot", "macro"}
    # 每组都得有：图标、短名、数据源、大白话
    for k, v in FAMILIES.items():
        assert len(v) == 4 and all(v), f"{k} 家族定义不完整"
        assert len(v[3]) > 20, f"{k} 缺大白话说明"
    labs = [Label("flow", "💰", "增仓", -1, "看跌侧 53.5×", "加权增仓", "近端")]
    pills = render_pills(labs, lambda x: x)
    assert "53.5×" in pills, "强度必须显示 —— 53.5× 与 1.4× 不能看起来一样"
    sec = render_section(labs, lambda x: x)
    assert "同一套压力数" in sec, "必须声明 ⚡强信号与增仓共线、不重复计票"
    # ⚠️ 不得把六组说成六份独立证据 —— 结构与增仓同出一份快照（存量 vs 增量），
    # 波动率面又参与了增仓的买卖方推断。2026-08-29 收紧了这处表述。
    assert "并非六份互相独立" in sec, "必须点明六组不是互相独立的证据"
    assert "同一份快照" in sec
    assert "固定" in sec and "0.8 票" in sec, "必须写明权重不随强度变化这个已知缺陷"
    print("PASS test_indicator_families_are_non_overlapping_sources")


def test_strength_scaling_is_off_by_default():
    """强度缩放默认关闭 —— 回测不支持它，打开就是拿一天的结果拟合。

    2026-08-29 回测（138 样本 / 35 日聚类）：
      高强度组 vs 低强度组命中率差 +2.5pp / 0.0pp / -1.2pp，几乎无区分度；
      「按强度加权」减「只用方向」中位 +3.1pp，95% 区间 [-3.7pp, +9.7pp] 跨 0；
      压力比≥20× 的 11 个饱和样本命中 6/11=55%，反低于全体 64.3%。
    """
    from undertow.analyze import strength as S
    assert S.USE_STRENGTH is False, "强度缩放未经验证，不得默认开启"
    # 唯一通过日聚类 bootstrap 的是 flow，它该拿最高权重
    assert S.GROUP_W["flow"] == max(S.GROUP_W.values()), \
        "flow 是唯一验证有效的一层（64.3%，区间下界 54.5%），权重必须最高"
    assert S.GROUP_W["vol"] < S.GROUP_W["flow"], "波动率面区间跨 50%，权重须低于 flow"
    print("PASS test_strength_scaling_is_off_by_default")


def test_strength_saturates_not_linear():
    """比值型强度必须饱和：53.5× 不该是 5.3× 的十倍话语权。"""
    from undertow.analyze.strength import _log_sat
    assert _log_sat(1.3, 1.3, 20.0) == 0.0
    assert 0.2 < _log_sat(3.0, 1.3, 20.0) < 0.4
    assert _log_sat(20.0, 1.3, 20.0) == 1.0
    assert _log_sat(53.5, 1.3, 20.0) == 1.0, "超过上限必须饱和，不能继续放大"
    print("PASS test_strength_saturates_not_linear")


def test_squeeze_is_observation_only_never_direction():
    """波动压缩只说「可能要变天」，绝不说「往哪边变」。

    区间跨 0（[-5,+44]pp / [-6,+41]pp），样本不足以下结论，
    所以它不得进综合分、不得产生方向票。
    """
    from undertow.analyze.squeeze import assess, Squeeze
    import undertow.analyze.squeeze as SQ
    # 模块里不能有任何方向词的输出接口
    src = (Path(__file__).resolve().parents[1] / "undertow" / "analyze"
           / "squeeze.py").read_text("utf-8")
    assert "不参与任何方向判定" in src and "不进综合分" in src
    for bad in ("看涨", "看跌", "偏多", "偏空"):
        assert f'"{bad}"' not in src, f"压缩模块不得输出方向词 {bad}"
    # 区间收敛那一维实测 -9pp，明确不纳入
    assert "不纳入" in src, "10/60 日区间收敛实测无效，必须写明已排除"
    # 两维都算不出来时必须 ok=False，不许用单腿硬撑
    assert assess(iv_pctile=None, highs=None, lows=None, closes=None) == Squeeze()
    # 双低才算 tight。⚠️ iv_pctile 由 volregime 传入 —— 上一版让本函数自己从
    # VolRegime.history 算，那个字段根本不存在，tight 在生产里永远是 False。
    # 前 60 根波幅宽、最后 5 根收窄 —— 这才是"压缩"，等差序列的 ATR 恒定测不出来
    C = [10.0] * 70
    H = [c + (0.5 if i < 65 else 0.05) for i, c in enumerate(C)]
    L = [c - (0.5 if i < 65 else 0.05) for i, c in enumerate(C)]
    r = assess(iv_pctile=0.20, highs=H, lows=L, closes=C)
    assert r.ok and r.iv_pctile == 0.20
    assert r.tight is True, "IV 分位 20% + 波幅收缩，必须能点亮 tight"
    # 未验证的规则不得对用户宣称胜率
    assert "45%" not in r.note and "%" not in r.note.split("→")[-1], \
        "AND 规则未单独回测，文案里不得出现具体胜率"
    print("PASS test_squeeze_is_observation_only_never_direction")


def test_spread_bias_refuses_complex_structures():
    """复杂结构必须返回「不知道」，不得强行定性。

    codex 2026-08-29 P0：上一版丢掉到期日、只要单腿方向抵消就按
    「最小买入行权价 vs 最大卖出行权价」定性，会把铁鹰误判成看跌、
    把日历价差任意定向。方向判错会漏掉真实风险告警或制造假告警。
    """
    from undertow.analyze.position_alert import position_bias

    # ✅ 认得出来的：标准垂直价差
    assert position_bias({"AAA260918C70000": 1, "AAA260918C73000": -1})["AAA"] == 1
    assert position_bias({"AAA260918C73000": 1, "AAA260918C70000": -1})["AAA"] == -1
    assert position_bias({"AAA260918P73000": 1, "AAA260918P70000": -1})["AAA"] == -1

    # ❌ 必须说「不知道」的：
    # 日历价差（同行权价、不同到期）
    assert position_bias({"AAA260918C70000": 1, "AAA261016C70000": -1})["AAA"] == 0
    # 铁鹰（call + put 四条腿）
    condor = {"AAA260918C75000": -1, "AAA260918C80000": 1,
              "AAA260918P65000": -1, "AAA260918P60000": 1}
    assert position_bias(condor)["AAA"] == 0
    # 蝶式（三腿）
    fly = {"AAA260918C70000": 1, "AAA260918C75000": -2, "AAA260918C80000": 1}
    assert position_bias(fly)["AAA"] == 0
    # 跨式（同行权价 call+put 同向）—— 单腿方向不抵消，走不到 _spread_bias，
    # 但结果也不该是一个自信的方向
    straddle = {"AAA260918C70000": 1, "AAA260918P70000": 1}
    assert position_bias(straddle)["AAA"] in (0, -1), "跨式无方向，不得判成看涨"
    # 比例价差（张数不等）
    ratio = {"AAA260918C70000": 1, "AAA260918C75000": -2}
    assert position_bias(ratio)["AAA"] in (0, -1)
    print("PASS test_spread_bias_refuses_complex_structures")


def test_expiry_buckets_split_and_conflict():
    """到期桶必须能拆开看，方向打架时要降置信。

    用户 2026-08-29 追问「近月、远月是区分开的吗」引出：
    逐腿判定确实按 (到期,行权价,C/P) 分开算，但最终 pressure 是 45 天内
    所有到期【加总】的 —— 0DTE 的赌明天和 30 天后的中期保护被压成一个数。
    实测 8/28 八品种里 5 个方向打架。
    """
    from undertow.analyze.flow import (_split_by_dte, _dte_dirs_conflict,
                                       DTE_BUCKETS)
    from dataclasses import dataclass
    from datetime import date as D

    @dataclass
    class L:
        expiry: D
        d_oi: int
        bias: str

    ref = "2026-08-28"
    # 近月看跌、远月看涨 → 必须判为打架
    legs = [L(D(2026, 8, 29), 9000, "bearish"), L(D(2026, 8, 29), 500, "bullish"),
            L(D(2026, 9, 25), 8000, "bullish"), L(D(2026, 9, 25), 400, "bearish")]
    rows = _split_by_dte(legs, ref)
    assert len(rows) == 2, "0-2天 与 22天+ 两个桶"
    assert rows[0]["sign"] == -1 and rows[1]["sign"] == 1
    assert _dte_dirs_conflict(rows) is True, "近月看跌+远月看涨必须判为打架"

    # 全部同向 → 不算打架
    same = [L(D(2026, 8, 29), 9000, "bearish"), L(D(2026, 9, 25), 8000, "bearish")]
    assert _dte_dirs_conflict(_split_by_dte(same, ref)) is False

    # 缺日期时不得瞎猜
    assert _split_by_dte(legs, "") == []
    assert _split_by_dte([], ref) == []
    print("PASS test_expiry_buckets_split_and_conflict")


def test_index_shows_one_marker_detail_goes_to_instrument_report():
    """index 上只留一个到期一致性标记，四桶明细放品种研报。

    用户 2026-08-29：「index 可以不用拆开这么多到期桶，如果全部同向可以
    额外标注一下。我觉得合并已经很有价值了。品种内部研报可以再拆开」
    """
    from undertow.analyze.flow import expiry_split_html
    from undertow.report.html import _facts_html

    sp_same = [{"bucket": "0-2天", "legs": 5, "dn": 9000, "up": 300,
                "sign": -1, "ratio": 30.0, "doi": 9300},
               {"bucket": "22天+", "legs": 4, "dn": 8000, "up": 200,
                "sign": -1, "ratio": 40.0, "doi": 8200}]
    idx = _facts_html({"exp_split": sp_same, "exp_conflict": False})
    assert "全部同向" in idx, "同向时必须额外标注 —— 这是更强的信号"
    assert "0-2天" not in idx, "index 不该再列各桶明细"

    idx2 = _facts_html({"exp_split": sp_same, "exp_conflict": True})
    assert "各到期方向不一致" in idx2 and "别当强信号看" in idx2
    assert "22天+" not in idx2

    # 明细必须在品种研报的表里
    detail = expiry_split_html(sp_same, lambda x: x)
    assert "0-2天" in detail and "22天+" in detail
    assert "30.0×" in detail and "40.0×" in detail
    assert "开盘前就能算出来" in detail, "必须说明这是可执行闸门而非事后分层"
    print("PASS test_index_shows_one_marker_detail_goes_to_instrument_report")


def test_every_card_shows_both_horizons_with_scores():
    """每个品种都要写出近端和中期，哪怕两层同向也要分开写。

    用户 2026-08-29：白银近端 -0.7 差 0.1 没够到 -0.8 门槛 → 显示"中性"，
    且因为两层不冲突，连分层都不显示，只剩一个"偏多"。可它的实际构成是
    看跌 1 票全来自 Flow、看涨 3 票全来自 Macro。那天白银 -4.38%。
    """
    from undertow.report.html import render_index_html

    items = [
        # 两层同向：以前只显示一个"偏多"
        {"name": "TLT", "fn": "t.html", "bias": "偏多", "conf": "高",
         "near_bias": "偏多(弱)", "mid_bias": "偏多", "near_score": 0.8, "mid_score": 3.9},
        # 贴着门槛的"中性"：必须标出来
        {"name": "SLV", "fn": "s.html", "bias": "偏多", "conf": "中",
         "near_bias": "中性", "mid_bias": "偏多", "near_score": -0.7, "mid_score": 3.1},
    ]
    h = render_index_html(items, "2026-08-28")
    assert h.count("近端") >= 2 and h.count("中期") >= 2, "每张卡都要分开写两层"
    # 「综合」不再上索引页：它就是把两层压成一个字的那个东西
    # （用户 2026-08-29：「你的综合偏多就算了吧，有近端和中期了」）
    assert "综合 偏" not in h, "索引页不得再显示综合结论"
    assert "(+0.8)" in h and "(+3.9)" in h, "必须带分数 —— 门槛是拍的"
    assert "(-0.7)" in h
    assert "仅差 0.1 未过门槛" in h, "贴着门槛的中性必须单独标注"
    # 底部图例
    assert "图例：近端 / 中期怎么分的" in h
    assert "CFTC" in h and "日频" in h
    assert "按【数据更新频率】分的" in h, "必须承认这不是按预测时域分的"
    print("PASS test_every_card_shows_both_horizons_with_scores")
