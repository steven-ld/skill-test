"""
统一 CLI 入口 — 产品级命令行界面。

命令:
  run       执行测试（实时进度面板 + 重试 + 历史记录）
  list      列出 tasks / skills / presets
  discover  自动扫描仓库中的 Skill 定义
  history   查看测试历史与趋势
  compare   对比不同 Skill 的结果
  report    从已有 JSON 结果重新生成报告
  init      生成默认配置文件模板
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config, build_default_config
from .log import setup_logging, get_logger
from .models import ReportFormat, TaskResult, TaskStatus


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c", "--config", default=None,
        help="YAML 配置文件路径",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="启用 DEBUG 日志",
    )
    parser.add_argument(
        "--log-file", default=None,
        help="日志输出到文件",
    )


# ─── run 命令 ────────────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> None:
    from .reporter import save_report, print_report
    from .runner import TestRunner
    from .comparator import ComparisonReport
    from .git_manager import resolve_git_repo

    log = get_logger("cli")
    config = load_config(args.config) if args.config else build_default_config()

    if args.task:
        config.tasks = [t for t in config.tasks if t.id == args.task]
        if not config.tasks:
            log.error("任务 '%s' 不存在", args.task)
            sys.exit(1)

    if args.skill:
        config.skills = [s for s in config.skills if s.name == args.skill]
        if not config.skills:
            log.error("Skill '%s' 不存在", args.skill)
            sys.exit(1)

    repo_path = args.repo
    if repo_path:
        repo_path = str(
            resolve_git_repo(
                repo_path,
                tasks=config.tasks,
                work_dir=args.dir,
            )
        )

    runner = TestRunner(
        config,
        repo_path=repo_path,
        work_dir=args.dir,
        enable_progress=not args.no_progress,
        enable_history=not args.no_history,
    )

    results = runner.run(
        mode=args.mode,
        experiment_mode=args.experiment_mode,
        commit=args.commit,
        push=args.push,
    )

    print()
    print_report(results)

    if len(set(r.skill_name for r in results)) > 1:
        comp = ComparisonReport(results)
        comp.print_rich()

    if args.format != ["text", "json"]:
        formats = [ReportFormat(f) for f in args.format]
    else:
        formats = config.report_formats
    saved = save_report(results, config.output_dir, formats)
    for p in saved:
        print(f"  报告: {p}")


# ─── list 命令 ───────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> None:
    config = load_config(args.config) if args.config else build_default_config()

    try:
        from .progress import make_console
        console = make_console()
        _list_rich(config, args.what, console)
    except ImportError:
        _list_plain(config, args.what)


def _list_rich(config, what, console):
    from rich.table import Table
    from rich import box

    if what in ("tasks", "all"):
        table = Table(title="[bold]可用任务[/]", box=box.ROUNDED)
        table.add_column("ID", style="bold cyan")
        table.add_column("名称", style="bold")
        table.add_column("模式", justify="center")
        table.add_column("Prompt (前80字)", ratio=3)
        table.add_column("超时", justify="right")

        for t in config.tasks:
            table.add_row(
                t.id, t.name,
                t.mode,
                t.prompt[:80].replace("\n", " ") + "…",
                f"{t.timeout}s" if t.timeout else "默认",
            )
        console.print(table)

    if what in ("skills", "all"):
        table = Table(title="[bold]可用 Skills[/]", box=box.ROUNDED)
        table.add_column("名称", style="bold cyan")
        table.add_column("工具", justify="center")
        table.add_column("类型", justify="center")
        table.add_column("来源")

        for s in config.skills:
            if s.is_baseline:
                stype, source = "基线", "-"
            elif s.skill_file:
                stype, source = "文件", s.skill_file
            else:
                stype, source = "内联", (s.system_prompt or "")[:60]
            table.add_row(s.name, s.tool, stype, source)
        console.print(table)

    if what in ("presets", "all"):
        from .discovery import list_presets
        presets = list_presets()
        table = Table(title="[bold]内置预设 Skills[/]", box=box.ROUNDED)
        table.add_column("名称", style="bold cyan")
        table.add_column("描述")
        for name, skill in presets.items():
            table.add_row(name, (skill.system_prompt or "")[:60])
        console.print(table)


def _list_plain(config, what):
    if what in ("tasks", "all"):
        print("\n  可用任务:")
        for t in config.tasks:
            print(f"    {t.id}: {t.name} [{t.mode}]")

    if what in ("skills", "all"):
        print("\n  可用 Skills:")
        for s in config.skills:
            label = "baseline" if s.is_baseline else (s.system_prompt or s.skill_file or "")
            print(f"    {s.name} ({s.tool}): {label[:60]}")

    if what in ("presets", "all"):
        from .discovery import list_presets
        print("\n  内置预设 Skills:")
        for name, s in list_presets().items():
            print(f"    {name}: {(s.system_prompt or '')[:60]}")
    print()


# ─── discover 命令 ───────────────────────────────────────────────────────────

def cmd_discover(args: argparse.Namespace) -> None:
    from .discovery import discover_skills
    path = args.path or "."
    skills = discover_skills(path)

    if not skills:
        print(f"  在 {path} 中未发现 Skill 定义")
        return

    try:
        from .progress import make_console
        from rich.table import Table
        from rich import box
        console = make_console()
        table = Table(title=f"[bold]在 {path} 中发现的 Skills[/]", box=box.ROUNDED)
        table.add_column("名称", style="bold cyan")
        table.add_column("工具", justify="center")
        table.add_column("Skill 文件")
        table.add_column("来源")
        table.add_column("引用文件数", justify="right")
        for s in skills:
            table.add_row(s.name, s.tool, s.skill_file or "-", s.origin, str(len(s.ref_files)))
        console.print(table)
    except ImportError:
        print(f"\n  发现 {len(skills)} 个 Skill:")
        for s in skills:
            print(f"    {s.name} ({s.tool}): {s.skill_file} [{s.origin}] ({len(s.ref_files)} refs)")
        print()


# ─── history 命令 ─────────────────────────────────────────────────────────────

def cmd_history(args: argparse.Namespace) -> None:
    from .history import HistoryDB
    config = load_config(args.config) if args.config else build_default_config()
    db_path = Path(config.output_dir) / "history.db"

    if not db_path.exists():
        print(f"  历史数据库不存在: {db_path}")
        print("  请先运行 `skill-test run` 生成测试数据")
        return

    with HistoryDB(db_path) as db:
        if args.sub == "stats":
            _history_stats(db, args)
        elif args.sub == "sessions":
            _history_sessions(db, args)
        elif args.sub == "trend":
            _history_trend(db, args)
        elif args.sub == "query":
            _history_query(db, args)
        else:
            _history_stats(db, args)


def _history_stats(db, args):
    stats = db.skill_stats(days=getattr(args, "days", None))
    if not stats:
        print("  暂无测试历史数据")
        return

    try:
        from .progress import make_console
        from rich.table import Table
        from rich import box
        console = make_console()
        table = Table(title="[bold]Skill 历史统计[/]", box=box.ROUNDED)
        table.add_column("Skill", style="bold cyan")
        table.add_column("运行数", justify="right")
        table.add_column("成功率", justify="center")
        table.add_column("平均耗时", justify="right")
        table.add_column("最快", justify="right")
        table.add_column("最慢", justify="right")
        table.add_column("最后运行")

        for s in stats:
            rate = s["success_rate"]
            color = "green" if rate >= 80 else ("yellow" if rate >= 50 else "red")
            table.add_row(
                s["skill_name"],
                str(s["total_runs"]),
                f"[{color}]{rate}%[/]",
                f"{s['avg_duration']}s",
                f"{s['min_duration']}s",
                f"{s['max_duration']}s",
                s["last_run"][:16] if s["last_run"] else "-",
            )
        console.print(table)
        console.print(f"  [dim]总历史记录: {db.total_runs}[/]\n")
    except ImportError:
        for s in stats:
            print(f"  {s['skill_name']:20s} | {s['success_rate']}% | {s['avg_duration']}s")


def _history_sessions(db, args):
    sessions = db.sessions(limit=getattr(args, "limit", 20))
    if not sessions:
        print("  暂无测试会话")
        return

    try:
        from .progress import make_console
        from rich.table import Table
        from rich import box
        console = make_console()
        table = Table(title="[bold]测试会话历史[/]", box=box.ROUNDED)
        table.add_column("会话 ID", style="bold cyan")
        table.add_column("运行数", justify="right")
        table.add_column("成功", justify="right")
        table.add_column("平均耗时", justify="right")
        table.add_column("Skills")
        table.add_column("时间")

        for s in sessions:
            table.add_row(
                s["session_id"],
                str(s["total_runs"]),
                str(s["successes"]),
                f"{s['avg_duration']}s",
                s["skills"] or "-",
                s["started_at"][:16] if s["started_at"] else "-",
            )
        console.print(table)
    except ImportError:
        for s in sessions:
            print(f"  {s['session_id']} | {s['total_runs']} runs | {s['started_at']}")


def _history_trend(db, args):
    skill = args.skill
    if not skill:
        print("  --skill 参数为必填")
        return

    data = db.trend(skill, limit=getattr(args, "limit", 20))
    if not data:
        print(f"  Skill '{skill}' 无历史数据")
        return

    try:
        from .progress import make_console
        from rich.table import Table
        from rich import box
        console = make_console()
        table = Table(title=f"[bold]{skill} 趋势数据[/]", box=box.ROUNDED)
        table.add_column("#", justify="right")
        table.add_column("状态", justify="center")
        table.add_column("耗时", justify="right")
        table.add_column("文件数", justify="right")
        table.add_column("时间")

        for i, d in enumerate(reversed(data), 1):
            s = d["status"]
            icon = "[green]OK[/]" if s == "success" else f"[red]{s}[/]"
            table.add_row(
                str(i), icon,
                f"{d['duration']:.1f}s",
                str(d["files_count"]),
                d["created_at"][:16],
            )
        console.print(table)
    except ImportError:
        for d in data:
            print(f"  {d['created_at'][:16]} | {d['status']} | {d['duration']:.1f}s")


def _history_query(db, args):
    results = db.query(
        skill=getattr(args, "skill", None),
        task_id=getattr(args, "task_id", None),
        status=getattr(args, "status", None),
        limit=getattr(args, "limit", 50),
        days=getattr(args, "days", None),
    )
    if not results:
        print("  无匹配记录")
        return

    for r in results:
        s = r["status"]
        icon = "OK" if s == "success" else s.upper()
        print(
            f"  [{icon:4s}] {r['task_id']:15s} × {r['skill_name']:20s} | "
            f"{r['duration']:.1f}s | {r['created_at'][:16]}"
        )
    print(f"\n  共 {len(results)} 条记录")


# ─── compare 命令 ─────────────────────────────────────────────────────────────

def cmd_compare(args: argparse.Namespace) -> None:
    from .comparator import ComparisonReport

    path = Path(args.input)
    if not path.exists():
        print(f"文件不存在: {path}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    raw_results = data.get("results", data) if isinstance(data, dict) else data
    results = _parse_results_json(raw_results)

    comp = ComparisonReport(results)
    comp.print_rich()

    if args.html:
        html_path = Path(args.html)
        html_path.write_text(comp.to_html(), encoding="utf-8")
        print(f"  HTML 对比报告: {html_path}")


# ─── report 命令 ─────────────────────────────────────────────────────────────

def cmd_report(args: argparse.Namespace) -> None:
    from .reporter import generate_report

    path = Path(args.input)
    if not path.exists():
        print(f"文件不存在: {path}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    raw_results = data.get("results", data) if isinstance(data, dict) else data
    results = _parse_results_json(raw_results)

    fmt = ReportFormat(args.format)
    content = generate_report(results, fmt)

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        print(f"报告已保存: {args.output}")
    else:
        print(content)


# ─── init 命令 ───────────────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> None:
    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"文件已存在: {output}（使用 --force 覆盖）")
        sys.exit(1)

    template = """\
# AI Skill 测试框架 v3 配置文件
# 文档: python -m skill_test --help

cli:
  command: claude          # Windows 自动检测为 claude.cmd
  base_args:
    - "--print"
    - "--dangerously-skip-permissions"
  timeout: 300             # 单个任务超时（秒）

git:
  base_branch: main
  branch_prefix: skill-test
  cleanup_on_finish: true

# 重试配置
retry:
  max_retries: 2           # 最大重试次数
  base_delay: 5            # 初始延迟（秒）
  max_delay: 60            # 最大延迟（秒）
  retry_on_timeout: true   # 超时时自动重试
  retry_on_failure: false  # 失败时自动重试

max_workers: 3
output_dir: results

report_formats:
  - text
  - json
  # - markdown
  # - html

# 自动发现仓库中的 Skill 定义
# discover_skills: true
# discover_path: "/path/to/repo"

tasks:
  - id: task_001
    name: "Python 快速排序"
    mode: coding
    prompt: |
      实现一个快速排序算法，要求：
      1. 使用 Python
      2. 包含类型标注和文档字符串
      3. 包含单元测试
    timeout: 180

skills:
  - name: baseline
    # 不设置 = 基线对照

  - name: write-expert
    system_prompt: "你是专业的代码编写专家，擅长生成高质量、结构清晰的代码。"
    tool: preset

  # 使用预设 Skill
  # - name: tdd
  #   preset: tdd-expert

  # 从文件加载 Skill
  # - name: project-rules
  #   skill_file: ".claude/skills/my-rules/SKILL.md"
  #   ref_files:
  #     - "references/arch.md"

# Skill 组合（多个 Skill 合并为一个）
# skill_groups:
#   - name: "expert+rules"
#     skills: ["write-expert", "project-rules"]
#     compose_mode: chain    # chain | merge
"""
    output.write_text(template, encoding="utf-8")
    print(f"配置文件已生成: {output}")


# ─── serve 命令 ──────────────────────────────────────────────────────────────

def cmd_serve(args: argparse.Namespace) -> None:
    """启动 Web 平台。"""
    try:
        from .server import run_server
    except ImportError:
        print("  Web 平台需要额外依赖:")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    log_level = "debug" if args.verbose else "info"
    run_server(
        host=args.host,
        port=args.port,
        config_path=args.config,
        log_level=log_level,
    )


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _parse_results_json(raw_results: list) -> list[TaskResult]:
    if not isinstance(raw_results, list):
        print("JSON 格式不符合预期")
        sys.exit(1)

    results = []
    for item in raw_results:
        results.append(TaskResult(
            task_id=item.get("task_id", ""),
            task_name=item.get("task_name", ""),
            skill_name=item.get("skill_name", ""),
            status=TaskStatus(item.get("status", "failed")),
            output=item.get("output", ""),
            error=item.get("error", ""),
            duration=item.get("duration", 0),
            worktree_branch=item.get("worktree_branch", ""),
            files_changed=item.get("files_changed", []),
            metadata=item.get("metadata", {}),
        ))
    return results


# ─── 主解析器 ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-test",
        description="AI Skill 测试框架 v3 — 多 Skill 对比、实时进度、历史追踪",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", help="子命令")

    # run
    p_run = sub.add_parser("run", help="执行测试")
    _add_common_args(p_run)
    p_run.add_argument("-t", "--task", help="只运行指定任务 ID")
    p_run.add_argument("-s", "--skill", help="只运行指定 Skill")
    p_run.add_argument("-d", "--dir", help="工作目录")
    p_run.add_argument("-r", "--repo", help="Git 仓库路径（启用隔离模式）")
    p_run.add_argument(
        "-m", "--mode", default="auto",
        choices=["auto", "simple", "parallel", "isolated"],
    )
    p_run.add_argument(
        "--experiment-mode", default="task",
        choices=["task", "solution", "coding"],
        help="实验模式：task=按任务配置，solution=技术方案模式，coding=直接编码模式",
    )
    p_run.add_argument("--commit", action="store_true", help="隔离模式下自动提交")
    p_run.add_argument("--push", action="store_true", help="提交后推送")
    p_run.add_argument(
        "-f", "--format", nargs="+", default=["text", "json"],
        help="报告格式: text json markdown html",
    )
    p_run.add_argument("--no-progress", action="store_true", help="禁用实时进度面板")
    p_run.add_argument("--no-history", action="store_true", help="不记录到历史数据库")
    p_run.set_defaults(func=cmd_run)

    # list
    p_list = sub.add_parser("list", help="列出 tasks / skills / presets")
    _add_common_args(p_list)
    p_list.add_argument(
        "what", nargs="?", default="all",
        choices=["tasks", "skills", "presets", "all"],
    )
    p_list.set_defaults(func=cmd_list)

    # discover
    p_disc = sub.add_parser("discover", help="自动扫描仓库中的 Skill")
    p_disc.add_argument("path", nargs="?", default=".", help="仓库路径")
    p_disc.set_defaults(func=cmd_discover)

    # history
    p_hist = sub.add_parser("history", help="测试历史与趋势")
    _add_common_args(p_hist)
    p_hist_sub = p_hist.add_subparsers(dest="sub")

    p_h_stats = p_hist_sub.add_parser("stats", help="Skill 统计")
    p_h_stats.add_argument("--days", type=int, help="最近 N 天")

    p_h_sess = p_hist_sub.add_parser("sessions", help="会话列表")
    p_h_sess.add_argument("--limit", type=int, default=20)

    p_h_trend = p_hist_sub.add_parser("trend", help="某 Skill 趋势")
    p_h_trend.add_argument("--skill", required=True, help="Skill 名称")
    p_h_trend.add_argument("--limit", type=int, default=20)

    p_h_query = p_hist_sub.add_parser("query", help="灵活查询")
    p_h_query.add_argument("--skill", help="按 Skill 筛选")
    p_h_query.add_argument("--task-id", help="按任务 ID 筛选")
    p_h_query.add_argument("--status", help="按状态筛选")
    p_h_query.add_argument("--days", type=int, help="最近 N 天")
    p_h_query.add_argument("--limit", type=int, default=50)

    p_hist.set_defaults(func=cmd_history)

    # compare
    p_comp = sub.add_parser("compare", help="对比 Skill 结果")
    p_comp.add_argument("input", help="JSON 结果文件")
    p_comp.add_argument("--html", help="输出 HTML 对比报告")
    p_comp.set_defaults(func=cmd_compare)

    # report
    p_report = sub.add_parser("report", help="从 JSON 重新生成报告")
    _add_common_args(p_report)
    p_report.add_argument("input", help="JSON 结果文件路径")
    p_report.add_argument(
        "-f", "--format", default="text",
        choices=["text", "json", "markdown", "html"],
    )
    p_report.add_argument("-o", "--output", help="输出文件路径")
    p_report.set_defaults(func=cmd_report)

    # init
    p_init = sub.add_parser("init", help="生成默认配置文件")
    p_init.add_argument("-o", "--output", default="skill_test.yaml")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    # serve (Web 平台)
    p_serve = sub.add_parser("serve", help="启动 Web 平台")
    _add_common_args(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.add_argument("-p", "--port", type=int, default=8080, help="端口")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "verbose"):
        level = "DEBUG" if args.verbose else "INFO"
        log_file = getattr(args, "log_file", None)
        setup_logging(level, log_file)

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
