"""
OpenAI / OpenAI-compatible API 执行器。

支持两种模式：
- responses: OpenAI Responses API
- chat_completions: OpenAI-compatible Chat Completions + function tool loop

设计目标：
- 保持与 ClaudeExecutor 相同的 execute / execute_with_retry 接口
- 不依赖第三方 SDK，便于在受限环境中直接工作
- 允许接入 MiniMax 这类仅兼容 OpenAI Chat Completions 的网关
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from .config import resolve_skill_file_path
from .log import get_logger
from .models import OpenAIResponsesConfig, RetryConfig, SkillConfig, TaskResult, TaskStatus

log = get_logger("openai_executor")


def _should_retry(result: TaskResult, retry: RetryConfig) -> bool:
    if result.status == TaskStatus.TIMEOUT and retry.retry_on_timeout:
        return True
    if result.status == TaskStatus.FAILED and retry.retry_on_failure:
        return True
    return False


def _retry_delay(attempt: int, retry: RetryConfig) -> float:
    return min(retry.base_delay * (2 ** attempt), retry.max_delay)


def _timeout_for_attempt(
    base_timeout: int | None,
    retry: RetryConfig,
    timeout_retry_count: int,
) -> int | None:
    if base_timeout is None:
        return None
    return base_timeout + retry.timeout_increment_on_timeout * timeout_retry_count


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class OpenAIResponsesExecutor:
    """OpenAI / 兼容网关执行器。"""

    def __init__(self, config: OpenAIResponsesConfig | None = None):
        self.config = config or OpenAIResponsesConfig()

    def _resolve_api_key(self) -> str:
        return (self.config.api_key or os.environ.get(self.config.api_key_env, "")).strip()

    def _resolve_base_url(self) -> str:
        base_url = (self.config.base_url or os.environ.get("OPENAI_BASE_URL", "")).strip()
        return base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"未配置 API Key，请设置 {self.config.api_key_env}")
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _post_json(self, path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        base_url = self._resolve_base_url()
        if not base_url:
            raise RuntimeError("未配置 openai.base_url / OPENAI_BASE_URL")
        url = f"{base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url, data=body, headers=self._headers(), method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"网络请求失败: {exc.reason}") from exc
        return json.loads(raw)

    def _build_system_prompt(self, skill: SkillConfig) -> str | None:
        if skill.is_baseline:
            return None

        parts: list[str] = []
        if skill.skill_file:
            resolved_skill_file = resolve_skill_file_path(skill.name, skill.skill_file)
            path = Path(resolved_skill_file or skill.skill_file)
            if resolved_skill_file:
                skill.skill_file = resolved_skill_file
            if path.exists():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
                for ref in skill.ref_files:
                    ref_path = path.parent / ref.replace("\\", "/")
                    if ref_path.exists():
                        parts.append(
                            f"## Reference: {ref_path.name}\n\n{ref_path.read_text(encoding='utf-8', errors='replace')}"
                        )

        if skill.system_prompt:
            parts.append(skill.system_prompt)

        return "\n\n---\n\n".join(part for part in parts if part) or None

    def _build_responses_tools(self) -> list[dict[str, Any]]:
        if self.config.tool_type == "local_shell":
            return [{"type": "local_shell"}]
        return [{"type": "shell", "environment": {"type": "local"}}]

    def _build_chat_completion_tools(self) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "在本地工作目录执行命令并返回 stdout/stderr/exit_code。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "需要执行的 shell 命令"},
                        "working_directory": {"type": "string", "description": "相对或绝对工作目录"},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }]

    def _run_local_command(
        self,
        command: str | list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str] | None,
        timeout_ms: int | None,
        max_output_length: int | None,
    ) -> dict[str, Any]:
        if isinstance(command, str):
            argv = ["/bin/zsh", "-lc", command]
        else:
            argv = list(command)
        timeout_seconds = (min(timeout_ms or self.config.shell_timeout_ms, self.config.shell_timeout_ms)) / 1000
        base_env = os.environ.copy()
        base_env["PATH"] = os.pathsep.join([
            path for path in [
                base_env.get("PATH", ""),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
            if path
        ])

        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                env={**base_env, **(env or {})},
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                encoding="utf-8",
                errors="replace",
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            limit = min(max_output_length or self.config.max_output_chars, self.config.max_output_chars)
            return {
                "stdout": stdout[:limit],
                "stderr": stderr[:limit],
                "exit_code": completed.returncode,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            limit = self.config.max_output_chars
            return {
                "stdout": stdout[:limit],
                "stderr": stderr[:limit],
                "exit_code": None,
                "timed_out": True,
            }

    def _execute_shell_call(self, call: dict[str, Any], *, cwd: str | Path | None) -> dict[str, Any]:
        args = _get(call, "action") or _get(call, "arguments") or {}
        commands = _get(args, "commands")
        if not commands:
            command = _get(args, "command")
            commands = [command] if command else []
        elif not isinstance(commands, list):
            commands = [commands]
        timeout_ms = _get(args, "timeout_ms")
        max_output_length = _get(args, "max_output_length") or self.config.max_output_chars
        env = _get(args, "env") or {}
        working_directory = _get(args, "working_directory") or cwd
        output = []
        for command in commands:
            if not command:
                continue
            item = self._run_local_command(
                command,
                cwd=working_directory,
                env=env,
                timeout_ms=timeout_ms,
                max_output_length=max_output_length,
            )
            output.append({
                "stdout": item["stdout"],
                "stderr": item["stderr"],
                "outcome": {"type": "timeout"} if item["timed_out"] else {"type": "exit", "exit_code": item["exit_code"]},
            })

        item_type = "local_shell_call_output" if self.config.tool_type == "local_shell" else "shell_call_output"
        payload: dict[str, Any] = {"type": item_type, "call_id": _get(call, "call_id")}
        if item_type == "shell_call_output":
            payload["max_output_length"] = max_output_length
            payload["output"] = output
        else:
            joined = "\n\n".join(
                f"$ {commands[idx]}\n{entry['stdout']}{entry['stderr']}".strip()
                for idx, entry in enumerate(output)
            ).strip()
            payload["output"] = joined
        return payload

    def _responses_create(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        previous_response_id: str | None = None,
        input_items: Any = None,
        timeout: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "tools": self._build_responses_tools(),
            "store": self.config.store,
            "input": input_items if previous_response_id else (input_items if input_items is not None else prompt),
        }
        if system_prompt:
            payload["instructions"] = system_prompt
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if self.config.reasoning_effort:
            payload["reasoning"] = {"effort": self.config.reasoning_effort}
        return self._post_json("/responses", payload, timeout)

    def _responses_execute(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        task_dir: str | Path | None,
        timeout: int,
        session_id: str | None = None,
    ) -> TaskResult:
        result = TaskResult()
        start = time.monotonic()
        response = self._responses_create(
            prompt=prompt,
            system_prompt=system_prompt,
            previous_response_id=session_id,
            timeout=timeout,
        )
        rounds = 0

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                result.duration = elapsed
                result.status = TaskStatus.TIMEOUT
                result.error = f"任务超时（{timeout}s）"
                return result

            outputs = list(response.get("output", []) or [])
            shell_calls = [item for item in outputs if item.get("type") in {"shell_call", "local_shell_call"}]
            if not shell_calls:
                result.duration = elapsed
                result.status = TaskStatus.SUCCESS
                result.output = response.get("output_text", "")
                result.metadata["response_id"] = response.get("id", "")
                result.metadata["provider"] = "openai_responses"
                result.metadata["api_mode"] = "responses"
                result.metadata["cloud_store_requested"] = bool(self.config.store)
                if self.config.store:
                    stored = response.get("store")
                    result.metadata["cloud_stored"] = bool(
                        stored if stored is not None else response.get("id")
                    )
                return result

            rounds += 1
            if rounds > self.config.max_tool_rounds:
                result.duration = elapsed
                result.status = TaskStatus.FAILED
                result.error = f"超过最大工具回合数: {self.config.max_tool_rounds}"
                return result

            input_items = [self._execute_shell_call(call, cwd=task_dir) for call in shell_calls]
            response = self._responses_create(
                prompt=prompt,
                system_prompt=system_prompt,
                previous_response_id=response.get("id"),
                input_items=input_items,
                timeout=timeout,
            )

    def _chat_completions_create(
        self,
        *,
        messages: list[dict[str, Any]],
        timeout: int,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "tools": self._build_chat_completion_tools(),
        }
        if tool_choice:
            payload["tool_choice"] = tool_choice
        return self._post_json("/chat/completions", payload, timeout)

    def _chat_completions_execute(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        task_dir: str | Path | None,
        timeout: int,
    ) -> TaskResult:
        result = TaskResult()
        start = time.monotonic()
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        rounds = 0

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                result.duration = elapsed
                result.status = TaskStatus.TIMEOUT
                result.error = f"任务超时（{timeout}s）"
                return result

            response = self._chat_completions_create(messages=messages, timeout=timeout)
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if tool_calls:
                rounds += 1
                if rounds > self.config.max_tool_rounds:
                    result.duration = elapsed
                    result.status = TaskStatus.FAILED
                    result.error = f"超过最大工具回合数: {self.config.max_tool_rounds}"
                    return result

                messages.append(message)
                for tool_call in tool_calls:
                    fn = tool_call.get("function") or {}
                    if fn.get("name") != "run_command":
                        continue
                    raw_args = fn.get("arguments") or "{}"
                    args = json.loads(raw_args)
                    cmd_result = self._run_local_command(
                        args.get("command", ""),
                        cwd=args.get("working_directory") or task_dir,
                        env={},
                        timeout_ms=self.config.shell_timeout_ms,
                        max_output_length=self.config.max_output_chars,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": json.dumps(cmd_result, ensure_ascii=False),
                    })
                continue

            result.duration = elapsed
            result.status = TaskStatus.SUCCESS
            result.output = message.get("content", "") or ""
            result.metadata["provider"] = "openai_compatible"
            result.metadata["api_mode"] = "chat_completions"
            result.metadata["cloud_store_requested"] = False
            result.metadata["cloud_stored"] = False
            return result

    def execute_with_retry(
        self,
        prompt: str,
        *,
        skill: SkillConfig | None = None,
        task_dir: str | Path | None = None,
        timeout: int | None = None,
        retry: RetryConfig | None = None,
    ) -> TaskResult:
        retry = retry or RetryConfig(max_retries=0)
        timeout_retry_count = 0
        total_duration = 0.0

        for attempt in range(retry.max_retries + 1):
            effective_timeout = _timeout_for_attempt(timeout, retry, timeout_retry_count)
            result = self.execute(
                prompt,
                skill=skill,
                task_dir=task_dir,
                timeout=effective_timeout,
            )
            total_duration += result.duration
            result.metadata["effective_timeout"] = effective_timeout
            if result.success or attempt >= retry.max_retries:
                result.duration = total_duration
                result.metadata["retries"] = attempt
                return result
            if not _should_retry(result, retry):
                result.duration = total_duration
                result.metadata["retries"] = attempt
                return result

            delay = _retry_delay(attempt, retry)
            total_duration += delay
            if result.status == TaskStatus.TIMEOUT:
                timeout_retry_count += 1
            log.warning(
                "兼容 API 重试 %d/%d | skill=%s | 等待 %.1fs | 原因: %s",
                attempt + 1,
                retry.max_retries,
                (skill or SkillConfig(name="?")).name,
                delay,
                result.status.value,
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
        skill = skill or SkillConfig(name="baseline")
        effective_timeout = timeout or self.config.timeout
        system_prompt = self._build_system_prompt(skill)

        try:
            if self.config.api_mode == "chat_completions":
                result = self._chat_completions_execute(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    task_dir=task_dir,
                    timeout=effective_timeout,
                )
            else:
                result = self._responses_execute(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    task_dir=task_dir,
                    timeout=effective_timeout,
                    session_id=session_id,
                )
        except Exception as exc:
            result = TaskResult(
                status=TaskStatus.FAILED,
                error=str(exc),
            )
            log.error("兼容 API 执行异常: %s", exc)

        result.skill_name = skill.name
        return result
