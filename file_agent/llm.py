"""LLM 工厂。

统一使用 OpenAI 兼容协议，因此可以直接接入：
    - OpenAI 官方
    - DeepSeek（https://api.deepseek.com/v1）
    - 通义千问（https://dashscope.aliyuncs.com/compatible-mode/v1）
    - Kimi / 智谱 / 本地 vLLM / Ollama（OpenAI 兼容模式）
只需在 .env 中配置 LLM_BASE_URL 与 LLM_API_KEY 即可。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from .config import LLMSettings, get_llm_settings


class LLMNotConfiguredError(RuntimeError):
    """未配置 API Key 时抛出，调用方可据此降级到规则模式。"""


def create_chat_model(settings: LLMSettings | None = None, **overrides) -> ChatOpenAI:
    """按配置创建 ChatOpenAI 实例。"""
    settings = settings or get_llm_settings()

    kwargs: dict = {
        "model": overrides.pop("model", settings.model),
        "temperature": overrides.pop("temperature", settings.temperature),
        "timeout": overrides.pop("timeout", settings.timeout),
        "max_retries": overrides.pop("max_retries", settings.max_retries),
    }
    base_url = settings.resolved_base_url()
    if base_url:
        kwargs["base_url"] = base_url

    api_key = settings.api_key.strip()
    if not api_key or api_key == "sk-your-api-key":
        raise LLMNotConfiguredError(
            "未检测到有效的 LLM_API_KEY，请在 .env 中配置后重试"
            "（或使用 --no-llm 启用纯规则模式）。"
        )
    kwargs["api_key"] = api_key
    kwargs.update(overrides)
    return ChatOpenAI(**kwargs)


@lru_cache(maxsize=1)
def get_default_model() -> ChatOpenAI:
    """进程内复用的默认模型实例。"""
    return create_chat_model()


def is_llm_available() -> bool:
    """判断 LLM 是否可用（不发起真实请求）。"""
    return get_llm_settings().is_configured()
