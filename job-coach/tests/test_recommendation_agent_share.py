"""Recommendation tool and temporary-share boundaries, using fictional local data."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import zipfile

from career.agent import dispatch, run_agent
from career.service import BusinessError
from share_server import ShareService
from test_agent import FakeClient, calls
import test_share_gateway as gateway_harness
from unittest.mock import patch


class RecommendationToolTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'runtime/test-temp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.service = ShareService(Path(self.temp.name) / 'data')
        self.session = 'a' * 32
        self.service.set_target(self.session, 'Java 后端实习', '实习')
        self.service.add_document(self.session, '课程项目', 'project', '我使用 Java、Spring 和 MySQL 编写课程管理接口，使用 Git 进行代码评审。')
        self.service.save_opportunity(self.session, '虚构甲公司', 'Java 实习', '要求 Java、Spring Boot 和 MySQL', deadline='2099-12-31')
        self.service.save_opportunity(self.session, '虚构乙公司', '前端实习', '要求 JavaScript 和 React', deadline='2099-12-31')

    def test_combined_recommendation_export_is_real_and_uses_same_limit(self):
        answer = run_agent(self.service, self.session, '推荐一个适合的岗位并导出Excel', FakeClient([
            calls(('recommend_jobs', {'limit': 1, 'export_format': 'xlsx'}))]))
        result = answer['trace'][0]['result']
        self.assertNotIn('error', result)
        self.assertEqual(len(result['recommendations']), 1)
        exported = result['export_result']
        self.assertEqual(exported['count'], 1)
        file = self.service.resolve_export(self.session, exported['files'][0]['name'])
        with zipfile.ZipFile(file) as book:
            self.assertIn('xl/workbook.xml', book.namelist())
            self.assertIsNone(book.testzip())
        self.assertIn('不代表录取概率', answer['answer'])
        self.assertIn('可下载', answer['answer'])
        self.assertFalse(self.service.list_applications(self.session)['applications'])
        with self.assertRaises(BusinessError):
            self.service.resolve_export('b' * 32, file.name)

    def test_model_cannot_create_jobs_or_fabricate_company_summary(self):
        denied = dispatch(self.service, self.session, 'save_opportunity', {'company': '模型虚构公司', 'role': '岗位', 'jd': 'Java'})
        self.assertIn('error', denied)
        answer = run_agent(self.service, self.session, '推荐岗位', FakeClient([
            calls(('recommend_jobs', {})), {'content': '保证被不存在的大厂录取，概率99%。'}]))
        self.assertNotIn('保证被', answer['answer'])
        self.assertNotIn('不存在的大厂', answer['answer'])
        self.assertIn('虚构甲公司', answer['answer'])
        self.assertEqual(len(self.service.list_opportunities(self.session)['opportunities']), 2)

    def test_recommendation_rejects_forged_identity_and_invalid_parameters(self):
        for name, args in [('recommend_jobs', {'session': 'b' * 32}),
                           ('recommend_jobs', {'limit': True}),
                           ('export_recommendations', {'format': 'exe'}),
                           ('skill_profile', {'sql': 'DELETE FROM profiles'})]:
            self.assertIn('error', dispatch(self.service, self.session, name, args))

    def test_share_job_quota_is_atomic_and_existing_jobs_can_be_updated(self):
        self.service.limits = type(self.service.limits)(max_opportunities_per_profile=3)
        def save(i):
            try:
                self.service.save_opportunity(self.session, f'虚构测试公司{i}', '实习', 'Java')
                return True
            except BusinessError:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(save, range(10))), 1)
        rows = self.service.list_opportunities(self.session)['opportunities']
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.service.save_opportunity(self.session, first['company'], first['role'], 'Java 和 Git', opportunity_id=first['id'])
        self.assertEqual(len(self.service.list_opportunities(self.session)['opportunities']), 3)

    def test_recommendation_export_obeys_existing_shared_export_budget(self):
        self.service.limits = type(self.service.limits)(max_exports_per_profile=1)
        first = self.service.export_recommendations(self.session, format='csv')
        second = dispatch(self.service, self.session, 'recommend_jobs', {'export_format': 'xlsx'})
        self.assertIn('error', second)
        self.assertEqual(len(self.service.dashboard(self.session)['exports']), 1)
        self.assertTrue(self.service.resolve_export(self.session, first['files'][0]['name']).is_file())


class RecommendationGatewayTests(unittest.TestCase):
    setUp = gateway_harness.GatewayTests.setUp
    tearDown = gateway_harness.GatewayTests.tearDown
    request = gateway_harness.GatewayTests.request

    def test_new_routes_and_xlsx_remain_owned_and_authenticated(self):
        with patch('share_gateway.fetch_upstream', return_value=(200, {}, b'{}')) as forward:
            self.assertEqual(self.request('POST', '/api/recommendations', {'session': 'a' * 32})[0], 401)
            token = self.state.login(self.password)
            cookie = 'qcs_share_auth=' + token
            for route in ('/api/skills/profile', '/api/opportunities/list', '/api/opportunities/save', '/api/recommendations', '/api/recommendations/export'):
                self.assertEqual(self.request('POST', route, {'session': 'a' * 32}, cookie=cookie)[0], 200)
            path = '/downloads/recommendations_' + 'c' * 32 + '.xlsx?session=' + 'a' * 32
            self.assertEqual(self.request('GET', path, cookie=cookie)[0], 200)
            other = 'qcs_share_auth=' + self.state.login(self.password)
            self.assertEqual(self.request('GET', path, cookie=other)[0], 403)
            self.assertEqual(self.request('POST', '/api/recommendations', {'session': 'a' * 32}, cookie=other)[0], 403)
            self.assertTrue(forward.called)


if __name__ == '__main__':
    unittest.main()
