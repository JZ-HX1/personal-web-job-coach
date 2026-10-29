"""只提取用户上传文档的文本，不执行宏、脚本、附件或外部链接。"""
import base64
import binascii
import io
import re
import zipfile
from pathlib import PurePath
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000


class DocumentError(ValueError):
    pass


def parse_document(filename, content_base64):
    if not isinstance(filename, str) or not 1 <= len(filename) <= 180:
        raise DocumentError("文件名无效。")
    if not isinstance(content_base64, str) or len(content_base64) > (MAX_FILE_BYTES * 4 // 3 + 16):
        raise DocumentError("每份文件最多 5 MB。")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise DocumentError("文件数据不完整，请重新选择文件。") from None
    if not content or len(content) > MAX_FILE_BYTES:
        raise DocumentError("文件为空或超过 5 MB。")
    suffix = PurePath(filename.replace('\\', '/')).suffix.lower()
    if suffix in ('.txt', '.md', '.csv'):
        text = None
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if text is None or '\x00' in text:
            raise DocumentError("无法识别文本编码，请另存为 UTF-8 TXT。")
    elif suffix == '.docx':
        text = _docx(content)
    elif suffix == '.pdf':
        text = _pdf(content)
    else:
        raise DocumentError("支持 TXT、MD、CSV、DOCX 和文字型 PDF；不支持旧版 DOC 或扫描图片。")
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        raise DocumentError("未提取到文字。扫描 PDF 或图片请先转成文本，再导入。")
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentError("文档文字超过 10 万字，请拆分为更小的材料。")
    return text


def _docx(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 1000 or sum(x.file_size for x in members) > 30 * 1024 * 1024:
                raise DocumentError("DOCX 内容过大，请移除大图后重试。")
            info = archive.getinfo('word/document.xml')
            if info.file_size > 8 * 1024 * 1024 or info.flag_bits & 1:
                raise DocumentError("DOCX 主文档过大或已加密。")
            data = archive.read(info)
            if re.search(br'<!\s*(DOCTYPE|ENTITY)', data, re.I):
                raise DocumentError("不支持含外部实体声明的 DOCX。")
            root = ET.fromstring(data)
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            paragraphs = []
            for paragraph in root.iter(ns + 'p'):
                pieces = []
                for node in paragraph.iter():
                    if node.tag == ns + 't':
                        pieces.append(node.text or '')
                    elif node.tag in (ns + 'tab', ns + 'br'):
                        pieces.append(' ')
                if pieces:
                    paragraphs.append(''.join(pieces))
            return '\n'.join(paragraphs)
    except DocumentError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError, OSError):
        raise DocumentError("DOCX 无法读取，请确认文件完整且不是加密文件。") from None


def _pdf(content):
    try:
        from pypdf import PdfReader
        import pypdf.filters
    except ImportError:
        raise DocumentError("PDF 解析组件尚未安装。请运行启动脚本安装依赖，或粘贴简历文字。") from None
    # pypdf 的单流解压保护与本应用的总流限额共同约束资源占用。
    if hasattr(pypdf.filters, 'ZLIB_MAX_OUTPUT_LENGTH'):
        pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH = 20 * 1024 * 1024
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise DocumentError("不支持加密 PDF，请解密后上传文字版。")
        if len(reader.pages) > 50:
            raise DocumentError("每份 PDF 最多 50 页，请拆分后导入。")
        pieces, total, stream_total = [], 0, 0
        for page in reader.pages:
            streams = page.get_contents()
            if streams is not None:
                decoded = streams.get_data()
                stream_total += len(decoded)
                if stream_total > 20 * 1024 * 1024:
                    raise DocumentError("PDF 解压内容过大，请转换为 TXT 后导入。")
            text = page.extract_text() or ''
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise DocumentError("PDF 文字超过 10 万字，请拆分后导入。")
            pieces.append(text)
        return '\n'.join(pieces)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("PDF 无法解析。请使用完整的文字型 PDF，或复制正文到文本框。") from None
