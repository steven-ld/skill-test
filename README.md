# AI Skill 测试框架

对比不同 AI Skill 对代码生成质量的影响，支持并行执行、Git Worktree 隔离和多格式报告。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 生成默认配置文件
python -m skill_test init

# 编辑配置
# vim skill_test.yaml

# 执行测试
python -m skill_test run -c skill_test.yaml
```

## 命令

```
skill-test run       执行测试
skill-test list      列出 tasks / skills
skill-test report    从 JSON 重新生成报告
skill-test init      生成默认配置文件
```

### 执行测试

```bash
# 简单模式 — 串行执行
python -m skill_test run -c config.yaml -m simple

# 并行模式 — 线程池并发
python -m skill_test run -c config.yaml -m parallel

# 隔离模式 — 每个 task×skill 在独立 worktree 中
python -m skill_test run -c config.yaml -r /path/to/repo -m isolated

# 隔离 + 自动提交推送
python -m skill_test run -c config.yaml -r /path/to/repo --commit --push

# 只运行指定任务和 Skill
python -m skill_test run -c config.yaml -t task_001 -s write-expert

# 输出多种报告格式
python -m skill_test run -c config.yaml -f text json markdown html

# 启用 DEBUG 日志
python -m skill_test run -c config.yaml -v
```

### 重新生成报告

```bash
# 从已有 JSON 结果生成 Markdown
python -m skill_test report results/report_20260320.json -f markdown -o report.md
```

## 配置

运行 `python -m skill_test init` 生成带注释的 YAML 模板。

核心配置项：

| 配置 | 说明 |
|------|------|
| `cli.command` | Claude CLI 命令（Windows: `claude.cmd`） |
| `cli.timeout` | 单个任务超时（秒） |
| `max_workers` | 最大并行数 |
| `tasks[]` | 测试任务列表 |
| `skills[]` | Skill 配置列表 |
| `git.*` | Git worktree / 提交配置 |

环境变量覆盖：

| 变量 | 对应配置 |
|------|----------|
| `SKILL_TEST_TIMEOUT` | `cli.timeout` |
| `SKILL_TEST_MAX_WORKERS` | `max_workers` |
| `SKILL_TEST_OUTPUT_DIR` | `output_dir` |
| `SKILL_TEST_CLI_COMMAND` | `cli.command` |

## 项目结构

```
skill_test/
├── __init__.py        # 版本号
├── __main__.py        # python -m 入口
├── cli.py             # CLI 命令解析
├── config.py          # 配置加载（YAML + 环境变量）
├── models.py          # 统一数据模型
├── exceptions.py      # 异常体系
├── log.py             # 日志配置
├── executor.py        # Claude Code 执行器
├── git_manager.py     # Git / Worktree 操作
├── runner.py          # 测试编排器
└── reporter.py        # 多格式报告生成
```

## 架构设计

```
配置 (YAML / ENV)
    ↓
CLI (cli.py) → TestRunner (runner.py)
                    ├── ClaudeExecutor (executor.py)
                    ├── WorktreeManager (git_manager.py)
                    └── CommitManager (git_manager.py)
                            ↓
                     TaskResult (models.py)
                            ↓
                     Reporter (reporter.py) → Text / JSON / Markdown / HTML
```

## 执行模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `simple` | 串行执行 | 调试、少量任务 |
| `parallel` | 线程池并行 | 无需 Git 隔离的批量测试 |
| `isolated` | Worktree 并行 | 需要独立代码空间的测试 |
| `auto` | 有 repo 用 isolated，否则 parallel | 默认 |
