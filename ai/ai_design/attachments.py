"""上传附件文档解析：把 Word / PDF / Excel / 纯文本转成可参与设计的技术文本。

GUI「上传文档…」按钮只负责选择文件，C++ 把绝对路径写入 ``ai_job.json`` 的
``attachments`` 数组；真正的文本提取与需求融合在本模块完成。

支持格式
--------
- ``.docx``：python-docx，段落 + 表格（必须可用）
- ``.pdf``：优先 pypdf，回退 PyPDF2 / pdfplumber / PyMuPDF(fitz) 任一可用
- ``.xlsx``：openpyxl；``.xls``：回退 xlrd / pandas（缺库给清晰错误）
- ``.txt`` / ``.md`` / ``.markdown`` / ``.csv`` / ``.json`` / ``.log`` \
  / ``.yaml`` / ``.yml`` / ``.ini``：UTF-8/UTF-8-BOM/GBK/GB18030/UTF-16 编码探测
- ``.doc``：明确提示“旧版 .doc 请另存为 .docx”（不做二进制解析）

安全与上限
----------
- 单文件字符上限 ``max_file_chars``（默认 60000），超出截断并标注
- 全部附件总字符上限 ``max_total_chars``（默认 240000），超出后跳过剩余文件
- 单文件大小上限 ``max_file_bytes``（默认 25 MB）、文件个数上限 ``max_files``（默认 12）
- 单个文件失败不影响其它文件；错误文本会进入摘要（``error`` 字段）

公共 API
--------
- :func:`extract_document_text` → :class:`AttachmentText`
- :func:`extract_attachments` → :class:`AttachmentBundle`（含合并文本与摘要）
- :func:`format_attachments_block` → 供 LLM 消息 / offline prompt 使用的文本块
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MAX_FILE_CHARS = 60000
MAX_TOTAL_CHARS = 240000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 12

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".log",
                 ".yaml", ".yml", ".ini"}
DOCX_SUFFIXES = {".docx"}
PDF_SUFFIXES = {".pdf"}
EXCEL_SUFFIXES = {".xlsx", ".xls"}
LEGACY_DOC_SUFFIXES = {".doc"}

SUPPORTED_SUFFIXES = (TEXT_SUFFIXES | DOCX_SUFFIXES | PDF_SUFFIXES
                      | EXCEL_SUFFIXES | LEGACY_DOC_SUFFIXES)

#: 编码探测顺序（BOM 单独优先处理，gb18030 覆盖 GBK/GB2312）
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5",
                  "utf-16", "utf-16-le", "utf-16-be", "latin-1")

FORMAT_LABELS = {
    "docx": "Word 文档",
    "pdf": "PDF 文档",
    "xlsx": "Excel 工作簿",
    "xls": "Excel 97-2003 工作簿",
    "text": "纯文本",
    "doc": "旧版 Word 文档",
    "unknown": "未知格式",
}


@dataclass
class AttachmentText:
    """单个附件的提取结果。"""

    path: str
    name: str = ""
    kind: str = "unknown"
    ok: bool = False
    chars: int = 0
    text: str = ""
    error: str | None = None
    truncated: bool = False
    original_chars: int = 0
    encoding: str | None = None

    def to_summary(self) -> dict[str, Any]:
        """result.json / 日志用摘要（不含全文）。"""
        summary: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "ok": self.ok,
            "chars": self.chars,
            "truncated": self.truncated,
            "error": self.error,
        }
        if self.encoding:
            summary["encoding"] = self.encoding
        return summary


@dataclass
class AttachmentBundle:
    """全部附件的提取与合并结果。"""

    documents: list[AttachmentText] = field(default_factory=list)
    total_chars: int = 0
    text: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for document in self.documents if document.ok)

    @property
    def count(self) -> int:
        return len(self.documents)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    def summary(self) -> list[dict[str, Any]]:
        return [document.to_summary() for document in self.documents]

    def as_dict(self) -> dict[str, Any]:
        return {
            "attachments": self.summary(),
            "document_chars": self.total_chars,
        }


def _kind_for_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix in DOCX_SUFFIXES:
        return "docx"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in EXCEL_SUFFIXES:
        return suffix.lstrip(".")
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in LEGACY_DOC_SUFFIXES:
        return "doc"
    return "unknown"


def summarize_attachments(paths: Iterable[str | os.PathLike] | None) -> int:
    """返回附件路径数量（供 config/CLI 快速使用）。"""
    if not paths:
        return 0
    return len([item for item in paths if str(item).strip()])


# --------------------------------------------------------------------------- #
# 各格式提取实现
# --------------------------------------------------------------------------- #
def _extract_docx(path: Path) -> tuple[str, str | None]:
    """提取 .docx 段落与表格文本。"""
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - 正常环境必有
        raise RuntimeError("解析 .docx 需要 python-docx（pip install python-docx）") from exc
    try:
        document = Document(str(path))
    except Exception as exc:
        raise RuntimeError(f"无法打开 .docx 文件: {exc}") from exc
    parts: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table_index, table in enumerate(document.tables, start=1):
        rows: list[str] = []
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"[表格 {table_index}]")
            parts.extend(rows)
    return "\n".join(parts), None


def _extract_pdf(path: Path) -> tuple[str, str | None]:
    """提取 .pdf 文本：pypdf → PyPDF2 → pdfplumber → fitz 依次回退。"""
    errors: list[str] = []
    # 1) pypdf
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n".join(pages).strip()
        if text:
            return text, "pypdf"
        errors.append("pypdf: 未提取到文本")
    except Exception as exc:  # pragma: no cover - 依赖版本差异
        errors.append(f"pypdf: {type(exc).__name__}: {exc}")
    # 2) PyPDF2
    try:
        from PyPDF2 import PdfReader as PdfReader2  # type: ignore

        reader = PdfReader2(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        if text:
            return text, "PyPDF2"
        errors.append("PyPDF2: 未提取到文本")
    except Exception as exc:
        errors.append(f"PyPDF2: {type(exc).__name__}: {exc}")
    # 3) pdfplumber
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(path)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages).strip()
        if text:
            return text, "pdfplumber"
        errors.append("pdfplumber: 未提取到文本")
    except Exception as exc:
        errors.append(f"pdfplumber: {type(exc).__name__}: {exc}")
    # 4) PyMuPDF (fitz)
    try:
        import fitz  # type: ignore

        document = fitz.open(str(path))
        try:
            text = "\n".join(page.get_text() for page in document).strip()
        finally:
            document.close()
        if text:
            return text, "fitz"
        errors.append("fitz: 未提取到文本")
    except Exception as exc:
        errors.append(f"fitz: {type(exc).__name__}: {exc}")
    joined = "；".join(errors[-4:])
    raise RuntimeError(
        "PDF 文本提取失败（若为扫描版 PDF 需 OCR 后再上传）: " + joined)


def _extract_xlsx(path: Path, suffix: str) -> tuple[str, str | None]:
    """提取 Excel 工作簿文本。"""
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "解析 .xlsx 需要 openpyxl（pip install openpyxl）") from exc
        try:
            workbook = load_workbook(str(path), read_only=True, data_only=True)
        except Exception as exc:
            raise RuntimeError(f"无法打开 .xlsx 文件: {exc}") from exc
        parts: list[str] = []
        try:
            for sheet in workbook.worksheets:
                rows: list[str] = []
                for row in sheet.iter_rows(values_only=True):
                    cells = ["" if value is None else str(value).strip()
                             for value in row]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    parts.append(f"[工作表: {sheet.title}]")
                    parts.extend(rows)
        finally:
            workbook.close()
        return "\n".join(parts), "openpyxl"
    # .xls：xlrd 优先，其次 pandas+xlrd
    try:
        import xlrd  # type: ignore

        workbook = xlrd.open_workbook(str(path))
        parts = []
        for sheet in workbook.sheets():
            rows = []
            for row_index in range(sheet.nrows):
                cells = [str(sheet.cell_value(row_index, column)).strip()
                         for column in range(sheet.ncols)]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"[工作表: {sheet.name}]")
                parts.extend(rows)
        return "\n".join(parts), "xlrd"
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError(f"无法打开 .xls 文件: {exc}") from exc
    try:
        import pandas as pd  # type: ignore

        sheets = pd.read_excel(str(path), sheet_name=None, header=None)
        parts = []
        for name, frame in sheets.items():
            rows = []
            for _, row in frame.iterrows():
                cells = [str(value).strip() for value in row.tolist()
                         if str(value) not in ("nan", "NaT", "None")]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"[工作表: {name}]")
                parts.extend(rows)
        return "\n".join(parts), "pandas"
    except ImportError as exc:
        raise RuntimeError(
            "解析旧版 .xls 需要 xlrd（pip install xlrd）或 pandas，"
            "建议另存为 .xlsx 后重新上传") from exc
    except Exception as exc:
        raise RuntimeError(
            f"无法解析 .xls 文件（建议另存为 .xlsx）: {exc}") from exc


def _decode_text_bytes(data: bytes) -> tuple[str, str]:
    """按 BOM 与常见编码探测解码文本。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _extract_text_file(path: Path) -> tuple[str, str | None]:
    data = path.read_bytes()
    if not data:
        return "", None
    # JSON 做一次美化（失败则按原文处理）
    text, encoding = _decode_text_bytes(data)
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
    return text, encoding


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #
def extract_document_text(path: str | os.PathLike, max_chars: int = MAX_FILE_CHARS,
                          max_bytes: int = MAX_FILE_BYTES) -> AttachmentText:
    """提取单个文档的文本（永不抛异常，失败写入 ``error`` 字段）。"""
    raw_path = str(path)
    file_path = Path(raw_path).expanduser()
    result = AttachmentText(path=raw_path, name=file_path.name,
                            kind=_kind_for_suffix(file_path.suffix))
    try:
        if not file_path.is_file():
            result.error = f"文件不存在: {raw_path}"
            return result
        size = file_path.stat().st_size
        if size > max_bytes:
            result.error = (f"文件过大（{size / 1024 / 1024:.1f} MB > "
                            f"{max_bytes / 1024 / 1024:.0f} MB 上限）")
            return result
        suffix = file_path.suffix.lower()
        if suffix in LEGACY_DOC_SUFFIXES:
            result.error = "旧版 .doc 不支持，请在 Word 中另存为 .docx 后重新上传"
            return result
        if suffix in DOCX_SUFFIXES:
            text, note = _extract_docx(file_path)
        elif suffix in PDF_SUFFIXES:
            text, note = _extract_pdf(file_path)
        elif suffix in EXCEL_SUFFIXES:
            text, note = _extract_xlsx(file_path, suffix)
        elif suffix in TEXT_SUFFIXES:
            text, note = _extract_text_file(file_path)
        else:
            result.error = (f"不支持的文档类型: {suffix or '<无扩展名>'}；"
                            f"支持 {', '.join(sorted(SUPPORTED_SUFFIXES))}")
            return result
        if note:
            result.encoding = note
        text = (text or "").strip()
        if not text:
            result.error = "未提取到文本内容（可能是空文档或扫描版 PDF）"
            return result
        result.original_chars = len(text)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "\n\n……[内容超出单文件上限，已截断]……"
            result.truncated = True
        result.text = text
        result.chars = len(text)
        result.ok = True
        return result
    except Exception as exc:  # 兜底：单文件失败不影响整体流程
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def format_attachments_block(bundle: AttachmentBundle,
                             header: str = "【上传技术文档解析内容】") -> str:
    """把附件文本拼成带文件标题的文本块（供 prompt 融合）。"""
    if not bundle or not bundle.has_text:
        return ""
    parts: list[str] = [header]
    index = 0
    for document in bundle.documents:
        if not document.ok or not document.text.strip():
            continue
        index += 1
        label = FORMAT_LABELS.get(document.kind, document.kind)
        flag = "，已截断" if document.truncated else ""
        parts.append(f"\n### 附件 {index}: {document.name}（{label}，"
                     f"{document.chars} 字符{flag}）")
        parts.append(document.text)
    return "\n".join(parts)


def extract_attachments(paths: Iterable[str | os.PathLike] | None = None,
                        max_file_chars: int = MAX_FILE_CHARS,
                        max_total_chars: int = MAX_TOTAL_CHARS,
                        max_files: int = MAX_FILES,
                        max_bytes: int = MAX_FILE_BYTES) -> AttachmentBundle:
    """批量提取附件文本，返回合并结果与摘要。

    - 不存在的文件、解析失败、不支持格式都会记录 warning/错误但不抛异常
    - 超过总字符上限后剩余文件被跳过（摘要中仍有记录）
    """
    bundle = AttachmentBundle()
    items = [str(item) for item in (paths or []) if str(item).strip()]
    if len(items) > max_files:
        bundle.warnings.append(
            f"附件数量 {len(items)} 超过上限 {max_files}，仅处理前 {max_files} 个")
        items = items[:max_files]
    remaining = max_total_chars
    for index, item in enumerate(items, start=1):
        if remaining <= 0:
            skipped = AttachmentText(path=item, name=Path(item).name,
                                     kind=_kind_for_suffix(Path(item).suffix),
                                     error="总字符数已达上限，已跳过该附件")
            bundle.documents.append(skipped)
            bundle.warnings.append(f"跳过附件（总字符超限）: {Path(item).name}")
            continue
        document = extract_document_text(
            item,
            max_chars=min(max_file_chars if max_file_chars > 0 else remaining,
                          remaining),
            max_bytes=max_bytes)
        # 截断到剩余预算
        if document.ok and document.chars > remaining:
            document.text = document.text[:remaining]
            document.chars = len(document.text)
            document.truncated = True
        remaining -= document.chars
        bundle.documents.append(document)
        if not document.ok:
            bundle.warnings.append(
                f"附件解析失败: {document.name}: {document.error}")
    successful = [document for document in bundle.documents
                  if document.ok and document.text.strip()]
    bundle.total_chars = sum(document.chars for document in successful)
    if successful:
        index = 0
        parts: list[str] = []
        for document in successful:
            index += 1
            label = FORMAT_LABELS.get(document.kind, document.kind)
            flag = "，已截断" if document.truncated else ""
            parts.append(f"### 附件 {index}: {document.name}（{label}，"
                         f"{document.chars} 字符{flag}）")
            parts.append(document.text)
        bundle.text = "\n".join(parts)
    return bundle


def extract_attachments_from_config(config_paths: Iterable[str] | None) -> AttachmentBundle:
    """便捷封装：直接接受配置中的 attachments 列表。"""
    return extract_attachments(config_paths)
