"""加载 config/tables.yaml 中声明的 Doris 目标表定义。

支持两种使用方式：
    1. 显式表名：``get_table_spec("ods_orders")``
    2. 文件名模式：``match_table_spec("orders_20260918.jsonl")``
       对应 YAML 中的 ``source_patterns``，实现「来源文件 → 目标表」的自动路由。
"""

from __future__ import annotations

from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import get_runtime_settings
from .schemas import FieldSpec, TableSpec


class TableConfigError(RuntimeError):
    """表配置格式错误。"""


def _parse_table(raw: dict[str, Any]) -> TableSpec:
    name = raw.get("name")
    if not name:
        raise TableConfigError("表定义缺少 name 字段")

    columns: list[FieldSpec] = []
    for item in raw.get("columns") or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise TableConfigError(f"表 {name} 存在非法的字段定义: {item!r}")
        columns.append(
            FieldSpec(
                name=str(item["name"]),
                dtype=str(item.get("dtype") or "STRING"),
                nullable=bool(item.get("nullable", True)),
                comment=item.get("comment"),
            )
        )
    if not columns:
        raise TableConfigError(f"表 {name} 未定义任何字段")

    return TableSpec(
        name=str(name),
        comment=raw.get("comment"),
        columns=columns,
        unique_key=[str(k) for k in raw.get("unique_key") or []],
        distributed_by=[str(k) for k in raw.get("distributed_by") or []],
        buckets=int(raw.get("buckets") or 4),
        properties={str(k): str(v) for k, v in (raw.get("properties") or {}).items()},
    )


def _load(path: Path) -> tuple[dict[str, TableSpec], tuple[tuple[str, str], ...]]:
    """返回 (表定义映射, ((文件名模式, 表名), ...))。"""
    if not path.exists():
        return {}, ()

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tables = document.get("tables") or []
    if not isinstance(tables, list):
        raise TableConfigError(f"{path} 中的 tables 必须是列表")

    specs: dict[str, TableSpec] = {}
    patterns: list[tuple[str, str]] = []
    for raw in tables:
        if not isinstance(raw, dict):
            raise TableConfigError(f"{path} 中存在非法的表定义: {raw!r}")
        spec = _parse_table(raw)
        specs[spec.name] = spec
        for pattern in raw.get("source_patterns") or []:
            patterns.append((str(pattern), spec.name))
    return specs, tuple(patterns)


@lru_cache(maxsize=8)
def _cached_config(path_str: str) -> tuple[dict[str, TableSpec], tuple[tuple[str, str], ...]]:
    return _load(Path(path_str))


def _config(
    path: str | Path | None = None,
) -> tuple[dict[str, TableSpec], tuple[tuple[str, str], ...]]:
    config_path = Path(path) if path else get_runtime_settings().tables_config_path
    return _cached_config(str(config_path))


def load_table_specs(path: str | Path | None = None) -> dict[str, TableSpec]:
    """读取配置，返回 {表名: TableSpec}。文件不存在时返回空字典。"""
    specs, _ = _config(path)
    return dict(specs)


def get_table_spec(name: str | None = None, path: str | Path | None = None) -> TableSpec | None:
    """按表名获取表定义。

    ``name`` 为空且配置中只声明了一张表时，直接返回该表。
    """
    specs, _ = _config(path)
    if not specs:
        return None
    if name:
        return specs.get(name)
    if len(specs) == 1:
        return next(iter(specs.values()))
    return None


def match_table_spec(source: str | Path, path: str | Path | None = None) -> TableSpec | None:
    """按文件名匹配目标表（对应 YAML 中的 ``source_patterns``）。"""
    specs, patterns = _config(path)
    filename = Path(source).name.lower()
    for pattern, table_name in patterns:
        if fnmatch(filename, pattern.lower()):
            return specs.get(table_name)
    return None


def list_table_names(path: str | Path | None = None) -> list[str]:
    specs, _ = _config(path)
    return sorted(specs)


def clear_table_config_cache() -> None:
    _cached_config.cache_clear()


__all__ = [
    "load_table_specs",
    "get_table_spec",
    "match_table_spec",
    "list_table_names",
    "clear_table_config_cache",
    "TableConfigError",
]
