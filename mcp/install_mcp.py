#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenVSP MCP 一键安装器（仅标准库 / stdlib only）。

为 Codex CLI、Claude Code、Kimi Code CLI、ZCode、DeepSeek Harness (DSH)
探测本机 OpenVSP / Python / MCP server 路径，生成或安全合并各 Agent 的
MCP 配置文件（JSON / TOML / Cordis overlay YAML）。

特点
----
* 自动探测：server = 本文件同目录 ``openvsp_mcp_server.py``；
  python = ``--python`` > ``OPENVSP_PYTHON`` > ``sys.executable``；
  openvsp_root = ``--openvsp-root`` > ``OPENVSP_ROOT`` > 向上/邻近查找
  ``vspaero.exe`` > 常见默认安装目录。
* 安全合并：JSON 解析失败时**不写入**（保留原文件）；TOML 写前用
  ``tomllib`` 校验且保留其他配置；DSH YAML 仅在能确认是列表 patch 时追加。
* 幂等：重复安装同一 target 返回 ``unchanged``，不产生重复条目、不重复备份。
* 备份：原配置在修改前置为 ``<文件名>.bak``（仅在实际修改时）。
* 不写任何 API Key：env 只写 ``OPENVSP_ROOT`` / ``OPENVSP_PYTHONPATH``。
* ``--dry-run`` 默认模式：只打印将要做的事情，不落盘。

用法
----
    python install_mcp.py --print-command [--target all] [--json]
    python install_mcp.py --dry-run       [--target all] [--json]
    python install_mcp.py --install       [--target all] [--json] [--no-cli]
    python install_mcp.py --verify        [--json]

退出码：0 = 成功/无需变更；1 = 探测失败（找不到 server/openvsp）；2 = 部分 target 写入失败。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

INSTALLER_VERSION = "0.1.0"
SERVER_NAME = "openvsp"
SERVER_TITLE = "OpenVSP MCP"
DEFAULT_SERVER_FILE = "openvsp_mcp_server.py"
DEFAULT_WORKDIR_NAME = "workspace"

# 写进每个 Agent 配置的 env（故意只包含路径，不含任何密钥）
ENV_KEYS = ("OPENVSP_ROOT", "OPENVSP_PYTHONPATH")

TARGETS = ("codex", "claude", "zcode", "kimi", "dsh")

# 常见 OpenVSP 安装位置（探测兜底）
KNOWN_OPENVSP_ROOTS = (
    os.environ.get("OPENVSP_ROOT", ""),
    r"C:\OpenVSP-3.51.3-win64",
    r"C:\Program Files\OpenVSP",
    r"C:\Program Files\OpenVSP-3.51.3-win64",
    r"C:\OpenVSP",
)

# 文档依据（教程与 --print-command 输出会引用）
DOCS = {
    "codex": "https://github.com/openai/codex/blob/main/docs/config.md",
    "claude": "https://code.claude.com/docs/en/mcp",
    "kimi": "https://moonshotai.github.io/kimi-cli/en/customization/mcp.html",
    "zcode": "https://zcode.z.ai/cn/docs/mcp-services",
    "dsh": "https://deepseek-harness.github.io/deepseek-harness/guide/mcp-memory",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, flush=True)


def _err(message: str) -> None:
    print("[error] " + message, file=sys.stderr, flush=True)


def _warn(message: str) -> None:
    print("[warn] " + message, file=sys.stderr, flush=True)


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def _dump_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def _os_pathsep_join(paths) -> str:
    return os.pathsep.join(str(p) for p in paths)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="strict")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(path))


def _write_if_changed(path: Path, text: str) -> bool:
    """内容不同才写入（保证幂等 / mtime 稳定）。返回是否写入。"""
    try:
        if path.is_file() and path.read_text(encoding="utf-8-sig") == text:
            return False
    except OSError:
        pass
    _write_text_atomic(path, text)
    return True


def _backup_file(path: Path):
    """把现有配置复制为 ``<file>.bak``（只在文件存在时）。"""
    if not path.is_file():
        return None
    bak = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(str(path), str(bak))
    except OSError as exc:
        _warn("备份 %s 失败: %s" % (path, exc))
        return None
    return str(bak)


# ---------------------------------------------------------------------------
# 路径探测
# ---------------------------------------------------------------------------


def resolve_home(args) -> Path:
    if getattr(args, "home", None):
        return Path(args.home).expanduser().resolve()
    for env_name in ("HOME", "USERPROFILE"):
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser().resolve()
    return Path.home()


def detect_server(args) -> Path:
    if getattr(args, "server", None):
        return Path(args.server).expanduser().resolve()
    return Path(__file__).resolve().parent / DEFAULT_SERVER_FILE


def _is_openvsp_root(path: Path) -> bool:
    try:
        return (path / "vspaero.exe").is_file() or (
            path / "vsp.exe").is_file() and (path / "python").is_dir()
    except OSError:
        return False


def detect_openvsp_root(explicit, server: Path):
    """探测 OpenVSP 安装根目录，返回 (Path|None, notes:list[str])。"""
    notes = []
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if _is_openvsp_root(candidate):
            return candidate, notes
        notes.append("--openvsp-root 指向的目录缺少 vspaero.exe/vsp.exe: %s" % candidate)
        return None, notes

    env_root = os.environ.get("OPENVSP_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser()
        if _is_openvsp_root(candidate):
            return candidate.resolve(), notes
        notes.append("环境变量 OPENVSP_ROOT 不可用（缺少 vspaero.exe）: %s" % env_root)

    # 1) 从 server 目录向上查找
    for parent in server.parents:
        if _is_openvsp_root(parent):
            return parent, notes

    # 2) 向上各级目录的兄弟目录（OpenVSP 与项目并排放置的常见情形）
    for parent in server.parents:
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and _is_openvsp_root(child):
                return child, notes

    # 3) 已知默认位置
    for candidate_text in KNOWN_OPENVSP_ROOTS:
        candidate = Path(candidate_text)
        if _is_openvsp_root(candidate):
            return candidate, notes

    # 4) 常见盘符顶层 OpenVSP* 目录（有限深度，避免全盘扫描）
    for drive in ("C:\\", "D:\\", "E:\\"):
        root_dir = Path(drive)
        if not root_dir.is_dir():
            continue
        try:
            for child in root_dir.glob("OpenVSP*"):
                if child.is_dir() and _is_openvsp_root(child):
                    return child, notes
        except OSError:
            continue
    return None, notes


def _probe_python(python: str, openvsp_root, server: Path, timeout: float = 45.0):
    """探测指定 Python 是否可 import openvsp（返回 dict，不抛异常）。"""
    info = {"executable": python, "exists": False, "version": None,
            "import_openvsp": False, "error": None}
    executable = Path(python)
    info["exists"] = executable.is_file() or shutil.which(python) is not None
    if not info["exists"]:
        info["error"] = "解释器不存在"
        return info
    script = (
        "import json, os, sys\n"
        "try:\n"
        "    import platform\n"
        "    root = %r\n"
        "    paths = [os.path.join(root, 'python', 'openvsp'),\n"
        "             os.path.join(root, 'python', 'utilities'),\n"
        "             os.path.join(root, 'python', 'degen_geom'),\n"
        "             os.path.join(root, 'python', 'openvsp_config'),\n"
        "             os.path.join(root, 'python', 'openvsp', 'openvsp')] if root else []\n"
        "    for p in paths:\n"
        "        if os.path.isdir(p) and p not in sys.path:\n"
        "            sys.path.append(p)\n"
        "    if root and hasattr(os, 'add_dll_directory') and os.path.isdir(root):\n"
        "        os.add_dll_directory(root)\n"
        "    import openvsp\n"
        "    print(json.dumps({'ok': True, 'version': openvsp.GetVSPVersion(),\n"
        "                      'python': platform.python_version()}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}))\n"
    ) % (str(openvsp_root) if openvsp_root else "",)
    try:
        proc = subprocess.run([python, "-c", script], capture_output=True,
                              timeout=timeout, text=True, encoding="utf-8",
                              errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
        return info
    payload = None
    for line in (proc.stdout or "").splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if payload is None:
        info["error"] = "探测脚本无输出/输出无法解析: %s" % (proc.stderr or "")[-400:]
        return info
    info["import_openvsp"] = bool(payload.get("ok"))
    info["version"] = payload.get("version")
    info["python_version"] = payload.get("python")
    if not payload.get("ok"):
        info["error"] = payload.get("error")
    return info


def detect_python(args, server: Path, openvsp_root, probe: bool = True):
    """返回 (python:str, info:dict, notes:list[str])。"""
    notes = []
    explicit = getattr(args, "python", None)
    python = explicit or os.environ.get("OPENVSP_PYTHON") or sys.executable
    python = str(Path(python).expanduser()) if explicit or os.environ.get("OPENVSP_PYTHON") \
        else python
    info = {"executable": python, "source": "explicit" if explicit else
            ("OPENVSP_PYTHON" if os.environ.get("OPENVSP_PYTHON") else "sys.executable")}
    if probe:
        probed = _probe_python(python, openvsp_root, server)
        info.update(probed)
        if not probed.get("import_openvsp"):
            notes.append(
                "该 Python 目前无法 import openvsp（%s）。请确认已安装 numpy 且 "
                "OPENVSP_ROOT 指向 OpenVSP 3.51.3；也可用 --python 指定能 import "
                "openvsp 的解释器。" % probed.get("error"))
    else:
        info.update({"exists": Path(python).is_file(), "import_openvsp": None})
    return python, info, notes


def python_api_paths(root: Path):
    if root is None:
        return []
    return [
        root / "python" / "openvsp",
        root / "python" / "utilities",
        root / "python" / "degen_geom",
        root / "python" / "openvsp_config",
        root / "python" / "openvsp" / "openvsp",
    ]


def build_server_env(openvsp_root: Path) -> dict:
    """只包含路径类环境变量，绝不写入密钥。"""
    env = {}
    if openvsp_root:
        env["OPENVSP_ROOT"] = str(openvsp_root)
        existing = [p for p in python_api_paths(openvsp_root) if p.is_dir()]
        if existing:
            env["OPENVSP_PYTHONPATH"] = _os_pathsep_join(existing)
    return env


def detect_workspace(args, server: Path) -> Path:
    if getattr(args, "workspace", None):
        return Path(args.workspace).expanduser().resolve()
    return server.parent / DEFAULT_WORKDIR_NAME


def collect_paths(args, probe: bool = True):
    """统一探测入口，返回 dict（包含 detected 与 notes）。"""
    notes = []
    server = detect_server(args)
    if not server.is_file():
        return {"ok": False, "error": "找不到 MCP server 文件: %s" % server,
                "detected": {"server": str(server)}}
    openvsp_root, root_notes = detect_openvsp_root(
        getattr(args, "openvsp_root", None), server)
    notes.extend(root_notes)
    if openvsp_root is None:
        notes.append("未能自动探测 OpenVSP 安装目录；安装配置仍会生成，"
                     "但请用 --openvsp-root 指定（或设置 OPENVSP_ROOT）。")
    python, python_info, python_notes = detect_python(
        args, server, openvsp_root, probe=probe)
    notes.extend(python_notes)
    home = resolve_home(args)
    workspace = detect_workspace(args, server)
    env = build_server_env(openvsp_root)
    detected = {
        "server": str(server),
        "server_exists": True,
        "python": python,
        "python_info": python_info,
        "openvsp_root": str(openvsp_root) if openvsp_root else None,
        "openvsp_root_detected": openvsp_root is not None,
        "workspace": str(workspace),
        "home": str(home),
        "env": env,
    }
    return {"ok": True, "detected": detected, "notes": notes,
            "home": home, "workspace": workspace, "server": server,
            "python": python, "openvsp_root": openvsp_root, "env": env}


# ---------------------------------------------------------------------------
# 各 Agent 配置路径 / 内容渲染
# ---------------------------------------------------------------------------


def codex_config_path(home: Path) -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else home / ".codex"
    return base / "config.toml"


def claude_config_path(home: Path) -> Path:
    # Claude Code 用户级 MCP 配置：~/.claude.json 顶层 mcpServers
    return home / ".claude.json"


def kimi_config_path(home: Path) -> Path:
    share = os.environ.get("KIMI_SHARE_DIR")
    base = Path(share).expanduser() if share else home / ".kimi"
    return base / "mcp.json"


def zcode_config_path(home: Path) -> Path:
    return home / ".zcode" / "cli" / "config.json"


def dsh_home(home: Path) -> Path:
    value = os.environ.get("DSH_HOME")
    return Path(value).expanduser() if value else home / ".dsh"


def dsh_config_path(home: Path) -> Path:
    return dsh_home(home) / "cordis.patch.yml"


def dsh_standalone_path(server: Path, home: Path = None) -> Path:
    """独立 Cordis patch 文件（不写在安装目录，避免污染只读资源目录）。"""
    if home is not None:
        base = dsh_home(home)
        if base.is_dir():
            return base / "openvsp-mcp.cordis.yml"
        return home / "openvsp-mcp.cordis.yml"
    return server.parent / "openvsp-mcp.cordis.yml"


def build_json_entry(python: str, server: Path, env: dict) -> dict:
    return {"command": python, "args": [str(server)], "env": dict(env)}


def build_claude_entry(python: str, server: Path, env: dict) -> dict:
    entry = build_json_entry(python, server, env)
    entry["type"] = "stdio"
    return entry


def toml_quote(value: str) -> str:
    # JSON 字符串与 TOML basic string 的转义规则兼容（反斜杠/引号/控制字符）
    return json.dumps(str(value), ensure_ascii=False)


def render_codex_block(python: str, server: Path, env: dict):
    lines = [
        "[mcp_servers.%s]" % SERVER_NAME,
        "command = %s" % toml_quote(python),
        "args = [%s]" % toml_quote(str(server)),
    ]
    if env:
        pairs = ", ".join("%s = %s" % (key, toml_quote(value))
                          for key, value in env.items())
        lines.append("env = { %s }" % pairs)
    return lines


def yaml_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def render_dsh_block(python: str, server: Path, env: dict, workspace: Path):
    lines = [
        "- insert:",
        "    - id: openvsp",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        "        serverName: openvsp",
        "        transport: stdio",
        "        command: %s" % yaml_quote(python),
        "        args:",
        "          - %s" % yaml_quote(str(server)),
    ]
    if env:
        lines.append("        env:")
        for key, value in env.items():
            lines.append("          %s: %s" % (key, yaml_quote(value)))
    else:
        lines.append("        env: {}")
    lines.append("        cwd: %s" % yaml_quote(str(workspace)))
    return lines


def render_standard_json(python: str, server: Path, env: dict) -> str:
    payload = {"mcpServers": {SERVER_NAME: build_json_entry(python, server, env)}}
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 安全合并原语
# ---------------------------------------------------------------------------


def merge_json_config(path: Path, mutate, dry_run: bool, target: str):
    """读取/合并/写回 JSON 配置；解析失败时不修改原文件。"""
    result = {"target": target, "path": str(path), "action": "unchanged"}
    original_exists = path.is_file()
    existing_text = None
    if original_exists:
        try:
            existing_text = _read_text(path)
            data = json.loads(existing_text) if existing_text.strip() else {}
        except (OSError, ValueError) as exc:
            result.update({"action": "error", "ok": False,
                           "error": "现有 JSON 无法解析，已保持原文件不变: %s" % exc})
            return result
        if not isinstance(data, dict):
            result.update({"action": "error", "ok": False,
                           "error": "现有 JSON 顶层不是对象，已保持原文件不变"})
            return result
    else:
        data = {}
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    mutate(data)
    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if before == after and original_exists:
        result["ok"] = True
        return result
    action = "updated" if original_exists else "created"
    if dry_run:
        result.update({"action": "would-" + action, "ok": True})
        return result
    backup = _backup_file(path) if original_exists else None
    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        _write_text_atomic(path, new_text)
    except OSError as exc:
        result.update({"action": "error", "ok": False, "error": "写入失败: %s" % exc})
        return result
    result.update({"action": action, "ok": True, "backup": backup,
                   "size_bytes": path.stat().st_size})
    return result


def _split_toml_section(text: str, header: str):
    """返回 (start_line, end_line, lines)；找不到返回 None。"""
    lines = text.splitlines()
    target = header.strip()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == target:
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            end = idx
            break
        if stripped.startswith("[["):
            end = idx
            break
    return start, end, lines


def merge_toml_config(path: Path, header: str, block_lines, dry_run: bool, target: str):
    """安全合并 TOML 配置：保留其他内容，写前用 tomllib 校验。"""
    result = {"target": target, "path": str(path), "action": "unchanged"}
    original_exists = path.is_file()
    text = ""
    if original_exists:
        try:
            text = _read_text(path)
        except OSError as exc:
            result.update({"action": "error", "ok": False, "error": "读取失败: %s" % exc})
            return result
        if text.strip():
            try:
                tomllib.loads(text)
            except ValueError as exc:
                result.update({"action": "error", "ok": False,
                               "error": "现有 TOML 无法解析，已保持原文件不变: %s" % exc})
                return result
    block = "\n".join(block_lines).rstrip("\n") + "\n"
    existing = _split_toml_section(text, header) if text else None
    if existing is not None:
        start, end, lines = existing
        old_block = "\n".join(lines[start:end]).rstrip("\n")
        if old_block == block.rstrip("\n"):
            result["ok"] = True
            return result
        new_lines = lines[:start] + block.rstrip("\n").splitlines() + lines[end:]
        new_text = "\n".join(new_lines) + "\n"
        action = "updated"
    else:
        base = text.rstrip("\n")
        new_text = (base + "\n\n" if base else "") + block
        action = "updated" if original_exists else "created"
    # 写前校验：新内容必须是合法 TOML，且包含目标 section
    try:
        parsed = tomllib.loads(new_text)
    except ValueError as exc:
        result.update({"action": "error", "ok": False,
                       "error": "生成的 TOML 校验失败，已放弃写入: %s" % exc})
        return result
    servers = parsed.get("mcp_servers") or {}
    if SERVER_NAME not in servers:
        result.update({"action": "error", "ok": False,
                       "error": "生成的 TOML 中未找到 [mcp_servers.%s]，已放弃写入" % SERVER_NAME})
        return result
    if dry_run:
        result.update({"action": "would-" + action, "ok": True})
        return result
    backup = _backup_file(path) if original_exists else None
    try:
        _write_text_atomic(path, new_text)
    except OSError as exc:
        result.update({"action": "error", "ok": False, "error": "写入失败: %s" % exc})
        return result
    result.update({"action": action, "ok": True, "backup": backup,
                   "size_bytes": path.stat().st_size})
    return result


def _yaml_looks_like_patch_list(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        return stripped.startswith("-")
    return True


def merge_yaml_patch(path: Path, block_lines, dry_run: bool, target: str,
                     standalone_path: Path = None):
    """向 Cordis patch YAML 追加 ``- insert:`` 项（无法确认结构时只生成独立文件）。"""
    result = {"target": target, "path": str(path), "action": "unchanged"}
    block = "\n".join(block_lines).rstrip("\n")
    marker = "serverName: openvsp"
    original_exists = path.is_file()
    text = ""
    if original_exists:
        try:
            text = _read_text(path)
        except OSError as exc:
            result.update({"action": "error", "ok": False, "error": "读取失败: %s" % exc})
            return result
    standalone = None
    if standalone_path is not None:
        if not dry_run:
            try:
                _write_if_changed(standalone_path, block + "\n")
                standalone = str(standalone_path)
            except OSError as exc:
                _warn("写入独立 DSH patch 文件失败: %s" % exc)
        else:
            standalone = "would-write:" + str(standalone_path)
    if marker in text:
        result.update({"ok": True, "note": "已存在 openvsp 条目，跳过",
                       "standalone_patch": standalone})
        return result
    if text.strip() and not _yaml_looks_like_patch_list(text):
        if standalone_path is not None:
            result.update({"ok": True, "action": "standalone-only",
                           "note": "现有文件不是 Cordis patch 列表，未修改；"
                                   "已生成独立 patch 文件，请用 dsh web --patch 加载",
                           "standalone_patch": standalone})
        else:
            result.update({"ok": False, "action": "error",
                           "error": "现有文件不是 Cordis patch 列表，无法安全追加"})
        return result
    base = text.rstrip("\n")
    new_text = (base + "\n\n" if base else "") + block + "\n"
    if dry_run:
        result.update({"action": "would-" + ("update" if original_exists else "create"),
                       "ok": True, "standalone_patch": standalone})
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_file(path) if original_exists else None
    try:
        _write_text_atomic(path, new_text)
    except OSError as exc:
        result.update({"action": "error", "ok": False, "error": "写入失败: %s" % exc})
        return result
    result.update({"action": "updated" if original_exists else "created",
                   "ok": True, "backup": backup,
                   "size_bytes": path.stat().st_size, "standalone_patch": standalone})
    return result


# ---------------------------------------------------------------------------
# 各 target 的“命令/片段/安装”实现
# ---------------------------------------------------------------------------


def claude_cli_command(python: str, server: Path, env: dict) -> str:
    parts = ["claude", "mcp", "add", "--transport", "stdio", "--scope", "user"]
    for key, value in env.items():
        parts.extend(["--env", _env_cli_arg(key, value)])
    parts.extend([SERVER_NAME, "--"])
    parts.append(_shell_quote(python))
    parts.append(_shell_quote(str(server)))
    return " ".join(parts)


def kimi_cli_command(python: str, server: Path, env: dict) -> str:
    parts = ["kimi", "mcp", "add", "--transport", "stdio"]
    for key, value in env.items():
        parts.extend(["--env", _env_cli_arg(key, value)])
    parts.extend([SERVER_NAME, "--"])
    parts.append(_shell_quote(python))
    parts.append(_shell_quote(str(server)))
    return " ".join(parts)


def codex_cli_command(python: str, server: Path, env: dict) -> str:
    parts = ["codex", "mcp", "add", SERVER_NAME]
    for key, value in env.items():
        parts.extend(["--env", _env_cli_arg(key, value)])
    parts.append("--")
    parts.append(_shell_quote(python))
    parts.append(_shell_quote(str(server)))
    return " ".join(parts)


def _shell_quote(value: str) -> str:
    value = str(value)
    if not value or any(ch in value for ch in ' \t"&|<>^;'):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _env_cli_arg(key: str, value: str) -> str:
    """CLI 环境变量参数：值含 ``;`` / 空格等时加引号，避免被 shell 拆分。"""
    return "%s=%s" % (key, _shell_quote(value))


def _detect_cli(name: str) -> str:
    override = os.environ.get("OPENVSP_MCP_%s_BIN" % name.upper())
    if override == "":
        return ""
    return shutil.which(override or name) or ""


def _run_cli_list(command_list) -> dict:
    """运行 ``claude mcp list`` 之类的只读命令（不经过 shell）。"""
    try:
        proc = subprocess.run(command_list, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-800:], "stderr": (proc.stderr or "")[-800:]}


def _run_cli_shell(command: str) -> dict:
    """运行 ``claude mcp add ...`` 之类的带引号命令字符串。"""
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-800:], "stderr": (proc.stderr or "")[-800:]}


def install_codex(ctx, dry_run: bool):
    path = codex_config_path(ctx["home"])
    block = render_codex_block(ctx["python"], ctx["server"], ctx["env"])
    result = merge_toml_config(path, "[mcp_servers.%s]" % SERVER_NAME, block,
                               dry_run, "codex")
    result["format"] = "toml"
    result["scope"] = "user"
    result["cli_hint"] = codex_cli_command(ctx["python"], ctx["server"], ctx["env"])
    return result


def install_claude(ctx, dry_run: bool, no_cli: bool = False):
    cli = "" if no_cli else _detect_cli("claude")
    result = {"target": "claude", "format": "json", "scope": "user"}
    if cli and not dry_run:
        command = claude_cli_command(ctx["python"], ctx["server"], ctx["env"])
        # 先检查是否已存在同名 server，避免重复 add 报错（幂等）
        listed = _run_cli_list([cli, "mcp", "list"])
        already = SERVER_NAME in (listed.get("stdout") or "")
        if already:
            result.update({"path": claude_config_path(ctx["home"]), "action": "unchanged",
                           "ok": True, "note": "claude mcp list 已包含 openvsp",
                           "cli_command": command})
            return result
        proc = _run_cli_shell(command)
        if proc.get("ok"):
            result.update({"path": claude_config_path(ctx["home"]), "action": "cli-added",
                           "ok": True, "cli_command": command, "cli_output": proc})
            return result
        result["cli_error"] = proc
        result["note"] = "claude CLI 调用失败，回退为合并 ~/.claude.json"
    elif cli and dry_run:
        result.update({"path": claude_config_path(ctx["home"]),
                       "action": "would-run-cli", "ok": True,
                       "cli_command": claude_cli_command(ctx["python"], ctx["server"], ctx["env"])})
        return result
    else:
        result["cli_command"] = claude_cli_command(ctx["python"], ctx["server"], ctx["env"])
        result["note"] = "未找到 claude CLI，改为合并 ~/.claude.json 的 mcpServers"

    path = claude_config_path(ctx["home"])

    def mutate(data):
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        servers[SERVER_NAME] = build_claude_entry(ctx["python"], ctx["server"], ctx["env"])

    merged = merge_json_config(path, mutate, dry_run, "claude")
    merged.update({"format": "json", "scope": "user"})
    merged.setdefault("cli_command", claude_cli_command(ctx["python"], ctx["server"], ctx["env"]))
    if "note" in result:
        merged.setdefault("note", result["note"])
    return merged


def install_kimi(ctx, dry_run: bool):
    path = kimi_config_path(ctx["home"])
    entry = build_json_entry(ctx["python"], ctx["server"], ctx["env"])

    def mutate(data):
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        servers[SERVER_NAME] = entry

    result = merge_json_config(path, mutate, dry_run, "kimi")
    result.update({"format": "json", "scope": "user",
                   "cli_hint": kimi_cli_command(ctx["python"], ctx["server"], ctx["env"]),
                   "config_hint": "Kimi Code CLI: ~/.kimi/mcp.json（可用 KIMI_SHARE_DIR 改目录）"})
    return result


def install_zcode(ctx, dry_run: bool):
    path = zcode_config_path(ctx["home"])
    entry = build_json_entry(ctx["python"], ctx["server"], ctx["env"])

    def mutate(data):
        mcp = data.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            mcp = {}
            data["mcp"] = mcp
        servers = mcp.setdefault("servers", {})
        if not isinstance(servers, dict):
            servers = {}
            mcp["servers"] = servers
        servers[SERVER_NAME] = entry

    result = merge_json_config(path, mutate, dry_run, "zcode")
    result.update({"format": "json", "scope": "user",
                   "config_hint": "ZCode 用户级: ~/.zcode/cli/config.json（键 mcp.servers）；"
                                  "工作区级: <项目>/.zcode/config.json；"
                                  "也支持 ~/.agents/mcp.json 的 mcpServers（兜底）"})
    return result


def install_dsh(ctx, dry_run: bool):
    path = dsh_config_path(ctx["home"])
    standalone = dsh_standalone_path(ctx["server"], ctx["home"])
    block = render_dsh_block(ctx["python"], ctx["server"], ctx["env"], ctx["workspace"])
    home_dir = dsh_home(ctx["home"])
    if not home_dir.is_dir() and not os.environ.get("DSH_HOME"):
        # DSH 未安装：只生成独立 patch 文件，不盲目创建 ~/.dsh 目录
        standalone_result = merge_yaml_patch(path, block, dry_run, "dsh",
                                             standalone_path=standalone)
        standalone_result.update({
            "ok": True, "action": "standalone-only",
            "format": "yaml",
            "scope": "standalone patch",
            "note": "未检测到 DSH_HOME（%s 不存在），已生成独立 patch 文件；"
                    "安装 DSH 后可用 dsh web --patch 加载，或设置 DSH_HOME 后重跑 --install"
                    % home_dir,
            "patch_command": "dsh web --patch \"%s\"" % standalone,
        })
        return standalone_result
    result = merge_yaml_patch(path, block, dry_run, "dsh", standalone_path=standalone)
    result.update({"format": "yaml", "scope": "machine-wide (cordis.patch.yml)",
                   "config_hint": "DSH Cordis overlay: $DSH_HOME/cordis.patch.yml"
                                  "（或 $DSH_HOME/profiles/<name>/cordis.patch.yml）；"
                                  "也可用 dsh web --patch <独立 patch 文件>",
                   "patch_command": "dsh web --patch \"%s\"" % standalone})
    return result


def install_target(target: str, ctx, dry_run: bool, no_cli: bool):
    if target == "codex":
        return install_codex(ctx, dry_run)
    if target == "claude":
        return install_claude(ctx, dry_run, no_cli=no_cli)
    if target == "kimi":
        return install_kimi(ctx, dry_run)
    if target == "zcode":
        return install_zcode(ctx, dry_run)
    if target == "dsh":
        return install_dsh(ctx, dry_run)
    raise ValueError("unknown target: %s" % target)


# ---------------------------------------------------------------------------
# 输出：print-command / 安装结果 / 验证
# ---------------------------------------------------------------------------


def agent_install_command(ctx) -> str:
    """可直接交给 Agent 执行的完整安装命令。"""
    installer = Path(__file__).resolve()
    return '%s %s --install --target all --json' % (
        _shell_quote(ctx["python"]), _shell_quote(str(installer)))


def print_commands(ctx, targets):
    py, server = ctx["python"], ctx["server"]
    env = ctx["env"]
    lines = []
    lines.append("=" * 72)
    lines.append("OpenVSP MCP 一键安装器 v%s" % INSTALLER_VERSION)
    lines.append("=" * 72)
    lines.append("检测结果：")
    lines.append("  server        : %s" % server)
    lines.append("  python        : %s (%s)" % (py, ctx["detected"]["python_info"].get("source")))
    lines.append("  import openvsp: %s" % (
        "OK" if ctx["detected"]["python_info"].get("import_openvsp") else "FAILED"))
    lines.append("  openvsp_root  : %s" % (ctx["openvsp_root"] or "<未探测到，请用 --openvsp-root 指定>"))
    lines.append("  workspace     : %s" % ctx["workspace"])
    lines.append("  home          : %s" % ctx["home"])
    for note in ctx.get("notes") or []:
        lines.append("  [note] %s" % note)
    lines.append("")
    lines.append("把这条命令交给 Agent 自动安装（推荐，幂等、会备份原配置）：")
    lines.append("  %s" % agent_install_command(ctx))
    lines.append("")
    lines.append("仅预览不落盘：")
    lines.append('  %s %s --dry-run --target all' % (
        _shell_quote(py), _shell_quote(str(Path(__file__).resolve()))))
    lines.append("")
    lines.append("标准 JSON 配置（任何支持 mcpServers 的客户端可直接用）：")
    lines.append(render_standard_json(py, server, env))
    lines.append("")

    for target in targets:
        lines.append("-" * 72)
        if target == "codex":
            lines.append("[Codex CLI] 配置文件: %s" % codex_config_path(ctx["home"]))
            lines.append("TOML [mcp_servers.%s] 内容：" % SERVER_NAME)
            lines.extend(render_codex_block(py, server, env))
            lines.append("CLI（若当前 Codex 版本支持 codex mcp 子命令）：")
            lines.append("  " + codex_cli_command(py, server, env))
            lines.append("依据: %s" % DOCS["codex"])
        elif target == "claude":
            lines.append("[Claude Code] 优先使用 CLI（用户级）：")
            lines.append("  " + claude_cli_command(py, server, env))
            lines.append("无 claude CLI 时写: %s 的顶层 mcpServers" %
                         claude_config_path(ctx["home"]))
            lines.append("条目：")
            lines.append(json.dumps({SERVER_NAME: build_claude_entry(py, server, env)},
                                    ensure_ascii=False, indent=2))
            lines.append("项目级替代方案: <项目>/.mcp.json（同上 mcpServers 结构）")
            lines.append("依据: %s" % DOCS["claude"])
        elif target == "kimi":
            lines.append("[Kimi Code CLI] 配置文件: %s" % kimi_config_path(ctx["home"]))
            lines.append("CLI：")
            lines.append("  " + kimi_cli_command(py, server, env))
            lines.append("JSON 条目：")
            lines.append(json.dumps({"mcpServers": {SERVER_NAME: build_json_entry(py, server, env)}},
                                    ensure_ascii=False, indent=2))
            lines.append("依据: %s" % DOCS["kimi"])
        elif target == "zcode":
            lines.append("[ZCode] 用户级配置文件: %s（键 mcp.servers）" %
                         zcode_config_path(ctx["home"]))
            lines.append("JSON：")
            lines.append(json.dumps({"mcp": {"servers": {
                SERVER_NAME: build_json_entry(py, server, env)}}},
                ensure_ascii=False, indent=2))
            lines.append("也可在 ZCode 设置 → MCP 服务器 → 完整配置 粘贴标准 JSON；"
                         "或使用工作区级 <项目>/.zcode/config.json")
            lines.append("依据: %s" % DOCS["zcode"])
        elif target == "dsh":
            lines.append("[DSH] Cordis overlay 配置文件: %s" % dsh_config_path(ctx["home"]))
            lines.append("YAML 追加内容：")
            lines.extend(render_dsh_block(py, server, env, ctx["workspace"]))
            lines.append("独立 patch 文件（不改 DSH 配置时）: %s" %
                         dsh_standalone_path(server, ctx["home"]))
            lines.append("加载: dsh web --patch \"%s\"" % dsh_standalone_path(server, ctx["home"]))
            lines.append("依据: %s" % DOCS["dsh"])
        lines.append("")
    lines.append("=" * 72)
    lines.append("安装后：重启/重载对应 Agent，新会话中让它调用 openvsp_status 验证。")
    lines.append("验证命令：%s %s --verify" % (_shell_quote(py),
                                            _shell_quote(str(Path(__file__).resolve()))))
    return "\n".join(lines)


def install_all(ctx, targets, dry_run: bool, no_cli: bool):
    results = {}
    errors = 0
    for target in targets:
        try:
            result = install_target(target, ctx, dry_run, no_cli)
        except Exception as exc:  # noqa: BLE001 - 单个 target 失败不影响其他
            result = {"target": target, "ok": False, "action": "error",
                      "error": "%s: %s" % (type(exc).__name__, exc)}
        if not result.get("ok", False):
            errors += 1
        results[target] = result
    return results, errors


def verify_openvsp(ctx) -> dict:
    py, server, root = ctx["python"], ctx["server"], ctx["openvsp_root"]
    outcome = {"ok": False, "server": str(server)}
    env = os.environ.copy()
    if root:
        env["OPENVSP_ROOT"] = str(root)
    env["OPENVSP_MCP_LOG_LEVEL"] = "ERROR"
    try:
        proc = subprocess.run([py, str(server), "--status"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=180, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        outcome["error"] = "%s: %s" % (type(exc).__name__, exc)
        return outcome
    payload = None
    stdout = (proc.stdout or "").strip()
    if stdout.startswith("{"):
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
    if payload is None:
        for line in stdout.splitlines():
            if line.strip().startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
    if payload is None:
        outcome["error"] = "server --status 无有效 JSON 输出"
        outcome["stderr"] = (proc.stderr or "")[-800:]
        return outcome
    openvsp = payload.get("openvsp") or {}
    outcome.update({
        "ok": bool(openvsp.get("available")),
        "openvsp_available": bool(openvsp.get("available")),
        "openvsp_version": openvsp.get("version"),
        "vspaero_available": bool(openvsp.get("vspaero_available")),
        "python": (payload.get("python") or {}).get("version"),
        "protocol": (payload.get("protocol") or {}).get("protocolVersion"),
    })
    return outcome


def verify_configs(ctx, targets):
    checks = {}
    for target in targets:
        if target == "codex":
            path = codex_config_path(ctx["home"])
            text = _read_text(path) if path.is_file() else ""
            ok = ("[mcp_servers.%s]" % SERVER_NAME) in text
            checks[target] = {"path": str(path), "installed": ok}
        elif target == "claude":
            path = claude_config_path(ctx["home"])
            ok = False
            if path.is_file():
                try:
                    ok = SERVER_NAME in (json.loads(_read_text(path)).get("mcpServers") or {})
                except ValueError:
                    ok = False
            cli = _detect_cli("claude")
            if not ok and cli:
                listed = _run_cli_list([cli, "mcp", "list"])
                ok = SERVER_NAME in (listed.get("stdout") or "")
            checks[target] = {"path": str(path), "installed": ok,
                              "note": "claude CLI" if cli else "~/.claude.json"}
        elif target == "kimi":
            path = kimi_config_path(ctx["home"])
            ok = False
            if path.is_file():
                try:
                    ok = SERVER_NAME in (json.loads(_read_text(path)).get("mcpServers") or {})
                except ValueError:
                    ok = False
            checks[target] = {"path": str(path), "installed": ok}
        elif target == "zcode":
            path = zcode_config_path(ctx["home"])
            ok = False
            if path.is_file():
                try:
                    data = json.loads(_read_text(path))
                    ok = SERVER_NAME in ((data.get("mcp") or {}).get("servers") or {})
                except ValueError:
                    ok = False
            checks[target] = {"path": str(path), "installed": ok}
        elif target == "dsh":
            path = dsh_config_path(ctx["home"])
            ok = path.is_file() and ("serverName: %s" % SERVER_NAME) in _read_text(path)
            checks[target] = {"path": str(path), "installed": ok,
                              "standalone": str(dsh_standalone_path(ctx["server"], ctx["home"]))}
    return checks


def print_install_results(results, targets, dry_run: bool):
    mode = "DRY-RUN（未写入任何文件）" if dry_run else "INSTALL"
    print("=" * 72)
    print("OpenVSP MCP 安装结果 [%s]" % mode)
    print("=" * 72)
    for target in targets:
        result = results.get(target) or {}
        ok = result.get("ok", False)
        action = result.get("action", "?")
        marker = "OK " if ok else "ERR"
        print("[%s] %-7s %-18s %s" % (marker, target, action, result.get("path", "")))
        if result.get("backup"):
            print("       备份: %s" % result["backup"])
        if result.get("standalone_patch"):
            print("       独立 patch: %s" % result["standalone_patch"])
        if result.get("note"):
            print("       说明: %s" % result["note"])
        if not ok and result.get("error"):
            print("       错误: %s" % result["error"])
    print("=" * 72)
    if dry_run:
        print("预览模式未写入文件；加 --install 实际写入。")
    else:
        print("安装完成：重启/重载对应 Agent，新会话中让它调用 openvsp_status 验证。")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="openvsp-mcp-install",
        description="OpenVSP MCP 一键安装器（Codex / Claude Code / Kimi / ZCode / DSH）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--print-command", action="store_true",
                      help="打印各 Agent 安装命令/配置片段（默认模式）")
    mode.add_argument("--install", action="store_true",
                      help="实际写入/合并各 Agent 配置（会备份原文件）")
    mode.add_argument("--dry-run", action="store_true",
                      help="预览将写入的内容，不落盘（默认模式）")
    mode.add_argument("--verify", action="store_true",
                      help="验证 server 可运行 + 各 Agent 配置是否包含 openvsp")
    parser.add_argument("--target", action="append", default=None,
                        choices=list(TARGETS) + ["all"],
                        metavar="TARGET",
                        help="目标 Agent（可重复指定），默认 all；可选 " + "|".join(TARGETS) + "|all")
    parser.add_argument("--python", default=None, help="指定 Python 解释器")
    parser.add_argument("--openvsp-root", default=None, help="指定 OpenVSP 安装根目录")
    parser.add_argument("--server", default=None, help="指定 openvsp_mcp_server.py 路径")
    parser.add_argument("--workspace", default=None, help="指定 MCP 输出工作目录")
    parser.add_argument("--home", default=None, help="覆盖用户 home（测试/便携环境用）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--no-cli", action="store_true",
                        help="Claude Code 即使检测到 claude CLI 也走配置文件合并")
    parser.add_argument("--no-probe", action="store_true",
                        help="跳过 import openvsp 探测（更快）")
    parser.add_argument("--version", action="store_true", help="打印版本")
    return parser


def _targets_from_args(target) -> list:
    """把 --target（可重复）归一化为按 TARGETS 顺序、去重的列表。"""
    if not target:
        return list(TARGETS)
    selected = set(target)
    if "all" in selected:
        return list(TARGETS)
    return [name for name in TARGETS if name in selected]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print("openvsp-mcp-install %s" % INSTALLER_VERSION)
        return 0
    targets = _targets_from_args(args.target)
    ctx = collect_paths(args, probe=not args.no_probe)
    if not ctx.get("ok"):
        if args.json:
            print(_dump_json({"ok": False, "error": ctx.get("error"),
                              "detected": ctx.get("detected")}))
        else:
            _err(ctx.get("error") or "探测失败")
        return 1

    mode = "dry-run"  # 默认安全模式：只预览、不落盘
    if args.print_command:
        mode = "print-command"
    elif args.install:
        mode = "install"
    elif args.verify:
        mode = "verify"

    if mode == "print-command":
        text = print_commands(ctx, targets)
        if args.json:
            payload = {
                "ok": True,
                "mode": mode,
                "installer_version": INSTALLER_VERSION,
                "detected": ctx["detected"],
                "notes": ctx.get("notes") or [],
                "install_command": agent_install_command(ctx),
                "standard_json": json.loads(render_standard_json(
                    ctx["python"], ctx["server"], ctx["env"])),
                "targets": {
                    "codex": {"path": str(codex_config_path(ctx["home"])),
                              "toml": "\n".join(render_codex_block(
                                  ctx["python"], ctx["server"], ctx["env"])),
                              "cli": codex_cli_command(ctx["python"], ctx["server"], ctx["env"]),
                              "docs": DOCS["codex"]},
                    "claude": {"path": str(claude_config_path(ctx["home"])),
                               "cli": claude_cli_command(ctx["python"], ctx["server"], ctx["env"]),
                               "json_entry": build_claude_entry(
                                   ctx["python"], ctx["server"], ctx["env"]),
                               "docs": DOCS["claude"]},
                    "kimi": {"path": str(kimi_config_path(ctx["home"])),
                             "cli": kimi_cli_command(ctx["python"], ctx["server"], ctx["env"]),
                             "json_entry": build_json_entry(
                                 ctx["python"], ctx["server"], ctx["env"]),
                             "docs": DOCS["kimi"]},
                    "zcode": {"path": str(zcode_config_path(ctx["home"])),
                              "native_json": {"mcp": {"servers": {
                                  SERVER_NAME: build_json_entry(
                                      ctx["python"], ctx["server"], ctx["env"])}}},
                              "docs": DOCS["zcode"]},
                    "dsh": {"path": str(dsh_config_path(ctx["home"])),
                            "yaml": "\n".join(render_dsh_block(
                                ctx["python"], ctx["server"], ctx["env"],
                                ctx["workspace"])),
                            "standalone": str(dsh_standalone_path(ctx["server"], ctx["home"])),
                            "docs": DOCS["dsh"]},
                },
            }
            print(_dump_json(payload))
        else:
            print(text)
        return 0

    if mode == "verify":
        openvsp = verify_openvsp(ctx)
        checks = verify_configs(ctx, targets)
        payload = {
            "ok": bool(openvsp.get("ok")) and all(
                item.get("installed") for item in checks.values()),
            "mode": "verify",
            "detected": ctx["detected"],
            "openvsp": openvsp,
            "targets": checks,
        }
        if args.json:
            print(_dump_json(payload))
        else:
            print("=" * 72)
            print("OpenVSP MCP 安装验证")
            print("=" * 72)
            print("server          : %s" % ctx["server"])
            print("python          : %s" % ctx["python"])
            print("openvsp_root    : %s" % (ctx["openvsp_root"] or "<未探测到>"))
            print("openvsp 可用    : %s" % openvsp.get("ok"))
            print("openvsp 版本    : %s" % openvsp.get("openvsp_version"))
            print("VSPAERO 可用    : %s" % openvsp.get("vspaero_available"))
            if openvsp.get("error"):
                print("错误            : %s" % openvsp["error"])
            print("-" * 72)
            for target in targets:
                item = checks.get(target) or {}
                print("[%s] %-7s %s" % ("OK " if item.get("installed") else "MISS",
                                        target, item.get("path", "")))
            print("=" * 72)
            print("提示：重启/重载 Agent 后，让新会话调用 openvsp_status 做端到端验证。")
        return 0 if payload["ok"] else 1

    dry_run = mode != "install"
    results, errors = install_all(ctx, targets, dry_run, no_cli=args.no_cli)
    if args.json:
        payload = {
            "ok": errors == 0,
            "mode": "dry-run" if dry_run else "install",
            "installer_version": INSTALLER_VERSION,
            "detected": ctx["detected"],
            "notes": ctx.get("notes") or [],
            "results": results,
        }
        print(_dump_json(payload))
    else:
        print_install_results(results, targets, dry_run)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
