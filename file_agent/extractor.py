"""LLM 结构化抽取。

提供两项能力：
    ``extract_records``   半结构化 / 非结构化文本  ->  规整记录列表
    ``infer_table_spec``  样例记录                ->  Doris 表结构定义

设计上不依赖「函数调用 / 结构化输出」能力，而是让模型直接产出 JSON 再解析，
因此可以兼容所有 OpenAI 兼容端点（含不支持 tool calling 的本地模型）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .llm import get_default_model
from .parsers.base import normalize_records
from .schemas import FieldSpec, TableSpec

EXTRACTION_SYSTEM_PROMPT = """你是一个数据抽取引擎，负责把半结构化文件内容转换为规整的 JSON 记录。

必须遵守：
1. 只输出 JSON，不要输出解释、前后缀；
2. 输出格式固定为 {"records": [ {...}, {...} ]}；
3. 每条记录代表文件中的一行或一条业务实体；
4. 字段名使用小写下划线风格，各条记录的字段集合保持一致；
5. 缺失值用 null；数字不加引号；时间统一为 "YYYY-MM-DD HH:MM:SS"；
6. 只提取文件中真实存在的信息，不要臆造。"""

SCHEMA_SYSTEM_PROMPT = """你是 Apache Doris 的建模专家。
根据用户提供的样例数据，为它设计一张 Doris 明细表。

必须遵守：
1. 只输出 JSON，不要输出解释；
2. 字段名使用小写下划线风格，语义清晰；
3. dtype 只能取以下之一：
   VARCHAR(n) / STRING / TEXT / INT / BIGINT / LARGEINT / DOUBLE /
   DECIMAL(p,s) / DATE / DATETIME / BOOLEAN / JSON；
4. 时间字段统一 DATETIME，日期用 DATE，长文本用 TEXT 或 STRING；
5. unique_key 填能唯一标识一行的字段，若无法判断则为空数组；
6. 输出格式：
{
  "table_name": "xxx",
  "comment": "表说明",
  "columns": [{"name": "字段", "dtype": "类型", "nullable": true, "comment": "说明"}],
  "unique_key": ["字段"],
  "distributed_by": ["字段"],
  "buckets": 4
}"""


class ExtractionError(RuntimeError):
    """抽取失败。"""


def message_text(message: Any) -> str:
    """兼容多模态 content 结构，提取纯文本。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content)


def parse_json_payload(text: str) -> Any:
    """从 LLM 输出中稳健地解析出 JSON。

    依次尝试：剥离 ``` 代码块 -> 扫描首个 ``{`` / ``[`` 并使用 raw_decode。
    """
    if not text or not text.strip():
        raise ExtractionError("LLM 返回内容为空")

    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(cleaned[index:])
            return payload
        except json.JSONDecodeError:
            continue

    raise ExtractionError(f"无法从 LLM 输出中解析 JSON：{cleaned[:200]}")


def normalize_field_name(name: str) -> str:
    """把任意字段名规范化为小写下划线风格。"""
    cleaned = re.sub(r"[^\w]+", "_", str(name).strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned.lower() or "col"


class LLMExtractor:
    """基于 LLM 的记录抽取与表结构推断。"""

    def __init__(
        self,
        model: BaseChatModel | None = None,
        table_spec: TableSpec | None = None,
    ) -> None:
        self._model = model
        self.table_spec = table_spec

    @property
    def model(self) -> BaseChatModel:
        if self._model is None:
            self._model = get_default_model()
        return self._model

    # ------------------------------------------------------------------ #
    # 记录抽取
    # ------------------------------------------------------------------ #
    def extract_records(
        self,
        text: str,
        *,
        instructions: str = "",
        table_spec: TableSpec | None = None,
    ) -> list[dict[str, Any]]:
        """把一段文本抽取成记录列表。"""
        spec = table_spec or self.table_spec
        prompt = self._build_extraction_prompt(text, spec, instructions)
        response = self.model.invoke(
            [
                SystemMessage(content=EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
        payload = parse_json_payload(message_text(response))
        records = normalize_records(payload)

        if spec is not None:
            records = [
                {normalize_field_name(k): v for k, v in record.items()} for record in records
            ]
        return records

    @staticmethod
    def _build_extraction_prompt(text: str, spec: TableSpec | None, instructions: str) -> str:
        blocks: list[str] = []
        if spec is not None:
            schema = {
                "table": spec.name,
                "columns": [{"name": c.name, "type": c.dtype} for c in spec.columns],
            }
            blocks.append("目标表结构（请严格按这些字段输出，字段名保持一致）：")
            blocks.append(json.dumps(schema, ensure_ascii=False, indent=2))
        if instructions:
            blocks.append(f"额外要求：{instructions}")
        blocks.append("待处理内容：")
        blocks.append("<<<FILE_CONTENT")
        blocks.append(text)
        blocks.append("FILE_CONTENT")
        blocks.append('请输出 {"records": [...]} 格式的 JSON。')
        return "\n".join(blocks)

    # ------------------------------------------------------------------ #
    # 表结构推断
    # ------------------------------------------------------------------ #
    def infer_table_spec(
        self,
        records: list[dict[str, Any]],
        table_name: str,
        *,
        comment: str | None = None,
        sample_size: int = 5,
    ) -> TableSpec:
        """根据样例记录让 LLM 设计 Doris 表结构。"""
        if not records:
            raise ExtractionError("没有可用于推断表结构的样例数据")

        sample = records[:sample_size]
        prompt = (
            f"目标表名：{table_name}\n"
            f"样例数据（最多 {len(sample)} 条）：\n"
            f"{json.dumps(sample, ensure_ascii=False, indent=2, default=str)}\n\n"
            "请输出约定的 JSON 表结构。"
        )
        response = self.model.invoke(
            [
                SystemMessage(content=SCHEMA_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
        payload = parse_json_payload(message_text(response))
        if not isinstance(payload, dict):
            raise ExtractionError("LLM 返回的表结构不是 JSON 对象")

        columns: list[FieldSpec] = []
        seen: set[str] = set()
        for raw in payload.get("columns", []):
            if not isinstance(raw, dict):
                continue
            name = normalize_field_name(raw.get("name", ""))
            if not name or name in seen:
                continue
            seen.add(name)
            columns.append(
                FieldSpec(
                    name=name,
                    dtype=str(raw.get("dtype") or "STRING"),
                    nullable=bool(raw.get("nullable", True)),
                    comment=raw.get("comment"),
                )
            )
        if not columns:
            raise ExtractionError("LLM 未返回有效的字段定义")

        valid_names = {c.name for c in columns}
        unique_key = [normalize_field_name(k) for k in payload.get("unique_key", [])]
        unique_key = [k for k in unique_key if k in valid_names]
        distributed_by = [normalize_field_name(k) for k in payload.get("distributed_by", [])]
        distributed_by = [k for k in distributed_by if k in valid_names] or unique_key
        buckets = payload.get("buckets")

        return TableSpec(
            name=normalize_field_name(payload.get("table_name") or table_name),
            comment=comment or payload.get("comment"),
            columns=columns,
            unique_key=unique_key,
            distributed_by=distributed_by,
            buckets=int(buckets) if isinstance(buckets, int) and buckets > 0 else 4,
        )


def extract_with_llm(
    text: str,
    *,
    model: BaseChatModel | None = None,
    table_spec: TableSpec | None = None,
    instructions: str = "",
) -> list[dict[str, Any]]:
    """便捷函数：一次性抽取。"""
    return LLMExtractor(model=model, table_spec=table_spec).extract_records(
        text, instructions=instructions
    )


__all__ = [
    "LLMExtractor",
    "ExtractionError",
    "extract_with_llm",
    "parse_json_payload",
    "message_text",
    "normalize_field_name",
]
