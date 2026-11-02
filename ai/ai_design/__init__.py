"""AI 设计后端核心包（OpenVSP + VSPAERO 智能体）。

模块划分：
    config          配置加载与校验、LLM 服务商预设
    providers       OpenAI 兼容 HTTP 客户端（仅标准库 urllib）
    logging_util    [status]/[tool]/[plan]/[ga]/[error] 逐行日志
    vsp_bridge      OpenVSP Python API 封装（惰性导入）
    designer        需求解析 → 参数 sizing → 几何 → VSPAERO → 指标 → 优化（含 offline 确定性流程）
    tools           OpenAI tool schema 与执行器
    agent           tool-calling 智能体循环（无 key 自动 offline / task=ga 分派）
    report          python-docx 中文设计报告生成（含 GA 收敛表/图）
    attachments     上传文档（Word/PDF/Excel/文本）解析与设计输入融合
    ga_optimizer    遗传算法优化任务（真实 OpenVSP/VSPAERO 评估，支持 case_model 基线）

为方便 ``import ai_design; ai_design.report.generate_report(...)`` 形式的调用，
包属性按需惰性加载子模块（``ai_design.report`` / ``ai_design.designer`` 等）。
"""

from __future__ import annotations

import importlib

__version__ = "1.3.0"

_SUBMODULES = ("config", "providers", "logging_util", "vsp_bridge", "designer",
               "tools", "agent", "report", "attachments", "ga_optimizer")


def __getattr__(name: str):
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["__version__", *_SUBMODULES]
