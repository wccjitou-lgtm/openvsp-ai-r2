"""AI 设计后端配置：加载、校验、服务商预设与路径解析。

配置文件为 JSON（UTF-8），示例见 ``examples/offline_config.json``。
所有相对路径的解析顺序：配置文件所在目录 → ai 根目录 → 当前工作目录。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AI_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = AI_ROOT / "templates" / "design_report_template.docx"
DEFAULT_OUTPUT_DIR = AI_ROOT / "outputs"
DEFAULT_MAX_ITERATIONS = 8
MAX_ITERATIONS_LIMIT = 50

#: OpenAI 兼容服务商预设。
#:
#: 2026-11 调研结论（见 D:\\Zcoding\\OpenVSP-R2\\docs\\00-调研报告.md）：
#: - DeepSeek 官方 base_url 不带 ``/v1``（``https://api.deepseek.com``）；
#:   ``deepseek-chat`` / ``deepseek-reasoner`` 已于 2026-07-24 停止，
#:   默认模型改为 ``deepseek-flash``。
#: - 智谱 GLM ``/models`` 未官方文档化，失败时回退 ``builtin_models`` 内置清单。
#: - Kimi 旧 ``moonshot-v1-*`` 已下线，默认 ``kimi-k3.1-pro``。
#: - OpenAI 模型迭代快，``/models`` 实测为准，内置清单仅作回退。
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "builtin_models": ["deepseek-flash"],
        "env_keys": ("DEEPSEEK_API_KEY",),
        "label": "DeepSeek",
        "tool_choice": "auto",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3",
        "builtin_models": ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2"],
        "env_keys": ("ZHIPUAI_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY"),
        "label": "智谱 GLM",
        # 智谱 tool_choice 仅支持 auto
        "tool_choice": "auto",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k3.1-pro",
        "builtin_models": ["kimi-k3.1-pro", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6"],
        "env_keys": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "label": "Kimi (Moonshot)",
        "tool_choice": "auto",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-6-astra",
        "builtin_models": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        "env_keys": ("OPENAI_API_KEY",),
        "label": "OpenAI",
        "tool_choice": "auto",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "builtin_models": [],
        "env_keys": ("AI_API_KEY", "OPENAI_API_KEY"),
        "label": "自定义 OpenAI 兼容服务",
        "tool_choice": "auto",
    },
}

VALID_MODES = ("auto", "offline", "llm")
_MODE_ALIASES = {"design": "auto", "cloud": "llm", "local": "offline", "none": "offline"}

#: 允许从环境变量注入 API key 的变量名。
#: UI 通过子进程环境变量 ``OPENVSP_AI_API_KEY`` 传入密钥（不写 ai_job.json）。
GENERIC_ENV_KEYS = ("OPENVSP_AI_API_KEY", "AI_API_KEY", "AERO_AI_API_KEY")
DEFAULT_API_KEY_ENV = "OPENVSP_AI_API_KEY"

_DESIGN_KEYS = (
    "aoa_start", "aoa_end", "aoa_npts", "beta_list", "wake_iters",
    "n_cpu", "mach", "velocity_ms", "altitude_m", "stability",
)
_VSP_KEYS = ("openvsp_root", "vspaero_exe", "pythonpath", "timeout_s", "export_formats")


class ConfigError(ValueError):
    """配置缺失或不合法。"""


def _expand_env_ref(value: str) -> str:
    """支持 ``${ENV_VAR}`` / ``$ENV_VAR`` 形式的 key 间接引用。"""
    if not isinstance(value, str) or not value:
        return value
    match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", value.strip())
    if not match:
        return value
    return os.environ.get(match.group(1), "")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    """把 attachments 配置归一化为字符串路径列表（支持单个字符串 / 列表 / 分号分隔）。"""
    if value is None:
        return []
    items: list[Any]
    if isinstance(value, str):
        items = re.split(r"[;\n]", value) if (";" in value or "\n" in value) else [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    paths: list[str] = []
    for item in items:
        text = str(item).strip().strip('"').strip("'")
        if text and text not in paths:
            paths.append(text)
    return paths


@dataclass
class AIConfig:
    """一次 AI 设计运行的完整配置。"""

    provider: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    model: str = ""
    prompt: str = ""
    output_dir: str = ""
    template: str = str(DEFAULT_TEMPLATE)
    mode: str = "auto"
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    temperature: float = 0.3
    timeout: float = 120.0
    offline: bool = False
    design: dict[str, Any] = field(default_factory=dict)
    vsp: dict[str, Any] = field(default_factory=dict)
    #: 上传附件文档路径列表（GUI「上传文档…」写入 ai_job.json 的 attachments）
    attachments: list[str] = field(default_factory=list)
    #: 任务模式：design（默认设计流程）| ga（遗传算法优化）
    task: str = "design"
    #: GA 参数（population/generations/time_budget_s/objective/...，见 ga_optimizer.GAConfig）
    ga: dict[str, Any] = field(default_factory=dict)
    #: GA 基线参考模型（上次 result.json 的 model_path 或空）
    seed_model: str = ""
    #: 上传案例 .vsp3（GUI「上传案例…」写入 ai_job.json 的 case_model；
    #: task=design 时作为修改基线、task=ga 时优先作为 GA 基线）
    case_model: str = ""
    #: 第一轮设计的模型/结果，用于“继续修改当前设计”
    parent_model: str = ""
    parent_result: str = ""
    #: 可选工况覆盖（altitude_m / speed_cruise_ms / rho / sound_speed_ms）
    operating_conditions: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    config_path: str = ""
    _config_dir: Path = field(default=AI_ROOT, repr=False)
    warnings: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ 加载
    @classmethod
    def from_file(cls, config_path: str | os.PathLike, offline: bool = False,
                  mode_override: str | None = None,
                  task_override: str | None = None) -> "AIConfig":
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"配置文件不存在: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON: {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件顶层必须是 JSON 对象: {path}")
        cfg = cls.from_dict(raw)
        cfg.config_path = str(path.resolve())
        cfg._config_dir = path.resolve().parent
        if offline:
            cfg.offline = True
            cfg.mode = "offline"
        if mode_override:
            cfg.mode = str(mode_override)
        if task_override:
            cfg.task = str(task_override)
        cfg._finalize()
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AIConfig":
        design = raw.get("design") if isinstance(raw.get("design"), dict) else {}
        vsp_cfg = raw.get("vsp") if isinstance(raw.get("vsp"), dict) else {}
        ga_cfg = raw.get("ga") if isinstance(raw.get("ga"), dict) else {}
        operating = (raw.get("operating_conditions")
                     if isinstance(raw.get("operating_conditions"), dict) else {})
        known = {
            "provider", "base_url", "api_key", "api_key_env", "model", "prompt",
            "output_dir", "template", "report_template", "ui", "mode",
            "max_iterations", "temperature", "attachments",
            "task", "ga", "seed_model", "case_model", "parent_model",
            "parent_result", "modify", "operating_conditions",
            "timeout", "offline", "design", "vsp", "title",
        }
        unknown = sorted(set(raw) - known)
        cfg = cls(
            provider=str(raw.get("provider", "deepseek")).strip().lower() or "deepseek",
            base_url=str(raw.get("base_url") or "").strip(),
            api_key=_expand_env_ref(str(raw.get("api_key") or "")),
            api_key_env=str(raw.get("api_key_env") or DEFAULT_API_KEY_ENV).strip(),
            model=str(raw.get("model") or "").strip(),
            prompt=str(raw.get("prompt") or "").strip(),
            output_dir=str(raw.get("output_dir") or "").strip(),
            template=str(raw.get("template") or raw.get("report_template") or DEFAULT_TEMPLATE),
            mode=str(raw.get("mode") or "auto").strip().lower(),
            max_iterations=_as_int(raw.get("max_iterations"), DEFAULT_MAX_ITERATIONS),
            temperature=_as_float(raw.get("temperature"), 0.3),
            timeout=_as_float(raw.get("timeout"), 120.0),
            offline=_as_bool(raw.get("offline"), False),
            design={k: design[k] for k in _DESIGN_KEYS if k in design},
            vsp={k: vsp_cfg[k] for k in _VSP_KEYS if k in vsp_cfg},
            attachments=_as_str_list(raw.get("attachments")),
            task=str(raw.get("task") or "design").strip().lower() or "design",
            ga={str(key): value for key, value in ga_cfg.items()},
            seed_model=str(raw.get("seed_model") or "").strip(),
            case_model=str(raw.get("case_model") or "").strip(),
            parent_model=str(raw.get("parent_model") or "").strip(),
            parent_result=str(raw.get("parent_result") or "").strip(),
            operating_conditions={str(key): value
                                  for key, value in operating.items()},
            title=str(raw.get("title") or "").strip(),
        )
        if unknown:
            cfg.warnings.append(f"忽略未知配置项: {', '.join(unknown)}")
        return cfg

    # ------------------------------------------------------------------ 校验
    def _finalize(self) -> None:
        if self.provider not in PROVIDER_PRESETS:
            raise ConfigError(
                f"未知 provider {self.provider!r}，可选: {', '.join(PROVIDER_PRESETS)}")
        preset = PROVIDER_PRESETS[self.provider]
        if not self.base_url:
            self.base_url = preset["base_url"]
        if not self.model:
            self.model = preset["model"]
        self.base_url = self.base_url.rstrip("/")
        if self.provider == "custom" and not self.base_url and self.mode != "offline":
            raise ConfigError("provider=custom 时必须显式配置 base_url")
        if self.mode in _MODE_ALIASES:
            self.mode = _MODE_ALIASES[self.mode]
        if self.mode not in VALID_MODES:
            raise ConfigError(f"mode 非法: {self.mode!r}，可选: {', '.join(VALID_MODES)}")
        self.max_iterations = max(1, min(_as_int(self.max_iterations, DEFAULT_MAX_ITERATIONS),
                                         MAX_ITERATIONS_LIMIT))
        if not self.prompt:
            raise ConfigError("配置缺少 prompt（自然语言设计需求）")
        if not self.output_dir:
            self.output_dir = str(DEFAULT_OUTPUT_DIR / "run")
        self.output_dir = str(self.resolve_path(self.output_dir))
        self.template = str(self.resolve_path(self.template))
        # 上传附件：解析为绝对路径；不存在/不支持仅记 warning，不致命
        from .attachments import SUPPORTED_SUFFIXES

        resolved_attachments: list[str] = []
        for item in self.attachments:
            path = self.resolve_path(item)
            resolved_attachments.append(str(path))
            if not path.is_file():
                self.warnings.append(f"附件不存在（解析时将跳过）: {item}")
            elif path.suffix.lower() not in SUPPORTED_SUFFIXES:
                self.warnings.append(
                    f"附件格式不受支持（解析时将报错）: {path.name} "
                    f"({path.suffix or '<无扩展名>'})")
        self.attachments = resolved_attachments
        if self.attachments:
            self.design.setdefault("attachments_max_chars", 60000)
            self.design.setdefault("attachments_max_total_chars", 240000)
        if not (0.0 <= self.temperature <= 2.0):
            self.temperature = 0.3
        if self.timeout <= 0:
            self.timeout = 120.0
        # 设计/求解默认参数
        self.design.setdefault("aoa_start", -4.0)
        self.design.setdefault("aoa_end", 10.0)
        self.design.setdefault("aoa_npts", 8)
        self.design.setdefault("wake_iters", 3)
        self.design.setdefault("n_cpu", 4)
        self.design.setdefault("stability", True)
        self.design.setdefault("beta_list", [0.0])
        self.vsp.setdefault("timeout_s", 300)
        self.vsp.setdefault("export_formats", ["vsp3", "stl", "csv"])
        # ---- 任务模式 / GA 配置 ------------------------------------------
        task = str(self.task or "design").strip().lower()
        if task in ("genetic", "genetic_algorithm", "ga_optimizer", "optimize"):
            task = "ga"
        if task in ("modify", "modify_design", "iteration", "followup"):
            task = "modify"
        if task not in ("design", "ga", "modify"):
            raise ConfigError(f"task 非法: {self.task!r}，可选: design / ga / modify")
        self.task = task
        if self.seed_model:
            self.seed_model = str(self.resolve_path(self.seed_model))
            if not Path(self.seed_model).is_file():
                self.warnings.append(
                    f"seed_model 不存在（GA 将忽略，仅按需求重新建模）: {self.seed_model}")
        if self.case_model:
            self.case_model = str(self.resolve_path(self.case_model))
            case_path = Path(self.case_model)
            if not case_path.is_file():
                self.warnings.append(
                    f"case_model 不存在（将忽略案例、按需求重新设计）: {self.case_model}")
            elif case_path.suffix.lower() != ".vsp3":
                self.warnings.append(
                    f"case_model 不是 .vsp3 文件（将忽略案例）: {case_path.name}")
        for field_name in ("parent_model", "parent_result"):
            value = getattr(self, field_name, "")
            if value:
                value = str(self.resolve_path(value))
                setattr(self, field_name, value)
                if not Path(value).is_file():
                    self.warnings.append(f"{field_name} 不存在（继续修改功能将无法引用）: {value}")
        cleaned_operating: dict[str, float] = {}
        for key, value in (self.operating_conditions or {}).items():
            if key not in ("altitude_m", "speed_cruise_ms", "velocity_ms",
                           "rho", "sound_speed_ms"):
                self.warnings.append(f"忽略未知 operating_conditions 字段: {key}")
                continue
            try:
                cleaned_operating[key] = float(value)
            except (TypeError, ValueError):
                self.warnings.append(
                    f"operating_conditions.{key} 不是数值，已忽略: {value!r}")
        self.operating_conditions = cleaned_operating
        if self.task == "ga" or self.ga:
            from .ga_optimizer import GAConfig

            raw_ga = dict(self.ga or {})
            normalized = GAConfig.from_dict(raw_ga)
            self.ga = normalized.to_dict()
            for key in ("population", "generations", "time_budget_s",
                        "max_evaluations"):
                if key not in raw_ga:
                    continue
                try:
                    raw_value = float(raw_ga[key])
                except (TypeError, ValueError):
                    self.warnings.append(
                        f"ga.{key} 不是数值，已用默认值 {self.ga[key]}")
                    continue
                if abs(raw_value - float(self.ga[key])) > 1e-9:
                    self.warnings.append(
                        f"ga.{key}={raw_ga[key]} 超出允许范围，已调整为 {self.ga[key]}")
            if self.task == "design":
                self.warnings.append(
                    "提供了 ga 参数但 task != ga，GA 参数不会生效（可设置 \"task\": \"ga\"）")

    # ------------------------------------------------------------------ 解析
    def resolve_path(self, value: str | os.PathLike | None) -> Path:
        """相对路径依次按 配置文件目录 → ai 根目录 → cwd 解析。"""
        if value is None:
            return AI_ROOT
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        candidates = [self._config_dir / path, AI_ROOT / path, Path.cwd() / path]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return candidates[1].resolve()

    def ensure_output_dir(self) -> Path:
        out = Path(self.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "figures").mkdir(parents=True, exist_ok=True)
        return out

    # ------------------------------------------------------------------ 派生
    @property
    def preset(self) -> dict[str, Any]:
        return PROVIDER_PRESETS.get(self.provider, {})

    @property
    def provider_label(self) -> str:
        return str(self.preset.get("label", self.provider))

    def _env_api_key(self) -> str:
        for name in GENERIC_ENV_KEYS + tuple(self.preset.get("env_keys", ())):
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    @property
    def builtin_models(self) -> list[str]:
        """服务商内置候选模型清单（``/models`` 不可用时的回退）。"""
        models = self.preset.get("builtin_models") or []
        return [str(item) for item in models]

    @property
    def tool_choice(self) -> str:
        """该服务商允许的 tool_choice（智谱仅支持 auto，其余默认 auto）。"""
        return str(self.preset.get("tool_choice", "auto"))

    @property
    def api_key_env_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for name in ((self.api_key_env,) + GENERIC_ENV_KEYS
                     + tuple(self.preset.get("env_keys", ()))):
            if name and name not in names:
                names.append(name)
        return tuple(names)

    @property
    def effective_api_key(self) -> str:
        """API key 解析：环境变量优先（UI 通过子进程 env 传入），config 字段兜底。"""
        for name in self.api_key_env_names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return (self.api_key or "").strip()

    @property
    def has_api_key(self) -> bool:
        return bool(self.effective_api_key)

    @property
    def can_use_llm(self) -> bool:
        """是否具备调用在线 LLM 的条件（模式 + base_url + key + model）。"""
        if self.mode == "offline" or self.offline:
            return False
        if self.mode == "llm" and not (self.has_api_key and self.model):
            return False
        return bool(self.base_url and self.has_api_key and self.model)

    def effective_mode(self) -> str:
        if self.mode == "offline" or self.offline:
            return "offline"
        if self.mode == "llm":
            return "llm"
        return "llm" if self.can_use_llm else "offline"

    def masked_key(self) -> str:
        key = self.effective_api_key
        if not key:
            return "<未配置>"
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}...{key[-4:]}"

    def validate(self) -> list[str]:
        """返回警告列表；致命问题抛 ConfigError。"""
        warnings = list(self.warnings)
        if not Path(self.template).is_file():
            warnings.append(f"报告模板不存在，将使用内置从头生成: {self.template}")
        if self.mode == "llm" and not self.has_api_key:
            raise ConfigError(
                "mode=llm 但未提供 api_key（环境变量 "
                + "/".join(self.api_key_env_names)
                + " 或配置项 api_key）")
        if self.mode == "llm" and not self.base_url:
            raise ConfigError("mode=llm 但未配置 base_url")
        if self.mode == "llm" and not self.model:
            raise ConfigError("mode=llm 但未配置 model")
        return warnings

    def to_public_dict(self) -> dict[str, Any]:
        """可安全写日志/结果文件的配置快照（不含明文 key）。"""
        data = {
            "provider": self.provider,
            "provider_label": self.provider_label,
            "base_url": self.base_url,
            "model": self.model,
            "mode": self.mode,
            "effective_mode": self.effective_mode(),
            "max_iterations": self.max_iterations,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "offline": self.offline,
            "output_dir": self.output_dir,
            "template": self.template,
            "prompt": self.prompt,
            "title": self.title,
            "api_key_masked": self.masked_key(),
            "api_key_env": self.api_key_env,
            "builtin_models": self.builtin_models,
            "tool_choice": self.tool_choice,
            "design": dict(self.design),
            "vsp": dict(self.vsp),
            "attachments": list(self.attachments),
            "task": self.task,
            "ga": dict(self.ga),
            "seed_model": self.seed_model,
            "case_model": self.case_model,
            "parent_model": self.parent_model,
            "parent_result": self.parent_result,
            "operating_conditions": dict(self.operating_conditions),
        }
        return data

    @property
    def design_title(self) -> str:
        if self.title:
            return self.title
        if self.task == "ga":
            return "遗传算法优化设计报告"
        return "无人机气动设计分析报告"


def load_config(config_path: str | os.PathLike, offline: bool = False,
                mode: str | None = None,
                task: str | None = None) -> AIConfig:
    config = AIConfig.from_file(config_path, offline=offline,
                                mode_override=mode, task_override=task)
    return config
