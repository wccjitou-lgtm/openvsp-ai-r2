# STATUS — OpenVSP R2 MCP 服务器（task-2 + task-6）

- 任务：`task-2` OpenVSP MCP 服务器（stdio JSON-RPC + 工具封装 + 测试）；
  `task-6` MCP 一键安装器与五类 Agent 安装教程（Codex / Claude Code / ZCode / Kimi / DSH）
- 写域：仅 `<REPO_ROOT>\mcp\**`（未改动其他目录）
- 状态：**已完成**（交付物齐全、测试通过、README/STATUS/安装教程已更新）
- 完成时间：2026-11-01（Asia/Shanghai）
- 服务器版本：`openvsp-mcp 0.1.0`，入口 `openvsp-mcp = openvsp_mcp_server:main`
- 安装器版本：`openvsp-mcp-install 0.1.0`（`install_mcp.py`，stdlib）
- 协议版本：**legacy MCP `2025-11-25`**（兼容 `2025-06-18` / `2024-11-05`）

---

## 1. 交付物

| 文件 | 大小 | 内容 |
|---|---|---|
| `openvsp_mcp_server.py` | 135,074 B / 2,899 行 | MCP stdio 服务器：协议层、8 个工具、VSPAERO/VSP 封装、worker 子进程、报告生成 |
| `install_mcp.py` | 53,259 B / 1,093 行 | MCP 一键安装器（stdlib）：自动探测 server/python/openvsp_root，五类 Agent 配置安全合并，JSON/TOML/YAML，备份+幂等，`--print-command/--dry-run/--install/--verify/--json` |
| `MCP-安装教程.md` | 18,028 B / 264 行 | 五类 Agent 安装教程（供 R2 AI 面板“MCP调用”按钮展示）：Agent 提示词、配置路径/命令、标准 JSON、验证、安全与 FAQ |
| `pyproject.toml` | 1,045 B | `name=openvsp-mcp`、`requires-python>=3.10`、`openvsp-mcp=openvsp_mcp_server:main`、**零第三方依赖**（`dependencies=[]`） |
| `README.md` | 19,608 B | 协议说明、环境变量、Claude Desktop / Cursor / DSH 配置、**一键安装器（§5.4）**、工具表、故障排除 |
| `tests/test_stdio.py` | 27,160 B | subprocess 端到端测试，21 个用例（`--full` 含 VSPAERO/报告/worker） |
| `tests/test_install_mcp.py` | 19,424 B | 安装器测试（pytest）14 个用例：dry-run/print-command/临时 HOME 合并 JSON+TOML+YAML/幂等/备份/非法原文件保护/不写凭据 |
| `STATUS.md` | 本文件 | 工具表、安装器、测试结果、协议与限制 |

运行期目录 `workspace/` 由服务器自动创建（`OPENVSP_MCP_WORKDIR` 可覆盖）：
`models/`（.vsp3）、`vspaero/`（.polar/.stab/...）、`exports/`（vsp3/stl/csv）、`reports/`（.docx）、`tmp/`（worker 临时文件）。

---

## 2. 工具表

| 工具 | 输入（要点） | 返回（要点） | 测试覆盖 |
|---|---|---|---|
| `openvsp_status` | 无 | 服务器/协议版本、OpenVSP 版本与安装目录、Python API/VSPAERO 可用性、Python/环境变量、AI 后端、当前模型 | `test_04_status` |
| `openvsp_new_model` | `name` | 清空后的模型名 | `test_05_new_model` |
| `openvsp_build_aircraft` | `spec{name,S_ref,AR,taper,sweep,dihedral,cant,v_tail,Sh,Vv,engine,fuselage_len,fuselage_fineness,include_fuselage,h_tail_AR,v_tail_AR,tail_taper,tail_sweep,x_wing,tail_x}` | 组件列表（Wing/HTail/VTail/Fuselage/Propeller）、派生尺寸（翼展/根梢弦/MAC/机身长/尾容量）、参考量 `S_ref/b_ref/c_ref/xcg`、.vsp3 路径、warnings | `test_06_build_aircraft` |
| `openvsp_get_geometry` | `path?`（可直接读 .vsp3）、`include_parms?` | 每组件 name/type/role/bbox/关键参数（展长、面积、AR、MAC、根梢弦、后掠/上反/扭转、机身长度/长细比、桨径）、整体包围盒、参考量 | `test_07_get_geometry`, `test_051_no_model_error` |
| `openvsp_run_vspaero` | `case{polar,stability,beta}`、`alpha/beta`（数字或 [start,end] / [start,end,npts]）、`mach,vinf,rho`、`refs{S_ref,b_ref,c_ref,xcg,ycg,zcg,re_ref}`、`solver{vlm,panel}`、`ncpu,wake_iters,output_dir,mass_kg,inertia` | `rows`（每工况 CL/CD/L-D/Cm/CS/...）、`summary`（CLmax、LDmax、CLα、CD0、Oswald e、SM/Xnp 回归估计）、`stability`（.stab 导数、SM/Xnp、模态估计）、`flow/reference/sets`、`files`（polar/stab/history/lod/vspgeom/vspaero/adb 路径）、`warnings`、`duration_s` | `test_20_vspaero_polar`、`test_21_vspaero_stability`、`test_24_vspaero_beta` |
| `openvsp_export` | `format{vsp3,stl,csv}`、`output_path?` | 输出文件路径、字节数 | `test_08_export`（3 种格式） |
| `openvsp_generate_report` | `result?`（上次 VSPAERO 结果）、`metrics?`、`report_path?`、`title?` | .docx 路径、生成引擎（`ai_design` / `python-docx` / `builtin-docx`）、章节、字节数 | `test_22_generate_report` |
| `openvsp_list_files` | `dir?`、`recursive?`、`pattern?`、`limit?` | 文件/目录列表（大小、修改时间） | `test_09_list_files` |

工具名全部为下划线命名（同时满足 MCP 与 OpenAI/DeepSeek 函数名约束）。
JSON-RPC 协议错误（未知工具/参数）返回 `error`；工具执行失败返回
`result.isError=true` + JSON 文本（`error.code/error.message`）。

---

## 3. 协议实现与双代（dual-era）兼容

### 3.1 legacy（默认实现）

- 传输：stdio，**换行分隔 JSON-RPC 2.0**（UTF-8，无 BOM；另容错解析 LSP 风格
  `Content-Length` 帧），`stdout` 只写协议 JSON。
- 生命周期：`initialize` → `notifications/initialized` → `tools/list` / `tools/call`；
  额外实现 `ping`、`shutdown`、`notifications/cancelled`（忽略并记录）、`exit`。
- `initialize` 响应：`protocolVersion` = 客户端请求版本（若在
  `2025-11-25 / 2025-06-18 / 2024-11-05` 内），否则返回 **`2025-11-25`**；
  `capabilities = {"tools": {"listChanged": false}}`；
  `serverInfo = {name:"openvsp-mcp", title:"OpenVSP R2 MCP Server", version:"0.1.0"}`；
  附 `instructions`。
- 错误码：`-32700` parse error、`-32600` invalid request、`-32601` method not found、
  `-32602` invalid params、`-32603` internal error。
- stdout 防污染：启动时 `os.dup(1)` 保留协议写句柄 → `dup2(2,1)` 把所有
  OpenVSP C++/VSPAERO `printf` 导向 stderr；`os.dup(0)` 保留读句柄 →
  `dup2(devnull,0)` 防止子进程抢读客户端输入。测试断言 stdout 无非 JSON 行。

### 3.2 modern（2026-07-28）兼容策略

**不做 modern 完整实现**，仅做检测 + 明确降级/错误提示（dual-era 安全边界）：

| 客户端行为 | 服务器响应 |
|---|---|
| `initialize` 带 `protocolVersion:"2026-07-28"` | 仍返回 legacy 结果（`protocolVersion:"2025-11-25"`），`result._meta` 中给出 `openvsp-mcp/requestedProtocolVersion`、协商版本与“modern 未实现”说明 |
| 未握手即带 `_meta.io.modelcontextprotocol/protocolVersion="2026-*"` 的请求 | JSON-RPC `-32600`，`error.data` 说明仅支持 legacy 握手 |
| `server/discover` | JSON-RPC `-32601`，`error.data` 含 `protocolEra:"legacy"`、`supportedProtocolVersions`、`modernProtocolVersion:"2026-07-28"`、`hint` |
| `initialize` 带 `2025-06-18` / `2024-11-05` | 原样回显该版本（legacy 兼容） |

结论：Claude Desktop / Cursor / 现有 DSH legacy 客户端可正常握手；强制 modern
（2026-07-28）且不支持降级的客户端会建立会话失败，这是已知限制。

---

## 4. 环境（实测）

| 项 | 值 |
|---|---|
| OS | Windows（`pwsh`） |
| Python | `<USER_HOME>\AppData\Local\Programs\Python\Python313\python.exe`（CPython 3.13.3） |
| OpenVSP | `<OPENVSP_ROOT>`，`OpenVSP 3.51.3`，`vsp.exe`/`vspaero.exe` 存在 |
| VSPAERO | v7.2.2（`<root>\python\openvsp\openvsp\vspaero.exe`，`GetVSPAEROPath()` 可用） |
| Python API | `<root>\python\openvsp`（`import openvsp` 实测可用；numpy 2.4.4） |
| 报告依赖 | `python-docx` 已安装（AI 报告 `ai_design.report` 已可导入） |
| AI 后端 | `<REPO_ROOT>\ai\ai_design` 1.0.0，子模块 `report` / `vsp_bridge` / `designer` 可导入 |

服务器无需设置 `PYTHONPATH`：按 `OPENVSP_ROOT` 自动推导
`python\openvsp`、`python\utilities`、`python\degen_geom`、`python\openvsp_config`、
`python\openvsp\openvsp`（顺序已修正 circular import 问题）并调用
`os.add_dll_directory(OPENVSP_ROOT)`。

---

## 5. 测试结果

测试方式：`tests/test_stdio.py` 用 `subprocess.Popen` 以 UTF-8 管道真实启动服务器，
发送 newline-delimited JSON-RPC 并逐条断言响应/协议纯净性。

```bat
cd <REPO_ROOT>\mcp
python tests\test_stdio.py          :: 快速套件
python tests\test_stdio.py --full   :: 追加 VSPAERO / 报告 / worker 模式
python -m pytest tests -q           :: 全部测试（stdio + install_mcp）
```

### 5.1 快速套件（默认，最近一次）

```
Ran 21 tests in 1.99s
OK (skipped=5)
```

- 21 个用例全部 OK；5 个 `--full` 专属用例按设计跳过。
- 覆盖：initialize / notifications 静默 / ping / tools/list（8 工具、schema）/
  status / new_model / 空模型错误码 / build_aircraft（50kg 级：S_ref=1.2、AR=8、
  taper=0.7、sweep=2、dihedral=3、Sh=0.18、Vv=0.05、fuselage_len=1.4）/
  get_geometry / export(vsp3+stl+csv) / list_files / 协议错误码
  （-32602、-32601、-32700）/ legacy-modern 双代降级 / shutdown 后进程退出。
- Lead 侧独立自测亦通过：`19 项 OK，4 项 full 跳过`（当时快速套件用例数）。

### 5.2 完整套件（`--full`，最近一次）

```
test_01_initialize ... ok            test_14_protocol_era ... ok
test_02_ping_and_notifications ... ok  test_20_vspaero_polar ... ok
test_03_tools_list ... ok            test_21_vspaero_stability ... ok
test_04_status ... ok                test_22_generate_report ... ok
test_051_no_model_error ... ok       test_23_worker_subprocess_mode ... ok
test_05_new_model ... ok             test_24_vspaero_beta ... ok
test_06_build_aircraft ... ok        test_99_shutdown ... ok
test_07_get_geometry ... ok
test_08_export ... ok                Ran 21 tests in 20.9s
test_09_list_files ... ok            OK
test_10_unknown_tool ... ok
test_11_invalid_params ... ok
test_12_unknown_method ... ok
test_13_parse_error ... ok
```

**21/21 通过，0 跳过，约 21 秒（不同机器负载下 20–33 s）。** 其中：

| 用例 | 说明 |
|---|---|
| `test_20_vspaero_polar` | 真实调用 VSPAERO（alpha 0/2/4/5/6/8、Mach 0.05、Vinf 20 m/s），断言 rows、CLmax>0.2、LDmax>5、.polar 产出；经 worker 子进程路径 |
| `test_21_vspaero_stability` | `-stab` 稳定导数：断言 CLα、Cmα、phugoid 模态、.stab 文件；经 worker 子进程路径 |
| `test_22_generate_report` | 生成 .docx（engine ∈ `ai_design`/`python-docx`/`builtin-docx`），zip 容器校验 |
| `test_23_worker_subprocess_mode` | `OPENVSP_MCP_FORCE_SUBPROCESS=1`：status/build/get_geometry 全走 worker |
| `test_24_vspaero_beta` | beta 扫描 [-2,0,2]，断言 `CY_beta`/`Cn_beta` |
| `test_14_protocol_era` | 2026-07-28 initialize 降级、`server/discover` -32601、modern `_meta` -32600 |
| `test_99_shutdown` | shutdown 后进程 10s 内退出且退出码 0 |
| 快速套件跳过项 | `test_20/21/22/23/24` 为 `--full` 专属（VSPAERO/报告/worker 耗时用例）；普通开发用默认套件快速回归 |

### 5.3 实测样例（`--full`/手动验证，2 m² 级 UAV：S_ref=2.0、AR=8、taper=0.65、sweep=3°、dihedral=4°、fuselage_len=2.0、Vinf=20 m/s、Mach=0.05）

- polar：alpha 2°/4°/6°/8° → CL 0.186/0.372/0.557/0.742，CD 0.0107/0.0154/0.0232/0.0342，
  L/D 17.4/24.1/24.0/21.7；`LD_max=24.12 @ alpha=4°`，`CLα≈5.31 /rad`，`CD0≈0.00913`，
  `Oswald e≈0.875`。
- stability（CG≈0.711 m）：`CLα=5.34 /rad`，`Cmα=-0.802 /rad`，`Cnβ=+0.136 /rad`，
  `SM≈0.150`，`X_np≈0.786 m`（CG 之后 → 纵向静稳定）；
  phugoid `ωn≈0.693 rad/s`；给定 mass=50 kg、Iyy=6 kg·m² 时短周期
  `ωn≈6.30 rad/s、ζ≈0.52`（近似估计，见限制 3）。
- 报告：`ai_design` 引擎生成 ~137 KB .docx（含 3 张 matplotlib 图、8 张表、封面/
  页眉页脚；`report_path` 精确落盘），`python-docx` 可正常打开。

---

## 6. MCP 一键安装器与五类 Agent 配置（task-6）

新增 `install_mcp.py`（stdlib）与 `MCP-安装教程.md`，供 R2 AI 面板“MCP调用”按钮弹出的
教程引用，并让 Agent 自动安装：R2 在教程顶部注入本机真实路径
（server/python/openvsp_root），教程中的命令也可由 `--print-command` 生成。

### 6.1 安装器能力

- **自动探测**：server = 本文件同目录 `openvsp_mcp_server.py`；python = `--python` >
  `OPENVSP_PYTHON` > `sys.executable`（可选 `import openvsp` 探测）；openvsp_root =
  `--openvsp-root` > `OPENVSP_ROOT` > server 目录向上/邻近查找 `vspaero.exe` >
  常见默认安装路径 > 盘符顶层 `OpenVSP*`。
- **目标/模式**：`--target codex|claude|kimi|zcode|dsh|all`（可重复）；
  `--print-command`（打印命令/片段 + “交给 Agent 自动安装”命令）、`--dry-run`
  （默认，不落盘）、`--install`、`--verify`；`--json` 输出机器可读结果供 Agent 解析。
- **安全合并**：JSON 解析失败 → 拒绝覆盖原文件；TOML 写前用 `tomllib` 校验并保留其他段；
  DSH YAML 仅向列表型 patch 追加，非列表文件只生成独立 patch。
- **备份/幂等**：实际修改前备份 `<文件名>.bak`；重复安装返回 `unchanged`，无重复条目、
  不重复备份（文件 hash/mtime 不变）。
- **安全**：**不写 API Key**（env 只写 `OPENVSP_ROOT` / `OPENVSP_PYTHONPATH`）。

### 6.2 五类 Agent 配置摘要（以 `--print-command` 输出为准）

| Agent | 配置文件（用户级） | 安装器行为 | 官方依据 |
|---|---|---|---|
| Codex CLI | `~/.codex/config.toml`（`CODEX_HOME` 可覆盖）→ `[mcp_servers.openvsp]` | TOML 安全合并；另给 `codex mcp add` 提示（以 `codex mcp --help` 为准） | <https://github.com/openai/codex/blob/main/docs/config.md> |
| Claude Code | 优先 CLI `claude mcp add --transport stdio --scope user ... openvsp -- <python> <server>`；无 CLI 写 `~/.claude.json` 顶层 `mcpServers` | 检测到 `claude` 时用 CLI（`--no-cli` 强制走 JSON 合并） | <https://code.claude.com/docs/en/mcp> |
| Kimi Code CLI | `~/.kimi/mcp.json`（`KIMI_SHARE_DIR` 可覆盖）→ `mcpServers.openvsp` | JSON 安全合并；另给 `kimi mcp add --transport stdio ...` 命令 | <https://moonshotai.github.io/kimi-cli/en/customization/mcp.html> |
| ZCode | `~/.zcode/cli/config.json` → `mcp.servers.openvsp`（工作区级 `<项目>/.zcode/config.json`；`~/.agents/mcp.json` 兜底） | JSON 安全合并；面板入口为“设置→MCP服务器→完整配置”粘贴标准 JSON | <https://zcode.z.ai/cn/docs/mcp-services> |
| DSH | `$DSH_HOME/cordis.patch.yml`（默认 `~/.dsh`，或 `profiles/<name>/cordis.patch.yml`）→ `@deepseek-ai/dsh-mcp-client` insert | 仅向列表型 patch 追加；另生成独立 patch 供 `dsh web --patch`；未检测到 DSH 时只生成独立文件 | <https://deepseek-harness.github.io/deepseek-harness/guide/mcp-memory> |

```bat
python install_mcp.py --print-command --target all   :: 打印真实路径命令/片段
python install_mcp.py --dry-run --target all         :: 预览（默认模式，不落盘）
python install_mcp.py --install --target all         :: 安装（备份 .bak，幂等）
python install_mcp.py --verify --target all          :: 验证 server + 五类配置
```

### 6.3 安装器测试结果（pytest，临时 HOME，不影响本机配置）

`python -m pytest tests/test_install_mcp.py -q` → **14 passed（约 2.6 s）**，覆盖：

- `--print-command`/`--json`：探测 server/python/openvsp_root、五类 target 片段与官方文档链接；
- Python `import openvsp` 探测（`OpenVSP 3.51.3`）；
- `--dry-run` 不创建任何文件；`--install` 在临时 HOME 生成 5 类配置并校验结构；
- JSON/TOML/YAML 合并保留原配置（`[model]`/`theme`/其他 MCP 服务）、`.bak` 备份、
  幂等（二次 `unchanged` 且文件 hash/mtime 不变）；
- 非法 JSON/TOML 原文件被拒绝覆盖（字节不变）；DSH 非列表 patch 不改原文件（standalone-only）；
- `CODEX_HOME`/`KIMI_SHARE_DIR`/`DSH_HOME` 覆盖生效；路径含空格；不写任何凭据；
  `--verify` 校验 server 可用 + 配置已安装。
- 全仓测试：`python -m pytest tests -q` → **30 passed, 5 skipped（约 4.4 s）**
  （5 skipped 为 stdio 的 `--full` 专属用例）。

---

## 7. ai_design（AI 后端）集成状态

- 服务器启动时按 `OPENVSP_MCP_AI_DIR`（默认 `<mcp>\..\ai`）把 `ai` 加入
  `sys.path`，尝试 `import ai_design`；`openvsp_status.ai_design` 返回
  `available/version/modules`（已实测 `1.0.0`，modules=`report/vsp_bridge/designer/tools/agent`）。
- `openvsp_generate_report` 优先调用 `ai_design.report`：按名称依次尝试
  `generate_report/build_report/create_report/write_report`，用
  `inspect.signature` 自适应关键字参数
  （`result/analysis/metrics/spec/geometry/reference/report_path/output_path/output_dir/title/data`）；
  调用失败或返回无效时回退本地生成器。**已实测走通 `engine="ai_design"`**：
  与 ai-backend 确认接口后，`generate_report(...)` 精确写入 `report_path`，返回
  路径；报告含 VSPAERO polar 表、稳定性章节、3 张 matplotlib 图（CL-α、L/D-α、
  极曲线）与文件清单。
- 适配说明：MCP 工具结果的行使用 `alpha_deg/CL/CD/...` 列名，而早期
  `ai_design.report` 图表字段为 `AoA/CLtot/CDtot/...`；服务器在传给 AI 报告前
  自动补充两组别名（`_adapt_analysis_for_ai`），无需修改 ai 后端即可出图。
  后续 ai-backend 已在 `ai_design.report` 内置同类字段兼容
  （`normalize_polar_row(s)`，含回归测试），MCP 侧别名作为旧版本兼容保留（双保险，
  两条路径均实测可出图：docx ~118–137 KB + 3 张图）。
- 若 ai 后端尚未完成/不可用：回退 `python-docx`；再不可用时回退内置最小
  .docx 写入器（zipfile 纯标准库）。因此 ai 后端未完成**不阻塞** MCP 服务器。

---

## 8. 已知限制

1. **modern MCP（2026-07-28）未实现**：仅做检测与降级提示（见 §3.2）；
   强制 modern 且不降级的客户端无法建立会话。后续如需支持，应补
   `server/discover` + 每请求 `_meta` 版本处理并保留 legacy 分支。
2. **单线程同步协议循环**：长时间 VSPAERO 调用会阻塞同一会话的其他请求；
   未发送 `notifications/progress`；`notifications/cancelled` 仅记录不中断求解
   （超时由 worker 控制）。如需要可改为线程池 + 请求取消。
3. **稳定性“模式”为近似估计**：VSPAERO 7.2.2 的 `.stab` 提供静/动导数与
   SM/Xnp，不直接给出模态；phugoid 用 `ωn=√2·g/V`、`ζ≈1/(√2·L/D)` 估计，
   短周期用 2-DoF 线性化近似（需调用方提供 `mass_kg` + `inertia.Iyy`），
   均为工程估算而非严格特征值解。
4. **默认全薄面 VLM**：`build_aircraft` 把机翼/平尾/垂尾放 thin set(3)、
   机身(POD)/螺旋桨放 thick set(4)；`openvsp_run_vspaero` 默认
   `solver=vlm`（只算薄面），避免厚体+薄面数值发散。机身/螺旋桨不参与气动，
   仅用于展示/导出/.vsp3。`solver=panel`（薄+厚）已接线但属实验性，
   未纳入回归测试。
5. **`cant` 为近似实现**：OpenVSP 3.51.3 的 WING 无独立 Cant 参数，服务器以
   整翼绕 X 轴刚性滚转近似，结果中会给出 warning。
6. **无操纵面/配平分析**：未创建 control surface，`openvsp_run_vspaero` 只做
   alpha/beta/Mach 扫描与稳定导数；`CLde/CMde` 等控制导数当前为空。
7. **仅 Windows 验证**：`os.add_dll_directory` / `vspaero.exe` / 路径分隔符按
   Windows 实现（非 Windows 已用 `hasattr` 保护，但未实测）。
8. **导出 csv 语义**：`format=csv` 为 DegenGeom 面板/分段几何 CSV（VSPAERO
   可直接使用），不是极曲线 CSV；极曲线数据请从 `openvsp_run_vspaero` 的
   `rows` 或 `.polar` 文件获取。
9. **路径/安全**：stdio 本地信任模型；`openvsp_export`/`openvsp_get_geometry`
   接受任意本地路径，未做白名单/确认（若对外暴露需在宿主层加权限与
   路径校验）。不要在 MCP 配置里写密钥（本服务器不需要密钥）。
10. **worker 状态为文件态**：`OPENVSP_MCP_FORCE_SUBPROCESS=1` 时每个工具调用
    从 `state.vsp3_path` 重新载入模型；内存中的未保存修改不会跨调用保留
    （每个 MCP 服务器进程 = 独立会话）。
11. **求解器版本字段**：返回中 `solver.version` 目前写死 `"7.2.2"`（本机实测
    版本），非从可执行文件动态探测。
12. **测试依赖本机 OpenVSP**：`tests/test_stdio.py` 会断言 VSPAERO 可用；
    在未安装 OpenVSP/无 numpy 的机器上会以失败形式暴露环境问题（这是预期）。
13. **安装器配置路径以各 Agent 官方文档/版本为准**：Codex/Claude Code/Kimi/ZCode
    迭代较快，安装器写入的是本文档版本的标准位置；若某版本改了路径，可用
    `--print-command` 输出的标准 JSON 在对应 Agent 中手工导入（教程第 5.2 节 Q5）。
14. **`codex mcp add` 语法未在本机实测**（本机未安装 Codex）：安装器以
    TOML 合并为主路径，CLI 仅作提示；`codex mcp --help` 为准。
15. **Claude Code `--install` 在检测到 `claude` CLI 时会实际调用它**（用户级、
    先查 `claude mcp list` 保证幂等）；不希望调用 CLI 时用 `--no-cli` 强制写
    `~/.claude.json`。
16. **DSH YAML 合并为保守文本追加**：仅检测是否已有 openvsp → 追加 `- insert:`
    列表项，不做通用 YAML 解析（stdlib 无 YAML 库）；非列表型文件不修改，只生成
    独立 patch 文件。
17. **安装器只写用户级配置**：Claude Code 的 `.mcp.json`、ZCode 的
    工作区级 `.zcode/config.json` 需按教程手工放置/提交仓库；安装器不会修改项目仓库。

---

## 9. 复现/验收命令

```bat
cd <REPO_ROOT>\mcp

:: 环境自检（直接输出 JSON 到 stdout）
python openvsp_mcp_server.py --status
python openvsp_mcp_server.py --self-test
python openvsp_mcp_server.py --version

:: 测试
python tests\test_stdio.py
python tests\test_stdio.py --full
python -m pytest tests -q
python -m pytest tests/test_install_mcp.py -q

:: MCP 一键安装（五类 Agent，用户级配置，自动备份；默认 dry-run）
python install_mcp.py --print-command --target all
python install_mcp.py --dry-run --target all
python install_mcp.py --install --target all
python install_mcp.py --verify --target all
```

客户端配置（Claude Desktop `%APPDATA%\Claude\claude_desktop_config.json`、
Cursor `.cursor/mcp.json`、DSH Cordis overlay YAML）见 `README.md` 第 5 节；
**五类 Agent（Codex / Claude Code / Kimi / ZCode / DSH）的一键安装见
`README.md` §5.4 与 [MCP-安装教程.md](./MCP-安装教程.md)**。它们均通过
`command` + `args` + `env(OPENVSP_ROOT/OPENVSP_PYTHONPATH/...)` 启动同一个
`openvsp_mcp_server.py`，无需安装即可运行（可选 `pip install -e .`
获得 `openvsp-mcp` 控制台命令）。
