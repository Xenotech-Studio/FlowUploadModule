#!/usr/bin/env bash
# 已注册的个人钩子扩展点。由各 hook source；调用方决定是否重定向 stdin。

FLOPS_HOOKS_EXTFILE_KEY='flops.hooks.extfile'

flops_personal_hook_problem() {
  local kind="$1" message="$2"
  if [ "$kind" = 'gate' ]; then
    echo "✗ 个人钩子扩展配置无效（${FLOPS_HOOKS_EXTFILE_KEY}）：${message}" >&2
    return 1
  fi
  echo "! 个人钩子扩展配置无效（${FLOPS_HOOKS_EXTFILE_KEY}）：${message}；已跳过个人扩展。" >&2
  return 0
}

# $1=hook event; $2=gate|post; 后续参数原样传给个人扩展。
# stdin 由调用 hook 保留或重定向：只有 pre-push / post-rewrite 需要可重放快照。
flops_run_personal_hook() {
  local event="$1" kind="$2" values ext rc
  shift 2

  # 同一事件从个人分发器回调仓库 hook 时，不再次进入个人层。
  [ "${FLOPS_PERSONAL_HOOK_ACTIVE:-}" = "$event" ] && return 0

  values="$(git config --local --get-all "$FLOPS_HOOKS_EXTFILE_KEY" 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 1 ]; then
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    flops_personal_hook_problem "$kind" '无法读取本地配置（当前值：<unreadable>）'
    return $?
  fi
  case "$values" in
    *$'\n'*)
      flops_personal_hook_problem "$kind" "配置含多个值（当前值：$values）"
      return $?
      ;;
  esac
  ext="$values"
  case "$ext" in
    /*) ;;
    *)
      flops_personal_hook_problem "$kind" "必须是绝对路径（当前值：$ext）"
      return $?
      ;;
  esac
  if [ ! -f "$ext" ] || [ ! -x "$ext" ]; then
    flops_personal_hook_problem "$kind" "不是可执行常规文件（当前值：$ext）"
    return $?
  fi

  # 在子 shell exec，既让递归护栏传给个人层及其子进程，也在返回后继续仓库检查。
  (
    FLOPS_PERSONAL_HOOK_ACTIVE="$event"
    export FLOPS_PERSONAL_HOOK_ACTIVE
    exec "$ext" "$event" "$@"
  )
  rc=$?
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if [ "$kind" = 'gate' ]; then
    return "$rc"
  fi
  echo "! 个人钩子扩展 ${event} 退出码 ${rc}；已继续仓库 post hook。" >&2
  return 0
}
