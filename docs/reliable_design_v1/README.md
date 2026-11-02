# 可靠设计软件开发与验收文档集

文档集：OVSP-RD-20261102，版本 1.0，2026 年 11 月 2 日，待评审。

本目录面向软件改造，不是三种飞机的定型设计书。目标是在批准适用域内，使软件忠实执行输入、计算与证据一致，并能够拒绝不可行或未验证方案。

## 主要文档

| 文件 | 用途 |
|---|---|
| 12-可靠设计软件改造开发方案.md / .pdf | 目标、模块、数据契约、计算链、工作包、里程碑和风险 |
| 14-OpenVSP-R2-软件优化方案.md / .docx / .pdf | 对两份文档的审查结论、当前差距、M0–M4 优化路线、工作包和验收映射 |
| 13-可靠设计软件验收测试规范.md / .pdf | 测试条件、62 个待执行用例、门禁、缺陷分级和签署 |
| requirements.json | 38 项需求、阶段和 NUM_V1 的唯一编号来源 |
| traceability.csv | 需求与验收用例的对应关系，全部初始状态 NOT_RUN |
| acceptance_cases.json | 从验收规范提取的用例索引，不是测试执行结果 |
| acceptance_record_template.json | 将来单次验收的空白受控记录格式 |

## 使用顺序

1. 评审开发方案的范围、状态定义和工作包。
2. 冻结 M0：源码与数据基线、需求确认、坐标、数值容差、工程及验证责任人。
3. 审查验收用例与追踪矩阵，先补足独立金样、ENG_V1 和 VAL_V1。
4. 按 M1 至 M4 开发，每项需求以实际执行记录和证据关闭。

所有新验收用例当前为 NOT_RUN。历史审查的 125 项测试不是本次改造的验收通过记录。数值容差不是适航限值，完整工程能力发布必须取得专业阈值、独立基准和责任人批准。

## 文档构建与检查

`tools/build_documents.py` 只构建和核对本目录文档，不执行软件验收，不修改应用源码。它使用 Codex 捆绑 Python、Node、marked、ReportLab、中文字体和 Poppler，不新增应用运行时依赖。

从项目根目录运行文档检查：

```powershell
& '<USER_HOME>\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' docs\reliable_design_v1\tools\build_documents.py --check-only
```

去掉 `--check-only` 会重新生成本目录两个 PDF 和 `_qa` 内页面图。正式交付 PDF 仍须完成渲染和视觉检查。其他机器的运行时与字体路径需适配，不能把本构建脚本当作应用自身的可移植性证明。
