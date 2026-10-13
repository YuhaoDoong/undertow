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


def test_concurrent_auto_writers_do_not_wedge_publish(tmp_path):
    """2026-10-08~12 daily 连续冲突：A 启动时文件=已登记 h0；B 写 h1 并登记；A 再写 h2。
    旧判据只比最后登记的 h1 → A 判冲突且不登记 h2 → 此后永久冲突。现：pre 命中任一登记哈希即本任务产物。"""
    _repo(tmp_path)
    (tmp_path / "data/history/a.txt").write_text("base\n"); _git(tmp_path, "add", "-A"); _git(tmp_path, "commit", "-qm", "b")
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending; : > $PUBLISH_PENDING
echo h0 > data/history/a.txt; printf 'data/history/a.txt\\t%s\\n' "$(git hash-object data/history/a.txt)" >> $PUBLISH_PENDING
publish_begin data/history                                  # A 启动：h0（已登记）
echo h1 > data/history/a.txt; printf 'data/history/a.txt\\t%s\\n' "$(git hash-object data/history/a.txt)" >> $PUBLISH_PENDING   # B 写并登记
echo h2 > data/history/a.txt                                # A 再写
PUBLISH_NO_PUSH=1 PUBLISH_LOCK_WAIT=2 publish_dirs "t" data/history; echo "RC=$?"
"""
    out = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "RC=0" in out and "PUBLISH_CONFLICT" not in out
    assert _git(tmp_path, "show", "HEAD:data/history/a.txt").stdout == "h2\n"


def test_unregistered_foreign_edit_still_conflicts(tmp_path):
    """真正的他人改动（运行前内容从未被自动任务登记）运行中又被写 → 仍判冲突、不发布。"""
    _repo(tmp_path)
    (tmp_path / "data/history/a.txt").write_text("base\n"); _git(tmp_path, "add", "-A"); _git(tmp_path, "commit", "-qm", "b")
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending; printf 'data/history/a.txt\\tdeadbeef\\n' > $PUBLISH_PENDING
echo manual > data/history/a.txt
publish_begin data/history
echo auto > data/history/a.txt
PUBLISH_NO_PUSH=1 PUBLISH_LOCK_WAIT=2 publish_dirs "t" data/history; echo "RC=$?"
"""
    out = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "RC=5" in out and "PUBLISH_CONFLICT" in out


def test_pending_append_not_lost_during_consume(tmp_path):
    """消费已提交登记（读→覆盖）与别的进程登记互斥：持锁期间的登记要等锁，不会被覆盖掉。
    10/13 首版锁文件不存在 → zsystem flock 失败 → 退化为无锁，旧测试因时序巧合仍通过；此版无锁必失败。"""
    _repo(tmp_path)
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending; printf 'x\\t1\\ny\\t2\\n' > $PUBLISH_PENDING
printf 'x\\t1\\n' > data/cm
slow_consume() { local c; c=$(cat $PUBLISH_PENDING); sleep 1; print -r -- "$c" > $PUBLISH_PENDING; _publish_pend_consume "$@"; }
_publish_pend_locked slow_consume data/cm &   # 先读、睡、再整份写回：无锁时 0.3s 的追加必被覆盖
sleep 0.3
_publish_pend_locked eval 'printf "z\\t3\\n" >> $PUBLISH_PENDING'
wait
cat $PUBLISH_PENDING
"""
    out = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout
    assert out.split() == ["y", "2", "z", "3"]


def test_consume_keeps_newer_uncommitted_version(tmp_path):
    """Codex 035 反例：A 提交 h1 后 B 登记 h2，A 清理不能把 h2 一起删掉；036：不按登记顺序推断版本，只删精确相等。"""
    _repo(tmp_path)
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending
printf 'a\\th0\\na\\th1\\nb\\thb\\na\\th2\\n' > $PUBLISH_PENDING
printf 'a\\th1\\n' > data/cm
_publish_pend_locked _publish_pend_consume data/cm; echo "RC=$?"
cat $PUBLISH_PENDING
"""
    out = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "RC=0" in out and out.split()[1:] == ["a", "h0", "b", "hb", "a", "h2"]   # 只删精确相等的 (a, h1)


def test_lock_unavailable_never_mutates_and_record_survives_in_spool(tmp_path):
    """Codex 035 反例：锁不可用时不得无锁改文件、不得返回 0；登记留在 spool，分类照样认得。"""
    _repo(tmp_path)
    (tmp_path / "data/history/a.txt").write_text("base\n"); _git(tmp_path, "add", "-A"); _git(tmp_path, "commit", "-qm", "b")
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending; : > $PUBLISH_PENDING
probe() { print MUTATED; }
zmodload() { return 1; }
_publish_pend_locked probe; echo "RC=$?"
publish_begin data/history
echo h1 > data/history/a.txt
publish_record data/history; echo "REC=$?"
echo "MAIN=$(wc -l < $PUBLISH_PENDING | tr -d ' ')"
unfunction zmodload
PRE2=$(mktemp); printf 'data/history/a.txt\\t%s\\n' "$(git hash-object data/history/a.txt)" > $PRE2; PUBLISH_PRE=$PRE2
_publish_classify data/history
"""
    r = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert "MUTATED" not in r.stdout and "RC=8" in r.stdout and "REC=8" in r.stdout
    assert "MAIN=0" in r.stdout and "LOCK_UNAVAILABLE" in r.stderr
    assert "OURS\tdata/history/a.txt" in r.stdout                       # 只在 spool 里也认得


def test_real_repo_publish_keeps_registration_made_during_commit(tmp_path):
    """真实临时仓库：A 发布 h1 的提交过程中 B 写 h2 并登记 → A 消费后 h2 的登记仍在、下次 A 认得 h2 是自动产物。"""
    _repo(tmp_path)
    (tmp_path / "data/history/a.txt").write_text("base\n"); _git(tmp_path, "add", "-A"); _git(tmp_path, "commit", "-qm", "b")
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending; : > $PUBLISH_PENDING
publish_begin data/history
echo h1 > data/history/a.txt
git() {   # 包一层：commit 刚完成时模拟 B 写 h2 并登记
  command git "$@"; local rc=$?
  if [[ "$1" == commit ]]; then
    echo h2 > data/history/a.txt
    printf 'data/history/a.txt\\t%s\\n' "$(command git hash-object data/history/a.txt)" >> $PUBLISH_PENDING
  fi
  return $rc
}
PUBLISH_NO_PUSH=1 PUBLISH_LOCK_WAIT=2 publish_dirs "t" data/history; echo "RC=$?"
unfunction git
echo "HEAD=$(git show HEAD:data/history/a.txt)"
grep -c "$(git hash-object data/history/a.txt)" $PUBLISH_PENDING
"""
    out = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "RC=0" in out and "HEAD=h1" in out
    assert out.strip().splitlines()[-1] == "1"                                # h2 的登记保留


def test_late_old_registration_does_not_delete_newer(tmp_path):
    """Codex 036 反例：迟到的旧 h1 登记排在未提交 h2 之后；提交的是 h1 → 只删 h1，h2 保留。"""
    _repo(tmp_path)
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending
printf 'f\\th_new_uncommitted\\nf\\th_committed\\n' > $PUBLISH_PENDING
mkdir -p $PUBLISH_PENDING.spool; printf 'f\\th_committed\\n' > $PUBLISH_PENDING.spool/1.1.1.rec; printf 'g\\tx\\n' > $PUBLISH_PENDING.spool/1.1.2.rec
printf 'f\\th_committed\\n' > data/cm
_publish_pend_locked _publish_pend_consume data/cm; echo "RC=$?"
cat $PUBLISH_PENDING; ls $PUBLISH_PENDING.spool | wc -l | tr -d ' '
"""
    out = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True).stdout.split()
    assert out[0] == "RC=0" and out[1:5] == ["f", "h_new_uncommitted", "g", "x"] and out[-1] == "0"


def test_record_not_persisted_blocks_publish(tmp_path):
    """登记根本没写进 spool（目录不可写）→ rc 9，publish_dirs 停止发布；与「已落 spool、锁暂不可用」(8) 区分。"""
    _repo(tmp_path)
    script = """
source scripts/lib_publish.sh
export PUBLISH_PENDING=data/pending; : > $PUBLISH_PENDING
touch $PUBLISH_PENDING.spool                       # 同名普通文件 → spool 目录建不了
publish_begin data/history
echo x > data/history/a.txt
PUBLISH_NO_PUSH=1 PUBLISH_LOCK_WAIT=2 publish_dirs "t" data/history; echo "RC=$?"
"""
    r = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert "RC=9" in r.stdout and "PUBLISH_RECORD_FAILED" in r.stdout and "NOT_PERSISTED" in r.stderr
