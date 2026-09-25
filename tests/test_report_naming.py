"""研报文件名 = 数据来源日 —— 回归测试。

用户 2026-09-25：「文件名都改成数据来源日期」。

命名口径演进了三次，每次都是被同一类 bug 推着走的：
  生成日 →（2026-08-29）可交易日/快照日 →（2026-09-25）数据来源日
前一版能防住「周六生成的报告写着周六」，但防不住与外部分析对齐时的错位：
数据来自 D−1，名字却写 D。我在 2026-09-23 因此把「结论一致」读成「结论相反」，
**被用户提醒后又错了第二次**。名字写数据来源日，这类错位结构性地不可能发生。

⚠️ 算法必须与 signal_ledger 的 base_date 完全一致（都取"日线序列里早于快照日的
最后一个交易日"）。两者一旦分叉，研报与台账就对不上，错位会以另一种形式回来。
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.cli import _data_source_day as dsd     # noqa: E402

# 真实交易日序列（2026-09-19/20 是周末）
PX = [date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21),
      date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]


def test_normal_day_is_previous_trading_day():
    """快照 D 的 OI 是 D−1 收盘结算的 → 数据日 = D 的上一个交易日。"""
    assert dsd("2026-09-24", "2026-09-23", PX, "gold") == "2026-09-23"
    print("PASS test_normal_day_is_previous_trading_day")


def test_monday_snapshot_names_friday():
    """周一的快照描述【上周五】—— 周末 OI 不结算（flow.py 时序约定，两个周末实测）。

    这是旧口径最刺眼的错位：周一的研报叫 2026-09-21，装的是 9/18 的数据。
    """
    assert dsd("2026-09-21", "2026-09-18", PX, "gold") == "2026-09-18"
    print("PASS test_monday_snapshot_names_friday")


def test_gap_uses_price_series_not_prev_snapshot():
    """漏抓过时，prev_date 会偏早 —— 必须以日线序列为准。

    2026-09-22~24 CBOE 停更实况：快照对是 (9/22, 9/24)，prev_date=9/22，
    但 9/24 快照的 OI 其实来自 **9/23** 收盘。用 prev_date 命名会偏早一天，
    正是这次要消灭的那类错位。
    """
    assert dsd("2026-09-24", "2026-09-22", PX, "gold") == "2026-09-23"
    print("PASS test_gap_uses_price_series_not_prev_snapshot")


def test_holiday_is_not_guessed_as_trading_day():
    """不能用"上一个工作日"近似 —— 它不认识休市日。

    构造：9/23(周三)休市（不在日线序列里），则 9/24 的数据日应是 9/22，
    而"上一个工作日"会给出 9/23 这个根本没有交易的日子。
    """
    px_holiday = [d for d in PX if d != date(2026, 9, 23)]
    assert dsd("2026-09-24", "2026-09-22", px_holiday, "gold") == "2026-09-22"
    print("PASS test_holiday_is_not_guessed_as_trading_day")


def test_degradation_is_loud(capsys):
    """降级必须出声。默默换口径是本仓库最贵的 bug 类别（AGENTS.md 第四节）。"""
    assert dsd("2026-09-24", "2026-09-22", [], "gold") == "2026-09-22"
    err = capsys.readouterr().err
    assert "日线" in err and "偏早" in err, f"退回 prev 时必须警告：{err!r}"

    assert dsd("2026-09-24", None, [], "gold") == "2026-09-24"
    err = capsys.readouterr().err
    assert "快照日" in err, f"退回快照日时必须警告：{err!r}"
    print("PASS test_degradation_is_loud")


def test_matches_ledger_base_date_algorithm():
    """与台账 base_date 同一算法 —— 两者分叉就会让交叉引用重新错位。

    台账的做法（signal_ledger.backfill）：在日线 dates 里二分找出严格早于
    快照日的最后一个索引。这里复刻该语义并逐日比对。
    """
    for snap in PX[1:]:
        earlier = [d for d in PX if d < snap]
        ledger_base = max(earlier).isoformat()
        assert dsd(snap.isoformat(), None, PX, "gold") == ledger_base, snap
    print("PASS test_matches_ledger_base_date_algorithm")


def test_cli_still_reports_both_dates():
    """文件名是数据日，但【可交易日】不能就此消失 —— 那是"哪天能下单"的答案。

    两个日期必须同时出现在 CLI 摘要里，否则读的人只能靠猜。
    """
    src = (ROOT / "undertow" / "cli.py").read_text("utf-8")
    i = src.index("已生成综合研判报告")
    line = src[i:i + 220]
    assert "数据日" in line and "可交易日" in line, line
    assert "文件名" in line, "必须点明哪个日期是文件名，否则仍要猜"
    print("PASS test_cli_still_reports_both_dates")


def test_written_tuple_unpacks_tolerantly():
    """那个 11 元组有 6 处位置解包 —— 必须全部容忍追加字段。

    本次加 _data_day 时六处一起 ValueError 炸掉，所以一律改成尾部 `*_`。
    """
    src = (ROOT / "undertow" / "cli.py").read_text("utf-8")
    seg = src[src.index("def cmd_report"):src.index("def cmd_backtest")]
    bad = [l.strip() for l in seg.splitlines()
           if " in written" in l and "*_" not in l and "written.append" not in l]
    assert not bad, f"这些解包会被新增字段炸掉：{bad}"
    print("PASS test_written_tuple_unpacks_tolerantly")
