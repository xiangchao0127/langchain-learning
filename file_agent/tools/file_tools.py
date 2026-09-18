"""文件解析相关的 LangChain 工具。

所有工具都返回 JSON 字符串，便于模型直接阅读与串联。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain.tools import tool

from ..extractor import LLMExtractor
from ..llm import is_llm_available
from ..loaders import file_meta, find_files
from ..parsers import build_default_registry
from ..pipeline import table_name_from_path
from ..schemas import heuristic_table_spec

_REGISTRY = build_default_registry()
_MAX_LISTED_FILES = 200


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@tool
def list_input_files(directory: str, pattern: str = "*", recursive: bool = False) -> str:
    """列出指定目录下待解析的文件。

    Args:
        directory: 目录路径
        pattern: 文件名通配符，默认 "*"（全部文件）
        recursive: 是否递归子目录

    Returns:
        JSON，包含文件总数与每个文件的大小 / 扩展名 / 编码。
    """
    files = find_files(directory, pattern=pattern, recursive=recursive)
    if not files:
        return _dumps({"count": 0, "files": [], "hint": f"目录 {directory} 下没有匹配 {pattern} 的文件"})

    metas = []
    for path in files[:_MAX_LISTED_FILES]:
        try:
            metas.append(file_meta(path))
        except OSError:
            continue
    return _dumps({"count": len(files), "files": metas})


@tool
def preview_file(path: str, max_chars: int = 2000) -> str:
    """预览文件开头内容，用于判断格式与字段。

    Args:
        path: 文件路径
        max_chars: 最多返回的字符数
    """
    from ..loaders import preview_text

    source = Path(path)
    if not source.exists():
        return _dumps({"error": f"文件不存在: {path}"})

    meta = file_meta(source)
    return _dumps({"meta": meta, "content": preview_text(source, max_chars=max_chars)})


@tool
def detect_file_format(path: str) -> str:
    """识别文件格式，返回推荐的解析器与探测到的编码。

    Args:
        path: 文件路径
    """
    source = Path(path)
    if not source.exists():
        return _dumps({"error": f"文件不存在: {path}"})

    parser = _REGISTRY.detect(source)
    meta = file_meta(source)
    meta.update(
        {
            "recommended_parser": parser.name,
            "parser_description": parser.description,
            "available_parsers": _REGISTRY.names(),
        }
    )
    return _dumps(meta)


@tool
def parse_file(path: str, parser: str = "", limit: int = 10) -> str:
    """把文件解析为结构化记录（JSON / CSV / YAML / 日志 / 键值对）。

    Args:
        path: 文件路径
        parser: 指定解析器名称，留空自动识别。可选：json / csv / yaml / log / key_value
        limit: 返回的样例记录条数上限
    """
    source = Path(path)
    if not source.exists():
        return _dumps({"error": f"文件不存在: {path}"})

    result = _REGISTRY.parse(source, parser_name=parser or None)
    columns: list[str] = []
    seen: set[str] = set()
    for record in result.records[:50]:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    return _dumps(
        {
            "source": result.source,
            "parser": result.parser,
            "record_count": result.record_count,
            "errors": result.errors,
            "columns": columns,
            "sample_records": result.records[:limit],
        }
    )


@tool
def infer_target_schema(path: str, table_name: str = "", sample_size: int = 5) -> str:
    """从文件样例推断 Doris 目标表结构，并给出建表 DDL。

    优先使用 LLM 推断；未配置 API Key 时自动退化为规则推断。

    Args:
        path: 文件路径
        table_name: 目标表名，留空则由文件名推导
        sample_size: 参与推断的样例记录条数
    """
    source = Path(path)
    if not source.exists():
        return _dumps({"error": f"文件不存在: {path}"})

    result = _REGISTRY.parse(source)
    if not result.records:
        return _dumps(
            {"error": "文件解析结果为空，无法推断表结构", "parse_errors": result.errors}
        )

    name = table_name or table_name_from_path(source)
    comment = f"由 {source.name} 自动推断"
    inference = "heuristic"
    spec = None

    if is_llm_available():
        try:
            spec = LLMExtractor().infer_table_spec(
                result.records[:sample_size], name, comment=comment
            )
            inference = "llm"
        except Exception:  # noqa: BLE001 - 降级到规则推断
            spec = None

    if spec is None:
        spec = heuristic_table_spec(result.records[:sample_size], name, comment=comment)

    return _dumps(
        {
            "inference": inference,
            "parser": result.parser,
            "sample_count": min(sample_size, len(result.records)),
            "table_spec": spec.model_dump(),
            "ddl": spec.ddl(),
        }
    )


FILE_TOOLS = [
    list_input_files,
    preview_file,
    detect_file_format,
    parse_file,
    infer_target_schema,
]

__all__ = [
    "list_input_files",
    "preview_file",
    "detect_file_format",
    "parse_file",
    "infer_target_schema",
    "FILE_TOOLS",
]
