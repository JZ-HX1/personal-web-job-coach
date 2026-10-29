"""A fresh one-browser invitation restores only host-selected owned profiles."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import share_gateway as gateway
import share_host as host
import test_share_gateway as harness


class RestoreTests(unittest.TestCase):
    request = harness.GatewayTests.request
    form = harness.InvitationTests.form
    redeem = harness.InvitationTests.redeem
    tearDown = harness.GatewayTests.tearDown

    def setUp(self):
        harness.InvitationTests.setUp(self)
        self.real = '1' * 32
        self.demo = '2' * 32
        self.older_demo = '3' * 32
        self.config.update(max_profiles_total=4,
            restore_profiles=[self.real, self.demo, self.older_demo],
            restore_selection={'real_session': self.real, 'demo_session': self.demo, 'mode': 'demo'})
        self.state = gateway.ShareState(self.config, self.clock)
        self.server.share_state = self.state

    def login(self):
        csrf, cookies, _ = self.form()
        status, headers, _, _ = self.redeem(csrf, cookies)
        self.assertEqual(status, 303)
        auth = headers.get_all('Set-Cookie')[0].split(';', 1)[0]
        return auth, cookies

    def test_invitation_preview_does_not_expose_or_claim_restored_ids(self):
        for route in ('/share/login', '/'):
            status, _, body, _ = self.request('GET', route)
            self.assertIn(status, (200, 303))
            for sid in self.config['restore_profiles']:
                self.assertNotIn(sid.encode(), body)
        self.assertEqual(self.state.data['visitors'], {})
        self.assertEqual(self.state.data['owners'], {})
        self.assertFalse(self.state.path.exists())

    def test_first_redemption_atomically_binds_all_restored_profiles(self):
        auth, _ = self.login()
        visitor = self.state.authenticate(auth.split('=', 1)[1])
        self.assertEqual(self.state.data['owners'], {sid: visitor for sid in self.config['restore_profiles']})
        reloaded = gateway.ShareState(self.config, self.clock)
        self.assertEqual(reloaded.data, self.state.data)
        for sid in self.config['restore_profiles']:
            reloaded.own_profile(visitor, sid)
        with patch.object(gateway, 'fetch_upstream', return_value=(200, {'Content-Type': 'application/json'}, b'{}')):
            self.assertEqual(self.request('POST', '/api/state', {'session': self.real}, cookie=auth)[0], 200)
            self.assertEqual(self.request('GET', '/downloads/recommendations_' + 'a' * 32 + '.xlsx?session=' + self.demo, cookie=auth)[0], 200)

    def test_failed_state_save_does_not_consume_invitation_or_assign_profiles(self):
        with patch.object(self.state, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.state.redeem(self.invite, 'a' * 43)
        self.assertEqual(self.state.data['visitors'], {})
        self.assertEqual(self.state.data['owners'], {})
        token, _ = self.state.redeem(self.invite, 'b' * 43)
        self.assertIsNotNone(self.state.authenticate(token))

    def test_owner_can_resume_but_second_browser_cannot_take_restored_records(self):
        auth, original_cookies = self.login()
        token = auth.split('=', 1)[1]
        visitor = self.state.authenticate(token)
        before = dict(self.state.data['owners'])
        self.state.logout(token)
        remembered = original_cookies.split('; ', 1)[1]
        csrf, cookies, _ = self.form(cookie=remembered)
        status, headers, _, _ = self.redeem(csrf, cookies, '')
        self.assertEqual(status, 303)
        new_token = headers.get_all('Set-Cookie')[0].split(';', 1)[0].split('=', 1)[1]
        self.assertEqual(self.state.authenticate(new_token), visitor)
        self.assertEqual(self.state.data['owners'], before)
        csrf_other, other_cookies, _ = self.form()
        self.assertEqual(self.redeem(csrf_other, other_cookies)[0], 403)
        self.assertEqual(self.state.data['owners'], before)

    def test_only_authenticated_app_html_receives_bootstrap_before_app_script(self):
        auth, _ = self.login()
        app = b'<html><body><script>const APP_START=true;</script></body></html>'
        with patch.object(gateway, 'fetch_upstream', return_value=(200, {'Content-Type': 'text/html; charset=utf-8'}, app)):
            status, _, body, _ = self.request('GET', '/', cookie=auth)
            self.assertEqual(status, 200)
            self.assertEqual(body.count(b'<script data-share-restore>'), 1)
            self.assertLess(body.index(b'data-share-restore'), body.index(b'const APP_START'))
            self.assertIn(self.real.encode(), body)
            self.assertIn(self.demo.encode(), body)
            self.assertNotIn(self.older_demo.encode(), body)
            self.assertIn(b'localStorage.getItem(marker)', body)
            self.assertIn(b'job_coach_share_restore_v1', body)
            self.assertIn(b'job_coach_mode_v1', body)
            self.assertNotIn(self.invite.encode(), body)
            self.assertNotIn(b'data-share-restore', self.request('GET', '/')[2])
        with self.assertRaises(gateway.GatewayError):
            self.state.restore_bootstrap('f' * 32)

    def test_restore_configuration_rejects_untrusted_ids_selection_and_quota(self):
        valid = copy.deepcopy(self.config)
        cases = []
        for value in ('../private', 'x' * 32, 'a' * 31, '<script>alert(1)</script>', 123):
            invalid = copy.deepcopy(valid)
            invalid['restore_profiles'][0] = value
            cases.append(invalid)
        for updates in (
            {'restore_profiles': [self.real, self.real]},
            {'restore_profiles': [str(i) * 32 for i in range(5)]},
            {'restore_profiles': []},
            {'restore_profiles': {'real': self.real}},
            {'restore_selection': {'real_session': 'f' * 32, 'mode': 'real'}},
            {'restore_selection': {'real_session': self.real, 'demo_session': self.real, 'mode': 'demo'}},
            {'restore_selection': {'real_session': self.real, 'mode': 'demo'}},
            {'restore_selection': {'real_session': self.real, 'mode': 'real', 'script': 'alert(1)'}},
            {'restore_selection': {'real_session': self.real, 'mode': 'other'}},
            {'max_profiles_total': 2},
            {'max_visitors': 2},
            {'auth_mode': 'password'},
        ):
            cases.append(valid | updates)
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ValueError):
                gateway.validate_restore_config(config)
        path = Path(self.temp.name) / 'valid-config.json'
        path.write_text(json.dumps(valid), encoding='utf-8')
        self.assertEqual(gateway.load_config(path)['restore_profiles'], valid['restore_profiles'])

    def test_existing_bound_state_cannot_be_reconfigured_to_claim_other_profiles(self):
        self.login()
        changed = copy.deepcopy(self.config)
        changed['restore_profiles'].append('4' * 32)
        with self.assertRaisesRegex(ValueError, 'refusing to reassign'):
            gateway.ShareState(changed, self.clock)


class ResumeExpiryTests(unittest.TestCase):
    def test_resume_preserves_previous_expiry_and_never_extends_six_hours(self):
        start = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        previous = datetime(2026, 10, 28, 16, 30, 10, tzinfo=timezone.utc)
        self.assertEqual(host.share_expiry(start, previous.isoformat()), previous)
        self.assertEqual(host.share_expiry(start, '2026-10-29T00:30:10+08:00'), previous)
        self.assertEqual(host.share_expiry(start, (start + timedelta(days=1)).isoformat()), start + timedelta(hours=6))
        self.assertEqual(host.share_expiry(start), start + timedelta(hours=6))

    def test_resume_rejects_expired_naive_and_invalid_values(self):
        start = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        for value in ('2026-10-28T11:59:59Z', '2026-10-28T12:00:00Z', '2026-10-29T00:30:10',
                      '2026-10-29', 'not a time', 123, {}, 'x' * 81):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                host.share_expiry(start, value)


if __name__ == '__main__':
    unittest.main()
