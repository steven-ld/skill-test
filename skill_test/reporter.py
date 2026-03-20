"""
报告生成器 — 支持多种输出格式（Text / JSON / Markdown / HTML）。

单一数据源 TaskResult，格式化逻辑彼此独立。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .log import get_logger
from .models import ReportFormat, TaskResult

log = get_logger("reporter")


# ─── 统计计算 ────────────────────────────────────────────────────────────────

def _compute_stats(results: list[TaskResult]) -> dict:
    """按 Skill 分组计算统计指标。"""
    groups: dict[str, list[TaskResult]] = defaultdict(list)
    for r in results:
        groups[r.skill_name].append(r)

    stats = {}
    for skill, items in groups.items():
        total = len(items)
        success = sum(1 for r in items if r.success)
        durations = [r.duration for r in items]
        stats[skill] = {
            "total": total,
            "success": success,
            "failed": total - success,
            "success_rate": (success / total * 100) if total else 0,
            "avg_duration": sum(durations) / len(durations) if durations else 0,
            "min_duration": min(durations) if durations else 0,
            "max_duration": max(durations) if durations else 0,
        }
    return stats


# ─── Text 格式 ───────────────────────────────────────────────────────────────

def format_text(results: list[TaskResult]) -> str:
    """生成纯文本报告。"""
    stats = _compute_stats(results)
    total = len(results)
    ok = sum(1 for r in results if r.success)
    committed = sum(1 for r in results if r.commit_hash)
    pushed = sum(1 for r in results if r.pushed)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "=" * 72,
        "                    AI SKILL 测试报告",
        "=" * 72,
        f"  生成时间:  {now}",
        f"  总测试数:  {total}",
        f"  成功:      {ok}",
        f"  失败:      {total - ok}",
        f"  已提交:    {committed}",
        f"  已推送:    {pushed}",
        "",
        "-" * 72,
        "  各 Skill 表现",
        "-" * 72,
    ]

    for skill, s in stats.items():
        lines.append(f"  [{skill}]")
        lines.append(f"    成功率:   {s['success_rate']:.1f}%  ({s['success']}/{s['total']})")
        lines.append(f"    平均耗时: {s['avg_duration']:.1f}s  (min={s['min_duration']:.1f}s, max={s['max_duration']:.1f}s)")
        lines.append("")

    lines.extend(["-" * 72, "  详细结果", "-" * 72])

    for r in results:
        icon = "OK  " if r.success else "FAIL"
        lines.append(f"  [{icon}] {r.task_id} | {r.skill_name} | {r.duration:.1f}s")
        lines.append(f"         模式: {r.experiment_mode}")
        if r.worktree_branch:
            lines.append(f"         分支: {r.worktree_branch}")
        if r.commit_hash:
            push_text = "已推送" if r.pushed else "未推送"
            lines.append(f"         提交: {r.commit_hash[:12]} ({push_text})")
        if r.pr_url:
            lines.append(f"         PR: {r.pr_url}")
        elif r.pr_urls:
            for item in r.pr_urls[:3]:
                if item.get("url"):
                    repo_name = Path(item.get("repo", "")).name or "repo"
                    lines.append(f"         PR[{repo_name}]: {item['url']}")
        if r.deliverable_path:
            lines.append(f"         交付: {r.deliverable_path}")
        if r.files_changed:
            lines.append(f"         变更: {len(r.files_changed)} 个文件")
        validation_errors = r.metadata.get("validation_errors") or []
        if validation_errors:
            lines.append(f"         校验: {'；'.join(validation_errors[:3])}")
        if r.error:
            lines.append(f"         错误: {r.error[:120]}")

    lines.append("=" * 72)
    return "\n".join(lines)


# ─── JSON 格式 ───────────────────────────────────────────────────────────────

def format_json(results: list[TaskResult]) -> str:
    """生成 JSON 报告。"""
    stats = _compute_stats(results)
    data = {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total": len(results),
            "success": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
            "committed": sum(1 for r in results if r.commit_hash),
            "pushed": sum(1 for r in results if r.pushed),
        },
        "skill_stats": stats,
        "results": [r.to_dict() for r in results],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# ─── Markdown 格式 ───────────────────────────────────────────────────────────

def format_markdown(results: list[TaskResult]) -> str:
    """生成 Markdown 报告。"""
    stats = _compute_stats(results)
    ok = sum(1 for r in results if r.success)
    committed = sum(1 for r in results if r.commit_hash)
    pushed = sum(1 for r in results if r.pushed)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# AI Skill 测试报告",
        "",
        f"> 生成时间: {now}",
        "",
        "## 概览",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 总测试数 | {len(results)} |",
        f"| 成功 | {ok} |",
        f"| 失败 | {len(results) - ok} |",
        f"| 已提交 | {committed} |",
        f"| 已推送 | {pushed} |",
        "",
        "## 各 Skill 表现",
        "",
        "| Skill | 成功率 | 平均耗时 | 最快 | 最慢 |",
        "|-------|--------|----------|------|------|",
    ]

    for skill, s in stats.items():
        lines.append(
            f"| {skill} | {s['success_rate']:.1f}% | "
            f"{s['avg_duration']:.1f}s | "
            f"{s['min_duration']:.1f}s | "
            f"{s['max_duration']:.1f}s |"
        )

    lines.extend(["", "## 详细结果", ""])
    for r in results:
        icon = "✅" if r.success else "❌"
        lines.append(f"### {icon} {r.task_id} — {r.skill_name}")
        lines.append("")
        lines.append(f"- **状态**: {r.status.value}")
        lines.append(f"- **实验模式**: {r.experiment_mode}")
        lines.append(f"- **耗时**: {r.duration:.1f}s")
        if r.worktree_branch:
            lines.append(f"- **分支**: `{r.worktree_branch}`")
        if r.commit_hash:
            lines.append(f"- **提交**: `{r.commit_hash}`")
            lines.append(f"- **推送**: {'是' if r.pushed else '否'}")
        if r.pr_url:
            lines.append(f"- **PR 链接**: {r.pr_url}")
        elif r.pr_urls:
            for item in r.pr_urls[:3]:
                if item.get("url"):
                    repo_name = Path(item.get("repo", "")).name or "repo"
                    lines.append(f"- **PR 链接 ({repo_name})**: {item['url']}")
        if r.deliverable_path:
            lines.append(f"- **交付文件**: `{r.deliverable_path}`")
        if r.files_changed:
            lines.append(f"- **变更文件**: {len(r.files_changed)}")
        validation_errors = r.metadata.get("validation_errors") or []
        if validation_errors:
            lines.append(f"- **校验失败**: `{'；'.join(validation_errors[:3])}`")
        if r.error:
            lines.append(f"- **错误**: `{r.error[:200]}`")
        lines.append("")

    return "\n".join(lines)


# ─── HTML 格式 ───────────────────────────────────────────────────────────────

def format_html(results: list[TaskResult]) -> str:
    """生成简洁 HTML 报告。"""
    stats = _compute_stats(results)
    ok = sum(1 for r in results if r.success)
    committed = sum(1 for r in results if r.commit_hash)
    pushed = sum(1 for r in results if r.pushed)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows_stats = ""
    for skill, s in stats.items():
        bar_width = int(s["success_rate"])
        rows_stats += f"""
        <tr>
          <td>{skill}</td>
          <td><div class="bar" style="width:{bar_width}%">{s['success_rate']:.0f}%</div></td>
          <td>{s['avg_duration']:.1f}s</td>
        </tr>"""

    rows_detail = ""
    for r in results:
        cls = "ok" if r.success else "fail"
        commit_text = r.commit_hash[:10] if r.commit_hash else "-"
        pr_cell = "-"
        if r.pr_url:
            pr_cell = f'<a href="{r.pr_url}" target="_blank" rel="noreferrer">打开</a>'
        elif r.pr_urls:
            links = []
            for item in r.pr_urls[:3]:
                if not item.get("url"):
                    continue
                repo_name = Path(item.get("repo", "")).name or "repo"
                links.append(
                    f'<a href="{item["url"]}" target="_blank" rel="noreferrer">{repo_name}</a>'
                )
            if links:
                pr_cell = "<br>".join(links)
        rows_detail += f"""
        <tr class="{cls}">
          <td>{r.task_id}</td>
          <td>{r.skill_name}</td>
          <td>{r.experiment_mode}</td>
          <td>{r.status.value}</td>
          <td>{r.duration:.1f}s</td>
          <td>{commit_text}</td>
          <td>{"yes" if r.pushed else "no"}</td>
          <td>{pr_cell}</td>
          <td>{r.error[:80] if r.error else '-'}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><title>Skill 测试报告</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: .5rem; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
  th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #ddd; }}
  th {{ background: #f5f5f5; }}
  .bar {{ background: #4caf50; color: #fff; padding: 2px 8px; border-radius: 3px; min-width: 40px; text-align: center; }}
  .ok {{ }}
  .fail td {{ color: #c62828; }}
  .meta {{ color: #666; }}
</style>
</head>
<body>
<h1>AI Skill 测试报告</h1>
<p class="meta">生成时间: {now} | 总计: {len(results)} | 成功: {ok} | 失败: {len(results) - ok} | 提交: {committed} | 推送: {pushed}</p>

<h2>各 Skill 表现</h2>
<table>
  <tr><th>Skill</th><th>成功率</th><th>平均耗时</th></tr>
  {rows_stats}
</table>

<h2>详细结果</h2>
<table>
  <tr><th>Task</th><th>Skill</th><th>实验模式</th><th>状态</th><th>耗时</th><th>提交</th><th>推送</th><th>PR</th><th>错误</th></tr>
  {rows_detail}
</table>
</body>
</html>"""


# ─── 统一入口 ────────────────────────────────────────────────────────────────

_FORMATTERS = {
    ReportFormat.TEXT: format_text,
    ReportFormat.JSON: format_json,
    ReportFormat.MARKDOWN: format_markdown,
    ReportFormat.HTML: format_html,
}

_EXT_MAP = {
    ReportFormat.TEXT: ".txt",
    ReportFormat.JSON: ".json",
    ReportFormat.MARKDOWN: ".md",
    ReportFormat.HTML: ".html",
}


def generate_report(
    results: list[TaskResult],
    fmt: ReportFormat = ReportFormat.TEXT,
) -> str:
    """生成指定格式的报告字符串。"""
    formatter = _FORMATTERS.get(fmt)
    if not formatter:
        raise ValueError(f"不支持的报告格式: {fmt}")
    return formatter(results)


def save_report(
    results: list[TaskResult],
    output_dir: str | Path,
    formats: list[ReportFormat] | None = None,
    prefix: str = "report",
) -> list[Path]:
    """
    保存报告文件到目录，支持同时输出多种格式。

    Returns:
        已保存的文件路径列表
    """
    formats = formats or [ReportFormat.TEXT, ReportFormat.JSON]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved: list[Path] = []

    for fmt in formats:
        content = generate_report(results, fmt)
        ext = _EXT_MAP[fmt]
        filepath = out / f"{prefix}_{ts}{ext}"
        filepath.write_text(content, encoding="utf-8")
        saved.append(filepath)
        log.info("报告已保存: %s", filepath)

    return saved


def print_report(results: list[TaskResult]) -> None:
    """直接在终端打印 Text 格式报告。"""
    print(generate_report(results, ReportFormat.TEXT))
