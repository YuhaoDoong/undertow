"""数据源停更告警 —— 回归测试。

2026-09-24 实测：CBOE 期权接口卡在 `2026-09-22T15:59:59` 超过 34 小时。
9/23 四个时点全部判「与上一交易日逐行相同」→ 一份快照没落盘、研报缺一天、
**而且零告警**，是用户追问「9/22 收盘的 OI 呢」才发现的。

根因：原设计只区分两种情况 ——
    「今天还没结算」→ unchanged，静默重试（正常，不该告警，否则天天狼来了）
    「抓取失败」    → failed，告警
**漏掉了第三种：源还活着但停止更新。** 它伪装成前者（同样是 unchanged、
同样没有网络错误），后果却和后者一样：数据永久缺失，而期权链不可再生。

判据：最新快照距今已跨 ≥2 个【工作日】仍无新数据。
用工作日而非日历日 —— 周五收盘的 OI 周六周日本就拿不到（见 flow.py 时序约定），
按日历日会每个周末误报一次。
"""
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _stale_workdays(last: str, today: str) -> int:
    """复刻 daily_update.sh 里的工作日差算法。"""
    n, d = 0, date.fromisoformat(last)
    t = date.fromisoformat(today)
    while d < t:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def test_same_day_no_update_stays_silent():
    """源当天还没结算 = 正常，绝不能告警 —— 否则天天响，真出事反被忽略。"""
    assert _stale_workdays("2026-09-22", "2026-09-23") == 1


def test_weekend_gap_not_reported():
    """周五→周一只算 1 个工作日。

    周五收盘的 OI 周六周日拿不到是既定事实（flow.py 时序约定里有两个周末的实测），
    按日历日算会是 3 天，每周误报一次。
    """
    assert _stale_workdays("2026-09-18", "2026-09-21") == 1, "周末不得计入"
    assert _stale_workdays("2026-09-18", "2026-09-22") == 2, "周五→周二确实漏了一天"


def test_cross_session_stall_triggers():
    """跨交易日仍无新数据 → 必须告警。这正是 2026-09-24 的实况。"""
    assert _stale_workdays("2026-09-22", "2026-09-24") == 2


def test_script_has_stale_guard_in_unchanged_branch():
    """告警必须长在 unchanged 分支里 —— 那是它唯一能被触发的地方。

    failed/crashed 分支本就会告警；停更的特征恰恰是「看起来一切正常」。
    """
    src = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    head, _, tail = src.partition("unchanged)")
    assert tail, "unchanged 分支必须存在"
    branch = tail.split("esac")[0]
    assert "STALE_DAYS" in branch, "停更判据必须在 unchanged 分支内"
    assert "alert" in branch, "跨交易日停更必须走 alert（推送 + 落兜底文件）"
    assert "weekday() < 5" in branch, "必须按工作日计数，不能用日历日"


def test_script_syntax_valid():
    """改完必须能过语法检查 —— 无人值守脚本语法错 = 静默不执行。"""
    r = subprocess.run(["zsh", "-n", str(ROOT / "scripts" / "daily_update.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
