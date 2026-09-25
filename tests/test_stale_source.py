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
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from undertow.core.clock import STALE_SESSIONS, sessions_between   # noqa: E402


def _stale_workdays(last: str, today: str) -> int:
    """⚠️ 直接调被测实现，**不复刻**。

    本文件原先自己写了一遍 while 循环 —— 那意味着 shell 一份、测试一份、
    后来 cmd_snapshot 的降级开关又一份，三处漂移只是时间问题
    （AGENTS.md：同一个量不许在两处各算一遍）。
    """
    return sessions_between(date.fromisoformat(last), date.fromisoformat(today))


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
    # 断言的是【调用共用实现】，而不是某段具体算法 ——
    # 从前这里断言 "weekday() < 5"，等于把实现细节钉死在 shell 里，
    # 共用化之后反而会拦住正确的改动。
    assert "sessions_between" in branch, "工作日差必须走 core.clock 的共用实现"
    assert "STALE_SESSIONS" in branch, "阈值必须来自 core.clock，不许在 shell 里写死"


def test_script_syntax_valid():
    """改完必须能过语法检查 —— 无人值守脚本语法错 = 静默不执行。"""
    r = subprocess.run(["zsh", "-n", str(ROOT / "scripts" / "daily_update.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_only_irreproducible_data_is_tracked():
    """同步原则（用户 2026-09-25）：只入库不可再生的东西。

    研报 HTML/PDF 是纯派生物（快照 + 代码即可重算），实测却占了工作区 57%
    （157MB，其中 archive 100MB + replay 30MB）与 git 历史 13%（32MB）。

    ⚠️ 但 FAILURE_*/ALERT_* 必须继续跟踪 —— 它们不是研报，是【失败的唯一凭证】。
    AGENTS.md 第四节：只弹通知不够（launchd 下 osascript 未必弹得出），
    这个落盘文件是事后唯一能证明"那天确实失败过"的东西。
    一起忽略掉 = 把静默失败亲手造回来。
    """
    import subprocess as sp

    def ignored(path: str) -> bool:
        # check-ignore 退出码 0 = 被忽略
        return sp.run(["git", "check-ignore", "-q", path],
                      cwd=ROOT).returncode == 0

    assert ignored("data/reports/gold_2026-09-24.html"), "研报 HTML 应忽略（可再生）"
    assert ignored("data/reports/index_2026-09-24.html"), "索引页应忽略（可再生）"
    assert ignored("data/reports/archive/x.html"), "归档研报应忽略"
    assert ignored("data/backtest/step2_grid.jsonl"), "回测中间网格应忽略（可重算）"

    assert not ignored("data/reports/FAILURE_2026-09-24.txt"), \
        "FAILURE 文件是失败的唯一凭证，必须入库"
    assert not ignored("data/reports/ALERT_2026-09-24.txt"), \
        "ALERT 文件是失败的唯一凭证，必须入库"
    assert not ignored("data/snapshots/options/GLD/2026-09-24.json.gz"), \
        "期权链快照不可再生，必须入库"
    assert not ignored("data/history/signals/ledger.jsonl"), \
        "台账是本项目唯一认可的统计口径，必须入库"
    assert not ignored("data/backtest/sell_put_wall_best.jsonl"), \
        "逐笔账本是已停用策略的失败证据，report/html.py 直接引用，必须入库"

    # 无人值守脚本必须真的会 add 到那两个凭证 —— 光有 ! 例外没用，
    # 如果脚本不 add data/reports，FAILURE 文件永远进不了 git。
    sh = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    add_line = next(l for l in sh.splitlines() if l.startswith("git add "))
    assert "data/snapshots" in add_line and "data/history" in add_line
    assert "data/reports" in add_line, \
        "必须仍 add data/reports，否则 FAILURE_/ALERT_ 凭证进不了 git"
    print("PASS test_only_irreproducible_data_is_tracked")
