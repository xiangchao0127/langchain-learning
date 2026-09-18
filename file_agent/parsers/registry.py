"""解析器注册表：按扩展名 / 内容自动选择合适的解析器。"""

from __future__ import annotations

from pathlib import Path

from ..loaders import file_meta, preview_text
from ..schemas import ParseResult
from .base import BaseParser
from .semi_structured import KeyValueParser, LogParser
from .structured import CsvParser, JsonParser, YamlParser

# 默认解析器，顺序即优先级（越靠前越优先）
DEFAULT_PARSER_CLASSES: tuple[type[BaseParser], ...] = (
    JsonParser,
    CsvParser,
    YamlParser,
    LogParser,
    KeyValueParser,
)

# 这些扩展名无法直接判定，需要基于内容嗅探
AMBIGUOUS_EXTENSIONS = {"", ".txt", ".text", ".dat"}


class ParserRegistry:
    """管理解析器集合，并提供自动识别能力。"""

    def __init__(self, parsers: list[BaseParser] | None = None) -> None:
        self._parsers: list[BaseParser] = (
            list(parsers) if parsers is not None else [cls() for cls in DEFAULT_PARSER_CLASSES]
        )

    # --------------------------- 注册与查询 --------------------------- #
    def register(self, parser: BaseParser, *, first: bool = False) -> None:
        if first:
            self._parsers.insert(0, parser)
        else:
            self._parsers.append(parser)

    def all(self) -> list[BaseParser]:
        return list(self._parsers)

    def names(self) -> list[str]:
        return [parser.name for parser in self._parsers]

    def get(self, name: str | None) -> BaseParser | None:
        if not name:
            return None
        for parser in self._parsers:
            if parser.name == name:
                return parser
        return None

    def by_extension(self, extension: str) -> BaseParser | None:
        extension = extension.lower()
        for parser in self._parsers:
            if parser.supports_extension(extension):
                return parser
        return None

    # --------------------------- 自动识别 --------------------------- #
    def detect(self, path: str | Path) -> BaseParser:
        """选择最合适的解析器。

        策略：
            1. 扩展名明确（.json/.csv/.yaml/.log 等）→ 直接使用；
            2. 扩展名有歧义（.txt / 无扩展名）→ 逐个 sniff，取第一个命中；
            3. 未知扩展名 → 先 sniff，全部失败则退化为键值对解析器。
        """
        path = Path(path)
        extension = path.suffix.lower()
        sample = preview_text(path, max_chars=8192)

        if extension in AMBIGUOUS_EXTENSIONS:
            for parser in self._parsers:
                if parser.sniff(path, sample):
                    return parser
        else:
            parser = self.by_extension(extension)
            if parser is not None:
                return parser
            for candidate in self._parsers:
                if candidate.sniff(path, sample):
                    return candidate

        return self.get("key_value") or self._parsers[-1]

    # --------------------------- 执行解析 --------------------------- #
    def parse(self, path: str | Path, parser_name: str | None = None) -> ParseResult:
        """解析文件并返回统一结果对象。"""
        path = Path(path)
        parser = self.get(parser_name) or self.detect(path)
        errors: list[str] = []
        records: list[dict] = []
        try:
            records = parser.parse(path)
        except Exception as exc:  # noqa: BLE001 - 解析失败需要向上汇报而非中断
            errors.append(f"{type(exc).__name__}: {exc}")

        meta = file_meta(path)
        meta["available_parsers"] = self.names()
        return ParseResult(
            source=str(path.resolve()),
            parser=parser.name,
            record_count=len(records),
            records=records,
            errors=errors,
            meta=meta,
        )


def build_default_registry() -> ParserRegistry:
    return ParserRegistry()


__all__ = ["ParserRegistry", "build_default_registry", "DEFAULT_PARSER_CLASSES"]
