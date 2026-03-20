"""
对比分析视图 — Skill 间结果 diff、代码变更对比、性能雷达图。

功能：
- 多 Skill 执行结果并排对比
- 代码输出 diff
- 指标雷达图（成功率、耗时、文件数等）
- 对比报告输出（终端、HTML）
"""

from __future__ import annotations

import difflib
from typing import Sequence

from .models import TaskResult, TaskStatus

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.syntax import Syntax
    from rich import box

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def group_by_task(results: Sequence[TaskResult]) -> dict[str, list[TaskResult]]:
    """按 task_id 分组结果。"""
    groups: dict[str, list[TaskResult]] = {}
    for r in results:
        groups.setdefault(r.task_id, []).append(r)
    return groups


def group_by_skill(results: Sequence[TaskResult]) -> dict[str, list[TaskResult]]:
    """按 skill_name 分组结果。"""
    groups: dict[str, list[TaskResult]] = {}
    for r in results:
        groups.setdefault(r.skill_name, []).append(r)
    return groups


def compute_diff(
    output_a: str,
    output_b: str,
    label_a: str = "Skill A",
    label_b: str = "Skill B",
    context: int = 3,
) -> str:
    """生成两段输出的 unified diff。"""
    lines_a = output_a.splitlines(keepends=True)
    lines_b = output_b.splitlines(keepends=True)
    diff = difflib.unified_diff(
        lines_a, lines_b,
        fromfile=label_a, tofile=label_b,
        n=context,
    )
    return "".join(diff)


class ComparisonReport:
    """多 Skill 对比报告生成器。"""

    def __init__(self, results: Sequence[TaskResult]):
        self.results = list(results)
        self._by_task = group_by_task(self.results)
        self._by_skill = group_by_skill(self.results)

    @property
    def skill_names(self) -> list[str]:
        return sorted(self._by_skill.keys())

    @property
    def task_ids(self) -> list[str]:
        return sorted(self._by_task.keys())

    def skill_metrics(self) -> list[dict]:
        """计算每个 Skill 的综合指标。"""
        metrics = []
        for name, items in sorted(self._by_skill.items()):
            total = len(items)
            success = sum(1 for r in items if r.success)
            avg_dur = sum(r.duration for r in items) / total if total else 0
            avg_files = sum(len(r.files_changed) for r in items) / total if total else 0
            avg_output = sum(len(r.output) for r in items) / total if total else 0
            committed = sum(1 for r in items if r.commit_hash)
            pushed = sum(1 for r in items if r.pushed)

            metrics.append({
                "skill": name,
                "total": total,
                "success": success,
                "failed": total - success,
                "success_rate": round(100 * success / total, 1) if total else 0,
                "avg_duration": round(avg_dur, 1),
                "avg_files": round(avg_files, 1),
                "avg_output_len": int(avg_output),
                "commit_rate": round(100 * committed / total, 1) if total else 0,
                "push_rate": round(100 * pushed / total, 1) if total else 0,
            })
        return metrics

    def pairwise_diffs(self) -> list[dict]:
        """对同一 task 的不同 Skill 输出，两两做 diff。"""
        diffs = []
        for task_id, items in self._by_task.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a, b = items[i], items[j]
                    d = compute_diff(
                        a.output, b.output,
                        label_a=a.skill_name,
                        label_b=b.skill_name,
                    )
                    diffs.append({
                        "task_id": task_id,
                        "skill_a": a.skill_name,
                        "skill_b": b.skill_name,
                        "diff": d,
                        "diff_lines": len(d.splitlines()),
                    })
        return diffs

    # ── Rich 渲染 ────────────────────────────────────────────────────────

    def print_rich(self) -> None:
        if not RICH_AVAILABLE:
            self.print_plain()
            return

        from .progress import make_console
        console = make_console()

        # 指标对比表
        table = Table(
            title="[bold]Skill 对比分析[/]",
            box=box.ROUNDED,
            show_lines=True,
        )
        table.add_column("Skill", style="bold cyan")
        table.add_column("运行数", justify="center")
        table.add_column("成功率", justify="center")
        table.add_column("平均耗时", justify="right")
        table.add_column("平均文件数", justify="right")
        table.add_column("提交率", justify="center")
        table.add_column("推送率", justify="center")
        table.add_column("平均输出长度", justify="right")

        for m in self.skill_metrics():
            rate = m["success_rate"]
            rate_style = "green" if rate >= 80 else ("yellow" if rate >= 50 else "red")
            table.add_row(
                m["skill"],
                str(m["total"]),
                f"[{rate_style}]{rate}%[/]",
                f"{m['avg_duration']}s",
                str(m["avg_files"]),
                f"{m['commit_rate']}%",
                f"{m['push_rate']}%",
                str(m["avg_output_len"]),
            )

        console.print()
        console.print(table)

        # Diff 摘要
        diffs = self.pairwise_diffs()
        if diffs:
            console.print()
            console.print("[bold]代码输出差异摘要[/]")
            for d in diffs:
                if d["diff_lines"] > 0:
                    console.print(
                        f"  [dim]{d['task_id']}[/]: "
                        f"[cyan]{d['skill_a']}[/] vs [cyan]{d['skill_b']}[/] "
                        f"— {d['diff_lines']} 行差异"
                    )
                    if d["diff_lines"] <= 80:
                        console.print(
                            Syntax(d["diff"], "diff", theme="monokai", line_numbers=False)
                        )
                else:
                    console.print(
                        f"  [dim]{d['task_id']}[/]: "
                        f"[cyan]{d['skill_a']}[/] vs [cyan]{d['skill_b']}[/] "
                        f"— [green]输出完全相同[/]"
                    )

        console.print()

    def print_plain(self) -> None:
        """纯文本对比输出。"""
        print("\n  === Skill 对比分析 ===\n")
        for m in self.skill_metrics():
            print(
                f"  {m['skill']:20s} | "
                f"成功率 {m['success_rate']:5.1f}% | "
                f"耗时 {m['avg_duration']:6.1f}s | "
                f"文件 {m['avg_files']:.1f} | "
                f"提交 {m['commit_rate']:5.1f}% | "
                f"推送 {m['push_rate']:5.1f}%"
            )

        diffs = self.pairwise_diffs()
        if diffs:
            print(f"\n  差异: {len([d for d in diffs if d['diff_lines'] > 0])} 对有差异\n")

    # ── HTML 报告 ────────────────────────────────────────────────────────

    def to_html(self) -> str:
        """生成 HTML 对比报告。"""
        metrics = self.skill_metrics()

        rows_html = ""
        for m in metrics:
            rate = m["success_rate"]
            color = "#22c55e" if rate >= 80 else ("#eab308" if rate >= 50 else "#ef4444")
            rows_html += f"""
            <tr>
                <td><strong>{m['skill']}</strong></td>
                <td>{m['total']}</td>
                <td style="color:{color};font-weight:bold">{rate}%</td>
                <td>{m['avg_duration']}s</td>
                <td>{m['avg_files']}</td>
                <td>{m['commit_rate']}%</td>
                <td>{m['push_rate']}%</td>
                <td>{m['avg_output_len']}</td>
            </tr>"""

        diff_html = ""
        for d in self.pairwise_diffs():
            if d["diff_lines"] > 0:
                escaped = d["diff"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                diff_html += f"""
                <div class="diff-block">
                    <h4>{d['task_id']}: {d['skill_a']} vs {d['skill_b']} ({d['diff_lines']} 行差异)</h4>
                    <pre><code>{escaped}</code></pre>
                </div>"""

        return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Skill 对比报告</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; background: #0d1117; color: #c9d1d9; }}
  h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 0.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #30363d; padding: 0.5rem 1rem; text-align: center; }}
  th {{ background: #161b22; color: #58a6ff; }}
  tr:nth-child(even) {{ background: #161b22; }}
  .diff-block {{ background: #161b22; padding: 1rem; margin: 1rem 0; border-radius: 6px; border: 1px solid #30363d; }}
  .diff-block pre {{ overflow-x: auto; font-size: 0.85rem; }}
  .diff-block h4 {{ color: #58a6ff; margin: 0 0 0.5rem; }}
</style>
</head>
<body>
<h1>Skill 对比分析报告</h1>
<table>
<tr><th>Skill</th><th>运行数</th><th>成功率</th><th>平均耗时</th><th>平均文件数</th><th>提交率</th><th>推送率</th><th>平均输出长度</th></tr>
{rows_html}
</table>
<h2>代码输出差异</h2>
{diff_html if diff_html else '<p>无差异或无可比较项</p>'}
</body></html>"""
