#!/usr/bin/env python
"""AI 无人机设计后端 CLI（OpenVSP R2）。

用法::

    python ai_design_agent.py --config examples/offline_config.json --offline
    python ai_design_agent.py --config my_llm_config.json

行为
----
- 读取 JSON 配置（provider / base_url / api_key / model / prompt / output_dir /
  template / mode / max_iterations）。
- stdout 逐行输出带前缀日志：``[status]`` / ``[tool]`` / ``[plan]`` / ``[error]``
  （UI 直接捕获 stdout 展示）。
- 结束后写 ``output_dir/result.json``::

      {"ok": bool, "report_path": str, "model_path": str,
       "metrics": {...}, "log_tail": [...], "error": str | null}

- API Key 可通过环境变量 ``OPENVSP_AI_API_KEY`` 注入（UI 子进程传入，不写配置
  文件）；环境变量优先于配置里的 ``api_key`` 字段。日志中只出现掩码。

退出码：0 = 成功（ok=true），1 = 失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parent
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

from ai_design.agent import DesignAgent  # noqa: E402
from ai_design.config import AIConfig, ConfigError, load_config  # noqa: E402
from ai_design.logging_util import Logger  # noqa: E402
from ai_design.vsp_bridge import VSPError  # noqa: E402

VERSION = "1.3.0"


def _reconfigure_stdout() -> None:
    """管道/重定向下强制 UTF-8，避免 Windows 代码页导致中文日志乱码或异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai_design_agent.py",
        description="OpenVSP R2 AI 无人机设计后端（OpenVSP + VSPAERO + 可选 LLM）",
    )
    parser.add_argument("--config", required=True, metavar="CONFIG.json",
                        help="设计任务配置文件（JSON）")
    parser.add_argument("--offline", action="store_true",
                        help="强制离线确定性流程（不调用任何网络服务）")
    parser.add_argument("--mode", choices=["auto", "offline", "llm"], default=None,
                        help="覆盖配置文件中的 mode")
    parser.add_argument("--ga", action="store_true",
                        help="等价于 task=ga：运行遗传算法优化（需 --offline 或可离线运行）")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def write_result(output_dir: Path, result: dict) -> Path:
    """把运行结果写入 output_dir/result.json（保证可被 UI 解析）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": bool(result.get("ok")),
        "report_path": result.get("report_path"),
        "model_path": result.get("model_path"),
        "metrics": result.get("metrics"),
        "log_tail": result.get("log_tail") or [],
        "error": result.get("error"),
        # 额外信息（UI 可选用）
        "mode": result.get("mode"),
        "elapsed_s": result.get("elapsed_s"),
        "exports": result.get("exports"),
        "summary": result.get("summary"),
        "tool_calls": result.get("tool_calls"),
        "iterations": result.get("iterations"),
        "degraded": result.get("degraded"),
        "provider": result.get("provider"),
        "model": result.get("model"),
        # 上传附件解析摘要（GUI「上传文档…」功能）
        "attachments": result.get("attachments") or [],
        "document_chars": result.get("document_chars") or 0,
        "attachment_count": result.get("attachment_count") or 0,
        "attachment_ok_count": result.get("attachment_ok_count") or 0,
        # 上传案例 .vsp3 基线（GUI「上传案例…」功能）
        "case_model": result.get("case_model"),
        "based_on_case": bool(result.get("based_on_case")),
        "case_parameters": result.get("case_parameters"),
        "case_applied": result.get("case_applied"),
        # GA 优化任务（task=ga）专用字段
        "task": result.get("task") or "design",
        "modify": bool(result.get("modify")),
        "parent_model": result.get("parent_model"),
        "parent_result": result.get("parent_result"),
        "baseline": result.get("baseline"),
        "best": result.get("best"),
        "history": result.get("history"),
        "evaluations": result.get("evaluations"),
        "operating_conditions": result.get("operating_conditions"),
        "ga_config": result.get("ga_config"),
        "improvement_ratio": result.get("improvement_ratio"),
        "seed_model": result.get("seed_model"),
        "baseline_source": result.get("baseline_source"),
        "evaluation_count": result.get("evaluation_count"),
        "generations_run": result.get("generations_run"),
        "bounds": result.get("bounds"),
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    target = output_dir / "result.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    args = build_parser().parse_args(argv)
    started = time.time()
    logger: Logger | None = None
    output_dir: Path | None = None
    result: dict = {"ok": False, "report_path": None, "model_path": None,
                    "metrics": None, "log_tail": [], "error": None}
    try:
        config = load_config(args.config, offline=args.offline, mode=args.mode,
                             task="ga" if args.ga else None)
        output_dir = config.ensure_output_dir()
        logger = Logger(log_file=output_dir / "agent.log")
        logger.status(f"AI 设计后端 v{VERSION} 启动")
        logger.status(f"配置文件: {config.config_path}")
        logger.status(f"任务模式: {config.task.upper()}"
                      + ("（遗传算法优化）" if config.task == "ga" else "（AI 设计）"))
        logger.status(f"服务商: {config.provider_label} ({config.provider}) "
                      f"base_url={config.base_url or '<offline>'}")
        logger.status(f"模型: {config.model or '<offline>'} | "
                      f"运行模式: {config.effective_mode()} | "
                      f"API Key: {config.masked_key()}")
        logger.status(f"输出目录: {output_dir}")
        logger.status(f"报告模板: {config.template}")
        if config.case_model:
            logger.status(f"上传案例 .vsp3: {config.case_model}")
            if config.task == "ga" and config.seed_model:
                logger.status("GA 基线优先级: case_model > seed_model")
        if config.task == "ga":
            logger.status(f"GA 参数: {config.ga}")
            if config.seed_model:
                logger.status(f"GA 基线参考模型(seed_model): {config.seed_model}")
            logger.status(f"GA 工况覆盖: {config.operating_conditions or '<取自设计需求>'}")
        if config.attachments:
            logger.status(f"上传附件: {len(config.attachments)} 个文件")
            for attachment in config.attachments:
                logger.status(f"  附件: {attachment}")
        for warning in config.validate():
            logger.warn(warning)
        agent = DesignAgent(config, logger=logger)
        result = agent.run()
        result["elapsed_s"] = round(time.time() - started, 1)
    except ConfigError as exc:
        message = f"配置错误: {exc}"
        if logger is not None:
            logger.error(message)
        else:
            print(f"[error] {message}", flush=True)
        result["error"] = message
    except VSPError as exc:
        message = f"OpenVSP/VSPAERO 错误: {exc}"
        if logger is not None:
            logger.error(message)
        else:
            print(f"[error] {message}", flush=True)
        result["error"] = message
    except KeyboardInterrupt:
        message = "用户中断"
        if logger is not None:
            logger.error(message)
        else:
            print(f"[error] {message}", flush=True)
        result["error"] = message
    except Exception as exc:  # pragma: no cover - 兜底
        message = f"未处理异常: {type(exc).__name__}: {exc}"
        if logger is not None:
            logger.error(message)
        else:
            print(f"[error] {message}", flush=True)
        result["error"] = message

    if logger is not None:
        result["log_tail"] = logger.tail(120)
        result["elapsed_s"] = result.get("elapsed_s", round(time.time() - started, 1))
    if output_dir is not None:
        try:
            target = write_result(output_dir, result)
            if logger is not None:
                logger.status(f"结果已写入: {target}")
        except OSError as exc:
            if logger is not None:
                logger.error(f"写入 result.json 失败: {exc}")
            result["error"] = result.get("error") or f"写入 result.json 失败: {exc}"
    else:
        if logger is None:
            # 配置都未能加载，无法写 result.json，只能返回非零
            pass

    if logger is not None:
        if result.get("ok"):
            logger.status(f"任务完成: report={result.get('report_path')} "
                          f"model={result.get('model_path')}")
        else:
            logger.error(f"任务失败: {result.get('error')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
