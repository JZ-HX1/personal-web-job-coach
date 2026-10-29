"""分享源站配额测试；使用独立临时目录与假的 socket，不启动监听服务。"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from career.agent import dispatch
from career.service import BusinessError, Service
from share_server import (LimitedThreadingHTTPServer, PRIVATE_RUNTIME, ShareService,
                          create_share_server, local_model_config, make_share_handler,
                          runtime_from_environment, validate_share_root)


class ShareQuotaTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'runtime' / 'test-temp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = 'a' * 32
        self.other = 'b' * 32

    def service(self, **limits):
        return ShareService(self.root / uuid.uuid4().hex, limits=limits)

    def target(self, service, session=None):
        service.set_target(session or self.session, 'Python 后端实习生', '实习')

    def pending(self, service, company='公司', session=None, **kwargs):
        return service.prepare_application(session or self.session, company, 'Python 实习生', **kwargs)['pending']['id']

    def test_import_and_missing_runtime_fail_closed_before_server_import(self):
        before = sys.modules.get('server')
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, '显式设置'):
                create_share_server()
        self.assertIs(sys.modules.get('server'), before)
        for path in (PRIVATE_RUNTIME, PRIVATE_RUNTIME.parent, PRIVATE_RUNTIME / 'share'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, '重叠'):
                validate_share_root(path)

    def test_nonempty_private_directory_is_not_reused(self):
        private = self.root / 'old-private'
        private.mkdir()
        original = private / 'resume.txt'
        original.write_text('private experience', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '必须为空'):
            ShareService(private)
        self.assertEqual(original.read_text(encoding='utf-8'), 'private experience')
        self.assertFalse((private / 'career.sqlite3').exists())
        shared = self.service()
        self.target(shared)
        reopened = ShareService(shared.root)
        self.assertEqual(reopened.dashboard(self.session)['profile']['role'], 'Python 后端实习生')

    def test_local_model_config_does_not_read_env_file(self):
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('must not read files')):
            config = local_model_config({})
        self.assertEqual(config['MODEL_API_KEY'], 'ollama')
        self.assertEqual(config['MODEL_BASE_URL'], 'http://127.0.0.1:11434/v1')
        for values in ({'MODEL_BASE_URL': 'https://api.example.com/v1'},
                       {'MODEL_BASE_URL': 'http://user:pass@127.0.0.1:11434/v1'},
                       {'MODEL_BASE_URL': 'http://127.0.0.1:bad/v1'},
                       {'MODEL_NAME': 'test:cloud'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                local_model_config(values)

    def test_profile_and_lock_registry_limit_is_atomic(self):
        service = self.service(max_profiles=3)
        sessions = [uuid.uuid4().hex for _ in range(12)]
        def register(session):
            try:
                service.session_lock(session)
                return True
            except BusinessError:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(register, sessions))
        self.assertEqual(sum(results), 3)
        self.assertEqual(service._count('profiles'), 3)
        self.assertEqual(len(service._session_locks), 3)

    def test_document_number_and_text_budget_are_shared_by_all_paths(self):
        service = self.service(max_documents_per_profile=2, max_document_chars_per_profile=12)
        self.target(service)
        service.add_document(self.session, 'one', 'resume', 'Python')
        with self.assertRaisesRegex(BusinessError, '合计'):
            service.add_document(self.session, 'large', 'resume', 'Python Python')
        service._add_document(self.session, 'two', 'project', 'SQL', False)
        with self.assertRaisesRegex(BusinessError, '2 份'):
            service.add_document(self.session, 'three', 'jd', 'Git')
        self.assertEqual(len(service.dashboard(self.session)['documents']), 2)

    def test_plain_service_remains_unrestricted_by_share_limits(self):
        shared = self.service(max_documents_per_profile=1)
        ordinary = Service(self.root / 'ordinary')
        for service in (shared, ordinary):
            service.add_document(self.session, 'one', 'resume', 'Python')
        with self.assertRaises(BusinessError):
            shared.add_document(self.session, 'two', 'project', 'SQL')
        ordinary.add_document(self.session, 'two', 'project', 'SQL')
        self.assertEqual(len(ordinary.dashboard(self.session)['documents']), 2)

    def test_pending_active_and_history_limits_without_deletion(self):
        service = self.service(max_pending_per_profile=1, max_pending_history_per_profile=3)
        self.target(service)
        first = self.pending(service, '公司一')
        with self.assertRaisesRegex(BusinessError, '未处理'):
            self.pending(service, '公司二')
        with service.connect() as db:
            db.execute('UPDATE pending SET created=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), first))
        second = self.pending(service, '公司二')
        service.confirm_application(self.session, second, False)
        third = self.pending(service, '公司三')
        service.confirm_application(self.session, third, False)
        with self.assertRaisesRegex(BusinessError, '历史上限'):
            self.pending(service, '公司四')
        self.assertEqual(service._count('pending', self.session), 3)

    def test_application_quota_rechecked_at_confirmation(self):
        service = self.service(max_applications_per_profile=2)
        self.target(service)
        prepared = [self.pending(service, f'公司{i}') for i in range(3)]
        service.confirm_application(self.session, prepared[0], True)
        service.confirm_application(self.session, prepared[1], True)
        with self.assertRaisesRegex(BusinessError, '上限'):
            service.confirm_application(self.session, prepared[2], True)
        self.assertEqual(len(service.list_applications(self.session)['applications']), 2)
        service.confirm_application(self.session, prepared[2], False)
        existing = service.list_applications(self.session)['applications'][0]
        changed = service.prepare_application(self.session, existing['company'], existing['role'], status='面试', application_id=existing['id'])['pending']['id']
        self.assertEqual(service.confirm_application(self.session, changed, True)['record']['status'], '面试')

    def test_audit_history_is_bounded(self):
        service = self.service(max_audit_per_profile=1)
        self.target(service)
        first = self.pending(service)
        service.confirm_application(self.session, first, True)
        second = self.pending(service, '另一公司')
        with self.assertRaisesRegex(BusinessError, '历史'):
            service.confirm_application(self.session, second, True)
        self.assertEqual(service._count('audit', self.session), 1)

    def test_bundle_first_run_six_files_then_model_indirect_export_blocked(self):
        service = self.service(max_exports_per_profile=6)
        service.load_demo(self.session)
        first = service.export_materials(self.session)
        self.assertEqual(len(first['files']), 6)
        archive = next(f for f in first['files'] if f['name'].endswith('.zip'))
        with zipfile.ZipFile(service.resolve_export(self.session, archive['name'])) as bundle:
            self.assertIn('简历初稿.html', bundle.namelist())
            self.assertIn('投递记录表.csv', bundle.namelist())
        direct = dispatch(service, self.session, 'export_materials', {'kind': 'bundle'})
        self.assertIn('最多生成', direct['error'])
        indirect = dispatch(service, self.session, 'analyze_fit', {'export_kind': 'bundle'})
        self.assertIn('最多生成', indirect['error'])
        self.assertEqual(service._count('exports', self.session), 6)
        self.assertEqual(len(list(service.exports.iterdir())), 6)

    def test_export_preflight_is_all_or_nothing_and_global(self):
        service = self.service(max_exports_per_profile=7, max_exports_global=7)
        service.load_demo(self.session)
        service.export_materials(self.session)
        with self.assertRaises(BusinessError):
            service.export_materials(self.session, 'resume')
        self.assertEqual(service._count('exports'), 6)
        self.target(service, self.other)
        service.export_materials(self.other, 'applications')
        with self.assertRaisesRegex(BusinessError, '分享实例最多'):
            service.export_materials(self.session, 'applications')
        self.assertEqual(service._count('exports'), 7)

    def test_parallel_exports_cannot_exceed_global_limit(self):
        service = self.service(max_exports_per_profile=10, max_exports_global=3)
        self.target(service)
        def export(_):
            try:
                service.export_materials(self.session, 'applications')
                return True
            except BusinessError:
                return False
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(export, range(9)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(service._count('exports'), 3)

    def test_capacity_stops_new_data_without_removing_existing_files(self):
        service = self.service(max_storage_bytes=1024 * 1024, database_reserve_bytes=64 * 1024, export_reserve_bytes=128 * 1024)
        self.target(service)
        service.add_document(self.session, 'existing', 'resume', '真实材料 Python')
        filler = service.root / 'owned-test-filler.bin'
        size = service.limits.max_storage_bytes - service._storage_bytes() - service.limits.database_reserve_bytes + 1
        filler.write_bytes(b'x' * size)
        with self.assertRaisesRegex(BusinessError, '容量预算'):
            service.add_document(self.session, 'new', 'jd', 'Python')
        with self.assertRaisesRegex(BusinessError, '容量预算'):
            service.export_materials(self.session, 'applications')
        self.assertEqual(len(service.dashboard(self.session)['documents']), 1)
        self.assertEqual(filler.stat().st_size, size)
        self.assertEqual(service._count('exports'), 0)

    def test_sessions_cache_and_interview_history_limits(self):
        service = self.service(max_session_bytes=1024, max_cache_bytes=1024, max_questions_per_profile=10)
        service.load_demo(self.session)
        service.save_messages(self.session, [{'role': 'user', 'content': '保留'}])
        with self.assertRaisesRegex(BusinessError, '对话内容'):
            service.save_messages(self.session, [{'role': 'user', 'content': 'a' * 2000}])
        self.assertEqual(service.load_messages(self.session)[0]['content'], '保留')
        service._cache(self.session, 'match', {'test': 'keep'})
        with self.assertRaisesRegex(BusinessError, '缓存预算'):
            service._cache(self.session, 'match', {'test': 'a' * 2000})
        self.assertEqual(service._cached(self.session, 'match'), {'test': 'keep'})
        service.start_interview(self.session)
        profile = service.dashboard(self.session)['profile']
        service.set_target(self.session, profile['role'], profile['stage'], '其他城市', profile['background'])
        with self.assertRaisesRegex(BusinessError, '历史面试题'):
            service.start_interview(self.session)
        self.assertEqual(service._count('questions', self.session), 10)


class ShareHTTPBoundsTests(unittest.TestCase):
    def test_chat_slot_remains_held_until_origin_handler_finishes(self):
        entered, finish = threading.Event(), threading.Event()
        backend = type('Backend', (), {'chat_slot': threading.BoundedSemaphore(1)})()
        class BaseHandler:
            def __init__(self, server, path):
                self.server, self.path, self.responses = server, path, []
            def do_POST(self):
                entered.set()
                finish.wait(2)
                return 'complete'
            def send(self, data, status=200):
                self.responses.append((status, data))
        Handler = make_share_handler(BaseHandler)
        first, second = Handler(backend, '/api/chat'), Handler(backend, '/api/chat')
        thread = threading.Thread(target=first.do_POST)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            second.do_POST()
            self.assertEqual(second.responses[0][0], 429)
            self.assertFalse(backend.chat_slot.acquire(blocking=False))
        finally:
            finish.set()
            thread.join(2)
        self.assertTrue(backend.chat_slot.acquire(blocking=False))
        backend.chat_slot.release()

    def test_request_cap_timeout_and_slot_release_without_listening(self):
        server = LimitedThreadingHTTPServer.__new__(LimitedThreadingHTTPServer)
        server._request_slots = threading.BoundedSemaphore(1)
        closed = []
        server.shutdown_request = lambda request: closed.append(request)
        class Socket:
            def __init__(self):
                self.sent = b''
                self.timeout = None
            def sendall(self, data):
                self.sent += data
            def settimeout(self, value):
                self.timeout = value
        first, second = Socket(), Socket()
        with mock.patch.object(ThreadingHTTPServer, 'process_request', return_value=None):
            server.process_request(first, ('127.0.0.1', 1234))
            server.process_request(second, ('127.0.0.1', 1235))
        self.assertIn(b'503 Service Unavailable', second.sent)
        self.assertEqual(closed, [second])
        with mock.patch.object(ThreadingHTTPServer, 'process_request_thread', side_effect=RuntimeError('handler exit')):
            with self.assertRaises(RuntimeError):
                server.process_request_thread(first, ('127.0.0.1', 1234))
        self.assertTrue(server._request_slots.acquire(blocking=False))
        server._request_slots.release()
        with mock.patch.object(ThreadingHTTPServer, 'get_request', return_value=(first, ('127.0.0.1', 1234))):
            server.get_request()
        self.assertEqual(first.timeout, 10)

    def test_never_binds_public_interface(self):
        with self.assertRaisesRegex(ValueError, '本机监听'):
            LimitedThreadingHTTPServer(('0.0.0.0', 8512), object, bind_and_activate=False)


if __name__ == '__main__':
    unittest.main()
