"""In-memory HTTP and concurrency tests; these tests never bind a listening port."""
import hashlib
from email import message_from_bytes
import io
import json
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import share_gateway as gateway


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now


class MemorySocket:
    def __init__(self, raw):
        self.input = io.BytesIO(raw)
        self.output = bytearray()
        self.timeout = None

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, value):
        self.timeout = value


class GatewayTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'runtime/test-temp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.clock = Clock()
        self.password = 'class-demo-Strong-Example'
        salt = bytes.fromhex('12' * 16)
        self.config = {'password_salt_hex': salt.hex(),
            'password_hash_hex': hashlib.pbkdf2_hmac('sha256', self.password.encode(), salt, 200000).hex(),
            'password_iterations': 200000, 'secret_hex': 'ab' * 32,
            'state_path': str(Path(self.temp.name) / 'state.json'), 'session_ttl_seconds': 21600,
            'max_visitors': 20, 'max_profiles_per_visitor': 4, 'max_profiles_total': 60,
            'upstream_port': 8512, 'port': 8513}
        self.state = gateway.ShareState(self.config, self.clock)
        self.server = SimpleNamespace(share_state=self.state, server_name='localhost', server_port=8513)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, path, body=None, cookie=None, host='127.0.0.1:8513', headers=None):
        values = {'Host': host}
        if cookie:
            values['Cookie'] = cookie
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            values['Content-Type'] = 'application/json'
        elif isinstance(body, str):
            body = body.encode()
        if body is not None:
            values['Content-Length'] = str(len(body))
        values.update(headers or {})
        raw = (method + ' ' + path + ' HTTP/1.0\r\n' + ''.join(k + ': ' + v + '\r\n' for k, v in values.items()) + '\r\n').encode() + (body or b'')
        sock = MemorySocket(raw)
        gateway.ShareHandler(sock, ('127.0.0.1', 12345), self.server)
        head, payload = bytes(sock.output).split(b'\r\n\r\n', 1)
        status_line, fields = head.split(b'\r\n', 1)
        return int(status_line.split()[1]), message_from_bytes(fields), payload, sock

    def form(self, host='127.0.0.1:8513'):
        status, headers, body, _ = self.request('GET', '/share/login', host=host)
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', body.decode()).group(1)
        return csrf, headers['Set-Cookie'].split(';', 1)[0]

    def login(self, host='127.0.0.1:8513', password=None):
        csrf, csrf_cookie = self.form(host)
        secure = host.endswith('trycloudflare.com')
        origin = ('https://' if secure else 'http://') + host
        status, headers, _, _ = self.request('POST', '/share/login',
            body=urlencode({'password': password or self.password, 'csrf': csrf}),
            cookie=csrf_cookie, host=host, headers={'Origin': origin, 'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 303)
        return headers.get_all('Set-Cookie')[0].split(';', 1)[0]

    def test_all_original_routes_require_authentication(self):
        with patch.object(gateway, 'fetch_upstream') as upstream:
            for route in ['/', '/health', '/presentation', '/api/state', '/downloads/resume_a.md?session=' + 'a' * 32]:
                status, headers, _, _ = self.request('GET', route)
                self.assertEqual(status, 303)
                self.assertEqual(headers['Location'], '/share/login')
            self.assertEqual(self.request('POST', '/api/chat', {'session': 'a' * 32, 'prompt': 'hi'})[0], 401)
            upstream.assert_not_called()

    def test_login_needs_signed_cookie_csrf_and_correct_password(self):
        csrf, cookie = self.form()
        form = urlencode({'password': self.password, 'csrf': csrf})
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        self.assertEqual(self.request('POST', '/share/login', form, headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/share/login', urlencode({'password': 'wrong', 'csrf': csrf}), cookie=cookie, headers=headers)[0], 403)
        self.assertEqual(self.state.data['visitors'], {})
        self.clock.now += 601
        self.assertEqual(self.request('POST', '/share/login', form, cookie=cookie, headers=headers)[0], 403)

    def test_public_cookie_is_secure_httponly_and_strict(self):
        host = 'class-demo-123.trycloudflare.com'
        csrf, cookie = self.form(host)
        status, headers, _, _ = self.request('POST', '/share/login', urlencode({'password': self.password, 'csrf': csrf}),
            cookie=cookie, host=host, headers={'Origin': 'https://' + host, 'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 303)
        auth = headers.get_all('Set-Cookie')[0]
        self.assertTrue(auth.startswith('__Host-qcs_share_auth='))
        self.assertIn('; Secure', auth)
        self.assertIn('; HttpOnly', auth)
        self.assertIn('; SameSite=Strict', auth)
        self.assertNotIn('Domain=', auth)

    def test_host_and_origin_restrictions(self):
        for host in ['evil.example', 'a.b.trycloudflare.com', 'good.trycloudflare.com.evil.example', '127.0.0.1:11434']:
            self.assertEqual(self.request('GET', '/share/login', host=host)[0], 400)
        self.assertEqual(self.request('GET', '/share/login', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', 'http://evil.example/')[0], 400)

    def test_login_rate_limits_global_and_source(self):
        csrf, cookie = self.form()
        for _ in range(6):
            self.assertEqual(self.request('POST', '/share/login', urlencode({'password': 'wrong', 'csrf': csrf}),
                cookie=cookie, headers={'Content-Type': 'application/x-www-form-urlencoded'})[0], 403)
        self.assertEqual(self.request('POST', '/share/login', urlencode({'password': self.password, 'csrf': csrf}),
            cookie=cookie, headers={'Content-Type': 'application/x-www-form-urlencoded'})[0], 429)
        for index in range(13):
            self.state.rates.check(('login-global',), 20)
        with self.assertRaises(gateway.GatewayError) as error:
            self.state.rates.check(('login-global',), 20)
        self.assertEqual(error.exception.status, 429)

    def test_profile_and_download_ownership_is_bound_to_browser(self):
        a, b = self.login(), self.login()
        session = 'a' * 32
        with patch.object(gateway, 'fetch_upstream', return_value=(200, {'Content-Type': 'application/json'}, b'{}')) as upstream:
            self.assertEqual(self.request('POST', '/api/state', {'session': session}, cookie=a)[0], 200)
            self.assertEqual(self.request('POST', '/api/state', {'session': session}, cookie=b)[0], 403)
            download = '/downloads/resume_' + 'f' * 32 + '.md?session=' + session
            self.assertEqual(self.request('GET', download, cookie=b)[0], 403)
            self.assertEqual(self.request('GET', download, cookie=a)[0], 200)
            self.assertEqual(upstream.call_count, 2)

    def test_ownership_and_login_survive_gateway_restart(self):
        token = self.state.login(self.password)
        visitor = self.state.authenticate(token)
        self.state.own_profile(visitor, 'a' * 32, register=True)
        again = gateway.ShareState(self.config, self.clock)
        self.assertEqual(again.authenticate(token), visitor)
        again.own_profile(visitor, 'a' * 32)
        self.assertNotIn(token, Path(self.config['state_path']).read_text())

    def test_expired_and_logged_out_sessions_are_rejected(self):
        token = self.state.login(self.password)
        self.clock.now += self.config['session_ttl_seconds'] + 1
        self.assertIsNone(self.state.authenticate(token))
        token = self.state.login(self.password)
        self.state.logout(token)
        self.assertIsNone(self.state.authenticate(token))

    def test_logout_requires_csrf_and_shared_page_discloses_host_storage(self):
        cookie = self.login()
        status, headers, body, _ = self.request('GET', '/share/login', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('资料会保存在提供此链接的电脑中'.encode(), body)
        csrf = re.search(r'name="csrf" value="([^"]+)"', body.decode()).group(1)
        csrf_cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(self.request('POST', '/share/logout', urlencode({'csrf': csrf}), cookie=cookie,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})[0], 403)
        status, _, _, _ = self.request('POST', '/share/logout', urlencode({'csrf': csrf}), cookie=cookie + '; ' + csrf_cookie,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 303)
        self.assertIsNone(self.state.authenticate(cookie.split('=', 1)[1]))

    def test_visitor_and_profile_quotas_persist(self):
        self.config.update(max_visitors=2, max_profiles_per_visitor=1, max_profiles_total=2)
        a = self.state.authenticate(self.state.login(self.password))
        b = self.state.authenticate(self.state.login(self.password))
        self.state.own_profile(a, 'a' * 32, register=True)
        with self.assertRaises(gateway.GatewayError):
            self.state.own_profile(a, 'b' * 32, register=True)
        self.state.own_profile(b, 'b' * 32, register=True)
        with self.assertRaises(gateway.GatewayError):
            gateway.ShareState(self.config, self.clock).login(self.password)

    def test_remembered_browser_requires_password_and_restores_ownership(self):
        token, browser_token = self.state.login_with_browser(self.password)
        visitor = self.state.authenticate(token)
        self.state.own_profile(visitor, 'a' * 32, register=True)
        self.state.logout(token)
        self.assertIsNone(self.state.authenticate(browser_token))
        with self.assertRaises(gateway.GatewayError):
            self.state.login_with_browser('wrong', browser_token=browser_token)
        again, remembered = self.state.login_with_browser(self.password, browser_token=browser_token)
        self.assertEqual(remembered, browser_token)
        self.assertEqual(self.state.authenticate(again), visitor)
        self.state.own_profile(visitor, 'a' * 32)
        self.assertEqual(len(self.state.data['visitors']), 1)
        self.clock.now += self.config['session_ttl_seconds'] + 1
        renewed, _ = self.state.login_with_browser(self.password, browser_token=browser_token)
        self.assertEqual(self.state.authenticate(renewed), visitor)

    def test_api_and_chat_rate_limits(self):
        for _ in range(6):
            with self.state.admission('a', '/api/chat'):
                pass
        with self.assertRaises(gateway.GatewayError):
            with self.state.admission('a', '/api/chat'):
                pass
        self.clock.now += 61
        for _ in range(60):
            with self.state.admission('a', '/api/state'):
                pass
        with self.assertRaises(gateway.GatewayError):
            with self.state.admission('a', '/api/state'):
                pass

    def test_global_chat_slot_is_held_until_upstream_returns(self):
        cookie = self.login()
        entered, release = threading.Event(), threading.Event()
        responses = []

        def delayed(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return 200, {}, b'{}'

        body = {'session': 'a' * 32, 'prompt': 'hello'}
        with patch.object(gateway, 'fetch_upstream', side_effect=delayed):
            thread = threading.Thread(target=lambda: responses.append(self.request('POST', '/api/chat', body, cookie=cookie)[0]))
            thread.start()
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(self.request('POST', '/api/chat', body, cookie=cookie)[0], 429)
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(responses, [200])
        with patch.object(gateway, 'fetch_upstream', side_effect=gateway.GatewayError(502, 'upstream unavailable')):
            self.assertEqual(self.request('POST', '/api/chat', body, cookie=cookie)[0], 502)
        with patch.object(gateway, 'fetch_upstream', return_value=(200, {}, b'{}')):
            self.assertEqual(self.request('POST', '/api/chat', body, cookie=cookie)[0], 200)

    def test_request_limits_and_timeout_before_forwarding(self):
        cookie = self.login()
        with patch.object(gateway, 'fetch_upstream') as upstream:
            response = self.request('POST', '/api/upload', b'{}', cookie=cookie,
                headers={'Content-Length': str(gateway.MAX_BODY + 1), 'Content-Type': 'application/json'})
            self.assertEqual(response[0], 413)
            self.assertEqual(response[3].timeout, 10)
            self.assertEqual(self.request('POST', '/api/state', b'{}', cookie=cookie,
                headers={'Transfer-Encoding': 'chunked', 'Content-Type': 'application/json'})[0], 400)
            upstream.assert_not_called()

    def test_response_limit_and_header_allowlist(self):
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: {'Content-Length': str(gateway.MAX_RESPONSE + 1)}.get(name, default)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(gateway.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(gateway.GatewayError):
                gateway.fetch_upstream(self.config, 'GET', '/', None, '')
        connection.close.assert_called_once()
        response.getheader.side_effect = lambda name, default=None: {'Content-Length': '2', 'Content-Type': 'application/json', 'Set-Cookie': 'evil=1'}.get(name, default)
        response.read.return_value = b'{}'
        with patch.object(gateway.http.client, 'HTTPConnection', return_value=connection):
            _, headers, _ = gateway.fetch_upstream(self.config, 'POST', '/api/state', b'{}', 'a' * 64)
        self.assertNotIn('Set-Cookie', headers)
        forwarded = connection.request.call_args.kwargs['headers']
        self.assertNotIn('Cookie', forwarded)
        self.assertNotIn('CF-Connecting-IP', forwarded)

    def test_http_concurrency_is_bounded_without_spawning_services(self):
        server = gateway.ShareServer.__new__(gateway.ShareServer)
        server.request_slots = threading.BoundedSemaphore(24)
        server.close_request = Mock()
        for _ in range(24):
            self.assertTrue(server.request_slots.acquire(blocking=False))
        self.assertFalse(server.request_slots.acquire(blocking=False))
        request = MemorySocket(b'')
        server.process_request(request, ('127.0.0.1', 12345))
        self.assertIn(b'503 Service Unavailable', request.output)
        server.close_request.assert_called_once_with(request)


class InvitationTests(unittest.TestCase):
    request = GatewayTests.request
    tearDown = GatewayTests.tearDown

    def setUp(self):
        GatewayTests.setUp(self)
        self.invite = 'i' * 43
        self.config.update(auth_mode='invite', max_visitors=1,
            invite_hash_hex=hashlib.sha256(self.invite.encode()).hexdigest())
        for key in ('password_salt_hex', 'password_hash_hex', 'password_iterations'):
            self.config.pop(key, None)
        self.state = gateway.ShareState(self.config, self.clock)
        self.server.share_state = self.state

    def form(self, cookie=None, host='127.0.0.1:8513'):
        status, headers, body, _ = self.request('GET', '/share/login', cookie=cookie, host=host)
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', body.decode()).group(1)
        cookies = '; '.join(value.split(';', 1)[0] for value in headers.get_all('Set-Cookie'))
        return csrf, cookies, body

    def redeem(self, csrf, cookies, invite=None, host='127.0.0.1:8513'):
        return self.request('POST', '/share/redeem', urlencode({'csrf': csrf, 'invite': self.invite if invite is None else invite}),
            cookie=cookies, host=host, headers={'Content-Type': 'application/x-www-form-urlencoded'})

    def test_invite_config_has_no_password_dependency(self):
        path = Path(self.temp.name) / 'config.json'
        path.write_text(json.dumps(self.config))
        loaded = gateway.load_config(path)
        self.assertEqual(loaded['auth_mode'], 'invite')
        self.assertNotIn('password_hash_hex', loaded)

    def test_native_login_form_keeps_same_origin_and_rejects_null_origin(self):
        # Browsers can send Origin:null for native POST under no-referrer.
        # Keep a usable same-origin policy instead of weakening Origin validation.
        _, headers, _, _ = self.request('GET', '/share/login')
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        csrf, cookies, _ = self.form()
        body = urlencode({'csrf': csrf, 'invite': self.invite})
        for origin, expected in [('null', 403), ('https://evil.example', 403), ('http://127.0.0.1:8513', 303)]:
            status, _, _, _ = self.request('POST', '/share/redeem', body, cookie=cookies,
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': origin})
            self.assertEqual(status, expected)

    def test_get_does_not_consume_invitation_or_submit_automatically(self):
        _, _, body = self.form()
        self.form()
        self.assertEqual(self.state.data['visitors'], {})
        page = body.decode()
        self.assertNotIn(self.invite, page)
        self.assertNotIn('type="password"', page)
        self.assertIn('history.replaceState', page)
        self.assertIn('event.isTrusted', page)
        self.assertIn('id="share-enter" disabled', page)
        self.assertNotIn('.submit()', page)

    def test_missing_invitation_or_browser_or_csrf_is_rejected(self):
        csrf, cookies, _ = self.form()
        self.assertEqual(self.redeem(csrf, cookies, '')[0], 403)
        csrf_cookie = cookies.split('; ', 1)[0]
        self.assertEqual(self.redeem(csrf, csrf_cookie)[0], 403)
        self.assertEqual(self.redeem('invalid', cookies)[0], 403)
        self.assertEqual(self.state.data['visitors'], {})

    def test_second_browser_cannot_reuse_first_invitation(self):
        csrf_a, cookie_a, _ = self.form()
        status, headers, _, _ = self.redeem(csrf_a, cookie_a)
        self.assertEqual(status, 303)
        auth_a = headers.get_all('Set-Cookie')[0].split(';', 1)[0]
        self.assertEqual(self.request('GET', '/share/login', cookie=auth_a)[0], 303)
        csrf_b, cookie_b, _ = self.form()
        self.assertEqual(self.redeem(csrf_b, cookie_b)[0], 403)
        self.assertEqual(len(self.state.data['visitors']), 1)
        reloaded = gateway.ShareState(self.config, self.clock)
        second_browser = cookie_b.split('qcs_share_browser=', 1)[1]
        with self.assertRaises(gateway.GatewayError):
            reloaded.redeem(self.invite, second_browser)

    def test_same_browser_can_resume_after_logout_without_transferring_slot(self):
        csrf, cookies, _ = self.form()
        _, headers, _, _ = self.redeem(csrf, cookies)
        auth = headers.get_all('Set-Cookie')[0].split(';', 1)[0]
        token = auth.split('=', 1)[1]
        owner = self.state.authenticate(token)
        self.state.own_profile(owner, 'a' * 32, register=True)
        self.state.logout(token)
        browser_cookie = cookies.split('; ', 1)[1]
        self.clock.now += self.config['session_ttl_seconds'] + 1
        csrf, renewed_cookies, page = self.form(cookie=browser_cookie)
        self.assertIn('继续体验'.encode(), page)
        status, headers, _, _ = self.redeem(csrf, renewed_cookies, '')
        self.assertEqual(status, 303)
        renewed_token = headers.get_all('Set-Cookie')[0].split(';', 1)[0].split('=', 1)[1]
        self.assertEqual(self.state.authenticate(renewed_token), owner)
        self.assertEqual(self.state.data['owners']['a' * 32], owner)
        self.assertEqual(len(self.state.data['visitors']), 1)

    def test_atomic_first_redemption_allows_only_one_browser(self):
        barrier = threading.Barrier(3)
        results = []

        def compete(browser):
            barrier.wait()
            try:
                self.state.redeem(self.invite, browser)
                results.append(200)
            except gateway.GatewayError as error:
                results.append(error.status)

        threads = [threading.Thread(target=compete, args=(letter * 43,)) for letter in ('a', 'b')]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(3)
        self.assertEqual(sorted(results), [200, 403])
        self.assertEqual(len(self.state.data['visitors']), 1)

    def test_invite_public_cookies_are_all_secure_host_cookies(self):
        host = 'one-student-demo.trycloudflare.com'
        csrf, cookies, _ = self.form(host=host)
        status, headers, _, _ = self.redeem(csrf, cookies, host=host)
        self.assertEqual(status, 303)
        for cookie in headers.get_all('Set-Cookie'):
            self.assertTrue(cookie.startswith('__Host-'))
            self.assertIn('; Secure', cookie)
            self.assertIn('; HttpOnly', cookie)


if __name__ == '__main__':
    unittest.main()
