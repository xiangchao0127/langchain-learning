"""file_agent —— 半结构化文件解析 + Apache Doris 入库智能体。

模块划分：
    config     配置加载（LLM / Doris / 运行时）
    schemas    数据模型（表结构、解析结果、入库结果）
    llm        LLM 工厂
    loaders    文件读取与编码探测
    parsers    解析器体系（JSON / CSV / YAML / 日志 / 键值文本）
    extractor  LLM 结构化抽取
    doris      Doris 客户端与写入器
    tools      LangChain 工具集
    agent      LangGraph 智能体
    pipeline   确定性端到端流水线
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
