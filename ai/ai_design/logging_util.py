"""逐行日志工具。

约定：stdout 每行一个日志记录，格式 ``[status] ...`` / ``[tool] ...`` /
``[plan] ...`` / ``[error] ...`` / ``[warn] ...`` / ``[info] ...``。
UI 直接捕获 stdout 展示，因此必须逐行 flush，并保证 Windows 控制台/管道
在 GBK / UTF-8 下都不会因编码异常崩溃。
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

LEVELS = ("status", "tool", "plan", "ga", "error", "warn", "info", "debug")


def _safe_write(stream, text: str) -> None:
    """写一行文本；遇到编码不支持时用 replace 降级，绝不让日志拖垮主流程。"""
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except UnicodeEncodeError:
        try:
            stream.write(text.encode(getattr(stream, "encoding", None) or "utf-8",
                                     errors="replace").decode(
                getattr(stream, "encoding", None) or "utf-8", errors="replace"))
            stream.flush()
        except Exception:  # pragma: no cover - 极端环境兜底
            pass
    except Exception:  # pragma: no cover - 流已关闭等
        pass


class Logger:
    """控制台 + 内存尾部缓冲 + 可选文件日志。"""

    def __init__(self, log_file: str | Path | None = None, echo: bool = True,
                 stream=None, tail_size: int = 200):
        self.echo = echo
        self.stream = stream if stream is not None else sys.stdout
        self.tail_size = max(tail_size, 10)
        self._tail: deque[str] = deque(maxlen=self.tail_size)
        self._lock = threading.Lock()
        self._log_path: Path | None = Path(log_file) if log_file else None
        if self._log_path is not None:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                # 每次运行覆盖写，避免日志无限增长
                self._log_path.write_text("", encoding="utf-8")
            except OSError:
                self._log_path = None

    # ------------------------------------------------------------------ 基础
    def log(self, level: str, message: object) -> str:
        level = level.lower()
        if level not in LEVELS:
            level = "info"
        text = str(message).replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        stamp = datetime.now().strftime("%H:%M:%S")
        rendered = "\n".join(f"[{level}] {line}" for line in lines)
        with self._lock:
            for line in rendered.split("\n"):
                self._tail.append(line)
            if self.echo:
                _safe_write(self.stream, rendered + "\n")
            if self._log_path is not None:
                try:
                    with self._log_path.open("a", encoding="utf-8") as fh:
                        for line in rendered.split("\n"):
                            fh.write(f"{stamp} {line}\n")
                except OSError:
                    self._log_path = None
        return rendered

    # 常用速记
    def status(self, message: object) -> str:
        return self.log("status", message)

    def tool(self, message: object) -> str:
        return self.log("tool", message)

    def plan(self, message: object) -> str:
        return self.log("plan", message)

    def ga(self, message: object) -> str:
        return self.log("ga", message)

    def error(self, message: object) -> str:
        return self.log("error", message)

    def warn(self, message: object) -> str:
        return self.log("warn", message)

    def info(self, message: object) -> str:
        return self.log("info", message)

    def debug(self, message: object) -> str:
        return self.log("debug", message)

    # ------------------------------------------------------------------ 辅助
    def tail(self, n: int | None = None) -> list[str]:
        with self._lock:
            items = list(self._tail)
        if n is not None:
            items = items[-max(1, int(n)):]
        return items

    def tail_text(self, n: int | None = None) -> str:
        return "\n".join(self.tail(n))

    def child(self, prefix: str) -> "PrefixedLogger":
        return PrefixedLogger(self, prefix)


class PrefixedLogger:
    """给子流程日志统一加前缀（例如 ``[plan]`` 下的步骤编号）。"""

    def __init__(self, parent: Logger, prefix: str):
        self._parent = parent
        self._prefix = prefix

    def log(self, level: str, message: object) -> str:
        return self._parent.log(level, f"{self._prefix} {message}")

    def status(self, message: object) -> str:
        return self.log("status", message)

    def tool(self, message: object) -> str:
        return self.log("tool", message)

    def plan(self, message: object) -> str:
        return self.log("plan", message)

    def ga(self, message: object) -> str:
        return self.log("ga", message)

    def error(self, message: object) -> str:
        return self.log("error", message)

    def warn(self, message: object) -> str:
        return self.log("warn", message)

    def info(self, message: object) -> str:
        return self.log("info", message)

    def tail(self, n: int | None = None) -> list[str]:
        return self._parent.tail(n)


def make_logger(log_file: str | Path | None = None, echo: bool = True,
                stream=None) -> Logger:
    return Logger(log_file=log_file, echo=echo, stream=stream)


def log_lines(messages: Iterable[object], level: str = "info") -> None:
    """便捷函数：批量输出到当前 stdout（不依赖 Logger 实例）。"""
    for message in messages:
        _safe_write(sys.stdout, f"[{level}] {message}\n")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def elapsed_str(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}min"
