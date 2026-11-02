# OpenVSP MCP 安装教程（Codex / Claude Code / Kimi / ZCode / DSH）

> 本页由 **OpenVSP R2 → AI 面板 → “MCP调用”** 按钮打开；R2 会在本页顶部自动注入
> 本机真实路径（`server` / `python` / `openvsp_root`）。
> 若你看到的是 `<python>`、`<server.py>` 这类占位符，请先在本机运行
> `python <R2安装目录>\mcp\install_mcp.py --print-command --target all`
> 获取带真实路径的命令（安装器会自动探测路径，不依赖本页占位符）。
>
> 安装器位置：`<R2安装目录>\mcp\install_mcp.py`
> MCP 服务器：`<R2安装目录>\mcp\openvsp_mcp_server.py`
> 本教程所有路径均可用安装器 `--print-command` 输出的真实值替换。

> ⚠️ **分发到其他电脑/安装目录时注意**：本教程中的 `D:\...` 或顶部注入的绝对路径
> 只对当前这台电脑的当前安装目录有效。每台电脑的 OpenVSP 安装目录、Python 路径都可能不同，
> 请勿把本机绝对路径直接复制给其他人。对方应使用其**自己安装目录**下的
> `mcp\install_mcp.py`（或点击 R2 面板的“复制安装命令”按钮获取其本机命令），
> 安装器会自动探测 server、Python 与 OpenVSP 根目录，无需手工改成本机路径。

---

## 1. 一键自动安装（把这条命令交给 Agent）

### 1.1 推荐：整段交给 Agent

把下面这段提示词（连同其中的命令）发给你正在使用的 Agent（Codex / Claude Code /
Kimi CLI / ZCode / DSH 均可），它会自动完成探测与五类 Agent 的配置写入：

```text
请在本机执行下面的命令，安装 OpenVSP MCP 服务器并配置所有支持的 Agent。
要求：1) 先运行 --dry-run 预览；2) 确认无报错后运行 --install；3) 最后运行 --verify；
4) 把每个 Agent 的配置文件路径、验证结果（openvsp_available / vspaero_available）
   和失败原因（如有）报告给我。

命令（把 <python> 和 <R2安装目录> 替换为本机真实路径，或直接使用 R2 顶部注入的路径）：
<python> "<R2安装目录>\mcp\install_mcp.py" --install --target all --json
```

安装器会：
- 自动探测 MCP server 路径、可用的 Python 解释器、OpenVSP 安装根目录；
- 以**安全合并**方式写入/更新 Codex、Claude Code、Kimi、ZCode、DSH 的用户级 MCP 配置；
- 修改前把原配置备份为 `*.bak`；原文件非法（JSON/TOML 解析失败）时**拒绝覆盖**；
- 重复执行是幂等的（已安装的 target 返回 `unchanged`），不会写重复条目；
- **不写任何 API Key**（env 只包含 `OPENVSP_ROOT` / `OPENVSP_PYTHONPATH`）。

### 1.2 手动三步

```bat
:: 1) 预览（不落盘）
<python> "<R2安装目录>\mcp\install_mcp.py" --dry-run --target all

:: 2) 实际安装（会备份原配置）
<python> "<R2安装目录>\mcp\install_mcp.py" --install --target all

:: 3) 验证
<python> "<R2安装目录>\mcp\install_mcp.py" --verify --target all
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--target codex\|claude\|kimi\|zcode\|dsh\|all` | 目标 Agent（可重复指定），默认 `all` |
| `--print-command` | 打印五类 Agent 的准确命令/配置片段（含“交给 Agent 自动安装”命令） |
| `--dry-run` | 只预览将写入的内容，不落盘（默认模式） |
| `--install` | 实际写入/合并配置（自动备份 `*.bak`） |
| `--verify` | 检查 server 可运行（openvsp/VSPAERO 可用）+ 各目标配置是否已含 openvsp |
| `--python` / `--openvsp-root` / `--server` / `--workspace` | 手动覆盖探测结果 |
| `--home` | 覆盖 home（便携/测试用） |
| `--no-cli` | Claude Code 即使有 `claude` CLI 也走配置文件合并 |
| `--json` | 输出机器可读 JSON（便于 Agent 解析结果） |

### 1.3 回滚

每个被修改的配置文件旁都有 `*.bak`（修改前的内容）。回滚示例：

```bat
copy /Y "%USERPROFILE%\.codex\config.toml.bak" "%USERPROFILE%\.codex\config.toml"
copy /Y "%USERPROFILE%\.claude.json.bak" "%USERPROFILE%\.claude.json"
copy /Y "%USERPROFILE%\.kimi\mcp.json.bak" "%USERPROFILE%\.kimi\mcp.json"
copy /Y "%USERPROFILE%\.zcode\cli\config.json.bak" "%USERPROFILE%\.zcode\cli\config.json"
copy /Y "%DSH_HOME%\cordis.patch.yml.bak" "%DSH_HOME%\cordis.patch.yml"
```

---

## 2. 五类 Agent 的准确配置

> 通用约定：条目统一以 `openvsp` 命名；stdio 启动命令为
> `<python> <R2安装目录>\mcp\openvsp_mcp_server.py`；
> `env` 只写路径（`OPENVSP_ROOT`、`OPENVSP_PYTHONPATH`）。
> 各 Agent 版本差异可能导致路径/字段变化，**以对应 Agent 官方文档与
> `--print-command` 输出为准**，官方依据见每节末尾。

### 2.1 Codex CLI

| 项 | 内容 |
|---|---|
| 配置文件 | `%USERPROFILE%\.codex\config.toml`（设置了 `CODEX_HOME` 时为 `%CODEX_HOME%\config.toml`） |
| 配置键 | `[mcp_servers.openvsp]`（`command` / `args` / `env`） |
| 自动安装 | `install_mcp.py --install --target codex`（TOML 安全合并，保留 `[model]` 等其他段） |
| 备用 CLI | 若当前 Codex 版本提供 `codex mcp` 子命令：`codex mcp add openvsp --env OPENVSP_ROOT=... -- <python> <server>`（以 `codex mcp --help` 为准） |
| 生效 | 重启 Codex；新会话中让它调用 `openvsp_status` |

```toml
[mcp_servers.openvsp]
command = "<python>"
args = ["<R2安装目录>\\mcp\\openvsp_mcp_server.py"]
env = { OPENVSP_ROOT = "<openvsp_root>", OPENVSP_PYTHONPATH = "<openvsp_root>\\python\\openvsp;<openvsp_root>\\python\\utilities;<openvsp_root>\\python\\degen_geom;<openvsp_root>\\python\\openvsp_config;<openvsp_root>\\python\\openvsp\\openvsp" }
```

依据：<https://github.com/openai/codex/blob/main/docs/config.md>

### 2.2 Claude Code

| 项 | 内容 |
|---|---|
| 首选（CLI，用户级） | `claude mcp add --transport stdio --scope user --env OPENVSP_ROOT=... --env OPENVSP_PYTHONPATH=... openvsp -- <python> <server>` |
| 无 CLI 时回退 | 合并 `%USERPROFILE%\.claude.json` 顶层的 `mcpServers.openvsp`（安装器自动处理） |
| 项目级替代 | `<项目>\.mcp.json` 的 `mcpServers`（仅该项目生效） |
| 自动安装 | `install_mcp.py --install --target claude`（检测到 `claude` CLI 时优先用 CLI；`--no-cli` 强制走 JSON 合并） |
| 生效/验证 | 重启 Claude Code；`claude mcp list` 应显示 `openvsp`；或在会话内 `/mcp` 查看 |

```json
{
  "mcpServers": {
    "openvsp": {
      "type": "stdio",
      "command": "<python>",
      "args": ["<R2安装目录>\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "<openvsp_root>",
        "OPENVSP_PYTHONPATH": "<openvsp_root>\\python\\openvsp;<openvsp_root>\\python\\utilities;<openvsp_root>\\python\\degen_geom;<openvsp_root>\\python\\openvsp_config;<openvsp_root>\\python\\openvsp\\openvsp"
      }
    }
  }
}
```

依据：<https://code.claude.com/docs/en/mcp>

### 2.3 Kimi Code CLI（Kimi CLI）

| 项 | 内容 |
|---|---|
| 配置文件 | `%USERPROFILE%\.kimi\mcp.json`（设置 `KIMI_SHARE_DIR` 时为 `%KIMI_SHARE_DIR%\mcp.json`） |
| 配置键 | `mcpServers`（与 Claude Code/Cursor 等通用 JSON 兼容） |
| CLI | `kimi mcp add --transport stdio --env OPENVSP_ROOT=... --env OPENVSP_PYTHONPATH=... openvsp -- <python> <server>` |
| 自动安装 | `install_mcp.py --install --target kimi`（JSON 安全合并） |
| 生效/验证 | 重启 Kimi CLI；`kimi mcp list` 显示 `openvsp`；`kimi mcp test openvsp` 列出工具（应含 `openvsp_status` 等 8 个） |
| 临时加载 | `kimi --mcp-config-file <mcp.json>` 或 `kimi --mcp-config '{"mcpServers":{...}}'` |

```json
{
  "mcpServers": {
    "openvsp": {
      "command": "<python>",
      "args": ["<R2安装目录>\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "<openvsp_root>",
        "OPENVSP_PYTHONPATH": "<openvsp_root>\\python\\openvsp;<openvsp_root>\\python\\utilities;<openvsp_root>\\python\\degen_geom;<openvsp_root>\\python\\openvsp_config;<openvsp_root>\\python\\openvsp\\openvsp"
      }
    }
  }
}
```

依据：<https://moonshotai.github.io/kimi-cli/en/customization/mcp.html>
（`kimi mcp` 子命令：<https://moonshotai.github.io/kimi-cli/en/reference/kimi-mcp.html>）

### 2.4 ZCode（智谱编码 Agent）

| 项 | 内容 |
|---|---|
| 用户级配置 | `%USERPROFILE%\.zcode\cli\config.json`，键 `mcp.servers`（注意：用户级在 `.zcode\cli\` 下） |
| 工作区级配置 | `<项目根>\.zcode\config.json`，键 `mcp.servers`（仅该项目生效，会话启动自动连接） |
| `.agents` 兼容 | `%USERPROFILE%\.agents\mcp.json`（键 `mcpServers`，仅当同作用域 `.zcode` 无任何服务时兜底） |
| 面板入口 | ZCode **设置 → MCP 服务器 → 新建**，或 **完整配置** 模式直接粘贴标准 JSON（支持 `{"mcpServers":{...}}` 与 `{"server-name":{...}}` 两种结构） |
| 自动安装 | `install_mcp.py --install --target zcode`（写入用户级原生配置，保留其他 MCP 服务） |
| 生效/验证 | 重启/重载 ZCode，设置 → MCP 服务器 列表中出现 `openvsp` 且启用 |
| 导入提示 | ZCode 支持从 Claude Code / Codex CLI 等外部配置**导入**，若已装过其他 Agent，可直接导入 |

```json
{
  "mcp": {
    "servers": {
      "openvsp": {
        "command": "<python>",
        "args": ["<R2安装目录>\\mcp\\openvsp_mcp_server.py"],
        "env": {
          "OPENVSP_ROOT": "<openvsp_root>",
          "OPENVSP_PYTHONPATH": "<openvsp_root>\\python\\openvsp;<openvsp_root>\\python\\utilities;<openvsp_root>\\python\\degen_geom;<openvsp_root>\\python\\openvsp_config;<openvsp_root>\\python\\openvsp\\openvsp"
        }
      }
    }
  }
}
```

依据：<https://zcode.z.ai/cn/docs/mcp-services>
（配置文件路径/优先级/导入来源均以该页为准；ZCode 版本更新较快，若路径变化以最新官方文档为准，
安装器仍可用“完整配置”粘贴的通用 JSON 作为兜底。）

### 2.5 DeepSeek Harness（DSH）

| 项 | 内容 |
|---|---|
| 配置文件 | `%DSH_HOME%\cordis.patch.yml`（默认 `%USERPROFILE%\.dsh\cordis.patch.yml`，对本机所有 profile 生效） |
| 单 profile | `%DSH_HOME%\profiles\<name>\cordis.patch.yml`（只对指定 profile 生效） |
| 插件 | `@deepseek-ai/dsh-mcp-client`（stdio / streamable-http 通用 MCP 客户端） |
| 独立 patch | 安装器同时在 `$DSH_HOME\openvsp-mcp.cordis.yml` 生成独立 patch，可用 `dsh web --patch "<文件>"` 临时加载 |
| 自动安装 | `install_mcp.py --install --target dsh`（仅向**列表型** patch 追加，不覆盖已有条目） |
| 生效/验证 | 重启/重载 DSH；新会话中工具以 `mcp__openvsp__<tool>` 暴露（等待首次发现完成）；让它调用 `mcp__openvsp__openvsp_status` |

```yaml
- insert:
    - id: openvsp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: openvsp
        transport: stdio
        command: '<python>'
        args:
          - '<R2安装目录>\mcp\openvsp_mcp_server.py'
        env:
          OPENVSP_ROOT: '<openvsp_root>'
          OPENVSP_PYTHONPATH: '<openvsp_root>\python\openvsp;<openvsp_root>\python\utilities;<openvsp_root>\python\degen_geom;<openvsp_root>\python\openvsp_config;<openvsp_root>\python\openvsp\openvsp'
        cwd: '<R2安装目录>\mcp'
```

注意：DSH stdio 桥接器启动子进程前会移除常见凭据类环境变量与所有 `DSH_*` 变量，
其余环境变量会继承；本服务器不需要任何凭据。**不要覆盖已有 `cordis.patch.yml`**，
安装器采用追加/幂等策略。

依据：<https://deepseek-harness.github.io/deepseek-harness/guide/mcp-memory>
（`DSH_HOME` 默认 `~/.dsh` 见 DSH 配置目录文档）

---

## 3. 标准 JSON 配置（通用兜底）

任何支持 `mcpServers` 的客户端（Claude Code、Kimi、Cursor、`.agents/mcp.json` 等）
都可直接使用：

```json
{
  "mcpServers": {
    "openvsp": {
      "command": "<python>",
      "args": ["<R2安装目录>\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "<openvsp_root>",
        "OPENVSP_PYTHONPATH": "<openvsp_root>\\python\\openvsp;<openvsp_root>\\python\\utilities;<openvsp_root>\\python\\degen_geom;<openvsp_root>\\python\\openvsp_config;<openvsp_root>\\python\\openvsp\\openvsp"
      }
    }
  }
}
```

环境变量说明（MCP 服务器读取）：

| 变量 | 说明 |
|---|---|
| `OPENVSP_ROOT` | OpenVSP 安装根目录（含 `vsp.exe` / `vspaero.exe`） |
| `OPENVSP_PYTHONPATH` | OpenVSP Python API 路径（`;` 分隔；不设置时服务器按 `OPENVSP_ROOT` 自动推导） |
| `OPENVSP_PYTHON`（可选） | worker 子进程使用的 Python；默认取启动服务器的解释器 |
| `OPENVSP_MCP_WORKDIR`（可选） | 输出目录（.vsp3 / VSPAERO / 报告），默认 `<mcp>\workspace` |

> 路径含空格时：JSON/YAML 字符串无需额外处理（安装器已正确转义）；TOML 中使用
> 安装器生成的带引号字符串。不要手工在 env 里写任何 API Key。

---

## 4. 验证安装

### 4.1 安装器自检

```bat
<python> "<R2安装目录>\mcp\install_mcp.py" --verify --target all
```

输出应包含：
- `openvsp 可用 : True`、`openvsp 版本 : OpenVSP 3.51.3`、`VSPAERO 可用 : True`
- 五个 target 全部 `OK`（`codex / claude / zcode / kimi / dsh`）

### 4.2 服务器自检

```bat
<python> "<R2安装目录>\mcp\openvsp_mcp_server.py" --status
```

返回 JSON 中 `openvsp.available=true`、`openvsp.version="OpenVSP 3.51.3"`、
`openvsp.vspaero_available=true`、`protocol.protocolVersion="2025-11-25"`。

### 4.3 端到端测试（可选）

```bat
cd /d "<R2安装目录>\mcp"
<python> tests\test_stdio.py
```

### 4.4 在 Agent 中验证

重启/重载对应 Agent，**新建会话**后发送：

```text
请调用 openvsp_status 工具，告诉我 OpenVSP 版本、安装目录以及 VSPAERO 是否可用；
然后调用 openvsp_build_aircraft 生成一个 S_ref=1.2、AR=8 的小型无人机模型。
```

预期：工具被正确调用并返回 OpenVSP 版本、几何组件列表与 `.vsp3` 路径。

### 4.5 验证失败排查

| 现象 | 处理 |
|---|---|
| `--verify` 报 openvsp 不可用 | 检查 `OPENVSP_ROOT`；用 `--python` 指定能 `import openvsp`（需 numpy）的 Python |
| Agent 中看不到 openvsp 工具 | 确认已重启/新建会话；确认配置文件被写入（`--print-command` 显示路径）；检查客户端是否要求 modern 协议（见 5.2） |
| 工具调用报 `vsp_unavailable` | 同上；也可在客户端 env 中补 `OPENVSP_ROOT`，或让 `--python` 指向正确解释器 |
| 配置被客户端覆盖 | 用 `.bak` 恢复后重新运行 `--install`；注意部分客户端保存设置时会重写配置文件 |
| 路径含空格导致启动失败 | 重新运行安装器（会自动正确引用/转义）；不要手工拼命令 |

---

## 5. 安全说明与常见问题

### 5.1 安全说明

1. **不写凭据**：安装器只写 `OPENVSP_ROOT` / `OPENVSP_PYTHONPATH` 两个路径变量，
   不读取、不写入任何 API Key/Token；不要手工在 Agent 配置里放密钥。
2. **最小改动 + 备份**：只修改五类 Agent 的**用户级 MCP 配置**（Codex TOML、
   Claude JSON、Kimi JSON、ZCode JSON、DSH patch），不触碰其他设置；修改前生成
   `*.bak`；原文件无法解析时**拒绝覆盖**，保证可回滚。
3. **幂等**：重复安装不会产生重复条目；已安装时返回 `unchanged`。
4. **本地 stdio**：MCP 服务器以 stdio 子进程运行，不监听端口；标准输出只输出
   JSON-RPC 协议，日志全部走 stderr；工具调用会执行本机 OpenVSP/VSPAERO
   （生成/读写工作目录内文件），请只连接可信的客户端与本机配置。
5. **建议**：安装后检查一次生成的配置文件内容（或看 `--dry-run` 输出）；
   不要随意运行来源不明的 `.vsp3`/宏脚本。

### 5.2 常见问题（FAQ）

**Q1：Agent 提示 MCP 握手失败 / 工具不可用？**
本服务器实现 **legacy MCP 生命周期**（`initialize` + `notifications/initialized`，
`protocolVersion=2025-11-25`，兼容 2025-06-18/2024-11-05）。若客户端强制 modern
协议（2026-07-28 的 `server/discover`/`_meta` 流程），会得到明确的降级错误而无法建立
会话——请把客户端切到 legacy 兼容模式/版本，或等待后续 modern 支持。

**Q2：找不到 OpenVSP 安装目录？**
安装器会依次尝试 `OPENVSP_ROOT`、server 目录向上/邻近查找、常见默认路径。
仍失败时：
```bat
<python> "...\install_mcp.py" --install --target all --openvsp-root "<OPENVSP_ROOT>"
```

**Q3：Python 无法 `import openvsp`？**
OpenVSP Python API 需要 numpy 与 OpenVSP DLL。用 `--python` 指向已验证可用的解释器
（与运行 MCP 服务器同一个），安装器会把该解释器写进配置；`OPENVSP_PYTHONPATH`
也会一并写入，无需手动设置 `PYTHONPATH`。

**Q4：Codex 版本没有 `codex mcp add` 命令？**
以 `codex mcp --help` 为准；安装器主路径是 **TOML 合并**
（`~/.codex/config.toml` 的 `[mcp_servers.openvsp]`），不依赖该 CLI。

**Q5：Kimi / ZCode 的配置路径与本文不一致？**
两个 Agent 迭代较快，请以官方文档为准：
Kimi <https://moonshotai.github.io/kimi-cli/en/customization/mcp.html>，
ZCode <https://zcode.z.ai/cn/docs/mcp-services>。
安装器写入的是截至本文档版本的标准位置；若你的版本改用了其他文件，可用
`--print-command` 输出的**标准 JSON** 直接粘贴到对应 Agent 的 MCP 配置/导入入口
（Kimi 支持 `kimi mcp add`/`--mcp-config-file`，ZCode 支持“完整配置”粘贴与外部导入）。

**Q6：DSH 没安装 / 不想改 DSH 配置？**
安装器只生成独立 patch 文件 `%DSH_HOME%\openvsp-mcp.cordis.yml` 并提示加载命令：
`dsh web --patch "<该文件>"`；不会修改已有 `cordis.patch.yml`。

**Q7：如何卸载？**
用对应 `.bak` 恢复配置文件（见 1.3），或运行
`install_mcp.py --print-command` 后手工删除配置中的 `openvsp` 条目。

**Q8：安装器输出被 Agent 解析使用？**
支持 `--json`：`--dry-run|--install|--verify` 均输出结构化 JSON
（`ok`、`detected`、`results`/`targets`），便于 Agent 自动判断与汇报。

---

> 文档版本：MCP 安装教程 v0.1.0（MCP 服务器 `openvsp-mcp 0.1.0`，legacy 协议 2025-11-25）
> 相关文档：[README.md](./README.md) · [STATUS.md](./STATUS.md)
> 安装器：`mcp/install_mcp.py`；测试：`python -m pytest mcp/tests -q`
