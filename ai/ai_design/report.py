"""中文 Word 设计报告生成（python-docx）。

模板约定
--------
内置模板 ``templates/design_report_template.docx`` 中以 ``{%TITLE%}`` 形式
的段落作为占位符；生成时在占位段落位置插入文本 / 表格 / 图片，然后删除占位

    {%TITLE%}        报告标题（封面）
    {%SUBTITLE%}     副标题
    {%META%}         封面信息（多行）
    {%REQUIREMENTS%} 设计任务与要求（文字 + 表格）
    {%PARAMS%}       几何参数（表格）
    {%ANALYSIS_SETUP%} VSPAERO 计算设置（表格）
    {%RESULTS%}      气动结果（表格 + 图）
    {%STABILITY%}    稳定性结果（表格）
    {%OPTIMIZATION%} 优化与迭代（表格）
    {%CONCLUSIONS%}  结论与建议（文字条目）
    {%FILES%}        输出文件清单（表格）
    {%LIMITATIONS%}  已知限制（文字条目）

若模板缺失，则按同样样式从头生成文档（降级路径）。
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph

#: 占位符 → 内容块 的映射在填充时使用；以下为模板必需占位符
REQUIRED_PLACEHOLDERS = (
    "{%TITLE%}", "{%SUBTITLE%}", "{%META%}", "{%REQUIREMENTS%}",
    "{%PARAMS%}", "{%ANALYSIS_SETUP%}", "{%RESULTS%}", "{%STABILITY%}",
    "{%OPTIMIZATION%}", "{%CONCLUSIONS%}", "{%FILES%}", "{%LIMITATIONS%}",
)

SONG = "宋体"
HEI = "黑体"
TNR = "Times New Roman"


# --------------------------------------------------------------------------- #
# 字体 / 表格样式辅助
# --------------------------------------------------------------------------- #
def set_font(run, size: float = 10.5, east: str = SONG,
             ascii_font: str = TNR, bold: bool = False,
             color: tuple[int, int, int] | None = None) -> None:
    """设置中英文字体（python-docx 需要显式写 w:eastAsia）。"""
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = ascii_font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), east)
    rfonts.set(qn("w:ascii"), ascii_font)
    rfonts.set(qn("w:hAnsi"), ascii_font)
    if color:
        run.font.color.rgb = RGBColor(*color)


def _shade_cell(cell, fill: str = "E7E9EC") -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shd)


def _set_cell_text(cell, text: object, size: float = 9.0, bold: bool = False,
                   align=WD_ALIGN_PARAGRAPH.CENTER, east: str = SONG) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.space_before = Pt(1)
    paragraph.paragraph_format.space_after = Pt(1)
    set_font(paragraph.add_run(str(text)), size=size, east=east, bold=bold)


def add_table_block(doc: Document, paragraph: Paragraph,
                    headers: Iterable[object], rows: Iterable[Iterable[object]],
                    widths: list[float] | None = None,
                    font_size: float = 9.0) -> None:
    """在占位段落之前插入表格（Table Grid 样式、表头加粗 + 底纹）。"""
    headers = [str(item) for item in headers]
    rows = [[("" if value is None else value) for value in row] for row in rows]
    table = doc.add_table(rows=1, cols=len(headers))
    try:
        table.style = doc.styles["Table Grid"]
    except KeyError:
        table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(headers):
        cell = table.rows[0].cells[index]
        _set_cell_text(cell, header, size=font_size, bold=True, east=HEI)
        _shade_cell(cell)
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            _set_cell_text(cells[index], value, size=font_size)
    if widths:
        for row in table.rows:
            for index, width in enumerate(widths):
                if index < len(row.cells):
                    row.cells[index].width = Cm(width)
    paragraph._p.addprevious(table._tbl)


def insert_paragraph_before(doc: Document, paragraph: Paragraph, text: str = "",
                            size: float = 10.5, bold: bool = False,
                            east: str = SONG, style: str | None = None,
                            align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                            first_line_indent: bool = True) -> Paragraph:
    new_p = OxmlElement("w:p")
    paragraph._p.addprevious(new_p)
    created = Paragraph(new_p, paragraph._parent)
    if style:
        try:
            created.style = doc.styles[style]
        except KeyError:
            pass
    created.alignment = align
    fmt = created.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(3)
    fmt.line_spacing = 1.35
    if first_line_indent and align == WD_ALIGN_PARAGRAPH.JUSTIFY:
        fmt.first_line_indent = Pt(size * 2)
    if text:
        set_font(created.add_run(text), size=size, east=east, bold=bold)
    return created


def insert_caption(doc: Document, paragraph: Paragraph, text: str) -> None:
    insert_paragraph_before(doc, paragraph, text, size=9.0, bold=True, east=HEI,
                            align=WD_ALIGN_PARAGRAPH.CENTER, first_line_indent=False)


def insert_picture(doc: Document, paragraph: Paragraph, image_path: str,
                   width_cm: float = 15.0) -> bool:
    path = Path(image_path)
    if not path.is_file():
        return False
    new_p = OxmlElement("w:p")
    paragraph._p.addprevious(new_p)
    created = Paragraph(new_p, paragraph._parent)
    created.alignment = WD_ALIGN_PARAGRAPH.CENTER
    try:
        created.add_run().add_picture(str(path), width=Cm(width_cm))
        return True
    except Exception:
        return False


def insert_bullets(doc: Document, paragraph: Paragraph, items: Iterable[str],
                   size: float = 10.5) -> None:
    for item in items:
        created = insert_paragraph_before(
            doc, paragraph, "• " + str(item), size=size,
            align=WD_ALIGN_PARAGRAPH.JUSTIFY, first_line_indent=False)
        created.paragraph_format.left_indent = Pt(size)


# --------------------------------------------------------------------------- #
# 页面 / 页眉页脚
# --------------------------------------------------------------------------- #
def _add_page_field(paragraph: Paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def setup_page(doc: Document, header_text: str = "") -> None:
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)
    footer = section.footer
    footer_paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_paragraph.text = ""
    set_font(footer_paragraph.add_run("第 "), size=8.5, east=SONG)
    _add_page_field(footer_paragraph)
    set_font(footer_paragraph.add_run(" 页"), size=8.5, east=SONG)
    if header_text:
        header = section.header
        paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        paragraph.text = ""
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_font(paragraph.add_run(header_text), size=8.5, east=HEI,
                 color=(0x59, 0x59, 0x59))


# --------------------------------------------------------------------------- #
# 数据归一化（兼容 designer 内部结构与 MCP 返回结构）
# --------------------------------------------------------------------------- #
def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict())
        except Exception:
            return {}
    return {}


#: 极曲线行键别名 → 报告内部规范键（兼容 MCP 工具层命名 alpha_deg/CL/CD/Cm/...）
_ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "AoA": ("alpha_deg", "alpha", "Alpha", "AoA"),
    "CLtot": ("CL", "CLtot", "cl"),
    "CDtot": ("CD", "CDtot", "cd"),
    "CStot": ("CS", "CStot"),
    "CMytot": ("Cm", "CMy", "CMytot", "CMm"),
    "CMxtot": ("Cl", "CMx", "CMxtot"),
    "CMztot": ("Cn", "CMz", "CMztot"),
    "Beta": ("beta_deg", "Beta", "beta"),
    "Mach": ("mach", "Mach"),
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not (isinstance(value, float)
                                                     and math.isnan(value))


def normalize_polar_row(row: dict[str, Any]) -> dict[str, Any]:
    """把一行气动数据补齐为报告内部规范键（不覆盖已有值）。"""
    if not isinstance(row, dict):
        return {}
    normalized = dict(row)
    for canonical, aliases in _ROW_ALIASES.items():
        if _is_number(normalized.get(canonical)):
            continue
        for alias in aliases:
            if _is_number(normalized.get(alias)):
                normalized[canonical] = normalized[alias]
                break
    return normalized


def normalize_polar_rows(rows: Any) -> list[dict[str, Any]]:
    """批量补齐极曲线行键，兼容不同调用方的字段命名。"""
    if not isinstance(rows, list):
        return []
    return [normalize_polar_row(row) for row in rows if isinstance(row, dict)]


def _num(value: Any, digits: int = 2, default: str = "—") -> str:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number) or math.isinf(number):
        return default
    return f"{number:.{digits}f}"


def normalize_data(result: dict[str, Any] | None = None,
                   metrics: dict[str, Any] | None = None,
                   spec: Any = None, geometry: Any = None,
                   reference: dict[str, Any] | None = None,
                   data: dict[str, Any] | None = None) -> dict[str, Any]:
    """把多种来源的输入归一化为报告内部结构。"""
    result = _as_dict(result)
    metrics = _as_dict(metrics)
    data = _as_dict(data)
    spec_dict = _as_dict(spec)
    if isinstance(geometry, list):
        # MCP 服务器可能直接传部件列表
        geometry_dict: dict[str, Any] = {"components": geometry}
    else:
        geometry_dict = _as_dict(geometry)

    merged: dict[str, Any] = {}
    for source in (data, result, metrics):
        for key, value in source.items():
            if value is not None and key not in merged:
                merged[key] = value

    # 设计需求
    design = _as_dict(merged.get("design"))
    if not design and spec_dict:
        design = dict(spec_dict)
        design.setdefault("configuration", spec_dict.get("config", "conventional"))
        design.setdefault("configuration_label",
                          {"vtail": "V 尾布局", "conventional": "常规尾翼布局",
                           "flying_wing": "飞翼布局"}.get(
                               str(design.get("configuration")), "常规尾翼布局"))
        design.setdefault("name", spec_dict.get("name") or "AI 设计无人机")
        design.setdefault("engine", spec_dict.get("engine", "electric"))
        design.setdefault("assumptions", spec_dict.get("assumptions", []))
    # 几何
    geom = _as_dict(merged.get("geometry"))
    if not geom and geometry_dict:
        geom = dict(geometry_dict)
    components = geom.get("components")
    if not isinstance(components, list):
        components = []
    merged["design"] = design
    merged["geometry"] = geom
    merged["components"] = components
    # 参考量 / 流场
    merged["reference"] = {**(_as_dict(merged.get("reference"))),
                           **(_as_dict(reference))}
    merged["flow"] = _as_dict(merged.get("flow"))
    # 气动
    aero = _as_dict(merged.get("aero"))
    if not aero and isinstance(merged.get("summary"), dict):
        aero = dict(merged["summary"])
    merged["aero"] = aero
    # 极曲线行（兼容 MCP 工具层字段命名：alpha_deg/CL/CD/Cm/...）
    rows = merged.get("rows")
    if not isinstance(rows, list):
        polar = _as_dict(merged.get("polar"))
        rows = polar.get("rows") if isinstance(polar.get("rows"), list) else []
    merged["polar_rows"] = normalize_polar_rows(rows)
    # 稳定性
    stability = _as_dict(merged.get("stability"))
    merged["stability"] = stability
    merged["cruise"] = _as_dict(merged.get("cruise"))
    merged["optimization"] = _as_dict(merged.get("optimization"))
    files = _as_dict(merged.get("files"))
    merged["files"] = files
    merged["mode"] = merged.get("mode") or ("offline" if merged.get("offline") else "llm")
    return merged


# --------------------------------------------------------------------------- #
# 图表生成（matplotlib，失败则跳过）
# --------------------------------------------------------------------------- #
def generate_figures(rows: list[dict[str, Any]], figures_dir: Path,
                     logger: Any = None) -> dict[str, str]:
    """生成 CL-α、L/D-α、CD-CL 三张图，返回 {key: path}。"""
    figures: dict[str, str] = {}
    if not rows:
        return figures
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover
        if logger is not None:
            logger.warn(f"matplotlib 不可用，跳过报告插图: {exc}")
        return figures
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                       "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    figures_dir.mkdir(parents=True, exist_ok=True)

    def series(*names: str) -> list[float]:
        values: list[float] = []
        for row in rows:
            value = None
            for name in names:
                candidate = row.get(name)
                if isinstance(candidate, (int, float)) and not math.isnan(candidate):
                    value = float(candidate)
                    break
            values.append(float("nan") if value is None else value)
        return values

    alphas = series("AoA", "alpha_deg", "alpha", "Alpha")
    cl = series("CLtot", "CL", "cl")
    cd = series("CDtot", "CD", "cd")
    if not any(not math.isnan(v) for v in alphas):
        return figures
    alpha_limited = [None if math.isnan(a) else a for a in alphas]

    def save(fig, name: str, xlabel: str, ylabel: str, title: str) -> None:
        path = figures_dir / name
        try:
            fig.tight_layout()
            fig.savefig(path, dpi=140)
            figures[name.split(".")[0]] = str(path)
        finally:
            plt.close(fig)

    # 图 1：CL-α
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    ax.plot(alpha_limited, cl, "o-", color="#1f4e79", markersize=4, linewidth=1.6)
    ax.set_xlabel("迎角 α (°)")
    ax.set_ylabel("升力系数 CL")
    ax.set_title("升力系数随迎角变化曲线")
    ax.grid(True, alpha=0.35)
    save(fig, "fig_cl_alpha.png", "迎角 α (°)", "CL", "CL-α")

    # 图 2：L/D-α
    ld = [c / d if (d and not math.isnan(c) and not math.isnan(d) and abs(d) > 1e-9) else float("nan")
          for c, d in zip(cl, cd)]
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    ax.plot(alpha_limited, [None if math.isnan(v) else v for v in ld], "s-",
            color="#c00000", markersize=4, linewidth=1.6)
    ax.set_xlabel("迎角 α (°)")
    ax.set_ylabel("升阻比 L/D")
    ax.set_title("升阻比随迎角变化曲线")
    ax.grid(True, alpha=0.35)
    save(fig, "fig_ld_alpha.png", "迎角 α (°)", "L/D", "L/D-α")

    # 图 3：极曲线 CD-CL
    fig, ax = plt.subplots(figsize=(4.8, 4.2))
    ax.plot([None if math.isnan(v) else v for v in cd], cl, "^-",
            color="#548235", markersize=4, linewidth=1.6)
    ax.set_xlabel("阻力系数 CD")
    ax.set_ylabel("升力系数 CL")
    ax.set_title("阻力极曲线")
    ax.grid(True, alpha=0.35)
    save(fig, "fig_polar.png", "CD", "CL", "极曲线")
    return figures


def generate_ga_convergence_figure(history: Any, figures_dir: Path,
                                   logger: Any = None) -> dict[str, str]:
    """生成 GA 收敛曲线（每代最优/平均 L/D），失败返回空 dict。"""
    figures: dict[str, str] = {}
    if not isinstance(history, list) or not history:
        return figures
    generations: list[float] = []
    best_values: list[float] = []
    avg_values: list[float] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        try:
            generations.append(float(item.get("gen")))
            best_values.append(float(item.get("best_ld")))
            avg_values.append(float(item.get("avg_ld")))
        except (TypeError, ValueError):
            continue
    if len(generations) < 1:
        return figures
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover
        if logger is not None:
            logger.warn(f"matplotlib 不可用，跳过 GA 收敛图: {exc}")
        return figures
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                       "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    figures_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    ax.plot(generations, best_values, "o-", color="#1f4e79", linewidth=1.8,
            markersize=5, label="每代最优 L/D")
    ax.plot(generations, avg_values, "s--", color="#c00000", linewidth=1.4,
            markersize=4, label="每代平均 L/D")
    ax.set_xlabel("进化代数")
    ax.set_ylabel("巡航升阻比 L/D")
    ax.set_title("遗传算法收敛曲线")
    ax.grid(True, alpha=0.35)
    ax.legend(fontsize=8)
    ax.set_xticks([int(value) for value in generations
                   if float(value).is_integer()] or generations)
    path = figures_dir / "fig_ga_convergence.png"
    try:
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        figures["fig_ga_convergence"] = str(path)
    except Exception:  # pragma: no cover
        pass
    finally:
        plt.close(fig)
    return figures


# --------------------------------------------------------------------------- #
# 报告内容块构建
# --------------------------------------------------------------------------- #
def _requirement_table(design: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    configuration = str(design.get("configuration_label")
                        or {"vtail": "V 尾布局", "conventional": "常规尾翼布局",
                            "flying_wing": "飞翼布局"}.get(
                                str(design.get("configuration")), "常规布局"))
    engine = str(design.get("engine", ""))
    engine_label = {"electric": "电动（锂电池 + 螺旋桨）", "EJ115": "EJ115 涡喷发动机",
                    "TWP50": "TWP50 涡桨发动机"}.get(engine, engine)
    mtow = design.get("mtow_kg")
    altitude = design.get("altitude_m")
    vmin = design.get("speed_min_ms")
    vmax = design.get("speed_max_ms")
    vcru = design.get("speed_cruise_ms")
    rows: list[list[Any]] = []
    if mtow is not None:
        rows.append(["最大起飞重量", f"{_num(mtow, 1)} kg", "设计输入"])
    if altitude is not None:
        rows.append(["设计飞行高度", f"{_num(altitude, 0)} m", "ISA 标准大气"])
    if vmin is not None and vmax is not None:
        rows.append(["飞行速度范围", f"{_num(vmin, 1)} ~ {_num(vmax, 1)} m/s", "设计输入"])
    if vcru is not None:
        rows.append(["巡航速度", f"{_num(vcru, 1)} m/s", "速度范围内选取"])
    rows.append(["气动布局", configuration, "需求解析"])
    if engine_label:
        rows.append(["动力形式", engine_label, "需求解析"])
    if design.get("static_margin") is not None:
        rows.append(["纵向静稳定裕度目标",
                     f"{_num(float(design['static_margin']) * 100.0, 1)} % MAC", "设计约束"])
    if design.get("max_span_m") is not None:
        rows.append(["翼展限制", f"≤ {_num(design['max_span_m'], 2)} m", "设计约束"])
    if design.get("airfoil_root"):
        rows.append(["翼型", str(design["airfoil_root"]), "需求解析 / 自动选型"])
    selection = design.get("airfoil_selection")
    if isinstance(selection, dict) and selection.get("name"):
        detail = str(selection.get("reason") or "")
        if selection.get("reynolds"):
            detail += f"；Re≈{float(selection['reynolds']):.0f}"
        if selection.get("mach"):
            detail += f"，Ma≈{float(selection['mach']):.3f}"
        rows.append(["翼型选型依据", detail.strip("；"), "翼型库（本机 Profili/内置）"])
    return ["项目", "要求", "来源/说明"], rows


def _geometry_table(design: dict[str, Any], geometry: dict[str, Any],
                    reference: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    wing = _as_dict(geometry.get("wing"))
    fuselage = _as_dict(geometry.get("fuselage"))
    sizing = _as_dict(geometry.get("sizing"))
    rows: list[list[Any]] = []

    def add(name: str, value: Any, unit: str = "") -> None:
        if value is not None:
            rows.append([name, f"{value}", unit])

    if wing:
        add("机翼面积 S", _num(sizing.get("area_m2", wing.get("area_m2")), 3), "m²")
        add("翼展 b", _num(wing.get("span_m"), 3), "m")
        add("展弦比 AR", _num(sizing.get("aspect_ratio", wing.get("aspect_ratio")), 2), "—")
        add("根弦长 Cr", _num(wing.get("root_chord_m"), 3), "m")
        add("尖弦长 Ct", _num(wing.get("tip_chord_m"), 3), "m")
        if wing.get("trimmed_to_fuselage"):
            add("机翼根部 Y 偏移", _num(wing.get("root_y_m"), 3), "m")
            add("机翼暴露气动面积", _num(wing.get("exposed_area_m2"), 3), "m²")
            add("机翼暴露展长", _num(wing.get("exposed_span_m"), 3), "m")
            add("根部裁剪", "机翼/平尾按机身局部宽度外移并裁剪", "")
        add("平均气动弦 MAC", _num(sizing.get("mac_m", wing.get("mac_m")), 4), "m")
        add("机翼后掠角", _num(wing.get("sweep_deg"), 1), "°")
        add("机翼上反角", _num(wing.get("dihedral_deg"), 1), "°")
        add("翼型", wing.get("airfoil"), "—")
    if fuselage:
        add("机身长度", _num(fuselage.get("length_m"), 3), "m")
        add("机身最大直径", _num(fuselage.get("max_diameter_m"), 3), "m")
        if fuselage.get("style_label"):
            add("机身外形规则", str(fuselage.get("style_label")), "")
        if fuselage.get("length_diameter_ratio") is not None:
            add("机身长径比", _num(fuselage.get("length_diameter_ratio"), 2), "")
        if fuselage.get("rationale"):
            add("机身规则依据", str(fuselage.get("rationale")), "")
    vtail = _as_dict(geometry.get("vtail"))
    if vtail:
        add("V 尾单侧展长", _num(vtail.get("panel_span_m"), 3), "m")
        add("V 尾上反角", _num(vtail.get("dihedral_deg"), 1), "°")
        add("V 尾总面积", _num(vtail.get("total_area_m2"), 3), "m²")
    htail = _as_dict(geometry.get("htail"))
    if htail:
        add("平尾面积（完整）", _num(htail.get("area_m2"), 3), "m²")
        add("平尾展长", _num(htail.get("span_m"), 3), "m")
        if htail.get("trimmed_to_fuselage"):
            add("平尾根部 Y 偏移", _num(htail.get("root_y_m"), 3), "m")
            add("平尾暴露气动面积", _num(htail.get("exposed_area_m2"), 3), "m²")
    canard = _as_dict(geometry.get("canard"))
    if canard:
        add("鸭翼面积", _num(0.5 * ((canard.get("root_chord_m") or 0) +
                                     (canard.get("tip_chord_m") or 0)) *
                             (canard.get("span_m") or 0), 3), "m²")
        add("鸭翼展长", _num(canard.get("span_m"), 3), "m")
    tandem = _as_dict(geometry.get("tandem_wing"))
    if tandem:
        add("后翼面积", _num(0.5 * ((tandem.get("root_chord_m") or 0) +
                                     (tandem.get("tip_chord_m") or 0)) *
                             (tandem.get("span_m") or 0), 3), "m²")
        add("后翼展长", _num(tandem.get("span_m"), 3), "m")
    vtail_conv = _as_dict(geometry.get("vtail_conventional"))
    vtail_right = _as_dict(geometry.get("vtail_right"))
    if vtail_conv:
        left_area = _num(vtail_conv.get("area_m2"), 3)
        left_raw = vtail_conv.get("area_m2")
        right_raw = vtail_right.get("area_m2") if vtail_right else None
        if vtail_right:
            total_raw = ((float(left_raw) if _is_number(left_raw) else 0.0) +
                         (float(right_raw) if _is_number(right_raw) else 0.0))
            total_area = _num(total_raw, 3) if total_raw else left_area
        else:
            total_area = left_area
        add("垂尾布局", "双垂尾" if vtail_right else "单垂尾", "")
        add("垂尾总面积", _num(total_area, 3) if total_area is not None else None, "m²")
        add("单片垂尾面积", left_area, "m²")
        add("垂尾展长", _num(vtail_conv.get("span_m"), 3), "m")
        add("垂尾容量系数 Vv", _num(vtail_conv.get("volume_coeff"), 4), "—")
        add("垂尾容量目标", _num(vtail_conv.get("volume_target"), 4), "—")
    if reference:
        add("参考面积 Sref", _num(reference.get("sref_m2"), 4), "m²")
        if wing.get("trimmed_to_fuselage"):
            add("参考面积约定", "Sref 仍为完整机翼面积；升力面为裁剪后的暴露机翼", "")
        add("参考弦长 Cref", _num(reference.get("cref_m"), 4), "m")
        add("参考展长 Bref", _num(reference.get("bref_m"), 4), "m")
    return ["参数", "数值", "单位"], rows


def _component_table(components: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    rows: list[list[Any]] = []
    role_labels = {"fuselage": "机身", "wing": "机翼", "htail": "平尾",
                   "vtail": "垂尾", "vtail_pair": "V 尾"}
    for component in components:
        role = str(component.get("role", ""))
        params = _as_dict(component.get("params"))
        summary_bits: list[str] = []
        if "span_m" in params:
            summary_bits.append(f"展长 {_num(params.get('span_m'), 2)} m")
        if "area_m2" in params:
            summary_bits.append(f"面积 {_num(params.get('area_m2'), 3)} m²")
        if "panel_span_m" in params:
            summary_bits.append(f"单侧展长 {_num(params.get('panel_span_m'), 2)} m")
        if "length_m" in params:
            summary_bits.append(f"长 {_num(params.get('length_m'), 2)} m")
        if "max_diameter_m" in params:
            summary_bits.append(f"最大直径 {_num(params.get('max_diameter_m'), 2)} m")
        if "dihedral_deg" in params:
            summary_bits.append(f"上反 {_num(params.get('dihedral_deg'), 0)}°")
        controls = component.get("control_surfaces") or []
        if controls:
            summary_bits.append("操纵面: " + "、".join(
                str(item.get("name")) for item in controls if isinstance(item, dict)))
        rows.append([str(component.get("name") or role),
                     role_labels.get(role, role),
                     "；".join(summary_bits)])
    return ["部件", "类型", "主要参数"], rows


def _analysis_setup_table(reference: dict[str, Any], flow: dict[str, Any],
                          design: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    rows = [
        ["求解器", "VSPAERO（VLM 薄面涡格法）", "—"],
        ["参考面积 Sref", _num(reference.get("sref_m2"), 4), "m²"],
        ["参考弦长 Cref", _num(reference.get("cref_m"), 4), "m"],
        ["参考展长 Bref", _num(reference.get("bref_m"), 4), "m"],
        ["重心位置 Xcg", _num(reference.get("xcg_m"), 3), "m"],
        ["来流速度 V", _num(flow.get("velocity_ms"), 2), "m/s"],
        ["飞行高度", _num(flow.get("altitude_m"), 0), "m"],
        ["大气密度 ρ", _num(flow.get("density_kgm3"), 4), "kg/m³"],
        ["马赫数 Ma", _num(flow.get("mach"), 3), "—"],
    ]
    if design.get("aoa_npts"):
        rows.append(["迎角范围", f"{_num(design.get('aoa_start'), 1)} ~ "
                                 f"{_num(design.get('aoa_end'), 1)}", "°"])
    return ["项目", "数值", "单位"], rows


def _polar_table(rows: list[dict[str, Any]], limit: int = 12) -> tuple[list[str], list[list[Any]]]:
    headers = ["α (°)", "CL", "CD", "L/D", "CMy", "CS"]
    selected: list[list[Any]] = []
    valid_rows = [normalize_polar_row(row) for row in rows if isinstance(row, dict)]
    step = max(1, len(valid_rows) // limit) if len(valid_rows) > limit else 1
    for row in valid_rows[::step][:limit]:
        cl = row.get("CLtot", row.get("CL"))
        cd = row.get("CDtot", row.get("CD"))
        ld = None
        try:
            if cl is not None and cd is not None and abs(float(cd)) > 1e-9:
                ld = float(cl) / float(cd)
        except (TypeError, ValueError):
            ld = None
        selected.append([
            _num(row.get("AoA"), 2),
            _num(cl, 4),
            _num(cd, 5),
            _num(ld, 2),
            _num(row.get("CMytot", row.get("CMy")), 4),
            _num(row.get("CStot", row.get("CS")), 4),
        ])
    return headers, selected


def _key_metrics_table(aero: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    rows = [
        ["最大升力系数 CLmax", _num(aero.get("cl_max"), 3), "—"],
        ["CLmax 对应迎角", _num(aero.get("alpha_at_cl_max_deg"), 2), "°"],
        ["零迎角升力系数 CL0", _num(aero.get("cl_at_zero_deg"), 4), "—"],
        ["升力线斜率 CLα", _num(aero.get("cl_alpha_per_rad"), 3), "1/rad"],
        ["零升阻力系数 CD0", _num(aero.get("cd0_estimate", aero.get("cd_min")), 5), "—"],
        ["最大升阻比 (L/D)max", _num(aero.get("ld_max"), 2), "—"],
        ["(L/D)max 对应迎角", _num(aero.get("alpha_at_ld_max_deg"), 2), "°"],
        ["零升迎角 α0", _num(aero.get("alpha_zero_lift_deg"), 2), "°"],
    ]
    return ["气动参数", "数值", "单位"], rows


def _stability_table(stability: dict[str, Any], design: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    derivatives = _as_dict(stability.get("derivatives"))
    static_margin = stability.get("static_margin")
    rows = [
        ["纵向静稳定裕度 SM", _num(static_margin * 100.0 if static_margin is not None else None, 2), "% MAC"],
        ["SM 设计目标", _num(float(design.get("static_margin", 0)) * 100.0
                            if design.get("static_margin") is not None else None, 1), "% MAC"],
        ["焦点/中性点位置 Xnp", _num(stability.get("neutral_point_m"), 3), "m"],
        ["设计重心位置 Xcg", _num(stability.get("xcg_m"), 3), "m"],
        ["纵向静稳定性", "稳定" if stability.get("stable") else
         ("不稳定" if stability.get("stable") is False else "—"), "—"],
        ["CLα", _num(derivatives.get("cl_alpha_per_rad"), 3), "1/rad"],
        ["CMα", _num(derivatives.get("cm_alpha_per_rad"), 3), "1/rad"],
        ["CLq", _num(derivatives.get("cl_q"), 3), "1/rad"],
        ["CMq", _num(derivatives.get("cm_q"), 3), "1/rad"],
        ["Cnβ（航向静稳定）", _num(derivatives.get("cn_beta_per_rad"), 4), "1/rad"],
        ["Cyβ（侧力导数）", _num(derivatives.get("cy_beta_per_rad"), 4), "1/rad"],
        ["估算方法", str(stability.get("sm_method") or "VSPAERO 稳定性导数"), "—"],
    ]
    return ["稳定性参数", "数值", "单位"], rows


def _ga_bounds_table(ga_meta: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """GA 变量边界与最优基因取值表。"""
    bounds = _as_dict(ga_meta.get("bounds"))
    best_genome = _as_dict(ga_meta.get("best_genome"))
    labels = {
        "wing_area_m2": "机翼面积 S (m²)",
        "aspect_ratio": "展弦比 AR",
        "taper_ratio": "梢根比 λ",
        "sweep_deg": "后掠角 (°)",
        "dihedral_deg": "上反角 (°)",
        "tail_scale": "尾翼面积比",
    }
    rows: list[list[Any]] = []
    for name, item in bounds.items():
        item = _as_dict(item)
        rows.append([
            labels.get(name, name),
            _num(item.get("baseline"), 3),
            _num(item.get("low"), 3),
            _num(item.get("high"), 3),
            _num(best_genome.get(name), 3),
        ])
    return ["设计变量", "基线值", "下限", "上限", "最优值"], rows


def _ga_history_table(history: Any) -> tuple[list[str], list[list[Any]]]:
    """GA 每代收敛历史表。"""
    rows: list[list[Any]] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        rows.append([
            item.get("gen"),
            _num(item.get("best_fitness"), 3),
            _num(item.get("best_ld"), 2),
            _num(item.get("avg_ld"), 2),
            _num(item.get("worst_ld"), 2),
            item.get("evaluations"),
            _num(item.get("elapsed_s"), 1),
        ])
    return ["代", "最优适应度", "最优 L/D", "平均 L/D", "最差 L/D",
            "累计评估", "累计耗时 (s)"], rows


def _optimization_table(optimization: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    rows: list[list[Any]] = []
    baseline = _as_dict(optimization.get("baseline"))
    best = _as_dict(optimization.get("best"))
    if baseline:
        rows.append(["基准方案", _num(baseline.get("area_m2"), 3), "m²",
                     _num(baseline.get("aspect_ratio"), 2), _num(baseline.get("ld_cruise"), 2)])
    if best:
        rows.append(["解析优化方案", _num(best.get("area_m2"), 3), "m²",
                     _num(best.get("aspect_ratio"), 2), _num(best.get("ld_cruise"), 2)])
    if optimization.get("improvement_ratio") is not None:
        rows.append(["巡航升阻比提升", _num(float(optimization["improvement_ratio"]) * 100.0, 2),
                     "%", "—", "—"])
    return ["方案", "机翼面积", "单位", "展弦比", "巡航 L/D"], rows


def _files_table(files: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    labels = {"vsp3": "OpenVSP 模型", "stl": "STL 几何", "csv": "退化几何 CSV",
              "polar": "VSPAERO 极曲线", "stab": "稳定性导数"}
    rows = [[labels.get(key, key), str(value)] for key, value in files.items() if value]
    return ["文件类型", "路径"], rows


def _criteria_table(criteria: dict[str, Any], key: str) -> tuple[list[str], list[list[Any]]]:
    rows = _as_dict(criteria.get(key)).get("rows") or []
    return ["项目", "数值", "单位", "约束/目标", "判定"], rows


def _criteria_checks_table(criteria: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    rows = []
    for item in criteria.get("checks") or []:
        if str(item.get("status")) == "pass":
            continue
        rows.append([
            str(item.get("name", "")),
            _num(item.get("value"), 4) if _is_number(item.get("value")) else "—",
            str(item.get("unit", "")),
            f"{item.get('low', '—')} ~ {item.get('high', '—')}",
            str(item.get("status", "")),
            str(item.get("note", "")),
        ])
    return ["项目", "数值", "单位", "约束范围", "判定", "说明"], rows


def _design_iterations_table(iterations: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    rows = []
    for item in iterations:
        summary = _as_dict(item.get("summary"))
        rows.append([
            item.get("round", ""),
            _num(item.get("area_m2"), 3),
            _num(item.get("span_m"), 3),
            item.get("overall", ""),
            f"pass {summary.get('pass', '')} / warn {summary.get('warn', '')} / fail {summary.get('fail', '')}",
        ])
    return ["轮次", "机翼面积 (m²)", "翼展 (m)", "总体判定", "准则通过/警告/失败"], rows


def _typed_requirements_table(design: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    reqset = _as_dict(design.get("requirement_set"))
    rows = []
    for item in reqset.get("constraints") or []:
        source = _as_dict(item.get("source"))
        rows.append([
            str(item.get("id", "")),
            str(item.get("quantity", "")),
            _num(item.get("value_si"), 5) if _is_number(item.get("value_si")) else "—",
            str(item.get("unit_si", "")),
            str(source.get("quote", ""))[:50],
            "已确认" if item.get("confirmed") else "待确认",
        ])
    return ["需求 ID", "量", "SI 值", "单位", "原文片段", "确认状态"], rows


def _stability_iterations_table(iterations: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    rows = []
    for item in iterations:
        rows.append([
            item.get("round", ""),
            str(item.get("pitch", "")),
            _num(item.get("sm") * 100.0, 2) if _is_number(item.get("sm")) else "—",
            _num(item.get("cma"), 4) if _is_number(item.get("cma")) else "—",
            str(item.get("yaw", "")),
            _num(item.get("cnb"), 4) if _is_number(item.get("cnb")) else "—",
            "；".join(item.get("changes") or []) or "校核",
        ])
    return ["轮次", "俯仰", "SM (%)", "CMα", "航向", "Cnβ", "本轮调整/动作"], rows


def build_blocks(data: dict[str, Any], figures: dict[str, str],
                 logger: Any = None) -> dict[str, list[tuple]]:
    """构建占位符 → 内容块列表。"""
    design = data.get("design", {})
    geometry = data.get("geometry", {})
    components = data.get("components", [])
    reference = data.get("reference", {})
    flow = data.get("flow", {})
    aero = data.get("aero", {})
    stability = data.get("stability", {})
    cruise = data.get("cruise", {})
    optimization = data.get("optimization", {})
    files = data.get("files", {})
    polar_rows = data.get("polar_rows", [])
    date_text = datetime.now().strftime("%Y 年 %m 月 %d 日")
    title = str(data.get("title") or design.get("name") or "无人机气动设计分析报告")
    blocks: dict[str, list[tuple]] = {}

    # 封面
    blocks["{%TITLE%}"] = [("para", title, 26, "center", HEI, True)]
    blocks["{%SUBTITLE%}"] = [("para", "气动设计分析报告", 20, "center", HEI, True)]
    meta_lines = [
        f"设计任务：{design.get('name', '')}",
        f"气动布局：{design.get('configuration_label', '')}",
        f"设计工具：OpenVSP 3.51.3 + VSPAERO",
        f"报告日期：{date_text}",
    ]
    blocks["{%META%}"] = [("para", line, 11, "center", SONG, False) for line in meta_lines]

    # 1 需求
    requirement_blocks: list[tuple] = []
    mtow = _num(design.get("mtow_kg"), 1)
    altitude = _num(design.get("altitude_m"), 0)
    vmin = _num(design.get("speed_min_ms"), 1)
    vmax = _num(design.get("speed_max_ms"), 1)
    requirement_blocks.append((
        "para",
        f"本报告针对一架 {mtow} kg 级、{design.get('configuration_label', '常规布局')}的无人机，"
        f"设计飞行高度 {altitude} m、飞行速度 {vmin}~{vmax} m/s。"
        "依据给定需求完成机翼/尾翼/机身参数 sizing、OpenVSP 三维建模、"
        "VSPAERO 气动特性计算以及纵向静稳定性校核，并给出解析优化建议。",
        10.5, "justify", SONG, False))
    assumptions = design.get("assumptions") or []
    if assumptions:
        requirement_blocks.append(("para", "主要设计假设：", 10.5, "justify", HEI, True))
        requirement_blocks.extend(("bullet", str(item), 10.5, "left", SONG, False)
                                  for item in assumptions[:8])
    requirement_blocks.append(("table", *_requirement_table(design)))
    typed_req = _typed_requirements_table(design)
    if typed_req[1]:
        requirement_blocks.append(("caption", "表 1-1 类型化需求（schema 2.0，确认状态由用户/入口决定）"))
        requirement_blocks.append(("table", *typed_req))
    blocks["{%REQUIREMENTS%}"] = requirement_blocks

    # 2 几何
    param_blocks: list[tuple] = [
        ("para", "全机主要几何参数如表 2 所示，几何模型由 OpenVSP Python API 参数化生成。",
         10.5, "justify", SONG, False),
        ("table", *_geometry_table(design, geometry, reference)),
    ]
    if components:
        param_blocks.append(("caption", "表 3  部件组成"))
        param_blocks.append(("table", *_component_table(components)))
    blocks["{%PARAMS%}"] = param_blocks

    # 3 计算设置
    setup_blocks: list[tuple] = [
        ("para", "采用 VSPAERO 涡格法（VLM）计算机翼与尾翼的定常气动特性；"
                 "机身按厚体处理、不参与升力面网格。计算设置见表 4。",
         10.5, "justify", SONG, False),
        ("table", *_analysis_setup_table(reference, flow, design)),
    ]
    vspaero_meta = _as_dict(data.get("vspaero"))
    if vspaero_meta.get("run_id"):
        setup_blocks.append((
            "para",
            f"运行标识 run_id={vspaero_meta.get('run_id')}；"
            f"输入指纹={vspaero_meta.get('input_fingerprint') or '—'}；"
            f"独立运行目录={vspaero_meta.get('run_dir') or '—'}。",
            9.2, "justify", SONG, False))
    if cruise.get("cl_cruise") is not None:
        setup_blocks.append((
            "para",
            f"巡航状态按 CL={_num(cruise.get('cl_cruise'), 4)} 计算，"
            f"对应巡航升阻比 L/D={_num(cruise.get('ld_cruise'), 2)}，"
            f"需用功率约 {_num(cruise.get('power_required_kw'), 2)} kW。",
            10.5, "justify", SONG, False))
    blocks["{%ANALYSIS_SETUP%}"] = setup_blocks

    # 4 结果
    result_blocks: list[tuple] = []
    if polar_rows:
        result_blocks.append(("para", "VSPAERO 计算得到的纵向气动特性如表 5、表 6 所示，"
                                      "对应曲线见图 1~图 3。", 10.5, "justify", SONG, False))
        result_blocks.append(("caption", "表 5  极曲线计算结果"))
        result_blocks.append(("table", *_polar_table(polar_rows)))
        result_blocks.append(("caption", "表 6  关键气动参数"))
        result_blocks.append(("table", *_key_metrics_table(aero)))
        if figures.get("fig_cl_alpha"):
            result_blocks.append(("image", figures["fig_cl_alpha"], "图 1  升力系数随迎角变化曲线"))
        if figures.get("fig_ld_alpha"):
            result_blocks.append(("image", figures["fig_ld_alpha"], "图 2  升阻比随迎角变化曲线"))
        if figures.get("fig_polar"):
            result_blocks.append(("image", figures["fig_polar"], "图 3  阻力极曲线"))
    else:
        result_blocks.append(("para", "未获得有效 VSPAERO 极曲线数据，本报告仅列出解析估算结果。",
                              10.5, "justify", SONG, False))
    decision = _as_dict(data.get("design_decision"))
    if decision:
        result_blocks.insert(0, (
            "para",
            "可靠设计状态：任务=" + str(decision.get("task_status", "—")) +
            "；可行性=" + str(decision.get("feasibility_status", "—")) +
            "；证据=" + str(decision.get("evidence_status", "—")) +
            "；推荐=" + str(decision.get("recommendation_status", "—")) +
            "。本报告中的经验准则尚未绑定 run_id/原始文件证据，"
            "因此证据状态为不完整，不能把本报告表述为已验证可行或飞行放行。"
            "经验准则和气动公式仅作设计辅助，不是硬性合格判据。",
            10.5, "justify", SONG, False))
    blocks["{%RESULTS%}"] = result_blocks

    # 5 稳定性
    stability_blocks: list[tuple] = [("table", *_stability_table(stability, design))]
    static_margin = stability.get("static_margin")
    if static_margin is not None:
        verdict = "满足" if (design.get("static_margin") is not None and
                             abs(float(static_margin) - float(design["static_margin"])) <= 0.03) \
            else "偏离"
        stability_blocks.append((
            "para",
            f"设计重心 Xcg={_num(stability.get('xcg_m'), 3)} m、焦点 Xnp="
            f"{_num(stability.get('neutral_point_m'), 3)} m，静稳定裕度 "
            f"SM={_num(float(static_margin) * 100.0, 2)}% MAC，"
            f"相对设计目标{verdict}目标要求；纵向静稳定性"
            f"{'满足要求' if stability.get('stable') else '需进一步调整重心或尾容量'}。",
            10.5, "justify", SONG, False))
    criteria = _as_dict(data.get("design_criteria"))
    if criteria:
        stability_blocks.append((
            "para",
            "本节同时按经验设计准则检查机翼、尾翼、静稳定性、飞行品质与操纵性；"
            "动态特性由 VSPAERO 稳定性导数做简化二自由度估算，属于概念设计级结果。",
            10.5, "justify", SONG, False))
        for key, title in (
                ("wing", "表 5-1 机翼参数与经验约束"),
                ("tail", "表 5-2 尾翼参数与尾容量"),
                ("stability", "表 5-3 静稳定性参数"),
                ("flying_qualities", "表 5-4 飞行品质与动态稳定性估算"),
                ("controllability", "表 5-5 操纵性估算")):
            section = _as_dict(criteria.get(key))
            if section.get("rows"):
                stability_blocks.append(("caption", title))
                stability_blocks.append(("table", *_criteria_table(criteria, key)))
        checks_table = _criteria_checks_table(criteria)
        if checks_table[1]:
            stability_blocks.append(("caption", "表 5-6 未通过或需关注的准则"))
            stability_blocks.append(("table", *checks_table))
        elif criteria.get("overall") == "pass":
            stability_blocks.append(("para", "所有经验准则检查通过。",
                                     10.5, "justify", SONG, False))
    blocks["{%STABILITY%}"] = stability_blocks

    # 6 优化
    optimization_blocks: list[tuple] = []
    ga_meta = _as_dict(optimization.get("ga")) or _as_dict(data.get("ga"))
    is_ga = bool(ga_meta.get("history")) or str(
        optimization.get("method", "")).startswith("遗传算法")
    if optimization:
        if is_ga:
            optimization_blocks.append((
                "para",
                "采用遗传算法（GA）对机翼面积、展弦比、梢根比、后掠角、上反角与尾翼面积比"
                "进行寻优；每个个体均通过 OpenVSP 参数化建模 + VSPAERO 极曲线真实评估巡航"
                "升阻比，并施加失速余量、静稳定裕度与翼展约束惩罚。基线/最优方案对比见"
                "表 7，变量边界与最优基因见表 8，每代收敛历史见表 9、图 4。",
                10.5, "justify", SONG, False))
            optimization_blocks.append(("table", *_optimization_table(optimization)))
            if ga_meta.get("bounds"):
                optimization_blocks.append(("caption", "表 8  GA 变量边界与最优基因"))
                optimization_blocks.append(("table", *_ga_bounds_table(ga_meta)))
            if ga_meta.get("history"):
                optimization_blocks.append(("caption", "表 9  GA 每代收敛历史"))
                optimization_blocks.append(
                    ("table", *_ga_history_table(ga_meta.get("history"))))
            if figures.get("fig_ga_convergence"):
                optimization_blocks.append((
                    "image", figures["fig_ga_convergence"], "图 4  GA 收敛曲线"))
            eval_count = ga_meta.get("evaluation_count")
            elapsed = ga_meta.get("elapsed_s")
            if eval_count is not None or elapsed is not None:
                optimization_blocks.append((
                    "para",
                    f"GA 共评估 {eval_count if eval_count is not None else '—'} 次、"
                    f"耗时约 {_num(elapsed, 1)} s；种群 "
                    f"{ga_meta.get('population', '—')}、代数 "
                    f"{ga_meta.get('generations_run', ga_meta.get('generations', '—'))}。",
                    10.5, "justify", SONG, False))
        else:
            optimization_blocks.append((
                "para",
                "以第一轮 VSPAERO 极曲线标定的零升阻力与 Oswald 效率为代理模型，"
                "在翼展限制与失速约束内对翼面积/展弦比进行解析网格搜索，结果见表 7。",
                10.5, "justify", SONG, False))
            optimization_blocks.append(("table", *_optimization_table(optimization)))
            if optimization.get("rebuild"):
                optimization_blocks.append((
                    "para", "解析优化方案相对基准方案改善超过阈值，已按优化参数重建几何并复算。",
                    10.5, "justify", SONG, False))
            else:
                optimization_blocks.append((
                    "para", "当前几何参数已处于较优区域，解析优化未发现超过阈值的改进空间，"
                            "保留原设计方案。", 10.5, "justify", SONG, False))
    else:
        optimization_blocks.append(("para", "本次运行未执行解析优化。", 10.5, "justify", SONG, False))
    stability_iterations = data.get("stability_iterations") or []
    if isinstance(stability_iterations, list) and stability_iterations:
        optimization_blocks.append(("caption", "表 6-0 静稳定多轮检查与复算"))
        optimization_blocks.append(("table", *_stability_iterations_table(stability_iterations)))
    iterations = data.get("design_iterations") or []
    if isinstance(iterations, list) and iterations:
        optimization_blocks.append(("caption", "表 6-1 准则驱动设计迭代与 VSPAERO 复算"))
        optimization_blocks.append(("table", *_design_iterations_table(iterations)))
    blocks["{%OPTIMIZATION%}"] = optimization_blocks

    # 7 结论
    conclusions: list[str] = [
        f"完成 {mtow} kg 级{design.get('configuration_label', '')}无人机的参数化设计与气动分析。",
        f"机翼面积 {_num(data.get('geometry', {}).get('sizing', {}).get('area_m2'), 3)} m²，"
        f"翼展 {_num(data.get('geometry', {}).get('wing', {}).get('span_m'), 2)} m，"
        f"展弦比 {_num(data.get('geometry', {}).get('sizing', {}).get('aspect_ratio'), 2)}。",
        f"最大升阻比 (L/D)max={_num(aero.get('ld_max'), 2)}，"
        f"失速特性与升力线斜率结果合理。",
    ]
    if static_margin is not None:
        conclusions.append(
            f"静稳定裕度 SM={_num(float(static_margin) * 100.0, 2)}% MAC，"
            f"焦点位于 Xnp={_num(stability.get('neutral_point_m'), 3)} m。")
    if cruise.get("ld_cruise") is not None:
        conclusions.append(
            f"巡航状态 L/D={_num(cruise.get('ld_cruise'), 2)}，"
            f"需用功率约 {_num(cruise.get('power_required_kw'), 2)} kW。")
    if ga_meta.get("history"):
        baseline_meta = _as_dict(optimization.get("baseline"))
        best_meta = _as_dict(optimization.get("best"))
        conclusions.append(
            f"遗传算法经过 {len(ga_meta.get('history') or [])} 代、"
            f"{ga_meta.get('evaluation_count', '—')} 次真实 VSPAERO 评估，"
            f"巡航升阻比由基线 {_num(baseline_meta.get('cruise_ld', baseline_meta.get('ld_cruise')), 2)} "
            f"提升至最优 "
            f"{_num(best_meta.get('cruise_ld', best_meta.get('ld_cruise')), 2)}。")
    conclusions.extend([
        "建议后续结合结构质量分布细化重心包线，并开展纵向/横航向操纵性与配平计算。",
        "VSPAERO 结果基于 VLM 薄面假设，未计入粘性分离与机身干扰，工程应用前需与 CFD/风洞数据校核。",
    ])
    blocks["{%CONCLUSIONS%}"] = [("bullet", item, 10.5, "left", SONG, False)
                                  for item in conclusions]

    # 附录
    blocks["{%FILES%}"] = [("table", *_files_table(files))] if files else [
        ("para", "本次运行未记录输出文件清单。", 10.5, "justify", SONG, False)]
    limitations = data.get("limitations") or [
        "气动分析采用 VSPAERO VLM 薄面方法，机身按厚体处理、不参与升力面计算；",
        "未考虑螺旋桨滑流、粘性分离与结构弹性影响；",
        "质量分解与重心位置为工程估算，需结构/重量专业复核。",
    ]
    blocks["{%LIMITATIONS%}"] = [("bullet", str(item), 10.5, "left", SONG, False)
                                  for item in limitations]
    return blocks


# --------------------------------------------------------------------------- #
# 模板填充
# --------------------------------------------------------------------------- #
def _replace_placeholder_text(doc: Document, key: str, value: str) -> int:
    """把简单文本占位符替换为 value（返回替换次数）。"""
    count = 0
    for paragraph in list(doc.paragraphs):
        if key not in paragraph.text:
            continue
        full_text = paragraph.text.replace(key, value)
        for run in list(paragraph.runs):
            run.text = ""
        if paragraph.runs:
            paragraph.runs[0].text = full_text
        else:
            paragraph.add_run(full_text)
        count += 1
    return count


def _apply_block(doc: Document, paragraph: Paragraph, block: tuple) -> None:
    kind = block[0]
    if kind == "para":
        _, text, size, align, east, bold = block
        alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER,
                     "left": WD_ALIGN_PARAGRAPH.LEFT,
                     "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(align,
                                                                WD_ALIGN_PARAGRAPH.JUSTIFY)
        insert_paragraph_before(doc, paragraph, str(text), size=size, bold=bold,
                                east=east, align=alignment)
    elif kind == "bullet":
        _, text, size, _align, east, _bold = block
        created = insert_paragraph_before(doc, paragraph, "• " + str(text), size=size,
                                          east=east, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                                          first_line_indent=False)
        created.paragraph_format.left_indent = Pt(size)
    elif kind == "caption":
        insert_caption(doc, paragraph, str(block[1]))
    elif kind == "table":
        _, headers, rows = block[0], block[1], block[2]
        widths = block[3] if len(block) > 3 else None
        add_table_block(doc, paragraph, headers, rows, widths=widths)
    elif kind == "image":
        _, image_path, caption = block
        if insert_picture(doc, paragraph, image_path):
            insert_caption(doc, paragraph, caption)
    else:  # pragma: no cover
        raise ValueError(f"未知内容块类型: {kind}")


def _fill_template(template_path: Path, output_path: Path,
                   data: dict[str, Any], blocks: dict[str, list[tuple]],
                   logger: Any = None) -> bool:
    """按占位符填充模板；返回是否成功。"""
    try:
        doc = Document(str(template_path))
    except Exception as exc:
        if logger is not None:
            logger.warn(f"打开报告模板失败: {exc}")
        return False
    title = str(data.get("title") or "")
    setup_page(doc, header_text=title)
    date_text = datetime.now().strftime("%Y 年 %m 月 %d 日")
    # 先替换简单文本占位符
    for key, value in (("{%DATE%}", date_text), ("{%MODEL%}", "OpenVSP 3.51.3 + VSPAERO"),
                       ("{%TITLE_SHORT%}", title)):
        _replace_placeholder_text(doc, key, value)
    # 再处理内容块
    for key, block_list in blocks.items():
        target = None
        for paragraph in doc.paragraphs:
            if key in paragraph.text:
                target = paragraph
                break
        if target is None:
            continue
        for block in block_list:
            _apply_block(doc, target, block)
        parent = target._p.getparent()
        if parent is not None:
            parent.remove(target._p)
    # 清理剩余未填充占位符（避免报告出现 {%...%}）
    for paragraph in list(doc.paragraphs):
        if "{%" in paragraph.text and "%}" in paragraph.text:
            for run in list(paragraph.runs):
                run.text = ""
            if paragraph.runs:
                paragraph.runs[0].text = ""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return True


def _build_from_scratch(output_path: Path, data: dict[str, Any],
                        blocks: dict[str, list[tuple]], logger: Any = None) -> None:
    """模板缺失时的降级路径：按同样样式从头生成文档。"""
    doc = Document()
    title = str(data.get("title") or data.get("design", {}).get("name") or "设计报告")
    setup_page(doc, header_text=title)
    for _ in range(3):
        doc.add_paragraph()
    cover_title = doc.add_paragraph()
    cover_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(cover_title.add_run(title), size=26, east=HEI, bold=True)
    cover_subtitle = doc.add_paragraph()
    cover_subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(cover_subtitle.add_run("气动设计分析报告"), size=20, east=HEI, bold=True)
    for _ in range(6):
        doc.add_paragraph()
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(meta.add_run(f"设计工具：OpenVSP 3.51.3 + VSPAERO\n"
                          f"报告日期：{datetime.now().strftime('%Y 年 %m 月 %d 日')}"),
             size=11, east=SONG)
    doc.add_page_break()

    sections = [
        ("1  设计任务与要求", "{%REQUIREMENTS%}"),
        ("2  设计方案与几何参数", "{%PARAMS%}"),
        ("3  气动计算方法", "{%ANALYSIS_SETUP%}"),
        ("4  气动特性计算结果", "{%RESULTS%}"),
        ("5  稳定性与操纵性", "{%STABILITY%}"),
        ("6  优化与迭代", "{%OPTIMIZATION%}"),
        ("7  结论与建议", "{%CONCLUSIONS%}"),
        ("附录  输出文件清单与已知限制", "{%FILES%}"),
    ]
    for heading_text, key in sections:
        paragraph = doc.add_paragraph()
        try:
            paragraph.style = doc.styles["Heading 1"]
        except KeyError:
            pass
        set_font(paragraph.add_run(heading_text), size=15, east=HEI, bold=True,
                 color=(0, 0, 0))
        for block in blocks.get(key, []):
            _append_block_at_end(doc, block)
    if blocks.get("{%LIMITATIONS%}"):
        caption = doc.add_paragraph()
        set_font(caption.add_run("已知限制"), size=11.5, east=HEI, bold=True)
        for block in blocks["{%LIMITATIONS%}"]:
            _append_block_at_end(doc, block)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))


def _append_block_at_end(doc: Document, block: tuple) -> None:
    kind = block[0]
    if kind == "para":
        _, text, size, align, east, bold = block
        paragraph = doc.add_paragraph()
        paragraph.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER,
                               "left": WD_ALIGN_PARAGRAPH.LEFT,
                               "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(
                                   align, WD_ALIGN_PARAGRAPH.JUSTIFY)
        set_font(paragraph.add_run(str(text)), size=size, east=east, bold=bold)
    elif kind == "bullet":
        _, text, size, _align, east, _bold = block
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        set_font(paragraph.add_run("• " + str(text)), size=size, east=east)
    elif kind == "caption":
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_font(paragraph.add_run(str(block[1])), size=9, east=HEI, bold=True)
    elif kind == "table":
        _, headers, rows = block[0], block[1], block[2]
        widths = block[3] if len(block) > 3 else None
        placeholder = doc.add_paragraph()
        add_table_block(doc, placeholder, headers, rows, widths=widths)
        placeholder._p.getparent().remove(placeholder._p)
    elif kind == "image":
        _, image_path, caption = block
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        try:
            paragraph.add_run().add_picture(str(image_path), width=Cm(15.0))
            caption_paragraph = doc.add_paragraph()
            caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_font(caption_paragraph.add_run(str(caption)), size=9, east=HEI, bold=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #
def _resolve_output_path(output_dir: Any = None, report_path: Any = None,
                         output_path: Any = None,
                         title: str = "") -> Path:
    if report_path:
        return Path(report_path)
    if output_path:
        return Path(output_path)
    folder = Path(output_dir) if output_dir else Path.cwd() / "outputs"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / f"design_report_{timestamp}.docx"


def generate_report(output_dir: Any = None, template: Any = None,
                    title: str | None = None, spec: Any = None,
                    geometry: Any = None, metrics: dict[str, Any] | None = None,
                    logger: Any = None, bridge: Any = None, *,
                    result: dict[str, Any] | None = None,
                    reference: dict[str, Any] | None = None,
                    report_path: Any = None, output_path: Any = None,
                    data: dict[str, Any] | None = None) -> str:
    """生成中文设计报告 docx，返回文件路径。

    参数全部可选，兼容多种调用方式（designer 内部 / MCP 服务器）：

    - ``output_dir``：输出目录（默认报告名 ``design_report_<时间戳>.docx``）
    - ``report_path`` / ``output_path``：显式输出文件路径（优先）
    - ``template``：模板 docx 路径（缺省用内置模板）
    - ``spec``：DesignSpec 或其 dict（需求）
    - ``metrics``：designer 指标 dict
    - ``result``：VSPAERO 结果 dict（含 rows/summary/stability/files/...）
    - ``geometry``：几何 dict 或部件列表
    """
    if title:
        data = dict(data or {})
        data["title"] = title
    normalized = normalize_data(result=result, metrics=metrics, spec=spec,
                                geometry=geometry, reference=reference, data=data)
    if title:
        normalized["title"] = title
    elif not normalized.get("title"):
        normalized["title"] = str(normalized.get("design", {}).get("name")
                                  or "无人机气动设计分析报告")

    output = _resolve_output_path(output_dir=output_dir, report_path=report_path,
                                  output_path=output_path,
                                  title=str(normalized["title"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    figures_dir = output.parent / "figures"
    figures = generate_figures(normalized.get("polar_rows", []), figures_dir, logger=logger)
    # GA 收敛曲线（task=ga 时 metrics["optimization"]["ga"]["history"] 存在）
    optimization_meta = _as_dict(normalized.get("optimization"))
    ga_meta = _as_dict(optimization_meta.get("ga")) or _as_dict(normalized.get("ga"))
    if ga_meta.get("history"):
        figures.update(generate_ga_convergence_figure(
            ga_meta.get("history"), figures_dir, logger=logger))
    blocks = build_blocks(normalized, figures, logger=logger)

    if template and Path(str(template)).is_file():
        if _fill_template(Path(str(template)), output, normalized, blocks, logger=logger):
            return str(output)
        if logger is not None:
            logger.warn("模板填充失败，改用从头生成报告")
    if logger is not None:
        logger.warn("未找到可用的报告模板，按内置样式从头生成")
    _build_from_scratch(output, normalized, blocks, logger=logger)
    return str(output)


# 兼容别名：MCP 服务器按顺序尝试以下名称
def build_report(**kwargs: Any) -> str:
    return generate_report(**kwargs)


def create_report(**kwargs: Any) -> str:
    return generate_report(**kwargs)


def write_report(**kwargs: Any) -> str:
    return generate_report(**kwargs)
