"""
配置管理 — 支持 YAML + 环境变量 + 编程式构建 + 配置验证。

加载优先级（高 → 低）：
1. 代码中显式传入
2. 环境变量 SKILL_TEST_*
3. YAML 配置文件
4. 内置默认值
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .log import get_logger
from .models import (
    AppConfig, CLIConfig, GitConfig, RetryConfig, SkillGroup,
    TaskConfig, SkillConfig, ReportFormat, ExperimentMode,
)

log = get_logger("config")

_YAML_AVAILABLE = False
try:
    import yaml  # type: ignore
    _YAML_AVAILABLE = True
except ImportError:
    pass


def _env(key: str, default: Any = None) -> Any:
    return os.environ.get(f"SKILL_TEST_{key}", default)


def _auto_cli_command() -> str:
    if platform.system() == "Windows" or os.environ.get("OS") == "Windows_NT":
        return "claude.cmd"
    return "claude"


def load_yaml(path: str | Path) -> dict:
    if not _YAML_AVAILABLE:
        raise ConfigError("需要安装 PyYAML：pip install pyyaml")
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在：{p}")
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件格式错误：{p}")
    return data


def _parse_cli(raw: dict) -> CLIConfig:
    return CLIConfig(
        command=raw.get("command", _auto_cli_command()),
        base_args=raw.get("base_args", CLIConfig().base_args),
        timeout=int(raw.get("timeout", 300)),
    )


def _parse_git(raw: dict) -> GitConfig:
    return GitConfig(
        author_name=raw.get("author_name", GitConfig.author_name),
        author_email=raw.get("author_email", GitConfig.author_email),
        base_branch=raw.get("base_branch", "main"),
        branch_prefix=raw.get("branch_prefix", "skill-test"),
        auto_push=raw.get("auto_push", False),
        cleanup_on_finish=raw.get("cleanup_on_finish", True),
    )


def _parse_retry(raw: dict) -> RetryConfig:
    return RetryConfig(
        max_retries=int(raw.get("max_retries", 2)),
        base_delay=float(raw.get("base_delay", 5.0)),
        max_delay=float(raw.get("max_delay", 60.0)),
        retry_on_timeout=raw.get("retry_on_timeout", True),
        retry_on_failure=raw.get("retry_on_failure", False),
    )


def _parse_tasks(raw_list: list[dict]) -> list[TaskConfig]:
    tasks = []
    for item in raw_list:
        if "id" not in item or "prompt" not in item:
            raise ConfigError(f"任务配置缺少必填字段 id/prompt：{item}")
        tasks.append(TaskConfig(
            id=item["id"],
            name=item.get("name", item["id"]),
            prompt=item["prompt"],
            expected_output=item.get("expected_output", ""),
            timeout=item.get("timeout"),
            mode=item.get("mode", ExperimentMode.CODING.value),
        ))
    return tasks


def _parse_skills(raw_list: list[dict]) -> list[SkillConfig]:
    skills = []
    for item in raw_list:
        if "name" not in item:
            raise ConfigError(f"Skill 配置缺少 name 字段：{item}")

        if item.get("preset"):
            from .discovery import get_preset
            preset = get_preset(item["preset"])
            if preset:
                preset_copy = SkillConfig(
                    name=item.get("name", preset.name),
                    system_prompt=preset.system_prompt,
                    skill_file=preset.skill_file,
                    ref_files=list(preset.ref_files),
                    tool=preset.tool,
                    origin=preset.origin,
                    description=preset.description,
                )
                skills.append(preset_copy)
                continue

        skills.append(SkillConfig(
            name=item["name"],
            system_prompt=item.get("system_prompt"),
            skill_file=item.get("skill_file"),
            ref_files=item.get("ref_files", []),
            tool=item.get("tool", "manual"),
            origin=item.get("origin", ""),
            description=item.get("description", ""),
        ))
    return skills


def _parse_skill_groups(raw_list: list[dict]) -> list[SkillGroup]:
    groups = []
    for item in raw_list:
        if "name" not in item or "skills" not in item:
            raise ConfigError(f"Skill 组配置缺少 name/skills 字段：{item}")
        groups.append(SkillGroup(
            name=item["name"],
            skills=item["skills"],
            compose_mode=item.get("compose_mode", "chain"),
        ))
    return groups


def validate_config(config: AppConfig) -> list[str]:
    """验证配置的完整性和合理性，返回警告列表。"""
    warnings: list[str] = []

    if not config.tasks:
        warnings.append("没有配置任何 task")

    if not config.skills:
        warnings.append("没有配置任何 skill")

    if config.max_workers < 1:
        warnings.append(f"max_workers 不合理: {config.max_workers}")

    if config.cli.timeout < 10:
        warnings.append(f"全局超时过短: {config.cli.timeout}s")

    for task in config.tasks:
        if not task.prompt.strip():
            warnings.append(f"任务 '{task.id}' 的 prompt 为空")
        if task.timeout and task.timeout < 10:
            warnings.append(f"任务 '{task.id}' 超时过短: {task.timeout}s")
        if task.mode not in {ExperimentMode.CODING.value, ExperimentMode.SOLUTION.value}:
            warnings.append(f"任务 '{task.id}' 的 mode 非法: {task.mode}")

    for skill in config.skills:
        if skill.skill_file and not Path(skill.skill_file).exists():
            warnings.append(f"Skill '{skill.name}' 的文件不存在: {skill.skill_file}")

    seen_ids = set()
    for task in config.tasks:
        if task.id in seen_ids:
            warnings.append(f"任务 ID 重复: {task.id}")
        seen_ids.add(task.id)

    if warnings:
        for w in warnings:
            log.warning("配置警告: %s", w)

    return warnings


def load_config(
    config_path: str | Path | None = None,
    overrides: dict | None = None,
) -> AppConfig:
    raw: dict = {}

    if config_path:
        raw = load_yaml(config_path)

    if overrides:
        raw = {**raw, **overrides}

    env_timeout = _env("TIMEOUT")
    env_workers = _env("MAX_WORKERS")
    env_output = _env("OUTPUT_DIR")
    env_command = _env("CLI_COMMAND")

    cli_raw = raw.get("cli", {})
    if env_command:
        cli_raw["command"] = env_command
    if env_timeout:
        cli_raw["timeout"] = int(env_timeout)

    git_raw = raw.get("git", {})
    retry_raw = raw.get("retry", {})

    config = AppConfig(
        cli=_parse_cli(cli_raw),
        git=_parse_git(git_raw),
        retry=_parse_retry(retry_raw),
        tasks=_parse_tasks(raw.get("tasks", [])),
        skills=_parse_skills(raw.get("skills", [])),
        skill_groups=_parse_skill_groups(raw.get("skill_groups", [])),
        output_dir=env_output or raw.get("output_dir", "results"),
        max_workers=int(env_workers or raw.get("max_workers", 3)),
        report_formats=[
            ReportFormat(f) for f in raw.get("report_formats", ["text", "json"])
        ],
        discover_skills=raw.get("discover_skills", False),
        discover_path=raw.get("discover_path", ""),
    )

    validate_config(config)

    return config


def build_default_config() -> AppConfig:
    return AppConfig(
        cli=CLIConfig(command=_auto_cli_command()),
        git=GitConfig(),
        retry=RetryConfig(),
        tasks=[
            TaskConfig(
                id="demo_001",
                name="Python 快速排序",
                prompt=(
                    "实现一个快速排序算法，要求：\n"
                    "1. 使用 Python\n"
                    "2. 包含类型标注\n"
                    "3. 包含单元测试"
                ),
                mode=ExperimentMode.CODING.value,
            ),
        ],
        skills=[
            SkillConfig(name="baseline", tool="builtin", origin="builtin", description="普通提示词基线"),
            SkillConfig(
                name="write-expert",
                system_prompt="你是专业的代码编写专家，擅长生成高质量、结构清晰的代码。",
                tool="preset",
                origin="builtin",
                description="强调代码质量与结构化输出的内置预设",
            ),
        ],
    )
