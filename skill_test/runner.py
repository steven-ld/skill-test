"""
测试编排器 v4 — 事件驱动架构，集成 diff 分析、实时进度、WebSocket 桥接。

事件总线:
  task_registered(task_id, task_name, skill_name)
  task_started(task_id, skill_name)
  task_completed(TaskResult)
  run_complete(list[TaskResult])
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

from .executor import ClaudeExecutor
from .openai_executor import OpenAIResponsesExecutor
from .git_manager import GitClient, WorktreeManager, CommitManager
from .log import get_logger
from .models import (
    AppConfig, TaskConfig, SkillConfig, TaskResult, TaskStatus,
    WorktreeInfo, RetryConfig, RunSession, ExperimentMode, DEFAULT_CODING_TIMEOUT_SECONDS,
)
from .progress import ProgressDashboard, RICH_AVAILABLE

log = get_logger("runner")


class ExecutionPlan:
    """Task × Skill 的执行矩阵。"""

    def __init__(self, tasks: list[TaskConfig], skills: list[SkillConfig]):
        self.tasks = tasks
        self.skills = skills

    @property
    def total_runs(self) -> int:
        return len(self.tasks) * len(self.skills)

    def items(self):
        for task in self.tasks:
            for skill in self.skills:
                yield task, skill

    def __repr__(self) -> str:
        return f"ExecutionPlan(tasks={len(self.tasks)}, skills={len(self.skills)}, total={self.total_runs})"


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    return text.strip("-") or "item"


def _resolve_experiment_mode(task: TaskConfig, experiment_mode: str | None) -> str:
    if experiment_mode in (None, "", "task", "auto"):
        return task.mode or ExperimentMode.CODING.value
    return experiment_mode


def resolve_run_mode(
    mode: str,
    *,
    has_repo: bool,
    commit: bool = False,
    push: bool = False,
) -> str:
    resolved = "isolated" if mode == "auto" and has_repo else ("parallel" if mode == "auto" else mode)
    if commit or push:
        if not has_repo:
            raise ValueError("启用 commit/push 需要提供 repo_path，并使用 isolated 模式。")
        if resolved != "isolated":
            log.info("启用 commit/push，执行模式从 %s 自动切换为 isolated", resolved)
            resolved = "isolated"
    return resolved


def _resolve_timeout(task: TaskConfig, config_timeout: int, experiment_mode: str) -> int:
    if task.timeout:
        return task.timeout
    if experiment_mode == ExperimentMode.CODING.value:
        return max(config_timeout, DEFAULT_CODING_TIMEOUT_SECONDS)
    return config_timeout


def _build_task_prompt(task: TaskConfig, skill: SkillConfig, experiment_mode: str) -> tuple[str, str]:
    deliverable_dir = f".skill-test/deliverables/{_slug(task.id)}"
    skill_slug = _slug(skill.name or "baseline")

    if experiment_mode == ExperimentMode.SOLUTION.value:
        deliverable_path = f"{deliverable_dir}/{skill_slug}-technical-plan.md"
        mode_instructions = (
            "当前执行的是技术方案模式。\n"
            f"请围绕需求产出一份完整、细化、可执行的技术方案，并保存到 `{deliverable_path}`。\n"
            "要求：\n"
            "1. 方案必须包含目标、范围、现状分析、设计拆解、接口/数据影响、实施步骤、风险与验证计划。\n"
            "2. 优先引用仓库中的真实文件路径、模块名称和约束，不要给空泛建议。\n"
            "3. 除了交付方案文档，不要修改业务代码，除非为了让方案文档落盘所需的最小目录或说明文件。\n"
            "4. 最终回复中说明你创建或更新了哪些文件。"
        )
    else:
        deliverable_path = f"{deliverable_dir}/{skill_slug}-delivery.md"
        mode_instructions = (
            "当前执行的是 Coding 模式。\n"
            "请直接完成实现，并确保变更已经写入工作区文件。\n"
            f"完成后请补充一份交付说明到 `{deliverable_path}`，说明改动摘要、影响范围、验证方式和剩余风险。\n"
            "交付结果以 Git 提交/推送记录为准，请保持变更处于可提交状态。\n"
            "不要在最终回复中虚构已提交、已推送、已创建的文件或代码变更；只有在实际完成且可验证时才能声明这些结果。"
        )

    expected = ""
    if task.expected_output:
        expected = f"\n\n期望输出/验收补充：\n{task.expected_output.strip()}"

    prompt = (
        f"{task.prompt.rstrip()}\n\n"
        "---\n\n"
        "以下是测试平台追加的执行约束，请一并遵守：\n"
        f"{mode_instructions}"
        f"{expected}\n"
    )
    return prompt, deliverable_path


def _resolve_deliverable_file(work_dir: Path, deliverable_path: str) -> Path | None:
    if not deliverable_path:
        return None
    path = Path(deliverable_path)
    return path if path.is_absolute() else work_dir / path


def _validate_result_artifacts(
    result: TaskResult,
    *,
    work_dir: Path,
    experiment_mode: str,
    commit_requested: bool = False,
    push_requested: bool = False,
) -> TaskResult:
    if not result.success:
        return result

    validation_errors: list[str] = []
    files_count = len(result.files_changed) or int((result.change_summary or {}).get("total_files", 0) or 0)
    result.metadata["files_detected"] = files_count

    deliverable_file = _resolve_deliverable_file(work_dir, result.deliverable_path)
    deliverable_exists = bool(deliverable_file and deliverable_file.is_file())
    result.metadata["deliverable_exists"] = deliverable_exists
    if deliverable_file:
        result.metadata["deliverable_file"] = str(deliverable_file)
        if not deliverable_exists:
            validation_errors.append(f"交付文件未生成: {result.deliverable_path}")

    if experiment_mode == ExperimentMode.CODING.value and files_count <= 0:
        validation_errors.append("Coding 模式未检测到任何文件变更")

    if commit_requested and not result.commit_hash:
        commit_error = result.metadata.get("commit_error")
        if commit_error:
            validation_errors.append(f"Git 提交失败: {commit_error.splitlines()[0]}")
        else:
            validation_errors.append("已启用 commit，但未生成有效提交")

    if push_requested and not result.pushed:
        validation_errors.append("已启用 push，但未完成推送")

    if not validation_errors:
        return result

    result.metadata["original_status"] = result.status.value
    result.metadata["validation_errors"] = validation_errors
    result.status = TaskStatus.FAILED
    result.error = "；".join(validation_errors)
    log.warning(
        "结果校验失败 | task=%s | skill=%s | %s",
        result.task_id or result.task_name,
        result.skill_name,
        result.error,
    )
    return result


def _can_publish_pr_link(result: TaskResult) -> bool:
    if not result.success:
        return False
    if result.metadata.get("cloud_store_requested"):
        return bool(result.metadata.get("cloud_stored"))
    return True


def _run_one(
    executor: Any,
    task: TaskConfig,
    skill: SkillConfig,
    work_dir: str | Path | None,
    experiment_mode: str,
    retry: RetryConfig | None = None,
) -> TaskResult:
    resolved_mode = _resolve_experiment_mode(task, experiment_mode)
    timeout = _resolve_timeout(task, executor.config.timeout, resolved_mode)
    prompt, deliverable_path = _build_task_prompt(task, skill, resolved_mode)
    if retry and retry.max_retries > 0:
        result = executor.execute_with_retry(
            prompt=prompt, skill=skill,
            task_dir=work_dir, timeout=timeout, retry=retry,
        )
    else:
        result = executor.execute(
            prompt=prompt, skill=skill,
            task_dir=work_dir, timeout=timeout,
        )
    result.task_id = task.id
    result.task_name = task.name
    result.experiment_mode = resolved_mode
    result.metadata.setdefault("deliverable_path", deliverable_path)
    result.metadata.setdefault("skill_tool", skill.tool)
    result.metadata.setdefault("skill_origin", skill.origin)
    return result


class EventBus:
    """线程安全的事件总线 — 支持同步回调 + 异步队列（WebSocket 桥接）。"""

    def __init__(self):
        self._callbacks: dict[str, list[Callable]] = {}
        self._queues: list[queue.Queue] = []

    def on(self, event: str, callback: Callable) -> None:
        self._callbacks.setdefault(event, []).append(callback)

    def create_queue(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        self._queues.append(q)
        return q

    def remove_queue(self, q: queue.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def emit(self, event: str, data: dict) -> None:
        data["event"] = event
        data["timestamp"] = time.time()

        for cb in self._callbacks.get(event, []):
            try:
                cb(data)
            except Exception as e:
                log.warning("事件回调异常 [%s]: %s", event, e)

        for q in self._queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


class TestRunner:
    """测试运行编排器 v4 — 事件驱动，集成 diff 分析。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        repo_path: str | Path | list[str] | list[Path] | None = None,
        work_dir: str | Path | None = None,
        enable_progress: bool = True,
        enable_history: bool = True,
    ):
        self.config = config
        self.executor = OpenAIResponsesExecutor(config.openai) if config.openai.enabled else ClaudeExecutor(config.cli)
        if repo_path and isinstance(repo_path, (list, tuple)):
            self.repo_paths = [Path(path) for path in repo_path]
        elif repo_path:
            self.repo_paths = [Path(repo_path)]
        else:
            self.repo_paths = []
        self.repo_path = self.repo_paths[0] if self.repo_paths else None
        self.work_dir = str(work_dir) if work_dir else None
        self.enable_progress = enable_progress and RICH_AVAILABLE
        self.enable_history = enable_history

        self._git: Optional[GitClient] = None
        self._wt_mgr: Optional[WorktreeManager] = None
        self._commit_mgr: Optional[CommitManager] = None
        self._git_clients: dict[str, GitClient] = {}
        self._wt_mgrs: dict[str, WorktreeManager] = {}
        self._commit_mgrs: dict[str, CommitManager] = {}

        for repo in self.repo_paths:
            git = GitClient(repo)
            key = str(repo.resolve())
            self._git_clients[key] = git
            self._wt_mgrs[key] = WorktreeManager(git, config.git)
            self._commit_mgrs[key] = CommitManager(git, config.git)

        if self.repo_path:
            primary_key = str(self.repo_path.resolve())
            self._git = self._git_clients[primary_key]
            self._wt_mgr = self._wt_mgrs[primary_key]
            self._commit_mgr = self._commit_mgrs[primary_key]

        self.events = EventBus()
        self._dashboard: Optional[ProgressDashboard] = None
        self._session: Optional[RunSession] = None

    def on_result(self, callback: Callable[[TaskResult], None]) -> None:
        self.events.on("task_completed", lambda d: callback(d.get("result")))

    def _emit_registered(self, task: TaskConfig, skill: SkillConfig, experiment_mode: str) -> None:
        self.events.emit("task_registered", {
            "task_id": task.id,
            "task_name": task.name,
            "skill_name": skill.name,
            "experiment_mode": _resolve_experiment_mode(task, experiment_mode),
            "skill_tool": skill.tool,
            "status": "pending",
        })

    def _emit_started(self, task: TaskConfig, skill: SkillConfig, experiment_mode: str) -> None:
        if self._dashboard:
            self._dashboard.mark_running(task.id, skill.name)
        self.events.emit("task_started", {
            "task_id": task.id,
            "skill_name": skill.name,
            "experiment_mode": _resolve_experiment_mode(task, experiment_mode),
            "skill_tool": skill.tool,
            "status": "running",
        })

    def _emit_completed(self, result: TaskResult) -> None:
        if self._dashboard:
            self._dashboard.mark_complete(result)
        if self._session:
            self._session.completed_tasks += 1
            self._session.results.append(result)
        self.events.emit("task_completed", {
            "task_id": result.task_id,
            "skill_name": result.skill_name,
            "experiment_mode": result.experiment_mode,
            "status": result.status.value,
            "duration": result.duration,
            "result": result.to_dict(),
        })

    def _analyze_diff(self, result: TaskResult, cwd: Path, *, repo: Path | None = None) -> None:
        """为 TaskResult 添加详细变更分析。"""
        target_repo = repo or self.repo_path
        if not target_repo:
            return
        git = self._git_clients.get(str(target_repo.resolve()))
        if not git:
            return
        try:
            from .diff_analyzer import analyze_changes
            summary = analyze_changes(git, cwd=cwd)
            result.change_summary = summary.to_dict()
            result.files_changed = [
                f"{c.status[0].upper()} {c.path}" for c in summary.changes
            ]
        except Exception as e:
            log.warning("变更分析失败: %s", e)

    def _collect_repo_summary(self, repo: Path, cwd: Path) -> dict | None:
        git = self._git_clients.get(str(repo.resolve()))
        if not git:
            return None
        try:
            from .diff_analyzer import analyze_changes
            return analyze_changes(git, cwd=cwd).to_dict()
        except Exception as e:
            log.warning("多仓库变更分析失败 [%s]: %s", repo, e)
            return None

    def _merge_repo_summaries(self, repo_summaries: list[dict]) -> tuple[dict | None, list[str]]:
        if not repo_summaries:
            return None, []

        merged = {
            "files_added": 0,
            "files_modified": 0,
            "files_deleted": 0,
            "files_renamed": 0,
            "total_files": 0,
            "total_lines_added": 0,
            "total_lines_deleted": 0,
            "net_lines": 0,
            "changes": [],
        }
        files_changed: list[str] = []

        for item in repo_summaries:
            repo_name = Path(item["repo"]).name
            summary = item.get("change_summary") or {}
            merged["files_added"] += summary.get("files_added", 0)
            merged["files_modified"] += summary.get("files_modified", 0)
            merged["files_deleted"] += summary.get("files_deleted", 0)
            merged["files_renamed"] += summary.get("files_renamed", 0)
            merged["total_files"] += summary.get("total_files", 0)
            merged["total_lines_added"] += summary.get("total_lines_added", 0)
            merged["total_lines_deleted"] += summary.get("total_lines_deleted", 0)
            merged["net_lines"] += summary.get("net_lines", 0)
            for change in summary.get("changes", []):
                repo_change = dict(change)
                repo_change["path"] = f"{repo_name}/{change.get('path', '')}"
                merged["changes"].append(repo_change)
                files_changed.append(f"{change.get('status', 'M')[:1].upper()} {repo_name}/{change.get('path', '')}")

        return merged, files_changed

    def _select_task_repos(self, task: TaskConfig) -> list[Path]:
        if len(self.repo_paths) <= 1:
            return list(self.repo_paths)

        target_names = {target.strip().lower() for target in task.repo_targets if target.strip()}
        if target_names:
            selected = [repo for repo in self.repo_paths if repo.name.lower() in target_names]
            if selected:
                return selected

        haystack = " ".join(
            part for part in [task.id, task.name, task.prompt, task.expected_output] if part
        ).lower()
        matched = [repo for repo in self.repo_paths if repo.name.lower() in haystack]
        if matched:
            return matched

        normalized_haystack = haystack.replace("-", "").replace("_", "")
        normalized = [
            repo for repo in self.repo_paths
            if repo.name.lower().replace("-", "").replace("_", "") in normalized_haystack
        ]
        return normalized or list(self.repo_paths)

    def _prepare_multi_repo_workspace(
        self,
        repos: list[Path],
        task: TaskConfig,
        skill: SkillConfig,
    ) -> tuple[Path, dict[str, WorktreeInfo]]:
        workspace_root = Path(tempfile.mkdtemp(prefix=f"skill_test_{_slug(task.id)}_{_slug(skill.name)}_"))
        repo_worktrees: dict[str, WorktreeInfo] = {}
        common_root = None
        try:
            common_root = Path(os.path.commonpath([str(repo.parent) for repo in repos]))
        except ValueError:
            common_root = None

        if common_root and common_root.exists():
            selected_names = {repo.name for repo in repos}
            for entry in common_root.iterdir():
                if entry.name in selected_names:
                    continue
                target = workspace_root / entry.name
                try:
                    target.symlink_to(entry, target_is_directory=entry.is_dir())
                except Exception:
                    continue

        for repo in repos:
            repo_key = str(repo.resolve())
            wt_mgr = self._wt_mgrs[repo_key]
            label = f"{task.id}-{skill.name}-{repo.name}"
            repo_worktrees[repo_key] = wt_mgr.create(
                label,
                worktree_dir=workspace_root / repo.name,
            )

        return workspace_root, repo_worktrees

    def _resolve_skills(self) -> list[SkillConfig]:
        skills = list(self.config.skills)
        if self.config.discover_skills and self.config.discover_path:
            from .discovery import discover_skills
            discovered = discover_skills(self.config.discover_path)
            existing = {s.name for s in skills}
            for s in discovered:
                if s.name not in existing:
                    skills.append(s)
        if self.config.skill_groups:
            from .discovery import compose_skills
            skill_map = {s.name: s for s in skills}
            for group in self.config.skill_groups:
                members = [skill_map[n] for n in group.skills if n in skill_map]
                if members:
                    skills.append(compose_skills(members, name=group.name, mode=group.compose_mode))
        return skills

    # ── 简单模式 ─────────────────────────────────────────────────────────

    def run_simple(self, tasks=None, skills=None, *, experiment_mode="task") -> list[TaskResult]:
        plan = ExecutionPlan(tasks or self.config.tasks, skills or self._resolve_skills())
        log.info("简单模式 | %s", plan)
        results: list[TaskResult] = []
        ctx = self._create_dashboard(plan) if self.enable_progress else _Null()
        with ctx:
            self._register_plan(plan, experiment_mode)
            for i, (task, skill) in enumerate(plan.items(), 1):
                self._emit_started(task, skill, experiment_mode)
                result = _run_one(
                    self.executor,
                    task,
                    skill,
                    self.work_dir,
                    experiment_mode,
                    self.config.retry,
                )
                results.append(result)
                self._emit_completed(result)
        return results

    # ── 并行模式 ─────────────────────────────────────────────────────────

    def run_parallel(self, tasks=None, skills=None, *, experiment_mode="task", max_workers=None) -> list[TaskResult]:
        plan = ExecutionPlan(tasks or self.config.tasks, skills or self._resolve_skills())
        workers = max_workers or self.config.max_workers
        log.info("并行模式 | %s | workers=%d", plan, workers)
        results: list[TaskResult] = []
        ctx = self._create_dashboard(plan) if self.enable_progress else _Null()
        with ctx:
            self._register_plan(plan, experiment_mode)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for task, skill in plan.items():
                    self._emit_started(task, skill, experiment_mode)
                    future = pool.submit(
                        _run_one,
                        self.executor,
                        task,
                        skill,
                        self.work_dir,
                        experiment_mode,
                        self.config.retry,
                    )
                    futures[future] = (task, skill)
                for future in as_completed(futures):
                    task, skill = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        result = TaskResult(
                            task_id=task.id,
                            task_name=task.name,
                            skill_name=skill.name,
                            experiment_mode=_resolve_experiment_mode(task, experiment_mode),
                            status=TaskStatus.FAILED,
                            error=str(e),
                            metadata={"skill_tool": skill.tool, "skill_origin": skill.origin},
                        )
                    results.append(result)
                    self._emit_completed(result)
        return results

    # ── 隔离模式 ─────────────────────────────────────────────────────────

    def run_isolated(
        self,
        tasks=None,
        skills=None,
        *,
        experiment_mode="task",
        commit=False,
        push=False,
        max_workers=None,
    ) -> list[TaskResult]:
        if not self.repo_paths:
            raise RuntimeError("隔离模式需要提供 repo_path")
        plan = ExecutionPlan(tasks or self.config.tasks, skills or self._resolve_skills())
        workers = max_workers or self.config.max_workers
        log.info("隔离模式 | %s | workers=%d", plan, workers)
        results: list[TaskResult] = []
        worktrees: list[WorktreeInfo] = []
        workspace_roots: list[Path] = []
        ctx = self._create_dashboard(plan) if self.enable_progress else _Null()
        with ctx:
            self._register_plan(plan, experiment_mode)
            try:
                wt_map: dict[tuple[str, str], object] = {}
                for task, skill in plan.items():
                    task_repos = self._select_task_repos(task)
                    if len(task_repos) == 1:
                        repo_key = str(task_repos[0].resolve())
                        label = f"{task.id}-{skill.name}"
                        wt = self._wt_mgrs[repo_key].create(label)
                        wt_map[(task.id, skill.name)] = wt
                        worktrees.append(wt)
                    else:
                        workspace_root, repo_worktrees = self._prepare_multi_repo_workspace(task_repos, task, skill)
                        wt_map[(task.id, skill.name)] = {
                            "workspace_root": workspace_root,
                            "worktrees": repo_worktrees,
                        }
                        workspace_roots.append(workspace_root)
                        worktrees.extend(repo_worktrees.values())

                def _do(task, skill):
                    self._emit_started(task, skill, experiment_mode)
                    wt_info = wt_map[(task.id, skill.name)]
                    resolved_mode = _resolve_experiment_mode(task, experiment_mode)

                    if isinstance(wt_info, WorktreeInfo):
                        repo_key = str(Path(wt_info.repo).resolve())
                        repo_path = Path(repo_key)
                        result = _run_one(
                            self.executor,
                            task,
                            skill,
                            wt_info.path,
                            experiment_mode,
                            self.config.retry,
                        )
                        result.worktree_branch = wt_info.branch
                        self._analyze_diff(result, wt_info.path, repo=repo_path)
                        if commit and result.success and result.files_changed:
                            try:
                                commit_hash = self._commit_mgrs[repo_key].commit(
                                    wt_info,
                                    f"skill-test: {task.name} [{skill.name}]",
                                )
                                result.metadata["commit_hash"] = commit_hash
                                result.metadata["committed"] = bool(commit_hash)
                                if push:
                                    self._commit_mgrs[repo_key].push(wt_info)
                                    result.metadata["pushed"] = True
                                    if _can_publish_pr_link(result):
                                        pr_url = self._commit_mgrs[repo_key].build_pr_url(wt_info)
                                        if pr_url:
                                            result.metadata["pr_url"] = pr_url
                            except Exception as e:
                                result.metadata["commit_error"] = str(e)
                        elif commit:
                            result.metadata["committed"] = False
                        return _validate_result_artifacts(
                            result,
                            work_dir=wt_info.path,
                            experiment_mode=resolved_mode,
                            commit_requested=commit,
                            push_requested=push,
                        )

                    workspace_root = wt_info["workspace_root"]
                    repo_worktrees = wt_info["worktrees"]
                    result = _run_one(
                        self.executor,
                        task,
                        skill,
                        workspace_root,
                        experiment_mode,
                        self.config.retry,
                    )

                    repo_summaries: list[dict] = []
                    commit_hashes: list[str] = []
                    pushed_any = False
                    branches: list[str] = []

                    for repo_key, repo_wt in repo_worktrees.items():
                        repo_path = Path(repo_key)
                        branches.append(repo_wt.branch)
                        summary = self._collect_repo_summary(repo_path, repo_wt.path)
                        repo_record = {
                            "repo": repo_key,
                            "branch": repo_wt.branch,
                            "change_summary": summary,
                        }
                        if commit and result.success and summary and summary.get("total_files", 0) > 0:
                            try:
                                commit_hash = self._commit_mgrs[repo_key].commit(
                                    repo_wt,
                                    f"skill-test: {task.name} [{skill.name}]",
                                )
                                repo_record["commit_hash"] = commit_hash
                                if commit_hash:
                                    commit_hashes.append(commit_hash)
                                if push and commit_hash:
                                    self._commit_mgrs[repo_key].push(repo_wt)
                                    repo_record["pushed"] = True
                                    pushed_any = True
                                    if _can_publish_pr_link(result):
                                        repo_pr_url = self._commit_mgrs[repo_key].build_pr_url(repo_wt)
                                        if repo_pr_url:
                                            repo_record["pr_url"] = repo_pr_url
                            except Exception as e:
                                repo_record["commit_error"] = str(e)
                        repo_summaries.append(repo_record)

                    merged_summary, files_changed = self._merge_repo_summaries(repo_summaries)
                    result.worktree_branch = ", ".join(branches)
                    result.change_summary = merged_summary
                    result.files_changed = files_changed
                    result.metadata["repo_changes"] = repo_summaries
                    result.metadata["commit_hashes"] = commit_hashes
                    result.metadata["commit_hash"] = ", ".join(hash_[:8] for hash_ in commit_hashes)
                    result.metadata["committed"] = bool(commit_hashes)
                    result.metadata["pushed"] = pushed_any
                    pr_urls = [
                        {
                            "repo": item.get("repo", ""),
                            "branch": item.get("branch", ""),
                            "url": item.get("pr_url", ""),
                        }
                        for item in repo_summaries
                        if item.get("pr_url")
                    ]
                    if pr_urls:
                        result.metadata["pr_urls"] = pr_urls
                        if len(pr_urls) == 1:
                            result.metadata["pr_url"] = pr_urls[0]["url"]
                    return _validate_result_artifacts(
                        result,
                        work_dir=workspace_root,
                        experiment_mode=resolved_mode,
                        commit_requested=commit,
                        push_requested=push,
                    )

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_do, t, s): (t, s) for t, s in plan.items()}
                    for future in as_completed(futures):
                        task, skill = futures[future]
                        try:
                            result = future.result()
                        except Exception as e:
                            result = TaskResult(
                                task_id=task.id,
                                task_name=task.name,
                                skill_name=skill.name,
                                experiment_mode=_resolve_experiment_mode(task, experiment_mode),
                                status=TaskStatus.FAILED,
                                error=str(e),
                                metadata={"skill_tool": skill.tool, "skill_origin": skill.origin},
                            )
                        results.append(result)
                        self._emit_completed(result)
            finally:
                if self.config.git.cleanup_on_finish:
                    for wt in worktrees:
                        try:
                            repo_key = str(Path(wt.repo).resolve())
                            self._wt_mgrs[repo_key].remove(wt)
                        except Exception as e:
                            log.warning("清理失败: %s", e)
                    for workspace_root in workspace_roots:
                        shutil.rmtree(workspace_root, ignore_errors=True)
        return results

    # ── 统一入口 ─────────────────────────────────────────────────────────

    def run(
        self,
        *,
        mode="isolated",
        experiment_mode="task",
        tasks=None,
        skills=None,
        commit=False,
        push=False,
    ) -> list[TaskResult]:
        mode = resolve_run_mode(
            mode,
            has_repo=bool(self.repo_paths),
            commit=commit,
            push=push,
        )

        self._session = RunSession(
            config_name=getattr(self.config, '_config_name', ''),
            mode=mode,
            experiment_mode=experiment_mode,
            repo_path=", ".join(str(path) for path in self.repo_paths),
            total_tasks=len(tasks or self.config.tasks) * len(skills or self.config.skills),
        )
        self.events.emit("run_started", {
            "run_id": self._session.id,
            "mode": mode,
            "experiment_mode": experiment_mode,
            "total": self._session.total_tasks,
        })

        start = time.monotonic()
        if mode == "simple":
            results = self.run_simple(tasks, skills, experiment_mode=experiment_mode)
        elif mode == "parallel":
            results = self.run_parallel(tasks, skills, experiment_mode=experiment_mode)
        elif mode == "isolated":
            results = self.run_isolated(
                tasks,
                skills,
                experiment_mode=experiment_mode,
                commit=commit,
                push=push,
            )
        else:
            raise ValueError(f"未知模式: {mode}")

        elapsed = time.monotonic() - start
        self._session.status = "completed"
        self._session.completed_at = __import__("datetime").datetime.now().isoformat()
        ok = sum(1 for r in results if r.success)
        log.info("完成 | 总计=%d | 成功=%d | 失败=%d | %.1fs", len(results), ok, len(results) - ok, elapsed)

        self.events.emit("run_complete", {
            "run_id": self._session.id,
            "total": len(results),
            "success": ok,
            "failed": len(results) - ok,
            "experiment_mode": experiment_mode,
            "elapsed": round(elapsed, 1),
            "results": [r.to_dict() for r in results],
        })

        self._save_history(results)
        return results

    @property
    def session(self) -> Optional[RunSession]:
        return self._session

    def _create_dashboard(self, plan):
        self._dashboard = ProgressDashboard(total=plan.total_runs, title="AI Skill 测试")
        return self._dashboard

    def _register_plan(self, plan: ExecutionPlan, experiment_mode: str) -> None:
        for task, skill in plan.items():
            if self._dashboard:
                self._dashboard.register(task.id, task.name, skill.name)
            self._emit_registered(task, skill, experiment_mode)

    def _save_history(self, results):
        if not self.enable_history or not results:
            return
        try:
            from .history import HistoryDB
            with HistoryDB(Path(self.config.output_dir) / "history.db") as db:
                db.record(results, session_id=self._session.id if self._session else "")
        except Exception as e:
            log.warning("历史记录保存失败: %s", e)


class _Null:
    def __enter__(self): return self
    def __exit__(self, *a): pass
