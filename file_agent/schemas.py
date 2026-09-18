"""贯穿「解析 → 抽取 → 建表 → 入库」全流程的数据模型。"""

from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, Field, field_validator

# Doris 支持的常用类型，用于校验 LLM 输出的字段类型
_DORIS_TYPE_RE = re.compile(
    r"^(STRING|TEXT|VARCHAR\((\d+)\)|CHAR\((\d+)\)|"
    r"BOOLEAN|TINYINT|SMALLINT|INT|INTEGER|BIGINT|LARGEINT|"
    r"FLOAT|DOUBLE|DECIMAL\((\d+),\s*(\d+)\)|"
    r"DATE|DATETIME|DATEV2|DATETIMEV2(\(\d+\))?|JSON|JSONB)$",
    re.IGNORECASE,
)


class LoadMode(str, Enum):
    """Doris 写入方式。"""

    STREAM_LOAD = "stream_load"
    INSERT = "insert"
    AUTO = "auto"


class FieldSpec(BaseModel):
    """Doris 表的一个字段。"""

    name: str
    dtype: str = "STRING"
    nullable: bool = True
    comment: str | None = None

    @field_validator("dtype")
    @classmethod
    def _normalize_dtype(cls, value: str) -> str:
        value = (value or "STRING").strip().upper()
        value = re.sub(r"\s+", "", value)
        if not _DORIS_TYPE_RE.match(value):
            # 非法或未知类型一律降级为 STRING，保证建表不至于失败
            return "STRING"
        return value

    def ddl(self) -> str:
        escaped = (self.comment or "").replace("'", "''")
        parts = [f"`{self.name}` {self.dtype}"]
        if not self.nullable:
            parts.append("NOT NULL")
        if self.comment:
            parts.append(f"COMMENT '{escaped}'")
        return " ".join(parts)


class TableSpec(BaseModel):
    """Doris 目标表定义。"""

    name: str
    columns: list[FieldSpec]
    comment: str | None = None
    unique_key: list[str] = Field(default_factory=list)
    distributed_by: list[str] = Field(default_factory=list)
    buckets: int = 4
    properties: dict[str, str] = Field(default_factory=dict)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def key_columns(self) -> list[str]:
        """Doris 建表必须指定 key；没有唯一键时退化为 DUPLICATE KEY(首列)。"""
        if self.unique_key:
            return [c for c in self.unique_key if c in self.column_names()]
        return self.column_names()[:1]

    def ddl(self, database: str | None = None) -> str:
        """生成 CREATE TABLE 语句。"""
        table_ref = f"`{database}`.`{self.name}`" if database else f"`{self.name}`"
        cols = ",\n  ".join(c.ddl() for c in self.columns)
        key_cols = "`, `".join(self.key_columns())
        key_type = "UNIQUE KEY" if self.unique_key else "DUPLICATE KEY"

        dist_cols = [c for c in self.distributed_by if c in self.column_names()]
        if dist_cols:
            distribution = f"DISTRIBUTED BY HASH(`{'`, `'.join(dist_cols)}`) BUCKETS {self.buckets}"
        else:
            distribution = f"DISTRIBUTED BY RANDOM BUCKETS {self.buckets}"

        props = {"replication_allocation": "tag.location.default: 1", **self.properties}
        props_str = ", ".join(f'"{k}" = "{v}"' for k, v in props.items())

        lines = [
            f"CREATE TABLE IF NOT EXISTS {table_ref} (",
            f"  {cols}",
            ") ENGINE=OLAP",
            f"{key_type}(`{key_cols}`)",
        ]
        if self.comment:
            escaped_comment = self.comment.replace("'", "''")
            lines.append(f"COMMENT '{escaped_comment}'")
        lines.append(distribution)
        lines.append(f"PROPERTIES ({props_str})")
        return "\n".join(lines)

    @classmethod
    def from_mapping(
        cls,
        name: str,
        columns: dict[str, str],
        *,
        comment: str | None = None,
        unique_key: Iterable[str] | None = None,
        distributed_by: Iterable[str] | None = None,
    ) -> TableSpec:
        """从 {字段名: Doris 类型} 的简单映射构造表结构。

        未显式指定分桶列时，默认与唯一键保持一致（Doris 的常见实践）。
        """
        keys = list(unique_key or [])
        return cls(
            name=name,
            comment=comment,
            columns=[FieldSpec(name=k, dtype=v) for k, v in columns.items()],
            unique_key=keys,
            distributed_by=list(distributed_by) if distributed_by is not None else list(keys),
        )


class ParseResult(BaseModel):
    """单文件解析结果。"""

    source: str
    parser: str
    record_count: int
    records: list[dict[str, Any]]
    errors: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def preview(self, limit: int = 3) -> list[dict[str, Any]]:
        return self.records[:limit]


class LoadResult(BaseModel):
    """写入 Doris 的结果。"""

    table: str
    mode: LoadMode = LoadMode.AUTO
    total: int = 0
    loaded: int = 0
    failed: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        return (
            f"[{self.mode.value}] 表 {self.table}: 共 {self.total} 行, "
            f"成功 {self.loaded}, 失败 {self.failed}. {self.message}".strip()
        )


class IngestReport(BaseModel):
    """一次完整「文件 → Doris」流程的结果汇总。"""

    source: str
    table: str
    parser: str | None = None
    parsed: int = 0
    extracted: int = 0
    load: LoadResult | None = None
    ddl: str | None = None
    dry_run: bool = False

    def summary(self) -> str:
        lines = [
            f"来源文件 : {self.source}",
            f"解析器   : {self.parser}",
            f"目标表   : {self.table}",
            f"解析行数 : {self.parsed}",
            f"抽取行数 : {self.extracted}",
        ]
        if self.dry_run:
            lines.append("运行模式 : dry-run（未写入 Doris）")
        if self.load:
            lines.append(f"入库结果 : {self.load.summary()}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 类型推断：Python 值 -> Doris 类型
# --------------------------------------------------------------------------- #
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def infer_doris_type(values: Iterable[Any]) -> str:
    """根据一组取值推断最合适的 Doris 类型。"""
    samples = [v for v in values if v not in (None, "")]
    if not samples:
        return "STRING"

    if all(isinstance(v, bool) or str(v).lower() in {"true", "false"} for v in samples):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in samples):
        biggest = max(abs(int(v)) for v in samples)
        if biggest < 2**31:
            return "INT"
        if biggest < 2**63:
            return "BIGINT"
        return "LARGEINT"
    if all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        or _INT_RE.match(str(v))
        or _FLOAT_RE.match(str(v))
        for v in samples
    ):
        return "DOUBLE"
    if all(_DATETIME_RE.match(str(v)) for v in samples):
        return "DATETIME"
    if all(_DATE_RE.match(str(v)) for v in samples):
        return "DATE"
    if any(isinstance(v, (dict, list)) for v in samples):
        return "JSON"

    max_len = max(len(str(v)) for v in samples)
    if max_len > 1024:
        return "TEXT"
    return f"VARCHAR({min(max(((max_len // 32) + 1) * 32, 32), 65533)})"


def heuristic_table_spec(
    records: list[dict[str, Any]],
    table_name: str,
    *,
    comment: str | None = None,
) -> TableSpec:
    """不依赖 LLM 的表结构推断：扫描记录的所有键并推断类型。"""
    columns: list[FieldSpec] = []
    seen: set[str] = set()

    for record in records:
        for key, value in record.items():
            if key in seen:
                continue
            seen.add(key)
            values = [r.get(key) for r in records]
            columns.append(
                FieldSpec(
                    name=key,
                    dtype=infer_doris_type(values),
                    nullable=any(v in (None, "") for v in values),
                    comment=None,
                )
            )

    # 猜测一个业务主键，便于使用 UNIQUE KEY 模型去重
    candidate_keys = ("id", "order_id", "device_sn", "uuid", "pk")
    unique_key = [c.name for c in columns if c.name.lower() in candidate_keys][:1]

    return TableSpec(
        name=table_name,
        comment=comment,
        columns=columns,
        unique_key=unique_key,
        distributed_by=unique_key,
    )


def align_records(
    records: list[dict[str, Any]],
    spec: TableSpec,
) -> list[dict[str, Any]]:
    """把记录裁剪并补齐到表结构定义的字段集合。

    字段名匹配对大小写与分隔符不敏感（如 ``Order ID`` ~ ``order_id``）。
    """
    columns = spec.column_names()
    aligned: list[dict[str, Any]] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        lowered = {str(k).lower(): v for k, v in record.items()}
        row: dict[str, Any] = {}
        for column in columns:
            value = record.get(column)
            if value is None:
                value = lowered.get(column.lower())
            row[column] = to_jsonable(value)
        if any(value not in (None, "") for value in row.values()):
            aligned.append(row)
    return aligned


def to_jsonable(value: Any) -> Any:
    """把 datetime / Decimal 等对象转成可 JSON 序列化的形式。"""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
