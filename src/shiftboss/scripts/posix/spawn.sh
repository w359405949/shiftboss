#!/bin/sh
# 后台起常驻进程，脱离终端与父进程。参数文件字段：工作目录、argv…（NUL 分隔）
file=$1
fail() { printf '{"ok": false, "error": "%s"}\n' "$1"; exit 1; }
pid=$(xargs -0 sh -c '
    cd "$1" || exit 1
    shift
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid "$@" >/dev/null 2>&1 </dev/null &
    else
        nohup "$@" >/dev/null 2>&1 </dev/null &
    fi
    echo $!' sh < "$file") || fail "起不来"
[ -n "$pid" ] || fail "没拿到 PID"
printf '{"ok": true, "pid": %s}\n' "$pid"
