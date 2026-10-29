"""临时分享源站：独立资料目录、业务配额和受限本机 HTTP 服务。

必须由带认证的 gateway 转发；本文件只监听 127.0.0.1。导入本模块不会
导入 server、读取 .env 或创建资料目录。容量检查是应用层预算，不是 OS 硬配额。
"""
import argparse
import csv
import html
import io
import json
import os
import re
import threading
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from career.service import BusinessError, Service, now, safe_csv

ROOT = Path(__file__).resolve().parent
PRIVATE_RUNTIME = (ROOT / 'runtime' / 'career').resolve()
MARKER_NAME = '.job-coach-share.json'
MARKER = {'kind': 'job-coach-share', 'version': 1}
QUOTA_LOCK = threading.RLock()


@dataclass(frozen=True)
class ShareLimits:
    max_profiles: int = 60
    max_documents_per_profile: int = 15
    max_document_chars_per_profile: int = 300_000
    max_applications_per_profile: int = 50
    max_opportunities_per_profile: int = 20
    max_pending_per_profile: int = 20
    max_pending_history_per_profile: int = 200
    max_exports_per_profile: int = 30
    max_exports_global: int = 300
    max_questions_per_profile: int = 100
    max_audit_per_profile: int = 200
    max_session_bytes: int = 384 * 1024
    max_session_messages: int = 200
    max_cache_bytes: int = 1024 * 1024
    max_answer_chars: int = 3000
    max_storage_bytes: int = 64 * 1024 * 1024
    database_reserve_bytes: int = 4 * 1024 * 1024
    export_reserve_bytes: int = 16 * 1024 * 1024


def validate_share_root(value):
    """拒绝私有目录、其上层和其子目录；不创建任何路径。"""
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError('分享实例必须显式设置独立 CAREER_RUNTIME，不能使用原来的本地资料目录。')
    root = Path(value).expanduser().resolve()
    if root == PRIVATE_RUNTIME or root in PRIVATE_RUNTIME.parents or PRIVATE_RUNTIME in root.parents:
        raise ValueError('CAREER_RUNTIME 与原本地资料目录重叠，请指定新的独立分享目录。')
    if root.exists():
        if not root.is_dir():
            raise ValueError('CAREER_RUNTIME 必须是目录。')
        marker = root / MARKER_NAME
        entries = list(root.iterdir())
        if entries and not marker.is_file():
            raise ValueError('分享资料目录必须为空，或具有本程序的分享标记；不会直接公开已有资料。')
        if marker.exists():
            if marker.is_symlink() or marker.stat().st_size > 1024:
                raise ValueError('分享目录标记无效。')
            try:
                if json.loads(marker.read_text(encoding='utf-8')) != MARKER:
                    raise ValueError('分享目录标记不匹配。')
            except (OSError, json.JSONDecodeError):
                raise ValueError('分享目录标记无法读取。') from None
    return root


def runtime_from_environment(environ=None):
    env = os.environ if environ is None else environ
    return validate_share_root(env.get('CAREER_RUNTIME', ''))


def local_model_config(environ=None):
    """只使用进程显式配置或本机默认值，绝不读取项目 .env 或云端 Key。"""
    env = os.environ if environ is None else environ
    base = env.get('MODEL_BASE_URL', 'http://127.0.0.1:11434/v1').strip().rstrip('/')
    try:
        parsed = urlparse(base)
        valid = (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
                 and parsed.path == '/v1' and not parsed.query and not parsed.fragment
                 and not parsed.username and not parsed.password and parsed.port is not None)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('分享实例只接受本机 Ollama 的 http://127.0.0.1:端口/v1 地址。')
    model = env.get('MODEL_NAME', 'qwen3:1.7b').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', model) or model.lower().endswith(':cloud'):
        raise ValueError('分享实例必须使用已下载的本地模型名称。')
    try:
        timeout = float(env.get('MODEL_TIMEOUT', '120'))
        if not 1 <= timeout <= 180:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('MODEL_TIMEOUT 应为 1～180 秒。') from None
    return {'MODEL_BASE_URL': base, 'MODEL_NAME': model, 'MODEL_API_KEY': 'ollama', 'MODEL_TIMEOUT': str(timeout)}


class ShareService(Service):
    """单个分享进程中所有入口共用同一把配额锁，模型工具无法绕开子类方法。"""
    def __init__(self, root, limits=None):
        if isinstance(limits, ShareLimits):
            self.limits = limits
        else:
            self.limits = ShareLimits(**(limits or {}))
        if any(type(v) is not int or v <= 0 for v in asdict(self.limits).values()):
            raise ValueError('分享配额必须为正整数。')
        self._session_locks = {}
        with QUOTA_LOCK:
            root = validate_share_root(root)
            root.mkdir(parents=True, exist_ok=True)
            marker = root / MARKER_NAME
            if not marker.exists():
                with marker.open('x', encoding='utf-8') as output:
                    json.dump(MARKER, output)
            super().__init__(root)

    def _storage_bytes(self):
        total = 0
        try:
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                for name in dirs + files:
                    path = Path(directory) / name
                    stat = path.lstat()
                    if path.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                        raise BusinessError('分享数据目录出现链接或重解析点，已停止写入，请管理员检查。')
                    if name in files:
                        total += stat.st_size
        except OSError:
            raise BusinessError('无法核对分享目录容量，已停止新增数据。') from None
        return total

    def _space(self, extra=0, reserve=None):
        reserve = self.limits.database_reserve_bytes if reserve is None else reserve
        if self._storage_bytes() + max(0, extra) + reserve > self.limits.max_storage_bytes:
            mib = self.limits.max_storage_bytes / (1024 * 1024)
            raise BusinessError(f'分享实例接近 {mib:g} MiB 应用容量预算，已停止新增材料或文件。不会自动删除已有资料，请联系管理员结束或更换分享实例。')

    def _count(self, table, session=None):
        # 调用者只传本文件固定表名，不接受用户 SQL/标识符。
        allowed = {'profiles', 'documents', 'applications', 'pending', 'exports', 'questions', 'audit'}
        if table not in allowed:
            raise ValueError('unknown quota table')
        with self.connect() as db:
            query = f'SELECT count(*) FROM {table}' + (' WHERE session=?' if session is not None else '')
            return db.execute(query, (session,) if session is not None else ()).fetchone()[0]

    def _ensure(self, session):
        self._check_session(session)
        with QUOTA_LOCK:
            with self.connect() as db:
                if db.execute('SELECT 1 FROM profiles WHERE session=?', (session,)).fetchone():
                    return
                if db.execute('SELECT count(*) FROM profiles').fetchone()[0] >= self.limits.max_profiles:
                    raise BusinessError(f'此分享实例最多接待 {self.limits.max_profiles} 个档案，已达到上限。')
            self._space(16 * 1024)
            with self.connect() as db:
                db.execute('INSERT INTO profiles(session) VALUES(?)', (session,))

    def session_lock(self, session):
        with QUOTA_LOCK:
            self._ensure(session)
            return self._session_locks.setdefault(session, threading.RLock())

    def set_target(self, session, role, stage='校招', city='', background=''):
        with QUOTA_LOCK:
            self._space(16 * 1024)
            return super().set_target(session, role, stage, city, background)

    def add_document(self, session, title, kind, text):
        with QUOTA_LOCK:
            return super().add_document(session, title, kind, text)

    def _add_document(self, session, title, kind, text, sample):
        with QUOTA_LOCK:
            self._ensure(session)
            if not isinstance(text, str):
                raise BusinessError('材料正文必须是文本。')
            with self.connect() as db:
                count, total = db.execute('SELECT count(*),coalesce(sum(length(text)),0) FROM documents WHERE session=?', (session,)).fetchone()
            if count >= self.limits.max_documents_per_profile:
                raise BusinessError(f'分享档案最多保存 {self.limits.max_documents_per_profile} 份材料。')
            if total + len(text) > self.limits.max_document_chars_per_profile:
                raise BusinessError(f'分享档案的材料正文合计最多 {self.limits.max_document_chars_per_profile} 字。')
            self._space(len(text.encode('utf-8')) * 2 + 32 * 1024)
            return super()._add_document(session, title, kind, text, sample)

    def import_document(self, session, filename, kind, content_base64):
        with QUOTA_LOCK:
            self._space()
        return super().import_document(session, filename, kind, content_base64)

    def delete_document(self, session, document_id):
        # 用户主动删除自己的材料仍沿用原接口；配额不会主动清理任何旧资料。
        with QUOTA_LOCK:
            return super().delete_document(session, document_id)

    def save_opportunity(self, session, company, role, jd, company_info='', url='', deadline='', city='', source='', opportunity_id=''):
        with QUOTA_LOCK:
            self._ensure(session)
            if not all(isinstance(value, str) for value in (jd, company_info)):
                raise BusinessError('岗位 JD 和公司资料必须是文本。')
            with self.connect() as db:
                count = db.execute('SELECT count(*) FROM opportunities WHERE session=?', (session,)).fetchone()[0]
                exists = bool(opportunity_id) and db.execute('SELECT 1 FROM opportunities WHERE id=? AND session=?', (opportunity_id, session)).fetchone()
            if not exists and count >= self.limits.max_opportunities_per_profile:
                raise BusinessError(f'分享档案最多保存 {self.limits.max_opportunities_per_profile} 条候选岗位。')
            self._space((len(jd.encode('utf-8')) + len(company_info.encode('utf-8'))) * 2 + 32 * 1024)
            return super().save_opportunity(session, company, role, jd, company_info, url, deadline, city, source, opportunity_id)

    def delete_opportunity(self, session, opportunity_id):
        with QUOTA_LOCK:
            return super().delete_opportunity(session, opportunity_id)

    def load_demo(self, session):
        with QUOTA_LOCK:
            self._space(128 * 1024)
            if self.limits.max_documents_per_profile < 4 or self.limits.max_document_chars_per_profile < 6000:
                raise BusinessError('当前分享材料配额不足以完整载入示例。')
            return super().load_demo(session)

    def _cache(self, session, kind, data):
        with QUOTA_LOCK:
            self._ensure(session)
            if kind not in ('match', 'resume', 'recommendations'):
                raise BusinessError('不允许创建额外分析缓存类别。')
            encoded = json.dumps(data, ensure_ascii=False).encode('utf-8')
            if len(encoded) > self.limits.max_cache_bytes:
                raise BusinessError('分析结果超过分享实例单份缓存预算，请缩小材料范围后重试。')
            self._space(len(encoded) * 2)
            return super()._cache(session, kind, data)

    def save_messages(self, session, messages):
        with QUOTA_LOCK:
            self._ensure(session)
            if not isinstance(messages, list) or len(messages) > self.limits.max_session_messages:
                raise BusinessError('当前分享档案的对话记录达到上限，请新建档案。')
            encoded = json.dumps(messages, ensure_ascii=False).encode('utf-8')
            if len(encoded) > self.limits.max_session_bytes:
                raise BusinessError('当前分享档案的对话内容达到容量上限，请新建档案。已有记录保留。')
            self._space(len(encoded) * 2)
            return super().save_messages(session, messages)

    def start_interview(self, session, count=10):
        with QUOTA_LOCK:
            profile = self._profile(session, True)
            if type(count) is not int or not 1 <= count <= 10:
                raise BusinessError('每轮面试题数应为 1～10。')
            with self.connect() as db:
                existing = db.execute('SELECT 1 FROM questions WHERE session=? AND target=? AND revision=?', (session, profile['role'], profile['revision'])).fetchone()
            if not existing:
                if self._count('questions', session) + count > self.limits.max_questions_per_profile:
                    raise BusinessError(f'分享档案最多保留 {self.limits.max_questions_per_profile} 道历史面试题，请继续已有题目或新建档案。')
                self._space(count * 12 * 1024)
            return super().start_interview(session, count)

    def review_answer(self, session, question_id, answer):
        with QUOTA_LOCK:
            self._ensure(session)
            if not isinstance(answer, str) or len(answer) > self.limits.max_answer_chars:
                raise BusinessError(f'分享实例每次面试回答最多 {self.limits.max_answer_chars} 字。')
            # 原评分最多重复引用五个回答片段；预留正文、提纲、证据和 SQLite 页开销。
            self._space(len(answer.encode('utf-8')) * 8 + 32 * 1024)
            return super().review_answer(session, question_id, answer)

    def prepare_application(self, session, company, role, url='', status='待投递', deadline='', notes='', application_id=''):
        with QUOTA_LOCK:
            self._profile(session, True)
            if self._count('pending', session) >= self.limits.max_pending_history_per_profile:
                raise BusinessError(f'分享档案最多保留 {self.limits.max_pending_history_per_profile} 次投递变更记录，已达到历史上限。')
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
            with self.connect() as db:
                active = db.execute("SELECT count(*) FROM pending WHERE session=? AND state='pending' AND created>=?", (session, cutoff)).fetchone()[0]
            if active >= self.limits.max_pending_per_profile:
                raise BusinessError(f'分享档案最多有 {self.limits.max_pending_per_profile} 条未处理待确认记录，请先确认或取消。')
            if not application_id and self._count('applications', session) >= self.limits.max_applications_per_profile:
                raise BusinessError(f'分享档案最多保存 {self.limits.max_applications_per_profile} 条投递记录，可以继续更新已有记录。')
            self._space(32 * 1024)
            return super().prepare_application(session, company, role, url, status, deadline, notes, application_id)

    def confirm_application(self, session, pending_id, accept):
        with QUOTA_LOCK:
            self._ensure(session)
            if accept is True:
                with self.connect() as db:
                    row = db.execute("SELECT payload FROM pending WHERE id=? AND session=? AND state='pending'", (pending_id, session)).fetchone()
                if row:
                    payload = json.loads(row['payload'])
                    if not payload.get('application_id') and self._count('applications', session) >= self.limits.max_applications_per_profile:
                        raise BusinessError(f'分享档案已达到 {self.limits.max_applications_per_profile} 条投递记录上限，本次尚未保存。')
                    if self._count('audit', session) >= self.limits.max_audit_per_profile:
                        raise BusinessError('当前分享档案的投递变更历史已达到上限，本次尚未保存。')
                self._space(32 * 1024)
            return super().confirm_application(session, pending_id, accept)

    def _export_slots(self, session, count):
        if self._count('exports', session) + count > self.limits.max_exports_per_profile:
            raise BusinessError(f'分享档案最多生成 {self.limits.max_exports_per_profile} 份文件，本次整组材料尚未生成。')
        if self._count('exports') + count > self.limits.max_exports_global:
            raise BusinessError(f'分享实例最多生成 {self.limits.max_exports_global} 份文件，本次整组材料尚未生成。')

    def _write_export(self, session, prefix, suffix, label, data):
        # 纵使其他业务方法未来直接调用单文件输出，也必须经过同一配额。
        with QUOTA_LOCK:
            self._ensure(session)
            encoded = data if isinstance(data, bytes) else data.encode('utf-8')
            self._export_slots(session, 1)
            self._space(len(encoded) + 16 * 1024)
            return super()._write_export(session, prefix, suffix, label, encoded)

    def _material_contents(self, session, kind):
        """先在内存里准备完整文件集合；源业务仍决定事实、STAR 和评分。"""
        profile = self._profile(session, True)
        sample = '虚构教学示例，请勿作为本人经历投递。\n\n' if profile['mode'] == 'demo' else ''
        contents, missing = [], []

        def put(prefix, suffix, label, filename, value):
            contents.append({'prefix': prefix, 'suffix': suffix, 'label': label, 'archive_name': filename,
                             'data': value if isinstance(value, bytes) else value.encode('utf-8')})

        if kind in ('bundle', 'resume'):
            resume = self.build_resume(session)
            missing += resume['missing']
            source_text = '\n'.join('- ' + source['title'] + ' [' + source['document_id'] + ']' for source in resume['sources'])
            text = sample + resume['text'] + '\n\n## 材料依据\n' + source_text + '\n\n' + resume['note']
            put('resume', 'md', '简历初稿 Markdown', '简历初稿.md', text)
            status = '待补全：含【需确认】，请核对后再投递。' if missing else '请本人逐项核对原文与事实后使用。'
            body = []
            for line in resume['text'].splitlines():
                if not line:
                    continue
                heading = re.match(r'^(#{1,3})\s+(.+)', line)
                if heading:
                    level = min(len(heading[1]) + 1, 4)
                    body.append(f'<h{level}>' + html.escape(heading[2]) + f'</h{level}>')
                else:
                    body.append('<p>' + html.escape(line) + '</p>')
            page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>求职简历初稿</title><style>body{max-width:820px;margin:40px auto;padding:24px;font:16px/1.8 "Microsoft YaHei",sans-serif;color:#182c41}p{white-space:pre-wrap}.notice{padding:14px;background:#fff2d9}@media print{body{margin:0;padding:0}.no-print{display:none}}</style><h1>' + html.escape(resume['title']) + '</h1><div class="notice">' + html.escape(sample + status) + '</div><p class="no-print">浏览器打印可另存 PDF。先补全缺项，再检查分页。</p>' + ''.join(body) + '<hr><p>' + html.escape(resume['note']) + '</p></html>'
            put('resume', 'html', '简历初稿 HTML（可打印）', '简历初稿.html', page)
        if kind == 'bundle':
            try:
                result = self.analyze_fit(session)
                score = '未计算' if result['score'] is None else str(result['score']) + '%'
                text = sample + '# 岗位匹配建议\n\n目标岗位：' + profile['role'] + '\n\n材料显式技能覆盖率：' + score + '\n\n' + result['note'] + '\n\n## 三项优先修改\n' + '\n'.join('- ' + p for p in result['priorities']) + '\n\n## 能力与原文证据\n'
                for item in result['ability_map']:
                    text += '\n### ' + item['skill'] + '\n' + '\n'.join('- ' + e['title'] + '：' + e['quote'] for e in item['evidence']) + '\n'
            except BusinessError as exc:
                # 缺 JD 可形成待补全材料；配额失败必须中止整组，而非吞掉限制。
                if not self._docs(session, ('jd',)):
                    text = sample + '# 岗位匹配建议\n\n【需确认】' + str(exc)
                    missing.append(str(exc))
                else:
                    raise
            put('match', 'md', '岗位匹配与能力映射', '岗位匹配建议.md', text)
        if kind in ('bundle', 'interview'):
            try:
                interview = self.start_interview(session, 10)
                answers = {answer['question_id']: answer for answer in self.dashboard(session)['interview']['answers']}
                text = sample + '# 面试练习清单\n\n' + interview['note'] + '\n'
                for index, question in enumerate(interview['questions'], 1):
                    text += f'\n## {index}. {question["question"]}\n\n考察方向：{question["focus"]}\n'
                    answer = answers.get(question['id'])
                    if answer:
                        text += f'\n我的回答：{answer["answer"]}\n\n表达完整度：{answer["score"]}/10\n\n改进建议：' + '；'.join(answer['feedback']) + '\n\n' + answer['note'] + '\n'
                    else:
                        text += '\n我的回答：【待练习】\n'
            except BusinessError as exc:
                if kind == 'bundle' and not self._docs(session, ('jd',)):
                    text = sample + '# 面试练习清单\n\n【需确认】' + str(exc)
                    missing.append(str(exc))
                else:
                    raise
            put('interview', 'md', '面试清单与练习反馈', '面试清单.md', text)
        if kind in ('bundle', 'applications'):
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer)
            writer.writerow(['公司', '岗位', '链接', '状态', '截止日期', '备注', '数据性质'])
            for application in self.list_applications(session)['applications']:
                writer.writerow([safe_csv(application[key]) for key in ('company', 'role', 'url', 'status', 'deadline', 'notes')] + ['虚构示例' if sample else '用户记录'])
            put('applications', 'csv', '投递记录表 CSV', '投递记录表.csv', buffer.getvalue().encode('utf-8-sig'))
        if kind == 'bundle':
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for content in contents:
                    archive.writestr(content['archive_name'], content['data'])
                archive.writestr('使用说明.txt', sample + '本材料在独立分享实例生成，未向任何公司发送。\n请补全【需确认】并核对事实后使用。匹配分是词表覆盖率，面试分是表达完整度分。\n导出含个人材料原文，请自行决定分享范围。')
            put('materials', 'zip', '完整求职材料包 ZIP', '完整求职材料包.zip', buffer.getvalue())
        return contents, list(dict.fromkeys(missing)), profile['mode'] == 'demo'

    def export_materials(self, session, kind='bundle'):
        if kind not in ('bundle', 'resume', 'interview', 'applications'):
            raise BusinessError('请选择简历、面试、投递记录或完整材料包。')
        expected = {'bundle': 6, 'resume': 2, 'interview': 1, 'applications': 1}[kind]
        with QUOTA_LOCK:
            self._profile(session, True)
            self._export_slots(session, expected)
            # 在分析/面试缓存更新前预留完整输出空间，减少容量临界时的半成品。
            self._space(reserve=max(self.limits.export_reserve_bytes, self.limits.database_reserve_bytes))
            contents, missing, sample = self._material_contents(session, kind)
            if len(contents) != expected:
                raise BusinessError('导出文件集合不完整，本次未写入任何下载文件。')
            self._export_slots(session, len(contents))
            self._space(sum(len(item['data']) for item in contents) + len(contents) * 16 * 1024)
            plans, written = [], []
            for item in contents:
                name = item['prefix'] + '_' + uuid.uuid4().hex + '.' + item['suffix']
                plans.append(item | {'name': name, 'session': session, 'kind': item['prefix'], 'created': now()})
            try:
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for item in plans:
                        path = self.exports / item['name']
                        if path.resolve().parent != self.exports.resolve():
                            raise BusinessError('导出路径异常。')
                        with path.open('xb') as stream:
                            written.append(path)
                            stream.write(item['data'])
                        db.execute('INSERT INTO exports(name,session,label,kind,created) VALUES(:name,:session,:label,:kind,:created)', item)
            except Exception:
                # 只回滚本次尚未完成的新文件，绝不删除历史材料或其他档案文件。
                for path in written:
                    if path.resolve().parent == self.exports.resolve():
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                raise
            return {'files': [self._export_row(item) for item in plans], 'missing': missing, 'sample_data': sample,
                    'note': '文件已在独立分享实例生成，没有投递或发送。请补全【需确认】并人工校对；示例材料不能作为本人经历使用。'}


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 24

    def __init__(self, address, handler, max_requests=24, bind_and_activate=True):
        if address[0] != '127.0.0.1' or not 1 <= max_requests <= 24:
            raise ValueError('分享源站只允许本机监听，最多 24 个并发请求。')
        self._request_slots = threading.BoundedSemaphore(max_requests)
        self.chat_slot = threading.BoundedSemaphore(1)
        super().__init__(address, handler, bind_and_activate=bind_and_activate)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            body = '分享服务繁忙，请稍后重试。'.encode('utf-8')
            response = b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
            try:
                request.sendall(response)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def make_share_handler(base_handler):
    class ShareHandler(base_handler):
        def do_POST(self):
            if self.path != '/api/chat':
                return super().do_POST()
            if not self.server.chat_slot.acquire(blocking=False):
                return self.send({'error': '分享实例正在处理另一条模型请求，请稍后重试。'}, status=429)
            try:
                return super().do_POST()
            finally:
                # 即使 gateway 已超时，源站真实处理结束前也不会释放模型槽位。
                self.server.chat_slot.release()
    return ShareHandler


def create_share_server(port=8512):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('端口必须为 1～65535。')
    runtime = runtime_from_environment()
    config = local_model_config()
    # 必须先通过目录检查，再导入会初始化 Service 的普通 server。
    share_service = ShareService(runtime)
    os.environ['CAREER_RUNTIME'] = str(runtime)
    import server
    server.SERVICE = share_service
    server.load_config = lambda _root: dict(config)
    server.session_lock = share_service.session_lock
    server.LOCKS = share_service._session_locks
    server.LOCKS_GUARD = QUOTA_LOCK
    return LimitedThreadingHTTPServer(('127.0.0.1', port), make_share_handler(server.Handler))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8512)
    args = parser.parse_args(argv)
    try:
        httpd = create_share_server(args.port)
    except (ValueError, BusinessError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(f'Job Coach share origin: http://127.0.0.1:{args.port} (gateway required)', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
