"""LangChain 工具集：文件解析 + Doris 入库。"""

from .doris_tools import DORIS_TOOLS
from .file_tools import FILE_TOOLS


def build_tools() -> list:
    """构建智能体可用的完整工具列表。"""
    return [*FILE_TOOLS, *DORIS_TOOLS]


__all__ = ["build_tools", "FILE_TOOLS", "DORIS_TOOLS"]
