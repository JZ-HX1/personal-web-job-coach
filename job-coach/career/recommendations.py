"""有来源的岗位推荐：同义词归一化和有限的相关技术映射，不预测录取。"""
import csv
import io
import re
import zipfile
from datetime import date
from xml.sax.saxutils import escape

NOTE = '分数是已识别岗位要求的加权材料适配度，不是录取概率；不验证技能熟练度。未识别要求、学历年限及公司实际情况仍需人工核对。'
PROFILE_NOTE = '画像仅显示材料中可追溯的技能标签。雷达值为支持词表中的证据标签覆盖率，不代表能力强弱；无证据显示未评估。'
DIMENSIONS = ('技术能力', '工具实践', '沟通协作', '项目管理', '行业知识')


def term(value):
    return r'(?<![a-z0-9_])(?:' + value + r')(?![a-z0-9_])'


# Canonical names are deliberately granular. Spring is not Spring Boot; using an
# AI tool is not evidence of model training, and React does not prove JavaScript.
SPECS = {
    'Python': (term('python'), '技术能力'),
    'Java': (term('java'), '技术能力'),
    'JavaScript': (term('javascript|js'), '技术能力'),
    'TypeScript': (term('typescript|ts'), '技术能力'),
    'C/C++': (term(r'c\+\+') + r'|C\s*语言', '技术能力'),
    'SQL': (term('sql') + r'|关系型数据库', '技术能力'),
    'MySQL': (term('mysql'), '技术能力'),
    'PostgreSQL': (term('postgresql|postgres'), '技术能力'),
    'SQLite': (term('sqlite'), '技术能力'),
    'Spring': (term(r'spring(?![\s-]*boot)'), '技术能力'),
    'Spring Boot': (term(r'spring[\s-]*boot'), '技术能力'),
    'Django': (term('django'), '技术能力'),
    'Flask': (term('flask'), '技术能力'),
    'FastAPI': (term('fastapi'), '技术能力'),
    'Vue': (term(r'vue(?:\.js)?'), '技术能力'),
    'React': (term(r'react(?:\.js)?'), '技术能力'),
    'HTTP/API': (term('http|https|restful|rest|api') + r'|接口开发|接口设计', '技术能力'),
    '软件测试': (r'单元测试|集成测试|接口测试|自动化测试|' + term('pytest|unittest'), '技术能力'),
    '数据分析': (r'数据分析|数据清洗|' + term('pandas|numpy'), '技术能力'),
    '算法与数据结构': (r'数据结构|算法设计|算法复杂度|排序算法', '技术能力'),
    '鸿蒙开发': (r'鸿蒙|' + term(r'harmony\s*os|openharmony'), '技术能力'),
    'ArkTS': (term('arkts'), '技术能力'),
    '嵌入式开发': (r'嵌入式|单片机|' + term('stm32|esp32|freertos'), '技术能力'),
    '电路设计': (r'电路设计|PCB|模电|数电', '技术能力'),
    'CAD/机械设计': (r'机械设计|' + term('cad|solidworks'), '技术能力'),
    'Git': (term('git') + r'|版本控制', '工具实践'),
    'Linux': (term('linux'), '工具实践'),
    'Docker': (term('docker'), '工具实践'),
    'Kubernetes': (term('kubernetes|k8s'), '工具实践'),
    'AI 工具应用': (term('ollama|chatgpt|qwen|deepseek|langchain') + r'|AI\s*工具|大模型调用', '工具实践'),
    'Excel': (term('excel') + r'|电子表格', '工具实践'),
    '需求沟通': (r'需求沟通|需求访谈|客户访谈|沟通需求', '沟通协作'),
    '团队协作': (r'团队协作|跨团队协作|跨部门协作|结对编程', '沟通协作'),
    '技术文档': (r'技术文档|项目文档|接口文档|开发文档', '沟通协作'),
    '公开表达': (r'项目答辩|技术分享|公开演讲|技术演讲', '沟通协作'),
    '项目计划': (r'项目计划|任务拆解|排期管理|里程碑管理|进度跟踪', '项目管理'),
    '敏捷协作': (r'敏捷开发|敏捷迭代|' + term('scrum|kanban'), '项目管理'),
    '质量管理': (r'质量管理|风险管理|代码评审|缺陷跟踪', '项目管理'),
    '金融业务': (r'金融业务|银行业务|证券交易业务|支付清算', '行业知识'),
    '医疗业务': (r'医疗业务|医疗信息化|医院业务|电子病历', '行业知识'),
    '电商业务': (r'电商业务|电商订单|电商平台|商品库存', '行业知识'),
    '物联网业务': (r'物联网业务|物联网平台|设备接入|智能家居', '行业知识'),
    '教育业务': (r'教育业务|教育信息化|教学管理|在线教育', '行业知识'),
}
PATTERNS = {name: re.compile(pattern, re.I) for name, (pattern, _) in SPECS.items()}
NEGATIVE = re.compile(r'尚未|未(?:曾)?(?:接触|使用|学习|掌握|做过)|没有(?:使用|掌握|学习|接触|完成)|(?:没有|暂无|缺乏|无).{0,15}(?:经验|基础|能力)|不会|不了解|不熟悉|不熟练|不具备|暂无|待(?:学习|补充|确认|掌握|验证)|计划(?:学习|掌握|使用)|希望(?:学习|掌握)|【需确认】|\b(?:not\s+(?:used?|know|familiar)|no\s+experience|plan\s+to\s+learn)\b', re.I)
INSTRUCTIONS = re.compile(r'忽略(?:之前|以上|系统|所有)|忽略.{0,10}(?:指令|规则)|编造.{0,20}(?:经历|成果|数据)|伪造|system\s*prompt|ignore\s+(?:all|previous)', re.I)
URL_SPAN = re.compile(r'https?://[^\s<>"\x27，,；;。]+', re.I)
# (evidence skill, requested skill) -> conservative transfer credit.
RELATED = {
    ('Spring', 'Spring Boot'): .5, ('Spring Boot', 'Spring'): .5,
    ('MySQL', 'SQL'): .5, ('PostgreSQL', 'SQL'): .5, ('SQLite', 'SQL'): .5,
    ('SQL', 'MySQL'): .5, ('SQL', 'PostgreSQL'): .5, ('SQL', 'SQLite'): .5,
    ('Vue', 'React'): .25, ('React', 'Vue'): .25,
    ('Docker', 'Kubernetes'): .25,
}


def clauses(text):
    for paragraph in text.splitlines():
        # An instruction before a comma must not turn its continuation into a fact.
        if INSTRUCTIONS.search(paragraph):
            continue
        for raw in re.split(r'[，,；;。]', paragraph):
            value = re.sub(r'^\s*(?:[-*•]|\d+[.)、])\s*', '', raw).strip()
            if value and not INSTRUCTIONS.search(value):
                yield value


def skill_match(text, skill):
    url_spans = [(match.start(), match.end()) for match in URL_SPAN.finditer(text)]
    return next((match for match in PATTERNS[skill].finditer(text)
                 if not any(start < match.end() and match.start() < end for start, end in url_spans)), None)


def names_in(text):
    return [name for name in PATTERNS if skill_match(text, name)]


def evidence_excerpt(text, skill):
    match = skill_match(text, skill)
    start = max(0, match.start() - 100) if match and len(text) > 420 else 0
    return text[start:start + 420]


def profile_from_documents(documents, sample_data=False):
    found = {}
    for doc in documents:
        for line in clauses(doc['text']):
            if NEGATIVE.search(line):
                continue
            for name in names_in(line):
                evidence = {'document_id': doc['id'], 'title': doc['title'], 'quote': evidence_excerpt(line, name), 'kind': doc['kind']}
                if evidence not in found.setdefault(name, []) and len(found[name]) < 4:
                    found[name].append(evidence)
    skills = [{'name': name, 'dimension': SPECS[name][1], 'evidence': found[name]} for name in SPECS if name in found]
    dimensions = []
    for name in DIMENSIONS:
        maximum = sum(spec[1] == name for spec in SPECS.values())
        count = sum(skill['dimension'] == name for skill in skills)
        dimensions.append({'name': name, 'score': round(count / maximum * 100, 1) if count else None,
                           'count': count, 'maximum': maximum,
                           'note': f'材料有 {count} / {maximum} 个支持标签的证据；不是能力评分。' if count else '材料中没有相关证据，暂未评估；不表示没有该能力。'})
    return {'skills': skills, 'dimensions': dimensions, 'note': PROFILE_NOTE, 'sample_data': sample_data}


def requirements(text):
    found, unassessed = {}, []
    weighted_lines = []
    section_weight = 2
    for raw in text.splitlines():
        if re.search(r'加分项|优先条件|nice.to.have|preferred', raw, re.I):
            section_weight = 1
        elif re.search(r'任职要求|岗位要求|必须|必需|required|学历要求|职责', raw, re.I):
            section_weight = 2
        for line in clauses(raw):
            weight = 1 if re.search(r'优先|加分|可选|了解即可|nice.to.have|preferred', line, re.I) else section_weight
            weighted_lines.append((line, weight))
    for line, weight in weighted_lines:
        if NEGATIVE.search(line) or re.search(r'不要求|无需|不限|非必须|非必需', line):
            if len(unassessed) < 12:
                unassessed.append(line[:300])
            continue
        skills = names_in(line)
        if len(skills) > 1 and re.search(r'或|任一|任意一种|任选|其中一种|二选一|\bor\b', line, re.I):
            # The scope of natural-language alternatives can be ambiguous. Do not
            # silently turn OR into AND or invent a missing alternative skill.
            message = '任选/或条件未自动评分，请按原文人工核对：' + line[:300]
            if message not in unassessed and len(unassessed) < 12:
                unassessed.append(message)
            continue
        for skill in skills:
            old = found.get(skill)
            if old is None or weight > old['weight']:
                found[skill] = {'skill': skill, 'weight': weight, 'quote': evidence_excerpt(line, skill)}
        qualification = re.search(r'学历|本科|硕士|博士|大专|学位|毕业|应届|工作经验|[零一二三四五六七八九十\d]+\s*年|证书|资格证|英语|雅思|托福|四六级|CET[- ]?[46]|IELTS|TOEFL', line, re.I)
        if qualification and len(unassessed) < 12:
            start = max(0, qualification.start() - 80)
            quote = line[start:start + 300]
            if quote not in unassessed:
                unassessed.append(quote)
        # Unmapped fragments stay visible. They never silently enter the score.
        pieces = re.split(r'[、/]|\s+与\s+|\s+and\s+', line)
        for piece in pieces:
            piece = piece.strip()
            if (piece and not names_in(piece) and len(piece) > 1 and
                    not re.fullmatch(r'(?:任职要求|岗位要求|加分项|优先条件|职责|要求)[:：]?', piece) and piece not in unassessed and len(unassessed) < 12):
                unassessed.append(piece[:300])
    return list(found.values()), unassessed


def learning_actions(skill):
    actions = {
        'Spring Boot': ['学习 Spring Boot 自动配置、依赖注入与 REST 接口。', '把已有 Spring 练习迁移成可运行的 Spring Boot 项目，补充测试与 README；完成后上传真实证据重新匹配。'],
        'Spring': ['练习 Spring 的依赖注入、配置与测试，说明与 Spring Boot 的区别。', '完成小型服务并保存代码、技术说明和个人贡献，再更新材料。'],
        'Docker': ['学习镜像、容器、卷和端口映射。', '为自己的项目编写 Dockerfile，记录构建、运行和排错步骤后补充项目材料。'],
        'Linux': ['练习 Linux 文件权限、进程、日志和常用命令。', '在本机虚拟环境部署已有项目，保留部署说明与排错记录。'],
        'Git': ['练习提交、分支、合并与冲突处理。', '为已有项目保存清晰提交记录，补充一次协作或版本管理说明。'],
        '鸿蒙开发': ['按目标 JD 核对 HarmonyOS / OpenHarmony 版本及开发工具要求。', '完成一个可演示的页面与数据交互项目，记录 ArkTS、组件和设备适配的实际使用。'],
        'Kubernetes': ['先掌握容器基础，再练习 Pod、Deployment、Service 和配置。', '用本地练习集群部署个人项目并记录故障恢复，不把 Docker 经历当作 Kubernetes 经验。'],
        '需求沟通': ['为现有项目整理真实需求访谈、问题清单与决策记录。', '用“背景—我的沟通动作—结果”描述一次实际沟通，未发生的经历不要补写。'],
        '项目计划': ['把一个正在进行的项目拆成任务、负责人、截止日与验收条件。', '完成后用计划与实际对照记录作为证据，不补造管理经历。'],
    }
    return actions.get(skill, [f'对照原 JD 确认 {skill} 要求的深度，学习对应基础概念。',
                               f'在一个与目标岗位相关的小项目中实际使用 {skill}，保存代码、演示或文档；完成后添加材料并重新匹配。'])


def recommend_one(profile, target, opportunity, today=None):
    today = today or date.today()
    skills = {item['name']: item for item in profile['skills']}
    required, unassessed = requirements(opportunity['jd'])
    matched, missing, earned = [], [], 0.0
    for req in required:
        wanted = req['skill']
        actual, credit, relation = (wanted, 1, '同名或同义词归一化') if wanted in skills else ('', 0, '')
        if not actual:
            candidates = [(RELATED[(known, wanted)], known) for known in skills if (known, wanted) in RELATED]
            if candidates:
                credit, actual = max(candidates)
                relation = '相关技术可迁移（部分证据，不等同已掌握目标技能）'
        if actual:
            earned += credit * req['weight']
            matched.append({'requirement': wanted, 'skill': actual, 'relation': relation, 'credit': credit,
                            'weight': req['weight'], 'evidence': skills[actual]['evidence'], 'job_quote': req['quote']})
        if credit < 1:
            missing.append({'skill': wanted, 'actions': learning_actions(wanted), 'job_quote': req['quote'],
                            'partial': bool(credit), 'note': '已有相关技术证据，仍需补目标技术的直接证据。' if credit else '当前个人材料未找到直接证据；不等于本人不会。'})
    denominator = sum(req['weight'] for req in required)
    score = round(100 * earned / denominator, 1) if denominator and skills else None
    warnings = []
    if not skills:
        warnings.append('【需确认】尚无可识别的个人技能证据，暂不评分。请添加简历、项目、技能清单、证书或作品集。')
    if not denominator:
        warnings.append('【需确认】JD 没有可识别的技能要求，暂不评分；请补充具体要求。')
    if unassessed:
        warnings.append('有未纳入评分的要求。学历、工作年限、语言等级、证书及未知技术须逐项人工核对。')
    company_fit = []
    for line in clauses(opportunity.get('company_info', '')):
        if NEGATIVE.search(line):
            continue
        for skill in names_in(line):
            if skill in skills and len(company_fit) < 6:
                company_fit.append({'dimension': SPECS[skill][1], 'reason': f'录入的公司资料提及“{skill}”，你的材料有该标签的直接证据；实际岗位需求仍需核对。',
                                    'company_quote': evidence_excerpt(line, skill), 'evidence': skills[skill]['evidence']})
    if not opportunity.get('company_info', '').strip():
        warnings.append('【需确认】尚未提供公司技术栈、业务或文化资料，不能推断公司适配。')
    elif not company_fit:
        warnings.append('【需确认】现有公司资料与个人材料没有可追溯的共同标签，不能据此判断文化或业务适配。')
    if not opportunity.get('city') or not target.get('city'):
        warnings.append('【需确认】目标城市或岗位城市未提供，请人工核对地点。')
    elif target['city'].strip().casefold() != opportunity['city'].strip().casefold():
        warnings.append(f'地点需核对：你的目标城市为 {target["city"]}，岗位录入城市为 {opportunity["city"]}；不计入技能分。')
    if not opportunity.get('url'):
        warnings.append('【需确认】尚未录入投递链接。')
    if not opportunity.get('deadline'):
        warnings.append('【需确认】尚未录入截止日期。')
    if not opportunity.get('source'):
        warnings.append('【需确认】岗位来源未填写，系统未联网验证。')
    expired = bool(opportunity.get('deadline') and opportunity['deadline'] < today.isoformat())
    if expired:
        warnings.append('已过录入截止日期，仅供复盘，请先核实是否仍开放。')
    if opportunity.get('sample'):
        warnings.append('虚构教学公司与岗位，不代表真实招聘机会，不能实际投递。')
    reasons = []
    direct = [item['requirement'] for item in matched if item['credit'] == 1]
    partial = [item['requirement'] for item in matched if item['credit'] < 1]
    if direct:
        reasons.append('材料有直接对应证据：' + '、'.join(direct) + '。')
    if partial:
        reasons.append('可迁移但仍需补证据：' + '、'.join(partial) + '。')
    if company_fit:
        reasons.append('录入的公司资料与个人材料存在共同标签，具体依据见公司适配分析。')
    if not reasons:
        reasons.append('当前证据不足以支持优先推荐，请补充个人材料或更具体的岗位要求。')
    return {key: opportunity.get(key, '') for key in ('id', 'company', 'role', 'url', 'deadline', 'city', 'source')} | {
        'score': score, 'score_basis': f'已识别 {len(required)} 项要求；普通/明确要求权重 2，优先/加分项权重 1；同义词 100%、部分可迁移 25% 或 50%。已得 {earned:g} / {denominator:g} 权重分。未识别要求不进入分母。',
        'matched': matched, 'missing': missing, 'company_fit': company_fit, 'reasons': reasons,
        'warnings': warnings, 'unassessed': unassessed, 'expired': expired, 'sample': bool(opportunity.get('sample')),
    }


EXPORT_HEADERS = ['公司名', '岗位名', '材料适配度（非录取概率）', '推荐理由', '差距与补齐建议', '投递链接', '截止日期', '城市', '岗位来源', '未评分要求/需确认', '数据性质']


def recommendation_rows(result):
    rows = [EXPORT_HEADERS]
    for item in result['recommendations']:
        rows.append([item['company'], item['role'], '未评估' if item['score'] is None else f'{item["score"]}%',
                     '；'.join(item['reasons']), '；'.join(gap['skill'] + '：' + ' / '.join(gap['actions']) for gap in item['missing']),
                     item['url'], item['deadline'], item['city'], item['source'],
                     '；'.join(item['unassessed'] + item['warnings']), '虚构教学示例' if item['sample'] else '用户录入，未联网核实'])
    return rows


def spreadsheet_bytes(rows):
    """Runtime XLSX writer: all input is inline text, never a formula or hyperlink."""
    def column(index):
        value = ''
        while index:
            index, remainder = divmod(index - 1, 26)
            value = chr(65 + remainder) + value
        return value
    def xml_text(value):
        # XML 1.0 disallows most C0 controls; Excel limits text cells to 32767 chars.
        return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))[:32767])
    contents = []
    for row_index, row in enumerate(rows, 1):
        cells = ''.join(f'<c r="{column(i)}{row_index}" s="{1 if row_index == 1 else 0}" t="inlineStr"><is><t xml:space="preserve">{xml_text(value)}</t></is></c>' for i, value in enumerate(row, 1))
        contents.append(f'<row r="{row_index}">{cells}</row>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as book:
        book.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        book.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="岗位推荐清单" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        book.writestr('xl/styles.xml', '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF4761CF"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        last = f'{column(len(rows[0]))}{len(rows)}'
        worksheet = '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:' + last + '"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="2" width="24" customWidth="1"/><col min="3" max="3" width="24" customWidth="1"/><col min="4" max="5" width="58" customWidth="1"/><col min="6" max="11" width="30" customWidth="1"/></cols><sheetData>' + ''.join(contents) + '</sheetData><autoFilter ref="A1:' + last + '"/></worksheet>'
        book.writestr('xl/worksheets/sheet1.xml', worksheet)
    return buffer.getvalue()
