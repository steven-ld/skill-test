"""统一数据模型 — 贯穿配置、执行、结果、报告全流程。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


# ─── 枚举 ────────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class ReportFormat(str, Enum):
    TEXT = "text"
    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"


class ExperimentMode(str, Enum):
    SOLUTION = "solution"
    CODING = "coding"


# ─── 配置模型 ────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_CODING_TIMEOUT_SECONDS = 1000
DEFAULT_TIMEOUT_INCREMENT_ON_TIMEOUT_SECONDS = 300

@dataclass
class SkillConfig:
    """单个 Skill 的配置。"""
    name: str
    system_prompt: Optional[str] = None
    skill_file: Optional[str] = None  # SKILL.md 路径，自动读取
    ref_files: list[str] = field(default_factory=list)
    tool: str = "manual"
    origin: str = ""
    description: str = ""

    @property
    def is_baseline(self) -> bool:
        return self.system_prompt is None and self.skill_file is None


@dataclass
class TaskConfig:
    """单个测试任务的配置。"""
    id: str
    name: str
    prompt: str
    expected_output: str = ""
    timeout: Optional[int] = None  # 覆盖全局超时
    mode: str = ExperimentMode.CODING.value
    repo_targets: list[str] = field(default_factory=list)


@dataclass
class CLIConfig:
    """Claude Code CLI 配置。"""
    command: str = "claude"
    base_args: list[str] = field(default_factory=lambda: [
        "--print",
        "--dangerously-skip-permissions",
    ])
    timeout: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class OpenAIResponsesConfig:
    """OpenAI Responses API 运行时配置。"""
    enabled: bool = False
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-5.3-codex"
    api_mode: str = "responses"
    timeout: int = DEFAULT_CODING_TIMEOUT_SECONDS
    tool_type: str = "local_shell"
    store: bool = False
    reasoning_effort: str = "medium"
    max_tool_rounds: int = 12
    shell_timeout_ms: int = 120000
    max_output_chars: int = 16000


@dataclass
class GitConfig:
    """Git 相关配置。"""
    author_name: str = "AI Skill Tester"
    author_email: str = "skill-test@local"
    base_branch: str = "main"
    branch_prefix: str = "skill-test"
    auto_push: bool = False
    cleanup_on_finish: bool = True


@dataclass
class RetryConfig:
    """重试配置。"""
    max_retries: int = 2
    base_delay: float = 5.0
    max_delay: float = 60.0
    retry_on_timeout: bool = True
    retry_on_failure: bool = False
    timeout_increment_on_timeout: int = DEFAULT_TIMEOUT_INCREMENT_ON_TIMEOUT_SECONDS


@dataclass
class SkillGroup:
    """多 Skill 组合配置。"""
    name: str
    skills: list[str] = field(default_factory=list)
    compose_mode: str = "chain"  # "merge" | "chain"


@dataclass
class AppConfig:
    """全局应用配置。"""
    cli: CLIConfig = field(default_factory=CLIConfig)
    openai: OpenAIResponsesConfig = field(default_factory=OpenAIResponsesConfig)
    git: GitConfig = field(default_factory=GitConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    tasks: list[TaskConfig] = field(default_factory=list)
    skills: list[SkillConfig] = field(default_factory=list)
    skill_groups: list[SkillGroup] = field(default_factory=list)
    output_dir: str = "results"
    max_workers: int = 3
    report_formats: list[ReportFormat] = field(
        default_factory=lambda: [ReportFormat.TEXT, ReportFormat.JSON]
    )
    discover_skills: bool = False
    discover_path: str = ""


# ─── 运行时模型 ──────────────────────────────────────────────────────────────

@dataclass
class WorktreeInfo:
    """活跃 Worktree 信息。"""
    path: Path
    branch: str
    repo: str
    label: str  # 用于标识此 worktree 对应的 skill/task 组合
    created_at: float = field(default_factory=time.time)


@dataclass
class TaskResult:
    """单次 AI 任务执行结果 — 框架唯一的结果数据类。"""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    task_id: str = ""
    task_name: str = ""
    skill_name: str = ""
    experiment_mode: str = ExperimentMode.CODING.value
    status: TaskStatus = TaskStatus.PENDING
    output: str = ""
    error: str = ""
    duration: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    worktree_branch: str = ""
    files_changed: list[str] = field(default_factory=list)
    change_summary: Optional[dict] = None
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == TaskStatus.SUCCESS

    @property
    def retries(self) -> int:
        return self.metadata.get("retries", 0)

    @property
    def files_added(self) -> int:
        return (self.change_summary or {}).get("files_added", 0)

    @property
    def files_modified(self) -> int:
        return (self.change_summary or {}).get("files_modified", 0)

    @property
    def files_deleted(self) -> int:
        return (self.change_summary or {}).get("files_deleted", 0)

    @property
    def lines_added(self) -> int:
        return (self.change_summary or {}).get("total_lines_added", 0)

    @property
    def lines_deleted(self) -> int:
        return (self.change_summary or {}).get("total_lines_deleted", 0)

    @property
    def commit_hash(self) -> str:
        return self.metadata.get("commit_hash", "")

    @property
    def pushed(self) -> bool:
        return bool(self.metadata.get("pushed"))

    @property
    def deliverable_path(self) -> str:
        return self.metadata.get("deliverable_path", "")

    @property
    def pr_url(self) -> str:
        return self.metadata.get("pr_url", "")

    @property
    def pr_urls(self) -> list[dict]:
        value = self.metadata.get("pr_urls")
        return value if isinstance(value, list) else []

    @property
    def cloud_stored(self) -> bool:
        return bool(self.metadata.get("cloud_stored"))

    @property
    def skill_tool(self) -> str:
        return self.metadata.get("skill_tool", "")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["files_added"] = self.files_added
        d["files_modified"] = self.files_modified
        d["files_deleted"] = self.files_deleted
        d["lines_added"] = self.lines_added
        d["lines_deleted"] = self.lines_deleted
        d["commit_hash"] = self.commit_hash
        d["pushed"] = self.pushed
        d["deliverable_path"] = self.deliverable_path
        d["pr_url"] = self.pr_url
        d["pr_urls"] = self.pr_urls
        d["cloud_stored"] = self.cloud_stored
        d["skill_tool"] = self.skill_tool
        return d


@dataclass
class RunSession:
    """一次完整的测试会话。"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    config_name: str = ""
    mode: str = "isolated"
    experiment_mode: str = "task"
    repo_path: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str = ""
    status: str = "running"
    total_tasks: int = 0
    completed_tasks: int = 0
    results: list[TaskResult] = field(default_factory=list)

    @property
    def progress(self) -> float:
        return (self.completed_tasks / self.total_tasks * 100) if self.total_tasks else 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "config_name": self.config_name,
            "mode": self.mode,
            "experiment_mode": self.experiment_mode,
            "repo_path": self.repo_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "progress": self.progress,
            "results": [r.to_dict() for r in self.results],
        }
