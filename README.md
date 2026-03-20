# AI Skill 测试平台

用于量化 Skill 带来的真实提升，支持 A/B Test、双实验模式、Git 交付追踪和平台化管理界面。

当前版本重点能力：

- `技术方案模式`：同一模型下，对比 `普通提示词基线` 与 `Skill 增强` 产出的详细技术方案
- `Coding 模式`：同一模型下，对比 `普通提示词基线` 与 `Skill 增强` 的真实代码交付
- `Git 交付记录优先`：测试结果可绑定 worktree、提交 hash 和推送状态，交付以 Git 记录为准
- `多工具 Skill 扫描`：可扫描项目内 Claude / Cursor / Codex / Gemini Skill，并在前端按工具来源展示、按需勾选引用文件
- `平台化管理体验`：通过 Web 平台完成配置加载、实验设计、运行监控、交付追踪与结果对比
- `微服务工作区支持`：`repo_path` 可以是包含多个子仓库的工作区根目录，平台会结合 `work_dir` 和任务文本自动尝试匹配目标子仓库
- `配置中心`：可直接在平台中编辑 `test_user_count.yaml` 这类 YAML，并回填到主工作台启动实验

## 快速开始

```bash
# 安装依赖
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 生成默认配置文件
python -m skill_test init

# 编辑配置
# vim skill_test.yaml

# 启动平台
python -m skill_test serve -c skill_test.yaml

# 或命令行执行测试
python -m skill_test run -c skill_test.yaml --experiment-mode coding
```

## 命令

```
skill-test run       执行测试
skill-test list      列出 tasks / skills
skill-test report    从 JSON 重新生成报告
skill-test init      生成默认配置文件
skill-test serve     启动 Web 平台
```

### 执行测试

```bash
# 简单模式 — 串行执行
python -m skill_test run -c config.yaml -m simple

# 并行模式 — 线程池并发
python -m skill_test run -c config.yaml -m parallel

# 隔离模式 — 每个 task×skill 在独立 worktree 中（默认）
python -m skill_test run -c config.yaml -r /path/to/repo -m isolated

# 隔离 + 自动提交推送
python -m skill_test run -c config.yaml -r /path/to/repo --commit --push

# 强制使用技术方案模式
python -m skill_test run -c config.yaml -r /path/to/repo --experiment-mode solution --commit

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
| `cli.timeout` | 单个任务超时（秒，`coding` 模式默认至少 1000s） |
| `openai.enabled` | 是否切换到 OpenAI Responses API 运行时 |
| `openai.model` | Responses API 使用的模型名 |
| `openai.api_mode` | `responses` 或 `chat_completions`，用于兼容不同网关能力 |
| `openai.tool_type` | `shell` 或 `local_shell`，用于 coding agent 本地命令回环 |
| `retry.timeout_increment_on_timeout` | 因超时进入重试时，下一次额外增加的秒数（默认 300s） |
| `max_workers` | 最大并行数 |
| `tasks[]` | 测试任务列表 |
| `skills[]` | Skill 配置列表 |
| `git.*` | Git worktree / 提交配置 |

任务新增关键字段：

| 字段 | 说明 |
|------|------|
| `tasks[].mode` | `coding` 或 `solution`，用于定义默认实验模式 |
| `tasks[].repo_targets` | 任务涉及的目标模块列表，如 `cloud-member` / `cloud-common` |

Skill 新增关键字段：

| 字段 | 说明 |
|------|------|
| `skills[].tool` | Skill 所属工具，如 `claude` / `cursor` / `codex` |
| `skills[].origin` | Skill 来源标识，用于平台显示 |
| `skills[].ref_files` | 可被选择引用的参考文件列表 |

环境变量覆盖：

| 变量 | 对应配置 |
|------|----------|
| `SKILL_TEST_TIMEOUT` | `cli.timeout` |
| `SKILL_TEST_MAX_WORKERS` | `max_workers` |
| `SKILL_TEST_OUTPUT_DIR` | `output_dir` |
| `SKILL_TEST_CLI_COMMAND` | `cli.command` |
| `OPENAI_API_KEY` / `SKILL_TEST_OPENAI_API_KEY` | `openai.api_key` |
| `OPENAI_BASE_URL` / `SKILL_TEST_OPENAI_BASE_URL` | `openai.base_url` |
| `OPENAI_MODEL` / `SKILL_TEST_OPENAI_MODEL` | `openai.model` |

### OpenAI Responses API 骨架

仓库内置了一个可选的 OpenAI Responses API 执行器骨架，路径为 [openai_executor.py](/Users/ga666666/Desktop/skill-test/skill_test/openai_executor.py)。

- 当 `openai.enabled=true` 时，`TestRunner` 会改用 Responses API 执行，而不是本地 `claude` CLI。
- 默认采用 OpenAI 官方推荐的本地 `shell` 回环模式；模型发出命令，你的本地 runtime 执行，再把 `shell_call_output` 回传给模型。
- 如果目标网关只兼容 OpenAI Chat Completions，可以将 `openai.api_mode` 设为 `chat_completions`；仓库内置了这一兼容回环实现。
- 如果你要对接兼容网关，可以在 `openai.base_url` 中指定；前提是该网关实现了 `/v1/responses` 与 shell 工具协议。
- 示例配置见 [openai_responses_example.yaml](/Users/ga666666/Desktop/skill-test/openai_responses_example.yaml)。
- MiniMax 兼容示例见 [minimax_compatible_example.yaml](/Users/ga666666/Desktop/skill-test/minimax_compatible_example.yaml)。

## 平台化工作流

1. 加载 YAML 配置，明确实验任务和默认 Skill 集合
2. 在平台中选择实验模式：`技术方案模式` 或 `Coding 模式`
3. 扫描目标项目中的 Skill，按工具来源查看并勾选需要引用的 reference 文件
4. 启动 A/B Test：基线组使用普通提示词，实验组使用所选 Skill
5. 在平台中查看实时运行、交付文档、提交 hash、推送状态和最终对比结果

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
├── runner.py          # 测试编排器（含实验模式注入）
├── server.py          # 平台 API / WebSocket / Web 仪表盘
├── discovery.py       # 多工具 Skill 扫描与引用发现
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
| `isolated` | Worktree 并行 | 默认模式，适合需要独立代码空间和 Git 交付记录的测试 |
| `auto` | 有 repo 用 isolated，否则 parallel | 默认 |

## 实验模式

| 模式 | 说明 | 默认交付 |
|------|------|----------|
| `solution` | 输出详细技术方案，不直接改业务代码 | `.skill-test/deliverables/<task>/<skill>-technical-plan.md` |
| `coding` | 直接落代码并补充交付说明 | `.skill-test/deliverables/<task>/<skill>-delivery.md` |

两种实验模式都可以与 `baseline` 和任意 Skill 形成 A/B 对照，确保对比对象使用同一模型，仅改变 Skill 使用方式。
