"""启程求职教练，本机单用户应用。python server.py"""
import argparse
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'runtime' / 'deps'))
from career.agent import ModelClient, load_config, run_agent
from career.service import BusinessError, Service

SERVICE = Service(os.environ.get('CAREER_RUNTIME', ROOT / 'runtime' / 'career'))
CSRF = secrets.token_hex(32)
LOCKS = {}
LOCKS_GUARD = threading.Lock()
SESSION_PATTERN = re.compile(r'[a-f0-9]{32}')


def session_lock(session):
    with LOCKS_GUARD:
        return LOCKS.setdefault(session, threading.RLock())


ROUTES = {
    '/api/target': ('set_target', {'role', 'stage', 'city', 'background'}),
    '/api/documents': ('add_document', {'title', 'kind', 'text'}),
    '/api/upload': ('import_document', {'filename', 'kind', 'content_base64'}),
    '/api/demo': ('load_demo', set()),
    '/api/match': ('analyze_fit', set()),
    '/api/resume': ('build_resume', set()),
    '/api/interview/start': ('start_interview', {'count'}),
    '/api/interview/answer': ('review_answer', {'question_id', 'answer'}),
    '/api/applications/prepare': ('prepare_application', {'company', 'role', 'url', 'status', 'deadline', 'notes', 'application_id'}),
    '/api/confirm': ('confirm_application', {'pending_id', 'accept'}),
    '/api/applications': ('list_applications', {'status', 'due_this_week'}),
    '/api/export': ('export_materials', {'kind'}),
    '/api/skills/profile': ('skill_profile', set()),
    '/api/opportunities/list': ('list_opportunities', set()),
    '/api/opportunities/save': ('save_opportunity', {'company', 'role', 'jd', 'company_info', 'url', 'deadline', 'city', 'source', 'opportunity_id'}),
    '/api/recommendations': ('recommend_jobs', {'limit', 'include_expired'}),
    '/api/recommendations/export': ('export_recommendations', {'format', 'limit', 'include_expired'}),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, data, mime='application/json; charset=utf-8', status=200, filename=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        elif isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/':
            self.send((ROOT / 'static/index.html').read_text(encoding='utf-8').replace('__CSRF__', CSRF), 'text/html; charset=utf-8')
        elif url.path == '/health':
            self.send({'status': 'ok', 'app': 'job-coach', 'features': ['company-job-recommendations-v1', 'agent-argument-repair-v1']})
        elif url.path == '/presentation':
            self.send((ROOT / 'presentation.html').read_bytes(), 'text/html; charset=utf-8')
        elif url.path.startswith('/downloads/'):
            name = url.path.removeprefix('/downloads/')
            session = parse_qs(url.query).get('session', [''])[0]
            if not SESSION_PATTERN.fullmatch(session) or not re.fullmatch(r'[A-Za-z0-9_-]+\.(?:md|html|csv|xlsx|zip|txt|docx|json)', name):
                return self.send({'error': '下载地址无效，请从材料包页面重新下载。'}, status=400)
            try:
                file = SERVICE.resolve_export(session, name)
                self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream', filename=file.name)
            except (BusinessError, FileNotFoundError):
                self.send({'error': '文件不存在或不属于当前档案。'}, status=404)
        else:
            self.send({'error': '页面不存在。'}, status=404)

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), CSRF):
            return self.send({'error': '页面已过期，请刷新后重试。'}, status=403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8 * 1024 * 1024:
                raise BusinessError('请求为空或过大，单个文件最多 5 MB。')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise BusinessError('请求内容必须是对象。')
            session = body.pop('session', '')
            if not isinstance(session, str) or not SESSION_PATTERN.fullmatch(session):
                raise BusinessError('档案编号无效，请刷新页面。')
            if self.path == '/api/state':
                if body:
                    raise BusinessError('状态请求含未允许字段。')
                result = SERVICE.dashboard(session)
                result['model'] = load_config(ROOT)['MODEL_NAME']
                result['messages'] = SERVICE.load_messages(session)
            else:
                with session_lock(session):
                    if self.path == '/api/chat':
                        prompt = body.get('prompt', '')
                        if set(body) != {'prompt'} or not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 6000:
                            raise BusinessError('请输入 1～6000 字的消息，长文请加入材料库。')
                        result = run_agent(SERVICE, session, prompt, ModelClient(load_config(ROOT)))
                    elif self.path == '/api/documents/delete':
                        if set(body) != {'document_id', 'confirmed'} or body['confirmed'] is not True:
                            raise BusinessError('请先确认删除这份材料。')
                        result = SERVICE.delete_document(session, body['document_id'])
                    elif self.path == '/api/opportunities/delete':
                        if set(body) != {'opportunity_id', 'confirmed'} or body['confirmed'] is not True:
                            raise BusinessError('请先确认删除这条岗位记录。')
                        result = SERVICE.delete_opportunity(session, body['opportunity_id'])
                    elif self.path in ROUTES:
                        method, allowed = ROUTES[self.path]
                        if set(body) - allowed:
                            raise BusinessError('请求包含未允许字段。')
                        result = getattr(SERVICE, method)(session, **body)
                    else:
                        return self.send({'error': '接口不存在。'}, status=404)
            self.send(result)
        except (BusinessError, ValueError, TypeError) as exc:
            self.send({'error': str(exc)}, status=400)
        except Exception:
            self.send({'error': '本地服务处理失败。请重试，或检查 runtime 目录写入权限。'}, status=500)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8501)
    args = parser.parse_args()
    print(f'Job Coach running at http://127.0.0.1:{args.port}', flush=True)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
    finally:
        server.server_close()
