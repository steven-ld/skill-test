"""统一日志配置。"""

import logging
import sys


def setup_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    """初始化并返回框架根 logger。"""
    logger = logging.getLogger("skill_test")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False)
    console = logging.StreamHandler(stream)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    """获取子 logger。"""
    base = "skill_test"
    return logging.getLogger(f"{base}.{name}" if name else base)
