"""
实时可视化进度系统 — 基于 Rich 的终端面板。

功能：
- Live Dashboard 显示所有任务实时状态
- 进度条追踪整体完成度
- 实时日志滚动（最近 N 条）
- 最终结果对比表格
- 无 Rich 时自动降级到纯文本模式
"""

from __future__ import annotations

import time
import threading
from datetime import datetime
from typing import Optional, Callable

from .models import TaskResult, TaskStatus

try:
    import io
    import sys as _sys

    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn,
        TextColumn, TimeElapsedColumn, TaskID,
    )
    from rich.columns import Columns
    from rich import box

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


_utf8_stream = None

def make_console() -> Console:
    """创建支持 UTF-8 的 Console（Windows 兼容）。"""
    global _utf8_stream
    try:
        if _utf8_stream is None or _utf8_stream.closed:
            _utf8_stream = open(
                _sys.stdout.fileno(), mode="w",
                encoding="utf-8", errors="replace",
                closefd=False, buffering=1,
            )
        return Console(file=_utf8_stream, force_terminal=True)
    except Exception:
        return Console()

_STATUS_ICONS = {
    TaskStatus.PENDING: "[dim]⏳ 等待[/]",
    TaskStatus.RUNNING: "[bold cyan]⚡ 执行中[/]",
    TaskStatus.SUCCESS: "[bold green]✅ 成功[/]",
    TaskStatus.FAILED: "[bold red]❌ 失败[/]",
    TaskStatus.TIMEOUT: "[bold yellow]⏰ 超时[/]",
    TaskStatus.SKIPPED: "[dim]⏭️  跳过[/]",
}

_STATUS_PLAIN = {
    TaskStatus.PENDING: "WAIT",
    TaskStatus.RUNNING: "RUN ",
    TaskStatus.SUCCESS: " OK ",
    TaskStatus.FAILED: "FAIL",
    TaskStatus.TIMEOUT: "TIME",
    TaskStatus.SKIPPED: "SKIP",
}


class _TaskSlot:
    """追踪单个 task×skill 的运行状态。"""

    def __init__(self, task_id: str, task_name: str, skill_name: str):
        self.task_id = task_id
        self.task_name = task_name
        self.skill_name = skill_name
        self.status: TaskStatus = TaskStatus.PENDING
        self.duration: float = 0.0
        self.error: str = ""
        self.files_changed: int = 0
        self.started_at: float = 0.0

    @property
    def elapsed(self) -> float:
        if self.status == TaskStatus.RUNNING and self.started_at:
            return time.monotonic() - self.started_at
        return self.duration


class ProgressDashboard:
    """
    实时进度面板 — 显示任务执行矩阵和实时日志。

    用法:
        with ProgressDashboard(total=6) as dash:
            dash.register("t1", "快速排序", "baseline")
            dash.mark_running("t1", "baseline")
            dash.mark_complete(result)
    """

    def __init__(self, total: int, title: str = "AI Skill 测试"):
        self.total = total
        self.title = title
        self._slots: dict[str, _TaskSlot] = {}
        self._logs: list[str] = []
        self._lock = threading.Lock()
        self._completed = 0
        self._success = 0
        self._failed = 0
        self._start_time = 0.0

        self._use_rich = RICH_AVAILABLE
        self._live: Optional[Live] = None
        self._console: Optional[Console] = None

    def _slot_key(self, task_id: str, skill_name: str) -> str:
        return f"{task_id}::{skill_name}"

    def register(self, task_id: str, task_name: str, skill_name: str) -> None:
        key = self._slot_key(task_id, skill_name)
        with self._lock:
            self._slots[key] = _TaskSlot(task_id, task_name, skill_name)

    def mark_running(self, task_id: str, skill_name: str) -> None:
        key = self._slot_key(task_id, skill_name)
        with self._lock:
            if key in self._slots:
                self._slots[key].status = TaskStatus.RUNNING
                self._slots[key].started_at = time.monotonic()
            self._log(f"▶ {task_id} × {skill_name} 开始执行")

    def mark_complete(self, result: TaskResult) -> None:
        key = self._slot_key(result.task_id, result.skill_name)
        with self._lock:
            if key in self._slots:
                slot = self._slots[key]
                slot.status = result.status
                slot.duration = result.duration
                slot.error = result.error
                slot.files_changed = len(result.files_changed)

            self._completed += 1
            if result.success:
                self._success += 1
            else:
                self._failed += 1

            icon = "✅" if result.success else "❌"
            self._log(
                f"{icon} {result.task_id} × {result.skill_name} "
                f"→ {result.status.value} ({result.duration:.1f}s)"
            )

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._logs.append(f"[{ts}] {msg}")
        if len(self._logs) > 50:
            self._logs = self._logs[-50:]

    # ── Rich 渲染 ────────────────────────────────────────────────────────

    def _build_dashboard(self) -> Panel:
        elapsed = time.monotonic() - self._start_time if self._start_time else 0

        # 任务状态表格
        task_table = Table(
            box=box.SIMPLE_HEAD, expand=True, pad_edge=False,
            title="[bold]任务执行矩阵[/]",
        )
        task_table.add_column("任务", style="bold", ratio=2)
        task_table.add_column("Skill", ratio=2)
        task_table.add_column("状态", justify="center", ratio=2)
        task_table.add_column("耗时", justify="right", ratio=1)
        task_table.add_column("文件", justify="right", ratio=1)

        with self._lock:
            for slot in self._slots.values():
                status_text = _STATUS_ICONS.get(slot.status, str(slot.status))
                dur = f"{slot.elapsed:.1f}s" if slot.elapsed > 0 else "-"
                files = str(slot.files_changed) if slot.files_changed else "-"
                task_table.add_row(
                    slot.task_name[:20], slot.skill_name, status_text, dur, files,
                )

        # 日志面板
        recent_logs = self._logs[-8:] if self._logs else ["等待任务开始..."]
        log_text = Text("\n".join(recent_logs))

        # 进度概览
        pct = (self._completed / self.total * 100) if self.total else 0
        bar_filled = int(pct / 100 * 30)
        bar_str = "█" * bar_filled + "░" * (30 - bar_filled)

        header = Text()
        header.append(f"  进度: ", style="bold")
        header.append(f"[{bar_str}] ", style="cyan")
        header.append(f"{self._completed}/{self.total} ", style="bold cyan")
        header.append(f"({pct:.0f}%)  ", style="dim")
        header.append(f"✅ {self._success} ", style="green")
        header.append(f"❌ {self._failed} ", style="red")
        header.append(f"⏱ {elapsed:.0f}s", style="dim")

        # 组合布局
        layout = Table.grid(expand=True)
        layout.add_row(header)
        layout.add_row(Text(""))
        layout.add_row(task_table)
        layout.add_row(Text(""))
        layout.add_row(Panel(log_text, title="[bold]实时日志[/]", border_style="dim", height=12))

        return Panel(
            layout,
            title=f"[bold blue] {self.title} [/]",
            border_style="blue",
            padding=(1, 2),
        )

    # ── 纯文本降级 ───────────────────────────────────────────────────────

    def _print_plain_update(self) -> None:
        with self._lock:
            for slot in self._slots.values():
                if slot.status == TaskStatus.RUNNING:
                    status = _STATUS_PLAIN[slot.status]
                    print(f"  [{status}] {slot.task_id} × {slot.skill_name} ({slot.elapsed:.0f}s)")

    def _print_plain_result(self, result: TaskResult) -> None:
        status = _STATUS_PLAIN.get(result.status, "????")
        print(f"  [{status}] {result.task_id} × {result.skill_name} — {result.duration:.1f}s")

    # ── 上下文管理 ────────────────────────────────────────────────────────

    def __enter__(self) -> ProgressDashboard:
        self._start_time = time.monotonic()

        if self._use_rich:
            self._console = make_console()
            self._live = Live(
                self._build_dashboard(),
                console=self._console,
                refresh_per_second=2,
                transient=True,
            )
            self._live.__enter__()

            # 后台刷新线程
            self._refresh_running = True
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop, daemon=True,
            )
            self._refresh_thread.start()
        else:
            print(f"\n  {self.title}")
            print(f"  总计 {self.total} 个任务")
            print()

        return self

    def __exit__(self, *args) -> None:
        if self._use_rich and self._live:
            self._refresh_running = False
            self._refresh_thread.join(timeout=2)
            # 最终渲染
            self._live.update(self._build_dashboard())
            self._live.__exit__(*args)
            # 打印最终摘要
            self._print_rich_summary()
        else:
            elapsed = time.monotonic() - self._start_time
            print(f"\n  完成: {self._success}/{self.total} 成功, {self._failed} 失败, {elapsed:.1f}s\n")

    def _refresh_loop(self) -> None:
        while self._refresh_running:
            try:
                if self._live:
                    self._live.update(self._build_dashboard())
            except Exception:
                pass
            time.sleep(0.5)

    def _print_rich_summary(self) -> None:
        if not self._console:
            return

        elapsed = time.monotonic() - self._start_time

        summary = Table(box=box.ROUNDED, title="[bold]测试结果摘要[/]")
        summary.add_column("Skill", style="bold")
        summary.add_column("状态", justify="center")
        summary.add_column("耗时", justify="right")
        summary.add_column("文件变更", justify="right")

        with self._lock:
            for slot in sorted(self._slots.values(), key=lambda s: s.skill_name):
                status_text = _STATUS_ICONS.get(slot.status, str(slot.status))
                dur = f"{slot.duration:.1f}s"
                files = str(slot.files_changed) if slot.files_changed else "-"
                summary.add_row(
                    f"{slot.task_name} × {slot.skill_name}",
                    status_text,
                    dur,
                    files,
                )

        self._console.print()
        self._console.print(summary)
        self._console.print(
            f"\n  [dim]总耗时 {elapsed:.1f}s | "
            f"成功 {self._success} | 失败 {self._failed}[/]\n"
        )


class PlainProgress:
    """无 Rich 时的纯文本进度回调。"""

    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.start_time = time.monotonic()

    def on_result(self, result: TaskResult) -> None:
        self.completed += 1
        icon = "OK" if result.success else "FAIL"
        elapsed = time.monotonic() - self.start_time
        print(
            f"  [{self.completed}/{self.total}] [{icon}] "
            f"{result.task_id} × {result.skill_name} — "
            f"{result.duration:.1f}s (total: {elapsed:.0f}s)"
        )
