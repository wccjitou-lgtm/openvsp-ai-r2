"""OpenAI 工具 schema 与执行器。

工具名统一使用下划线前缀 ``openvsp_``（与 MCP / 各厂商 tool calling 兼容）；
同时接受不带前缀的旧名字作为别名（build_aircraft → openvsp_build_aircraft）。

执行器绑定 :class:`ai_design.designer.Designer`，所有异常都归一化为
``{"ok": false, "error": "..."}`` 返回给模型，让模型自行修正，不直接抛给用户。
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from .designer import Designer
from .logging_util import Logger
from .vsp_bridge import VSPError

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "openvsp_get_geometry",
            "description": "查看当前已建立的 OpenVSP 几何摘要（机翼/机身/尾翼参数、"
                           "参考量、模型文件路径）。在建模后用于确认几何。",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_list_fuselage_styles",
            "description": "列出机身规则。当前规则为十截面圆筒机身（首尾 0.5R、其余 R、圆形端盖）："
                           "长度按经验翼展比与尾臂确定，最大半径按质量/容积/长径比约束确定；"
                           "可在 openvsp_build_aircraft 中通过 fuselage_style=circular_profile 显式指定。",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_list_airfoils",
            "description": "从本机 Profili 翼型库（含 2200+ 翼型）或内置 NACA 翼型中，"
                           "按任务类型、雷诺数、马赫数、厚度范围筛选候选翼型；"
                           "返回名称、厚度、弯度、评分与选择理由。选择翼型前调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string",
                                "enum": ["general", "flying_wing", "high_speed",
                                         "low_re", "endurance"],
                                "description": "任务类型：常规/飞翼/高速/低Re/长航时"},
                    "reynolds": {"type": "number", "description": "设计雷诺数"},
                    "mach": {"type": "number", "description": "设计马赫数"},
                    "thickness_min": {"type": "number", "description": "最小相对厚度 %"},
                    "thickness_max": {"type": "number", "description": "最大相对厚度 %"},
                    "count": {"type": "integer", "description": "返回候选数量，默认 8"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_build_aircraft",
            "description": "根据设计要求建立/重建 OpenVSP 无人机模型：解析需求 → "
                           "机翼/尾翼/机身参数 sizing → 生成 .vsp3。未提供的参数沿用"
                           "已解析的设计需求。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mtow_kg": {"type": "number", "description": "最大起飞重量 kg"},
                    "altitude_m": {"type": "number", "description": "设计飞行高度 m"},
                    "speed_min_ms": {"type": "number", "description": "最小飞行速度 m/s"},
                    "speed_max_ms": {"type": "number", "description": "最大飞行速度 m/s"},
                    "configuration": {
                        "type": "string",
                        "enum": ["conventional", "vtail", "flying_wing", "canard", "tandem"],
                        "description": "气动布局：常规尾翼 / V 尾 / 飞翼 / 鸭翼 / 串列翼",
                    },
                    "engine": {"type": "string",
                               "description": "动力形式，如 electric / EJ115 / TWP50"},
                    "aspect_ratio": {"type": "number", "description": "机翼展弦比"},
                    "wing_area_m2": {"type": "number", "description": "机翼面积 m²"},
                    "max_span_m": {"type": "number", "description": "翼展上限 m"},
                    "airfoil": {"type": "string", "description": "翼型，如 NACA4412"},
                    "static_margin": {"type": "number",
                                      "description": "静稳定裕度（小数，0.10 表示 10%）"},
                    "taper_ratio": {"type": "number",
                                    "description": "机翼梢根比，常规 0.35~0.70"},
                    "sweep_deg": {"type": "number",
                                  "description": "机翼后掠角（度）"},
                    "dihedral_deg": {"type": "number",
                                     "description": "机翼上反角（度），低速无人机常用 0~5°"},
                    "tail_volume_h": {"type": "number",
                                      "description": "平尾容量系数 Vh，目标约 0.35~0.80"},
                    "tail_volume_v": {"type": "number",
                                      "description": "垂尾容量系数 Vv，目标约 0.03~0.05"},
                    "trim_roots_to_fuselage": {"type": "boolean",
                                               "description": "是否按机身宽度裁剪翼根/平尾根，默认 true"},
                    "fuselage_style": {
                        "type": "string",
                        "enum": ["circular_profile"],
                        "description": "机身规则：circular_profile（十截面圆筒，首尾半半径）",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_evaluate_design",
            "description": "按经验设计准则评估当前完成气动分析的设计：机翼参数、尾翼容量、"
                           "静稳定性、飞行品质（短周期/长周期/荷兰滚/滚转模态）与操纵性，"
                           "并返回通过/警告/失败项。用于迭代修改前判断。",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_modify_parameter",
            "description": "修改已建模型某个部件的几何参数并重建模型、重新分析。",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "enum": ["wing", "fuselage", "htail", "vtail"],
                        "description": "部件名称",
                    },
                    "parameter": {
                        "type": "string",
                        "description": "参数名，例如 span_m / root_chord_m / "
                                       "tip_chord_m / sweep_deg / dihedral_deg / "
                                       "x_le_m / length_m / max_diameter_m",
                    },
                    "value": {"type": "number", "description": "新数值（SI 单位）"},
                },
                "required": ["component", "parameter", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_run_vspaero",
            "description": "运行 VSPAERO 气动分析（极曲线 polar + 稳定性导数 .stab），"
                           "返回 CLmax、(L/D)max、CD0、静稳定裕度等指标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "aoa_start": {"type": "number", "description": "起始迎角 deg"},
                    "aoa_end": {"type": "number", "description": "终止迎角 deg"},
                    "aoa_npts": {"type": "integer", "description": "迎角点数"},
                    "betas": {"type": "array", "items": {"type": "number"},
                              "description": "侧滑角列表 deg"},
                    "velocity_ms": {"type": "number", "description": "来流速度 m/s"},
                    "stability": {"type": "boolean",
                                  "description": "是否计算稳定性导数（默认 true）"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_optimize",
            "description": "基于实测极曲线标定 CD0/Oswald 效率，在翼展与失速约束内"
                           "解析优化翼面积/展弦比，返回最优候选与预期改善。",
            "parameters": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string",
                                  "enum": ["cruise_ld", "endurance", "climb"],
                                  "description": "优化目标，默认 cruise_ld"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_export",
            "description": "导出模型文件（.vsp3 / STL / DegenGeom CSV）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "formats": {"type": "array",
                                "items": {"type": "string",
                                          "enum": ["vsp3", "stl", "csv"]},
                                "description": "导出格式列表，默认全部"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openvsp_generate_report",
            "description": "根据当前设计结果按内置模板生成中文 Word 气动设计报告（docx）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "报告标题"},
                },
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = [item["function"]["name"] for item in TOOL_DEFINITIONS]

#: 兼容不带前缀的旧工具名
TOOL_ALIASES = {
    "build_aircraft": "openvsp_build_aircraft",
    "modify_parameter": "openvsp_modify_parameter",
    "run_vspaero": "openvsp_run_vspaero",
    "run_analysis": "openvsp_run_vspaero",
    "optimize": "openvsp_optimize",
    "export": "openvsp_export",
    "generate_report": "openvsp_generate_report",
    "get_geometry": "openvsp_get_geometry",
    "list_airfoils": "openvsp_list_airfoils",
    "list_fuselage_styles": "openvsp_list_fuselage_styles",
    "evaluate_design": "openvsp_evaluate_design",
}


def normalize_tool_name(name: str) -> str:
    value = str(name or "").strip()
    if value in TOOL_NAMES:
        return value
    return TOOL_ALIASES.get(value, value)


def tool_schemas() -> list[dict[str, Any]]:
    """返回可传给 chat/completions 的工具 schema 列表。"""
    return json.loads(json.dumps(TOOL_DEFINITIONS, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class ToolExecutor:
    """把模型 tool_call 映射到 Designer 方法，返回 JSON 可序列化结果。"""

    def __init__(self, designer: Designer, logger: Logger,
                 max_result_chars: int = 8000):
        self.designer = designer
        self.logger = logger
        self.max_result_chars = max_result_chars
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "openvsp_get_geometry": self._get_geometry,
            "openvsp_list_airfoils": self._list_airfoils,
            "openvsp_list_fuselage_styles": self._list_fuselage_styles,
            "openvsp_evaluate_design": self._evaluate_design,
            "openvsp_build_aircraft": self._build_aircraft,
            "openvsp_modify_parameter": self._modify_parameter,
            "openvsp_run_vspaero": self._run_vspaero,
            "openvsp_optimize": self._optimize,
            "openvsp_export": self._export,
            "openvsp_generate_report": self._generate_report,
        }

    # ------------------------------------------------------------------ 入口
    def execute(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        tool_name = normalize_tool_name(name)
        handler = self._handlers.get(tool_name)
        if handler is None:
            self.logger.tool(f"{name}(?) -> 未知工具")
            return {"ok": False, "error": f"未知工具: {name!r}",
                    "available_tools": list(self._handlers)}
        arguments = arguments if isinstance(arguments, dict) else {}
        self.logger.tool(f"{tool_name}({_brief_args(arguments)})")
        try:
            result = handler(arguments)
        except VSPError as exc:
            self.logger.error(f"工具 {tool_name} 失败: {exc}")
            return {"ok": False, "tool": tool_name, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - 工具容错
            self.logger.error(f"工具 {tool_name} 异常: {type(exc).__name__}: {exc}")
            return {"ok": False, "tool": tool_name,
                    "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        if "ok" not in result:
            result["ok"] = True
        brief = _brief_result(result)
        self.logger.tool(f"{tool_name} -> {brief}")
        return result

    def compact_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """压缩工具结果，避免超出模型上下文。"""
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= self.max_result_chars:
            return result
        compact: dict[str, Any] = {"ok": result.get("ok", True),
                                   "compacted": True,
                                   "note": "结果过大已压缩，请用 openvsp_get_geometry "
                                           "或指标摘要查看关键信息"}
        for key in ("model_path", "report_path", "error", "message", "mode"):
            if result.get(key):
                compact[key] = result[key]
        metrics = result.get("metrics")
        if isinstance(metrics, dict):
            compact["metrics"] = _compact_metrics(metrics)
        for key in ("geometry", "exports"):
            if result.get(key):
                compact[key] = result[key] if key == "exports" else "已省略"
        return compact

    # ------------------------------------------------------------------ 具体
    def _get_geometry(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.designer.get_geometry()
        if not result.get("ok"):
            return result
        geometry = result.get("geometry", {})
        totals = geometry.get("totals", {})
        return {
            "ok": True,
            "model_path": self.designer.model_path,
            "configuration": geometry.get("configuration"),
            "totals": totals,
            "wing": geometry.get("wing"),
            "fuselage": geometry.get("fuselage"),
            "vtail": geometry.get("vtail"),
            "htail": geometry.get("htail"),
            "vtail_conventional": geometry.get("vtail_conventional"),
            "control_surfaces": geometry.get("control_surfaces"),
            "sizing": geometry.get("sizing"),
        }

    def _list_airfoils(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .airfoils import shortlist, estimate_conditions
        designer = self.designer
        spec = designer.spec
        conditions = estimate_conditions(spec, designer.geometry) if spec else {}
        purpose = str(arguments.get("purpose") or "").strip() or None
        reynolds = arguments.get("reynolds")
        mach = arguments.get("mach")
        count = int(arguments.get("count") or 8)
        kwargs: dict[str, Any] = {"count": max(1, min(count, 20))}
        if purpose:
            kwargs["purpose"] = purpose
        if reynolds is not None:
            kwargs["reynolds"] = float(reynolds)
        else:
            kwargs["reynolds"] = round(float(conditions.get("reynolds", 250000.0)), 1)
        if mach is not None:
            kwargs["mach"] = float(mach)
        else:
            kwargs["mach"] = round(float(conditions.get("mach", 0.1)), 4)
        if arguments.get("thickness_min") is not None:
            kwargs["thickness_min"] = float(arguments["thickness_min"])
        if arguments.get("thickness_max") is not None:
            kwargs["thickness_max"] = float(arguments["thickness_max"])
        result = shortlist(**kwargs)
        result["note"] = ("选择后将 airfoil 名称传给 openvsp_build_aircraft；"
                          "若未传，后端会按同一规则自动选择。")
        return result

    def _list_fuselage_styles(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .fuselage_rules import list_styles
        return {"ok": True, "styles": list_styles(),
                "note": "机身固定 10 个圆截面、均匀分布；首尾截面半径 0.5R、其余 8 个为 R，首尾圆形端盖；长度按经验翼展比与机翼/尾翼/重心/焦点，"
                        "半径按工况自动确定。"}

    def _build_aircraft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        designer = self.designer
        if designer.spec is None:
            designer.parse_requirements()
        spec = designer.spec
        assert spec is not None
        overrides = {
            "mtow_kg": ("mtow_kg", "mtow_kg"),
            "altitude_m": ("altitude_m", "altitude_m"),
            "speed_min_ms": ("speed_min_ms", "speed_min_ms"),
            "speed_max_ms": ("speed_max_ms", "speed_max_ms"),
            "configuration": ("configuration", "configuration"),
            "engine": ("engine", "engine"),
            "aspect_ratio": ("aspect_ratio", "aspect_ratio"),
            "wing_area_m2": ("wing_area_m2", "wing_area_m2"),
            "max_span_m": ("max_span_m", "max_span_m"),
            "static_margin": ("static_margin", "static_margin"),
            "taper_ratio": ("taper_ratio", "taper_ratio"),
            "sweep_deg": ("sweep_deg", "sweep_deg"),
            "dihedral_deg": ("dihedral_deg", "dihedral_deg"),
            "tail_volume_h": ("tail_volume_h", "tail_volume_h"),
            "tail_volume_v": ("tail_volume_v", "tail_volume_v"),
        }
        applied: dict[str, Any] = {}
        for key, (attr, _json_key) in overrides.items():
            if key in arguments and arguments[key] is not None:
                value = arguments[key]
                if key in ("mtow_kg", "altitude_m", "speed_min_ms", "speed_max_ms",
                           "aspect_ratio", "wing_area_m2", "max_span_m",
                           "static_margin", "taper_ratio", "sweep_deg",
                           "dihedral_deg", "tail_volume_h", "tail_volume_v"):
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                setattr(spec, attr, value)
                applied[key] = value
        if "airfoil" in arguments and arguments["airfoil"]:
            spec.airfoil_root = str(arguments["airfoil"])
            designer.airfoil_locked = True
            applied["airfoil"] = spec.airfoil_root
        if arguments.get("fuselage_style"):
            spec.fuselage_style = str(arguments["fuselage_style"])
            applied["fuselage_style"] = spec.fuselage_style
        if "trim_roots_to_fuselage" in arguments:
            spec.trim_roots_to_fuselage = bool(arguments["trim_roots_to_fuselage"])
            applied["trim_roots_to_fuselage"] = spec.trim_roots_to_fuselage
        if applied:
            designer.logger.status(f"按模型要求更新设计参数: {applied}")
        result = designer.build_aircraft()
        geo = designer.geometry or {}
        return {
            "ok": True,
            "model_path": result.get("model_path"),
            "applied_overrides": applied,
            "wing": geo.get("wing"),
            "fuselage": geo.get("fuselage"),
            "vtail": geo.get("vtail"),
            "htail": geo.get("htail"),
            "vtail_conventional": geo.get("vtail_conventional"),
            "sizing": geo.get("sizing"),
            "control_surfaces": geo.get("control_surfaces"),
            "geometry": _compact_geometry(geo),
        }

    def _evaluate_design(self, arguments: dict[str, Any]) -> dict[str, Any]:
        designer = self.designer
        if designer.metrics is None:
            return {"ok": False, "error": "尚未完成 VSPAERO 分析，无法评估设计准则"}
        criteria = (designer.metrics or {}).get("design_criteria")
        if not criteria:
            return {"ok": False, "error": "设计准则结果不可用"}
        return {"ok": True, "design_criteria": criteria,
                "geometry": _compact_geometry(designer.geometry or {})}

    def _modify_parameter(self, arguments: dict[str, Any]) -> dict[str, Any]:
        component = arguments.get("component")
        parameter = arguments.get("parameter")
        if component is None or parameter is None or "value" not in arguments:
            return {"ok": False, "error": "需要 component / parameter / value 三个参数"}
        result = self.designer.modify_parameter(str(component), str(parameter),
                                                float(arguments["value"]))
        metrics = result.pop("metrics", None)
        if metrics:
            result["metrics"] = _compact_metrics(metrics)
        return result

    def _run_vspaero(self, arguments: dict[str, Any]) -> dict[str, Any]:
        designer = self.designer
        if designer.geometry is None:
            return {"ok": False, "error": "尚未建立几何模型，请先调用 "
                                          "openvsp_build_aircraft"}
        params: dict[str, Any] = {}
        aoa_start = arguments.get("aoa_start")
        aoa_end = arguments.get("aoa_end")
        aoa_npts = arguments.get("aoa_npts")
        if aoa_start is not None or aoa_end is not None or aoa_npts is not None:
            start = float(aoa_start if aoa_start is not None
                          else designer.config.design.get("aoa_start", -4.0))
            end = float(aoa_end if aoa_end is not None
                        else designer.config.design.get("aoa_end", 10.0))
            npts = int(aoa_npts if aoa_npts is not None
                       else designer.config.design.get("aoa_npts", 8))
            npts = max(3, min(npts, 30))
            params["aoa_list"] = [round(start + index * (end - start) / (npts - 1), 3)
                                  for index in range(npts)]
        if arguments.get("betas"):
            params["betas"] = [float(value) for value in arguments["betas"]][:6]
        if arguments.get("velocity_ms"):
            designer.config.design["velocity_ms"] = float(arguments["velocity_ms"])
        if arguments.get("stability") is not None:
            params["stability"] = bool(arguments["stability"])
        result = designer.run_analysis(params)
        metrics = result.get("metrics", {})
        return {"ok": True, "metrics": _compact_metrics(metrics)}

    def _optimize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        designer = self.designer
        if designer.metrics is None:
            return {"ok": False, "error": "尚未运行 VSPAERO 分析，无法优化"}
        objective = str(arguments.get("objective") or "cruise_ld")
        result = designer.optimize(objective=objective)
        return result

    def _export(self, arguments: dict[str, Any]) -> dict[str, Any]:
        designer = self.designer
        if designer.geometry is None:
            return {"ok": False, "error": "尚未建立几何模型"}
        result = designer.finalize()
        return {"ok": True, "exports": result.get("exports", {})}

    def _generate_report(self, arguments: dict[str, Any]) -> dict[str, Any]:
        designer = self.designer
        if designer.metrics is None:
            return {"ok": False, "error": "尚未完成气动分析，无法生成报告"}
        title = arguments.get("title")
        path = designer.generate_report(title=str(title) if title else None)
        return {"ok": True, "report_path": path}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _brief_args(arguments: dict[str, Any], limit: int = 160) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:
        text = str(arguments)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _brief_result(result: dict[str, Any], limit: int = 200) -> str:
    ok = result.get("ok")
    for key in ("error", "report_path", "model_path", "message"):
        if result.get(key):
            return f"ok={ok} {key}={result[key]}"
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        aero = metrics.get("aero") or {}
        parts = [f"ok={ok}"]
        for name in ("cl_max", "ld_max", "cd_min"):
            if isinstance(aero.get(name), (int, float)):
                parts.append(f"{name}={aero[name]:.4g}")
        return " ".join(parts)
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        text = str(result)
    return text[:limit]


def _compact_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """挑出报告/对话所需的关键指标，避免把整条极曲线塞给模型。"""
    if not isinstance(metrics, dict):
        return {}
    compact: dict[str, Any] = {}
    design = metrics.get("design") or {}
    compact["design"] = {key: design.get(key) for key in
                         ("name", "mtow_kg", "altitude_m", "speed_min_ms",
                          "speed_max_ms", "speed_cruise_ms", "configuration",
                          "engine", "configuration_label") if design.get(key) is not None}
    geometry = metrics.get("geometry") or {}
    wing = geometry.get("wing") or {}
    sizing = geometry.get("sizing") or {}
    compact["geometry"] = {
        "span_m": wing.get("span_m"), "area_m2": sizing.get("area_m2"),
        "aspect_ratio": sizing.get("aspect_ratio"), "mac_m": sizing.get("mac_m"),
        "fuselage_length_m": (geometry.get("fuselage") or {}).get("length_m"),
    }
    aero = metrics.get("aero") or {}
    compact["aero"] = {key: aero.get(key) for key in
                       ("cl_max", "alpha_at_cl_max_deg", "cl_at_zero_deg",
                        "cl_alpha_per_rad", "cd_min", "cd0_estimate", "ld_max",
                        "alpha_at_ld_max_deg", "alpha_zero_lift_deg", "stable")
                       if aero.get(key) is not None}
    stability = metrics.get("stability") or {}
    compact["stability"] = {key: stability.get(key) for key in
                            ("static_margin", "static_margin_target",
                             "neutral_point_m", "xcg_m", "stable")
                            if stability.get(key) is not None}
    cruise = metrics.get("cruise") or {}
    compact["cruise"] = {key: cruise.get(key) for key in
                         ("velocity_ms", "cl_cruise", "cd_cruise", "ld_cruise",
                          "power_required_kw") if cruise.get(key) is not None}
    optimization = metrics.get("optimization") or {}
    if optimization:
        compact["optimization"] = {
            "improvement_ratio": optimization.get("improvement_ratio"),
            "best": optimization.get("best"),
            "rebuild": optimization.get("rebuild"),
        }
    vspaero = metrics.get("vspaero") or {}
    if vspaero:
        compact["vspaero"] = {"pass": vspaero.get("pass"),
                              "elapsed_s": vspaero.get("elapsed_s"),
                              "stab_available": bool(metrics.get("stability", {}).get("sm_method", "").startswith("VSPAERO"))}
    compact["files"] = {key: value for key, value in (metrics.get("files") or {}).items()
                        if value}
    compact["elapsed_s"] = metrics.get("elapsed_s")
    return compact


def _compact_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    wing = geometry.get("wing") or {}
    return {
        "configuration": geometry.get("configuration"),
        "wing_span_m": wing.get("span_m"),
        "wing_area_m2": (geometry.get("sizing") or {}).get("area_m2"),
        "aspect_ratio": (geometry.get("sizing") or {}).get("aspect_ratio"),
        "components": len(geometry.get("components") or []),
    }
