"""attachments.py 单元测试：docx / xlsx / txt 编码 / PDF / 上限与错误处理。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_design.attachments import (
    AttachmentBundle,
    extract_attachments,
    extract_document_text,
    format_attachments_block,
)


# --------------------------------------------------------------------------- #
# 测试夹具：生成临时文档
# --------------------------------------------------------------------------- #
def _make_docx(path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_paragraph("最大起飞重量 200 kg，巡航高度 5000 m。")
    document.add_paragraph("翼展不超过 4.5 m，采用 V 尾布局，翼型 NACA 4415。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "参数"
    table.cell(0, 1).text = "数值"
    table.cell(1, 0).text = "静稳定裕度"
    table.cell(1, 1).text = "12% MAC"
    document.save(str(path))


def _make_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "总体参数"
    sheet.append(["参数", "数值", "单位"])
    sheet.append(["翼展", 4.2, "m"])
    sheet.append(["机翼面积", 1.8, "m2"])
    sheet.append(["展弦比", 9.5, "-"])
    second = workbook.create_sheet("动力")
    second.append(["设备", "型号"])
    second.append(["电机", "5010 无刷电机"])
    workbook.save(str(path))


def _make_pdf(path: Path) -> bool:
    """用 PyMuPDF 生成一个含文本的简单 PDF；不可用时返回 False。"""
    try:
        import fitz  # type: ignore
    except Exception:
        return False
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "MTOW 200 kg, cruise altitude 5000 m, "
                               "speed 30-110 m/s, V-tail.", fontsize=12)
    page.insert_text((72, 100), "Static margin requirement: 10% MAC.",
                     fontsize=12)
    document.save(str(path))
    document.close()
    return True


# --------------------------------------------------------------------------- #
# 单个格式
# --------------------------------------------------------------------------- #
def test_extract_docx_paragraphs_and_table(tmp_path):
    path = tmp_path / "设计输入.docx"
    _make_docx(path)
    document = extract_document_text(path)
    assert document.ok is True
    assert document.kind == "docx"
    assert document.chars > 0
    assert "最大起飞重量 200 kg" in document.text
    # 表格内容也必须提取
    assert "静稳定裕度" in document.text
    assert "12% MAC" in document.text


def test_extract_xlsx_multiple_sheets(tmp_path):
    path = tmp_path / "设备参数.xlsx"
    _make_xlsx(path)
    document = extract_document_text(path)
    assert document.ok is True
    assert document.kind == "xlsx"
    assert "总体参数" in document.text
    assert "5010 无刷电机" in document.text
    assert "展弦比" in document.text


def test_extract_text_utf8_and_gbk(tmp_path):
    utf8_path = tmp_path / "说明.md"
    utf8_path.write_text("# 设计说明\n任务载荷 8 kg\n静稳定裕度 12%\n",
                         encoding="utf-8")
    document = extract_document_text(utf8_path)
    assert document.ok is True
    assert document.kind == "text"
    assert "任务载荷 8 kg" in document.text

    gbk_path = tmp_path / "gbk.txt"
    gbk_path.write_bytes("最大起飞重量 50 kg，巡航速度 33 m/s".encode("gb18030"))
    document = extract_document_text(gbk_path)
    assert document.ok is True
    assert "最大起飞重量 50 kg" in document.text
    assert document.encoding in ("gb18030", "gbk")


def test_extract_text_utf8_bom(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes("\ufeff巡航高度 2000 m".encode("utf-8"))
    document = extract_document_text(path)
    assert document.ok is True
    assert "巡航高度 2000 m" in document.text


def test_extract_json_pretty(tmp_path):
    path = tmp_path / "参数.json"
    path.write_text('{"mtow_kg": 50, "velocity_ms": 33}', encoding="utf-8")
    document = extract_document_text(path)
    assert document.ok is True
    assert "mtow_kg" in document.text
    assert "velocity_ms" in document.text


def test_extract_pdf(tmp_path):
    path = tmp_path / "任务约束.pdf"
    if not _make_pdf(path):
        pytest.skip("未安装 PyMuPDF，无法生成测试 PDF")
    document = extract_document_text(path)
    assert document.ok is True, document.error
    assert document.kind == "pdf"
    assert "MTOW 200 kg" in document.text or "MTOW" in document.text


# --------------------------------------------------------------------------- #
# 错误 / 边界
# --------------------------------------------------------------------------- #
def test_missing_file(tmp_path):
    document = extract_document_text(tmp_path / "不存在.docx")
    assert document.ok is False
    assert "不存在" in document.error


def test_legacy_doc(tmp_path):
    path = tmp_path / "旧文档.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0fake-doc-bytes")
    document = extract_document_text(path)
    assert document.ok is False
    assert ".docx" in document.error
    assert "另存为" in document.error


def test_unsupported_suffix(tmp_path):
    path = tmp_path / "图片.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    document = extract_document_text(path)
    assert document.ok is False
    assert "不支持" in document.error


def test_empty_docx(tmp_path):
    from docx import Document

    path = tmp_path / "空文档.docx"
    Document().save(str(path))
    document = extract_document_text(path)
    assert document.ok is False
    assert "未提取到文本" in document.error


def test_truncation(tmp_path):
    path = tmp_path / "长文本.txt"
    path.write_text("参数" * 500, encoding="utf-8")
    document = extract_document_text(path, max_chars=100)
    assert document.ok is True
    assert document.truncated is True
    assert document.chars <= 160
    assert "已截断" in document.text


def test_oversize_bytes(tmp_path):
    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * 1024)
    document = extract_document_text(path, max_chars=10000, max_bytes=100)
    assert document.ok is False
    assert "过大" in document.error


# --------------------------------------------------------------------------- #
# 批量与融合
# --------------------------------------------------------------------------- #
def test_extract_attachments_bundle(tmp_path):
    docx_path = tmp_path / "设计输入.docx"
    _make_docx(docx_path)
    md_path = tmp_path / "备注.md"
    md_path.write_text("补充约束：起落架高度 0.35 m。", encoding="utf-8")
    bundle = extract_attachments([docx_path, md_path, tmp_path / "missing.pdf"])
    assert isinstance(bundle, AttachmentBundle)
    assert bundle.count == 3
    assert bundle.ok_count == 2
    assert bundle.total_chars > 0
    assert "最大起飞重量 200 kg" in bundle.text
    assert "附件 1: 设计输入.docx" in bundle.text
    assert "附件 2: 备注.md" in bundle.text
    failed = [item for item in bundle.summary() if not item["ok"]]
    assert failed and "不存在" in failed[0]["error"]
    assert any("附件解析失败" in warning for warning in bundle.warnings)
    assert bundle.as_dict()["document_chars"] == bundle.total_chars


def test_total_chars_limit(tmp_path):
    first = tmp_path / "a.txt"
    first.write_text("A" * 200, encoding="utf-8")
    second = tmp_path / "b.txt"
    second.write_text("B" * 200, encoding="utf-8")
    bundle = extract_attachments([first, second], max_total_chars=120)
    assert bundle.ok_count == 1
    assert bundle.total_chars <= 120
    skipped = [item for item in bundle.summary() if not item["ok"]]
    assert skipped and "上限" in skipped[0]["error"]


def test_max_files_limit(tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"f{index}.txt"
        path.write_text(f"content-{index}", encoding="utf-8")
        paths.append(path)
    bundle = extract_attachments(paths, max_files=2)
    assert bundle.count == 2
    assert any("超过上限" in warning for warning in bundle.warnings)


def test_format_attachments_block():
    bundle = AttachmentBundle(total_chars=5, text="参数 A=1")
    bundle.documents = []
    from ai_design.attachments import AttachmentText

    bundle.documents = [AttachmentText(
        path="a.txt", name="a.txt", kind="text", ok=True, chars=5, text="参数 A=1")]
    block = format_attachments_block(bundle)
    assert "【上传技术文档解析内容】" in block
    assert "附件 1: a.txt" in block
    assert "参数 A=1" in block
    # 空 bundle 返回空字符串
    assert format_attachments_block(AttachmentBundle()) == ""
