"""LangGraph 智能体。

基于 LangChain 1.x 的 ``create_agent``（底层为 LangGraph 状态机），
让模型自主决策：读哪个文件 → 用什么解析器 → 建什么表 → 如何入库 → 如何校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from ..extractor import message_text
from ..llm import get_default_model
from ..tools import build_tools

SYSTEM_PROMPT = """你是「半结构化文件解析入库智能体」，负责把本地文件解析成规整数据并写入 Apache Doris。

工作准则：
1. 先侦察再行动：用 list_input_files / detect_file_format / preview_file 了解文件，不要盲目解析；
2. 能用规则就不用模型：parse_file 能处理的文件不要浪费推理；
3. 入库前必须确定目标表：优先复用 list_configured_tables 中已声明的表定义；
4. 写库前先用 inspect_doris_connection 确认 Doris 可用；
5. 用户要求预览或你不确定时，先以 dry_run=true 查看建表语句与统计；
6. 完成后用 query_doris 抽查数据，并在回答中给出：处理文件、目标表、入库行数、异常情况；
7. 失败时说明具体原因与修复建议，绝不编造成功结果。

回答使用简体中文，结论先行，简洁准确。"""


@dataclass
class AgentRun:
    """一次智能体调用的结果。"""

    answer: str
    messages: list[Any] = field(default_factory=list)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """本次运行中模型发起的所有工具调用。"""
        calls: list[dict[str, Any]] = []
        for message in self.messages:
            for call in getattr(message, "tool_calls", None) or []:
                calls.append({"name": call.get("name"), "args": call.get("args")})
        return calls


def build_agent(
    model: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
    *,
    system_prompt: str | None = SYSTEM_PROMPT,
):
    """构建智能体，返回编译后的 LangGraph 状态图。

    Args:
        model: 聊天模型；默认读取 .env 中配置的模型
        tools: 工具列表；默认包含全部文件与 Doris 工具
        system_prompt: 系统提示词
    """
    resolved_model = model or get_default_model()
    resolved_tools = list(tools) if tools is not None else build_tools()
    return create_agent(resolved_model, resolved_tools, system_prompt=system_prompt)


def run_agent(
    query: str,
    agent=None,
    *,
    history: list[Any] | None = None,
    model: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
) -> AgentRun:
    """执行一次对话。

    Args:
        query: 用户输入
        agent: 已构建的智能体；为空时自动构建
        history: 历史消息，用于多轮对话
    """
    graph = agent or build_agent(model=model, tools=tools)
    messages: list[Any] = list(history or [])
    messages.append(HumanMessage(content=query))

    state = graph.invoke({"messages": messages})
    all_messages = list(state.get("messages", []))
    answer = message_text(all_messages[-1]) if all_messages else ""
    return AgentRun(answer=answer, messages=all_messages)


def stream_agent(
    query: str,
    agent=None,
    *,
    history: list[Any] | None = None,
    model: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
) -> Iterator[dict[str, Any]]:
    """流式执行，逐步产出 LangGraph 的事件块。"""
    graph = agent or build_agent(model=model, tools=tools)
    messages: list[Any] = list(history or [])
    messages.append(HumanMessage(content=query))
    yield from graph.stream({"messages": messages}, stream_mode="updates")


def format_tool_calls(run: AgentRun) -> str:
    """把工具调用轨迹格式化为可读文本。"""
    if not run.tool_calls:
        return "(本次未调用工具)"
    lines = []
    for index, call in enumerate(run.tool_calls, start=1):
        args = ", ".join(f"{k}={v!r}" for k, v in (call.get("args") or {}).items())
        lines.append(f"{index}. {call.get('name')}({args})")
    return "\n".join(lines)


__all__ = ["AgentRun", "build_agent", "run_agent", "stream_agent", "format_tool_calls", "SYSTEM_PROMPT"]
