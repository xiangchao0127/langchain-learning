"""半结构化文本解析器：应用日志、键值对文本。

这类文件没有严格的 schema，但存在可被规则识别的行级模式，
是「半结构化文件」最常见的形态。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..loaders import read_lines
from .base import BaseParser

# 常见日志行：2026-09-18 10:12:31,123 INFO  [main] c.e.OrderService - 创建订单成功
LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"
    r"(?:\s+(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL))?"
    r"(?:\s+\[(?P<thread>[^\]]*)\])?"
    r"(?:\s+(?P<logger>[A-Za-z_][\w.$:/\-]*))?"
    r"\s*[-:>]?\s*(?P<message>.*)$",
    re.IGNORECASE,
)

# 键值对行：key: value / key=value
KV_LINE_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][\w.\- ]{0,60}?)\s*[:=]\s*(?P<value>.+?)\s*$")


def normalize_timestamp(value: str | None) -> str | None:
    """把各种时间写法统一成 Doris DATETIME 可接受的 'YYYY-MM-DD HH:MM:SS'。"""
    if not value:
        return None
    text = value.strip().replace("/", "-").replace("T", " ").replace(",", ".")
    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    if match:
        return match.group(1)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if match:
        return f"{match.group(1)} 00:00:00"
    return value.strip()


class LogParser(BaseParser):
    """逐行解析应用日志，把时间 / 级别 / 来源 / 内容拆成字段。

    无法匹配日志头的行会被视为上一条记录的续行（堆栈信息常见形态）。
    """

    name = "log"
    description = "应用日志（时间 + 级别 + 来源 + 内容）"
    extensions = (".log",)

    def parse(self, path: str | Path) -> list[dict[str, Any]]:
        path = Path(path)
        records: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        for line in read_lines(path):
            if not line.strip():
                continue
            match = LOG_LINE_RE.match(line)
            if match and match.group("timestamp"):
                current = {
                    "log_time": normalize_timestamp(match.group("timestamp")),
                    "level": (match.group("level") or "").upper() or None,
                    "logger": (match.group("logger") or "").strip() or None,
                    "message": (match.group("message") or "").strip(),
                    "source": path.name,
                }
                records.append(current)
            elif current is not None:
                current["message"] = f"{current['message']}\n{line.rstrip()}"
            else:
                current = {
                    "log_time": None,
                    "level": None,
                    "logger": None,
                    "message": line.strip(),
                    "source": path.name,
                }
                records.append(current)
        return records

    def sniff(self, path: str | Path, sample: str | None = None) -> bool:
        sample = sample or self.sample(path)
        lines = [line for line in sample.splitlines() if line.strip()][:20]
        if not lines:
            return False
        hits = sum(1 for line in lines if LOG_LINE_RE.match(line))
        return hits / len(lines) >= 0.5


class KeyValueParser(BaseParser):
    """解析键值对文本，按空行 / 分区切成多条记录。"""

    name = "key_value"
    description = "键值对文本（key: value / key=value / ini 分区）"
    extensions = (".txt", ".kv", ".conf", ".ini", ".properties", ".env")

    def parse(self, path: str | Path) -> list[dict[str, Any]]:
        path = Path(path)
        records: list[dict[str, Any]] = []
        current: dict[str, Any] = {}

        def flush() -> None:
            if current:
                records.append(dict(current))
                current.clear()

        for line in read_lines(path):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                flush()
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                flush()
                current["section"] = stripped[1:-1]
                continue
            match = KV_LINE_RE.match(stripped)
            if match:
                current[match.group("key").strip()] = match.group("value").strip()
            else:
                current["text"] = f"{current.get('text', '')}\n{stripped}".strip()
        flush()
        return records

    def sniff(self, path: str | Path, sample: str | None = None) -> bool:
        sample = sample or self.sample(path)
        lines = [
            line
            for line in sample.splitlines()
            if line.strip() and not line.strip().startswith(("#", ";"))
        ][:20]
        if not lines:
            return False
        hits = sum(1 for line in lines if KV_LINE_RE.match(line))
        return hits / len(lines) >= 0.5


__all__ = ["LogParser", "KeyValueParser", "normalize_timestamp"]
