"""
AI 执行器 — 单一职责：调用 Claude Code CLI 并返回结构化结果。

不涉及 Git、文件变更检测、报告等逻辑。

Windows 兼容：
- 超长 system prompt 写入临时文件，避免命令行长度限制
- prompt 通过 stdin 管道传递
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .exceptions import ExecutionError, TimeoutError
from .log import get_logger
from .models import CLIConfig, RetryConfig, SkillConfig, TaskResult, TaskStatus

log = get_logger("executor")

_MAX_CLI_ARG_LEN = 4000


def _should_retry(result: TaskResult, retry: RetryConfig) -> bool:
    if result.status == TaskStatus.TIMEOUT and retry.retry_on_timeout:
        return True
    if result.status == TaskStatus.FAILED and retry.retry_on_failure:
        return True
    return False


def _retry_delay(attempt: int, retry: RetryConfig) -> float:
    delay = min(retry.base_delay * (2 ** attempt), retry.max_delay)
    return delay


class ClaudeExecutor:
    """Claude Code CLI 执行器。"""

    def __init__(self, config: CLIConfig | None = None):
        self.config = config or CLIConfig()
        self._validate_cli()

    def _validate_cli(self) -> None:
        """验证 CLI 命令可用。"""
        if not shutil.which(self.config.command):
            log.warning(
                "CLI 命令 '%s' 未在 PATH 中找到，执行时可能失败",
                self.config.command,
            )

    def _build_system_prompt(self, skill: SkillConfig) -> str | None:
        """从 SkillConfig 构建完整的 system prompt。"""
        if skill.is_baseline:
            return None

        parts: list[str] = []

        if skill.skill_file:
            path = Path(skill.skill_file)
            if path.exists():
                content = path.read_text(encoding="utf-8")
                header = f"## Skill: {skill.name}"
                if skill.tool:
                    header += f" [{skill.tool}]"
                parts.append(f"{header}\n\n{content}")

                for ref in skill.ref_files:
                    normalized_ref = ref.replace("\\", "/")
                    ref_path = path.parent / normalized_ref
                    if ref_path.exists():
                        ref_content = ref_path.read_text(encoding="utf-8")
                        parts.append(f"### {ref_path.name}\n\n{ref_content}")
            else:
                log.warning("Skill 文件不存在: %s", path)

        if skill.system_prompt:
            parts.append(skill.system_prompt)

        return "\n\n---\n\n".join(parts) if parts else None

    def _build_command_and_input(
        self,
        prompt: str,
        system_prompt: str | None,
        session_id: str | None = None,
    ) -> tuple[list[str], str, list[Path]]:
        """
        构建 CLI 命令，处理超长参数问题。

        Returns:
            (cmd, stdin_text, temp_files_to_cleanup)
        """
        cmd = [self.config.command] + list(self.config.base_args)
        temp_files: list[Path] = []

        if system_prompt:
            if len(system_prompt) > _MAX_CLI_ARG_LEN:
                # 写入临时文件，让 prompt 包含引用
                tf = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", encoding="utf-8",
                    delete=False, prefix="skill_test_sp_",
                )
                tf.write(system_prompt)
                tf.close()
                temp_files.append(Path(tf.name))
                cmd.extend([
                    "--system-prompt",
                    f"严格遵循文件 {tf.name} 中的所有规范和约束来完成任务。",
                ])
                prompt = (
                    f"请先阅读以下规范文件中的所有规则：{tf.name}\n"
                    f"然后严格按照规范执行以下任务：\n\n{prompt}"
                )
            else:
                cmd.extend(["--system-prompt", system_prompt])

        if session_id:
            cmd.extend(["--resume", session_id])

        # prompt 通过 stdin 传递，避免命令行过长
        cmd.append("-")

        return cmd, prompt, temp_files

    def execute_with_retry(
        self,
        prompt: str,
        *,
        skill: SkillConfig | None = None,
        task_dir: str | Path | None = None,
        timeout: int | None = None,
        retry: RetryConfig | None = None,
    ) -> TaskResult:
        """带重试的执行 — 自动重试超时或失败的任务。"""
        retry = retry or RetryConfig(max_retries=0)

        for attempt in range(retry.max_retries + 1):
            result = self.execute(
                prompt, skill=skill, task_dir=task_dir, timeout=timeout,
            )
            if result.success or attempt >= retry.max_retries:
                result.metadata["retries"] = attempt
                return result

            if not _should_retry(result, retry):
                result.metadata["retries"] = attempt
                return result

            delay = _retry_delay(attempt, retry)
            log.warning(
                "重试 %d/%d | skill=%s | 等待 %.1fs | 原因: %s",
                attempt + 1, retry.max_retries, (skill or SkillConfig(name="?")).name,
                delay, result.status.value,
            )
            time.sleep(delay)

        return result  # type: ignore

    def execute(
        self,
        prompt: str,
        *,
        skill: SkillConfig | None = None,
        task_dir: str | Path | None = None,
        timeout: int | None = None,
        session_id: str | None = None,
    ) -> TaskResult:
        """
        执行单次 AI 任务。

        Args:
            prompt:     用户 prompt
            skill:      Skill 配置（为 None 则使用 baseline）
            task_dir:   工作目录
            timeout:    超时秒数，覆盖配置中的默认值
            session_id: 恢复会话 ID

        Returns:
            TaskResult 包含执行状态、输出和耗时
        """
        skill = skill or SkillConfig(name="baseline")
        effective_timeout = timeout or self.config.timeout
        system_prompt = self._build_system_prompt(skill)
        cmd, stdin_text, temp_files = self._build_command_and_input(
            prompt, system_prompt, session_id,
        )

        log.info(
            "执行任务 | skill=%s | timeout=%ds | cwd=%s",
            skill.name, effective_timeout, task_dir or ".",
        )
        log.debug("命令: %s", cmd[0])
        log.debug("system_prompt 长度: %d", len(system_prompt or ""))

        result = TaskResult(skill_name=skill.name)
        start = time.monotonic()

        try:
            proc = subprocess.run(
                cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                encoding="utf-8",
                cwd=str(task_dir) if task_dir else None,
            )
            result.duration = time.monotonic() - start

            if proc.returncode == 0:
                result.status = TaskStatus.SUCCESS
                result.output = proc.stdout or ""
            else:
                result.status = TaskStatus.FAILED
                result.error = proc.stderr or proc.stdout or "未知错误"

        except subprocess.TimeoutExpired:
            result.duration = time.monotonic() - start
            result.status = TaskStatus.TIMEOUT
            result.error = f"任务超时（{effective_timeout}s）"
            log.warning("任务超时: %s", skill.name)

        except FileNotFoundError:
            result.duration = time.monotonic() - start
            result.status = TaskStatus.FAILED
            result.error = f"CLI 命令未找到: {self.config.command}"
            log.error("CLI 命令未找到: %s", self.config.command)

        except Exception as e:
            result.duration = time.monotonic() - start
            result.status = TaskStatus.FAILED
            result.error = str(e)
            log.error("执行异常: %s", e)

        finally:
            for tf in temp_files:
                try:
                    tf.unlink(missing_ok=True)
                except Exception:
                    pass

        log.info(
            "任务完成 | skill=%s | status=%s | %.1fs",
            skill.name, result.status.value, result.duration,
        )
        return result
