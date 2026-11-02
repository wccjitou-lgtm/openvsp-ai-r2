# OpenVSP R2 MCP 服务器使用说明

## 1. 是什么

`mcp/openvsp_mcp_server.py` 将 OpenVSP 的建模、VSPAERO 计算、导出、报告能力封装为
**MCP（Model Context Protocol）工具**，通过 stdio 运行，可被 Claude Desktop、Cursor、
DSH（DeepSeek Harness）等支持 MCP 的客户端安装/调用。

- 传输：stdio + 换行分隔 JSON-RPC 2.0
- 协议：legacy 版 `initialize` + `notifications/initialized`（`protocolVersion=2025-11-25`）
- stdout 只输出协议 JSON；日志全部写 stderr
- 工具命名：小写 + 下划线，兼容 OpenAI/DeepSeek function calling

## 2. 环境要求

- Windows 10/11 x64
- Python 3.10+（建议 3.13）
- OpenVSP R2（含 `vspaero.exe` 与 Python API `_vsp.pyd`）
- Python 依赖：
  ```bat
  python -m pip install numpy python-docx
  ```
  （VSPAERO 调度与 vsp3 读写不依赖第三方包；报告生成使用 python-docx）

环境变量（建议在 MCP 客户端配置中设置）：

| 变量 | 说明 | 示例 |
|---|---|---|
| `OPENVSP_ROOT` | OpenVSP R2 安装目录（便携包含 `vspaero.exe`、`python\openvsp`） | `<REPO_ROOT>\dist\OpenVSP-3.51.3-zh-CN-r2` |
| `OPENVSP_PYTHON` | Python 解释器（可选，默认当前解释器） | `C:\Python313\python.exe` |
| `OPENVSP_PYTHONPATH` | OpenVSP Python API 目录（可选，默认自动探测） | `<root>\python` |

> R2 便携包已包含 `<root>\python\openvsp`（OpenVSP Python API，cp313）；服务器会自动搜索
> `OPENVSP_PYTHONPATH`、`<OPENVSP_ROOT>\python`、`<OPENVSP_ROOT>\python`。

## 3. 启动

```bat
set OPENVSP_ROOT=<REPO_ROOT>\dist\OpenVSP-3.51.3-zh-CN-r2
python <REPO_ROOT>\mcp\openvsp_mcp_server.py
```

正常启动后向 stderr 打印版本；stdin 等待 JSON-RPC。

## 4. 客户端配置示例

### 4.1 Claude Desktop（`%APPDATA%\Claude\claude_desktop_config.json`）

```json
{
  "mcpServers": {
    "openvsp-r2": {
      "command": "python",
      "args": ["D:\\Zcoding\\OpenVSP-R2\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "D:\\Zcoding\\OpenVSP-R2\\dist\\OpenVSP-3.51.3-zh-CN-r2"
      }
    }
  }
}
```

### 4.2 Cursor（项目 `.cursor/mcp.json` 或 `~/.cursor/mcp.json`）

```json
{
  "mcpServers": {
    "openvsp-r2": {
      "type": "stdio",
      "command": "python",
      "args": ["D:\\Zcoding\\OpenVSP-R2\\mcp\\openvsp_mcp_server.py"],
      "env": {
        "OPENVSP_ROOT": "D:\\Zcoding\\OpenVSP-R2\\dist\\OpenVSP-3.51.3-zh-CN-r2"
      }
    }
  }
}
```

### 4.3 DSH（DeepSeek Harness，Cordis overlay）

DSH 通过 `@deepseek-ai/dsh-mcp-client` 插件接入 stdio MCP，在 overlay YAML 中：

```yaml
plugins:
  '@deepseek-ai/dsh-mcp-client':
    servers:
      openvsp-r2:
        transport: stdio
        command: python
        args:
          - '<REPO_ROOT>\mcp\openvsp_mcp_server.py'
        env:
          OPENVSP_ROOT: '<REPO_ROOT>\dist\OpenVSP-3.51.3-zh-CN-r2'
```

> 具体字段以你安装的 DSH 版本文档为准；核心是 `command` + `args` + `env`。

## 5. 工具清单

| 工具 | 权限 | 说明 |
|---|---|---|
| `openvsp_status` | read | 版本、安装目录、Python API/VSPAERO 可用性 |
| `openvsp_new_model` | write | 新建空模型（name 可选） |
| `openvsp_build_aircraft` | write | 按 spec 生成机翼/机身/V 尾或常规尾/操纵面 |
| `openvsp_get_geometry` | read | 列出部件、类型、关键参数 |
| `openvsp_run_vspaero` | run | polar / stability / beta 计算，返回解析结果 |
| `openvsp_export` | write | 导出 vsp3 / STL / CSV |
| `openvsp_generate_report` | write | 按内置模板生成 Word 设计报告 |
| `openvsp_list_files` | read | 列出工作目录文件 |

### 5.1 `openvsp_build_aircraft` 参数示例

```json
{
  "name": "R2_Demo_UAV",
  "spec": {
    "name": "R2_Demo_UAV",
    "S_ref": 4.5,
    "AR": 13.5,
    "taper": 0.55,
    "sweep_deg": 0.0,
    "dihedral_deg": 3.0,
    "v_tail": true,
    "v_cant_deg": 45.0,
    "tail_area": 0.9,
    "fuselage_length": 6.5,
    "fuselage_diameter": 0.5,
    "engine": "EJ115"
  }
}
```

### 5.2 `openvsp_run_vspaero` 参数示例

```json
{
  "case": "polar",
  "alpha_start": -4,
  "alpha_end": 12,
  "alpha_npts": 9,
  "mach": 0.3,
  "vinf": 100.0,
  "rho": 0.7364,
  "sref": 4.5,
  "bref": 7.794,
  "cref": 0.5936
}
```

## 6. 安全与限制

- MCP 客户端配置中的 `env` 为明文；不要把 API Key 写进 MCP 配置（MCP 工具本身不需要大模型 Key）。
- 工具只允许写入指定工作目录；不提供任意 shell 执行。
- VSPAERO 为长任务，当前为同步调用；大模型/客户端需设置足够超时。
- 复杂厚体/薄面/操纵面切口几何可能出现数值不稳定，服务器会自动降级到全薄面 VLM 并在返回值中说明。
- legacy 握手（2025-11-25）优先；modern（2026-07-28，无 `initialize`）双栈支持列入后续版本。

## 7. 快速自测

```bat
cd <REPO_ROOT>\mcp
python tests\test_stdio.py
```

测试会依次执行 `initialize → tools/list → tools/call openvsp_status → tools/call openvsp_build_aircraft` 并断言响应。
