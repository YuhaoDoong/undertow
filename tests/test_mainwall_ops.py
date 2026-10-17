"""主墙研究账的调度与运维（Codex 057 R1/R4）：真实脚本段 + 替身命令，验证失败不阻断 daily、采样失败可见。"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _daily_block() -> str:
    src = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    a, b = src.index("# >>> mainwall-daily"), src.index("# <<< mainwall-daily")
    return src[a:b]


def _run_daily(tmp_path, rc_capture: int, rc_settle: int):
    marks = tmp_path / "marks.txt"
    script = f"""set -e
ET_DATE=2026-10-19; ET_NOW=06:00
stage_done() {{ return 1; }}
stage_mark() {{ echo "$1 $2 $3" >> {marks}; }}
rc_status() {{ case "$1" in 0) echo ok ;; 3) echo "${{2:-pending}}" ;; *) echo failed ;; esac; }}
alert() {{ echo "ALERT $1" >> {marks}; }}
python3() {{ if [[ "$4" == capture ]]; then echo "⚠️ cap"; return {rc_capture}; else echo "⚠️ set"; return {rc_settle}; fi; }}
{_daily_block()}
echo REACHED_DOWNSTREAM
"""
    r = subprocess.run(["zsh", "-c", script], capture_output=True, text=True, cwd=tmp_path)
    return r, (marks.read_text() if marks.exists() else "")


def test_optional_mainwall_failure_does_not_abort_daily(tmp_path):
    r, marks = _run_daily(tmp_path, 1, 1)
    assert r.returncode == 0 and "REACHED_DOWNSTREAM" in r.stdout
    assert "mainwall_capture failed 1" in marks and "mainwall_settle failed 1" in marks
    assert "ALERT ⚠️ 主墙研究账结算失败" in marks                 # 失败照样可见，没有被 || true 吞掉


def test_optional_mainwall_success_marks_ok(tmp_path):
    r, marks = _run_daily(tmp_path, 0, 0)
    assert r.returncode == 0 and "mainwall_capture ok 0" in marks and "ALERT" not in marks


def test_block_restores_errexit():
    lines = [ln.strip() for ln in _daily_block().splitlines()]
    i_off, i_on = lines.index("set +e"), lines.index("set -e")
    i_cap = next(i for i, ln in enumerate(lines) if "mainwall capture --status-file" in ln)
    assert i_off < i_cap < i_on
    assert not any("|| true" in ln for ln in lines[i_off:i_on])


def _fn(name: str) -> str:
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    a = src.index(f"{name}() {{")
    return src[a:src.index("\n}\n", a) + 3]


def _run_sample(tmp_path, et_min: int, rc=1, ov="partial", notify_ok=True):
    (tmp_path / "data/reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    script = f"""
LOG_DIR=logs; ET_DATE=2026-10-19; ET_MIN={et_min}
hb() {{ echo "HB $1" >> hb.txt; }}
ov_text() {{ echo "$1"; }}
notify() {{ echo "NOTIFY $1" >> notify.txt; {'' if notify_ok else 'return 1'}; }}
run_bound() {{ RB_RES="  主墙账盘中采样 10:00：成员 5，已观测 1 → {ov}"; RB_RC={rc}; RB_OV={ov}; }}
{_fn("mainwall_sample")}
mainwall_sample 585 779
"""
    subprocess.run(["zsh", "-c", script], cwd=tmp_path, check=True)
    f = tmp_path / "data/reports/FAILURE_2026-10-19.txt"
    return (f.read_text() if f.exists() else ""), ((tmp_path / "notify.txt").read_text() if (tmp_path / "notify.txt").exists() else "")


def test_sample_failure_alerts_at_bucket_end_once_with_fallback(tmp_path):
    fail, note = _run_sample(tmp_path, 600 - 8)                    # 桶 [585,600)：末 6 分钟之前不提醒
    assert fail == "" and note == ""
    fail, note = _run_sample(tmp_path, 596)
    assert "盘中采样本桶未补齐" in fail and "NOTIFY" in note
    fail2, _ = _run_sample(tmp_path, 598)                          # 同桶不重复
    assert fail2 == fail


def test_sample_alert_fallback_written_even_if_notify_fails(tmp_path):
    fail, _ = _run_sample(tmp_path, 597, notify_ok=False)
    assert "盘中采样本桶未补齐" in fail


def test_sample_complete_no_alert(tmp_path):
    fail, note = _run_sample(tmp_path, 597, rc=0, ov="complete")
    assert fail == "" and note == ""
