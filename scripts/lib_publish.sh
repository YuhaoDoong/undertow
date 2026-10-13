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
#     6 未调用 publish_begin（分不清本次产物，拒绝发布）；7 全局发布锁 60 秒拿不到；9 本次产物登记未能持久化。3/5/6/7/9 时产物原样保留。
#   不 stash、不 reset、不改动任何他人内容。

_publish_dirty() {   # 目录下相对索引有改动或未跟踪的路径（每行一个）
  git ls-files --modified --others --exclude-standard -- "$@" 2>/dev/null | sort -u
}

_publish_hash() {    # 路径的内容哈希；已删除记 DELETED
  if [[ -e "$1" ]]; then git hash-object -- "$1" 2>/dev/null; else echo DELETED; fi
}

_publish_classify() {   # 每行输出 OURS\t路径 / CONFLICT\t路径 / FOREIGN\t路径
  # 运行前内容只要等于待发布记录里【任何一次】登记过的哈希，就是自动任务自己的产物。
  # 原错（2026-10-08 起 daily 连续 4 个交易日 PUBLISH_CONFLICT、影子账 10/8–10/12 未入库）：只比「最后一次」登记的哈希。
  # 并发的两个自动进程 A、B：A 启动时文件为已登记的 h0；B 写 h1 并登记；A 再写 h2 → A 看到 pre=h0 ≠ 最后登记 h1 → 判冲突、
  # 且冲突路径不登记 → h2 永远没人认领，此后每次运行都冲突，卡死。
  local p pre_h pend_hit PEND="${PUBLISH_PENDING:-}"
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    pre_h=$(awk -F'\t' -v k="$p" '$1 == k {print $2; exit}' "$PUBLISH_PRE")
    pend_hit=""
    [[ -n "$pre_h" && -n "$PEND" ]] && \
      pend_hit=$(awk -F'\t' -v k="$p" -v h="$pre_h" '$1 == k && $2 == h {print "y"; exit}' $(_publish_pend_sources) </dev/null 2>/dev/null)
    if [[ -z "$pre_h" ]]; then
      printf 'OURS\t%s\n' "$p"
    elif [[ -n "$pend_hit" ]]; then
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

_publish_pend_sources() {   # 待发布记录的全部来源：主文件 + 尚未并入的 spool 文件（每行一个路径）
  [[ -f "$PUBLISH_PENDING" ]] && print -r -- "$PUBLISH_PENDING"
  local f; for f in "${PUBLISH_PENDING}.spool"/*(N); do print -r -- "$f"; done
}

_publish_pend_locked() {   # 在待发布记录的文件锁下执行 "$@"；拿不到锁 → 不执行、留痕、返回 8（Codex 035：不得无锁改文件）
  # 原错 ①：publish_dirs 删已发布行是「awk 读全文件 → mv 覆盖」，与其它进程的追加无锁并发 → 中间追加的登记被抹掉。
  # 原错 ②（035 探针）：zmodload / flock 失败时退化成无锁直接执行，且返回 0 —— 修并发的代码在失败路径上又回到并发。
  local fd rc
  if [[ -z "${PUBLISH_PENDING:-}" ]]; then return 0; fi
  if ! zmodload zsh/system 2>/dev/null; then
    print -u2 -r -- "PUBLISH_PENDING_LOCK_UNAVAILABLE zsh/system 加载失败，未改动待发布记录（登记留在 spool，下次拿到锁时并入）"
    return 8
  fi
  # zsystem flock 不会创建锁文件（文件不存在直接失败）—— 10/13 首版因此从未真正上锁，一直走无锁退化分支。
  if ! { : >> "${PUBLISH_PENDING}.lock"; } 2>/dev/null \
     || ! zsystem flock -t ${PUBLISH_PENDING_LOCK_WAIT:-30} -f fd "${PUBLISH_PENDING}.lock" 2>/dev/null; then
    print -u2 -r -- "PUBLISH_PENDING_LOCK_TIMEOUT 待发布记录锁 ${PUBLISH_PENDING_LOCK_WAIT:-30}s 未拿到，未改动（登记留在 spool，下次并入）"
    return 8
  fi
  "$@"; rc=$?
  zsystem flock -u $fd
  return $rc
}

_publish_spool_merge() {   # 把 spool 里的登记按文件名顺序并入主文件（须在锁下调用）；并入后才删 spool 文件
  local f
  for f in "${PUBLISH_PENDING}.spool"/*(N); do
    cat -- "$f" >> "$PUBLISH_PENDING" || return 2
    rm -f -- "$f"
  done
  return 0
}

publish_record() {
  # 先把本次产物的哈希写进一个只属于本进程的 spool 文件（原子改名，无需锁、不会丢），再在锁下并入主文件。
  # 拿不到锁时登记仍在 spool 里，分类时照样被读到；返回非零让调用方知道（不再总是 return 0）。
  [[ -z "${PUBLISH_PENDING:-}" || -z "${PUBLISH_PRE:-}" || ! -f "$PUBLISH_PRE" ]] && return 0
  local kind p dir="${PUBLISH_PENDING}.spool" tmp
  if ! mkdir -p -- "$dir" || ! tmp=$(mktemp "$dir/.rec.XXXXXX"); then
    print -u2 -r -- "PUBLISH_RECORD_NOT_PERSISTED spool 目录不可写：本次产物未登记"
    return 9
  fi
  _publish_classify "$@" | while IFS=$'\t' read -r kind p; do
    [[ "$kind" == "OURS" ]] && printf '%s\t%s\n' "$p" "$(_publish_hash "$p")"
  done > "$tmp"
  if [[ -s "$tmp" ]]; then
    if ! mv -- "$tmp" "$dir/$(date +%s).$$.${RANDOM}.rec"; then
      print -u2 -r -- "PUBLISH_RECORD_NOT_PERSISTED 登记未能写入 spool：本次产物下次会被当成他人改动"
      return 9                                   # 9 = 登记根本没持久化（不可恢复，必须告警）
    fi
  else
    rm -f -- "$tmp"
  fi
  _publish_pend_locked _publish_spool_merge      # 8 = 已落 spool、暂未并入（可恢复，下次拿到锁并入）
}

_publish_pend_consume() {   # 只消费与【实际提交】完全相等的 (路径, 哈希) 登记（须在锁下调用）。参数：路径\t已提交哈希 的清单文件
  # 原错一（035）：按路径删全部登记，A 提交 h1 后 B 登记的 h2 被删。
  # 原错二（036）：改成「删最后一次出现已提交哈希的行及之前的同路径行」，仍假定登记顺序 = 版本顺序；
  # A 读到 h1、B 写 h2 并先登记、A 的 h1 迟到并入 → 顺序是 h2、h1，清理把未提交的 h2 也删了。
  # 现：只删精确相等的行，其它哈希一律保留；旧行多留可接受（判定按「命中任一登记哈希」，多留不影响正确性）。
  _publish_spool_merge || return 2
  local tmp; tmp=$(mktemp "${PUBLISH_PENDING}.XXXXXX") || return 2
  awk -F'\t' 'NR==FNR {c[$1 "\t" $2]=1; next} !(($1 "\t" $2) in c)' "$1" "$PUBLISH_PENDING" > "$tmp" \
    && mv -- "$tmp" "$PUBLISH_PENDING"
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
  local REC_RC; publish_record "${DIRS[@]}"; REC_RC=$?   # 先记下（发布失败时下次还认得）
  if (( REC_RC == 9 )); then
    echo "PUBLISH_RECORD_FAILED 本次产物的登记未能持久化，停止发布（产物保留；需人工核对 spool 目录权限/磁盘）"
    return 9
  fi
  (( REC_RC == 8 )) && echo "PUBLISH_RECORD_DEFERRED 登记已落 spool、待并入（锁暂不可用），不影响本次发布"
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
  # 全局发布锁（Codex 024-6）：daily / session / 收盘备份可能同时发布。shlock 在锁文件里记 PID，
  # 持锁进程已死 → 视为失效锁自动接管（不按耗时强删活锁）。等不到 → rc=7，产物保留、下次再发。
  local LOCKF="${PUBLISH_LOCK:-$(git rev-parse --git-dir 2>/dev/null)/undertow_publish.lock}" TRY=0
  until shlock -f "$LOCKF" -p $$ 2>/dev/null; do
    (( ++TRY > ${PUBLISH_LOCK_WAIT:-60} )) && { echo "PUBLISH_BUSY 发布锁被 $(cat "$LOCKF" 2>/dev/null) 占用超过 ${PUBLISH_LOCK_WAIT:-60} 秒，本次不发布（产物保留）"; return 7; }
    sleep 1
  done
  [[ -n "${PUBLISH_MANIFEST:-}" ]] && printf '%s\n' "${OURS[@]}" > "$PUBLISH_MANIFEST"
  git add -- "${OURS[@]}" || { rm -f "$LOCKF"; return 2; }
  git commit -q -m "$MSG" --only -- "${OURS[@]}"; RC=$?
  if (( RC != 0 )); then rm -f "$LOCKF"; return 2; fi
  if [[ -n "${PUBLISH_PENDING:-}" ]]; then   # 按 HEAD 里实际提交的内容哈希消费登记（提交期间别人登记的新版本保留）
    local CM; CM=$(mktemp "${TMPDIR:-/tmp}/publish_committed.XXXXXX")
    for p in "${OURS[@]}"; do
      printf '%s\t%s\n' "$p" "$(git rev-parse -q --verify "HEAD:$p" 2>/dev/null || echo DELETED)"
    done > "$CM"
    _publish_pend_locked _publish_pend_consume "$CM" \
      || echo "PUBLISH_PENDING_CONSUME_DEFERRED 已提交，但待发布记录未能清理（锁不可用）；只会多留旧行，不影响判定"
    rm -f "$CM"
  fi
  if [[ -n "${PUBLISH_NO_PUSH:-}" ]]; then rm -f "$LOCKF"; return 0; fi
  git push -q origin main >/dev/null 2>&1 || { rm -f "$LOCKF"; return 4; }
  rm -f "$LOCKF"
  return 0
}

publish_dir() { publish_dirs "$1" "$2"; }
