"""持仓实时体检的确定性测试。

核心事实（2026-08-26 TQQQ 76/80 实测，同一时刻三个口径）：
    App(last)   1.68-0.68 = $100  →  +$10  ✅ 看着在赚
    中价        1.52-0.67 = $ 85  →  -$5
    真实可平仓   1.37-0.70 = $ 67  →  -$23  ❌ 其实在亏
差 $33。止损判定若看 App，会系统性晚动手——本模块就是为了消除这个偏差。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.livecheck import (LegQuote, build_ledger, check_position,
                                        render_ledger_md, render_md)


def _tqqq():
    return [LegQuote("TQQQ76C", 1, bid=1.37, ask=1.67, last=1.68),
            LegQuote("TQQQ80C", -1, bid=0.64, ask=0.70, last=0.68)]


def test_exit_value_uses_bid_for_long_ask_for_short():
    """多头按 bid 卖、空头按 ask 买回——这是唯一诚实的出场口径。"""
    c = check_position("spread", _tqqq(), cost=90.0)
    assert abs(c.exit_value - (1.37 - 0.70) * 100) < 1e-6, c.exit_value
    assert abs(c.last_value - (1.68 - 0.68) * 100) < 1e-6
    assert abs(c.mid_value - (1.52 - 0.67) * 100) < 1e-6
    print("PASS test_exit_value_uses_bid_for_long_ask_for_short")


def test_app_optimism_is_flagged():
    """App 口径高出真实可平仓 15% 以上必须告警——这正是当日 $33 的缺口。"""
    c = check_position("spread", _tqqq(), cost=90.0)
    assert c.pnl_exit < 0 < c.pnl_last, (c.pnl_exit, c.pnl_last)   # 一个亏一个赚
    assert any("App" in w for w in c.warnings), c.warnings
    assert abs(c.gap - 33.0) < 1e-6, c.gap
    print("PASS test_app_optimism_is_flagged")


def test_missing_side_returns_none_not_guess():
    """单边空档时不许拿 last 顶替——算不出就说算不出。"""
    legs = [LegQuote("A", 1, bid=None, ask=1.67, last=1.68),
            LegQuote("B", -1, bid=0.64, ask=0.70, last=0.68)]
    c = check_position("x", legs, cost=90.0)
    assert c.exit_value is None and c.mid_value is None
    assert c.last_value is not None            # last 口径仍可算，但只作参考
    assert any("单边缺失" in w for w in c.warnings)
    print("PASS test_missing_side_returns_none_not_guess")


def test_zero_value_still_triggers_stop():
    """可平仓价恰好归零时必须报「已触及止损」——这正是最该告警的时刻。

    旧写法用 `and ev` 做真值判断，会把 0.0 当成缺失，在最该喊的时候闭嘴。
    （codex review 2026-08-26）
    """
    legs = [LegQuote("A", 1, bid=0.0, ask=0.01, last=0.01),
            LegQuote("B", -1, bid=0.0, ask=0.0, last=0.0)]
    c = check_position("dead", legs, cost=90.0, stop=45.0)
    assert c.exit_value == 0.0
    assert any("已触及止损" in w for w in c.warnings), c.warnings
    print("PASS test_zero_value_still_triggers_stop")


def test_stop_and_target_flags():
    c = check_position("spread", _tqqq(), cost=90.0, stop=80.0)
    assert any("已触及止损" in w for w in c.warnings), c.warnings
    c2 = check_position("spread", _tqqq(), cost=90.0, stop=45.0)
    assert not any("已触及止损" in w for w in c2.warnings)
    assert c2.to_stop_pct > 0
    c3 = check_position("spread", _tqqq(), cost=90.0, target=60.0)
    assert any("已达止盈" in w for w in c3.warnings)
    print("PASS test_stop_and_target_flags")


def test_short_only_position():
    """纯空头：平仓要按 ask 买回，价值为负。"""
    c = check_position("naked", [LegQuote("S", -1, bid=0.64, ask=0.70, last=0.68)], cost=-68.0)
    assert abs(c.exit_value - (-70.0)) < 1e-6
    print("PASS test_short_only_position")


def test_total_exposure_flags_incomplete():
    """任一持仓算不出可平仓价时，总敞口必须显式标为【不完整的下界】。

    这是第一轮修复（None 传播）引入的新风险：TQQQ 缺价时总敞口只算了 SLV 的 $19，
    显示「净资产 4.5%」，看着还有大量加仓空间——而真实敞口还要加上那笔约 $70。
    **低估敞口比高估危险**。
    """
    good = check_position("有价", _tqqq(), cost=90.0)
    bad = check_position("缺价", [LegQuote("A", 1, bid=None, ask=1.0, last=1.0)], cost=50.0)
    md = render_md([good, bad], net_assets=436.77)
    assert "总敞口不完整" in md and "缺价" in md
    assert "这是下界，不是实际敞口" in md
    # 全部有价时不应出现该告警
    md2 = render_md([good], net_assets=436.77)
    assert "总敞口不完整" not in md2 and "总敞口（可平仓口径）" in md2
    print("PASS test_total_exposure_flags_incomplete")


def test_zero_value_position_counted_in_total():
    """价值恰好为 0 的持仓要计入总敞口，不能被真值判断跳过。"""
    z = check_position("零值", [LegQuote("A", 1, bid=0.0, ask=0.0, last=0.0)], cost=10.0)
    md = render_md([z], net_assets=100.0)
    assert "总敞口不完整" not in md, "0 值不是缺价，不该报不完整"
    print("PASS test_zero_value_position_counted_in_total")


def test_render_contains_exit_basis_warning():
    md = render_md([check_position("spread", _tqqq(), cost=90.0, stop=45.0)], net_assets=436.77)
    # ⚠️ 断言不得与运行时刻相关：盘中/休市两版措辞不同，但「止损该看什么」
    # 这条指引必须始终在场（休市版是"不得据本表判止损"）。
    assert "真实可平仓" in md
    assert ("止损判定用本表" in md) or ("不得据本表判止损" in md)
    assert "总敞口" in md
    print("PASS test_render_contains_exit_basis_warning")


def test_empty_legs():
    c = check_position("x", [])
    assert not c.ok
    print("PASS test_empty_legs")


# ── 品种累计台账 ──────────────────────────────────────────────────

def _slv_flows():
    """2026-08-26 SLV 70C/73C 的真实流水（含 8/20-8/21 那轮盈利的往返）。"""
    return [
        {"symbol": "SLV260918C70000.US", "balance": "-336.00"},   # 8/20 买
        {"symbol": "SLV260918C70000.US", "balance": "-2.41"},
        {"symbol": "SLV260918C70000.US", "balance": "+384.00"},   # 8/21 卖 → 第一轮赚
        {"symbol": "SLV260918C70000.US", "balance": "-2.43"},
        {"symbol": "SLV260918C70000.US", "balance": "-105.00"},   # 8/24 买 3 张
        {"symbol": "SLV260918C70000.US", "balance": "-105.00"},
        {"symbol": "SLV260918C70000.US", "balance": "-105.00"},
        {"symbol": "SLV260918C70000.US", "balance": "-2.42"},
        {"symbol": "SLV260918C70000.US", "balance": "+126.00"},   # 8/25 卖 2 张 → 亏损已实现
        {"symbol": "SLV260918C70000.US", "balance": "-1.61"},
        {"symbol": "SLV260918C73000.US", "balance": "+36.00"},    # 8/26 卖保护腿
    ]


def test_ledger_matches_hand_computed_total():
    """台账必须复现手工核对的 -97.47 —— 这是当日的锚定值。"""
    g = build_ledger("SLV", _slv_flows(), closeable=18.0, exit_fee=1.60)
    assert abs(g.net_cash_flow - (-113.87)) < 1e-9, g.net_cash_flow
    assert abs(g.total - (-97.47)) < 1e-9, g.total
    print("PASS test_ledger_matches_hand_computed_total")


def test_ledger_missing_quote_is_not_zero():
    """盘口缺失时 total 必须是 None，不能用 0 冒充可平仓价。

    折零会把「行情拿不到」显示成「持仓已归零」，并据此算出一个假的最终亏损——
    对小账户，这个数字足以直接改变平仓判断。（codex review 2026-08-26）
    """
    g = build_ledger("X", _slv_flows(), closeable=None, exit_fee=1.60)
    assert g.closeable is None and g.total is None
    md = render_ledger_md([g])
    assert "不可计算" in md and "盘口缺失" in md
    print("PASS test_ledger_missing_quote_is_not_zero")


def test_ledger_ignores_broker_cost_basis():
    """台账只吃现金流水，不接受任何「成本价」输入——券商的 1.89 是摊销产物。

    实付每张 1.05，券商显示 1.89 = (3×1.05 − 2×0.63)/1，是把已实现亏损摊进了
    剩余持仓。用它判断亏损会同时错两次：既非实付价，也不含更早那轮的 +43.16。
    """
    import inspect
    src = inspect.getsource(build_ledger)
    assert "cost" not in src.lower(), "build_ledger 不应接触成本价"
    print("PASS test_ledger_ignores_broker_cost_basis")


def test_ledger_bad_rows_are_skipped_not_crashed():
    rows = [{"symbol": "X", "balance": "10.0"}, {"symbol": "X", "balance": None},
            {"symbol": "X"}, {"symbol": "X", "balance": "abc"}]
    g = build_ledger("X", rows, closeable=0.0)
    assert abs(g.net_cash_flow - 10.0) < 1e-9
    print("PASS test_ledger_bad_rows_are_skipped_not_crashed")


def test_ledger_render_warns_about_cost_basis():
    md = render_ledger_md([build_ledger("SLV", _slv_flows(), 18.0, 1.60)])
    assert "不可用来判断亏了多少" in md
    assert "沉没" in md
    # 必须明说「净现金流 ≠ 已实现盈亏」——前者含未平仓头寸的建仓支出
    assert "不等于「已实现盈亏」" in md
    assert render_ledger_md([]) == ""
    print("PASS test_ledger_render_warns_about_cost_basis")




def test_market_session_flags_closed_hours():
    """休市时段必须明示报价不可成交 —— 否则会据昨夜残留挂单误判止损。

    2026-08-27 ET03:27 实测：TQQQ 价差「真实可平仓」显示 $67、中价 $84，
    差 $17 全部来自休市宽点差，而报表当时毫无提示，据此算出的「距止损 33%」
    是个不可成交的数。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from undertow.analyze.livecheck import market_session
    ET = ZoneInfo("America/New_York")
    assert market_session(datetime(2026, 8, 27, 3, 27, tzinfo=ET))[0] == "休市"
    assert market_session(datetime(2026, 8, 27, 9, 29, tzinfo=ET))[0] == "休市"
    assert market_session(datetime(2026, 8, 27, 9, 30, tzinfo=ET))[0] == "盘中"
    assert market_session(datetime(2026, 8, 27, 15, 59, tzinfo=ET))[0] == "盘中"
    assert market_session(datetime(2026, 8, 27, 16, 0, tzinfo=ET))[0] == "休市"
    assert market_session(datetime(2026, 8, 29, 11, 0, tzinfo=ET))[0] == "休市"   # 周六
    assert "期权盘未开" in market_session(datetime(2026, 8, 27, 3, 27, tzinfo=ET))[1]
    print("PASS test_market_session_flags_closed_hours")




def test_session_hooks_has_postevent_window():
    """事件后复核窗口必须存在，且只在【今日有高影响事件】+【有持仓】时才跑。

    美国宏观数据/美联储讲话多在 ET 10:00 落地。事件后 IV 与价格骤变，
    ET09:40 那次体检算出的「距止损 X%」当场作废 —— 而 TQQQ 这类组合单
    在券商端锁腿、**挂不了自动止损**，只能靠提醒后手动平，
    所以事件后必须重新核一次真实可平仓价。

    同时守住三条既有铁律：
      · live 失败不得写成"成功"文件（否则幂等检查阻止当日重试）
      · 用 mkdir 原子锁防并发重入
      · 无持仓则跳过，不产出空文件
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "scripts" / "session_hooks.sh"
    txt = src.read_text(encoding="utf-8")
    assert "ET_MIN >= 610 && ET_MIN <= 625" in txt, "缺事件后复核窗口 ET10:10-10:25"
    assert "_postevent.md" in txt
    assert ".lock_postevent_" in txt, "事件后分支缺并发锁"
    assert "不落盘，等下一次唤醒重试" in txt
    assert "当前无持仓" in txt
    # 必须先判有无高影响事件，无事件则跳过
    assert "今日无高影响事件" in txt
    # 距止损过近要单独告警（止损是手动的）
    assert "距止损仅" in txt
    print("PASS test_session_hooks_has_postevent_window")




def test_daily_update_alerts_on_silent_failure():
    """快照/研报失败必须【推送】，不能只写进日志 —— 静默失败是最危险的失败。

    2026-08-28 复盘发现：8/21 ET02:07、8/22 ET02:09 两次全部品种
    「网络错误 nodename nor servname」→「没有保存任何快照」，**只写进了日志文件**。
    用户不会去翻日志；若当天后续重试点也失败，他会以为一切正常而实际当天无数据。
    整条交易流程依赖每日研报（据其期权结构决定交易策略），缺一天必须当场知道。

    三条必须守住：
      · 全部抓取失败 → 告警
      · 全部因「与上一交易日逐行相同」跳过（OCC 未结算）→ **正常，不得告警**
      · 末班车（ET≥8时）仍缺当日快照 → 告警（今天大概率补不上了）
    """
    from pathlib import Path
    txt = (Path(__file__).resolve().parents[1] / "scripts" / "daily_update.sh").read_text(encoding="utf-8")
    assert "快照失败" in txt and "没有保存任何快照" in txt, "缺失败检测"
    assert "全部品种抓取失败" in txt, "缺全失败告警"
    assert "研报生成失败" in txt, "缺研报失败告警"
    assert "IS_LAST_SLOT" in txt and "末班车" in txt, "缺末班车兜底告警"
    # 关键：必须能区分「抓取失败」与「OCC 未结算」，后者不得告警
    assert "无 failure 行 = 全部因" in txt, "缺「未结算不告警」的区分逻辑"
    print("PASS test_daily_update_alerts_on_silent_failure")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
