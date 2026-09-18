"""文件读取工具：编码探测、文本预览、分块。"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any, Iterator

import chardet

DEFAULT_SAMPLE_SIZE = 64 * 1024


def detect_encoding(path: str | Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> str:
    """探测文件编码，失败时回退 utf-8。"""
    path = Path(path)
    with path.open("rb") as fh:
        raw = fh.read(sample_size)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if not raw:
        return "utf-8"
    result = chardet.detect(raw)
    encoding = (result.get("encoding") or "utf-8").lower()
    # 中文环境下 GB2312/GBK 经常被识别为 GB2312，统一用 GB18030 兼容性更好
    if encoding in {"gb2312", "gbk"}:
        encoding = "gb18030"
    return encoding


def read_text(path: str | Path, encoding: str | None = None, errors: str = "replace") -> str:
    """读取文本文件，自动探测编码。"""
    path = Path(path)
    encoding = encoding or detect_encoding(path)
    return path.read_text(encoding=encoding, errors=errors)


def read_lines(
    path: str | Path,
    encoding: str | None = None,
    errors: str = "replace",
) -> list[str]:
    """按行读取，保留原始内容（不剥离换行符）。"""
    path = Path(path)
    encoding = encoding or detect_encoding(path)
    with path.open("r", encoding=encoding, errors=errors) as fh:
        return fh.read().splitlines()


def first_lines(path: str | Path, limit: int = 20, encoding: str | None = None) -> list[str]:
    return read_lines(path, encoding=encoding)[:limit]


def preview_text(path: str | Path, max_chars: int = 2000, encoding: str | None = None) -> str:
    """截取文件开头若干字符，用于给 LLM 提供样例。"""
    path = Path(path)
    encoding = encoding or detect_encoding(path)
    with path.open("r", encoding=encoding, errors="replace") as fh:
        return fh.read(max_chars)


def count_lines(path: str | Path, encoding: str | None = None) -> int:
    path = Path(path)
    encoding = encoding or detect_encoding(path)
    with path.open("r", encoding=encoding, errors="replace") as fh:
        return sum(1 for _ in fh)


def chunk_lines(lines: list[str], chunk_size: int) -> Iterator[list[str]]:
    """把行列表切成固定大小的块，避免一次性塞给 LLM。"""
    if chunk_size <= 0:
        yield lines
        return
    for start in range(0, len(lines), chunk_size):
        yield lines[start : start + chunk_size]


def file_meta(path: str | Path) -> dict[str, Any]:
    """文件基础元信息，便于智能体决策。"""
    path = Path(path)
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "size_human": _human_size(stat.st_size),
        "encoding": detect_encoding(path),
        "modified_at": _format_mtime(stat.st_mtime),
    }


def _human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def _format_mtime(timestamp: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def find_files(directory: str | Path, pattern: str = "*", recursive: bool = False) -> list[Path]:
    """按 glob 查找文件。"""
    directory = Path(directory)
    if not directory.exists():
        return []
    iterator = directory.rglob(pattern) if recursive else directory.glob(pattern)
    return sorted(p for p in iterator if p.is_file())


# 兼容：某些场景下需要限制长任务耗时
def with_timeout(seconds: int):  # pragma: no cover - 仅 POSIX 有效
    def decorator(func):
        def wrapper(*args, **kwargs):
            if not hasattr(signal, "SIGALRM"):
                return func(*args, **kwargs)

            def _handler(signum, frame):
                raise TimeoutError(f"执行超时（{seconds}s）")

            old = signal.signal(signal.SIGALRM, _handler)
            signal.alarm(seconds)
            try:
                return func(*args, **kwargs)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)

        return wrapper

    return decorator
