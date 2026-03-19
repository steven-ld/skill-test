"""
历史追踪与趋势分析 — SQLite 持久化测试结果。

功能：
- 自动记录每次测试结果到 SQLite
- 按 Skill / Task / 时间范围查询历史
- 趋势分析：成功率、耗时变化
- 对比不同 Skill 的历史表现
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .log import get_logger
from .models import TaskResult, TaskStatus

log = get_logger("history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    task_name   TEXT NOT NULL,
    skill_name  TEXT NOT NULL,
    status      TEXT NOT NULL,
    duration    REAL NOT NULL,
    output_len  INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    files_count INTEGER NOT NULL DEFAULT 0,
    files_list  TEXT NOT NULL DEFAULT '[]',
    branch      TEXT NOT NULL DEFAULT '',
    retries     INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_session ON test_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_skill   ON test_runs(skill_name);
CREATE INDEX IF NOT EXISTS idx_task    ON test_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_created ON test_runs(created_at);
"""


class HistoryDB:
    """测试历史数据库。"""

    def __init__(self, db_path: str | Path = "results/history.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> HistoryDB:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ── 写入 ────────────────────────────────────────────────────────────

    def record(self, results: list[TaskResult], session_id: str = "") -> int:
        """记录一批测试结果，返回写入条数。"""
        conn = self._get_conn()
        session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")

        rows = []
        for r in results:
            rows.append((
                r.run_id,
                session_id,
                r.task_id,
                r.task_name,
                r.skill_name,
                r.status.value,
                r.duration,
                len(r.output),
                r.error[:500],
                len(r.files_changed),
                json.dumps(r.files_changed, ensure_ascii=False),
                r.worktree_branch,
                r.retries,
                json.dumps(r.metadata, ensure_ascii=False),
            ))

        conn.executemany(
            """INSERT INTO test_runs
               (run_id, session_id, task_id, task_name, skill_name,
                status, duration, output_len, error, files_count,
                files_list, branch, retries, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        log.info("已记录 %d 条测试结果 (session=%s)", len(rows), session_id)
        return len(rows)

    # ── 查询 ────────────────────────────────────────────────────────────

    def query(
        self,
        *,
        skill: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        days: int | None = None,
    ) -> list[dict]:
        """灵活查询历史记录。"""
        conn = self._get_conn()
        where: list[str] = []
        params: list = []

        if skill:
            where.append("skill_name = ?")
            params.append(skill)
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if days:
            where.append("created_at >= datetime('now', 'localtime', ?)")
            params.append(f"-{days} days")

        clause = " AND ".join(where) if where else "1=1"
        sql = f"""
            SELECT * FROM test_runs
            WHERE {clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(limit)

        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def skill_stats(self, days: int | None = None) -> list[dict]:
        """按 Skill 聚合统计。"""
        conn = self._get_conn()
        time_filter = ""
        params: list = []
        if days:
            time_filter = "WHERE created_at >= datetime('now', 'localtime', ?)"
            params.append(f"-{days} days")

        sql = f"""
            SELECT
                skill_name,
                COUNT(*) as total_runs,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failures,
                SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeouts,
                ROUND(AVG(duration), 1) as avg_duration,
                ROUND(MIN(duration), 1) as min_duration,
                ROUND(MAX(duration), 1) as max_duration,
                ROUND(AVG(files_count), 1) as avg_files,
                ROUND(100.0 * SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
                MIN(created_at) as first_run,
                MAX(created_at) as last_run
            FROM test_runs
            {time_filter}
            GROUP BY skill_name
            ORDER BY success_rate DESC, avg_duration ASC
        """
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def sessions(self, limit: int = 20) -> list[dict]:
        """列出最近的测试会话。"""
        conn = self._get_conn()
        sql = """
            SELECT
                session_id,
                COUNT(*) as total_runs,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes,
                ROUND(AVG(duration), 1) as avg_duration,
                MIN(created_at) as started_at,
                GROUP_CONCAT(DISTINCT skill_name) as skills
            FROM test_runs
            GROUP BY session_id
            ORDER BY started_at DESC
            LIMIT ?
        """
        return [dict(row) for row in conn.execute(sql, [limit]).fetchall()]

    def trend(self, skill: str, limit: int = 20) -> list[dict]:
        """某个 Skill 的趋势数据。"""
        conn = self._get_conn()
        sql = """
            SELECT
                session_id,
                status,
                duration,
                files_count,
                created_at
            FROM test_runs
            WHERE skill_name = ?
            ORDER BY created_at DESC
            LIMIT ?
        """
        return [dict(row) for row in conn.execute(sql, [skill, limit]).fetchall()]

    @property
    def total_runs(self) -> int:
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
