"""Own a single-browser, six-hour Windows invitation without touching local records."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
ROOT_ID = str(ROOT).casefold()
RUNTIME = ROOT / 'runtime'
CURRENT = RUNTIME / 'share-current.json'
CLOUDFLARED = RUNTIME / 'tools/cloudflared.exe'
CLOUDFLARED_SHA256 = 'db4e4c413612a0916f3e1679d3caaf376f94911952d163028fb726fb2e0516a9'
MODEL_URL = 'http://127.0.0.1:11434'
UPSTREAM_URL = 'http://127.0.0.1:8512'
GATEWAY_URL = 'http://127.0.0.1:8513'
LIFETIME_SECONDS = 21600
HEX64 = re.compile(r'[a-f0-9]{64}')
RUN_NAME = re.compile(r'share-\d{8}-\d{6}-[a-f0-9]{8}')
QUICK_URL = re.compile(r'https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.trycloudflare\.com(?![A-Za-z0-9.-])')
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def share_expiry(start_time, resume_until=None):
    """A resumed share may shorten, never extend, the ordinary six-hour lifetime."""
    expiry = start_time + timedelta(seconds=LIFETIME_SECONDS)
    if resume_until is None:
        return expiry
    if not isinstance(resume_until, str) or len(resume_until) > 80 or 'T' not in resume_until:
        raise RuntimeError('恢复分享的截止时间必须是带时区的 ISO 日期时间。')
    try:
        previous_expiry = datetime.fromisoformat(resume_until)
        if previous_expiry.tzinfo is None or previous_expiry.utcoffset() is None:
            raise ValueError()
        previous_expiry = previous_expiry.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RuntimeError('恢复分享的截止时间必须是带时区的 ISO 日期时间。') from None
    if previous_expiry <= start_time:
        raise RuntimeError('原分享已经到期，不能以恢复方式延长公开时间。')
    return min(expiry, previous_expiry)


def private_json(path, value):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    with temporary.open('x', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
    temporary.replace(path)


def run_path(value):
    path = Path(value).resolve()
    if path.parent != RUNTIME.resolve() or not RUN_NAME.fullmatch(path.name) or not path.is_dir():
        raise RuntimeError('分享目录必须是本项目 runtime 下由 --prepare-only 创建的 share-目录。')
    return path


def prepare():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / ('share-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
    path.mkdir(mode=0o700)
    (path / 'data').mkdir(mode=0o700)
    invite_token = secrets.token_urlsafe(32)
    config = {
        'auth_mode': 'invite', 'invite_hash_hex': hashlib.sha256(invite_token.encode()).hexdigest(),
        'secret_hex': secrets.token_hex(32),
        'state_path': str(path / 'gateway-state.json'), 'session_ttl_seconds': LIFETIME_SECONDS,
        'max_visitors': 1, 'max_profiles_per_visitor': 4, 'max_profiles_total': 4,
        'upstream_port': 8512, 'port': 8513,
    }
    private_json(path / 'gateway-config.json', config)
    private_json(path / 'share-info.json', {
        'status': 'prepared', 'url': None, 'base_url': None, 'invite_url': None, 'invite_token': invite_token,
        'created_at': utc_now(), 'started_at': None, 'expires_at': None, 'stopped_at': None,
        'run_dir': str(path), 'visitor_limit': 1,
        'usage_note': '仅限首次点击“进入体验”的一个浏览器。请保留其登录 Cookie；更换浏览器请停止后重新创建分享。',
    })
    return path


def load_run(path):
    path = run_path(path)
    config = json.loads((path / 'gateway-config.json').read_text(encoding='utf-8'))
    info = json.loads((path / 'share-info.json').read_text(encoding='utf-8'))
    if config.get('auth_mode') != 'invite':
        raise RuntimeError('当前分享仅支持单人邀请模式，请重新准备分享。')
    for key in ('invite_hash_hex', 'secret_hex'):
        if not isinstance(config.get(key), str) or not HEX64.fullmatch(config[key]):
            raise RuntimeError('分享配置的凭据字段无效，请重新准备分享。')
    if config.get('session_ttl_seconds') != LIFETIME_SECONDS:
        raise RuntimeError('分享配置的会话时长不符合要求。')
    if config.get('port') != 8513 or config.get('upstream_port') != 8512:
        raise RuntimeError('分享配置必须使用隔离端口 8512/8513。')
    if Path(config.get('state_path', '')).resolve() != path / 'gateway-state.json':
        raise RuntimeError('网关状态必须保存在当前分享目录。')
    for key, maximum in (('max_visitors', 1), ('max_profiles_per_visitor', 4), ('max_profiles_total', 4)):
        if type(config.get(key)) is not int or not 1 <= config[key] <= maximum:
            raise RuntimeError('分享配置的访客或档案上限无效。')
    invite_token = info.get('invite_token')
    if not isinstance(invite_token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', invite_token):
        raise RuntimeError('私有分享说明缺少有效邀请标识。')
    digest = hashlib.sha256(invite_token.encode()).hexdigest()
    if not hmac.compare_digest(digest, config['invite_hash_hex']):
        raise RuntimeError('私有分享说明与网关邀请配置不一致。')
    if info.get('run_dir') != str(path):
        raise RuntimeError('分享说明所属目录不一致。')
    share_expiry(datetime.now(timezone.utc), info.get('resume_until'))
    return path, config, info


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def probe(url, *, method='GET', token=None, timeout=3):
    headers = {'X-Share-Host-Token': token} if token else {}
    data = None
    if method == 'POST':
        data = b'{}'
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read(256 * 1024)


def json_request(url, *, token=None, method='GET', timeout=3):
    status, _, raw = probe(url, token=token, method=method, timeout=timeout)
    if status != 200:
        raise RuntimeError(f'接口未就绪（HTTP {status}）。')
    return json.loads(raw)


def read_current():
    try:
        value = json.loads(CURRENT.read_text(encoding='utf-8'))
        if (value.get('protocol') != 1 or value.get('root') != ROOT_ID
                or type(value.get('control_port')) is not int or not 1 <= value['control_port'] <= 65535
                or not isinstance(value.get('token'), str) or not HEX64.fullmatch(value['token'])
                or not isinstance(value.get('nonce'), str) or not HEX64.fullmatch(value['nonce'])):
            return None
        run_path(value['run_dir'])
        return value
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        return None


def active_status(current=None):
    current = current or read_current()
    if not current:
        return None
    try:
        result = json_request(f"http://127.0.0.1:{current['control_port']}/status", token=current['token'], timeout=2)
        if (result.get('app') == 'job-coach-share-host' and result.get('root') == ROOT_ID
                and result.get('nonce') == current['nonce'] and result.get('run_dir') == current['run_dir']):
            return result
    except (OSError, ValueError, RuntimeError, urllib.error.URLError, AttributeError):
        pass
    return None


def safe_status(value):
    result = {key: value.get(key) for key in ('status', 'run_dir', 'started_at', 'expires_at', 'visitor_limit',
                                             'stage', 'last_http_status', 'last_probe_error')}
    if value.get('run_dir'):
        result['share_info_path'] = str(Path(value['run_dir']) / 'share-info.json')
    return result


def stop_current():
    current = read_current()
    if not active_status(current):
        print(json.dumps({'status': 'not_running'}, ensure_ascii=False))
        return 0
    result = json_request(f"http://127.0.0.1:{current['control_port']}/stop", token=current['token'], method='POST')
    if result.get('nonce') != current['nonce'] or not result.get('stopping'):
        raise RuntimeError('当前分享启动器未确认停止，未操作其他程序。')
    for _ in range(40):
        if not active_status(current):
            print(json.dumps({'status': 'stopped', 'run_dir': current['run_dir']}, ensure_ascii=False))
            return 0
        time.sleep(.25)
    raise RuntimeError('本次分享正在退出，请稍等后再次检查状态。')


def free_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            raise RuntimeError(f'端口 {port} 已被占用，未结束占用该端口的程序。') from None


def preflight():
    if os.name != 'nt':
        raise RuntimeError('此临时分享启动器用于 Windows 本机。')
    for path in (ROOT / 'share_server.py', ROOT / 'share_gateway.py', CLOUDFLARED):
        if not path.is_file():
            raise RuntimeError(f'缺少分享运行文件：{path.name}。')
    digest = hashlib.sha256()
    with CLOUDFLARED.open('rb') as source:
        for block in iter(lambda: source.read(2 * 1024 * 1024), b''):
            digest.update(block)
    if not hmac.compare_digest(digest.hexdigest(), CLOUDFLARED_SHA256):
        raise RuntimeError('cloudflared 与已审核的 2026.10.5 版本校验值不符，未启动分享。')


class WinProcesses:
    """Create suspended children, assign them to a kill-on-close job, then resume."""
    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.processes = []
        self.job = None
        self.mutex = None
        self.handles = []
        api = self.api
        api.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        name = 'Local\\JobCoachShare-' + hashlib.sha256(ROOT_ID.encode()).hexdigest()[:24]
        self.mutex = api.CreateMutexW(None, False, name)
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == 183
        if self.already_running:
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        api.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateProcess.restype = wintypes.BOOL
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        self.job = api.CreateJobObjectW(None, None)
        if not self.job:
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def start(self, arguments, env, cwd, label):
        import msvcrt
        class StartupInfo(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                        ('lpTitle', wintypes.LPWSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
                        ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD),
                        ('dwYCountChars', wintypes.DWORD), ('dwFillAttribute', wintypes.DWORD),
                        ('dwFlags', wintypes.DWORD), ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
                        ('lpReserved2', ctypes.POINTER(ctypes.c_ubyte)), ('hStdInput', wintypes.HANDLE),
                        ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]
        class ProcessInfo(ctypes.Structure):
            _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                        ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]
        api = self.api
        api.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
            wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
            ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
        api.CreateProcessW.restype = wintypes.BOOL
        stdout = (self.log_dir / f'{label}.log').open('ab', buffering=0)
        stderr = (self.log_dir / f'{label}.err.log').open('ab', buffering=0)
        stdin = open(os.devnull, 'rb')
        opened = [stdin, stdout, stderr]
        info = ProcessInfo()
        try:
            for file in opened:
                os.set_inheritable(file.fileno(), True)
            startup = StartupInfo()
            startup.cb = ctypes.sizeof(startup)
            startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.hStdInput = msvcrt.get_osfhandle(stdin.fileno())
            startup.hStdOutput = msvcrt.get_osfhandle(stdout.fileno())
            startup.hStdError = msvcrt.get_osfhandle(stderr.fileno())
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in arguments]))
            environment = ctypes.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(env.items(), key=lambda item: item[0].upper())) + '\0\0')
            if not api.CreateProcessW(str(arguments[0]), command, None, None, True,
                    CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                    environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not api.AssignProcessToJobObject(self.job, info.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            if api.ResumeThread(info.hThread) == 0xffffffff:
                raise ctypes.WinError(ctypes.get_last_error())
            self.processes.append((label, info.hProcess, info.dwProcessId))
            return info.hProcess
        except Exception:
            if info.hProcess:
                api.TerminateProcess(info.hProcess, 1)
                api.CloseHandle(info.hProcess)
            raise
        finally:
            if info.hThread:
                api.CloseHandle(info.hThread)
            for file in opened:
                file.close()

    def exited(self):
        for label, handle, _ in self.processes:
            code = wintypes.DWORD()
            if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return label, code.value
        return None

    def close(self):
        if self.job:
            self.api.TerminateJobObject(self.job, 0)
            self.api.CloseHandle(self.job)
            self.job = None
        for _, handle, _ in self.processes:
            self.api.CloseHandle(handle)
        self.processes.clear()
        if self.mutex:
            self.api.CloseHandle(self.mutex)
            self.mutex = None



def check_protection(base):
    status, _, _ = probe(base + '/share/login')
    if status != 200:
        raise RuntimeError('分享登录页尚未就绪。')
    for suffix in ('/', '/health', '/api/state', '/presentation', '/downloads/host-readiness-check'):
        status, headers, _ = probe(base + suffix)
        location = next((value for key, value in headers.items() if key.lower() == 'location'), '')
        parsed = urllib.parse.urlparse(location)
        redirected = status in (301, 302, 303, 307, 308) and parsed.path == '/share/login' and (not parsed.netloc or parsed.netloc == urllib.parse.urlparse(base).netloc)
        if status not in (401, 403) and not redirected:
            raise RuntimeError(f'分享保护检查失败：{suffix} 未阻止未认证访问（HTTP {status}）。')
    status, _, _ = probe(base + '/api/state', method='POST')
    if status not in (401, 403):
        raise RuntimeError(f'分享保护检查失败：未认证 API POST 返回 HTTP {status}。')


def run_share(path, no_display=False):
    preflight()
    path, config, info = load_run(path)
    processes = WinProcesses(path)
    controller = None
    control_thread = None
    stopped = threading.Event()
    interrupted = [False]
    old_handlers = {}
    nonce = secrets.token_hex(32)
    owned_state = False
    result_status = 'failed'
    try:
        if processes.already_running:
            existing = active_status()
            print(json.dumps(safe_status(existing) if existing else {'status': 'already_starting'}, ensure_ascii=False))
            return 0
        if info.get('started_at'):
            raise RuntimeError('此分享目录已经使用过。请不带 --start-run 重新运行，以生成新邀请和独立数据。')
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda _sig, _frame: interrupted.__setitem__(0, True))
        free_port(8512)
        free_port(8513)
        start_time = datetime.now(timezone.utc)
        expiry = share_expiry(start_time, info.get('resume_until'))
        deadline = time.monotonic() + (expiry - start_time).total_seconds()
        info.update(status='starting', started_at=start_time.isoformat(timespec='seconds'),
                    expires_at=expiry.isoformat(timespec='seconds'),
                    visitor_limit=config['max_visitors'])
        private_json(path / 'share-info.json', info)
        token = secrets.token_hex(32)
        class ControlHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, status=200):
                raw = json.dumps(value).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                supplied = self.headers.get('X-Share-Host-Token', '')
                return bool(HEX64.fullmatch(supplied)) and hmac.compare_digest(supplied, token)

            def do_GET(self):
                if self.path != '/status' or not self.authorized():
                    return self.reply({'error': 'not authorized'}, 403)
                self.reply(dict(safe_status(info), app='job-coach-share-host', root=ROOT_ID, nonce=nonce))

            def do_POST(self):
                if self.path != '/stop' or not self.authorized():
                    return self.reply({'error': 'not authorized'}, 403)
                self.reply({'stopping': True, 'nonce': nonce})
                stopped.set()

        controller = ThreadingHTTPServer(('127.0.0.1', 0), ControlHandler)
        control_thread = threading.Thread(target=controller.serve_forever, daemon=True)
        control_thread.start()
        private_json(CURRENT, {'protocol': 1, 'root': ROOT_ID, 'nonce': nonce, 'token': token,
                              'control_port': controller.server_port, 'run_dir': str(path)})
        owned_state = True
        env = os.environ.copy()
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
        for key in ('TUNNEL_TOKEN', 'TUNNEL_CRED_FILE', 'TUNNEL_ORIGIN_CERT', 'TUNNEL_CONFIG'):
            env.pop(key, None)
        env.update({'OLLAMA_HOST': '127.0.0.1:11434', 'OLLAMA_NO_CLOUD': '1', 'OLLAMA_CONTEXT_LENGTH': '8192',
                    'MODEL_BASE_URL': MODEL_URL + '/v1', 'MODEL_NAME': 'qwen3:1.7b',
                    'MODEL_API_KEY': 'ollama', 'MODEL_TIMEOUT': '120', 'CAREER_RUNTIME': str(path / 'data'),
                    'NO_PROXY': 'localhost,127.0.0.1,::1', 'no_proxy': 'localhost,127.0.0.1,::1',
                    'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
        def ensure_running():
            if interrupted[0] or stopped.is_set():
                raise InterruptedError('分享启动已取消。')
            if time.monotonic() >= deadline:
                raise TimeoutError('分享已达到本次截止时间。')
            exited = processes.exited()
            if exited:
                raise RuntimeError(f'{exited[0]} 已退出（代码 {exited[1]}），请查看当前分享目录中的日志。')

        def wait_for(check, *, stage, seconds=45, poll_interval=.25):
            began = time.monotonic()
            end = min(deadline, began + seconds)
            attempts = 0
            last_printed = None
            last_printed_at = 0.0
            last = {}
            info.update(stage=stage, last_http_status=None, last_probe_error=None)
            private_json(path / 'share-info.json', info)
            while time.monotonic() < end:
                ensure_running()
                attempts += 1
                ready = False
                code = None
                error_type = None
                reason_type = None
                error_number = None
                try:
                    result = check()
                    if isinstance(result, tuple):
                        ready, code = result
                    else:
                        ready = bool(result)
                except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                    # Do not persist exception strings: they can contain arbitrary URLs or data.
                    error_type = type(exc).__name__
                    reason_type = type(exc.reason).__name__ if isinstance(exc, urllib.error.URLError) else None
                    error_number = exc.errno if isinstance(getattr(exc, 'errno', None), int) else None
                    code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
                info.update(last_http_status=code, last_probe_error=error_type)
                last = {'stage': stage, 'attempt': attempts, 'ready': bool(ready),
                        'http_status': code, 'error_type': error_type, 'reason_type': reason_type,
                        'errno': error_number, 'checked_at': utc_now(),
                        'elapsed_seconds': round(time.monotonic() - began, 1)}
                private_json(path / 'readiness.json', last)
                signature = (bool(ready), code, error_type, reason_type, error_number)
                if signature != last_printed or time.monotonic() - last_printed_at >= 5:
                    print(json.dumps({'readiness': last}, ensure_ascii=False), flush=True)
                    last_printed = signature
                    last_printed_at = time.monotonic()
                if ready:
                    private_json(path / 'share-info.json', info)
                    return
                stopped.wait(poll_interval)
            detail = f"最后 HTTP {last['http_status']}" if last.get('http_status') is not None else f"最后错误 {last.get('error_type') or '尚未满足就绪条件'}"
            raise RuntimeError(f'等待阶段 {stage} 超时（{detail}）。请查看当前分享目录中的 readiness.json 和服务日志。')

        def http_ready(url, *, require_health=False, timeout=3):
            code, _, raw = probe(url, timeout=timeout)
            if code != 200:
                return False, code
            if require_health:
                try:
                    return json.loads(raw).get('status') == 'ok', code
                except (ValueError, AttributeError):
                    return False, code
            return True, code

        try:
            model_tags = json_request(MODEL_URL + '/api/tags', timeout=2)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            free_port(11434)
            ollama = shutil.which('ollama')
            if not ollama:
                candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Ollama/ollama.exe'
                ollama = str(candidate) if candidate.is_file() else None
            if not ollama:
                raise RuntimeError('未找到 Ollama。请先安装并准备 qwen3:1.7b 模型，本启动器不下载模型。')
            processes.start([ollama, 'serve'], env, ROOT, 'ollama')
            wait_for(lambda: http_ready(MODEL_URL + '/api/tags'), stage='ollama-ready')
            model_tags = json_request(MODEL_URL + '/api/tags')
        if not any(model.get('name') == 'qwen3:1.7b' for model in model_tags.get('models', [])):
            raise RuntimeError('本机 Ollama 中没有 qwen3:1.7b，未启动公网分享。')
        processes.start([sys.executable, '-B', '-u', str(ROOT / 'share_server.py'), '--port', '8512'], env, ROOT, 'backend')
        wait_for(lambda: http_ready(UPSTREAM_URL + '/health', require_health=True), stage='backend-ready')
        processes.start([sys.executable, '-B', '-u', str(ROOT / 'share_gateway.py'), '--config', str(path / 'gateway-config.json')], env, ROOT, 'gateway')
        wait_for(lambda: http_ready(GATEWAY_URL + '/share/login'), stage='gateway-ready')
        # These checks happen before any tunnel is created and never redeem the invitation.
        check_protection(GATEWAY_URL)
        processes.start([CLOUDFLARED, 'tunnel', '--no-autoupdate', '--protocol', 'http2', '--url', GATEWAY_URL], env, path, 'cloudflared')
        candidates = []
        def find_url():
            for log in (path / 'cloudflared.log', path / 'cloudflared.err.log'):
                try:
                    text = log.read_text(encoding='utf-8', errors='replace')[-500000:]
                except OSError:
                    continue
                for match in QUICK_URL.finditer(text):
                    if match.group(1) not in ('api', 'www') and match.group(0) not in candidates:
                        candidates.append(match.group(0))
            return bool(candidates)
        wait_for(find_url, stage='tunnel-url', seconds=90, poll_interval=.5)
        public_url = candidates[-1]
        # Confirm the published endpoint also presents the gate, without consuming the visitor slot.
        wait_for(lambda: http_ready(public_url + '/share/login', timeout=5), stage='public-login', seconds=90, poll_interval=2)
        check_protection(public_url)
        invite_url = public_url + '/share/login#invite=' + info['invite_token']
        info.update(status='running', stage='ready', base_url=public_url, invite_url=invite_url, url=invite_url)
        private_json(path / 'share-info.json', info)
        result_status = 'stopped'
        print(json.dumps(safe_status(info), ensure_ascii=False), flush=True)
        if not no_display and sys.stdout.isatty():
            print('\n专属邀请链接：' + invite_url)
            print('无需口令；首次点击“进入体验”的浏览器会被绑定。请保留该浏览器的登录 Cookie。')
            print('保留此窗口；按 Ctrl+C 或双击 stop-share.bat 结束。最多六小时，恢复时保留原截止时间。', flush=True)
        while not stopped.wait(.5) and not interrupted[0]:
            if time.monotonic() >= deadline:
                result_status = 'expired'
                break
            exited = processes.exited()
            if exited:
                result_status = 'failed'
                raise RuntimeError(f'{exited[0]} 意外退出，请查看当前分享目录中的日志。')
        return 0
    except (KeyboardInterrupt, InterruptedError):
        result_status = 'stopped'
        return 0
    finally:
        processes.close()
        if controller:
            controller.shutdown()
            controller.server_close()
        if control_thread:
            control_thread.join(timeout=2)
        if owned_state:
            info.update(status=result_status, stopped_at=utc_now())
            private_json(path / 'share-info.json', info)
            current = read_current()
            if current and current.get('nonce') == nonce:
                CURRENT.unlink(missing_ok=True)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def start_background(path):
    preflight()
    path, _, _ = load_run(path)
    with (path / 'host.log').open('ab', buffering=0) as stdout, (path / 'host.err.log').open('ab', buffering=0) as stderr:
        process = subprocess.Popen([sys.executable, '-B', '-u', '-X', 'utf8', str(Path(__file__).resolve()), '--start-run', str(path), '--no-display'],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, creationflags=CREATE_NO_WINDOW)
    for _ in range(240):
        code = process.poll()
        if code is not None:
            existing = active_status()
            if code == 0 and existing:
                print(json.dumps(safe_status(existing), ensure_ascii=False))
                return 0
            raise RuntimeError('后台分享启动器已退出，请查看本次分享目录中的 host.err.log。')
        current = active_status()
        if current and current.get('run_dir') == str(path) and current.get('status') == 'running':
            print(json.dumps(safe_status(current), ensure_ascii=False), flush=True)
            return 0
        time.sleep(.5)
    print(json.dumps({'status': 'starting', 'run_dir': str(path), 'note': '可用 --status 查看进展'}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description='Single-browser temporary invitation (Windows, six-hour lifetime)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare-only', action='store_true')
    mode.add_argument('--start-run', metavar='DIRECTORY')
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--stop', action='store_true')
    parser.add_argument('--no-display', action='store_true', help='Never print the private invitation link')
    parser.add_argument('--background', action='store_true', help='Start a hidden host and never print the invitation link')
    args = parser.parse_args()
    try:
        if args.status:
            current = active_status()
            print(json.dumps(safe_status(current) if current else {'status': 'not_running'}, ensure_ascii=False))
            return 0
        if args.stop:
            return stop_current()
        if not args.prepare_only:
            current = active_status()
            if current:
                print(json.dumps(safe_status(current), ensure_ascii=False))
                return 0
        path = run_path(args.start_run) if args.start_run else prepare()
        if args.prepare_only:
            print(json.dumps({'status': 'prepared', 'run_dir': str(path), 'config_path': str(path / 'gateway-config.json'),
                              'share_info_path': str(path / 'share-info.json'), 'invite_printed': False}, ensure_ascii=False))
            return 0
        if args.background:
            return start_background(path)
        return run_share(path, args.no_display)
    except Exception as exc:
        print('分享操作失败：' + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
