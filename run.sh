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

is_running() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pid_file"
    fi
    return 1
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
        if [ "$count" -ge 10 ]; then
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
    stop_one "backend"  "$BACKEND_PID"
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
