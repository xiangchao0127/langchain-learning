"""解析器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..loaders import preview_text


class ParserError(RuntimeError):
    """解析失败时抛出。"""


class BaseParser(ABC):
    """所有解析器的父类。

    子类需要提供：
        name         解析器标识
        extensions   关联的文件扩展名
        parse()      把文件转成 ``list[dict]``
    可选实现 ``sniff()``，用于扩展名无法判断时基于内容识别。
    """

    name: str = "base"
    description: str = ""
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, path: str | Path) -> list[dict[str, Any]]:
        """把文件解析为结构化记录列表。"""

    def sniff(self, path: str | Path, sample: str | None = None) -> bool:
        """基于内容特征判断本解析器是否适用。默认不支持。"""
        return False

    def supports_extension(self, extension: str) -> bool:
        return extension.lower() in self.extensions

    @staticmethod
    def sample(path: str | Path, max_chars: int = 8192) -> str:
        """读取一段样例内容。"""
        return preview_text(path, max_chars=max_chars)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name}>"


def normalize_records(data: Any) -> list[dict[str, Any]]:
    """把任意 JSON/YAML 结构规整为记录列表。

    支持的形式：
        [ {...}, {...} ]                       -> 直接使用
        { "records": [...] } / data / items    -> 取出内层数组
        { ... }                                -> 单条记录
        标量                                    -> {"value": 标量}
    """
    if isinstance(data, list):
        return [item if isinstance(item, dict) else {"value": item} for item in data]
    if isinstance(data, dict):
        for key in ("records", "data", "items", "rows", "results", "list", "content"):
            inner = data.get(key)
            if isinstance(inner, list):
                return normalize_records(inner)
        return [data]
    if data is None:
        return []
    return [{"value": data}]
