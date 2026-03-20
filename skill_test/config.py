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
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .log import get_logger
from .models import (
    AppConfig, CLIConfig, OpenAIResponsesConfig, GitConfig, RetryConfig, SkillGroup,
    TaskConfig, SkillConfig, ReportFormat, ExperimentMode, DEFAULT_TIMEOUT_SECONDS,
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


def _iter_cli_name_candidates(command: str) -> list[str]:
    names: list[str] = []

    def _add(value: str | None) -> None:
        text = (value or "").strip()
        if text and text not in names:
            names.append(text)

    raw = command.strip()
    _add(raw)

    lower = raw.lower()
    if lower.endswith(".cmd") or lower.endswith(".exe"):
        _add(raw.rsplit(".", 1)[0])
    else:
        _add(f"{raw}.cmd")
        _add(f"{raw}.exe")

    for fallback in ("claude", "claude.cmd", "codex", "codex.cmd", "codex.exe"):
        _add(fallback)

    return names


def _iter_path_command_candidates(name: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    def _add(candidate: Path) -> None:
        normalized = candidate.expanduser()
        if normalized in seen:
            return
        seen.add(normalized)
        paths.append(normalized)

    candidate = Path(name).expanduser()
    if candidate.is_absolute() or "/" in name or "\\" in name:
        _add(candidate)
        return paths

    resolved = shutil.which(name)
    if resolved:
        _add(Path(resolved))

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        _add(Path(entry) / name)

    return paths


def _iter_app_bundle_candidates(name: str) -> list[Path]:
    if "/" in name or "\\" in name:
        return []

    paths: list[Path] = []
    seen: set[Path] = set()

    for base in (Path("/Applications"), Path.home() / "Applications"):
        if not base.is_dir():
            continue
        pattern = f"*.app/Contents/Resources/{name}"
        for candidate in sorted(base.glob(pattern)):
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)

    return paths


def _probe_cli_command(candidate: Path) -> bool:
    if not candidate.exists() or candidate.is_dir():
        return False
    if platform.system() != "Windows" and not os.access(candidate, os.X_OK):
        return False

    try:
        proc = subprocess.run(
            [str(candidate), "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            encoding="utf-8",
        )
    except (FileNotFoundError, PermissionError, OSError):
        return False
    except subprocess.TimeoutExpired:
        return True

    stderr = (proc.stderr or "").lower()
    if proc.returncode == 127 and (
        "not found" in stderr or "no such file or directory" in stderr
    ):
        return False

    return True


def resolve_cli_command(command: str | None = None) -> str:
    """解析当前平台可用的 CLI 命令，必要时自动回退。"""
    raw = (command or _auto_cli_command()).strip() or _auto_cli_command()
    seen: set[Path] = set()
    for name in _iter_cli_name_candidates(raw):
        for candidate in _iter_path_command_candidates(name):
            if candidate in seen:
                continue
            seen.add(candidate)
            if _probe_cli_command(candidate):
                resolved = str(candidate) if candidate.is_absolute() else name
                if resolved != raw:
                    log.warning("CLI 命令 '%s' 不可用，自动改用 '%s'", raw, resolved)
                return resolved

        for candidate in _iter_app_bundle_candidates(name):
            if candidate in seen:
                continue
            seen.add(candidate)
            if _probe_cli_command(candidate):
                resolved = str(candidate)
                if resolved != raw:
                    log.warning("CLI 命令 '%s' 不可用，自动改用 '%s'", raw, resolved)
                return resolved

    return raw


def _installed_skill_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    home = Path.home()
    codex_home = os.environ.get("CODEX_HOME")

    base_dirs = [
        Path(codex_home) if codex_home else home / ".codex",
        home / ".claude",
        home / ".agents",
        home / ".cursor",
        home / ".gemini",
    ]

    for base_dir in base_dirs:
        skills_dir = base_dir / "skills"
        if skills_dir in seen:
            continue
        seen.add(skills_dir)
        roots.append(skills_dir)

    return roots


def _skill_name_candidates(skill_name: str, skill_file: str | None) -> list[str]:
    candidates: list[str] = []

    def _add(value: str | None) -> None:
        text = (value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    _add(skill_name)

    normalized = (skill_file or "").replace("\\", "/").strip()
    if normalized:
        parts = [part for part in normalized.split("/") if part]
        if parts:
            tail = parts[-1]
            if tail.lower() == "skill.md" and len(parts) >= 2:
                _add(parts[-2])
            elif "." in tail:
                _add(Path(tail).stem)
            else:
                _add(tail)

    return candidates


def resolve_skill_file_path(skill_name: str, skill_file: str | None) -> str | None:
    """尝试将不可用的 skill_file 映射到本机已安装的同名 Skill。"""
    if not skill_file:
        return None

    raw = skill_file.strip()
    if not raw:
        return None

    path = Path(raw)
    if path.exists():
        return str(path)

    for root in _installed_skill_roots():
        for candidate in _skill_name_candidates(skill_name, raw):
            possible_paths = [
                root / candidate / "SKILL.md",
                root / f"{candidate}.md",
            ]
            for possible in possible_paths:
                if possible.exists():
                    log.warning(
                        "Skill '%s' 的文件不存在: %s，自动改用本机 Skill: %s",
                        skill_name,
                        raw,
                        possible,
                    )
                    return str(possible)

    return raw


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
        command=resolve_cli_command(raw.get("command")),
        base_args=raw.get("base_args", CLIConfig().base_args),
        timeout=int(raw.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
    )


def _default_openai_enabled(raw: dict) -> bool:
    if "enabled" in raw:
        return bool(raw.get("enabled"))
    env_candidates = [
        _env("OPENAI_ENABLED"),
        _env("OPENAI_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
        os.environ.get("ANTHROPIC_AUTH_TOKEN"),
        os.environ.get("ANTHROPIC_API_KEY"),
    ]
    return any(bool((value or "").strip()) for value in env_candidates)


def _parse_openai(raw: dict) -> OpenAIResponsesConfig:
    return OpenAIResponsesConfig(
        enabled=_default_openai_enabled(raw),
        api_key_env=str(raw.get("api_key_env", OpenAIResponsesConfig.api_key_env)).strip() or OpenAIResponsesConfig.api_key_env,
        api_key=str(raw.get("api_key", "")),
        base_url=str(raw.get("base_url", "")),
        model=str(raw.get("model", OpenAIResponsesConfig.model)).strip() or OpenAIResponsesConfig.model,
        api_mode=str(raw.get("api_mode", OpenAIResponsesConfig.api_mode)).strip() or OpenAIResponsesConfig.api_mode,
        timeout=int(raw.get("timeout", OpenAIResponsesConfig.timeout)),
        tool_type=str(raw.get("tool_type", OpenAIResponsesConfig.tool_type)).strip() or OpenAIResponsesConfig.tool_type,
        store=bool(raw.get("store", False)),
        reasoning_effort=str(raw.get("reasoning_effort", OpenAIResponsesConfig.reasoning_effort)).strip() or OpenAIResponsesConfig.reasoning_effort,
        max_tool_rounds=int(raw.get("max_tool_rounds", OpenAIResponsesConfig.max_tool_rounds)),
        shell_timeout_ms=int(raw.get("shell_timeout_ms", OpenAIResponsesConfig.shell_timeout_ms)),
        max_output_chars=int(raw.get("max_output_chars", OpenAIResponsesConfig.max_output_chars)),
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
        timeout_increment_on_timeout=int(raw.get("timeout_increment_on_timeout", 300)),
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
            repo_targets=item.get("repo_targets", []),
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
            skill_file=resolve_skill_file_path(item["name"], item.get("skill_file")),
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


def validate_config(config: AppConfig, *, emit_logs: bool = True) -> list[str]:
    """验证配置的完整性和合理性，返回警告列表。"""
    warnings: list[str] = []

    if not config.tasks:
        warnings.append("没有配置任何 task")

    if not config.skills:
        warnings.append("没有配置任何 skill")

    if config.max_workers < 1:
        warnings.append(f"max_workers 不合理: {config.max_workers}")

    if config.retry.timeout_increment_on_timeout < 0:
        warnings.append(
            f"retry.timeout_increment_on_timeout 不合理: {config.retry.timeout_increment_on_timeout}s"
        )

    if config.cli.timeout < 10:
        warnings.append(f"全局超时过短: {config.cli.timeout}s")
    if config.openai.enabled:
        api_key = (config.openai.api_key or os.environ.get(config.openai.api_key_env, "")).strip()
        if not api_key:
            warnings.append(
                f"OpenAI Responses 已启用，但未找到 API Key：请设置 {config.openai.api_key_env}"
            )
        if config.openai.tool_type not in {"local_shell", "shell"}:
            warnings.append(f"openai.tool_type 非法: {config.openai.tool_type}")
        if config.openai.api_mode not in {"responses", "chat_completions"}:
            warnings.append(f"openai.api_mode 非法: {config.openai.api_mode}")
        if config.openai.max_tool_rounds < 1:
            warnings.append(f"openai.max_tool_rounds 不合理: {config.openai.max_tool_rounds}")
        if config.openai.shell_timeout_ms < 1000:
            warnings.append(f"openai.shell_timeout_ms 过短: {config.openai.shell_timeout_ms}ms")
        if config.openai.max_output_chars < 512:
            warnings.append(f"openai.max_output_chars 过小: {config.openai.max_output_chars}")

    for task in config.tasks:
        if not task.prompt.strip():
            warnings.append(f"任务 '{task.id}' 的 prompt 为空")
        if task.timeout and task.timeout < 10:
            warnings.append(f"任务 '{task.id}' 超时过短: {task.timeout}s")
        if task.mode not in {ExperimentMode.CODING.value, ExperimentMode.SOLUTION.value}:
            warnings.append(f"任务 '{task.id}' 的 mode 非法: {task.mode}")
        if not isinstance(task.repo_targets, list):
            warnings.append(f"任务 '{task.id}' 的 repo_targets 必须是列表")

    for skill in config.skills:
        if skill.skill_file and not Path(skill.skill_file).exists():
            warnings.append(f"Skill '{skill.name}' 的文件不存在: {skill.skill_file}")

    seen_ids = set()
    for task in config.tasks:
        if task.id in seen_ids:
            warnings.append(f"任务 ID 重复: {task.id}")
        seen_ids.add(task.id)

    if warnings and emit_logs:
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
    env_openai_model = _env("OPENAI_MODEL") or os.environ.get("OPENAI_MODEL")
    env_openai_base_url = _env("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    env_openai_key = _env("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    env_openai_enabled = _env("OPENAI_ENABLED")
    env_anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL")
    env_anthropic_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    env_anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    env_anthropic_model = os.environ.get("ANTHROPIC_MODEL")
    env_api_timeout_ms = os.environ.get("API_TIMEOUT_MS")

    cli_raw = raw.get("cli", {})
    if env_command:
        cli_raw["command"] = env_command
    if env_timeout:
        cli_raw["timeout"] = int(env_timeout)

    openai_raw = raw.get("openai", {})
    if env_openai_model:
        openai_raw["model"] = env_openai_model
    if env_openai_base_url:
        openai_raw["base_url"] = env_openai_base_url
    if env_openai_key:
        openai_raw["api_key"] = env_openai_key
    if env_timeout:
        openai_raw["timeout"] = int(env_timeout)
    if env_openai_enabled is not None:
        openai_raw["enabled"] = str(env_openai_enabled).strip().lower() in {"1", "true", "yes", "on"}
    if not openai_raw.get("api_key") and (env_anthropic_auth_token or env_anthropic_api_key):
        openai_raw["api_key"] = env_anthropic_auth_token or env_anthropic_api_key
    if not openai_raw.get("model") and env_anthropic_model:
        openai_raw["model"] = env_anthropic_model
    if not openai_raw.get("base_url") and env_anthropic_base_url:
        if env_anthropic_base_url.rstrip("/").endswith("/anthropic"):
            openai_raw["base_url"] = f"{env_anthropic_base_url.rsplit('/', 1)[0]}/v1"
            openai_raw.setdefault("api_mode", "chat_completions")
    if not openai_raw.get("timeout") and env_api_timeout_ms:
        try:
            openai_raw["timeout"] = max(int(int(env_api_timeout_ms) / 1000), 1)
        except ValueError:
            pass

    git_raw = raw.get("git", {})
    retry_raw = raw.get("retry", {})

    config = AppConfig(
        cli=_parse_cli(cli_raw),
        openai=_parse_openai(openai_raw),
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
        openai=_parse_openai({}),
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
                repo_targets=[],
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


def config_to_dict(config: AppConfig) -> dict:
    data = {
        "cli": {
            "command": config.cli.command,
            "base_args": list(config.cli.base_args),
            "timeout": config.cli.timeout,
        },
        "openai": {
            "enabled": config.openai.enabled,
            "api_key_env": config.openai.api_key_env,
            "base_url": config.openai.base_url,
            "model": config.openai.model,
            "api_mode": config.openai.api_mode,
            "timeout": config.openai.timeout,
            "tool_type": config.openai.tool_type,
            "store": config.openai.store,
            "reasoning_effort": config.openai.reasoning_effort,
            "max_tool_rounds": config.openai.max_tool_rounds,
            "shell_timeout_ms": config.openai.shell_timeout_ms,
            "max_output_chars": config.openai.max_output_chars,
        },
        "git": {
            "author_name": config.git.author_name,
            "author_email": config.git.author_email,
            "base_branch": config.git.base_branch,
            "branch_prefix": config.git.branch_prefix,
            "auto_push": config.git.auto_push,
            "cleanup_on_finish": config.git.cleanup_on_finish,
        },
        "retry": {
            "max_retries": config.retry.max_retries,
            "base_delay": config.retry.base_delay,
            "max_delay": config.retry.max_delay,
            "retry_on_timeout": config.retry.retry_on_timeout,
            "retry_on_failure": config.retry.retry_on_failure,
            "timeout_increment_on_timeout": config.retry.timeout_increment_on_timeout,
        },
        "max_workers": config.max_workers,
        "output_dir": config.output_dir,
        "report_formats": [fmt.value for fmt in config.report_formats],
        "discover_skills": config.discover_skills,
        "discover_path": config.discover_path,
        "tasks": [
            {
                "id": task.id,
                "name": task.name,
                "mode": task.mode,
                "prompt": task.prompt,
                "expected_output": task.expected_output,
                "timeout": task.timeout,
                "repo_targets": task.repo_targets,
            }
            for task in config.tasks
        ],
        "skills": [
            {
                "name": skill.name,
                "system_prompt": skill.system_prompt,
                "skill_file": skill.skill_file,
                "ref_files": list(skill.ref_files),
                "tool": skill.tool,
                "origin": skill.origin,
                "description": skill.description,
            }
            for skill in config.skills
        ],
    }
    return data


def save_config(config: AppConfig, output_path: str | Path) -> Path:
    if not _YAML_AVAILABLE:
        raise ConfigError("需要安装 PyYAML：pip install pyyaml")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(  # type: ignore[name-defined]
            config_to_dict(config),
            f,
            allow_unicode=True,
            sort_keys=False,
        )
    return path
