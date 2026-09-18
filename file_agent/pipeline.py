"""端到端流水线：文件 → 解析 → (LLM 抽取) → 建表 → 入库。

与智能体的关系：
    智能体负责「自主决策」（选哪个文件、用什么策略）；
    本模块提供「确定性执行」，两者共用同一套解析器 / 抽取器 / 写入器。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from .config import get_doris_settings
from .doris import DorisClient, DorisWriter
from .extractor import ExtractionError, LLMExtractor
from .llm import is_llm_available
from .loaders import read_text
from .parsers import ParserRegistry, build_default_registry
from .schemas import (
    IngestReport,
    LoadMode,
    ParseResult,
    TableSpec,
    align_records,
    heuristic_table_spec,
)
from .table_config import get_table_spec, list_table_names, match_table_spec


class PipelineError(RuntimeError):
    """流水线执行失败。"""


def table_name_from_path(source: str | Path) -> str:
    """由文件名推导目标表名，例如 ``orders.jsonl`` -> ``ods_orders``。"""
    stem = Path(source).stem.lower()
    stem = re.sub(r"[^\w]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_") or "data"
    return stem if stem.startswith("ods_") else f"ods_{stem}"


def resolve_table_spec(
    records: list[dict[str, Any]],
    table_name: str,
    *,
    source: str | Path | None = None,
    explicit_table: bool = False,
    ignore_config: bool = False,
    model: BaseChatModel | None = None,
    use_llm: bool | None = None,
) -> tuple[TableSpec, str]:
    """确定目标表结构。

    优先级：
        1. ``config/tables.yaml``：用户显式指定的表名，或文件的 source_patterns 匹配结果；
        2. LLM 推断（默认，需配置 API Key）；
        3. 规则推断（无 LLM 时的兜底）。

    Args:
        explicit_table: 表名是否由用户显式指定（显式指定时优先按名字查找）
        ignore_config: 忽略配置文件，强制走推断

    Returns:
        (表结构, 来源标识)
    """
    declared: TableSpec | None = None
    if not ignore_config:
        if explicit_table:
            # 用户显式指定表名时，不再让文件名规则覆盖其选择
            declared = get_table_spec(table_name)
        else:
            if source is not None:
                declared = match_table_spec(source)
            if declared is None:
                declared = get_table_spec(table_name)

    if declared is not None:
        return declared, "config/tables.yaml"

    if not records:
        raise PipelineError("没有可用于推断表结构的记录")

    comment = f"由 {Path(source).name} 自动推断" if source else None
    allow_llm = is_llm_available() if use_llm is None else use_llm

    if allow_llm:
        try:
            extractor = LLMExtractor(model=model)
            return extractor.infer_table_spec(records, table_name, comment=comment), "llm"
        except Exception:  # noqa: BLE001 - 推断失败降级到规则
            pass

    return heuristic_table_spec(records, table_name, comment=comment), "heuristic"


def needs_extraction(records: list[dict[str, Any]], spec: TableSpec) -> bool:
    """判断记录是否还需要 LLM 抽取（字段与目标表差异过大时）。"""
    if not records:
        return True
    keys: set[str] = set()
    for record in records[:30]:
        keys.update(str(k).lower() for k in record)
    if not keys:
        return True
    columns = {c.lower() for c in spec.column_names()}
    overlap = len(keys & columns) / len(keys)
    return overlap < 0.5


def parse_source(
    source: str | Path,
    *,
    parser_name: str | None = None,
    registry: ParserRegistry | None = None,
) -> ParseResult:
    registry = registry or build_default_registry()
    return registry.parse(source, parser_name=parser_name)


def run_pipeline(
    source: str | Path,
    *,
    table: str | None = None,
    parser_name: str | None = None,
    use_llm: bool | None = None,
    force_extraction: bool = False,
    dry_run: bool = False,
    mode: LoadMode | str = LoadMode.AUTO,
    create_table: bool = True,
    model: BaseChatModel | None = None,
    registry: ParserRegistry | None = None,
) -> IngestReport:
    """执行完整的「文件 → Doris」流程。

    Args:
        source: 输入文件路径
        table: 目标表名；留空则由文件名推导
        parser_name: 强制指定解析器
        use_llm: 是否允许调用 LLM；None 表示有 Key 就用
        force_extraction: 强制走一次 LLM 抽取（即使字段已对齐）
        dry_run: 只生成 DDL 与统计，不写库
        mode: 写入方式 auto / stream_load / insert
        create_table: 是否自动建库建表
    """
    source_path = Path(source)
    if not source_path.exists():
        raise PipelineError(f"文件不存在: {source_path}")

    registry = registry or build_default_registry()
    result = registry.parse(source_path, parser_name=parser_name)

    explicit_table = bool(table)
    table_name = table or table_name_from_path(source_path)
    records: list[dict[str, Any]] = list(result.records)

    # 解析失败（例如纯文本日志上下文）时，尝试让 LLM 直接从原文抽取
    if not records:
        records = _extract_from_raw_text(source_path, model=model, use_llm=use_llm)

    if not records:
        raise PipelineError(
            f"未能从文件中解析出任何记录: {source_path}\n错误: {result.errors}"
        )

    spec, inference = resolve_table_spec(
        records,
        table_name,
        source=source_path,
        explicit_table=explicit_table,
        model=model,
        use_llm=use_llm,
    )

    extracted = 0
    if force_extraction or needs_extraction(records, spec):
        records = _extract_records(
            source_path,
            records,
            spec,
            model=model,
            use_llm=use_llm,
        )
        extracted = len(records)

    aligned = align_records(records, spec)
    report = IngestReport(
        source=str(source_path.resolve()),
        table=spec.name,
        parser=result.parser,
        parsed=result.record_count,
        extracted=extracted or len(aligned),
        ddl=f"-- 表结构来源: {inference}\n{spec.ddl(database=get_doris_settings().database)}",
        dry_run=dry_run,
    )

    if dry_run:
        report.load = None
        return report

    client = DorisClient()
    if create_table:
        client.create_table(spec)

    writer = DorisWriter(client=client)
    report.load = writer.write(
        aligned,
        spec.name,
        columns=spec.column_names(),
        mode=mode,
    )
    return report


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _extract_from_raw_text(
    source_path: Path,
    *,
    model: BaseChatModel | None,
    use_llm: bool | None,
) -> list[dict[str, Any]]:
    allow_llm = is_llm_available() if use_llm is None else use_llm
    if not allow_llm:
        return []
    try:
        text = read_text(source_path)
    except Exception:  # noqa: BLE001
        return []
    try:
        return LLMExtractor(model=model).extract_records(text[:20000])
    except ExtractionError:
        return []


def _extract_records(
    source_path: Path,
    records: list[dict[str, Any]],
    spec: TableSpec,
    *,
    model: BaseChatModel | None,
    use_llm: bool | None,
) -> list[dict[str, Any]]:
    allow_llm = is_llm_available() if use_llm is None else use_llm
    if not allow_llm:
        return records
    try:
        text = read_text(source_path)
    except Exception:  # noqa: BLE001
        return records

    try:
        extracted = LLMExtractor(model=model, table_spec=spec).extract_records(text[:20000])
    except Exception:  # noqa: BLE001 - 抽取失败时保留原记录
        return records
    return extracted or records


def available_tables() -> list[str]:
    """配置文件中已声明的表名。"""
    return list_table_names()


__all__ = [
    "run_pipeline",
    "parse_source",
    "resolve_table_spec",
    "needs_extraction",
    "table_name_from_path",
    "available_tables",
    "PipelineError",
]
