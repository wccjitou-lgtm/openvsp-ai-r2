# 设计报告模板说明

`design_report_template.docx` 由 `build_template.py` 程序生成（可随时重建）：

```bash
python templates/build_template.py
```

## 占位符约定

模板正文中的 `{%KEY%}` 段落是占位符。`ai_design.report.generate_report()`
会在占位符位置插入文字、表格或图片，然后删除占位符段落；未识别的
`{%...%}` 会被清空，保证最终 docx 不残留占位符。

| 占位符 | 内容 | 插入形式 |
| --- | --- | --- |
| `{%TITLE%}` | 报告标题（封面） | 文字（26pt 黑体） |
| `{%SUBTITLE%}` | 副标题 | 文字（20pt 黑体） |
| `{%META%}` | 封面信息（任务/布局/工具/日期） | 多行文字 |
| `{%REQUIREMENTS%}` | 设计任务与要求 | 段落 + 任务要求表 |
| `{%PARAMS%}` | 几何参数 | 参数表 + 部件表 |
| `{%ANALYSIS_SETUP%}` | 气动计算设置 | VSPAERO 设置表 |
| `{%RESULTS%}` | 气动结果 | 极曲线表、关键参数表、图 1~3 |
| `{%STABILITY%}` | 稳定性 | 静稳定裕度/导数表 |
| `{%OPTIMIZATION%}` | 解析优化 | 方案对比表 |
| `{%CONCLUSIONS%}` | 结论与建议 | 条目列表 |
| `{%FILES%}` | 输出文件清单 | 文件表 |
| `{%LIMITATIONS%}` | 已知限制 | 条目列表 |

此外还支持简单文本占位符：`{%DATE%}`、`{%MODEL%}`、`{%TITLE_SHORT%}`。

## 使用方式

```python
from ai_design.report import generate_report

path = generate_report(
    output_dir=r"D:\...\outputs\demo",
    template=r"<REPO_ROOT>\ai\templates\design_report_template.docx",
    title="50 kg 级电动无人机",
    metrics=metrics,      # ai_design.designer 生成的指标 dict
    spec=spec,            # DesignSpec 或 dict
    geometry=geometry,    # 几何 dict（可选）
)
```

模板缺失时 `generate_report` 会自动降级为按同样样式从头生成，不影响流程可用性。
