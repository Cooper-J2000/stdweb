#!/bin/bash
# STDWeb 本地服务停止脚本
# 停止: Celery worker + Django runserver (Redis 保持运行)
cd "$(dirname "$0")"

if pgrep -f "[m]anage.py runserver" > /dev/null 2>&1; then
    pkill -f "[m]anage.py runserver" && echo "[OK] Django 已停止"
else
    echo "[OK] Django 未在运行"
fi

if pgrep -f "[p]ython.*-m celery -A stdweb worker" > /dev/null 2>&1; then
    pkill -f "[p]ython.*-m celery -A stdweb worker" && echo "[OK] Celery worker 已停止"
else
    echo "[OK] Celery worker 未在运行"
fi

echo "完成 (Redis 保持运行, 可用: sudo systemctl stop redis-server)"
