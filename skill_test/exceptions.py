"""统一异常体系。"""


class SkillTestError(Exception):
    """框架基础异常。"""


class ConfigError(SkillTestError):
    """配置相关错误：文件缺失、格式非法、必填项为空等。"""


class GitError(SkillTestError):
    """Git 操作失败：仓库不存在、worktree 创建失败、push 失败等。"""

    def __init__(self, message: str, stderr: str = ""):
        self.stderr = stderr
        super().__init__(f"{message}\n{stderr}" if stderr else message)


class ExecutionError(SkillTestError):
    """AI 执行器异常：CLI 不存在、超时、返回非零等。"""


class TimeoutError(ExecutionError):
    """AI 任务执行超时。"""

    def __init__(self, timeout: int):
        self.timeout = timeout
        super().__init__(f"任务超时（{timeout}s）")
