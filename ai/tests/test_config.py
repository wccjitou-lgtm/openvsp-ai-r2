"""config.py 单元测试：服务商预设、API Key 环境变量优先、路径与校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_design.config import (
    DEFAULT_TEMPLATE,
    PROVIDER_PRESETS,
    AIConfig,
    ConfigError,
    load_config,
)


def test_provider_presets_2026_11():
    """与 2026-11 调研结论一致：base_url / 默认模型 / 内置清单。"""
    assert PROVIDER_PRESETS["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert PROVIDER_PRESETS["deepseek"]["model"] == "deepseek-flash"
    assert PROVIDER_PRESETS["glm"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert PROVIDER_PRESETS["glm"]["model"] == "glm-5.3"
    assert {"glm-5.3", "glm-5.3-flash", "glm-5.3-flashx",
            "glm-5.2"} <= set(PROVIDER_PRESETS["glm"]["builtin_models"])
    assert PROVIDER_PRESETS["kimi"]["base_url"] == "https://api.moonshot.cn/v1"
    assert PROVIDER_PRESETS["kimi"]["model"] == "kimi-k3.1-pro"
    assert PROVIDER_PRESETS["openai"]["base_url"] == "https://api.openai.com/v1"
    assert PROVIDER_PRESETS["openai"]["model"] == "gpt-6-astra"
    for name in ("deepseek", "glm", "kimi", "openai", "custom"):
        assert name in PROVIDER_PRESETS


def _write_config(tmp_path, **overrides):
    raw = {
        "provider": "deepseek",
        "prompt": "设计一架 50kg 级电动无人机，高度 2000 m，速度 25-45 m/s。",
        "output_dir": "outputs/test_run",
        "mode": "offline",
    }
    raw.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_offline_config(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path, offline=True)
    assert config.provider == "deepseek"
    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-flash"
    assert config.mode == "offline"
    assert config.effective_mode() == "offline"
    assert config.has_api_key is False
    assert config.effective_api_key == ""
    # 相对输出目录按 配置文件目录 → ai 根目录 → cwd 解析
    assert "outputs" in config.output_dir
    assert config.output_dir.endswith("test_run")


def test_api_key_env_priority(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENVSP_AI_API_KEY", raising=False)
    path = _write_config(tmp_path, api_key="sk-from-config")
    config = load_config(path)
    assert config.effective_api_key == "sk-from-config"
    assert config.masked_key().startswith("sk-f")
    assert "sk-from-config" not in config.masked_key()

    # 环境变量优先于配置文件字段（UI 通过子进程 env 传入）
    monkeypatch.setenv("OPENVSP_AI_API_KEY", "sk-from-env-123456")
    config = load_config(path)
    assert config.effective_api_key == "sk-from-env-123456"
    public = config.to_public_dict()
    assert "sk-from-env-123456" not in json.dumps(public, ensure_ascii=False)
    assert public["api_key_env"] == "OPENVSP_AI_API_KEY"


def test_custom_api_key_env_field(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_LLM_KEY", raising=False)
    monkeypatch.setenv("MY_LLM_KEY", "sk-custom-abcdef")
    path = _write_config(tmp_path, api_key_env="MY_LLM_KEY")
    config = load_config(path)
    assert config.effective_api_key == "sk-custom-abcdef"


def test_env_ref_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY_REF", "sk-ref-999")
    path = _write_config(tmp_path, api_key="${MY_KEY_REF}")
    config = load_config(path)
    assert config.effective_api_key == "sk-ref-999"


def test_validation_errors(tmp_path):
    path = _write_config(tmp_path, provider="unknown-provider")
    with pytest.raises(ConfigError):
        load_config(path, offline=True)

    # custom 必须提供 base_url
    path = _write_config(tmp_path, provider="custom", mode="llm")
    with pytest.raises(ConfigError):
        load_config(path)

    # mode=llm 但无 key（validate() 在 CLI 启动时调用）
    path = _write_config(tmp_path, mode="llm")
    config = load_config(path)
    with pytest.raises(ConfigError):
        config.validate()

    # 缺少 prompt
    path = tmp_path / "noprompt.json"
    path.write_text(json.dumps({"provider": "deepseek"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_max_iterations_clamped(tmp_path):
    path = _write_config(tmp_path, max_iterations=999)
    config = load_config(path, offline=True)
    assert config.max_iterations == 50


def test_unknown_fields_warn(tmp_path):
    path = _write_config(tmp_path, something_new=1)
    config = load_config(path, offline=True)
    assert any("something_new" in item for item in config.warnings)


def test_default_template_path(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path, offline=True)
    assert config.template.endswith("design_report_template.docx")
    assert DEFAULT_TEMPLATE.name == "design_report_template.docx"


def test_effective_mode_auto(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENVSP_AI_API_KEY", raising=False)
    path = _write_config(tmp_path, mode="auto", api_key="")
    config = load_config(path)
    assert config.effective_mode() == "offline"
    config.api_key = "sk-xxx"
    assert config.effective_mode() == "llm"


# --------------------------------------------------------------------------- #
# 上传附件（attachments）配置
# --------------------------------------------------------------------------- #
def test_attachments_parsed_and_resolved(tmp_path):
    (tmp_path / '设计说明.docx').write_bytes(b'placeholder')
    path = _write_config(tmp_path, attachments=['设计说明.docx'])
    config = load_config(path, offline=True)
    assert len(config.attachments) == 1
    assert Path(config.attachments[0]).is_absolute()
    assert config.attachments[0].endswith('设计说明.docx')
    assert config.to_public_dict()['attachments'] == config.attachments
    assert not any('附件不存在' in item for item in config.warnings)


def test_attachments_missing_and_unsupported_warn(tmp_path):
    (tmp_path / '图片.png').write_bytes(b'\x89PNG')
    path = _write_config(
        tmp_path, attachments=['不存在.docx', '图片.png'])
    config = load_config(path, offline=True)
    # 不存在/不支持的附件只记 warning，不致命、仍保留在列表中
    assert len(config.attachments) == 2
    assert any('附件不存在' in item for item in config.warnings)
    assert any('格式不受支持' in item for item in config.warnings)
    assert all(item.endswith(('.docx', '.png')) for item in config.attachments)


def test_attachments_accepts_string_and_semicolon(tmp_path):
    (tmp_path / 'a.txt').write_text('a', encoding='utf-8')
    (tmp_path / 'b.md').write_text('b', encoding='utf-8')
    path = _write_config(tmp_path, attachments='a.txt; b.md')
    config = load_config(path, offline=True)
    assert len(config.attachments) == 2
    path = _write_config(tmp_path, attachments='a.txt')
    config = load_config(path, offline=True)
    assert len(config.attachments) == 1


def test_attachments_default_empty(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path, offline=True)
    assert config.attachments == []


# --------------------------------------------------------------------------- #
# 任务模式 / GA 配置
# --------------------------------------------------------------------------- #
def test_task_default_and_ga_config(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path, offline=True)
    assert config.task == 'design'
    assert config.ga == {}
    assert config.seed_model == ''
    assert config.operating_conditions == {}

    path = _write_config(
        tmp_path, task='ga',
        ga={'population': 6, 'generations': 2, 'time_budget_s': 120,
            'objective': 'max_ld'},
        operating_conditions={'altitude_m': 3000, 'speed_cruise_ms': 40})
    config = load_config(path, offline=True)
    assert config.task == 'ga'
    assert config.ga['population'] == 6
    assert config.ga['generations'] == 2
    assert config.ga['time_budget_s'] == 120.0
    assert config.ga['objective'] == 'max_ld'
    assert config.operating_conditions == {'altitude_m': 3000.0,
                                           'speed_cruise_ms': 40.0}
    public = config.to_public_dict()
    assert public['task'] == 'ga'
    assert public['operating_conditions']['altitude_m'] == 3000.0


def test_task_invalid_raises(tmp_path):
    path = _write_config(tmp_path, task='bogus')
    with pytest.raises(ConfigError):
        load_config(path, offline=True)


def test_ga_clamped_with_warning(tmp_path):
    path = _write_config(
        tmp_path, task='ga',
        ga={'population': 999, 'generations': 0, 'time_budget_s': 5,
            'max_evaluations': 999})
    config = load_config(path, offline=True)
    assert config.ga['population'] == 24
    assert config.ga['generations'] == 1
    assert config.ga['time_budget_s'] == 30.0
    assert config.ga['max_evaluations'] == 40
    assert any('population' in item for item in config.warnings)
    assert any('generations' in item for item in config.warnings)


def test_ga_task_override_from_cli(tmp_path):
    path = _write_config(tmp_path)  # task=design
    config = load_config(path, offline=True, task='ga')
    assert config.task == 'ga'
    assert config.ga['population'] == 8


def test_seed_model_resolution_and_warning(tmp_path):
    (tmp_path / 'seed.vsp3').write_text('<?xml version="1.0"?>', encoding='utf-8')
    path = _write_config(tmp_path, task='ga', seed_model='seed.vsp3')
    config = load_config(path, offline=True)
    assert Path(config.seed_model).is_absolute()
    assert Path(config.seed_model).is_file()
    assert not any('seed_model' in item for item in config.warnings)

    path = _write_config(tmp_path, task='ga', seed_model='missing.vsp3')
    config = load_config(path, offline=True)
    assert any('seed_model' in item for item in config.warnings)


def test_operating_conditions_validation_warns(tmp_path):
    path = _write_config(
        tmp_path, task='ga',
        operating_conditions={'altitude_m': 'abc', 'unknown_key': 1,
                              'speed_cruise_ms': 33})
    config = load_config(path, offline=True)
    assert config.operating_conditions == {'speed_cruise_ms': 33.0}
    assert any('altitude_m' in item for item in config.warnings)
    assert any('unknown_key' in item for item in config.warnings)
