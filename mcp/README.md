# openvsp-mcp — OpenVSP R2 MCP 服务器

OpenVSP R2 项目的 **MCP（Model Context Protocol）服务器**：通过 stdio + JSON-RPC 2.0
把 OpenVSP 3.51.3 的 Python API 与 VSPAERO 求解器暴露给 Claude Desktop、Cursor、
DSH（DeepSeek Harness）等 MCP 客户端。纯标准库实现（`requires-python >= 3.10`，
无第三方依赖），可选复用同项目 `../ai` 的 `ai_design` 包生成报告。

- 服务器入口：`openvsp_mcp_server.py`（`python openvsp_mcp_server.py`）
- 控制台脚本：`openvsp-mcp`（`pip install -e .` 后可用）
- 一键安装：`install_mcp.py`（自动探测路径，为 **Codex / Claude Code / Kimi CLI /
  ZCode / DSH** 五类 Agent 安全写入 MCP 配置；详见
  [MCP-安装教程.md](./MCP-安装教程.md)）
- 测试：`python tests/test_stdio.py`（`--full` 追加 VSPAERO / 报告 / worker 测试）、
  `python -m pytest tests -q`（含安装器测试）

---

## 1. 目录结构

```
<REPO_ROOT>\mcp\
├── openvsp_mcp_server.py    # MCP stdio 服务器（协议层 + 工具实现 + worker 模式）
├── install_mcp.py           # MCP 一键安装器（Codex/Claude Code/Kimi/ZCode/DSH，stdlib）
├── MCP-安装教程.md           # 五类 Agent 安装教程（R2 AI 面板“MCP调用”按钮展示）
├── pyproject.toml           # 打包配置（name=openvsp-mcp，入口 openvsp-mcp=openvsp_mcp_server:main）
├── README.md                # 本文件
├── STATUS.md                # 工具表、测试结果、已知限制
├── tests\
│   ├── test_stdio.py        # subprocess 端到端测试（JSON-RPC 断言）
│   └── test_install_mcp.py  # 安装器测试（dry-run/合并/幂等/备份/不破坏原配置）
└── workspace\               # 运行期默认输出目录（自动创建，可被 OPENVSP_MCP_WORKDIR 覆盖）
    ├── models\              # openvsp_build_aircraft 生成的 .vsp3
    ├── vspaero\             # VSPAERO 输出（.polar/.stab/.history/.lod/.vspgeom ...）
    ├── exports\             # openvsp_export 输出（vsp3/stl/csv）
    ├── reports\             # openvsp_generate_report 生成的 .docx
    └── tmp\                 # worker 子进程请求/响应临时文件
```

---

## 2. 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（当前实测环境） |
| Python | ≥ 3.10，实测 `<USER_HOME>\AppData\Local\Programs\Python\Python313\python.exe`（3.13.3） |
| OpenVSP | 3.51.3 安装目录 `<OPENVSP_ROOT>`（含 `vsp.exe` / `vspaero.exe`） |
| OpenVSP Python API | 位于 `<OPENVSP_ROOT>\python\openvsp`，导入需 numpy（实测 2.4.4 已安装） |
| 可选 | `python-docx`（本地 Word 报告）、`../ai` 的 `ai_design` 包（AI 报告） |

服务器**不需要**把 OpenVSP 的 python 目录永久写进 `PYTHONPATH`：它会自动按
`OPENVSP_ROOT` 推导并临时加入以下路径（顺序很重要，`utilities` 必须先于
`openvsp/openvsp`，否则会触发 circular import）：

```
<OPENVSP_ROOT>\python\openvsp
<OPENVSP_ROOT>\python\utilities
<OPENVSP_ROOT>\python\degen_geom
<OPENVSP_ROOT>\python\openvsp_config
<OPENVSP_ROOT>\python\openvsp\openvsp
```

同时调用 `os.add_dll_directory(<OPENVSP_ROOT>)` 以加载 OpenVSP DLL。

---

## 3. MCP 协议：legacy 握手（2025-11-25）与 modern 差异

本服务器实现 **legacy MCP 生命周期**，`protocolVersion` 返回 **`2025-11-25`**，
并兼容 `2025-06-18` / `2024-11-05`（客户端请求这些版本时原样回显）：

```
initialize -> result{protocolVersion:"2025-11-25", capabilities.tools, serverInfo}
notifications/initialized
tools/list -> result{tools:[...]}
tools/call -> result{content:[{type:"text", text:"<JSON>"}], isError:false|true}
ping -> {}
shutdown -> null（随后进程退出）
```

- **stdio 传输**：每行一条 JSON-RPC 2.0 消息（UTF-8、无 BOM、LF 换行）；
  `stdout` 只允许协议 JSON，**所有日志写 stderr**。服务器启动时会把真实 stdout
  文件描述符复制出来专供协议帧使用，并把 fd1 重定向到 stderr——因此 OpenVSP
  C++ API / VSPAERO 的 `printf` 输出不会污染协议流；fd0 重定向到 /dev/null，
  防止子进程抢读客户端输入。
- **错误码**：parse error `-32700`、invalid request `-32600`、method not found
  `-32601`、invalid params `-32602`、internal error `-32603`。未知工具/参数错误
  返回 JSON-RPC error；**工具执行失败**返回 `result.isError=true` + JSON 文本
  （含 `error.code`/`error.message`），便于模型自我修正。
- **modern 协议（2026-07-28）不做完整实现**，但做 dual-era 检测与降级提示：
  - `initialize` 请求 `protocolVersion:"2026-07-28"` 时：仍返回 legacy 结果
    （`protocolVersion:"2025-11-25"`），并在 `result._meta` 中说明
    `openvsp-mcp/requestedProtocolVersion` 与降级原因；
  - 未做 legacy 握手、请求 `_meta.io.modelcontextprotocol/protocolVersion`
    为 `2026-*` 的请求：返回 JSON-RPC `-32600`，`error.data` 提示仅支持
    legacy 握手；
  - `server/discover`：返回 `-32601`，`error.data` 含 `protocolEra:"legacy"`、
    `supportedProtocolVersions` 与提示。
  - 若客户端强制 modern 而无法降级，会在客户端侧会话建立失败，这是当前
    legacy 实现的已知边界（见 `STATUS.md`）。

---

## 4. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENVSP_ROOT` | `<OPENVSP_ROOT>` | OpenVSP 安装根目录（含 vsp.exe/vspaero.exe） |
| `OPENVSP_PYTHON` | 服务器自身 `sys.executable` | 运行 OpenVSP API / VSPAERO worker 子进程的 Python 解释器 |
| `OPENVSP_PYTHONPATH` | 空（自动推导） | 额外/覆盖的 OpenVSP Python API 路径，`os.pathsep` 分隔（Windows 用 `;`） |
| `OPENVSP_MCP_WORKDIR` | `<mcp>\workspace` | 输出目录（models/vspaero/exports/reports/tmp） |
| `OPENVSP_MCP_AI_DIR` | `<mcp>\..\ai` | 可选 AI 后端 `ai_design` 包所在目录 |
| `OPENVSP_MCP_AERO_SET` | `3` | 气动几何使用的 OpenVSP geometry-set 序号（3 起为 user set） |
| `OPENVSP_MCP_ANALYSIS_SUBPROCESS` | `1` | VSPAERO 计算默认放 worker 子进程（可超时/隔离崩溃） |
| `OPENVSP_MCP_FORCE_SUBPROCESS` | `0` | 所有 OpenVSP 操作都走 worker 子进程（服务器进程自身无需能 import openvsp） |
| `OPENVSP_MCP_WORKER_TIMEOUT` | `1800` | worker 超时秒数 |
| `OPENVSP_MCP_ANALYSIS_TIMEOUT` | `900` | VSPAERO `-stab` 子进程超时秒数 |
| `OPENVSP_MCP_LOG_LEVEL` | `INFO` | stderr 日志级别（DEBUG/INFO/WARN/ERROR） |

> `OPENVSP_PYTHONPATH` 示例：
> `<OPENVSP_ROOT>\python\openvsp;<OPENVSP_ROOT>\python\utilities;<OPENVSP_ROOT>\python\degen_geom;<OPENVSP_ROOT>\python\openvsp\openvsp`
> （不设置时服务器自动按 `OPENVSP_ROOT` 推导同样路径。）

---

## 5. 客户端配置示例

### 5.1 Claude Desktop

配置文件：`%APPDATA%\Claude\claude_desktop_config.json`（改完完全退出并重启 Claude Desktop）

```json
{
  "mcpServers": {
    "openvsp": {
      "command": "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
      "args": ["D:\\Zcoding\\OpenVSP-R2\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "D:\\Zcoding\\OpenVSP-3.51.3-win64",
        "OPENVSP_PYTHON": "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
        "OPENVSP_PYTHONPATH": "D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\openvsp;D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\utilities;D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\degen_geom;D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\openvsp\\openvsp",
        "OPENVSP_MCP_WORKDIR": "D:\\Zcoding\\OpenVSP-R2\\mcp\\workspace"
      }
    }
  }
}
```

### 5.2 Cursor

项目级 `.cursor/mcp.json`（或全局 `~/.cursor/mcp.json`）

```json
{
  "mcpServers": {
    "openvsp": {
      "type": "stdio",
      "command": "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
      "args": ["D:\\Zcoding\\OpenVSP-R2\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "D:\\Zcoding\\OpenVSP-3.51.3-win64",
        "OPENVSP_PYTHON": "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
        "OPENVSP_PYTHONPATH": "D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\openvsp;D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\utilities;D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\degen_geom;D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\openvsp\\openvsp"
      }
    }
  }
}
```

### 5.3 DSH（DeepSeek Harness，Cordis overlay YAML）

DSH 不使用 JSON 配置，而是 Cordis overlay YAML + `@deepseek-ai/dsh-mcp-client`
插件。写入 `$DSH_HOME/profiles/<name>/cordis.patch.yml`（仅该 profile）或
`$DSH_HOME/cordis.patch.yml`（全机生效），然后重启/重载：

```yaml
- insert:
    - id: openvsp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: openvsp
        transport: stdio
        command: '<USER_HOME>\AppData\Local\Programs\Python\Python313\python.exe'
        args:
          - '<REPO_ROOT>\mcp\openvsp_mcp_server.py'
        env:
          OPENVSP_ROOT: '<OPENVSP_ROOT>'
          OPENVSP_PYTHON: '<USER_HOME>\AppData\Local\Programs\Python\Python313\python.exe'
          OPENVSP_PYTHONPATH: '<OPENVSP_ROOT>\python\openvsp;<OPENVSP_ROOT>\python\utilities;<OPENVSP_ROOT>\python\degen_geom;<OPENVSP_ROOT>\python\openvsp\openvsp'
          OPENVSP_MCP_WORKDIR: '<REPO_ROOT>\mcp\workspace'
        cwd: '<REPO_ROOT>\mcp'
```

> 注意：DSH stdio 桥接器启动子进程前会移除 `DSH_*` 与常见凭据类环境变量；
> 本服务器不需要密钥，只需上表中的 `OPENVSP_*` 变量。
> 若 DSH 的 MCP 客户端实现强制 modern（2026-07-28）协议，则当前 legacy
> 服务器会拒绝/降级并在客户端侧失败，见第 3 节。

### 5.4 一键安装器（`install_mcp.py`，推荐）

上面的手工配置都可交给安装器自动完成。它自动探测 **server / python / openvsp_root**，
把 MCP 配置安全合并进五类 Agent 的用户级配置（JSON / TOML / Cordis YAML），
修改前备份 `*.bak`、幂等、不写任何 API Key：

```bat
cd /d <REPO_ROOT>\mcp

:: 0) 打印带真实路径的安装命令与配置片段（含“交给 Agent 自动安装”命令）
python install_mcp.py --print-command --target all

:: 1) 预览（默认模式，不落盘）
python install_mcp.py --dry-run --target all

:: 2) 实际安装（可重复；只写用户级 MCP 配置）
python install_mcp.py --install --target all

:: 3) 验证（server 可运行 + 五类配置是否已包含 openvsp）
python install_mcp.py --verify --target all
```

| Agent | 安装位置（用户级） | 安装器行为 |
|---|---|---|
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.openvsp]` | TOML 安全合并，保留 `[model]` 等其他段 |
| Claude Code | `~/.claude.json` → `mcpServers.openvsp`（优先 `claude mcp add --transport stdio --scope user`） | 有 CLI 用 CLI，无 CLI 合并 JSON |
| Kimi Code CLI | `~/.kimi/mcp.json` → `mcpServers.openvsp` | JSON 安全合并（支持 `KIMI_SHARE_DIR`） |
| ZCode | `~/.zcode/cli/config.json` → `mcp.servers.openvsp` | JSON 安全合并（另有工作区级 `.zcode/config.json` 可选） |
| DSH | `$DSH_HOME/cordis.patch.yml`（默认 `~/.dsh`）→ `@deepseek-ai/dsh-mcp-client` insert | 仅向列表型 patch 追加；另生成独立 patch 供 `dsh web --patch` |

`--target` 可重复指定（`codex`/`claude`/`kimi`/`zcode`/`dsh`/`all`）；
`--json` 输出机器可读结果，便于 Agent 自动安装/汇报。
完整教程（含 Agent 提示词、依据、FAQ、安全说明）：[MCP-安装教程.md](./MCP-安装教程.md)。

> 官方依据：Codex <https://github.com/openai/codex/blob/main/docs/config.md>；
> Claude Code <https://code.claude.com/docs/en/mcp>；
> Kimi <https://moonshotai.github.io/kimi-cli/en/customization/mcp.html>；
> ZCode <https://zcode.z.ai/cn/docs/mcp-services>；
> DSH <https://deepseek-harness.github.io/deepseek-harness/guide/mcp-memory>。
> 各 Agent 版本迭代较快，路径/字段以官方文档与 `--print-command` 输出为准。

---

## 6. 工具一览

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `openvsp_status` | 环境自检：版本、安装目录、Python API/VSPAERO 可用性、当前模型、协议版本 | 无 |
| `openvsp_new_model` | 清空并新建空模型 | `name` |
| `openvsp_build_aircraft` | 按参数生成常规布局飞机（机翼 + 平尾 + 可选垂尾 + POD 机身 + 可选螺旋桨） | `spec{name,S_ref,AR,taper,sweep,dihedral,cant,v_tail,Sh,Vv,engine,fuselage_len,...}` |
| `openvsp_get_geometry` | 返回组件列表/类型/包围盒/关键参数（展长、面积、MAC、根梢弦、后掠、上反、机身长、桨径） | `path?`、`include_parms?` |
| `openvsp_run_vspaero` | 运行 VSPAERO：`polar` 极曲线 / `stability` 稳定导数+SM/Xnp+模态估计 / `beta` 侧滑扫描 | `case,alpha,beta,mach,vinf,rho,refs,solver,ncpu,wake_iters,mass_kg,inertia,output_dir` |
| `openvsp_export` | 导出 `.vsp3` / `.stl` / `.csv`（DegenGeom 面板数据） | `format,output_path?` |
| `openvsp_generate_report` | 生成 Word 气动报告（优先 `ai_design.report`，否则本地生成） | `result?,metrics?,report_path?,title?` |
| `openvsp_list_files` | 列出输出目录文件 | `dir?,recursive?,pattern?,limit?` |

工具名全部使用**下划线**命名（同时满足 MCP 与 OpenAI/DeepSeek 函数名约束）。

### 6.1 最小调用序列（JSON-RPC，legacy）

```jsonc
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"demo","version":"0.1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"openvsp_status","arguments":{}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"openvsp_build_aircraft","arguments":{"spec":{"name":"uav50","S_ref":1.2,"AR":8,"taper":0.7,"sweep":2,"dihedral":3,"v_tail":true,"Sh":0.18,"Vv":0.05,"engine":"electric","fuselage_len":1.4}}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"openvsp_run_vspaero","arguments":{"case":"polar","alpha":[0,8,5],"mach":0.05,"vinf":20,"rho":1.225}}}
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"openvsp_export","arguments":{"format":"stl"}}}
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"openvsp_generate_report","arguments":{"title":"UAV 气动分析报告"}}}
```

工具结果统一为 `content[0].text` 内的 JSON 文本（`ok:true` 或
`error:{code,message,...}`）；VSPAERO 结果包含 `rows`（每个迎角的 CL/CD/L-D/Cm/CS）、
`summary`（CLmax/LDmax/CLα/CD0/Oswald e/SM/Xnp 估计）、`stability`（.stab 解析出的
导数与模态估计）、`files`（.polar/.stab/.history/.lod/.vspgeom 等路径）、`warnings`。

### 6.2 VSPAERO 输出文件

| 文件 | 内容 |
|---|---|
| `*.polar` | 各工况力/力矩系数（CL/CD/L-D/CMx/CMytot/CMz ...） |
| `*.stab` | 稳定性导数（`case=stability`，由 `vspaero.exe -stab` 生成） |
| `*.lod` | 展向载荷分布 |
| `*.history` | 尾迹迭代收敛历史 |
| `*.vspgeom` / `*.vkey` | VSPAERO 几何/关键表文件 |
| `*.adb` | vspviewer 可打开的求解数据库 |

---

## 7. 实现要点

1. **stdout 保护**：启动时 `os.dup(1)` 保留协议写句柄，`dup2(2,1)` 将 C++ 输出
   全部导向 stderr；`os.dup(0)` 保留读句柄，`dup2(devnull,0)` 防止子进程抢读。
   测试会断言 `stdout` 无非 JSON 行。
2. **全薄面 VLM 默认**：几何构建时把机翼/平尾/垂尾加入 geometry set 3（薄面），
   POD 机身/螺旋桨加入 set 4（厚体）；`openvsp_run_vspaero` 默认 `solver=vlm`
   只计算薄面（`ThinGeomSet=3, GeomSet=none`），避免“厚体+薄面”数值发散；
   `solver=panel` 可切换为薄面+厚体混合面板。机身/螺旋桨仅用于展示与导出。
3. **worker 子进程**：VSPAERO 计算默认在 worker 子进程中执行（同一文件
   `--worker` 模式），超时可控、求解器崩溃不拖垮协议循环；若服务器自身解释器
   无法 import openvsp，全部工具自动回退到 `OPENVSP_PYTHON` worker（文件态持久）。
4. **参考量自动推导**：从机翼 `TotalArea/TotalSpan/MAC` 计算
   `S_ref/b_ref/c_ref`，`xcg` 默认取近似 MAC 四分之一弦点；可在 `refs` 中覆盖。
5. **报告**：优先 `../ai` 的 `ai_design.report`（按签名自适应调用
   `generate_report/build_report/create_report/write_report`）；不可用时用
   `python-docx` 本地生成，未安装 python-docx 时回退到内置最小 .docx 写入器。
6. **状态**：单会话内存态（模型/设计参数/参考量/上次分析结果）；worker 与
   主进程通过 vsp3 文件 + state JSON 同步。

---

## 8. 故障排除

| 现象 | 处理 |
|---|---|
| `openvsp_status` 显示 `openvsp.available=false` | 检查 `OPENVSP_ROOT`；确认 `OPENVSP_PYTHON`/当前 Python 装有 numpy 且能 `import openvsp`；必要时用 `python openvsp_mcp_server.py --status` 直接诊断 |
| 客户端看不到工具 | 客户端必须走 legacy `initialize` 握手；确认 stderr 日志已出现 `initialize: ...`；modern 客户端会收到明确不支持提示 |
| 工具返回 `vsp_unavailable` | 同上；或设置 `OPENVSP_MCP_FORCE_SUBPROCESS=1` 并让 `OPENVSP_PYTHON` 指向可用的 OpenVSP Python |
| VSPAERO 失败/无 .polar | 用 `openvsp_run_vspaero` 的 `warnings` 排查参考量/几何；先用 `openvsp_get_geometry` 确认模型非空；必要时 `solver=vlm` + 小迎角范围 |
| VSPAERO 超时 | 增大 `OPENVSP_MCP_WORKER_TIMEOUT` / 减少迎角点数或 `wake_iters` |
| 报告无 VSPAERO 数据 | 先生成 `openvsp_run_vspaero`，或在 `openvsp_generate_report` 的 `result` 参数传入上次结果 |
| 中文乱码 | 所有协议均为 UTF-8；Windows 控制台下 stderr 日志编码取决于控制台代码页，不影响 MCP 客户端 |

直接诊断命令：

```bat
python openvsp_mcp_server.py --version
python openvsp_mcp_server.py --status
python openvsp_mcp_server.py --self-test
```

---

## 9. 测试

```bat
cd <REPO_ROOT>\mcp

:: MCP 服务器端到端测试（unittest 风格，可单独运行）
python tests\test_stdio.py          :: 快速套件：initialize/tools/status/build/geometry/export/错误码/shutdown
python tests\test_stdio.py --full   :: 追加 VSPAERO polar/stability/beta、报告、worker 模式
python tests\test_stdio.py -v       :: unittest 详细输出

:: 全部测试（pytest，含安装器测试）
python -m pytest tests -q
python -m pytest tests/test_install_mcp.py -q   :: 仅安装器：dry-run/合并/幂等/备份/不破坏原配置
```

测试通过 subprocess 真实启动服务器（UTF-8 管道），逐条断言 JSON-RPC 响应、
stdout 纯净性（无非 JSON 行）以及 OpenVSP API 可用性；安装器测试在临时 HOME 下
验证 JSON/TOML/YAML 合并、`*.bak` 备份、幂等与非法原文件保护，不影响本机配置。
服务器测试工作目录默认 `tests\_workdir_<pid>`（可用 `OPENVSP_MCP_WORKDIR` 覆盖，
`OPENVSP_MCP_KEEP_TEST_FILES=1` 保留产物）。最近一次测试结果见 `STATUS.md`。
