#!/bin/sh
# 在新终端标签（tmux 窗口）起一个会话。参数文件字段：工作目录、标题、argv…（NUL 分隔）
file=$1
fail() { printf '{"ok": false, "error": "%s"}\n' "$1"; exit 1; }
command -v tmux >/dev/null 2>&1 || fail "没有 tmux"
# 没有 tmux 会话就建一个后台的 shiftboss 会话，用 `tmux attach -t shiftboss` 查看。
tmux has-session 2>/dev/null || tmux new-session -d -s shiftboss || fail "建不了 tmux 会话"
xargs -0 sh -c 'cwd=$1; title=$2; shift 2; exec tmux new-window -d -n "$title" -c "$cwd" "$@"' sh < "$file" \
    || fail "tmux new-window 失败"
printf '{"ok": true, "terminal": "tmux"}\n'
