"""Authenticated, bounded loopback gateway for a small temporary class demo."""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from contextlib import contextmanager
import hashlib
import hmac
import html
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 12 * 1024 * 1024
SESSION = re.compile(r'[a-f0-9]{32}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
PUBLIC_HOST = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com')
DOWNLOAD = re.compile(r'/downloads/[A-Za-z0-9_-]+\.(?:md|html|csv|xlsx|zip|txt|docx|json)')
POST_ROUTES = {'/api/state', '/api/target', '/api/documents', '/api/upload', '/api/demo',
    '/api/match', '/api/resume', '/api/interview/start', '/api/interview/answer',
    '/api/applications/prepare', '/api/confirm', '/api/applications', '/api/export',
    '/api/chat', '/api/documents/delete', '/api/skills/profile', '/api/opportunities/list',
    '/api/opportunities/save', '/api/opportunities/delete', '/api/recommendations', '/api/recommendations/export'}


class GatewayError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def validate_restore_config(config):
    """Only host-prepared profile identifiers may cross a temporary-domain change."""
    profiles = config.get('restore_profiles', [])
    selection = config.get('restore_selection')
    if not isinstance(profiles, list) or len(profiles) > 4:
        raise ValueError('Invalid restored profile list')
    if any(not isinstance(value, str) or not SESSION.fullmatch(value) for value in profiles):
        raise ValueError('Invalid restored profile identifier')
    if len(set(profiles)) != len(profiles):
        raise ValueError('Duplicate restored profile identifier')
    if not profiles:
        if selection is not None:
            raise ValueError('Restored selection requires restored profiles')
        return [], None
    if config.get('auth_mode') != 'invite' or config.get('max_visitors') != 1:
        raise ValueError('Profile restoration requires a single-browser invitation')
    if len(profiles) > min(config.get('max_profiles_per_visitor', 4), config.get('max_profiles_total', 4)):
        raise ValueError('Restored profiles exceed the configured limit')
    if not isinstance(selection, dict) or set(selection) - {'real_session', 'demo_session', 'mode'}:
        raise ValueError('Invalid restored profile selection')
    real, demo, mode = selection.get('real_session'), selection.get('demo_session'), selection.get('mode')
    if not isinstance(real, str) or real not in profiles:
        raise ValueError('Restored real profile must belong to the restored set')
    if demo is not None and (not isinstance(demo, str) or demo not in profiles or demo == real):
        raise ValueError('Restored demo profile must belong to the restored set')
    if mode not in ('real', 'demo') or (mode == 'demo' and demo is None):
        raise ValueError('Invalid restored profile mode')
    return list(profiles), {'real_session': real, 'demo_session': demo, 'mode': mode}


def load_config(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    data.setdefault('auth_mode', 'password')
    if data['auth_mode'] not in ('password', 'invite'):
        raise ValueError('Invalid authentication mode')
    fields = [('secret_hex', 32)]
    fields += [('invite_hash_hex', 32)] if data['auth_mode'] == 'invite' else [('password_salt_hex', 16), ('password_hash_hex', 32)]
    for name, size in fields:
        raw = bytes.fromhex(data[name])
        if len(raw) < size or (name in ('password_hash_hex', 'invite_hash_hex') and len(raw) != 32):
            raise ValueError('Invalid private gateway configuration')
    defaults = {'password_iterations': 200000, 'session_ttl_seconds': 21600,
        'max_visitors': 20, 'max_profiles_per_visitor': 4, 'max_profiles_total': 60,
        'upstream_port': 8512, 'port': 8513}
    for name, default in defaults.items():
        data.setdefault(name, default)
        if type(data[name]) is not int or data[name] <= 0:
            raise ValueError('Invalid private gateway configuration')
    if not 200000 <= data['password_iterations'] <= 1000000:
        raise ValueError('Invalid password iteration count')
    if data['port'] != 8513 or data['upstream_port'] != 8512:
        raise ValueError('The share gateway uses loopback ports 8513 and 8512')
    if data['auth_mode'] == 'invite' and data['max_visitors'] != 1:
        raise ValueError('A single-browser invitation requires max_visitors=1')
    validate_restore_config(data)
    state_path = Path(data['state_path'])
    data['state_path'] = str((path.parent / state_path).resolve()) if not state_path.is_absolute() else str(state_path.resolve())
    return data


def host_context(host):
    """Never trust forwarded host/scheme headers for cookie or origin decisions."""
    if not isinstance(host, str) or not host or re.search(r'[\s\\/#?@,]', host):
        raise GatewayError(400, '访问地址无效。')
    try:
        url = urlsplit('//' + host)
        name, port = url.hostname, url.port
    except ValueError:
        raise GatewayError(400, '访问地址无效。') from None
    if name in ('localhost', '127.0.0.1', '::1'):
        if port not in (None, 8513):
            raise GatewayError(400, '访问端口无效。')
        rendered = '[' + name + ']' if name == '::1' else name
        return False, 'http://' + rendered + (':' + str(port) if port else '')
    if name and PUBLIC_HOST.fullmatch(name) and port in (None, 443):
        return True, 'https://' + name
    raise GatewayError(400, '此地址不属于当前分享入口。')


def check_origin(origin, expected):
    if origin is None:
        return
    try:
        parsed = urlsplit(origin)
        if parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError()
        scheme = parsed.scheme.lower()
        name = parsed.hostname
        if not name or scheme not in ('http', 'https'):
            raise ValueError()
        port = parsed.port
        rendered = '[' + name + ']' if ':' in name else name
        normalized = scheme + '://' + rendered
        if port is not None and port != (443 if scheme == 'https' else 80):
            normalized += ':' + str(port)
        if normalized != expected:
            raise ValueError()
    except (TypeError, ValueError):
        raise GatewayError(403, '请从当前分享页面操作。') from None


class RateLimits:
    def __init__(self, clock=time.time):
        self.clock, self.lock = clock, threading.Lock()
        self.events = defaultdict(deque)

    def check(self, key, limit, period=60):
        now = self.clock()
        with self.lock:
            # Source-address keys are untrusted and must not grow memory without bound.
            if len(self.events) >= 2048:
                self.events = defaultdict(deque, {k: v for k, v in self.events.items() if v and v[-1] > now - 60})
                if len(self.events) >= 2048 and key not in self.events:
                    raise GatewayError(429, '访问较多，请一分钟后再试。')
            events = self.events[key]
            while events and events[0] <= now - period:
                events.popleft()
            if len(events) >= limit:
                raise GatewayError(429, '操作太频繁，请一分钟后再试。')
            events.append(now)


class ShareState:
    def __init__(self, config, clock=time.time):
        self.config, self.clock = config, clock
        self.restore_profiles, self.restore_selection = validate_restore_config(config)
        self.secret = bytes.fromhex(config['secret_hex'])
        self.path = Path(config['state_path'])
        self.lock = threading.RLock()
        self.rates = RateLimits(clock)
        self.chat_slots = threading.BoundedSemaphore(1)
        self.upload_slots = threading.BoundedSemaphore(2)
        self.data = {'version': 1, 'visitors': {}, 'owners': {}}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('visitors'), dict) or not isinstance(data.get('owners'), dict):
                raise ValueError('Invalid share state; refusing to reset ownership')
            for visitor, record in data['visitors'].items():
                if not SESSION.fullmatch(visitor) or not isinstance(record, dict) or not re.fullmatch(r'[a-f0-9]{64}', record.get('token_hash', '')):
                    raise ValueError('Invalid persisted visitor')
                if not isinstance(record.get('expires'), (int, float)) or not isinstance(record.get('created'), (int, float)):
                    raise ValueError('Invalid persisted visitor expiry')
                if 'browser_hash' in record and not re.fullmatch(r'[a-f0-9]{64}', record['browser_hash']):
                    raise ValueError('Invalid persisted browser identity')
            if any(not SESSION.fullmatch(s) or v not in data['visitors'] for s, v in data['owners'].items()):
                raise ValueError('Invalid persisted profile ownership')
            self.data = data
        if config.get('auth_mode') == 'invite' and len(self.data['visitors']) > 1:
            raise ValueError('Existing state is not a single-browser invitation')
        if self.restore_profiles and self.data['visitors']:
            visitor = next(iter(self.data['visitors']))
            if len(self.data['visitors']) != 1 or any(self.data['owners'].get(profile) != visitor for profile in self.restore_profiles):
                raise ValueError('Existing state does not own the restored profiles; refusing to reassign them')

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(self.data, output, ensure_ascii=False)
        os.replace(temporary, self.path)

    def token_hash(self, token, purpose='auth'):
        return hmac.new(self.secret, (purpose + ':' + token).encode(), hashlib.sha256).hexdigest()

    def token_owner(self, token, purpose='auth'):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        digest = self.token_hash(token, purpose)
        field = 'token_hash' if purpose == 'auth' else 'browser_hash'
        with self.lock:
            for visitor, record in self.data['visitors'].items():
                if hmac.compare_digest(record.get(field, ''), digest):
                    return visitor
        return None

    def authenticate(self, token):
        with self.lock:
            visitor = self.token_owner(token)
            if visitor and self.data['visitors'][visitor]['expires'] > self.clock():
                return visitor
        return None

    def valid_password(self, password):
        if not isinstance(password, str) or not 1 <= len(password) <= 200:
            return False
        derived = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(self.config['password_salt_hex']), self.config['password_iterations'])
        return hmac.compare_digest(derived, bytes.fromhex(self.config['password_hash_hex']))

    def login(self, password, old_token=None):
        return self.login_with_browser(password, old_token)[0]

    def login_with_browser(self, password, old_token=None, browser_token=None):
        if not self.valid_password(password):
            raise GatewayError(403, '访问口令不正确。')
        with self.lock:
            # A remembered browser proves archive ownership only after the password passes.
            visitor = self.token_owner(old_token) or self.token_owner(browser_token, 'browser')
            if visitor is None:
                if len(self.data['visitors']) >= self.config['max_visitors']:
                    raise GatewayError(429, '本次体验人数已达到上限，请联系分享者。')
                visitor = secrets.token_hex(16)
            if self.token_owner(browser_token, 'browser') != visitor:
                browser_token = secrets.token_urlsafe(32)
            token = secrets.token_urlsafe(32)
            now = self.clock()
            self.data['visitors'][visitor] = {'token_hash': self.token_hash(token), 'browser_hash': self.token_hash(browser_token, 'browser'),
                'created': self.data['visitors'].get(visitor, {}).get('created', now),
                'expires': now + self.config['session_ttl_seconds']}
            self.save()
            return token, browser_token

    def logout(self, token):
        with self.lock:
            visitor = self.authenticate(token)
            if visitor:
                self.data['visitors'][visitor]['expires'] = 0
                self.save()

    def redeem(self, invite, browser_token):
        """First explicit redemption binds forever to one remembered browser."""
        failure = GatewayError(403, '此邀请无法在当前浏览器使用，请联系分享者。')
        if self.config.get('auth_mode') != 'invite' or not isinstance(browser_token, str) or not TOKEN.fullmatch(browser_token):
            raise failure
        with self.lock:
            first_redemption = not self.data['visitors']
            visitor = self.token_owner(browser_token, 'browser')
            if self.data['visitors']:
                if visitor is None or len(self.data['visitors']) != 1:
                    raise failure
            else:
                if not isinstance(invite, str) or not TOKEN.fullmatch(invite):
                    raise failure
                supplied = hashlib.sha256(invite.encode('ascii')).hexdigest()
                if not hmac.compare_digest(supplied, self.config['invite_hash_hex']):
                    raise failure
                visitor = secrets.token_hex(16)
            token = secrets.token_urlsafe(32)
            now = self.clock()
            previous = {'version': 1, 'visitors': dict(self.data['visitors']), 'owners': dict(self.data['owners'])}
            self.data['visitors'][visitor] = {'token_hash': self.token_hash(token),
                'browser_hash': self.token_hash(browser_token, 'browser'),
                'created': self.data['visitors'].get(visitor, {}).get('created', now),
                'expires': now + self.config['session_ttl_seconds']}
            if first_redemption:
                self.data['owners'].update({profile: visitor for profile in self.restore_profiles})
            try:
                self.save()
            except Exception:
                self.data = previous
                raise
            return token, browser_token

    def restore_bootstrap(self, visitor):
        """Authenticated HTML only; never reveal profile ids on the invitation page."""
        if not self.restore_selection:
            return b''
        with self.lock:
            if visitor not in self.data['visitors'] or any(self.data['owners'].get(profile) != visitor for profile in self.restore_profiles):
                raise GatewayError(403, '无法恢复不属于当前浏览器的档案。')
        selection = json.dumps(self.restore_selection, separators=(',', ':'), ensure_ascii=True)
        marker = hashlib.sha256(selection.encode('ascii')).hexdigest()
        script = ('<script data-share-restore>(function(){"use strict";const r=' + selection + ';'
            'try{const marker="job_coach_share_restore_v1";if(localStorage.getItem(marker)==="' + marker + '")return;'
            'localStorage.setItem("job_coach_session_v1",r.real_session);'
            'if(r.demo_session)localStorage.setItem("job_coach_demo_session_v1",r.demo_session);'
            'else localStorage.removeItem("job_coach_demo_session_v1");'
            'localStorage.setItem("job_coach_mode_v1",r.mode);'
            'localStorage.setItem(marker,"' + marker + '");}catch(e){}})();</script>')
        return script.encode('ascii')

    def own_profile(self, visitor, session, register=False):
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise GatewayError(400, '档案编号无效，请刷新页面。')
        with self.lock:
            owner = self.data['owners'].get(session)
            if owner is not None:
                if owner != visitor:
                    raise GatewayError(403, '无法访问不属于当前浏览器的档案。')
                return
            if not register:
                raise GatewayError(403, '无法访问不属于当前浏览器的档案。')
            if visitor not in self.data['visitors']:
                raise GatewayError(401, '请先登录体验入口。')
            owned = sum(v == visitor for v in self.data['owners'].values())
            if owned >= self.config['max_profiles_per_visitor'] or len(self.data['owners']) >= self.config['max_profiles_total']:
                raise GatewayError(429, '本次体验的档案数量已达上限，请继续使用已有档案。')
            self.data['owners'][session] = visitor
            self.save()

    def csrf(self, origin):
        value = str(int(self.clock())) + '.' + secrets.token_hex(16)
        signature = hmac.new(self.secret, ('login:' + origin + ':' + value).encode(), hashlib.sha256).hexdigest()
        return value + '.' + signature

    def verify_csrf(self, submitted, cookie, origin):
        if not isinstance(submitted, str) or not isinstance(cookie, str) or len(submitted) > 160 or not hmac.compare_digest(submitted, cookie):
            raise GatewayError(403, '登录验证已失效，请刷新登录页重试。')
        try:
            timestamp, random, signature = submitted.split('.')
            if not timestamp.isdigit() or not re.fullmatch(r'[a-f0-9]{32}', random) or not re.fullmatch(r'[a-f0-9]{64}', signature):
                raise ValueError()
            if not 0 <= self.clock() - int(timestamp) <= 600:
                raise ValueError()
            expected = hmac.new(self.secret, ('login:' + origin + ':' + timestamp + '.' + random).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
        except (ValueError, TypeError):
            raise GatewayError(403, '登录验证已失效，请刷新登录页重试。') from None

    @contextmanager
    def admission(self, visitor, path):
        self.rates.check(('visitor', visitor), 60)
        semaphore = None
        if path == '/api/chat':
            self.rates.check(('chat', visitor), 6)
            semaphore = self.chat_slots
        elif path == '/api/upload':
            self.rates.check(('upload', visitor), 6)
            semaphore = self.upload_slots
        if semaphore is not None and not semaphore.acquire(blocking=False):
            raise GatewayError(429, '当前有同学正在使用模型或导入文件，请稍后再试。')
        try:
            yield
        finally:
            if semaphore is not None:
                semaphore.release()


def cookie_header(name, value, secure, age):
    item = f'{name}={value}; Path=/; Max-Age={int(age)}; HttpOnly; SameSite=Strict'
    return item + ('; Secure' if secure else '')


def fetch_upstream(config, method, path, data, csrf):
    # A chat can contain up to six bounded model rounds; hold its slot through the full body.
    connection = http.client.HTTPConnection('127.0.0.1', config['upstream_port'], timeout=750 if path == '/api/chat' else 30)
    headers = {'Connection': 'close', 'User-Agent': 'JobCoach-Share-Gateway'}
    if method == 'POST':
        headers['Content-Type'] = 'application/json'
        headers['X-CSRF-Token'] = csrf
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        length = response.getheader('Content-Length')
        if length and (not length.isdigit() or int(length) > MAX_RESPONSE):
            raise GatewayError(502, '返回内容过大，请缩小材料后重试。')
        payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise GatewayError(502, '返回内容过大，请缩小材料后重试。')
        response_headers = {'Content-Type': response.getheader('Content-Type', 'application/octet-stream')}
        disposition = response.getheader('Content-Disposition')
        if disposition and '\r' not in disposition and '\n' not in disposition:
            response_headers['Content-Disposition'] = disposition
        return response.status, response_headers, payload
    except GatewayError:
        raise
    except (OSError, http.client.HTTPException):
        raise GatewayError(502, '求职教练暂时没有响应，请稍后重试。') from None
    finally:
        connection.close()


class ShareHandler(BaseHTTPRequestHandler):
    server_version = 'JobCoachShare'
    sys_version = ''

    def log_message(self, *args):
        pass

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    @property
    def state(self):
        return self.server.share_state

    def send(self, status, data, headers=None):
        self.close_connection = True
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif isinstance(data, str):
            data = data.encode('utf-8')
            content_type = 'text/html; charset=utf-8'
        else:
            content_type = 'application/octet-stream'
        headers = headers or []
        if isinstance(headers, dict):
            headers = list(headers.items())
        self.send_response(status)
        if not any(k.lower() == 'content-type' for k, _ in headers):
            self.send_header('Content-Type', content_type)
        for key, value in headers:
            self.send_header(key, value)
        for key, value in [('Content-Length', str(len(data))), ('Connection', 'close'),
                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                # Native form POSTs need their same-origin Origin; no-referrer can make it null.
                ('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'same-origin')]:
            self.send_header(key, value)
        if status == 429:
            self.send_header('Retry-After', '60')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (OSError, socket.timeout):
            pass

    def context(self):
        if len(self.headers.get_all('Host', [])) != 1:
            raise GatewayError(400, '访问地址无效。')
        secure, origin = host_context(self.headers['Host'])
        if len(self.headers.get_all('Origin', [])) > 1:
            raise GatewayError(403, '访问来源无效。')
        check_origin(self.headers.get('Origin'), origin)
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            pass
        prefix = '__Host-' if secure else ''
        auth_name, csrf_name = prefix + 'qcs_share_auth', prefix + 'qcs_login_csrf'
        browser_name = prefix + 'qcs_share_browser'
        token = cookies[auth_name].value if auth_name in cookies else None
        login_csrf = cookies[csrf_name].value if csrf_name in cookies else None
        browser_token = cookies[browser_name].value if browser_name in cookies else None
        return secure, origin, auth_name, csrf_name, token, login_csrf, browser_name, browser_token

    def read_body(self, limit):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise GatewayError(400, '请求长度无效。')
        length = int(lengths[0])
        if not 0 < length <= limit:
            raise GatewayError(413, '请求内容过大或为空。')
        data = self.rfile.read(length)
        if len(data) != length:
            raise GatewayError(400, '请求内容不完整。')
        return data

    def login_form(self, secure, origin, csrf_name, authenticated, browser_token=None):
        nonce = self.state.csrf(origin)
        invite_mode = self.state.config.get('auth_mode') == 'invite'
        headers = [('Set-Cookie', cookie_header(csrf_name, nonce, secure, 600))]
        script, script_policy = '', ''
        button_extra = ''
        if authenticated:
            action, field, button = '/share/logout', '<p>当前浏览器已登录。</p><p><a href="/">返回求职教练</a></p>', '退出当前会话'
        elif invite_mode:
            action = '/share/redeem'
            remembered = self.state.token_owner(browser_token, 'browser') is not None
            if not isinstance(browser_token, str) or not TOKEN.fullmatch(browser_token):
                browser_token = secrets.token_urlsafe(32)
            browser_name = ('__Host-' if secure else '') + 'qcs_share_browser'
            headers.append(('Set-Cookie', cookie_header(browser_name, browser_token, secure, 7 * 24 * 60 * 60)))
            field = '<input type="hidden" id="share-invite" name="invite" value=""><p id="invite-status">请使用分享者发给你的完整邀请链接。</p><noscript>首次进入需要启用浏览器 JavaScript。</noscript>'
            button = '继续体验' if remembered else '进入体验'
            button_extra = ' id="share-enter"' + ('' if remembered else ' disabled')
            script_nonce = secrets.token_urlsafe(24)
            code = '''(function(){
const field=document.getElementById('share-invite'),button=document.getElementById('share-enter'),status=document.getElementById('invite-status');
let invite='';try{invite=new URLSearchParams(location.hash.slice(1)).get('invite')||'';}catch(e){}
if(location.hash)history.replaceState(null,'',location.pathname+location.search);
if(/^[A-Za-z0-9_-]{43}$/.test(invite)){field.value=invite;button.disabled=false;status.textContent='点击后将名额绑定到当前浏览器。请在准备使用的浏览器中操作。';}
else if(REMEMBERED_BOOLEAN){button.disabled=false;status.textContent='已识别当前浏览器，点击可恢复本次体验。';}
document.querySelector('form').addEventListener('submit',function(event){if(!event.isTrusted)event.preventDefault();});
})();'''.replace('REMEMBERED_BOOLEAN', 'true' if remembered else 'false')
            script = '<script nonce="' + script_nonce + '">' + code + '</script>'
            script_policy = " script-src 'nonce-" + script_nonce + "';"
        else:
            action = '/share/login'
            field = '<label>访问口令<input type="password" name="password" maxlength="200" required autocomplete="current-password"></label>'
            button = '进入体验'
        binding_note = '只允许首个点击进入的浏览器使用。退出当前会话不会转让名额；同一浏览器可继续体验。' if invite_mode else '登录有效期约 6 小时。档案绑定当前浏览器。'
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>启程 · 同学体验</title><style>body{font:16px/1.7 system-ui,sans-serif;background:#f5f6fa;color:#202b3d;max-width:480px;margin:10vh auto;padding:24px}main{background:white;padding:28px;border:1px solid #e4e7ed;border-radius:14px}h1{font-size:25px}input,button{box-sizing:border-box;display:block;width:100%;font:inherit;margin:12px 0;padding:10px;border:1px solid #ccd3e4;border-radius:7px}button{background:#4761cf;color:white}button:disabled{opacity:.45}small{color:#6d778c}</style><main><h1>启程 · 求职教练</h1><p>同学临时体验入口。资料会保存在提供此链接的电脑中，请使用示例或脱敏资料，勿上传身份证号、住址等敏感信息。</p><form method="post" action="' + action + '"><input type="hidden" name="csrf" value="' + html.escape(nonce, quote=True) + '">' + field + '<button type="submit"' + button_extra + '>' + button + '</button></form><small>' + binding_note + ' 不向企业发送材料。</small></main>' + script + '</html>'
        headers.append(('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline';" + script_policy + " form-action 'self'; base-uri 'none'; frame-ancestors 'none'"))
        self.send(200, page, headers)

    def _dispatch(self):
        secure, origin, auth_name, csrf_name, token, login_csrf, browser_name, browser_token = self.context()
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or '\\' in parsed.path:
            raise GatewayError(400, '请求地址无效。')
        path = parsed.path
        visitor = self.state.authenticate(token)
        invite_mode = self.state.config.get('auth_mode') == 'invite'
        if path == '/share/login' and self.command == 'GET' and parsed.query in ('', 'manage=1'):
            self.state.rates.check(('login-page-global',), 120)
            if invite_mode and visitor and not parsed.query:
                return self.send(303, '', [('Location', '/')])
            return self.login_form(secure, origin, csrf_name, bool(visitor), browser_token)
        if path in ('/share/login', '/share/logout', '/share/redeem') and self.command == 'POST' and not parsed.query:
            if (path == '/share/redeem' and not invite_mode) or (path == '/share/login' and invite_mode):
                raise GatewayError(400, '请使用当前体验页面进入。')
            self.state.rates.check(('login-global',), 20)
            address = self.headers.get('CF-Connecting-IP', self.client_address[0])
            try:
                address = str(ipaddress.ip_address(address))
            except ValueError:
                address = 'unknown'
            self.state.rates.check(('login-source', address), 6)
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
                raise GatewayError(400, '请通过登录表单提交。')
            try:
                values = parse_qs(self.read_body(4096).decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            except (ValueError, UnicodeError):
                raise GatewayError(400, '登录表单无效。') from None
            expected_fields = {'csrf', 'password'} if path == '/share/login' else ({'csrf', 'invite'} if path == '/share/redeem' else {'csrf'})
            if set(values) != expected_fields or any(len(v) != 1 for v in values.values()):
                raise GatewayError(400, '登录表单无效。')
            self.state.verify_csrf(values['csrf'][0], login_csrf, origin)
            if path == '/share/logout':
                if not visitor:
                    raise GatewayError(401, '登录已失效。')
                self.state.logout(token)
                result_token, age, location = '', 0, '/share/login'
                remembered = []
            else:
                if path == '/share/redeem':
                    result_token, browser_token = self.state.redeem(values['invite'][0], browser_token)
                else:
                    result_token, browser_token = self.state.login_with_browser(values['password'][0], token, browser_token)
                age, location = self.state.config['session_ttl_seconds'], '/'
                remembered = [('Set-Cookie', cookie_header(browser_name, browser_token, secure, 7 * 24 * 60 * 60))]
            return self.send(303, '', [('Location', location), ('Set-Cookie', cookie_header(auth_name, result_token, secure, age)),
                ('Set-Cookie', cookie_header(csrf_name, '', secure, 0))] + remembered)
        if not visitor:
            if self.command == 'GET':
                return self.send(303, '', [('Location', '/share/login')])
            message = '请先打开完整邀请链接并点击进入体验。' if invite_mode else '请先打开分享链接并输入访问口令。'
            raise GatewayError(401, message)
        with self.state.admission(visitor, path):
            return self.proxy_route(visitor, parsed)

    def proxy_route(self, visitor, parsed):
        path = parsed.path
        data = None
        if self.command == 'GET':
            if DOWNLOAD.fullmatch(path):
                values = parse_qs(parsed.query, keep_blank_values=True)
                if set(values) != {'session'} or len(values['session']) != 1:
                    raise GatewayError(400, '下载链接无效。')
                self.state.own_profile(visitor, values['session'][0])
            elif path not in ('/', '/health', '/presentation') or parsed.query:
                raise GatewayError(404, '页面不存在。')
        elif self.command == 'POST':
            if path not in POST_ROUTES or parsed.query:
                raise GatewayError(404, '接口不存在。')
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                raise GatewayError(400, '请求格式无效。')
            data = self.read_body(MAX_BODY)
            try:
                body = json.loads(data)
            except (ValueError, UnicodeError):
                raise GatewayError(400, '请求格式无效。') from None
            if not isinstance(body, dict):
                raise GatewayError(400, '请求格式无效。')
            self.state.own_profile(visitor, body.get('session'), register=True)
        status, headers, payload = fetch_upstream(self.state.config, self.command, self.path, data,
            self.headers.get('X-CSRF-Token', ''))
        if self.command == 'GET' and path == '/' and status == 200 and headers.get('Content-Type', '').startswith('text/html'):
            bootstrap = self.state.restore_bootstrap(visitor)
            if bootstrap:
                if b'<script>' not in payload:
                    raise GatewayError(502, '暂时无法恢复页面档案，请联系分享者。')
                payload = payload.replace(b'<script>', bootstrap + b'<script>', 1)
            manage_path = '/share/login?manage=1' if self.state.config.get('auth_mode') == 'invite' else '/share/login'
            payload = payload.replace('● 本地求职档案</span>'.encode('utf-8'),
                ('● 临时体验档案</span><a href="' + manage_path + '" style="font-size:12px;white-space:nowrap">退出体验</a>').encode('utf-8'), 1)
        self.send(status, payload, headers)

    def handle_request(self):
        try:
            self._dispatch()
        except GatewayError as error:
            self.send(error.status, {'error': error.message})
        except (socket.timeout, TimeoutError):
            self.send(408, {'error': '请求读取超时，请重新操作。'})
        except Exception:
            self.send(500, {'error': '分享入口暂时无法处理请求，请稍后重试。'})

    do_GET = handle_request
    do_POST = handle_request


class ShareServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 24

    def __init__(self, config):
        self.share_state = ShareState(config)
        self.request_slots = threading.BoundedSemaphore(24)
        super().__init__(('127.0.0.1', config['port']), ShareHandler)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                payload = b'{"error":"Share server is busy. Please retry later."}'
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
            except OSError:
                pass
            finally:
                self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    server = ShareServer(load_config(args.config))
    print('Job Coach temporary share gateway: http://127.0.0.1:8513/share/login', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
