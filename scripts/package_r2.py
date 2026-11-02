"""Build the OpenVSP 3.51.3-zh-CN R2 portable distribution.

Inputs:
  - R1 installed payload (baseline app files)
  - newly built vsp.exe (with AI menu/dialog)
  - R2 ai/ + mcp/ + docs/ + patches/

Output:
  - dist/OpenVSP-3.51.3-zh-CN-r2/ (portable folder)
  - dist/OpenVSP-3.51.3-zh-CN-r2-Portable.zip
  - dist/OpenVSP-3.51.3-zh-CN-r2-Portable.zip.sha256
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

R2 = Path(r"D:\Zcoding\OpenVSP-R2")
SRC = Path(r"D:\Zcoding\OpenVSP-3.51.3-win64\work\OpenVSP-3.51.3-src")
BASE_PAYLOAD = SRC / "work" / "windows-package" / "candidate03" / "payload"
PY_API_SRC = Path(r"D:\Zcoding\OpenVSP-3.51.3-win64\python")
NEW_EXE = SRC / "build" / "vsp" / "src" / "vsp" / "Release" / "vsp.exe"
DIST = R2 / "dist"
STAGE = DIST / "OpenVSP-3.51.3-zh-CN-r2"
ZIP_BASE = DIST / "OpenVSP-3.51.3-zh-CN-r2-Portable"
VERSION = "3.51.3-zh-CN-r2"
BUILD_DATE = "2026-11-01"

IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "_scratch", ".pytest_cache",
    "*.pdb", "*.ilk", "*.obj", "*.lib", "*.exp",
    "offline_run", "*.log", "workspace", "*.tmp",
    "runs", "pdf_pages", "opt_plan_pages", "outputs", "data", "tests",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        print("WARN missing directory:", src)
        return
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=IGNORE)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    if not BASE_PAYLOAD.is_dir():
        print("ERROR: R1 payload not found:", BASE_PAYLOAD)
        return 2
    if not NEW_EXE.is_file():
        print("ERROR: new vsp.exe not found:", NEW_EXE)
        return 2

    if STAGE.exists():
        shutil.rmtree(STAGE)
    DIST.mkdir(parents=True, exist_ok=True)

    print("1) copy R1 payload ->", STAGE)
    shutil.copytree(BASE_PAYLOAD, STAGE, dirs_exist_ok=True)

    print("2) overwrite vsp.exe")
    shutil.copy2(NEW_EXE, STAGE / "vsp.exe")

    print("2b) add OpenVSP Python API (needed by AI backend)")
    if PY_API_SRC.is_dir():
        for sub in ("openvsp", "degen_geom", "utilities", "vsp_airfoils"):
            source = PY_API_SRC / sub
            if not source.is_dir():
                continue
            target = STAGE / "python" / sub
            shutil.copytree(source, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.exe", "__pycache__", "*.pyc"))
        # openvsp.setup_vspaero_path()/CheckForVSPAERO() requires vspaero.exe +
        # vspviewer.exe + vsploads.exe in python/openvsp/openvsp; keep those small
        # copies so GetVSPAEROPath() and MCP solvers work from the portable package.
        api_bin_dir = PY_API_SRC / "openvsp" / "openvsp"
        for exe_name in ("vspaero.exe", "vspviewer.exe", "vsploads.exe"):
            exe_src = api_bin_dir / exe_name
            if exe_src.is_file():
                shutil.copy2(exe_src, STAGE / "python" / "openvsp" / "openvsp" / exe_name)
    else:
        print("WARN Python API source missing:", PY_API_SRC)

    print("3) add ai/ mcp/ docs/ patches/")
    copy_tree(R2 / "ai", STAGE / "ai")
    copy_tree(R2 / "mcp", STAGE / "mcp")
    copy_tree(R2 / "docs", STAGE / "docs")
    # 发布包只带可复用、轻量的验证证据；审查原始日志/大型审计目录不进入便携包
    VALIDATION_KEEP = ("reliable_design_v2", "layouts", "fuselage_styles",
                       "airfoil_auto_test")
    validate_stage = STAGE / "validation"
    validate_stage.mkdir(parents=True, exist_ok=True)
    for name in VALIDATION_KEEP:
        copy_tree(R2 / "validation" / name, validate_stage / name)
    (STAGE / "patches").mkdir(exist_ok=True)

    # source changes snapshot
    src_changes = STAGE / "patches" / "source-changes"
    for rel in ("src/gui_and_draw/AIDesignWindow.h",
                "src/gui_and_draw/AIDesignWindow.cpp",
                "src/gui_and_draw/MainVSPScreen.h",
                "src/gui_and_draw/MainVSPScreen.cpp",
                "src/gui_and_draw/CMakeLists.txt"):
        source = SRC / rel
        target = src_changes / rel
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    try:
        patch = subprocess.run(
            ["git", "-C", str(SRC), "diff", "--", "src/gui_and_draw"],
            check=True, capture_output=True, text=True, encoding="utf-8",
            errors="replace").stdout
        write_text(STAGE / "patches" / "r2-ui.patch", patch)
    except Exception as exc:  # pragma: no cover - optional
        print("WARN git diff failed:", exc)

    # root-level docs / scripts
    print("4) write README / version / setup scripts")
    write_text(STAGE / "README-R2-zh-CN.txt", README_R2)
    write_text(STAGE / "VERSION-R2.txt", json.dumps({
        "product": f"OpenVSP-{VERSION}",
        "base": "OpenVSP 3.51.3-zh-CN-r1",
        "date": BUILD_DATE,
        "changes": [
            "AI 设计模块（FLTK 菜单/对话框 + WinHTTP 模型列表 + DPAPI 密钥 + Python 后端）",
            "内置设计报告模板与自动报告生成",
            "OpenVSP MCP 服务器（stdio legacy 2025-11-25）",
            "R2 文档、源码补丁、便携打包",
        ],
        "python_api_bundled": (STAGE / "python" / "openvsp" / "openvsp" / "_vsp.pyd").is_file(),
        "vsp_exe_sha256": sha256(STAGE / "vsp.exe"),
    }, ensure_ascii=False, indent=2))

    if (R2 / "docs" / "02-AI设计模块使用说明.md").is_file():
        shutil.copy2(R2 / "docs" / "02-AI设计模块使用说明.md",
                     STAGE / "AI设计模块使用说明.md")
    if (R2 / "docs" / "03-MCP服务器使用说明.md").is_file():
        shutil.copy2(R2 / "docs" / "03-MCP服务器使用说明.md",
                     STAGE / "MCP服务器使用说明.md")

    write_text(STAGE / "setup_ai_env.bat", SETUP_AI_ENV)

    modifications = STAGE / "MODIFICATIONS.txt"
    r1_text = modifications.read_text(encoding="utf-8", errors="replace") if modifications.is_file() else ""
    write_text(modifications, r1_text.rstrip() + "\n\n" + MODIFICATIONS_R2)

    print("5) zip ->", ZIP_BASE)
    zip_path = Path(str(ZIP_BASE) + ".zip")
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(ZIP_BASE), "zip", root_dir=str(DIST),
                        base_dir=STAGE.name)
    digest = sha256(zip_path)
    write_text(DIST / "OpenVSP-3.51.3-zh-CN-r2-Portable.zip.sha256",
               digest + "  OpenVSP-3.51.3-zh-CN-r2-Portable.zip\n")
    size = zip_path.stat().st_size
    print(f"OK  {zip_path}  {size/1024/1024:.1f} MB  sha256={digest}")
    return 0


README_R2 = """OpenVSP 3.51.3-zh-CN R2（便携版）
================================

一、这是什么
  R2 在 R1 汉化版基础上新增：
    1. OpenVSP 主界面菜单栏“AI辅助 → AI 设计...”模块：
       - DeepSeek / GLM（智谱）/ Kimi（月之暗面）/ GPT（OpenAI）/ 自定义
       - 输入请求地址与 API Key，拉取模型列表（失败时回退内置清单，支持手填）
       - 上传 Word/PDF/Excel/TXT 等技术文档，AI 解析技术参数/设备选型并与需求合并
       - 上传 .vsp3 案例作为基线，AI 按需求修改/优化，GA 优先以案例为基线
       - API Key 按服务商分别 DPAPI 加密保存，切换自动载入并实际调用模型验证
       - AI 面板“MCP调用”按钮：一键查看/复制 Codex、Claude Code、Kimi、ZCode、DSH 安装教程，Agent 自动配置 MCP
       - 自然语言需求 → 自动驱动 OpenVSP 建模 → VSPAERO 计算 → 优化 → Word 设计报告
       - 遗传算法优化（按工况优化几何）与“导入OpenVSP”回导 OpenVSP 编辑
       - API Key 使用 Windows DPAPI 加密保存在本机，不写入任务配置
    2. 内置 AI 设计后端（ai/）与 Word 报告模板（ai/templates/）
    3. OpenVSP MCP 服务器（mcp/），可被 Claude Desktop / Cursor / DSH 等直接调用
    4. R2 文档（docs/）、源码补丁（patches/）

二、快速开始
  1. 运行 vsp.exe，菜单栏打开“AI辅助 → AI 设计...”
  2. 选择服务商，填写 API Key，点“获取模型列表”
  3. 输入设计需求，点“开始设计”
  4. 完成后点“打开设计报告”（报告位于 文档\\OpenVSP-R2\\designs\\<时间戳>\\）

三、AI 后端 Python 依赖
  首次使用可双击 setup_ai_env.bat 安装依赖（numpy / python-docx / matplotlib）。
  需要 Python 3.13.x（随包 OpenVSP Python API 为 cp313 构建）；
  也可临时设置环境变量 OPENVSP_AI_PYTHON 指向 Python 3.13 的 python.exe。
  本便携包已包含 OpenVSP Python API（python\openvsp，不含 vspaero.exe 副本）和内置 Python 3.13。

四、无 Key / 离线
  未填写 API Key 时选择“离线运行”，使用内置规则设计流程，仍可生成模型、VSPAERO 结果与报告。

五、MCP 服务器
  见 MCP服务器使用说明.md；自测：
    cd mcp
    python tests\\test_stdio.py

六、许可与声明
  本项目基于 NASA OpenVSP（NOSA 1.3）的汉化修改版本；修改与分发声明见 MODIFICATIONS.txt。
  本软件为社区修改版，不代表 NASA 或上游背书。仅供合法民用/科研用途。
"""

SETUP_AI_ENV = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo  OpenVSP AI R2 AI 后端环境检查
echo ============================================
set "PYTHON=%~dp0ai\python\python.exe"
if exist "%PYTHON%" goto verify
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON set "PYTHON=%%P"
if not defined PYTHON (
  echo [错误] 未找到 python.exe，也未找到内置运行时 ai\python\python.exe。
  echo        请安装 Python 3.13（64 位，勾选 Add python.exe to PATH），
  echo        或设置环境变量 OPENVSP_AI_PYTHON 指向真实 python.exe。
  pause
  exit /b 1
)
:verify
echo 使用解释器: "%PYTHON%"
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,13) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] 该解释器不是可运行的 Python 3.13，可能是 Microsoft Store 占位别名。
  echo        请安装 Python 3.13，或设置 OPENVSP_AI_PYTHON。
  pause
  exit /b 1
)
"%PYTHON%" -c "import numpy, docx, matplotlib, pypdf, openpyxl, six" >nul 2>nul
if errorlevel 1 (
  echo 正在安装 AI 依赖...
  "%PYTHON%" -m pip install -r "%~dp0ai\requirements.txt"
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络/代理。
    pause
    exit /b 1
  )
)
echo.
echo AI 后端环境可用。现在可以启动 vsp.exe 使用 AI 设计模块。
pause
"""

MODIFICATIONS_R2 = """OpenVSP-zh-CN-r2 modified distribution
Date: 2026-11-01

This Modification is derived, directly or indirectly, from Original Software
provided by NASA under the NASA Open Source Agreement 1.3. No government or
upstream endorsement is asserted.

R2 changes (relative to OpenVSP-3.51.3-zh-CN-r1):
  - Added the AI Design module to the OpenVSP GUI: top-level "AI" menu,
    FLTK AI design dialog (provider/base URL/API key/model list/prompt/log),
    WinHTTP model-list retrieval, DPAPI-protected API key storage, and a
    child-process runner for the bundled Python design backend.
  - Added the bundled Python AI design backend (ai/): OpenAI-compatible
    providers (DeepSeek/GLM/Kimi/OpenAI/custom), tool-calling agent,
    OpenVSP/VSPAERO bridge, requirement-to-geometry designer, offline
    rule-based fallback, and Word report generation from the built-in
    template.
  - Added the OpenVSP MCP server (mcp/): stdio JSON-RPC 2.0, legacy
    (2025-11-25) initialize handshake, tools for model building, VSPAERO
    runs, geometry export and report generation.
  - Added R2 documentation, source changes snapshot and portable packaging.
  - Preserved R1 Simplified-Chinese localization, geometry/solver kernels and
    file-format compatibility.

The corresponding modified source is supplied in patches/source-changes/ and
patches/r2-ui.patch. Preserve this notice and the LICENSE when sharing this
distribution.
"""


if __name__ == "__main__":
    sys.exit(main())
