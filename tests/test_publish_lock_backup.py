"""全局发布锁（shlock，失效锁可接管）与收盘备份（Codex 024-6）。临时 git 仓库，不碰真实仓库。"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("zsh") is None or shutil.which("shlock") is None, reason="需要 zsh 与 shlock")


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def _repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/lib_publish.sh", tmp_path / "scripts/lib_publish.sh")
    (tmp_path / "data/history").mkdir(parents=True)
    _git(tmp_path, "init", "-q"); _git(tmp_path, "config", "user.email", "t@t"); _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("x"); _git(tmp_path, "add", "README"); _git(tmp_path, "commit", "-qm", "init")


def _publish(tmp_path, pre=""):
    script = f"""
source scripts/lib_publish.sh
publish_begin data/history
{pre}
echo new > data/history/a.txt
PUBLISH_NO_PUSH=1 PUBLISH_LOCK_WAIT=2 publish_dirs "t" data/history; echo "RC=$?"
"""
    return subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout


def test_publish_blocked_by_live_lock_holder(tmp_path):
    _repo(tmp_path)
    holder = subprocess.Popen(["sleep", "30"])
    try:
        lockf = tmp_path / ".git/undertow_publish.lock"
        lockf.write_text(f"{holder.pid}\n")
        out = _publish(tmp_path)
        assert "RC=7" in out and "PUBLISH_BUSY" in out
        assert "a.txt" not in _git(tmp_path, "log", "--name-only", "--format=").stdout      # 产物保留、未提交
    finally:
        holder.kill(); holder.wait()


def test_stale_lock_of_dead_process_is_taken_over(tmp_path):
    _repo(tmp_path)
    dead = subprocess.Popen(["true"]); dead.wait()
    (tmp_path / ".git/undertow_publish.lock").write_text(f"{dead.pid}\n")
    out = _publish(tmp_path)
    assert "RC=0" in out and "a.txt" in _git(tmp_path, "log", "--name-only", "--format=").stdout
    assert not (tmp_path / ".git/undertow_publish.lock").exists()                           # 用完释放


def test_session_hook_close_backup_wiring():
    src = (ROOT / "scripts/session_hooks.sh").read_text("utf-8")
    assert src.index("close_backup() {") < src.index("then close_backup; fi")
    assert "(( ET_MIN >= 1000 )); then close_backup; fi" in src
    body = src[src.index("close_backup() {"):src.index("then close_backup; fi")]
    assert "publish_dirs" in body and "data/history data/snapshots" in body and "git add" not in body   # 不全仓 add
    assert src.index("source scripts/lib_publish.sh") < src.index("close_backup() {")
