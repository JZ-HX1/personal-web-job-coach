"""求职业务规则：数据归属、事实证据、可解释评分和用户确认均独立于模型。"""
import csv
import html
import io
import json
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from .documents import DocumentError, MAX_TEXT_CHARS, parse_document
from .recommendations import NOTE as RECOMMENDATION_NOTE, profile_from_documents, recommend_one, recommendation_rows, spreadsheet_bytes


class BusinessError(ValueError):
    pass


KINDS = ('resume', 'project', 'jd', 'notice', 'interview', 'example', 'transcript', 'skill_list', 'certificate', 'portfolio')
PERSONAL_KINDS = ('resume', 'project', 'transcript', 'skill_list', 'certificate', 'portfolio')
STATUSES = ('待投递', '已投递', '笔试', '面试', 'Offer', '未通过', '已撤回')
SKILLS = {
    'Python': r'(?<![a-z0-9_])python(?![a-z0-9_])',
    'Java': r'(?<![a-z0-9_])java(?![a-z0-9_])',
    'JavaScript': r'(?<![a-z0-9_])(?:javascript|js)(?![a-z0-9_])',
    'TypeScript': r'(?<![a-z0-9_])typescript(?![a-z0-9_])',
    'C/C++': r'(?<![a-z0-9_])c\+\+(?![a-z0-9_])|C语言|C 语言',
    'SQL': r'(?<![a-z0-9_])(?:sql|mysql|postgresql|sqlite)(?![a-z0-9_])|关系型数据库',
    'Git': r'(?<![a-z0-9_])git(?![a-z0-9_])|版本控制',
    'Linux': r'(?<![a-z0-9_])linux(?![a-z0-9_])',
    'Docker': r'(?<![a-z0-9_])docker(?![a-z0-9_])',
    'HTTP/API': r'(?<![a-z0-9_])(?:http|https|restful|rest|api)(?![a-z0-9_])|接口开发|接口设计',
    '软件测试': r'单元测试|集成测试|接口测试|自动化测试|(?<![a-z])(?:pytest|unittest)(?![a-z])',
    '数据分析': r'数据分析|数据清洗|(?<![a-z])(?:pandas|numpy)(?![a-z])',
    '前端开发': r'前端|(?<![a-z])(?:html|css|vue|react)(?![a-z])',
    'Spring': r'(?<![a-z])spring(?:\s*boot)?(?![a-z])',
    '算法与数据结构': r'数据结构|算法设计|算法复杂度|排序算法',
    'AI 工具应用': r'(?<![a-z])(?:ollama|chatgpt|qwen|deepseek|langchain)(?![a-z])|AI工具|AI 工具|大模型调用',
    '嵌入式开发': r'嵌入式|单片机|(?<![a-z])(?:stm32|esp32|freertos)(?![a-z])',
    '电路设计': r'电路设计|PCB|模电|数电',
    'CAD/机械设计': r'机械设计|(?<![a-z])(?:cad|solidworks)(?![a-z])',
    '协作与文档': r'团队协作|需求沟通|技术文档|项目文档|代码评审',
}
NEGATIVE = re.compile(r'尚未|未(?:曾)?(?:接触|使用|学习|掌握)|没有(?:使用|掌握|学习|接触|完成)|(?:没有|暂无|缺乏|无).{0,15}(?:经验|基础|能力)|不会|不了解|不熟悉|不熟练|不具备|暂无|待(?:学习|补充|确认|掌握|验证)|计划(?:学习|掌握|使用)|希望(?:学习|掌握)|【需确认】')
INSTRUCTIONS = re.compile(r'忽略(?:之前|以上|系统|所有)|忽略.{0,10}(?:指令|规则)|编造(?:经历|成果|数据)|伪造|system\s*prompt|ignore\s+(?:all|previous)', re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def text_field(value, label, maximum=200, required=False):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise BusinessError(f'{label}必须是 {maximum} 字以内的文本。')
    value = value.strip()
    if required and not value:
        raise BusinessError(f'请填写{label}。')
    return value


def safe_csv(value):
    value = str(value or '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else value


def evidence_lines(text):
    return [re.sub(r'^(?:[-*•]\s+)+', '', line.strip()) for line in re.split(r'[\n\r]+', text)
            if line.strip() and not INSTRUCTIONS.search(line)]


def _skill_evidence(documents):
    found = {}
    for skill, pattern in SKILLS.items():
        for doc in documents:
            for line in evidence_lines(doc['text']):
                # 子句切分避免“熟悉 Python，但尚未接触 Docker”误将 Docker 记作事实。
                clauses = re.split(r'[，,；;。]', line)
                if any(re.search(pattern, clause, re.I) and not NEGATIVE.search(clause) for clause in clauses):
                    quote = line if len(line) <= 420 else next(c for c in clauses if re.search(pattern, c, re.I) and not NEGATIVE.search(c))[:420]
                    found.setdefault(skill, []).append({'document_id': doc['id'], 'title': doc['title'], 'quote': quote})
                    break
    return found


def _resume_project_blocks(document):
    """仅从简历里明确命名的项目段落取材，不把教育/技能段落推断成项目。"""
    blocks, current = [], None
    for raw in document['text'].splitlines():
        line = raw.strip()
        start = re.match(r'^(?:#{1,4}\s*)?项目(?:经历|经验|说明|名称|名)?\s*[:：]\s*(.*)', line)
        if start:
            if current:
                blocks.append(current)
            current = {'lines': [line], 'heading': start[1]}
            continue
        if current:
            if re.match(r'^(?:#{1,4}\s*)?(?:教育|技能|个人信息|基本信息|荣誉|比赛|证书|校园|实习|工作|联系方式|自我评价)', line):
                blocks.append(current)
                current = None
            elif line:
                current['lines'].append(line)
    if current:
        blocks.append(current)
    result = []
    for block in blocks:
        heading = re.split(r'[，,。；;]', block['heading'], maxsplit=1)[0].strip()
        result.append(document | {'text': '\n'.join(block['lines']), 'project_title': heading or '简历中的项目经历'})
    return result


def _project_star(document):
    """纯文本重排：标签和句式确定栏位，正文词句仍来自原文。"""
    labels = {'situation': '项目背景', 'task': '个人职责', 'action': '具体行动', 'result': '可核验结果'}
    buckets = {key: [] for key in labels}
    quotes = {key: [] for key in labels}
    other = []
    project = document.get('project_title', document['title'])
    for line in evidence_lines(document['text']):
        title_match = re.match(r'^项目(?:名称|名|经历|经验|说明)?\s*[:：]\s*(.*)', line)
        if title_match:
            pieces = re.split(r'[，,。；;]', title_match[1], maxsplit=1)
            if pieces[0].strip():
                project = pieces[0].strip()
            if len(pieces) == 1:
                continue
            line = pieces[1].strip()
        explicit = re.match(r'^(?:([STAR])\s*[:：]?\s*(?:背景|情境|任务|目标|职责|行动|结果|成果)?|((?:项目)?背景|情境|任务|目标|个人职责|职责|行动|结果|成果))\s*[:：]\s*(.*)', line, re.I)
        if explicit:
            marker, chinese, body = explicit.groups()
            key = {'S': 'situation', 'T': 'task', 'A': 'action', 'R': 'result'}.get((marker or '').upper())
            if key is None:
                key = 'situation' if chinese in ('项目背景', '背景', '情境') else 'task' if chinese in ('任务', '目标', '个人职责', '职责') else 'result' if chinese in ('结果', '成果') else 'action'
            if body.strip():
                buckets[key].append(body.strip())
                quotes[key].append(line)
            continue
        # 拆开带明确句意的逗号/分号子句，避免把“背景，行动，结果”全放进 A。
        for clause in re.split(r'(?<=[。；;])\s*|[，,]', line):
            clause = clause.strip()
            if not clause:
                continue
            if re.search(r'背景|情境|为了解决|为解决|针对|用户需要|课程(?:实践|项目)?中.*需要|需求是', clause):
                key = 'situation'
            elif re.search(r'^(?:我|本人)?(?:负责|承担|担任)|目标是|任务是|个人职责', clause):
                key = 'task'
            elif re.search(r'结果|成果|最终|完成了|交付|获得|提升|减少|降低|通过\s*\d+.*测试|完成.*演示|可.*演示', clause) and not NEGATIVE.search(clause):
                key = 'result'
            elif re.search(r'使用|采用|基于|实现|编写|开发|搭建|设计|维护|排查|修复|优化|接入|部署|测试|验证', clause) and not NEGATIVE.search(clause):
                key = 'action'
            else:
                other.append(clause)
                continue
            buckets[key].append(clause)
            quotes[key].append(clause)
    star = {'project': project, 'source': {'document_id': document['id'], 'title': document['title'], 'quotes': quotes}, 'supplement': other}
    missing = []
    for key, label in labels.items():
        star[key] = '\n'.join(buckets[key]) if buckets[key] else f'【需确认】补充{label}，不要猜测。'
        if not buckets[key] or '【需确认】' in star[key]:
            missing.append(project + '：' + label + '【需确认】')
    # 只连接已有句子，不生成新的动词、数字、技术名或成果描述。
    paragraphs = []
    for key in ('situation', 'task', 'action', 'result'):
        if buckets[key]:
            paragraphs.extend(buckets[key])
    star['paragraph'] = '；'.join(s.rstrip('。；;') for s in dict.fromkeys(paragraphs)) + ('。' if paragraphs else '')
    if not buckets['task']:
        star['paragraph'] += '【需确认】补充个人职责。'
    if not buckets['action']:
        star['paragraph'] += '【需确认】补充具体实现行动。'
    if not buckets['result']:
        star['paragraph'] += '【需确认】补充可核验结果。'
    return star, missing


class Service:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.exports = self.root / 'exports'
        self.exports.mkdir(exist_ok=True)
        self.db = self.root / 'career.sqlite3'
        self.initialize()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS profiles(session TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '校招', city TEXT NOT NULL DEFAULT '', background TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'real', revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES profiles(session),
                title TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS documents_session ON documents(session);
            CREATE TABLE IF NOT EXISTS cache(session TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(session,kind));
            CREATE TABLE IF NOT EXISTS applications(id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES profiles(session),
                company TEXT NOT NULL, role TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL, deadline TEXT NOT NULL,
                notes TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, identity_key TEXT NOT NULL,
                UNIQUE(session,identity_key));
            CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES profiles(session),
                payload TEXT NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY, session TEXT NOT NULL, target TEXT NOT NULL,
                revision INTEGER NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS answers(question_id TEXT PRIMARY KEY REFERENCES questions(id), session TEXT NOT NULL,
                data TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exports(name TEXT PRIMARY KEY, session TEXT NOT NULL, label TEXT NOT NULL,
                kind TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(session TEXT PRIMARY KEY, messages TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, session TEXT NOT NULL, action TEXT NOT NULL, detail TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS opportunities(id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES profiles(session),
                company TEXT NOT NULL, role TEXT NOT NULL, jd TEXT NOT NULL, company_info TEXT NOT NULL,
                url TEXT NOT NULL, deadline TEXT NOT NULL, city TEXT NOT NULL, source TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS opportunities_session ON opportunities(session);
            ''')

    @staticmethod
    def _check_session(session):
        if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session):
            raise BusinessError('会话编号无效，请刷新页面。')

    def _ensure(self, session):
        self._check_session(session)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO profiles(session) VALUES(?)', (session,))

    def _profile(self, session, require=False):
        self._ensure(session)
        with self.connect() as db:
            result = dict(db.execute('SELECT * FROM profiles WHERE session=?', (session,)).fetchone())
        if require and not result['role']:
            raise BusinessError('先明确目标岗位：你想申请什么岗位？还可以补充校招/实习、城市和专业背景。')
        return result

    def _docs(self, session, kinds=()):
        self._ensure(session)
        with self.connect() as db:
            docs = [dict(r) for r in db.execute('SELECT * FROM documents WHERE session=? ORDER BY created,id', (session,))]
        return [d for d in docs if not kinds or d['kind'] in kinds]

    @staticmethod
    def _doc_summary(doc):
        return {k: doc[k] for k in ('id', 'title', 'kind', 'created', 'sample')} | {'preview': doc['text'][:220], 'characters': len(doc['text'])}

    def _invalidate(self, db, session):
        db.execute('DELETE FROM cache WHERE session=?', (session,))
        db.execute('UPDATE profiles SET revision=revision+1 WHERE session=?', (session,))

    def _cache(self, session, kind, data):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)', (session, kind, json.dumps(data, ensure_ascii=False)))

    def _cached(self, session, kind):
        with self.connect() as db:
            row = db.execute('SELECT data FROM cache WHERE session=? AND kind=?', (session, kind)).fetchone()
        return json.loads(row['data']) if row else None

    def dashboard(self, session):
        profile = self._profile(session)
        documents = [self._doc_summary(d) for d in self._docs(session)]
        app_data = self.list_applications(session)
        with self.connect() as db:
            pending = []
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
            for row in db.execute("SELECT * FROM pending WHERE session=? AND state='pending' AND created>=? ORDER BY created", (session, cutoff)):
                pending.append(json.loads(row['payload']) | {'id': row['id']})
            questions = [json.loads(r['data']) for r in db.execute('SELECT data FROM questions WHERE session=? AND target=? AND revision=? ORDER BY position', (session, profile['role'], profile['revision']))]
            answers = [json.loads(r['data']) for r in db.execute('SELECT a.data FROM answers a JOIN questions q ON q.id=a.question_id WHERE a.session=? AND q.target=? AND q.revision=? ORDER BY q.position', (session, profile['role'], profile['revision']))]
            exports = [self._export_row(dict(r)) for r in db.execute('SELECT * FROM exports WHERE session=? ORDER BY created DESC LIMIT 30', (session,))]
        return {'profile': {k: profile[k] for k in ('role', 'stage', 'city', 'background')}, 'mode': profile['mode'],
                'documents': documents, 'applications': app_data['applications'], 'pending': pending,
                'interview': {'questions': questions, 'answers': answers}, 'latest_match': self._cached(session, 'match'),
                'latest_resume': self._cached(session, 'resume'), 'exports': exports, 'deadlines': app_data['deadlines'],
                'today': date.today().isoformat(), 'skill_profile': self.skill_profile(session),
                'opportunities': self.list_opportunities(session)['opportunities'],
                'latest_recommendations': self._cached(session, 'recommendations')}

    def skill_profile(self, session):
        profile = self._profile(session)
        return profile_from_documents(self._docs(session, PERSONAL_KINDS), profile['mode'] == 'demo')

    def list_opportunities(self, session):
        self._ensure(session)
        with self.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM opportunities WHERE session=? ORDER BY created,id', (session,))]
        today = date.today().isoformat()
        for row in rows:
            row.pop('session', None)
            row['sample'] = bool(row['sample'])
            row['expired'] = bool(row['deadline'] and row['deadline'] < today)
        return {'opportunities': rows, 'note': '仅使用当前档案中手动录入的岗位与公司资料；没有抓取或核实招聘网站。'}

    def save_opportunity(self, session, company, role, jd, company_info='', url='', deadline='', city='', source='', opportunity_id=''):
        profile = self._profile(session, True)
        if profile['mode'] == 'demo':
            raise BusinessError('当前是虚构示例档案。请新建真实档案，再录入真实岗位，避免与示例混合。')
        return self._save_opportunity(session, company, role, jd, company_info, url, deadline, city, source, opportunity_id)

    def _save_opportunity(self, session, company, role, jd, company_info='', url='', deadline='', city='', source='', opportunity_id='', sample=False):
        self._profile(session, True)
        values = {'company': text_field(company, '公司名', 160, True), 'role': text_field(role, '岗位名', 160, True),
                  'jd': text_field(jd, '岗位 JD', 20000, True), 'company_info': text_field(company_info, '公司资料', 10000),
                  'url': text_field(url, '投递链接', 2000), 'deadline': text_field(deadline, '截止日期', 10),
                  'city': text_field(city, '岗位城市', 100), 'source': text_field(source, '岗位来源', 500)}
        if values['url']:
            try:
                parsed = urlparse(values['url'])
                valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
                         and not any(c.isspace() or ord(c) < 32 for c in values['url']))
                if parsed.port is not None and not 1 <= parsed.port <= 65535:
                    valid = False
            except ValueError:
                valid = False
            if not valid:
                raise BusinessError('投递链接必须为不含账号密码的完整 http/https 地址。')
        if values['deadline']:
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', values['deadline']):
                    raise ValueError()
                date.fromisoformat(values['deadline'])
            except ValueError:
                raise BusinessError('截止日期必须是有效的 YYYY-MM-DD；不确定时留空。') from None
        if not isinstance(opportunity_id, str) or (opportunity_id and not re.fullmatch(r'[a-f0-9]{32}', opportunity_id)):
            raise BusinessError('岗位编号无效。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM opportunities WHERE session=? AND id=?', (session, opportunity_id)).fetchone() if opportunity_id else None
            if opportunity_id and old is None:
                raise BusinessError('没有找到当前档案中的岗位。')
            count, size = db.execute('SELECT count(*),coalesce(sum(length(jd)+length(company_info)),0) FROM opportunities WHERE session=?', (session,)).fetchone()
            if old is None and count >= 60:
                raise BusinessError('每个档案最多保存 60 个岗位，请先整理岗位库。')
            old_size = len(old['jd']) + len(old['company_info']) if old else 0
            if size - old_size + len(values['jd']) + len(values['company_info']) > 1_000_000:
                raise BusinessError('岗位与公司资料合计最多 100 万字。')
            row = values | {'id': opportunity_id or uuid.uuid4().hex, 'session': session,
                            'created': old['created'] if old else now(), 'updated': now(), 'sample': bool(old['sample']) if old else bool(sample)}
            db.execute('INSERT OR REPLACE INTO opportunities(id,session,company,role,jd,company_info,url,deadline,city,source,created,updated,sample) '
                       'VALUES(:id,:session,:company,:role,:jd,:company_info,:url,:deadline,:city,:source,:created,:updated,:sample)', row)
            db.execute("DELETE FROM cache WHERE session=? AND kind='recommendations'", (session,))
        row.pop('session')
        row['expired'] = bool(row['deadline'] and row['deadline'] < date.today().isoformat())
        return {'opportunity': row, 'message': '岗位已保存到当前档案。重新推荐将使用最新 JD 与公司资料；没有提交投递。'}

    def delete_opportunity(self, session, opportunity_id):
        self._ensure(session)
        if not isinstance(opportunity_id, str) or not re.fullmatch(r'[a-f0-9]{32}', opportunity_id):
            raise BusinessError('岗位编号无效。')
        with self.connect() as db:
            row = db.execute('SELECT id FROM opportunities WHERE session=? AND id=?', (session, opportunity_id)).fetchone()
            if not row:
                raise BusinessError('没有找到当前档案中的岗位。')
            db.execute('DELETE FROM opportunities WHERE session=? AND id=?', (session, opportunity_id))
            db.execute("DELETE FROM cache WHERE session=? AND kind='recommendations'", (session,))
        return {'deleted': opportunity_id, 'message': '岗位已从当前岗位库删除，历史导出文件和投递台账不变。'}

    def recommend_jobs(self, session, limit=10, include_expired=False):
        if type(limit) is not int or not 1 <= limit <= 60 or type(include_expired) is not bool:
            raise BusinessError('推荐数量须为 1～60 的整数，过期筛选须为布尔值。')
        target = self._profile(session, True)
        profile = self.skill_profile(session)
        rows = self.list_opportunities(session)['opportunities']
        excluded = sum(row['expired'] for row in rows) if not include_expired else 0
        candidates = [recommend_one(profile, target, row) for row in rows if include_expired or not row['expired']]
        candidates.sort(key=lambda row: (row['score'] is None, -(row['score'] or 0), row['expired'], row['company'], row['role'], row['id']))
        result = {'profile': profile, 'recommendations': candidates[:limit], 'total': len(candidates),
                  'excluded_expired': excluded, 'note': RECOMMENDATION_NOTE, 'generated_at': now(),
                  'limit': limit, 'include_expired': include_expired}
        self._cache(session, 'recommendations', result)
        return result

    def export_recommendations(self, session, format='csv', limit=10, include_expired=False):
        if format not in ('csv', 'xlsx'):
            raise BusinessError('推荐清单支持 CSV 或 XLSX。')
        result = self.recommend_jobs(session, limit, include_expired)
        if not result['recommendations']:
            raise BusinessError('当前筛选没有可导出的岗位。请先录入岗位，或检查截止日期。')
        rows = recommendation_rows(result)
        if format == 'csv':
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer)
            writer.writerows([[safe_csv(value) for value in row] for row in rows])
            data = buffer.getvalue().encode('utf-8-sig')
        else:
            data = spreadsheet_bytes(rows)
        file = self._write_export(session, 'recommendations', format, '岗位推荐清单 ' + format.upper(), data)
        return {'files': [file], 'count': len(result['recommendations']), 'sample_data': result['profile']['sample_data'],
                'note': '清单按当前数量和过期筛选重新计算；只是本地文件，未投递。' + RECOMMENDATION_NOTE}

    def set_target(self, session, role, stage='校招', city='', background=''):
        self._ensure(session)
        fields = [text_field(role, '目标岗位', 100, True), text_field(stage, '求职阶段', 40, True),
                  text_field(city, '目标城市', 100), text_field(background, '专业背景', 1000)]
        with self.connect() as db:
            old = db.execute('SELECT role,stage,city,background FROM profiles WHERE session=?', (session,)).fetchone()
            db.execute('UPDATE profiles SET role=?,stage=?,city=?,background=? WHERE session=?', (*fields, session))
            if list(old) != fields:
                self._invalidate(db, session)
        return {'profile': dict(zip(('role', 'stage', 'city', 'background'), fields)), 'message': '目标已保存。请补充你的简历或项目材料，以及该岗位的 JD。'}

    def add_document(self, session, title, kind, text):
        profile = self._profile(session)
        if profile['mode'] == 'demo':
            raise BusinessError('当前是虚构示例档案。请点击“新建真实档案”，避免把示例经历混进你的简历。')
        return self._add_document(session, title, kind, text, False)

    def _add_document(self, session, title, kind, text, sample):
        title = text_field(title, '材料标题', 160, True)
        text = text_field(text, '材料正文', MAX_TEXT_CHARS, True)
        if kind not in KINDS:
            raise BusinessError('材料类型无效。')
        with self.connect() as db:
            size = db.execute('SELECT count(*),coalesce(sum(length(text)),0) FROM documents WHERE session=?', (session,)).fetchone()
            if size[0] >= 60 or size[1] + len(text) > 1_000_000:
                raise BusinessError('每个档案最多 60 份材料、合计 100 万字。请先整理已有材料。')
            doc = {'id': uuid.uuid4().hex, 'session': session, 'title': title, 'kind': kind, 'text': text,
                   'created': now(), 'sample': bool(sample)}
            db.execute('INSERT INTO documents VALUES(:id,:session,:title,:kind,:text,:created,:sample)', doc)
            self._invalidate(db, session)
        return {'document': self._doc_summary(doc), 'message': '材料已加入本地资料库。旧匹配结果已清除，下一次分析将使用新材料。'}

    def import_document(self, session, filename, kind, content_base64):
        try:
            text = parse_document(filename, content_base64)
        except DocumentError as exc:
            raise BusinessError(str(exc)) from None
        return self.add_document(session, Path(filename.replace('\\', '/')).name, kind, text)

    def delete_document(self, session, document_id):
        self._ensure(session)
        with self.connect() as db:
            row = db.execute('SELECT title FROM documents WHERE id=? AND session=?', (document_id, session)).fetchone()
            if not row:
                raise BusinessError('没有找到当前档案中的这份材料。')
            db.execute('DELETE FROM documents WHERE id=? AND session=?', (document_id, session))
            self._invalidate(db, session)
        return {'deleted': document_id, 'message': '材料已删除，旧匹配和简历草稿已失效。已导出的历史文件不会被改写。'}

    def load_demo(self, session):
        profile = self._profile(session)
        with self.connect() as db:
            occupied = any(db.execute(f'SELECT 1 FROM {table} WHERE session=? LIMIT 1', (session,)).fetchone() for table in ('documents', 'applications', 'opportunities'))
            if occupied or profile['role'] or profile['mode'] == 'demo':
                raise BusinessError('示例只能载入空档案。请先新建档案，再选择体验示例。')
            db.execute("UPDATE profiles SET mode='demo' WHERE session=?", (session,))
        self.set_target(session, 'Python 后端开发实习生', '实习', '上海', '计算机专业学生（虚构教学样例）')
        docs = [
            ('林同学的简历（虚构示例）', 'resume', '姓名：林同学（虚构教学样例）\n求职方向：Python 后端开发实习生\n教育背景：示例大学，计算机科学与技术本科，2027 年毕业（虚构）\n技能：使用 Python、SQLite、Git；能编写 HTTP API 和 unittest 测试。\n项目：校园志愿服务原型，使用 Python 与 SQLite 实现查询和报名待确认功能。\n尚未使用 Docker，Linux 部署经验待补充。'),
            ('志愿服务项目说明（虚构示例）', 'project', '项目名称：校园志愿服务原型\nS 背景：课程实践中，需要整理校园志愿活动查询和报名记录。\nT 任务：负责后端接口、数据表设计和报名确认流程。\nA 行动：使用 Python 编写 HTTP API，使用 SQLite 保存活动、报名和会话；使用 Git 管理代码，使用 unittest 编写业务测试。\nA 行动：通过 Ollama 调用 Qwen 本地模型，由工具调用连接业务函数。\nR 结果：完成可在本机演示的查询、待确认报名和记录导出流程。测试数量和真实用户反馈【需确认】。'),
            ('星桥科技 Python 实习 JD（虚构示例）', 'jd', '星桥科技（虚构单位）招聘 Python 后端开发实习生。\n岗位要求：Python 编程基础；SQL 数据库；Git 版本控制；HTTP API 开发；单元测试；Linux 基础；Docker 容器使用。\n职责：根据需求编写接口、维护数据表、参与测试和代码评审。\n本 JD 为课堂演示编写，不是真实招聘信息。'),
            ('本周校招说明（虚构示例）', 'notice', f'虚构校招通知：星桥科技 Python 实习生的材料截止日期为 {(date.today() + timedelta(days=2)).isoformat()}。\n材料包括简历和项目说明。请以本人核实后的官方通知为准，此处仅为教学样例。')]
        for title, kind, body in docs:
            self._add_document(session, title, kind, body, True)
        demo_opportunities = [
            ('星桥科技（虚构示例）', 'Python 后端实习生', '岗位要求：Python；SQLite；Git；HTTP API；单元测试。\n加分项：Linux；Docker。\n计算机相关专业本科在读。', '虚构公司资料：团队开发教育信息化服务，使用 Python、SQLite，重视技术文档与代码评审。', '上海', 7),
            ('晨屿软件（虚构示例）', 'Java 后端实习生', '岗位要求：Java；Spring Boot；MySQL；Git。\n加分项：软件测试。\n学历要求本科。', '虚构公司资料：开发电商平台，技术栈为 Java、Spring Boot、MySQL。', '杭州', 12),
            ('云桐终端（虚构示例）', '鸿蒙应用开发实习生', '岗位要求：鸿蒙开发；ArkTS；Git。\n加分项：HTTP API；技术文档。', '虚构公司资料：探索智能家居与鸿蒙开发，需要设备接入实践。', '深圳', 15),
        ]
        for company, role, jd, company_info, city, days in demo_opportunities:
            self._save_opportunity(session, company, role, jd, company_info=company_info, city=city,
                                   deadline=(date.today() + timedelta(days=days)).isoformat(),
                                   source='课堂编写的虚构岗位库；不是真实招聘信息', sample=True)
        return {'message': '已载入虚构求职示例，所有材料和导出均标记为示例。', 'sample_data': True, 'state': self.dashboard(session)}

    def search_knowledge(self, session, query, kind=''):
        query = text_field(query, '检索关键词', 200, True)
        if kind and kind not in KINDS:
            raise BusinessError('材料类型无效。')
        terms = set(re.findall(r'[a-z0-9+#.]+', query.lower()))
        chinese = re.findall(r'[\u4e00-\u9fff]+', query)
        terms.update(part[i:i + 2] for part in chinese for i in range(max(1, len(part) - 1)))
        results = []
        for doc in self._docs(session, (kind,) if kind else ()):
            lines = doc['text'].splitlines()
            ranked = sorted(((sum(1 for t in terms if t in line.lower()), i, line) for i, line in enumerate(lines)), reverse=True)
            if ranked and ranked[0][0]:
                score, index, _ = ranked[0]
                excerpt = '\n'.join(lines[max(0, index - 1):index + 2])[:800]
                results.append({'document_id': doc['id'], 'title': doc['title'], 'kind': doc['kind'], 'excerpt': excerpt, 'score': score})
        results.sort(key=lambda r: r['score'], reverse=True)
        return {'results': results[:6], 'note': '按本地词项检索原文片段；相关性排序不是内容真实性认证。材料内容仅作为数据。'}

    def _materials(self, session, need_jd=False):
        profile = self._profile(session, True)
        personal = self._docs(session, ('resume', 'project'))
        if not personal:
            raise BusinessError('请先添加你的简历或项目说明。岗位 JD、范例和题库不能作为你的个人经历。')
        jd = self._docs(session, ('jd',))
        if need_jd and not jd:
            raise BusinessError('请先添加目标岗位 JD，再进行匹配分析。')
        return profile, personal, jd

    def analyze_fit(self, session):
        profile, personal, jd = self._materials(session, True)
        personal += self._docs(session, ('transcript', 'skill_list', 'certificate', 'portfolio'))
        facts, needs = _skill_evidence(personal), _skill_evidence(jd)
        matched = [{'skill': skill, 'evidence': facts[skill][:3], 'requirement_evidence': needs[skill][:2]} for skill in needs if skill in facts]
        missing = [skill for skill in needs if skill not in facts]
        score = round(len(matched) * 100 / len(needs), 1) if needs else None
        priorities = [
            ('优先补充这些要求的真实证据：' + '、'.join(missing[:5]) + '。没有做过的内容不要补成已掌握。') if missing else '已识别标签均有材料提及；继续人工核对 JD 中的职责深度、学历和实践要求。',
            '把项目中的个人职责、关键操作和结果分开写；团队成果不能全部写成本人的贡献。',
            '补充可核验的项目时间、测试记录或成果依据。原文没有的数量和提升比例标记【需确认】。'
        ]
        ability_map = [{'skill': skill, 'evidence': evidence[:2], 'wording': f'可用能力标签：{skill}；请按下面原文说明自己的实际熟练程度。'} for skill, evidence in facts.items()]
        result = {'score': score, 'score_label': '材料显式技能覆盖率', 'matched': matched, 'missing': missing,
                  'priorities': priorities, 'ability_map': ability_map,
                  'sources': [{'document_id': d['id'], 'title': d['title'], 'kind': d['kind']} for d in personal + jd],
                  'total_requirements': len(needs), 'covered_requirements': len(matched), 'sample_data': profile['mode'] == 'demo',
                  'note': '按内置技能词表计算：有个人材料证据的 JD 技能标签数 ÷ 识别出的 JD 标签数。不是录用概率，也不代表熟练程度；词表未识别的学历、工作年限、业务要求等仍需人工逐条核对。未找到证据不等于你不具备该能力。'}
        self._cache(session, 'match', result)
        return result

    def build_resume(self, session):
        profile, personal, jd = self._materials(session)
        resumes = [d for d in personal if d['kind'] == 'resume']
        projects = [d for d in personal if d['kind'] == 'project']
        inferred_blocks = [block for doc in resumes for block in _resume_project_blocks(doc)]
        projects += inferred_blocks
        missing, stars = [], []
        seen_projects = set()
        if not resumes:
            missing.append('姓名、联系方式、教育背景和毕业时间【需确认】')
        for doc in projects:
            star, project_missing = _project_star(doc)
            identity = re.sub(r'\s+', '', star['project']).casefold()
            if identity in seen_projects:
                continue
            seen_projects.add(identity)
            stars.append(star)
            missing += project_missing
        if not projects:
            missing.append('项目的背景、个人职责、具体行动和可核验结果【需确认】；建议单独添加项目说明。')
        raw_personal = '\n'.join(d['text'] for d in personal)
        for label, pattern in [('联系方式', r'\b1[3-9]\d{9}\b|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|联系方式\s*[:：].+'), ('项目时间', r'20\d{2}[年./-]\d{1,2}')]:
            if not re.search(pattern, raw_personal):
                missing.append(label + '【需确认】')
        lines = ['# 求职简历初稿', '', f'目标岗位：{profile["role"]}', f'求职阶段：{profile["stage"]}']
        if profile['city']:
            lines.append('目标城市：' + profile['city'])
        if profile['mode'] == 'demo':
            lines += ['', '**虚构教学示例，请勿作为本人经历投递。**']
        if resumes:
            lines += ['', '## 基本信息与已有经历']
            for doc in resumes:
                project_lines = {line for block in _resume_project_blocks(doc) for line in evidence_lines(block['text'])}
                lines += ['', *[line for line in evidence_lines(doc['text']) if line not in project_lines]]
        else:
            lines += ['', '## 基本信息', '姓名：【需确认】', '联系方式：【需确认】', '教育背景与毕业时间：【需确认】']
        lines += ['', '## 项目经历（可直接修改的表述）']
        for star in stars:
            lines += ['', '### ' + star['project'], star['paragraph']]
            if star['supplement']:
                lines += ['原文补充信息：' + '；'.join(star['supplement'])]
        if not stars:
            lines.append('【需确认】请补充单独的项目说明，按背景、个人职责、行动、结果填写。')
        if missing:
            lines += ['', '## 投递前需补全', *['- ' + item for item in dict.fromkeys(missing)]]
        result = {'title': profile['role'] + ' · 简历初稿', 'text': '\n'.join(lines), 'star': stars,
                  'missing': list(dict.fromkeys(missing)), 'sources': [{'document_id': d['id'], 'title': d['title']} for d in personal],
                  'sample_data': profile['mode'] == 'demo', 'note': '按标签和明确句式将个人材料整理成 STAR，再连接为项目经历段落；只调整前缀与顺序，不补造数字、技术名或成果。source.quotes 保留原文依据。含【需确认】时不能直接视为最终投递版。'}
        self._cache(session, 'resume', result)
        return result

    def start_interview(self, session, count=10):
        profile = self._profile(session, True)
        if type(count) is not int or not 1 <= count <= 10:
            raise BusinessError('每轮面试题数应为 1～10。')
        jd = self._docs(session, ('jd',))
        if not jd:
            raise BusinessError('请先添加目标岗位 JD，面试题才有明确岗位依据。')
        with self.connect() as db:
            existing = [json.loads(r['data']) for r in db.execute('SELECT data FROM questions WHERE session=? AND target=? AND revision=? ORDER BY position', (session, profile['role'], profile['revision']))]
        if existing:
            return {'questions': existing, 'note': '继续当前题集，已作答记录保留。修改目标或材料后会创建新题集。'}
        requirements = _skill_evidence(jd)
        skills = list(requirements)
        source = {'document_id': jd[0]['id'], 'title': jd[0]['title'], 'quote': jd[0]['text'][:240]}
        templates = [
            (f'请用一分钟介绍自己，说明你与“{profile["role"]}”相关的真实经历。', '岗位相关性', source),
            ('选择一个真实项目，用 STAR 说明背景、你的职责、关键行动和结果。', '项目表达', '内置 STAR 表达练习模板'),
            ('项目中最难定位的问题是什么？你如何复现、排查和验证修复？', '问题解决', '内置项目复盘练习模板'),
        ]
        for skill in skills[:4]:
            templates.append((f'JD 提到了“{skill}”。请结合真实项目说明你用它完成过什么，并说明尚不熟悉的部分。', skill, requirements[skill][0]))
        extras = [
            ('你的工作与团队其他成员如何分工？请区分个人贡献和团队成果。', '协作与个人贡献'),
            ('你如何证明项目结果有效？请说明测试、演示或记录依据，没有数据时如实说明。', '成果依据'),
            ('如果重新做这个项目，你会优先改进什么，为什么？', '复盘与取舍'),
            ('对照 JD，你目前最需要补充哪项能力？计划如何用一个小项目验证学习效果？', '差距与学习计划'),
            ('当需求不明确时，你会如何与需求方澄清目标和验收条件？', '需求沟通'),
            ('你想向面试官了解哪些岗位职责、指导安排或考核方式？', '反向提问'),
            ('请说明一个技术选择的理由、限制，以及你考虑过的替代方案。', '技术表达'),
        ]
        templates += [(q, focus, '内置通用面试练习模板') for q, focus in extras]
        bank = self._docs(session, ('interview',))
        bank_questions = [(line, '用户题库', {'document_id': d['id'], 'title': d['title'], 'quote': line}) for d in bank for line in evidence_lines(d['text']) if ('?' in line or '？' in line) and len(line) <= 500]
        if bank_questions:
            templates = templates[:3] + bank_questions[:3] + templates[3:]
        questions = [{'id': uuid.uuid4().hex, 'question': q, 'focus': focus, 'source': src} for q, focus, src in templates[:count]]
        with self.connect() as db:
            for position, question in enumerate(questions):
                db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?)', (question['id'], session, profile['role'], profile['revision'], position, json.dumps(question, ensure_ascii=False), now()))
        return {'questions': questions, 'note': '题目依据 JD 和通用表达模板生成，不代表企业真实题目。逐题作答后可得到表达完整度评分。'}

    def review_answer(self, session, question_id, answer):
        self._ensure(session)
        answer = text_field(answer, '面试回答', 6000, True)
        with self.connect() as db:
            row = db.execute('SELECT data FROM questions WHERE id=? AND session=?', (question_id, session)).fetchone()
        if not row:
            raise BusinessError('未找到当前档案中的面试题，请先开始面试。')
        question = json.loads(row['data'])
        sentences = [s.strip() for s in re.split(r'[。！？\n]', answer) if s.strip()]
        checks = [
            ('情境与任务', r'背景|目标|任务|需求|当时|为了解决|项目中', '补充当时的情境和你承担的任务。'),
            ('个人行动', r'我(?:负责|使用|实现|设计|编写|排查|验证|完成|分析|改进)|本人|个人负责', '说明“我做了什么”，区分团队工作。'),
            ('具体证据', r'测试|日志|代码|截图|演示|记录|复现|对照|数据|\d', '给出可核验的测试、记录或演示依据，不编造数字。'),
            ('结果说明', r'结果|最终|实现了|解决了|完成了|验证了|未能|失败|仍然', '补充结果，失败或尚未完成也可以如实说明。'),
            ('复盘改进', r'改进|不足|反思|下次|下一次|重新|后续|取舍|局限|还需', '说明限制、取舍或下一次改进。')]
        rubric, feedback = [], []
        for label, pattern, tip in checks:
            evidence = next((s for s in sentences if re.search(pattern, s)), '')
            points = 2 if evidence and len(evidence) >= 12 else 1 if evidence else 0
            rubric.append({'dimension': label, 'score': points, 'max_score': 2, 'reason': ('回答依据：' + evidence[:200]) if evidence else tip})
            if points < 2:
                feedback.append(tip)
        personal = '\n'.join(d['text'] for d in self._docs(session, PERSONAL_KINDS))
        unknown_numbers = [n for n in re.findall(r'\d+(?:\.\d+)?%?', answer) if n not in personal]
        if unknown_numbers:
            feedback.append('回答中的这些数字尚未在个人材料中找到依据：【需确认】' + '、'.join(dict.fromkeys(unknown_numbers)) + '。请提供记录，不要为了表达完整而编造。')
        if not feedback:
            feedback.append('表达结构较完整。请继续核对技术细节和证据，规则评分不验证经历真实性或技术正确性。')
        outline = '\n'.join(label + '：' + (next((s for s in sentences if re.search(pattern, s)), '【需确认】' + tip)) for label, pattern, tip in checks)
        result = {'question_id': question_id, 'question': question['question'], 'score': sum(r['score'] for r in rubric), 'max_score': 10,
                  'rubric': rubric, 'feedback': feedback, 'improved_outline': outline, 'answer': answer,
                  'note': '此分数是五项表达与证据完整度规则分，不是招聘评价，也不判断技术答案正确性。改写提纲只重组本次回答原文。'}
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO answers VALUES(?,?,?,?)', (question_id, session, json.dumps(result, ensure_ascii=False), now()))
        return result

    def prepare_application(self, session, company, role, url='', status='待投递', deadline='', notes='', application_id=''):
        self._profile(session, True)
        company, role = text_field(company, '公司名称', 150, True), text_field(role, '岗位名称', 150, True)
        url, notes = text_field(url, '岗位链接', 1200), text_field(notes, '备注', 2000)
        application_id = text_field(application_id, '记录编号', 32)
        if status not in STATUSES:
            raise BusinessError('投递状态无效。')
        deadline = text_field(deadline, '截止日期', 10)
        if deadline:
            try:
                if date.fromisoformat(deadline).isoformat() != deadline:
                    raise ValueError()
            except ValueError:
                raise BusinessError('截止日期请使用 YYYY-MM-DD。') from None
        if url:
            try:
                parsed = urlparse(url)
                valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
                parsed.port
            except ValueError:
                valid = False
            if not valid or any(c in url for c in '\r\n\t'):
                raise BusinessError('岗位链接必须是普通 http/https 链接，不能包含账号密码。')
        previous_updated = ''
        with self.connect() as db:
            if application_id:
                old = db.execute('SELECT * FROM applications WHERE id=? AND session=?', (application_id, session)).fetchone()
                if not old:
                    raise BusinessError('没有找到当前档案中的这条投递记录。')
                previous_updated = old['updated']
            payload = {'company': company, 'role': role, 'url': url, 'status': status, 'deadline': deadline,
                       'notes': notes, 'application_id': application_id, 'previous_updated': previous_updated}
            pid = uuid.uuid4().hex
            db.execute('INSERT INTO pending VALUES(?,?,?,?,?)', (pid, session, json.dumps(payload, ensure_ascii=False), 'pending', now()))
        return {'status': 'awaiting_user_confirmation', 'pending': payload | {'id': pid},
                'message': '请在页面核对并确认保存。这里只更新本地投递台账，不会向公司提交简历或发送消息。'}

    def confirm_application(self, session, pending_id, accept):
        self._ensure(session)
        if type(accept) is not bool:
            raise BusinessError('确认选择必须是 true 或 false。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute("SELECT * FROM pending WHERE id=? AND session=? AND state='pending'", (pending_id, session)).fetchone()
            if not pending:
                raise BusinessError('待确认记录不存在、已处理或不属于当前档案。')
            if datetime.fromisoformat(pending['created']) < datetime.now(timezone.utc) - timedelta(minutes=15):
                raise BusinessError('待确认记录已过期，请重新准备。')
            if not accept:
                db.execute("UPDATE pending SET state='cancelled' WHERE id=?", (pending_id,))
                return {'status': 'cancelled', 'message': '已取消，本地投递台账未变更。'}
            payload = json.loads(pending['payload'])
            identity = json.dumps([payload['company'].casefold(), payload['role'].casefold(), payload['url'].rstrip('/')], ensure_ascii=False)
            stamp = now()
            app_id = payload['application_id'] or uuid.uuid4().hex
            try:
                if payload['application_id']:
                    old = db.execute('SELECT updated FROM applications WHERE id=? AND session=?', (app_id, session)).fetchone()
                    if not old or old['updated'] != payload['previous_updated']:
                        raise BusinessError('这条记录已被更新，请重新核对后准备变更。')
                    db.execute('UPDATE applications SET company=?,role=?,url=?,status=?,deadline=?,notes=?,updated=?,identity_key=? WHERE id=? AND session=?',
                               (*[payload[k] for k in ('company', 'role', 'url', 'status', 'deadline', 'notes')], stamp, identity, app_id, session))
                else:
                    db.execute('INSERT INTO applications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (app_id, session, *[payload[k] for k in ('company', 'role', 'url', 'status', 'deadline', 'notes')], stamp, stamp, identity))
            except sqlite3.IntegrityError:
                raise BusinessError('这家公司、岗位和链接已存在，请选择原记录更新状态。') from None
            db.execute("UPDATE pending SET state='confirmed' WHERE id=?", (pending_id,))
            db.execute('INSERT INTO audit(session,action,detail,created) VALUES(?,?,?,?)', (session, 'confirm_application', app_id, stamp))
            record = dict(db.execute('SELECT * FROM applications WHERE id=?', (app_id,)).fetchone())
        record.pop('identity_key', None)
        record.pop('session', None)
        return {'status': 'saved', 'record': record, 'message': '已保存到本地投递台账；没有向公司投递或发送材料。'}

    def list_applications(self, session, status='', due_this_week=False):
        self._ensure(session)
        if status and status not in STATUSES:
            raise BusinessError('投递状态无效。')
        if type(due_this_week) is not bool:
            raise BusinessError('本周筛选应为 true 或 false。')
        today = date.today()
        monday, sunday = today - timedelta(days=today.weekday()), today + timedelta(days=6 - today.weekday())
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM applications WHERE session=? ORDER BY updated DESC', (session,))]
        for row in rows:
            row.pop('session', None)
            row.pop('identity_key', None)
        deadlines = []
        for row in rows:
            if row['deadline'] and row['status'] not in ('Offer', '未通过', '已撤回'):
                deadlines.append({'date': row['deadline'], 'title': row['company'] + ' · ' + row['role'], 'source': '本地投递台账', 'application_id': row['id'], 'overdue': row['deadline'] < today.isoformat()})
        for doc in self._docs(session, ('notice',)):
            for line in doc['text'].splitlines():
                if not re.search(r'截止|截至|报名结束|申请结束|投递结束', line):
                    continue
                for match in re.finditer(r'(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?', line):
                    try:
                        day = date(*map(int, match.groups())).isoformat()
                    except ValueError:
                        continue
                    deadlines.append({'date': day, 'title': doc['title'], 'source': '材料原文', 'document_id': doc['id'], 'quote': line[:500], 'overdue': day < today.isoformat()})
        deadlines.sort(key=lambda d: (d['date'], d['title']))
        if status:
            rows = [r for r in rows if r['status'] == status]
        if due_this_week:
            rows = [r for r in rows if r['deadline'] and monday.isoformat() <= r['deadline'] <= sunday.isoformat()]
            deadlines = [d for d in deadlines if monday.isoformat() <= d['date'] <= sunday.isoformat()]
        return {'applications': rows, 'deadlines': deadlines, 'today': today.isoformat(), 'week_start': monday.isoformat(), 'week_end': sunday.isoformat(),
                'note': '只读取本地台账和通知里带完整年月日的截止文字。年份不完整、相对日期或通知变更需本人核对；不会联网核实或自动投递。'}

    def _export_row(self, row):
        return {'name': row['name'], 'label': row['label'], 'url': f'/downloads/{row["name"]}?session={row["session"]}', 'created': row.get('created', '')}

    def _write_export(self, session, prefix, suffix, label, data):
        name = prefix + '_' + uuid.uuid4().hex + '.' + suffix
        path = self.exports / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        row = {'name': name, 'session': session, 'label': label, 'kind': prefix, 'created': now()}
        with self.connect() as db:
            db.execute('INSERT INTO exports VALUES(:name,:session,:label,:kind,:created)', row)
        return self._export_row(row)

    def export_materials(self, session, kind='bundle'):
        if kind not in ('bundle', 'resume', 'interview', 'applications'):
            raise BusinessError('请选择简历、面试、投递记录或完整材料包。')
        profile = self._profile(session, True)
        files, missing = [], []
        sample = '虚构教学示例，请勿作为本人经历投递。\n\n' if profile['mode'] == 'demo' else ''
        if kind in ('bundle', 'resume'):
            resume = self.build_resume(session)
            missing += resume['missing']
            sources = '\n'.join('- ' + s['title'] + ' [' + s['document_id'] + ']' for s in resume['sources'])
            content = sample + resume['text'] + '\n\n## 材料依据\n' + sources + '\n\n' + resume['note']
            files.append(self._write_export(session, 'resume', 'md', '简历初稿 Markdown', content))
            status = '待补全：含【需确认】，请核对后再投递。' if missing else '请本人逐项核对原文与事实后使用。'
            body_parts = []
            for line in resume['text'].splitlines():
                if not line:
                    continue
                heading = re.match(r'^(#{1,3})\s+(.+)', line)
                if heading:
                    level = min(len(heading[1]) + 1, 4)
                    body_parts.append(f'<h{level}>' + html.escape(heading[2]) + f'</h{level}>')
                else:
                    body_parts.append('<p>' + html.escape(line) + '</p>')
            body = ''.join(body_parts)
            page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>求职简历初稿</title><style>body{max-width:820px;margin:40px auto;padding:24px;font:16px/1.8 "Microsoft YaHei",sans-serif;color:#182c41}h1{font-size:28px}.notice{background:#fff2d9;padding:14px;border-left:4px solid #b8791f}p{white-space:pre-wrap;margin:8px 0}@media print{body{margin:0;padding:0}.no-print{display:none}}</style><h1>' + html.escape(resume['title']) + '</h1><div class="notice">' + html.escape(sample + status) + '</div><p class="no-print">浏览器打印可另存 PDF。先补全缺项，再检查分页。</p>' + body + '<hr><p>' + html.escape(resume['note']) + '</p></html>'
            files.append(self._write_export(session, 'resume', 'html', '简历初稿 HTML（可打印）', page))
        if kind == 'bundle':
            try:
                match = self.analyze_fit(session)
                score = '未计算' if match['score'] is None else str(match['score']) + '%'
                content = sample + f'# 岗位匹配建议\n\n目标岗位：{profile["role"]}\n\n材料显式技能覆盖率：{score}\n\n' + match['note'] + '\n\n## 三项优先修改\n' + '\n'.join('- ' + p for p in match['priorities'])
                content += '\n\n## 能力与原文证据\n'
                for item in match['ability_map']:
                    content += '\n### ' + item['skill'] + '\n' + '\n'.join('- ' + e['title'] + '：' + e['quote'] for e in item['evidence']) + '\n'
            except BusinessError as exc:
                content = sample + '# 岗位匹配建议\n\n【需确认】' + str(exc)
                missing.append(str(exc))
            files.append(self._write_export(session, 'match', 'md', '岗位匹配与能力映射', content))
        if kind in ('bundle', 'interview'):
            try:
                interview = self.start_interview(session, 10)
                state = self.dashboard(session)['interview']
                answers = {a['question_id']: a for a in state['answers']}
                content = sample + '# 面试练习清单\n\n' + interview['note'] + '\n'
                for i, q in enumerate(interview['questions'], 1):
                    content += f'\n## {i}. {q["question"]}\n\n考察方向：{q["focus"]}\n'
                    if q['id'] in answers:
                        a = answers[q['id']]
                        content += f'\n我的回答：{a["answer"]}\n\n表达完整度：{a["score"]}/10\n\n改进建议：' + '；'.join(a['feedback']) + '\n\n' + a['note'] + '\n'
                    else:
                        content += '\n我的回答：【待练习】\n'
            except BusinessError as exc:
                if kind == 'interview':
                    raise
                content = sample + '# 面试练习清单\n\n【需确认】' + str(exc)
                missing.append(str(exc))
            files.append(self._write_export(session, 'interview', 'md', '面试清单与练习反馈', content))
        if kind in ('bundle', 'applications'):
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer)
            writer.writerow(['公司', '岗位', '链接', '状态', '截止日期', '备注', '数据性质'])
            for a in self.list_applications(session)['applications']:
                writer.writerow([safe_csv(a[k]) for k in ('company', 'role', 'url', 'status', 'deadline', 'notes')] + ['虚构示例' if sample else '用户记录'])
            files.append(self._write_export(session, 'applications', 'csv', '投递记录表 CSV', buffer.getvalue().encode('utf-8-sig')))
        if kind == 'bundle':
            buffer = io.BytesIO()
            labels = {'resume.md': '简历初稿.md', 'resume.html': '简历初稿.html', 'match.md': '岗位匹配建议.md', 'interview.md': '面试清单.md', 'applications.csv': '投递记录表.csv'}
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for file in files:
                    prefix, ext = file['name'].split('_')[0], file['name'].rsplit('.', 1)[1]
                    archive.write(self.exports / file['name'], labels[prefix + '.' + ext])
                archive.writestr('使用说明.txt', sample + '这是本地生成的求职材料，未向任何公司发送。\n含【需确认】的内容应补全或删除，不能编造。\n匹配分是词表覆盖率，面试分是表达完整度分。\n导出包含个人材料和原文证据，请自行决定分享范围。')
            files.append(self._write_export(session, 'materials', 'zip', '完整求职材料包 ZIP', buffer.getvalue()))
        return {'files': files, 'missing': list(dict.fromkeys(missing)), 'sample_data': profile['mode'] == 'demo',
                'note': '文件已在本地生成，没有投递或发送。请补全【需确认】并人工校对；示例档案的文件不能作为本人经历使用。'}

    def resolve_export(self, session, name):
        self._check_session(session)
        if not isinstance(name, str) or not re.fullmatch(r'(?:resume|match|interview|applications|materials|recommendations)_[a-f0-9]{32}\.(?:md|html|csv|zip|xlsx)', name):
            raise BusinessError('导出文件名不合法。')
        with self.connect() as db:
            row = db.execute('SELECT 1 FROM exports WHERE session=? AND name=?', (session, name)).fetchone()
        path = self.exports / name
        if not row or not path.is_file() or path.resolve().parent != self.exports.resolve():
            raise BusinessError('文件不存在或不属于当前档案。')
        return path

    def load_messages(self, session):
        self._ensure(session)
        with self.connect() as db:
            row = db.execute('SELECT messages FROM sessions WHERE session=?', (session,)).fetchone()
        return json.loads(row['messages']) if row else []

    def save_messages(self, session, messages):
        self._ensure(session)
        if not isinstance(messages, list):
            raise BusinessError('会话消息必须是列表。')
        serialized = json.dumps(messages, ensure_ascii=False)
        if len(serialized) > 2_000_000:
            raise BusinessError('会话过长，请新建档案继续。')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)', (session, serialized))
