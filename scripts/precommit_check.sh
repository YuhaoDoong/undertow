#!/bin/zsh
# 提交前检查入口（Codex 027；2026-09-29 两次出现 `pytest | tail && git commit` 漏门控：管道退出码是 tail 的，恒为 0）。
#
#   scripts/precommit_check.sh [日志文件] && git commit ...
#
# ① 跑 pytest，原样写日志、单独捕获它自己的退出码；非零 → 立即以 10 退出（不做后续、不提交）。
# ② 暂存区含敏感路径（AGENTS 第一节：data/soul、data/account、data/paper、docs/screenshot、docs/author_*.md、article）→ 以 11 退出。
#    另打印 AGENTS 的宽匹配结果供人看（会命中 scripts/author_levels_score.py 这类公开代码，不据此阻断）。
# 只检查、不提交、不回滚。PYTEST 环境变量可替换测试命令（隔离测试用）。
LOG="${1:-${TMPDIR:-/tmp}/undertow_precommit_pytest.log}"
if [[ -n "${PYTEST:-}" ]]; then
  zsh -c "$PYTEST" > "$LOG" 2>&1
else
  python3 -m pytest -q > "$LOG" 2>&1
fi
RC=$?
tail -1 "$LOG"
if (( RC != 0 )); then
  echo "PRECOMMIT_FAIL pytest rc=$RC（日志 $LOG）——不得提交"
  exit 10
fi
STAGED_SENS=$(git diff --cached --name-only | grep -E '^(data/soul/|data/account/|data/paper/|docs/screenshot/)|^docs/author_[^/]*\.md$|article')
if [[ -n "$STAGED_SENS" ]]; then
  echo "PRECOMMIT_FAIL 暂存区含敏感路径——不得提交：$(printf '%s' "$STAGED_SENS" | tr '\n' ' ')"
  exit 11
fi
WIDE=$(git status --short | grep -iE "article|screenshot|author|playbook|private|account/|soul/|paper/")
[[ -n "$WIDE" ]] && echo "（AGENTS 宽匹配，仅提示）$(printf '%s' "$WIDE" | head -5 | tr '\n' ' ')"
echo "PRECOMMIT_OK pytest rc=0"
exit 0
