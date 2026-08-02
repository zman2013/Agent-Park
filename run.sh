#!/bin/bash
# Agent Park 启动/停止脚本
# 用法: ./run.sh start | stop | status | restart

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
PID_DIR="$SCRIPT_DIR"
BACKEND_PID="$PID_DIR/backend.pid"
FRONTEND_PID="$PID_DIR/frontend.pid"
LOG_DIR="$SCRIPT_DIR/logs"

# Python 解释器（优先使用项目 .venv）
PYTHON="python3"
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
fi

# 从 config.json 读取端口
read_config() {
    "$PYTHON" -c "
import json, sys
with open('$CONFIG_FILE') as f:
    c = json.load(f)
print(c.get('server',{}).get('host','0.0.0.0'))
print(c.get('server',{}).get('port',8001))
print(c.get('frontend',{}).get('port',3000))
"
}

# 判断 pid 是否为僵尸进程（已退出但未被父进程 reap）
# 僵尸进程 kill -0 仍返回成功，但实际已死，必须视为「未运行」
is_zombie() {
    local pid="$1"
    local state
    # /proc/<pid>/stat 第 3 个字段是进程状态，Z 表示僵尸
    state=$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null) || return 1
    [ "$state" = "Z" ]
}

is_running() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null && ! is_zombie "$pid"; then
            return 0
        fi
        # 进程不存在或为僵尸：清理陈旧 pidfile
        rm -f "$pid_file"
    fi
    return 1
}

# 清理占用指定端口的孤儿进程（pidfile 丢失但进程仍卡在端口上时会发生）
# 仅在确认对应服务「未运行」后调用，避免误杀正常进程。
# 不依赖 ss/lsof/fuser（本机未安装），纯 /proc 实现：
#   1. 从 /proc/net/tcp{,6} 找到 LISTEN 该端口的 socket inode
#   2. 扫描 /proc/*/fd 反查持有该 inode 的进程
free_port() {
    local port="$1"
    local name="$2"
    local port_hex inodes pids=""

    # 端口转大写十六进制（/proc/net/tcp 中本地端口为 HEX）
    port_hex=$(printf '%04X' "$port")

    # 收集 st=0A(LISTEN) 且本地端口匹配的 socket inode
    inodes=$(awk -v ph=":$port_hex" '
        NR>1 && $2 ~ ph"$" && $4=="0A" { print $10 }
    ' /proc/net/tcp /proc/net/tcp6 2>/dev/null | sort -u)

    [ -z "$inodes" ] && return 0

    local pid inode link
    for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
        for link in /proc/"$pid"/fd/*; do
            [ -e "$link" ] || continue
            # socket fd 的符号链接形如 socket:[<inode>]
            local target
            target=$(readlink "$link" 2>/dev/null) || continue
            case "$target" in
                socket:\[*\])
                    inode="${target#socket:[}"
                    inode="${inode%]}"
                    if echo "$inodes" | grep -qx "$inode"; then
                        case " $pids " in *" $pid "*) ;; *) pids="$pids $pid" ;; esac
                    fi
                    ;;
            esac
        done
    done

    pids="${pids# }"
    if [ -n "$pids" ]; then
        # 只清理属于本项目的进程，避免误杀同端口的无关服务。
        # 判据：cmdline 含项目目录（venv python / 含路径参数），
        # 或 /proc/$pid/cwd 解析为项目目录（全局 python3/npx 从项目目录启动的情况）。
        local safe_pids=""
        for pid in $pids; do
            local matched=0
            local cmdline
            cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || continue
            case "$cmdline" in
                *"$SCRIPT_DIR"*) matched=1 ;;
            esac
            if [ "$matched" -eq 0 ]; then
                local cwd
                cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || true
                case "$cwd" in
                    "$SCRIPT_DIR" | "$SCRIPT_DIR/"*) matched=1 ;;
                esac
            fi
            if [ "$matched" -eq 1 ]; then
                case " $safe_pids " in *" $pid "*) ;; *) safe_pids="$safe_pids $pid" ;; esac
            fi
        done
        safe_pids="${safe_pids# }"
        if [ -n "$safe_pids" ]; then
            echo "检测到端口 ${port} 被孤儿 ${name} 进程占用 (PID: ${safe_pids})，清理中..."
            # shellcheck disable=SC2086
            kill -9 $safe_pids 2>/dev/null || true
            sleep 1
        fi
    fi
}

do_start() {
    if is_running "$BACKEND_PID" && is_running "$FRONTEND_PID"; then
        echo "服务已在运行 (backend PID: $(cat "$BACKEND_PID"), frontend PID: $(cat "$FRONTEND_PID"))"
        return 0
    fi

    mkdir -p "$LOG_DIR"

    # 读取配置
    local cfg
    cfg=$(read_config)
    local host port fe_port
    host=$(echo "$cfg" | sed -n '1p')
    port=$(echo "$cfg" | sed -n '2p')
    fe_port=$(echo "$cfg" | sed -n '3p')

    # 启动 backend
    if ! is_running "$BACKEND_PID"; then
        free_port "$port" "backend"
        echo "正在启动 backend (${host}:${port})..."
        cd "$SCRIPT_DIR"
        nohup "$PYTHON" -m uvicorn server.main:app \
            --host "$host" --port "$port" \
            >> "$LOG_DIR/backend.log" 2>&1 &
        echo $! > "$BACKEND_PID"
        sleep 1
        if is_running "$BACKEND_PID"; then
            echo "backend 已启动 (PID: $(cat "$BACKEND_PID"))"
        else
            echo "backend 启动失败，请检查日志: $LOG_DIR/backend.log"
            return 1
        fi
    fi

    # 启动 frontend
    if ! is_running "$FRONTEND_PID"; then
        free_port "$fe_port" "frontend"
        echo "正在启动 frontend (:${fe_port})..."
        cd "$SCRIPT_DIR/frontend"
        nohup npx vite --host 0.0.0.0 --port "$fe_port" \
            >> "$LOG_DIR/frontend.log" 2>&1 &
        echo $! > "$FRONTEND_PID"
        sleep 2
        if is_running "$FRONTEND_PID"; then
            echo "frontend 已启动 (PID: $(cat "$FRONTEND_PID"))"
        else
            echo "frontend 启动失败，请检查日志: $LOG_DIR/frontend.log"
            return 1
        fi
    fi

    echo "所有服务已启动"
    echo "  Backend:  http://${host}:${port}"
    echo "  Frontend: http://0.0.0.0:${fe_port}"
    echo "  日志目录: $LOG_DIR/"
}

stop_one() {
    local name="$1"
    local pid_file="$2"
    local grace="${3:-10}"

    if ! is_running "$pid_file"; then
        echo "${name} 未在运行"
        return 0
    fi

    local pid
    pid=$(cat "$pid_file")
    echo "正在停止 ${name} (PID: $pid)..."
    kill "$pid"

    local count=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        count=$((count + 1))
        if [ "$count" -ge "$grace" ]; then
            echo "${name} 未响应，强制终止..."
            kill -9 "$pid" 2>/dev/null || true
            break
        fi
    done
    rm -f "$pid_file"
    echo "${name} 已停止"
}

do_stop() {
    stop_one "frontend" "$FRONTEND_PID"
    # backend's shutdown() waits up to 10s for in-flight agent subprocesses to
    # finalize, then drains pending Feishu notifications. Same-task
    # notifications are serialized, so that drain scales with queue depth up to
    # NOTIFY_DRAIN_MAX_SECONDS (100s in agent_runner.py) — give it enough grace
    # to clear both before force-killing.
    stop_one "backend"  "$BACKEND_PID" 115
    echo "所有服务已停止"
}

do_status() {
    if is_running "$BACKEND_PID"; then
        echo "backend  运行中 (PID: $(cat "$BACKEND_PID"))"
    else
        echo "backend  未在运行"
    fi
    if is_running "$FRONTEND_PID"; then
        echo "frontend 运行中 (PID: $(cat "$FRONTEND_PID"))"
    else
        echo "frontend 未在运行"
    fi
}

case "${1:-}" in
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        do_start
        ;;
    status)
        do_status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
