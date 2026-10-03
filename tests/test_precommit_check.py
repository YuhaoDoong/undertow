"""提交前检查入口（Codex 027）：假 pytest 返回非零时，`precommit_check.sh && git commit` 的提交命令不会被调用；
暂存敏感路径同样阻断。临时 git 仓库，不碰真实仓库。"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="需要 zsh")


def _repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/precommit_check.sh", tmp_path / "scripts/precommit_check.sh")
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)


def _run(tmp_path, pytest_cmd):
    marker = tmp_path / "COMMIT_CALLED"
    script = f'PYTEST="{pytest_cmd}" scripts/precommit_check.sh "{tmp_path}/log.txt" && touch "{marker}" && git commit -qm t'
    r = subprocess.run(["zsh", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    n = subprocess.run(["git", "rev-list", "--all", "--count"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    return r, marker.exists(), int(n or 0)


@pytest.mark.parametrize("fake,rc", [("false", 1), ("sh -c 'echo 1 failed; exit 1'", 1), ("sh -c 'exit 5'", 5)])
def test_failing_pytest_never_reaches_commit(tmp_path, fake, rc):
    _repo(tmp_path)
    r, called, commits = _run(tmp_path, fake)
    assert r.returncode == 10 and "PRECOMMIT_FAIL" in r.stdout and f"rc={rc}" in r.stdout
    assert not called and commits == 0


def test_passing_pytest_allows_commit(tmp_path):
    _repo(tmp_path)
    r, called, commits = _run(tmp_path, "sh -c 'echo 3 passed; exit 0'")
    assert r.returncode == 0 and "PRECOMMIT_OK" in r.stdout and called and commits == 1


def test_staged_sensitive_path_blocks(tmp_path):
    _repo(tmp_path)
    (tmp_path / "data/soul").mkdir(parents=True)
    (tmp_path / "data/soul/journal.json").write_text("{}")
    subprocess.run(["git", "add", "-f", "data/soul/journal.json"], cwd=tmp_path, check=True)
    r, called, commits = _run(tmp_path, "true")
    assert r.returncode == 11 and "敏感路径" in r.stdout and not called and commits == 0


def test_staged_non_ascii_docs_dir_blocks(tmp_path):
    """外部作者帖子目录：docs/ 下首字符非 ASCII（目录名含作者名，不写进公开文件；此处用占位名）。"""
    _repo(tmp_path)
    (tmp_path / "docs/作者甲").mkdir(parents=True)
    (tmp_path / "docs/作者甲/a.png").write_text("x")
    subprocess.run(["git", "add", "-f", "docs/作者甲/a.png"], cwd=tmp_path, check=True)
    r, called, commits = _run(tmp_path, "true")
    assert r.returncode == 11 and "敏感路径" in r.stdout and not called and commits == 0
