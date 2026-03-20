"""
Git 操作统一模块 — Worktree 生命周期 + 变更检测 + 提交推送。

所有 Git 子进程调用集中在此，其他模块不直接 subprocess git。
"""

from __future__ import annotations

import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, TYPE_CHECKING

from .exceptions import GitError
from .log import get_logger
from .models import GitConfig, WorktreeInfo

if TYPE_CHECKING:
    from .models import TaskConfig

log = get_logger("git")


def is_git_repo(path: str | Path) -> bool:
    repo = Path(path).resolve()
    return (repo / ".git").exists() or repo.name.endswith(".git")


def discover_git_repos(root: str | Path, *, max_depth: int = 3) -> list[Path]:
    """
    扫描工作区下的 Git 仓库。

    默认只看较浅层目录，适配微服务根目录场景。
    """
    root_path = Path(root).resolve()
    if not root_path.exists() or not root_path.is_dir():
        return []

    if is_git_repo(root_path):
        return [root_path]

    repos: list[Path] = []
    for current, dirs, _files in __import__("os").walk(root_path):
        current_path = Path(current)
        depth = len(current_path.relative_to(root_path).parts)
        if depth > max_depth:
            dirs[:] = []
            continue

        if ".git" in dirs:
            repos.append(current_path)
            dirs[:] = []

    return sorted(repos)


def _score_repo_match(repo: Path, tasks: list["TaskConfig"]) -> int:
    repo_name = repo.name.lower()
    score = 0
    for task in tasks:
        for target in task.repo_targets:
            if target and repo_name == target.lower():
                score += 8
        haystacks = [task.id, task.name, task.prompt, task.expected_output]
        for text in haystacks:
            if not text:
                continue
            lowered = text.lower()
            if repo_name in lowered:
                score += 3
            normalized = repo_name.replace("-", "").replace("_", "")
            if normalized and normalized in lowered.replace("-", "").replace("_", ""):
                score += 1
    return score


def resolve_git_repo(
    repo_path: str | Path,
    *,
    tasks: list["TaskConfig"] | None = None,
    work_dir: str | Path | None = None,
) -> Path:
    """
    将用户输入的 repo_path 解析成真正的 Git 仓库。

    支持：
    - 直接传入 Git 仓库
    - 传入微服务工作区根目录，再从子目录中推断目标仓库
    - 传入 work_dir，优先从工作目录反推出所属仓库
    """
    root = Path(repo_path).resolve()
    if not root.exists():
        raise GitError(f"路径不存在：{root}")

    if is_git_repo(root):
        return root

    if work_dir:
        work_path = Path(work_dir).resolve()
        for candidate in [work_path, *work_path.parents]:
            if candidate == candidate.parent:
                break
            if is_git_repo(candidate):
                return candidate
            if candidate == root:
                break

    repos = discover_git_repos(root)
    if not repos:
        raise GitError(f"目录下未发现 Git 仓库：{root}")

    if len(repos) == 1:
        return repos[0]

    tasks = tasks or []
    scored = sorted(
        ((repo, _score_repo_match(repo, tasks)) for repo in repos),
        key=lambda item: item[1],
        reverse=True,
    )

    if scored and scored[0][1] > 0:
        top_repo, top_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else -1
        if top_score > second_score:
            log.info("从工作区根目录自动匹配子仓库: %s", top_repo)
            return top_repo

    repo_names = ", ".join(repo.name for repo in repos[:8])
    if len(repos) > 8:
        repo_names += ", ..."
    raise GitError(
        f"'{root}' 不是 Git 仓库，但检测到多个子仓库：{repo_names}。"
        " 请在工作目录中指定目标模块，或让任务描述包含更明确的模块名。"
    )


def resolve_git_repos(
    repo_path: str | Path,
    *,
    tasks: list["TaskConfig"] | None = None,
    work_dir: str | Path | None = None,
    explicit_repo_paths: list[str] | None = None,
) -> list[Path]:
    """
    解析一次任务运行需要使用的一个或多个 Git 仓库。
    """
    if explicit_repo_paths:
        resolved: list[Path] = []
        for path in explicit_repo_paths:
            repo = Path(path).resolve()
            if not is_git_repo(repo):
                raise GitError(f"显式选择的路径不是 Git 仓库：{repo}")
            if repo not in resolved:
                resolved.append(repo)
        return resolved

    root = Path(repo_path).resolve()
    tasks = tasks or []

    if not root.exists():
        raise GitError(f"路径不存在：{root}")

    if is_git_repo(root):
        return [root]

    repos = discover_git_repos(root)
    if not repos:
        raise GitError(f"目录下未发现 Git 仓库：{root}")

    scored = [(repo, _score_repo_match(repo, tasks)) for repo in repos]
    matched = sorted(repo for repo, score in scored if score > 0)
    if matched:
        return matched

    if work_dir:
        work_path = Path(work_dir).resolve()
        for candidate in [work_path, *work_path.parents]:
            if candidate == candidate.parent:
                break
            if is_git_repo(candidate):
                return [candidate.resolve()]
            if candidate == root:
                break

    if len(repos) == 1:
        return repos

    repo_names = ", ".join(repo.name for repo in repos[:8])
    if len(repos) > 8:
        repo_names += ", ..."
    raise GitError(
        f"'{root}' 不是 Git 仓库，但检测到多个子仓库：{repo_names}。"
        " 请勾选目标模块，或在任务配置中补充 repo_targets。"
    )


class GitClient:
    """底层 Git 命令封装，提供统一的错误处理和日志。"""

    def __init__(self, repo: str | Path):
        self.repo = Path(repo).resolve()
        if not (self.repo / ".git").exists() and not self.repo.name.endswith(".git"):
            raise GitError(f"非 Git 仓库：{self.repo}")

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """
        执行 git 命令。

        Args:
            args:  git 子命令及参数（不含 'git'）
            cwd:   工作目录，默认 self.repo
            check: 为 True 时若返回码非 0 则抛 GitError
        """
        work_dir = str(cwd or self.repo)
        cmd = ["git"] + args
        log.debug("exec: %s  (cwd=%s)", " ".join(cmd), work_dir)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=work_dir,
            encoding="utf-8",
        )

        if check and result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} 失败 (code={result.returncode})",
                stderr=result.stderr.strip(),
            )
        return result

    # ── 常用快捷方法 ─────────────────────────────────────────────────────

    def current_branch(self, cwd: str | Path | None = None) -> str:
        r = self.run(["branch", "--show-current"], cwd=cwd, check=False)
        return r.stdout.strip()

    def changed_files(self, cwd: str | Path | None = None) -> list[dict[str, str]]:
        r = self.run(["status", "--porcelain"], cwd=cwd, check=False)
        files = []
        for line in r.stdout.strip().splitlines():
            if line:
                files.append({"status": line[:2].strip(), "file": line[3:].strip()})
        return files

    def head_hash(self, cwd: str | Path | None = None) -> str:
        r = self.run(["rev-parse", "HEAD"], cwd=cwd)
        return r.stdout.strip()


class WorktreeManager:
    """
    Worktree 生命周期管理。

    支持上下文管理器自动清理：
        with wt_mgr.create(...) as wt:
            # 在 wt.path 中操作
        # 自动清理
    """

    def __init__(self, git: GitClient, config: GitConfig | None = None):
        self.git = git
        self.config = config or GitConfig()
        self._active: list[WorktreeInfo] = []

    def create(
        self,
        label: str,
        *,
        base_ref: str | None = None,
        worktree_dir: str | Path | None = None,
    ) -> WorktreeInfo:
        """
        创建新 Worktree 并返回信息。

        Args:
            label:         标识标签（会出现在分支名和目录名中）
            base_ref:      基于哪个 ref 创建，默认 origin/<base_branch>
            worktree_dir:  自定义 worktree 目录
        """
        uid = uuid.uuid4().hex[:8]
        safe_label = label.replace("/", "-").replace(" ", "-").lower()
        branch = f"{self.config.branch_prefix}/{safe_label}-{uid}"
        base = base_ref or f"origin/{self.config.base_branch}"

        if worktree_dir:
            wt_path = Path(worktree_dir).resolve()
        else:
            wt_path = self.git.repo / "worktrees" / f"{safe_label}_{uid}"

        wt_path.parent.mkdir(parents=True, exist_ok=True)

        self.git.run([
            "worktree", "add",
            "-b", branch,
            str(wt_path),
            base,
        ])

        info = WorktreeInfo(
            path=wt_path,
            branch=branch,
            repo=str(self.git.repo),
            label=label,
        )
        self._active.append(info)
        log.info("Worktree 已创建: %s → %s", branch, wt_path)
        return info

    @contextmanager
    def managed(
        self, label: str, **kwargs
    ) -> Generator[WorktreeInfo, None, None]:
        """上下文管理器：自动创建并在退出时清理。"""
        wt = self.create(label, **kwargs)
        try:
            yield wt
        finally:
            if self.config.cleanup_on_finish:
                self.remove(wt)

    def remove(self, wt: WorktreeInfo, *, force: bool = True) -> None:
        """移除指定 Worktree 及其分支。"""
        args = ["worktree", "remove", str(wt.path)]
        if force:
            args.append("--force")
        self.git.run(args, check=False)
        self.git.run(["branch", "-D", wt.branch], check=False)

        if wt in self._active:
            self._active.remove(wt)
        log.info("Worktree 已移除: %s", wt.branch)

    def cleanup_all(self) -> None:
        """清理所有活跃 Worktree。"""
        for wt in self._active[:]:
            try:
                self.remove(wt)
            except Exception as e:
                log.warning("清理失败 %s: %s", wt.path, e)

    @property
    def active(self) -> list[WorktreeInfo]:
        return self._active.copy()


class CommitManager:
    """提交 + 推送操作。"""

    def __init__(self, git: GitClient, config: GitConfig | None = None):
        self.git = git
        self.config = config or GitConfig()

    def commit(
        self,
        wt: WorktreeInfo,
        message: str,
        *,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> str:
        """
        提交 worktree 中的变更。

        Returns:
            commit hash，无变更则返回空字符串
        """
        cwd = wt.path
        exclude = set(exclude_patterns or ["worktrees/"])
        changes = self.git.changed_files(cwd=cwd)

        filenames = []
        for c in changes:
            if any(c["file"].startswith(ex) for ex in exclude):
                continue
            if include_patterns and not any(c["file"].startswith(inc) for inc in include_patterns):
                continue
            filenames.append(c["file"])

        if not filenames:
            log.warning("无可提交文件")
            return ""

        self.git.run(
            ["config", "user.name", self.config.author_name], cwd=cwd
        )
        self.git.run(
            ["config", "user.email", self.config.author_email], cwd=cwd
        )

        for f in filenames:
            self.git.run(["add", f], cwd=cwd)

        self.git.run(["commit", "-m", message], cwd=cwd)
        commit_hash = self.git.head_hash(cwd=cwd)
        log.info("已提交 %d 个文件: %s", len(filenames), commit_hash[:8])
        return commit_hash

    def push(self, wt: WorktreeInfo) -> None:
        """推送分支到 origin。"""
        self.git.run(["push", "-u", "origin", wt.branch], cwd=wt.path)
        log.info("已推送: origin/%s", wt.branch)
