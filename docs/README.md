# R2 文档索引

| 编号 | 文档 | 内容 |
|---|---|---|
| 00 | [调研报告](00-调研报告.md) | OpenAI 兼容 API、四家 provider、MCP legacy/modern 协议、安全、风险与待实测清单（含官方参考链接） |
| 01 | [R2 架构设计](01-R2-架构设计.md) | 总体架构、UI/后端/MCP 组件、接口契约、关键技术决策、目录结构、已知限制 |
| 02 | [AI 设计模块使用说明](02-AI设计模块使用说明.md) | 打开方式、服务商/Key/模型列表、设计流程、输出位置、离线模式、Key 安全、FAQ |
| 03 | [MCP 服务器使用说明](03-MCP服务器使用说明.md) / [MCP 安装教程](../mcp/MCP-安装教程.md) | 环境、启动、Claude Desktop/Cursor/DSH 配置、工具清单、安全限制、自测 |
| 04 | [开发与构建说明](04-开发与构建说明.md) | 源码修改清单、构建命令、测试方法、R2 打包流程、后续开发建议 |
| 05 | [R2 发布说明](05-R2-发布说明.md) | 新功能、验证记录、已知问题、许可与修改声明 |
| 06 | [五案例设计验证报告](06-五案例设计验证报告.md) | 常规固定翼/飞翼/V 尾高速/高空长航时/串列翼五案例计算、需求符合性、常规经验对比与大模型参与验证（附 Word/PDF） |
| 07 | [报错修复与回归验证](07-报错修复与回归验证.md) | 五案例暴露的 4 类真实缺陷的根因、修复与回归验证（飞翼 Sref、参数修改覆盖、飞翼重心限位、指标一致性） |
| 09 | [Profili 翼型库与 AI 自动选型](09-Profili翼型库与AI自动选型.md) | 本机 Profili 翼型库导入、任务/Re/Ma 自动选型、OpenVSP 真实坐标导入与验收 |
| 08 | [Kimi k3 与 DeepSeek 五案例对比验证报告](08-Kimi与DeepSeek五案例对比报告.md) | 同一修复后后端、相同五案例下 Kimi k3 与 DeepSeek deepseek-flash 的几何/气动/需求符合性/耗时差异（附 Word/PDF） |
| 12 | [可靠设计软件改造开发方案](reliable_design_v1/12-可靠设计软件改造开发方案.md) | 结果可信、质量配平、稳定性操纵性、任务性能、LLM 闭环、分阶段工作包与数据契约（附 PDF） |
| 13 | [可靠设计软件验收测试规范](reliable_design_v1/13-可靠设计软件验收测试规范.md) | 38 项需求、62 个待执行用例、三布局回归、独立基准、验收门禁与签署（附 PDF 和追踪矩阵） |

## 界面截图

- [中文菜单栏 AI 入口](images/openvsp-ai-menu.png)
- [AI 飞行器设计助手面板（含上传文档）](images/ai-design-dialog.png)
- [上传文档原生文件选择框](images/native-file-chooser.png)
- [上传 Word/PDF/Excel/TXT 后解析并完成设计](images/ai-attachments-success.png)
- [遗传算法优化完成](images/ai-ga-run.png)
- [输入模型：GA 优化模型导入 OpenVSP 编辑](images/ga-model-imported.png)
- [模型列表获取失败自动回退内置清单](images/model-list-fallback.png)

## 相关目录

- `../ai/`：AI 设计后端（README、STATUS、tests）
- `../mcp/`：MCP 服务器（README、STATUS、tests）
- `../ui-tests/`：AI 面板自动化测试与截图
- `../patches/`：R2 源码变更
