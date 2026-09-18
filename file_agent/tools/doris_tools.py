"""Apache Doris 相关的 LangChain 工具。"""

from __future__ import annotations

import json
from pathlib import Path

from langchain.tools import tool
from pydantic import ValidationError

from ..doris import DorisClient, DorisWriter
from ..pipeline import run_pipeline
from ..schemas import TableSpec
from ..table_config import list_table_names

_READONLY_PREFIXES = ("select", "show", "desc", "describe", "explain", "with")


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _ensure_readonly(sql: str) -> None:
    head = sql.strip().split(maxsplit=1)[0].lower() if sql.strip() else ""
    if head not in _READONLY_PREFIXES:
        raise ValueError("出于安全考虑，仅允许只读查询（SELECT / SHOW / DESC / EXPLAIN）")


@tool
def inspect_doris_connection() -> str:
    """检查 Doris 连通性，并列出可用的数据库与当前库中的表。

    在正式入库前应先调用本工具确认环境可用。
    """
    client = DorisClient()
    try:
        databases = client.list_databases()
    except Exception as exc:  # noqa: BLE001
        return _dumps({"connected": False, "error": str(exc), "target": client.settings.safe_repr()})

    payload: dict = {
        "connected": True,
        "target": client.settings.safe_repr(),
        "databases": databases,
    }
    try:
        payload["current_db_tables"] = client.list_tables()
    except Exception as exc:  # noqa: BLE001
        payload["current_db_tables_error"] = str(exc)
    return _dumps(payload)


@tool
def list_configured_tables() -> str:
    """列出 config/tables.yaml 中已声明的目标表名，入库时优先复用这些定义。"""
    names = list_table_names()
    if not names:
        return _dumps({"tables": [], "hint": "配置文件中暂未声明任何表，将由 LLM 自动推断表结构"})
    return _dumps({"tables": names})


@tool
def describe_target_table(table: str) -> str:
    """查看 Doris 中一张表的结构（字段名、类型、注释）。

    Args:
        table: 表名
    """
    client = DorisClient()
    try:
        if not client.table_exists(table):
            return _dumps({"exists": False, "table": table, "hint": "该表不存在，可先用 create_target_table 创建"})
        columns = client.describe_table(table)
        return _dumps(
            {
                "exists": True,
                "table": table,
                "row_count": client.count_rows(table),
                "columns": columns,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _dumps({"error": str(exc), "table": table})


@tool
def create_target_table(table_spec_json: str, drop_if_exists: bool = False) -> str:
    """在 Doris 中创建目标表。

    可先调用 infer_target_schema 得到 table_spec，再把其中的 table_spec 字段原样传进来。

    Args:
        table_spec_json: 表结构 JSON（{"name": ..., "columns": [...]}）
        drop_if_exists: 是否先删除已存在的同名表（谨慎使用）
    """
    try:
        spec = TableSpec.model_validate(json.loads(table_spec_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        return _dumps({"error": f"表结构 JSON 非法: {exc}"})

    client = DorisClient()
    try:
        ddl = client.create_table(spec, drop_if_exists=drop_if_exists)
    except Exception as exc:  # noqa: BLE001
        return _dumps({"success": False, "error": str(exc), "table": spec.name})

    return _dumps({"success": True, "table": spec.name, "ddl": ddl})


@tool
def ingest_file_into_doris(
    file_path: str,
    table: str = "",
    mode: str = "auto",
    dry_run: bool = False,
) -> str:
    """一站式把文件解析并写入 Doris（解析 → 推断表结构 → 建表 → 入库）。

    Args:
        file_path: 待处理的文件路径
        table: 目标表名，留空则由文件名推导
        mode: 写入方式：auto（默认，优先 Stream Load）/ stream_load / insert
        dry_run: 为 True 时只生成建表语句与统计，不写库
    """
    try:
        report = run_pipeline(file_path, table=table or None, mode=mode, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return _dumps({"success": False, "error": f"{type(exc).__name__}: {exc}", "file": file_path})

    payload = report.model_dump()
    payload["success"] = bool(report.load.ok) if report.load else True
    payload["summary"] = report.summary()
    return _dumps(payload)


@tool
def query_doris(sql: str, limit: int = 20) -> str:
    """执行只读 SQL 查询，用于校验入库结果。

    仅允许 SELECT / SHOW / DESC / EXPLAIN 语句。

    Args:
        sql: 查询语句
        limit: 返回的最大行数
    """
    try:
        _ensure_readonly(sql)
    except ValueError as exc:
        return _dumps({"error": str(exc)})

    client = DorisClient()
    try:
        rows = client.query(sql)
    except Exception as exc:  # noqa: BLE001
        return _dumps({"error": str(exc), "sql": sql})

    return _dumps({"row_count": len(rows), "rows": rows[: max(1, limit)]})


DORIS_TOOLS = [
    inspect_doris_connection,
    list_configured_tables,
    describe_target_table,
    create_target_table,
    ingest_file_into_doris,
    query_doris,
]

__all__ = [
    "inspect_doris_connection",
    "list_configured_tables",
    "describe_target_table",
    "create_target_table",
    "ingest_file_into_doris",
    "query_doris",
    "DORIS_TOOLS",
]
