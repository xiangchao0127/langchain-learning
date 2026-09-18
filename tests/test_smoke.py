"""离线冒烟测试：不依赖真实 LLM 与 Doris 集群。

覆盖：文件识别 → 解析 → 表结构推断 → DDL 生成 → 流程编排 → 智能体工具调用。
运行：pytest -q
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from file_agent.agent import build_agent, run_agent
from file_agent.doris import normalize_records, normalize_value
from file_agent.parsers import build_default_registry
from file_agent.pipeline import run_pipeline, table_name_from_path
from file_agent.schemas import TableSpec, heuristic_table_spec
from file_agent.tools import build_tools

SAMPLES = Path(__file__).resolve().parent.parent / "examples" / "sample_data"


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
def test_detect_parsers_by_extension():
    registry = build_default_registry()
    assert registry.detect(SAMPLES / "orders.jsonl").name == "json"
    assert registry.detect(SAMPLES / "app.log").name == "log"
    assert registry.detect(SAMPLES / "devices.csv").name == "csv"


def test_parse_jsonl():
    result = build_default_registry().parse(SAMPLES / "orders.jsonl")
    assert result.record_count == 8
    assert result.errors == []
    assert result.records[0]["order_id"] == "SO20260918001"


def test_parse_log_merges_stacktrace():
    result = build_default_registry().parse(SAMPLES / "app.log")
    assert result.errors == []
    assert result.record_count == 12
    error_record = next(r for r in result.records if r["level"] == "ERROR")
    assert "HikariPool" in error_record["message"]


def test_parse_csv_keeps_utf8():
    result = build_default_registry().parse(SAMPLES / "devices.csv")
    assert result.record_count == 8
    assert result.records[0]["region"] == "华东"


# --------------------------------------------------------------------------- #
# 表结构与 DDL
# --------------------------------------------------------------------------- #
def test_ddl_generation():
    spec = TableSpec.from_mapping(
        "t_demo", {"id": "INT", "name": "VARCHAR(32)"}, unique_key=["id"]
    )
    ddl = spec.ddl(database="demo")
    assert "`demo`.`t_demo`" in ddl
    assert "UNIQUE KEY(`id`)" in ddl
    assert "DISTRIBUTED BY HASH(`id`)" in ddl


def test_ddl_falls_back_to_duplicate_key():
    spec = TableSpec.from_mapping("t_demo", {"id": "INT", "name": "VARCHAR(32)"})
    assert "DUPLICATE KEY(`id`)" in spec.ddl()
    assert "DISTRIBUTED BY RANDOM" in spec.ddl()


def test_invalid_dtype_is_downgraded():
    spec = TableSpec.from_mapping("t_demo", {"id": "NOT_A_TYPE"})
    assert spec.columns[0].dtype == "STRING"


def test_heuristic_spec_infers_types():
    records = [
        {"id": "a", "cnt": 3, "price": 1.5, "ts": "2026-09-18 10:00:00", "ok": True},
    ]
    spec = heuristic_table_spec(records, "t")
    types = {c.name: c.dtype for c in spec.columns}
    assert types["cnt"] == "INT"
    assert types["price"] == "DOUBLE"
    assert types["ts"] == "DATETIME"
    assert types["ok"] == "BOOLEAN"
    assert spec.unique_key == ["id"]


def test_table_name_from_path():
    assert table_name_from_path("orders.jsonl") == "ods_orders"
    assert table_name_from_path("my-data.csv") == "ods_my_data"


# --------------------------------------------------------------------------- #
# Doris 写入层的数据规范化
# --------------------------------------------------------------------------- #
def test_normalize_value():
    assert normalize_value(True) == 1
    assert normalize_value({"a": 1}) == '{"a": 1}'
    assert normalize_value(None) is None


def test_normalize_records_drops_empty_rows():
    rows = normalize_records([{"a": 1, "b": None}, {"a": None, "b": None}], ["a", "b"])
    assert rows == [{"a": 1, "b": None}]


# --------------------------------------------------------------------------- #
# 流水线
# --------------------------------------------------------------------------- #
def test_pipeline_matches_source_pattern():
    """orders.jsonl 应通过 source_patterns 自动路由到 ods_order_events。"""
    report = run_pipeline(SAMPLES / "orders.jsonl", dry_run=True, use_llm=False)
    assert report.table == "ods_order_events"
    assert report.parsed == 8
    assert report.load is None
    assert "CREATE TABLE" in (report.ddl or "")


def test_pipeline_declared_table_wins():
    report = run_pipeline(SAMPLES / "devices.csv", dry_run=True, use_llm=False)
    assert report.table == "ods_device_status"
    assert "UNIQUE KEY(`device_sn`)" in (report.ddl or "")


def test_pipeline_explicit_table_overrides_pattern():
    report = run_pipeline(
        SAMPLES / "orders.jsonl", table="ods_order_events", dry_run=True, use_llm=False
    )
    assert report.table == "ods_order_events"


def test_pipeline_unknown_file_uses_heuristic():
    """app.log 命中 *.log 规则；用一个不匹配任何规则的文件验证兜底推断。"""
    report = run_pipeline(SAMPLES / "app.log", table="ods_custom_log", dry_run=True, use_llm=False)
    assert report.table == "ods_custom_log"
    assert "表结构来源: heuristic" in (report.ddl or "")


# --------------------------------------------------------------------------- #
# 工具与智能体
# --------------------------------------------------------------------------- #
def test_tools_registered():
    names = {t.name for t in build_tools()}
    assert {"parse_file", "infer_target_schema", "ingest_file_into_doris", "query_doris"} <= names


def test_tool_invocation_returns_json():
    tool = next(t for t in build_tools() if t.name == "detect_file_format")
    payload = tool.invoke({"path": str(SAMPLES / "orders.jsonl")})
    assert '"recommended_parser": "json"' in payload


class ScriptedToolModel(BaseChatModel):
    """脚本化模型：第一轮发起工具调用，第二轮给出最终回答。

    用于在不访问真实 LLM 的前提下验证智能体的「模型 → 工具 → 模型」闭环。
    """

    responses: list[AIMessage] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ARG002
        message = self.responses.pop(0) if self.responses else AIMessage(content="完成")
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_agent_executes_tool_calls():
    model = ScriptedToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_input_files",
                        "args": {"directory": str(SAMPLES)},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="共发现 3 个文件：orders.jsonl、app.log、devices.csv"),
        ]
    )
    agent = build_agent(model=model)
    run = run_agent("列出示例数据目录下的文件", agent=agent)

    assert run.tool_calls, "智能体应当发起工具调用"
    assert run.tool_calls[0]["name"] == "list_input_files"
    assert "3" in run.answer
