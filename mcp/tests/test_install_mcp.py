#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_mcp.py 的单元/集成测试（pytest）。

覆盖：
* 自动探测 + --print-command（JSON 机器可读输出）
* --dry-run 不落盘
* --install 在临时 HOME 下安全合并 5 类 Agent 配置（JSON / TOML / Cordis YAML）
* 保留原配置、备份 .bak、幂等、非法原文件不被破坏
* 不写入 API Key、路径含空格、环境变量覆盖配置目录、--verify

运行：
    python -m pytest tests/test_install_mcp.py -q
"""

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parent.parent
INSTALLER = MCP_DIR / "install_mcp.py"
SERVER = MCP_DIR / "openvsp_mcp_server.py"
PYTHON = sys.executable

DEFAULT_OPENVSP_ROOT = r"<OPENVSP_ROOT>"
OPENVSP_ROOT = os.environ.get("OPENVSP_ROOT") or DEFAULT_OPENVSP_ROOT
if not Path(OPENVSP_ROOT, "vspaero.exe").is_file():
    pytestmark = pytest.mark.skip(
        reason="未找到 OpenVSP 安装目录（可用 OPENVSP_ROOT 指定），跳过安装器测试")
    OPENVSP_ROOT = DEFAULT_OPENVSP_ROOT

TARGETS = ("codex", "claude", "zcode", "kimi", "dsh")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clean_env(home: Path, extra=None):
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "OPENVSP_ROOT": OPENVSP_ROOT,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    for key in ("CODEX_HOME", "KIMI_SHARE_DIR", "DSH_HOME",
                "OPENVSP_PYTHON", "OPENVSP_PYTHONPATH",
                "OPENVSP_MCP_CLAUDE_BIN"):
        env.pop(key, None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run_installer(args, home: Path, extra_env=None, timeout=300):
    return subprocess.run(
        [PYTHON, str(INSTALLER)] + list(args),
        cwd=str(MCP_DIR), env=_clean_env(home, extra_env),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)


def run_json(args, home: Path, extra_env=None):
    proc = run_installer(list(args) + ["--json"], home, extra_env)
    stdout = (proc.stdout or "").strip()
    start = stdout.find("{")
    end = stdout.rfind("}")
    assert start >= 0 and end > start, (
        "安装器未输出 JSON（exit=%s）\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (proc.returncode, proc.stdout[-3000:], proc.stderr[-3000:]))
    payload = json.loads(stdout[start:end + 1])
    return proc, payload


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def home_file(home: Path, *parts) -> Path:
    return home.joinpath(*parts)


def assert_no_secrets(text: str, where: str = ""):
    lowered = text.lower()
    for marker in ("api_key", "apikey", "api-key", "secret", "token", "sk-"):
        assert marker not in lowered, "配置中不应包含凭据字段（%s）: %s" % (marker, where)


def assert_entry_ok(entry: dict, expected_server: str):
    assert entry["command"] == PYTHON
    assert entry["args"] == [str(SERVER)]
    assert entry["env"]["OPENVSP_ROOT"] == OPENVSP_ROOT
    assert "OPENVSP_PYTHONPATH" in entry["env"]


# ---------------------------------------------------------------------------
# 1. 探测 / print-command
# ---------------------------------------------------------------------------


def test_print_command_json(tmp_path):
    proc, payload = run_json(["--print-command", "--target", "all", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    detected = payload["detected"]
    assert Path(detected["server"]).name == "openvsp_mcp_server.py"
    assert Path(detected["server"]).is_file()
    assert Path(detected["python"]).is_file()
    assert detected["openvsp_root"] is not None
    assert Path(detected["openvsp_root"], "vspaero.exe").is_file()
    assert "--install --target all" in payload["install_command"]
    assert "--json" in payload["install_command"]

    standard = payload["standard_json"]["mcpServers"]["openvsp"]
    assert_entry_ok(standard, str(SERVER))

    targets = payload["targets"]
    assert set(targets) == set(TARGETS)
    assert "[mcp_servers.openvsp]" in targets["codex"]["toml"]
    assert "claude mcp add" in targets["claude"]["cli"]
    assert "kimi mcp add" in targets["kimi"]["cli"]
    assert targets["zcode"]["native_json"]["mcp"]["servers"]["openvsp"]
    assert "dsh-mcp-client" in targets["dsh"]["yaml"]
    for target in TARGETS:
        assert targets[target]["docs"].startswith("http")


def test_probe_python_import_openvsp(tmp_path):
    proc, payload = run_json(["--print-command", "--target", "all"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    info = payload["detected"]["python_info"]
    assert info["exists"] is True
    assert info["import_openvsp"] is True, info
    assert "OpenVSP" in str(info["version"])


# ---------------------------------------------------------------------------
# 2. dry-run 不落盘
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    (tmp_path / ".dsh").mkdir()
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    proc, payload = run_json(["--dry-run", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    for target in TARGETS:
        assert payload["results"][target]["ok"], payload["results"][target]
        assert payload["results"][target]["action"].startswith("would-"), \
            payload["results"][target]
    after = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    assert before == after, "dry-run 不应创建任何文件: %s" % (after,)
    assert not (MCP_DIR / "openvsp-mcp.cordis.yml").exists()


# ---------------------------------------------------------------------------
# 3. install all（空 HOME）
# ---------------------------------------------------------------------------


def test_install_all_creates_configs(tmp_path):
    (tmp_path / ".dsh").mkdir()
    proc, payload = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert payload["ok"] is True
    for target in TARGETS:
        assert payload["results"][target]["ok"], payload["results"][target]

    # Codex: TOML
    toml_path = home_file(tmp_path, ".codex", "config.toml")
    data = tomllib.loads(read(toml_path))
    entry = data["mcp_servers"]["openvsp"]
    assert entry["command"] == PYTHON
    assert entry["args"] == [str(SERVER)]
    assert entry["env"]["OPENVSP_ROOT"] == OPENVSP_ROOT

    # Claude Code: ~/.claude.json mcpServers
    claude = json.loads(read(home_file(tmp_path, ".claude.json")))
    assert_entry_ok(claude["mcpServers"]["openvsp"], str(SERVER))
    assert claude["mcpServers"]["openvsp"]["type"] == "stdio"

    # Kimi: ~/.kimi/mcp.json
    kimi = json.loads(read(home_file(tmp_path, ".kimi", "mcp.json")))
    assert_entry_ok(kimi["mcpServers"]["openvsp"], str(SERVER))

    # ZCode: ~/.zcode/cli/config.json -> mcp.servers
    zcode = json.loads(read(home_file(tmp_path, ".zcode", "cli", "config.json")))
    assert_entry_ok(zcode["mcp"]["servers"]["openvsp"], str(SERVER))

    # DSH: ~/.dsh/cordis.patch.yml + 独立 patch
    dsh_text = read(home_file(tmp_path, ".dsh", "cordis.patch.yml"))
    assert "- insert:" in dsh_text
    assert "name: '@deepseek-ai/dsh-mcp-client'" in dsh_text
    assert "serverName: openvsp" in dsh_text
    assert "transport: stdio" in dsh_text
    assert "OPENVSP_ROOT:" in dsh_text
    assert "cwd:" in dsh_text
    standalone = home_file(tmp_path, ".dsh", "openvsp-mcp.cordis.yml")
    assert standalone.is_file()
    assert "dsh-mcp-client" in read(standalone)

    # 安全：不写任何凭据
    for path in (toml_path, home_file(tmp_path, ".claude.json"),
                 home_file(tmp_path, ".kimi", "mcp.json"),
                 home_file(tmp_path, ".zcode", "cli", "config.json"),
                 home_file(tmp_path, ".dsh", "cordis.patch.yml")):
        assert_no_secrets(read(path), str(path))
        # 备份不应在首次创建时出现
        assert not path.with_name(path.name + ".bak").exists()


# ---------------------------------------------------------------------------
# 4. 合并保留原配置 + 备份
# ---------------------------------------------------------------------------


def test_merge_preserves_existing_json(tmp_path):
    (tmp_path / ".dsh").mkdir()
    claude_original = {
        "numStartups": 3,
        "mcpServers": {"existing": {"command": "npx", "args": ["x"]}},
    }
    kimi_original = {"mcpServers": {"context7": {"url": "https://example.invalid/mcp"}}}
    zcode_original = {"mcp": {"servers": {"memory": {"command": "npx"}}}, "theme": "dark"}
    for path, content in (
            (home_file(tmp_path, ".claude.json"), claude_original),
            (home_file(tmp_path, ".kimi", "mcp.json"), kimi_original),
            (home_file(tmp_path, ".zcode", "cli", "config.json"), zcode_original)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")

    proc, payload = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr

    claude = json.loads(read(home_file(tmp_path, ".claude.json")))
    assert claude["numStartups"] == 3
    assert "existing" in claude["mcpServers"]
    assert "openvsp" in claude["mcpServers"]
    bak = home_file(tmp_path, ".claude.json.bak")
    assert bak.is_file()
    assert json.loads(read(bak)) == claude_original

    kimi = json.loads(read(home_file(tmp_path, ".kimi", "mcp.json")))
    assert "context7" in kimi["mcpServers"]
    assert "openvsp" in kimi["mcpServers"]
    assert home_file(tmp_path, ".kimi", "mcp.json.bak").is_file()

    zcode = json.loads(read(home_file(tmp_path, ".zcode", "cli", "config.json")))
    assert zcode["theme"] == "dark"
    assert "memory" in zcode["mcp"]["servers"]
    assert "openvsp" in zcode["mcp"]["servers"]
    assert home_file(tmp_path, ".zcode", "cli", "config.json.bak").is_file()


def test_toml_merge_preserves_other_sections(tmp_path):
    codex_path = home_file(tmp_path, ".codex", "config.toml")
    codex_path.parent.mkdir(parents=True)
    original = (
        '[model]\n'
        'name = "gpt-5"\n'
        '\n'
        '[mcp_servers.other]\n'
        'command = "npx"\n'
        'args = ["other-server"]\n'
    )
    codex_path.write_text(original, encoding="utf-8")

    proc, payload = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert payload["results"]["codex"]["action"] == "updated"
    assert home_file(tmp_path, ".codex", "config.toml.bak").is_file()
    assert read(home_file(tmp_path, ".codex", "config.toml.bak")) == original

    new_text = read(codex_path)
    data = tomllib.loads(new_text)
    assert data["model"]["name"] == "gpt-5"
    assert data["mcp_servers"]["other"]["command"] == "npx"
    assert data["mcp_servers"]["openvsp"]["args"] == [str(SERVER)]
    assert new_text.count("[mcp_servers.openvsp]") == 1
    # 再次安装：内容不变、无第二个 openvsp 段
    before = digest(codex_path)
    proc2, payload2 = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc2.returncode == 0
    assert payload2["results"]["codex"]["action"] == "unchanged"
    assert digest(codex_path) == before
    assert read(codex_path).count("[mcp_servers.openvsp]") == 1


# ---------------------------------------------------------------------------
# 5. 幂等
# ---------------------------------------------------------------------------


def test_install_is_idempotent(tmp_path):
    (tmp_path / ".dsh").mkdir()
    proc1, payload1 = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc1.returncode == 0, proc1.stderr
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    hashes = {str(p): digest(p) for p in files}
    mtimes = {str(p): p.stat().st_mtime_ns for p in files}

    proc2, payload2 = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc2.returncode == 0, proc2.stderr
    for target in TARGETS:
        assert payload2["results"][target]["action"] == "unchanged", payload2["results"][target]
    for path in files:
        assert digest(path) == hashes[str(path)]
        assert path.stat().st_mtime_ns == mtimes[str(path)]
    # 幂等时不应新增 .bak
    assert not any(p.name.endswith(".bak") for p in tmp_path.rglob("*"))


# ---------------------------------------------------------------------------
# 6. 非法原文件保护
# ---------------------------------------------------------------------------


def test_invalid_json_is_preserved(tmp_path):
    (tmp_path / ".dsh").mkdir()
    broken = home_file(tmp_path, ".claude.json")
    broken.write_text("{ this is not valid json ", encoding="utf-8")
    proc, payload = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 2  # 有 target 失败
    assert payload["ok"] is False
    claude = payload["results"]["claude"]
    assert claude["ok"] is False
    assert claude["action"] == "error"
    assert read(broken) == "{ this is not valid json "
    assert not home_file(tmp_path, ".claude.json.bak").exists()
    # 其他 target 仍然写入成功
    assert payload["results"]["codex"]["ok"] is True
    assert home_file(tmp_path, ".codex", "config.toml").is_file()


def test_invalid_toml_is_preserved(tmp_path):
    codex = home_file(tmp_path, ".codex", "config.toml")
    codex.parent.mkdir(parents=True)
    broken = '[model\nname = "broken"\n'
    codex.write_text(broken, encoding="utf-8")
    proc, payload = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert payload["results"]["codex"]["ok"] is False
    assert payload["results"]["codex"]["action"] == "error"
    assert read(codex) == broken


# ---------------------------------------------------------------------------
# 7. DSH 合并的特殊情况
# ---------------------------------------------------------------------------


def test_dsh_merges_existing_patch_and_backup(tmp_path):
    dsh = home_file(tmp_path, ".dsh")
    dsh.mkdir(parents=True)
    patch = dsh / "cordis.patch.yml"
    original = (
        "- insert:\n"
        "    - id: memory-other\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: memory-other\n"
        "        transport: stdio\n"
        "        command: other-mcp\n"
    )
    patch.write_text(original, encoding="utf-8")
    proc, payload = run_json(["--install", "--target", "dsh", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    text = read(patch)
    assert "memory-other" in text
    assert "serverName: openvsp" in text
    assert text.count("serverName: openvsp") == 1
    bak = dsh / "cordis.patch.yml.bak"
    assert bak.is_file() and read(bak) == original
    # 幂等
    proc2, payload2 = run_json(["--install", "--target", "dsh", "--no-cli", "--no-probe"], tmp_path)
    assert proc2.returncode == 0
    assert payload2["results"]["dsh"]["action"] == "unchanged"
    assert read(patch).count("serverName: openvsp") == 1


def test_dsh_non_patch_file_not_modified(tmp_path):
    dsh = home_file(tmp_path, ".dsh")
    dsh.mkdir(parents=True)
    patch = dsh / "cordis.patch.yml"
    original = "some: mapping\nnot-a-patch-list: true\n"
    patch.write_text(original, encoding="utf-8")
    proc, payload = run_json(["--install", "--target", "dsh", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    result = payload["results"]["dsh"]
    assert result["ok"] is True
    assert result["action"] == "standalone-only"
    assert read(patch) == original
    standalone = dsh / "openvsp-mcp.cordis.yml"
    assert standalone.is_file()
    assert "serverName: openvsp" in read(standalone)


# ---------------------------------------------------------------------------
# 8. 环境变量覆盖配置目录 / 路径含空格
# ---------------------------------------------------------------------------


def test_env_overrides_config_dirs(tmp_path):
    codex_home = tmp_path / "custom-codex"
    kimi_share = tmp_path / "custom-kimi"
    dsh_home = tmp_path / "custom-dsh"
    dsh_home.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    proc, payload = run_json(
        ["--install", "--target", "codex", "--target", "kimi", "--target", "dsh",
         "--no-cli", "--no-probe"],
        home, extra_env={"CODEX_HOME": codex_home, "KIMI_SHARE_DIR": kimi_share,
                         "DSH_HOME": dsh_home})
    assert proc.returncode == 0, proc.stderr
    assert (codex_home / "config.toml").is_file()
    assert tomllib.loads(read(codex_home / "config.toml"))["mcp_servers"]["openvsp"]
    assert (kimi_share / "mcp.json").is_file()
    assert (dsh_home / "cordis.patch.yml").is_file()
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".kimi" / "mcp.json").exists()


def test_paths_with_spaces(tmp_path):
    home = tmp_path / "home with spaces"
    home.mkdir()
    server_dir = tmp_path / "mcp dir with spaces"
    server_dir.mkdir()
    import shutil as _shutil
    shutil_server = server_dir / "openvsp_mcp_server.py"
    _shutil.copy2(str(SERVER), str(shutil_server))
    proc, payload = run_json(
        ["--install", "--target", "all", "--no-cli", "--no-probe",
         "--server", str(shutil_server), "--home", str(home),
         "--workspace", str(server_dir / "workspace")],
        home, extra_env={"DSH_HOME": str(home / ".dsh")})
    assert proc.returncode == 0, proc.stderr
    entry = tomllib.loads(read(home / ".codex" / "config.toml"))["mcp_servers"]["openvsp"]
    assert entry["args"] == [str(shutil_server)]
    claude = json.loads(read(home / ".claude.json"))
    assert claude["mcpServers"]["openvsp"]["args"] == [str(shutil_server)]
    dsh_text = read(home / ".dsh" / "cordis.patch.yml")
    assert str(server_dir) in dsh_text.replace("''", "'")


# ---------------------------------------------------------------------------
# 9. verify
# ---------------------------------------------------------------------------


def test_verify_after_install(tmp_path):
    (tmp_path / ".dsh").mkdir()
    proc, _ = run_json(["--install", "--target", "all", "--no-cli", "--no-probe"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    proc, payload = run_json(
        ["--verify", "--target", "all", "--no-cli", "--no-probe"],
        tmp_path, extra_env={"DSH_HOME": str(tmp_path / ".dsh")})
    assert proc.returncode == 0, proc.stderr
    assert payload["openvsp"]["ok"] is True, payload["openvsp"]
    assert payload["openvsp"]["openvsp_available"] is True
    assert payload["openvsp"]["vspaero_available"] is True
    for target in TARGETS:
        assert payload["targets"][target]["installed"] is True, payload["targets"][target]
