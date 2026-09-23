"""给 SIGTERM / SIGINT / SIGHUP / SIGQUIT 留痕：记录**是谁**给本进程发的终止信号。

背景：Python 的 signal 处理器只拿到信号编号，拿不到发信者；内核的 siginfo（含 si_pid /
si_uid）只有 signalfd / sigwaitinfo 这条路径可见。所以这里的做法是：

  1. 启动时用 sigprocmask 阻塞这几个信号（新建线程/子进程继承该掩码）；
  2. 用 signalfd 在后台线程里读 struct signalfd_siginfo，拿到 ssi_pid / ssi_uid / ssi_code；
  3. 落一条 JSONL：发信者 pid/uid/cmdline/exe + 祖先链（谁在背后操作），本进程信息，时间；
  4. 解除阻塞并把信号重新投递给自己，让进程按**原有**路径退出
     （unit 的 Restart=always 负责拉起）；Django 侧再挂一个超时兜底，避免极端情况下卡住不退。

拿不到发信者的情形只有两种：SIGKILL（内核不给机会，任何用户态方案都做不到）和发信者进程
秒退成僵尸（cmdline 会读空）——后者用祖先链补足。

单独查看：`python -m stdweb.signal_trace`（默认打印最后 10 条，参数给数字可改）。
落盘路径：项目根目录 signal_trace.jsonl（可用环境变量 STDWEB_SIGNAL_TRACE 覆盖）。
"""

import ctypes
import json
import logging
import os
import signal
import struct
import sys
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE_PATH = os.environ.get('STDWEB_SIGNAL_TRACE') or os.path.join(PROJECT_DIR, 'signal_trace.jsonl')

TRACED_SIGNALS = [s for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
                  if s is not None]

# 重新投递后若还没退出，兜底强杀的超时（秒）。Celery 侧不用：warm shutdown 可能要等任务跑完。
FORCE_EXIT_AFTER = {'django': 15.0, 'celery': None, 'python': 15.0}

_SI_CODE_MEANING = {
    0: 'SI_USER (kill(2) / raise(3) 直接发的)',
    -1: 'SI_QUEUE (sigqueue(3), 带 payload)',
    -6: 'SI_TKILL (tkill/tgkill, 线程内)',
    128: 'SI_KERNEL (内核发的)',
}

_libc = None
_installed = False
_mask = None
_lock = threading.Lock()


def _load_libc():
    global _libc
    if _libc is not None:
        return _libc
    try:
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        libc.sigemptyset.argtypes = [ctypes.c_void_p]
        libc.sigaddset.argtypes = [ctypes.c_void_p, ctypes.c_int]
        libc.sigprocmask.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        libc.signalfd.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.signalfd.restype = ctypes.c_int
        _libc = libc
    except (OSError, AttributeError) as exc:      # 非 Linux / 无 libc
        logger.debug('signal_trace: 不可用 (%s)', exc)
        _libc = False
    return _libc


class _SigSet(ctypes.Structure):
    # kernel sigset_t 是 1024 位
    _fields_ = [('val', ctypes.c_ulong * ((1024 + 8 * ctypes.sizeof(ctypes.c_ulong) - 1)
                                          // (8 * ctypes.sizeof(ctypes.c_ulong))))]


def _cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw:                                   # 僵尸进程：没有 mm，cmdline 是空的
        return ''
    return raw.replace(b'\0', b' ').decode('utf-8', 'replace').strip()


def _proc_snapshot(pid):
    """发信者（及其祖先）在收到信号那一刻的样子。"""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as fh:
            stat = fh.read().decode('utf-8', 'replace')
        # 格式: pid (comm) state ppid ... —— comm 里可能有空格/括号，从右往左切
        close = stat.rfind(')')
        fcomm = stat[:stat.find('(')]
        rest = stat[close + 2:].split()
        comm = stat[stat.find('(') + 1:close]
        state, ppid = rest[0], int(rest[1])
    except (OSError, ValueError, IndexError):
        return None

    info = {'pid': pid, 'comm': comm, 'state': state, 'ppid': ppid,
            'cmdline': _cmdline(pid)}
    try:
        info['exe'] = os.readlink(f'/proc/{pid}/exe')
    except OSError:
        info['exe'] = None
    try:
        with open(f'/proc/{pid}/status') as fh:
            for line in fh:
                if line.startswith('Uid:'):
                    info['uid'] = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    return info


def _ancestry(pid, depth=4):
    chain, seen, cur = [], set(), pid
    while cur and cur > 1 and len(chain) < depth and cur not in seen:
        seen.add(cur)
        snap = _proc_snapshot(cur)
        if snap is None:
            break
        chain.append(snap)
        cur = snap.get('ppid')
    return chain


def _write_record(record):
    line = json.dumps(record, ensure_ascii=False, sort_keys=False) + '\n'
    try:
        fd = os.open(TRACE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode('utf-8'))     # 单次 write + O_APPEND = 多进程安全
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning('signal_trace: 写入 %s 失败: %s', TRACE_PATH, exc)


def _unblock():
    """解除本进程的掩码（子进程 fork 后也要调一次，见 unblock_for_children）。"""
    try:
        libc, mask = _load_libc(), _mask
        if libc and mask is not None:
            libc.sigprocmask(signal.SIG_UNBLOCK, ctypes.byref(mask), None)
    except Exception:
        pass


def _tag():
    argv = ' '.join(sys.argv[:3])
    base = os.path.basename(sys.argv[0] or '')
    if base == 'manage.py' or 'manage.py' in argv:
        return 'django'
    if 'celery' in base or 'celery' in argv:
        return 'celery'
    return 'python'


def _handle(signo, info, extra, force_after):
    def _proc_name(pid):
        snap = _proc_snapshot(pid)
        return snap.get('cmdline') or snap.get('comm') if snap else '<已消失>'

    ancestry = _ancestry(info['ssi_pid'])
    sender = ancestry[0] if ancestry else {'pid': info['ssi_pid']}
    now = datetime.now(timezone.utc)
    record = {
        'ts_epoch': time.time(),                      # 权威时间；本地时间交给读取端渲染
        'ts_utc': now.isoformat(timespec='milliseconds'),
        # 注意：Django 会把进程的 TZ 设成 settings.TIME_ZONE(UTC)，所以进程内“本地时间”可能是
        # UTC —— 需要本地时间请用 ts_epoch 自己渲染（本模块的 __main__ 查看器就是这么做的）。
        'ts_proc_local': now.astimezone().isoformat(timespec='milliseconds'),
        'proc_tz': os.environ.get('TZ'),
        'tag': _tag(),
        'signal': signal.Signals(signo).name,
        'signo': signo,
        'si_code': info['si_code'],
        'si_code_meaning': _SI_CODE_MEANING.get(info['si_code'], f'其它({info["si_code"]})'),
        'sender_pid': info['ssi_pid'],
        'sender_uid': info['ssi_uid'],
        'sender_cmdline': sender.get('cmdline'),
        'sender_exe': sender.get('exe'),
        'sender_state': sender.get('state'),
        'sender_ancestry': [
            {'pid': s.get('pid'), 'uid': s.get('uid'), 'comm': s.get('comm'),
             'cmdline': s.get('cmdline')} for s in ancestry
        ],
        'self_pid': os.getpid(),
        'self_ppid': os.getppid(),
        'self_cmdline': _cmdline(os.getpid()),
        'self_cwd': os.getcwd(),
        'self_argv': sys.argv,
    }
    if extra:
        record.update(extra)

    _write_record(record)
    logger.warning(
        'signal_trace: 收到 %s —— 发信者 pid=%s uid=%s (%s)；它的父进程 pid=%s (%s)。'
        '完整记录见 %s',
        record['signal'], info['ssi_pid'], info['ssi_uid'], _proc_name(info['ssi_pid']),
        info['ssi_pid'] and (ancestry[0].get('ppid') if ancestry else '?'),
        ancestry[1].get('cmdline') if len(ancestry) > 1 else '-', TRACE_PATH)

    # 交回原有退出路径：解除阻塞 -> 重新投递
    _unblock()
    try:
        os.kill(os.getpid(), signo)
    except Exception:
        os._exit(128 + signo)

    if force_after:
        def _force():
            time.sleep(force_after)
            logger.error('signal_trace: 重新投递 %s 后 %.0fs 仍未退出，强制退出',
                         record['signal'], force_after)
            os._exit(128 + signo)
        threading.Thread(target=_force, name='signal-trace-force-exit', daemon=True).start()


def _reader(fd, extra, force_after):
    while True:
        try:
            data = os.read(fd, 128)
        except OSError:
            return
        if not data or len(data) < 20:
            continue
        signo, errno_, code, pid, uid = struct.unpack_from('<iiiII', data, 0)
        try:
            _handle(signo, {'signo': signo, 'errno': errno_, 'si_code': code,
                            'ssi_pid': pid, 'ssi_uid': uid}, extra, force_after)
        except Exception:
            logger.exception('signal_trace: 处理信号留痕时出错（继续按原路径退出）')
            try:
                os.kill(os.getpid(), signo)
            except Exception:
                os._exit(128 + signo)


def install(extra=None):
    """安装留痕（幂等；非 Linux / 任何环节失败 -> 静默 no-op，绝不影响启动）。"""
    global _installed, _mask
    with _lock:
        if _installed:
            return False
        try:
            libc = _load_libc()
            if not libc:
                return False
            mask = _SigSet()
            libc.sigemptyset(ctypes.byref(mask))
            for sig in TRACED_SIGNALS:
                libc.sigaddset(ctypes.byref(mask), sig)
            if libc.sigprocmask(signal.SIG_BLOCK, ctypes.byref(mask), None) != 0:
                logger.debug('signal_trace: sigprocmask 失败 (errno=%s)', ctypes.get_errno())
                return False
            fd = libc.signalfd(-1, ctypes.byref(mask), 0)
            if fd < 0:
                libc.sigprocmask(signal.SIG_UNBLOCK, ctypes.byref(mask), None)
                logger.debug('signal_trace: signalfd 失败 (errno=%s)', ctypes.get_errno())
                return False
            _mask = mask
            _installed = True
            threading.Thread(target=_reader, name='signal-trace', args=(fd, extra, FORCE_EXIT_AFTER.get(_tag())),
                             daemon=True).start()
            logger.info('signal_trace: 已监控 %s，发信者信息写入 %s',
                        '/'.join(s.name for s in TRACED_SIGNALS), TRACE_PATH)
            return True
        except Exception:
            logger.exception('signal_trace: 安装失败（忽略，服务照常运行）')
            return False


def unblock_for_children():
    """在 fork 出来的子进程里解除掩码（Celery 的 worker 子进程必须能被 SIGTERM 干掉）。"""
    _unblock()


def _fmt_time(record):
    """按**读取端**的本地时区渲染记录时间（记录里存 epoch 为准）。"""
    epoch = record.get('ts_epoch')
    if isinstance(epoch, (int, float)):
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='milliseconds')
    return record.get('ts_proc_local') or record.get('ts_local') or '?'


def _show(count=10):
    if not os.path.exists(TRACE_PATH):
        print(f'{TRACE_PATH} 不存在（还没收到过被监控的信号）')
        return
    with open(TRACE_PATH) as fh:
        lines = [ln for ln in fh if ln.strip()]
    print(f'{TRACE_PATH} 共 {len(lines)} 条，末 {count} 条：\n')
    for ln in lines[-count:]:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            print(ln.rstrip())
            continue

        if r.get('tag') == 'systemd-stop':          # unit 停止时由 ExecStopPost 写的
            print(f"{_fmt_time(r)}  [systemd] {r.get('unit')} 停止: "
                  f"result={r.get('service_result')} exit_code={r.get('exit_code')} "
                  f"exit_status={r.get('exit_status')}")
            print()
            continue

        print(f"{_fmt_time(r)}  {r.get('signal')}  tag={r.get('tag')}  "
              f"si_code={r.get('si_code_meaning')}")
        print(f"    发信者: pid={r.get('sender_pid')} uid={r.get('sender_uid')} "
              f"state={r.get('sender_state')} cmdline={r.get('sender_cmdline')!r}")
        for s in (r.get('sender_ancestry') or [])[1:]:
            print(f"    ↑ 祖先: pid={s.get('pid')} ({s.get('cmdline') or s.get('comm')})")
        print(f"    本进程: pid={r.get('self_pid')} cwd={r.get('self_cwd')}")
        print()


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    _show(n)
