# 无人值守脚本的「只发布本次产物」提交函数（Codex 008 G10 / 009 N01）。用 source 引入，函数定义在调用之前。
#
# 原错（两轮）：
#   ① event_watch.sh 按目录 git status 判断后普通 `git commit`，会把索引里【别人已暂存】的任何文件一起提交；
#   ② 第一版修复用「目录相对 HEAD 的全部差异」当清单 —— 目录里【运行前】就有的他人改动也被当成本次产物提交
#      （Codex 009 在临时仓库复现：预先改 old.txt、本次新建 new.txt，两者都进了提交）。
#
# 定义：路径属于「本任务」当且仅当 ①运行开始后才变脏，或 ②运行前就脏、但内容哈希与本任务的待发布记录一致
#       （= 本任务以前写的、还没发布成功，例如当天较早一次写的 FAILURE_ 凭证、没走到发布步骤就退出时追加的日志）。
#   其余运行前就脏的路径 = 他人改动：内容没变 → 留在工作区不提交；运行中又被改 → 冲突，整次不发布。
#
#   publish_begin <目录>...     脚本开始时调用：记下运行前的脏路径与哈希（$PUBLISH_PRE）。
#   publish_record <目录>...    脚本退出时调用（trap EXIT）：把本任务写过、仍未提交的路径记进 $PUBLISH_PENDING，
#                               下次运行还认得是自己的 —— 没有这一步，没走到发布就退出的产物会被当成他人改动。
#   publish_dirs <提交信息> <目录>...
#     返回 0 成功/无事；2 git 失败；3 索引里有他人已暂存文件；4 已提交但推送失败；5 与他人改动冲突；
#     6 未调用 publish_begin（分不清本次产物，拒绝发布）。3/5/6 时产物原样保留。
#   不 stash、不 reset、不改动任何他人内容。

_publish_dirty() {   # 目录下相对索引有改动或未跟踪的路径（每行一个）
  git ls-files --modified --others --exclude-standard -- "$@" 2>/dev/null | sort -u
}

_publish_hash() {    # 路径的内容哈希；已删除记 DELETED
  if [[ -e "$1" ]]; then git hash-object -- "$1" 2>/dev/null; else echo DELETED; fi
}

_publish_classify() {   # 每行输出 OURS\t路径 / CONFLICT\t路径 / FOREIGN\t路径
  local p pre_h pend_h PEND="${PUBLISH_PENDING:-}"
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    pre_h=$(awk -F'\t' -v k="$p" '$1 == k {print $2; exit}' "$PUBLISH_PRE")
    pend_h=""
    [[ -n "$PEND" && -f "$PEND" ]] && pend_h=$(awk -F'\t' -v k="$p" '$1 == k {h=$2} END {print h}' "$PEND")
    if [[ -z "$pre_h" ]]; then
      printf 'OURS\t%s\n' "$p"
    elif [[ -n "$pend_h" && "$pend_h" == "$pre_h" ]]; then
      printf 'OURS\t%s\n' "$p"
    elif [[ "$(_publish_hash "$p")" != "$pre_h" ]]; then
      printf 'CONFLICT\t%s\n' "$p"
    else
      printf 'FOREIGN\t%s\n' "$p"
    fi
  done < <(_publish_dirty "$@")
}

publish_begin() {
  PUBLISH_PRE=$(mktemp "${TMPDIR:-/tmp}/publish_pre.XXXXXX") || return 2
  export PUBLISH_PRE
  local p
  _publish_dirty "$@" | while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    printf '%s\t%s\n' "$p" "$(_publish_hash "$p")"
  done > "$PUBLISH_PRE"
  return 0
}

publish_record() {
  [[ -z "${PUBLISH_PENDING:-}" || -z "${PUBLISH_PRE:-}" || ! -f "$PUBLISH_PRE" ]] && return 0
  local kind p
  _publish_classify "$@" | while IFS=$'\t' read -r kind p; do
    [[ "$kind" == "OURS" ]] && printf '%s\t%s\n' "$p" "$(_publish_hash "$p")"
  done >> "$PUBLISH_PENDING"
  return 0
}

publish_dirs() {     # publish_dirs <提交信息> <目录>...
  local MSG="$1"; shift
  local -a DIRS OURS; local d kind p RC STAGED CONFLICT=""
  for d in "$@"; do [[ -e "${d%/}" ]] && DIRS+=("${d%/}"); done   # 不存在的目录跳过（git add 会报错）
  (( ${#DIRS[@]} == 0 )) && return 0
  if [[ -z "${PUBLISH_PRE:-}" || ! -f "$PUBLISH_PRE" ]]; then
    echo "PUBLISH_REFUSED 未调用 publish_begin：没有运行前状态，分不清哪些是本次产物，不发布"
    return 6
  fi
  while IFS=$'\t' read -r kind p; do
    case "$kind" in
      OURS) OURS+=("$p") ;;
      CONFLICT) CONFLICT="${CONFLICT}${p} " ;;
    esac
  done < <(_publish_classify "${DIRS[@]}")
  publish_record "${DIRS[@]}"                       # 先记下（发布失败时下次还认得）
  STAGED=$(git diff --cached --name-only 2>/dev/null)
  if [[ -n "$STAGED" ]]; then
    echo "PUBLISH_BLOCKED 索引里有他人已暂存的文件，停止发布（产物保留）：$(printf '%s' "$STAGED" | head -3 | tr '\n' ' ')"
    return 3
  fi
  if [[ -n "$CONFLICT" ]]; then
    echo "PUBLISH_CONFLICT 运行前已有他人改动、本次又写了同一路径，停止发布（产物保留）：$(printf '%s' "$CONFLICT" | cut -c1-200)"
    return 5
  fi
  (( ${#OURS[@]} == 0 )) && return 0
  [[ -n "${PUBLISH_MANIFEST:-}" ]] && printf '%s\n' "${OURS[@]}" > "$PUBLISH_MANIFEST"
  git add -- "${OURS[@]}" || return 2
  git commit -q -m "$MSG" --only -- "${OURS[@]}"; RC=$?
  (( RC != 0 )) && return 2
  if [[ -n "${PUBLISH_PENDING:-}" && -f "$PUBLISH_PENDING" ]]; then   # 已发布的从待发布记录里删掉
    local tmp; tmp=$(mktemp "${TMPDIR:-/tmp}/publish_pend.XXXXXX")
    awk -F'\t' 'NR==FNR {done[$0]=1; next} !($1 in done)' <(printf '%s\n' "${OURS[@]}") "$PUBLISH_PENDING" > "$tmp" \
      && mv "$tmp" "$PUBLISH_PENDING"
  fi
  if [[ -n "${PUBLISH_NO_PUSH:-}" ]]; then return 0; fi
  git push -q origin main >/dev/null 2>&1 || return 4
  return 0
}

publish_dir() { publish_dirs "$1" "$2"; }
