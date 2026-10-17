"""主墙研究账的调度与运维（Codex 057 R1/R4）：真实脚本段 + 替身命令，验证失败不阻断 daily、采样失败可见。"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _daily_block() -> str:
    src = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    a, b = src.index("# >>> mainwall-daily"), src.index("# <<< mainwall-daily")
    return src[a:b]


def _cov_cmd(new_gaps=0, gaps=0, *, crash=False, mismatch=False, line=None):
    """替身 coverage：按参数写（或不写）本次 run 的状态文件；$6=--status-file 路径，$8=--run-id。"""
    if crash:
        return 'echo Traceback; return 1'
    rid = "bogus" if mismatch else "$8"                          # 双引号内 $8 展开为本次 --run-id
    ov, rc = ("partial", 1) if gaps else ("complete", 0)
    q = '\\"'
    js = (f'{{{q}schema{q}:2,{q}command{q}:{q}mainwall coverage{q},{q}run_id{q}:{q}{rid}{q},'
          f'{q}session{q}:{q}2026-10-19{q},{q}overall{q}:{q}{ov}{q},{q}counts{q}:{{{q}new{q}:{new_gaps}}}}}')
    return f'mkdir -p "${{6:h}}"; echo "{js}" > "$6"; echo "NEW_GAPS {new_gaps if line is None else line}"; return {rc}'


def _run_daily(tmp_path, rc_capture: int, rc_settle: int, new_gaps: int = 0, cov=None):
    cov = cov or _cov_cmd(new_gaps, new_gaps)
    marks = tmp_path / "marks.txt"
    script = f"""set -e
ET_DATE=2026-10-19; ET_NOW=06:00
stage_done() {{ return 1; }}
stage_mark() {{ echo "$1 $2 $3" >> {marks}; }}
rc_status() {{ case "$1" in 0) echo ok ;; 3) echo "${{2:-pending}}" ;; *) echo failed ;; esac; }}
alert() {{ echo "ALERT $1" >> {marks}; }}
python3() {{
  if [[ "$1" == scripts/bound_status.py ]]; then command python3 {ROOT}/scripts/bound_status.py "${{@:2}}"; return; fi
  if [[ "$4" == capture ]]; then echo "⚠️ cap"; return {rc_capture}
  elif [[ "$4" == coverage ]]; then {cov}
  else echo "⚠️ set"; return {rc_settle}; fi; }}
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


def test_daily_coverage_new_gaps_alerted(tmp_path):
    r, marks = _run_daily(tmp_path, 0, 0, new_gaps=3)
    assert "REACHED_DOWNSTREAM" in r.stdout and "ALERT ⚠️ 主墙研究账发现 3 个覆盖缺口" in marks


def test_daily_coverage_crash_or_bad_status_alerts_failure(tmp_path):
    """059 A2：rc1 + 无状态（崩溃）、身份不符、NEW_GAPS 与 counts 不一致 → 核查失败告警，不当成「正常有缺口」。"""
    for i, cov in enumerate((_cov_cmd(crash=True), _cov_cmd(1, 1, mismatch=True), _cov_cmd(0, 1, line=3))):
        d = tmp_path / str(i); d.mkdir()
        r, marks = _run_daily(d, 0, 0, cov=cov)
        assert "REACHED_DOWNSTREAM" in r.stdout and "ALERT ⚠️ 主墙研究账覆盖核查失败" in marks, (i, marks)
    d = tmp_path / "ok"; d.mkdir()
    r, marks = _run_daily(d, 0, 0, cov=_cov_cmd(0, 2))                  # 合法 partial、无新缺口 → 不重复提醒
    assert "ALERT" not in marks


def _hook_prelude() -> str:
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    a = src.index("bound_overall() {")
    b = src.index("ov_text() {")
    return src[a:src.index("\n", b) + 1]


def _run_cov(tmp_path, cov: str, *, et_min=600, reports_ok=True):
    """真实 run_bound / bound_overall / bound_count；$PY 是替身：-c 透传给真 python，-m 跑 cov 场景。"""
    (tmp_path / "logs").mkdir(exist_ok=True)
    if reports_ok:
        (tmp_path / "data/reports").mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fakepy"
    fake.write_text(f"""#!/bin/zsh
if [[ "$1" == -c ]]; then exec python3 "$@"; fi
shift 2   # -m undertow.cli → $1=mainwall $2=coverage $4=状态文件 $6=run_id
_c() {{ {cov.replace('$6', '$4').replace('$8', '$6').replace('${6:h}', '${4:h}')} }}
_c "$@"
""")
    fake.chmod(0o755)
    script = f"""
LOG_DIR=logs; ET_DATE=2026-10-19; ET_MIN={et_min}; PY={fake}
hb() {{ echo "HB $1" >> hb.txt; }}
notify() {{ echo "NOTIFY $1" >> notify.txt; }}
{_hook_prelude()}
{_fn("mainwall_coverage")}
mainwall_coverage; true
"""
    subprocess.run(["zsh", "-c", script], cwd=tmp_path, check=True)
    rd = lambda n: (tmp_path / n).read_text() if (tmp_path / n).exists() else ""
    return rd("notify.txt"), rd("data/reports/FAILURE_2026-10-19.txt"), rd("hb.txt")


def test_hook_coverage_notifies_only_new_gaps(tmp_path):
    note, _, _ = _run_cov(tmp_path, _cov_cmd(2, 2))
    assert "2 个覆盖缺口" in note
    (tmp_path / "b").mkdir()
    note, fail, hb = _run_cov(tmp_path / "b", _cov_cmd(0, 3))           # 合法 partial + new 0：不重复提醒
    assert note == "" and fail == "" and "无新缺口" in hb


def test_hook_coverage_rc1_without_status_is_failure(tmp_path):
    """059 A2 复现：LedgerConflictError 时 rc1、无状态、无 NEW_GAPS —— 旧 hook 只写 HB；现在通知 + 持久兜底。"""
    note, fail, _ = _run_cov(tmp_path, _cov_cmd(crash=True))
    assert "覆盖核查失败" in note and "覆盖核查失败" in fail


def test_hook_coverage_mismatch_or_inconsistent_line_is_failure(tmp_path):
    for i, cov in enumerate((_cov_cmd(1, 1, mismatch=True), _cov_cmd(0, 1, line=2))):
        d = tmp_path / str(i); d.mkdir()
        note, fail, _ = _run_cov(d, cov)
        assert "覆盖核查失败" in note and "覆盖核查失败" in fail, i


def test_hook_coverage_failure_fallback_retried_until_written(tmp_path):
    note, fail, _ = _run_cov(tmp_path, _cov_cmd(crash=True), reports_ok=False)   # 兜底写不进去 → 不建哨兵
    assert fail == "" and not list((tmp_path / "logs").glob(".mainwall_cov_failed_*"))
    (tmp_path / "notify.txt").unlink()
    note, fail, _ = _run_cov(tmp_path, _cov_cmd(crash=True))                    # 下一次唤醒重试并写成
    assert "覆盖核查失败" in fail and list((tmp_path / "logs").glob(".mainwall_cov_failed_*"))
    (tmp_path / "notify.txt").unlink()
    note, fail2, _ = _run_cov(tmp_path, _cov_cmd(crash=True))                   # 同一小时不重复
    assert note == "" and fail2 == fail
