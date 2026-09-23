#!/bin/bash
# 由 unit 的 ExecStopPost= 调用：unit 每次停止都往 signal_trace.jsonl 追加一条记录。
#
# 作用：补齐用户态拿不到的信息（SIGKILL / OOM / timeout —— 这些没法在进程内留痕），
# 并给出 systemd 自己的判定。配合 stdweb/signal_trace.py 的进程内记录，形成完整链条：
#   进程内记录 = 谁发的（SIGTERM/INT/HUP/QUIT 的 pid/uid/cmdline）
#   本记录     = systemd 视角的结果（正常/退出码/被信号杀/OOM/超时）+ 每次停止的时间线
#
# systemd 注入的环境变量：SERVICE_RESULT / EXIT_CODE / EXIT_STATUS / MAINPID / INVOCATION_ID
# 用法：record_service_stop.sh <unit-name>

TRACE="${STDWEB_SIGNAL_TRACE:-/home/ajst/Astro_Software/stdweb/signal_trace.jsonl}"
UNIT="${1:-unknown}"

exec /usr/bin/python3 - "$TRACE" "$UNIT" <<'PY'
import datetime
import json
import os
import sys
import time

trace, unit = sys.argv[1], sys.argv[2]
now = datetime.datetime.now(datetime.timezone.utc)
record = {
    'ts_epoch': time.time(),                      # 权威时间；本地时间交给读取端渲染
    'ts_utc': now.isoformat(timespec='milliseconds'),
    'ts_local': now.astimezone().isoformat(timespec='milliseconds'),
    'tag': 'systemd-stop',
    'unit': unit,
    'service_result': os.environ.get('SERVICE_RESULT'),
    'exit_code': os.environ.get('EXIT_CODE'),
    'exit_status': os.environ.get('EXIT_STATUS'),
    'invocation_id': os.environ.get('INVOCATION_ID'),
}
with open(trace, 'a', encoding='utf-8') as fh:
    fh.write(json.dumps(record, ensure_ascii=False) + '\n')
PY
