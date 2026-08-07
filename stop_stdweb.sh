#!/bin/bash
# STDWeb 本地服务停止脚本
# 停止: Celery worker + Django runserver (Redis 保持运行)
# 停止后等待进程完全退出, 避免 start 脚本误判"已在运行"跳过启动
cd "$(dirname "$0")"

wait_for_exit() {
    local pattern="$1" timeout="${2:-15}" waited=0
    while pgrep -f "$pattern" > /dev/null 2>&1; do
        if [ "$waited" -ge "$timeout" ]; then
            echo "[WARN] 进程未在 ${timeout}s 内退出: $pattern, 强制终止"
            pkill -9 -f "$pattern" 2>/dev/null
            return 1
        fi
        sleep 1
        waited=$((waited+1))
    done
    return 0
}

if pgrep -f "[m]anage.py runserver" > /dev/null 2>&1; then
    pkill -f "[m]anage.py runserver" && echo "[OK] Django 已停止"
    wait_for_exit "[m]anage.py runserver" 10
else
    echo "[OK] Django 未在运行"
fi

if pgrep -f "[p]ython.*-m celery -A stdweb worker" > /dev/null 2>&1; then
    pkill -f "[p]ython.*-m celery -A stdweb worker" && echo "[OK] Celery worker 已停止"
    wait_for_exit "[p]ython.*-m celery -A stdweb worker" 20
else
    echo "[OK] Celery worker 未在运行"
fi

echo "完成 (Redis 保持运行, 可用: sudo systemctl stop redis-server)"
