"""Local text import only. No file storage, rendering, network or model calls."""
import re
from pathlib import PurePosixPath

MAX_UPLOAD_BYTES = 256 * 1024
MAX_CONTENT_CHARS = 60_000
MAX_OWNER_CHARS = 1_000_000


def knowledge_chunks(text):
    """Keep short Q&A intact; split long documents with bounded heading context."""
    if len(text) <= 2000:
        return [text]
    chunks = []
    heading = ""
    remaining = text
    while remaining:
        end = min(len(remaining), 1400)
        if end < len(remaining):
            boundary = max(remaining.rfind("\n", 500, end), remaining.rfind("。", 500, end))
            if boundary >= 0:
                end = boundary + 1
        part, remaining = remaining[:end], remaining[end:]
        prefix = heading + "\n" if heading and not part.lstrip().startswith(heading) else ""
        chunks.append(prefix + part.strip())
        headings = re.findall(r"(?m)^#{1,6}\s+(.+)$", part)
        if headings:
            heading = headings[-1][:150]
    return chunks


def preview_document(filename, raw):
    name = PurePosixPath(filename.replace("\\", "/")).name
    if not name or PurePosixPath(name).suffix.lower() not in {".txt", ".md"}:
        raise ValueError("仅支持 .txt 和 .md 文档")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("文件不能超过256KB")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    except UnicodeDecodeError as error:
        raise ValueError("请将文件另存为 UTF-8 编码后再上传") from error
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("文件含非文本控制字符，请上传 UTF-8 纯文本")
    if not text or len(text) > MAX_CONTENT_CHARS:
        raise ValueError("文档不能为空，且最多60000字")
    return {"filename": name, "topic": PurePosixPath(name).stem[:80], "content": text,
            "chunks": knowledge_chunks(text), "model_called": False}
