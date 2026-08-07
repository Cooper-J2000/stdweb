#!/bin/bash
# STDWeb 本地服务启动脚本
# 启动: Redis (systemd) + Celery worker + Django runserver
# 用法: ./start_stdweb.sh
set -e
cd "$(dirname "$0")"
PY=/home/ajst/miniconda3/envs/stdweb/bin/python

# 1. Redis (系统服务)
if systemctl is-active --quiet redis-server 2>/dev/null; then
    echo "[OK] Redis 已在运行"
else
    sudo systemctl start redis-server && echo "[OK] Redis 已启动" || { echo "[FAIL] Redis 启动失败"; exit 1; }
fi

# 2. Celery worker
if pgrep -f "[p]ython.*-m celery -A stdweb worker" > /dev/null 2>&1; then
    echo "[OK] Celery worker 已在运行"
else
    export OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 OMP_NUM_THREADS=1
    nohup "$PY" -m celery -A stdweb worker --loglevel=info >> celery.log 2>&1 &
    echo "[OK] Celery worker 已启动 (PID $!)"
fi

# 3. Django web server (只监听本机回环)
if pgrep -f "[m]anage.py runserver" > /dev/null 2>&1; then
    echo "[OK] Django 已在运行"
else
    nohup "$PY" manage.py runserver 127.0.0.1:8000 --insecure >> server.log 2>&1 &
    echo "[OK] Django 已启动 (PID $!)"
fi

# 4. 等待就绪并验证
for i in $(seq 1 10); do
    if curl -s -o /dev/null http://127.0.0.1:8000/ 2>/dev/null; then
        echo ""
        echo "STDWeb 已就绪: http://127.0.0.1:8000  (仅本机可访问)"
        exit 0
    fi
    sleep 1
done
echo "[WARN] 服务未就绪，请查看 celery.log / server.log"
exit 1
