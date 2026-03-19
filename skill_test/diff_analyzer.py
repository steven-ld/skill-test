"""
Git 变更分析器 — 精确统计文件新增/修改/删除及行级变更。

功能：
- 解析 git status --porcelain 获取文件状态
- 解析 git diff --numstat 获取行级增删
- 未追踪文件自动计算行数
- 聚合统计：新增文件数、修改文件数、删除文件数、总增删行数
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .log import get_logger

if TYPE_CHECKING:
    from .git_manager import GitClient

log = get_logger("diff")


@dataclass
class FileChange:
    """单个文件的变更详情。"""
    path: str
    status: str  # "added" | "modified" | "deleted" | "renamed" | "untracked"
    lines_added: int = 0
    lines_deleted: int = 0
    is_binary: bool = False

    @property
    def net_lines(self) -> int:
        return self.lines_added - self.lines_deleted

    def to_dict(self) -> dict:
        d = asdict(self)
        d["net_lines"] = self.net_lines
        return d


@dataclass
class ChangeSummary:
    """变更汇总统计。"""
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_renamed: int = 0
    total_files: int = 0
    total_lines_added: int = 0
    total_lines_deleted: int = 0
    changes: list[FileChange] = field(default_factory=list)

    @property
    def net_lines(self) -> int:
        return self.total_lines_added - self.total_lines_deleted

    def to_dict(self) -> dict:
        return {
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "files_renamed": self.files_renamed,
            "total_files": self.total_files,
            "total_lines_added": self.total_lines_added,
            "total_lines_deleted": self.total_lines_deleted,
            "net_lines": self.net_lines,
            "changes": [c.to_dict() for c in self.changes],
        }


_STATUS_MAP = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "??": "untracked",
    "AM": "added",
    "MM": "modified",
}


def analyze_changes(
    git: GitClient,
    *,
    cwd: str | Path | None = None,
) -> ChangeSummary:
    """
    分析工作树中的所有变更。

    Returns:
        ChangeSummary 包含文件列表和聚合统计
    """
    work_dir = Path(cwd) if cwd else git.repo

    # 1. 获取文件状态
    status_result = git.run(["status", "--porcelain"], cwd=work_dir, check=False)
    status_lines = status_result.stdout.strip().splitlines()

    file_statuses: dict[str, str] = {}
    for line in status_lines:
        if not line or len(line) < 4:
            continue
        raw_status = line[:2].strip()
        filepath = line[3:].strip()
        if " -> " in filepath:
            filepath = filepath.split(" -> ")[-1]
        file_statuses[filepath] = _STATUS_MAP.get(raw_status, "modified")

    # 2. 获取行级变更（已追踪文件）
    numstat_result = git.run(
        ["diff", "--numstat", "HEAD"], cwd=work_dir, check=False,
    )
    line_stats: dict[str, tuple[int, int]] = {}
    for line in numstat_result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added_str, deleted_str, filepath = parts[0], parts[1], parts[2]
            if added_str == "-":
                line_stats[filepath] = (0, 0)  # binary
            else:
                try:
                    line_stats[filepath] = (int(added_str), int(deleted_str))
                except ValueError:
                    line_stats[filepath] = (0, 0)

    # 3. 也检查已暂存的变更
    cached_result = git.run(
        ["diff", "--numstat", "--cached"], cwd=work_dir, check=False,
    )
    for line in cached_result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added_str, deleted_str, filepath = parts[0], parts[1], parts[2]
            if filepath not in line_stats and added_str != "-":
                try:
                    line_stats[filepath] = (int(added_str), int(deleted_str))
                except ValueError:
                    pass

    # 4. 构建 FileChange 列表
    changes: list[FileChange] = []

    for filepath, status in file_statuses.items():
        added, deleted = line_stats.get(filepath, (0, 0))

        if status == "untracked" and added == 0:
            full_path = work_dir / filepath
            if full_path.is_file():
                try:
                    added = len(full_path.read_text(
                        encoding="utf-8", errors="replace",
                    ).splitlines())
                except Exception:
                    pass
            status = "added"

        is_binary = filepath in line_stats and line_stats[filepath] == (0, 0) and status != "deleted"

        changes.append(FileChange(
            path=filepath,
            status=status,
            lines_added=added,
            lines_deleted=deleted,
            is_binary=is_binary,
        ))

    # 5. 聚合统计
    summary = ChangeSummary(changes=changes)
    for c in changes:
        summary.total_files += 1
        summary.total_lines_added += c.lines_added
        summary.total_lines_deleted += c.lines_deleted
        if c.status == "added":
            summary.files_added += 1
        elif c.status == "modified":
            summary.files_modified += 1
        elif c.status == "deleted":
            summary.files_deleted += 1
        elif c.status == "renamed":
            summary.files_renamed += 1

    log.info(
        "变更分析: +%d -%d 文件 (新增%d 修改%d 删除%d) | +%d -%d 行",
        summary.files_added, summary.files_deleted,
        summary.files_added, summary.files_modified, summary.files_deleted,
        summary.total_lines_added, summary.total_lines_deleted,
    )

    return summary
