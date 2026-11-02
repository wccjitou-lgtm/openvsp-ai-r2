# GitHub 开源发布说明

**仓库：** https://github.com/wccjitou-lgtm/openvsp-ai-r2  
**可见性：** Public  
**默认分支：** main  
**首发提交：** e83c3b792c07045ad7a31009568548cc34378621  
**日期：** 2026-11-02

## 已发布内容

- `ai/`：AI 设计后端、可靠性契约、需求解析、静稳定多轮优化、报告生成和测试；
- `mcp/`：MCP stdio 服务器和安装脚本；
- `patches/`：OpenVSP GUI R2 补丁及源码变更；
- `docs/`：研究、架构、使用、构建、可靠性 M0/M1/M2 文档；
- `validation/`：轻量四布局结果和证据；
- `LICENSE` / `LICENSE-AI` / `NOTICE.md` / `README.md`。

## 未发布内容

- OpenVSP 二进制和完整源码（需从官方获取 3.51.3 源码构建）；
- API Key、DPAPI 配置、用户附件和本机运行输出；
- Profili 商业数据库及缓存；
- 大型审查日志和临时 run 目录；
- 便携版 zip（当前保留在本地 `dist/`，未上传 GitHub Release）。

## 开源许可

- OpenVSP 及补丁：NASA NOSA 1.3；
- 本项目原创 AI/MCP/文档/脚本：MIT；
- 详见仓库 `NOTICE.md`。

## 更新方式

GitHub 仓库通过 Git Data/Contents API 初始化；后续更新可在网络允许时使用普通 git push，或继续通过 GitHub API 上传变更。
