# 无人值守脚本的「只发布本次产物」提交函数（Codex 008 G10）。用 source 引入，函数定义在调用之前。
#
# 原错：event_watch.sh 按整个目录的 git status 判断是否提交，然后普通 `git commit`——
# 它会把当时索引里【别人已暂存】的任何文件一起提交（与 Claude/Codex 同目录交叉工作时，提交归属混乱）。
#
# publish_dir <提交信息> <目录>：
#   - 清单 = 该目录下相对 HEAD 的新增/修改（未跟踪也算）；为空 → 返回 0，什么都不做。
#   - 索引里若有该目录以外的已暂存文件 → 不提交、不 stash、不 reset，产物原样保留，返回 3（下次再发）。
#   - 否则只 add 并以 --only 语义提交该目录；推送失败返回 4。清单写入 $PUBLISH_MANIFEST（若设置）。
publish_dirs() {   # publish_dirs <提交信息> <目录>...：多个目录一起（同一套规则）
  local MSG="$1"; shift
  local -a DIRS; local d
  for d in "$@"; do [[ -e "${d%/}" ]] && DIRS+=("${d%/}"); done   # 不存在的目录跳过（git add 会报错）
  (( ${#DIRS[@]} == 0 )) && return 0
  local CHANGED FOREIGN RC PAT
  CHANGED=$(git status --porcelain --untracked-files=all -- "${DIRS[@]}" 2>/dev/null)
  [[ -z "$CHANGED" ]] && return 0
  PAT=""
  for d in "${DIRS[@]}"; do PAT="${PAT:+$PAT|}^${d}/"; done
  FOREIGN=$(git diff --cached --name-only 2>/dev/null | grep -Ev "$PAT")
  if [[ -n "$FOREIGN" ]]; then
    echo "PUBLISH_BLOCKED 索引里有他人已暂存的文件，停止发布（产物保留）：$(printf '%s' "$FOREIGN" | head -3 | tr '\n' ' ')"
    return 3
  fi
  [[ -n "${PUBLISH_MANIFEST:-}" ]] && printf '%s\n' "$CHANGED" > "$PUBLISH_MANIFEST"
  git add -- "${DIRS[@]}" || return 2
  if git diff --cached --quiet -- "${DIRS[@]}"; then return 0; fi     # 只有被忽略文件变化
  git commit -q -m "$MSG" --only -- "${DIRS[@]}"; RC=$?
  (( RC != 0 )) && return 2
  if [[ -n "${PUBLISH_NO_PUSH:-}" ]]; then return 0; fi
  git push -q origin main >/dev/null 2>&1 || return 4
  return 0
}

publish_dir() { publish_dirs "$1" "$2"; }
