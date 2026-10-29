"""工具权限、虚假完成修复、流程门禁与真实结果输出。"""
import json
import tempfile
import unittest
from pathlib import Path
from career.agent import ModelError, dispatch, run_agent
from career.service import Service


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages, tools):
        self.requests.append(json.loads(json.dumps({'messages': messages, 'tools': tools})))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def calls(*pairs):
    return {'content': '', 'tool_calls': [{'id': f'call_{i}', 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(pairs)]}


class AgentTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'runtime' / 'test-temp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.service = Service(self.temp.name)
        self.session = 'a' * 32

    def tearDown(self):
        self.temp.cleanup()

    def demo(self):
        self.service.load_demo(self.session)

    def test_first_turn_asks_goal_without_side_effects(self):
        client = FakeClient([{'content': '欢迎！'}])
        result = run_agent(self.service, self.session, '你好', client)
        self.assertIn('什么岗位', result['answer'])
        self.assertEqual(result['trace'], [])
        self.assertEqual([t['function']['name'] for t in client.requests[0]['tools']], ['set_target'])

    def test_goal_gate_cannot_be_bypassed_by_model(self):
        client = FakeClient([calls(('export_materials', {})), {'content': '请补充岗位'}])
        result = run_agent(self.service, self.session, '生成材料', client)
        self.assertIn('先', result['trace'][0]['result']['error'])
        self.assertEqual(self.service.dashboard(self.session)['exports'], [])

    def test_whitelist_blocks_confirmation(self):
        result = dispatch(self.service, self.session, 'confirm_application', {'pending_id': 'x', 'accept': True})
        self.assertIn('不允许', result['error'])

    def test_extra_fields_and_wrong_types_do_not_mutate(self):
        self.demo()
        for name, args in [('analyze_fit', {'session': 'b' * 32}), ('start_interview', {'count': True}), ('start_interview', {'count': 100})]:
            self.assertIn('error', dispatch(self.service, self.session, name, args))

    def test_real_results_override_fabricated_model_summary(self):
        self.demo()
        client = FakeClient([calls(('analyze_fit', {})), {'content': '匹配率100%，你在大公司实习了两年。'}])
        result = run_agent(self.service, self.session, '分析匹配', client)
        self.assertIn('材料显式技能覆盖率', result['answer'])
        self.assertNotIn('实习了两年', result['answer'])
        self.assertIn('不代表录用概率', result['answer'])

    def test_multistep_tools_create_actual_files(self):
        self.demo()
        client = FakeClient([calls(('analyze_fit', {})), calls(('export_materials', {'kind': 'bundle'})), {'content': '好了'}])
        result = run_agent(self.service, self.session, '分析后导出', client)
        self.assertEqual([x['tool'] for x in result['trace']], ['analyze_fit', 'export_materials'])
        for f in result['trace'][-1]['result']['files']:
            self.assertTrue(self.service.resolve_export(self.session, f['name']).is_file())

    def test_combined_analysis_and_export_runs_real_operations(self):
        self.demo()
        result = run_agent(self.service, self.session, '分析并导出', FakeClient([
            calls(('analyze_fit', {'export_kind': 'bundle'}))]))
        data = result['trace'][0]['result']
        self.assertEqual(data['score'], 62.5)
        self.assertEqual(len(data['export_result']['files']), 6)
        self.assertIn('完整求职材料包', result['answer'])

    def test_answer_grade_is_bound_to_user_not_model_text(self):
        self.demo()
        self.service.start_interview(self.session)
        answer = '项目中我负责接口开发，编写测试验证功能，最终完成本地演示，后续改进部署。'
        result = run_agent(self.service, self.session, answer, FakeClient([
            calls(('review_answer', {'question_id': 'Q1', 'answer': '模型编造了完全不同的内容'}))]))
        self.assertEqual(result['trace'][0]['result']['answer'], answer)

    def test_false_completion_requires_actual_tool(self):
        self.demo()
        client = FakeClient([{'content': '已生成材料包。'}, calls(('export_materials', {'kind': 'applications'})), {'content': '完成'}])
        result = run_agent(self.service, self.session, '导出投递表', client)
        self.assertEqual(result['trace'][0]['tool'], 'export_materials')
        self.assertFalse(any(m.get('role') == 'system' for m in self.service.load_messages(self.session)))

    def test_repeated_false_completion_is_not_saved_as_success(self):
        self.demo()
        client = FakeClient([{'content': '已导出文件。'}, {'content': '已导出文件。'}])
        result = run_agent(self.service, self.session, '导出', client)
        self.assertIn('还没有执行', result['answer'])
        self.assertEqual(self.service.dashboard(self.session)['exports'], [])

    def test_pending_record_stops_model_and_requires_user(self):
        self.demo()
        client = FakeClient([calls(('prepare_application', {'company': '示例公司', 'role': 'Python实习生'}))])
        result = run_agent(self.service, self.session, '记录岗位', client)
        self.assertEqual(result['trace'][0]['result']['status'], 'awaiting_user_confirmation')
        self.assertEqual(self.service.list_applications(self.session)['applications'], [])

    def test_duplicate_call_ids_rejected_before_execution(self):
        self.demo()
        reply = calls(('export_materials', {}), ('export_materials', {}))
        reply['tool_calls'][1]['id'] = reply['tool_calls'][0]['id']
        result = run_agent(self.service, self.session, '导出', FakeClient([reply]))
        self.assertEqual(result['trace'], [])
        self.assertEqual(self.service.dashboard(self.session)['exports'], [])

    def test_model_failure_keeps_executed_results(self):
        self.demo()
        result = run_agent(self.service, self.session, '分析', FakeClient([calls(('analyze_fit', {})), ModelError('连接失败')]))
        self.assertIn('连接失败', result['answer'])
        self.assertIn('覆盖率', result['answer'])

    def test_history_keeps_paired_calls_and_results(self):
        self.demo()
        run_agent(self.service, self.session, '分析', FakeClient([calls(('analyze_fit', {})), {'content': 'ok'}]))
        history = self.service.load_messages(self.session)
        call_ids = {c['id'] for m in history for c in m.get('tool_calls', [])}
        result_ids = {m['tool_call_id'] for m in history if m.get('role') == 'tool'}
        self.assertEqual(call_ids, result_ids)

    def test_followup_after_one_turn_and_short_question_ids(self):
        self.demo()
        run_agent(self.service, self.session, '面试', FakeClient([calls(('start_interview', {'count': 10})), {'content': 'ok'}]))
        result = run_agent(self.service, self.session, '回答第一题', FakeClient([
            calls(('review_answer', {'question_id': 'Q1', 'content': '项目中我负责接口开发，编写测试验证功能，最终完成本地演示，后续改进部署。'})), {'content': 'ok'}]))
        self.assertNotIn('error', result['trace'][0]['result'])
        self.assertEqual(len(self.service.dashboard(self.session)['interview']['answers']), 1)

    def test_redundant_context_never_overrides_owned_materials(self):
        self.demo()
        result = dispatch(self.service, self.session, 'analyze_fit', {'role': 'CEO', 'material_ids': ['foreign']})
        self.assertNotIn('error', result)
        self.assertTrue(result['argument_adjustments'])
        self.assertNotEqual(self.service.dashboard(self.session)['profile']['role'], 'CEO')

    def test_new_questions_never_grade_unanswered_items(self):
        self.demo()
        result = run_agent(self.service, self.session, '准备十道题', FakeClient([
            calls(('start_interview', {'count': 10}), ('review_answer', {'question_id': 'Q1', 'answer': '模型自编答案'}))]))
        self.assertEqual([t['tool'] for t in result['trace']], ['start_interview'])
        self.assertEqual(self.service.dashboard(self.session)['interview']['answers'], [])

    def test_role_update_keeps_existing_preferences(self):
        self.service.set_target(self.session, 'Python开发', '实习', '上海', '计算机专业')
        dispatch(self.service, self.session, 'set_target', {'role': 'Java开发'})
        profile = self.service.dashboard(self.session)['profile']
        self.assertEqual((profile['stage'], profile['city'], profile['background']), ('实习', '上海', '计算机专业'))

    def test_no_tool_fabricated_resume_never_reaches_user_or_history(self):
        self.demo()
        invented = '建议简历写：曾在某大厂实习两年，熟练掌握Docker。'
        result = run_agent(self.service, self.session, '优化简历', FakeClient([{'content': invented}, {'content': invented}]))
        self.assertNotIn('某大厂', result['answer'])
        self.assertNotIn('某大厂', json.dumps(self.service.load_messages(self.session), ensure_ascii=False))

    def test_production_recommendation_context_fields_export_once(self):
        self.demo()
        jobs = self.service.dashboard(self.session)['opportunities']
        redundant = [{k: job[k] for k in ('company', 'role', 'deadline')} | {'export_format': 'xlsx'} for job in jobs]
        client = FakeClient([calls(*(('recommend_jobs', args) for args in redundant))])
        result = run_agent(self.service, self.session, '根据我的材料推荐适合的公司和岗位，说明差距，并导出 Excel 投递清单。', client)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len(result['trace']), 1)
        data = result['trace'][0]['result']
        self.assertNotIn('error', data)
        self.assertEqual(len(data['export_result']['files']), 1)
        self.assertTrue(self.service.resolve_export(self.session, data['export_result']['files'][0]['name']).is_file())
        self.assertEqual(len(self.service.dashboard(self.session)['exports']), 1)
        self.assertTrue(any('company' in item for item in data['argument_adjustments']))
        self.assertTrue(any('deadline' in item for item in data['argument_adjustments']))
        self.assertEqual(len(self.service.dashboard(self.session)['opportunities']), len(jobs))
        history = self.service.load_messages(self.session)
        self.assertEqual({c['id'] for m in history for c in m.get('tool_calls', [])},
                         {m['tool_call_id'] for m in history if m.get('role') == 'tool'})
        self.assertNotIn('参数缺失', result['answer'])

    def test_recommendation_context_compatibility_never_accepts_identity_or_paths(self):
        self.demo()
        for key in ('session', 'path', 'sql', 'database', 'opportunity_ids'):
            result = dispatch(self.service, self.session, 'recommend_jobs',
                              {'company': '无关上下文', 'deadline': '2099-01-01', key: 'untrusted', 'export_format': 'xlsx'})
            self.assertEqual(result['error_code'], 'invalid_arguments')
        self.assertEqual(self.service.dashboard(self.session)['exports'], [])

    def test_unrepaired_tool_errors_stop_after_two_and_show_one_actionable_message(self):
        self.demo()
        client = FakeClient([calls(('recommend_jobs', {'resume_text': '模型附带正文'})),
                             calls(('recommend_jobs', {'resume_text': '再次附带正文'}))])
        result = run_agent(self.service, self.session, '推荐岗位并导出Excel', client)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(len(result['trace']), 2)
        self.assertTrue(result['trace'][-1]['result']['retry_stopped'])
        self.assertEqual(result['answer'].count('这次推荐调用未能完成'), 1)
        self.assertIn('生成 / 更新推荐', result['answer'])
        self.assertNotIn('参数缺失或含未允许字段', result['answer'])
        self.assertEqual(self.service.dashboard(self.session)['exports'], [])

    def test_repaired_attempt_keeps_trace_but_not_obsolete_error_in_answer(self):
        self.demo()
        client = FakeClient([calls(('recommend_jobs', {'extra_unknown': 'wrong'})),
                             calls(('recommend_jobs', {'export_format': 'xlsx'}))])
        result = run_agent(self.service, self.session, '推荐并导出Excel', client)
        self.assertEqual(len(result['trace']), 2)
        self.assertIn('error', result['trace'][0]['result'])
        self.assertNotIn('error', result['trace'][1]['result'])
        self.assertNotIn('未能完成', result['answer'])
        self.assertIn('可下载', result['answer'])

    def test_missing_recommendation_inputs_ask_once_without_export(self):
        self.service.set_target(self.session, 'Java 后端实习', '实习')
        client = FakeClient([calls(('recommend_jobs', {'export_format': 'xlsx'}))])
        result = run_agent(self.service, self.session, '推荐岗位并导出Excel', client)
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(result['trace'][0]['result']['needs_input'])
        self.assertIn('简历、项目或技能清单', result['answer'])
        self.assertIn('至少一个公司的岗位 JD', result['answer'])
        self.assertEqual(self.service.dashboard(self.session)['exports'], [])

    def test_model_cannot_invent_goal_for_recommendation_request(self):
        client = FakeClient([calls(('set_target', {'role': 'Java 后端开发'}))])
        result = run_agent(self.service, self.session, '帮我推荐适合的公司', client)
        self.assertTrue(result['trace'][0]['result']['needs_input'])
        self.assertFalse(self.service.dashboard(self.session)['profile']['role'])
        self.assertIn('具体岗位', result['answer'])

    def test_explicit_goal_can_still_be_saved_from_chat(self):
        client = FakeClient([calls(('set_target', {'role': 'Java 后端实习生', 'stage': '实习'})), {'content': '请添加材料'}])
        run_agent(self.service, self.session, '我想申请 Java 后端实习生', client)
        self.assertEqual(self.service.dashboard(self.session)['profile']['role'], 'Java 后端实习生')


if __name__ == '__main__':
    unittest.main()
