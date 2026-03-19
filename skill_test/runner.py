"""
测试编排器 v4 — 事件驱动架构，集成 diff 分析、实时进度、WebSocket 桥接。

事件总线:
  task_registered(task_id, task_name, skill_name)
  task_started(task_id, skill_name)
  task_completed(TaskResult)
  run_complete(list[TaskResult])
"""

from __future__ import annotations

import queue
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from .executor import ClaudeExecutor
from .git_manager import GitClient, WorktreeManager, CommitManager
from .log import get_logger
from .models import (
    AppConfig, TaskConfig, SkillConfig, TaskResult, TaskStatus,
    WorktreeInfo, RetryConfig, RunSession,
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


def _run_one(
    executor: ClaudeExecutor,
    task: TaskConfig,
    skill: SkillConfig,
    work_dir: str | Path | None,
    retry: RetryConfig | None = None,
) -> TaskResult:
    timeout = task.timeout or executor.config.timeout
    if retry and retry.max_retries > 0:
        result = executor.execute_with_retry(
            prompt=task.prompt, skill=skill,
            task_dir=work_dir, timeout=timeout, retry=retry,
        )
    else:
        result = executor.execute(
            prompt=task.prompt, skill=skill,
            task_dir=work_dir, timeout=timeout,
        )
    result.task_id = task.id
    result.task_name = task.name
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
        repo_path: str | Path | None = None,
        work_dir: str | Path | None = None,
        enable_progress: bool = True,
        enable_history: bool = True,
    ):
        self.config = config
        self.executor = ClaudeExecutor(config.cli)
        self.repo_path = Path(repo_path) if repo_path else None
        self.work_dir = str(work_dir) if work_dir else None
        self.enable_progress = enable_progress and RICH_AVAILABLE
        self.enable_history = enable_history

        self._git: Optional[GitClient] = None
        self._wt_mgr: Optional[WorktreeManager] = None
        self._commit_mgr: Optional[CommitManager] = None

        if self.repo_path:
            self._git = GitClient(self.repo_path)
            self._wt_mgr = WorktreeManager(self._git, config.git)
            self._commit_mgr = CommitManager(self._git, config.git)

        self.events = EventBus()
        self._dashboard: Optional[ProgressDashboard] = None
        self._session: Optional[RunSession] = None

    def on_result(self, callback: Callable[[TaskResult], None]) -> None:
        self.events.on("task_completed", lambda d: callback(d.get("result")))

    def _emit_registered(self, task: TaskConfig, skill: SkillConfig) -> None:
        self.events.emit("task_registered", {
            "task_id": task.id,
            "task_name": task.name,
            "skill_name": skill.name,
            "status": "pending",
        })

    def _emit_started(self, task: TaskConfig, skill: SkillConfig) -> None:
        if self._dashboard:
            self._dashboard.mark_running(task.id, skill.name)
        self.events.emit("task_started", {
            "task_id": task.id,
            "skill_name": skill.name,
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
            "status": result.status.value,
            "duration": result.duration,
            "result": result.to_dict(),
        })

    def _analyze_diff(self, result: TaskResult, cwd: Path) -> None:
        """为 TaskResult 添加详细变更分析。"""
        if not self._git:
            return
        try:
            from .diff_analyzer import analyze_changes
            summary = analyze_changes(self._git, cwd=cwd)
            result.change_summary = summary.to_dict()
            result.files_changed = [
                f"{c.status[0].upper()} {c.path}" for c in summary.changes
            ]
        except Exception as e:
            log.warning("变更分析失败: %s", e)

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

    def run_simple(self, tasks=None, skills=None) -> list[TaskResult]:
        plan = ExecutionPlan(tasks or self.config.tasks, skills or self._resolve_skills())
        log.info("简单模式 | %s", plan)
        results: list[TaskResult] = []
        ctx = self._create_dashboard(plan) if self.enable_progress else _Null()
        with ctx:
            for i, (task, skill) in enumerate(plan.items(), 1):
                self._emit_started(task, skill)
                result = _run_one(self.executor, task, skill, self.work_dir, self.config.retry)
                results.append(result)
                self._emit_completed(result)
        return results

    # ── 并行模式 ─────────────────────────────────────────────────────────

    def run_parallel(self, tasks=None, skills=None, max_workers=None) -> list[TaskResult]:
        plan = ExecutionPlan(tasks or self.config.tasks, skills or self._resolve_skills())
        workers = max_workers or self.config.max_workers
        log.info("并行模式 | %s | workers=%d", plan, workers)
        results: list[TaskResult] = []
        ctx = self._create_dashboard(plan) if self.enable_progress else _Null()
        with ctx:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for task, skill in plan.items():
                    self._emit_started(task, skill)
                    future = pool.submit(_run_one, self.executor, task, skill, self.work_dir, self.config.retry)
                    futures[future] = (task, skill)
                for future in as_completed(futures):
                    task, skill = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        result = TaskResult(task_id=task.id, task_name=task.name, skill_name=skill.name,
                                            status=TaskStatus.FAILED, error=str(e))
                    results.append(result)
                    self._emit_completed(result)
        return results

    # ── 隔离模式 ─────────────────────────────────────────────────────────

    def run_isolated(self, tasks=None, skills=None, *, commit=False, push=False, max_workers=None) -> list[TaskResult]:
        if not self._wt_mgr or not self._git:
            raise RuntimeError("隔离模式需要提供 repo_path")
        plan = ExecutionPlan(tasks or self.config.tasks, skills or self._resolve_skills())
        workers = max_workers or self.config.max_workers
        log.info("隔离模式 | %s | workers=%d", plan, workers)
        results: list[TaskResult] = []
        worktrees: list[WorktreeInfo] = []
        ctx = self._create_dashboard(plan) if self.enable_progress else _Null()
        with ctx:
            try:
                wt_map: dict[tuple[str, str], WorktreeInfo] = {}
                for task, skill in plan.items():
                    label = f"{task.id}-{skill.name}"
                    wt = self._wt_mgr.create(label)
                    wt_map[(task.id, skill.name)] = wt
                    worktrees.append(wt)

                def _do(task, skill):
                    self._emit_started(task, skill)
                    wt = wt_map[(task.id, skill.name)]
                    result = _run_one(self.executor, task, skill, wt.path, self.config.retry)
                    result.worktree_branch = wt.branch
                    self._analyze_diff(result, wt.path)
                    if commit and result.success and result.files_changed:
                        try:
                            self._commit_mgr.commit(wt, f"skill-test: {task.name} [{skill.name}]")  # type: ignore
                            if push:
                                self._commit_mgr.push(wt)  # type: ignore
                            result.metadata["committed"] = True
                        except Exception as e:
                            result.metadata["commit_error"] = str(e)
                    return result

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_do, t, s): (t, s) for t, s in plan.items()}
                    for future in as_completed(futures):
                        task, skill = futures[future]
                        try:
                            result = future.result()
                        except Exception as e:
                            result = TaskResult(task_id=task.id, task_name=task.name,
                                                skill_name=skill.name, status=TaskStatus.FAILED, error=str(e))
                        results.append(result)
                        self._emit_completed(result)
            finally:
                if self.config.git.cleanup_on_finish:
                    for wt in worktrees:
                        try:
                            self._wt_mgr.remove(wt)  # type: ignore
                        except Exception as e:
                            log.warning("清理失败: %s", e)
        return results

    # ── 统一入口 ─────────────────────────────────────────────────────────

    def run(self, *, mode="auto", tasks=None, skills=None, commit=False, push=False) -> list[TaskResult]:
        if mode == "auto":
            mode = "isolated" if self.repo_path else "parallel"

        self._session = RunSession(
            config_name=getattr(self.config, '_config_name', ''),
            mode=mode,
            repo_path=str(self.repo_path or ''),
            total_tasks=len(tasks or self.config.tasks) * len(skills or self.config.skills),
        )
        self.events.emit("run_started", {
            "run_id": self._session.id,
            "mode": mode,
            "total": self._session.total_tasks,
        })

        start = time.monotonic()
        if mode == "simple":
            results = self.run_simple(tasks, skills)
        elif mode == "parallel":
            results = self.run_parallel(tasks, skills)
        elif mode == "isolated":
            results = self.run_isolated(tasks, skills, commit=commit, push=push)
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
        for task, skill in plan.items():
            self._dashboard.register(task.id, task.name, skill.name)
            self._emit_registered(task, skill)
        return self._dashboard

    def _save_history(self, results):
        if not self.enable_history or not results:
            return
        try:
            from .history import HistoryDB
            with HistoryDB(Path(self.config.output_dir) / "history.db") as db:
                db.record(results)
        except Exception as e:
            log.warning("历史记录保存失败: %s", e)


class _Null:
    def __enter__(self): return self
    def __exit__(self, *a): pass
