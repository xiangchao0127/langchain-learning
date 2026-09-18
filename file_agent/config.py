"""配置加载。

所有配置项均通过「环境变量 / .env 文件」注入，便于在不同环境间切换：
    LLM_*    大模型相关
    DORIS_*  Apache Doris 相关
    AGENT_*  运行时相关
"""

from __future__ import annotations

import shutil
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的可执行文件中。"""
    return bool(getattr(sys, "frozen", False))


# APP_DIR —— 可写目录：
#   打包后 = exe 所在目录（用户能看到、能改配置）
#   未打包 = 项目根目录
APP_DIR = (
    Path(sys.executable).resolve().parent
    if is_frozen()
    else Path(__file__).resolve().parent.parent
)

# BUNDLE_DIR —— 只读资源目录：
#   打包后 = PyInstaller 的临时解压目录（含内置默认配置与前端静态文件）
#   未打包 = 项目根目录
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) if is_frozen() else APP_DIR

PROJECT_ROOT = APP_DIR  # 兼容既有引用

ENV_FILE = APP_DIR / ".env"
BUNDLED_ENV = BUNDLE_DIR / ".env"
CONFIG_DIR = APP_DIR / "config"
BUNDLED_CONFIG_DIR = BUNDLE_DIR / "config"


def ensure_user_config() -> list[str]:
    """打包运行时，首次启动把内置配置释放到 exe 同级目录，便于用户修改。

    Returns:
        本次新生成的文件 / 目录路径列表。
    """
    if not is_frozen():
        return []

    created: list[str] = []
    if not ENV_FILE.exists() and BUNDLED_ENV.exists():
        shutil.copy2(BUNDLED_ENV, ENV_FILE)
        created.append(str(ENV_FILE))
    if not CONFIG_DIR.exists() and BUNDLED_CONFIG_DIR.exists():
        shutil.copytree(BUNDLED_CONFIG_DIR, CONFIG_DIR)
        created.append(str(CONFIG_DIR))
    return created

_BASE_CONFIG = SettingsConfigDict(
    env_file=ENV_FILE,
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


class LLMSettings(BaseSettings):
    """大模型配置，环境变量前缀 ``LLM_``。"""

    model_config = SettingsConfigDict(env_prefix="LLM_", **_BASE_CONFIG)

    provider: str = Field(default="openai", description="模型提供方（统一走 OpenAI 兼容协议）")
    model: str = Field(default="gpt-4o-mini", description="模型名称")
    api_key: str = Field(default="", description="API Key")
    base_url: str | None = Field(default=None, description="OpenAI 兼容接口地址，留空用官方")
    temperature: float = 0.0
    timeout: int = 60
    max_retries: int = 2

    def resolved_base_url(self) -> str | None:
        """空字符串统一按 None 处理，避免传给 SDK 后报错。"""
        return self.base_url.strip() or None if self.base_url else None

    def is_configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "sk-your-api-key"


class DorisSettings(BaseSettings):
    """Apache Doris 配置，环境变量前缀 ``DORIS_``。"""

    model_config = SettingsConfigDict(env_prefix="DORIS_", **_BASE_CONFIG)

    host: str = "127.0.0.1"
    mysql_port: int = 9030
    http_port: int = 8030
    user: str = "root"
    password: str = ""
    database: str = "demo"
    batch_size: int = 500
    load_timeout: int = 300
    stream_load_enabled: bool = True

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    def stream_load_url(self, table: str) -> str:
        """Stream Load 的 REST 端点。"""
        return f"{self.http_base}/api/{self.database}/{table}/_stream_load"

    def safe_repr(self) -> str:
        """用于日志输出，隐藏密码。"""
        return (
            f"Doris(host={self.host}, mysql_port={self.mysql_port}, "
            f"http_port={self.http_port}, user={self.user}, database={self.database})"
        )


class RuntimeSettings(BaseSettings):
    """运行时配置，环境变量前缀 ``AGENT_``。"""

    model_config = SettingsConfigDict(env_prefix="AGENT_", **_BASE_CONFIG)

    output_dir: Path = PROJECT_ROOT / ".output"
    max_preview_chars: int = 2000
    chunk_lines: int = 200

    @property
    def tables_config_path(self) -> Path:
        """优先用 exe 同级目录的配置，回落到打包内置的默认配置。"""
        user_config = CONFIG_DIR / "tables.yaml"
        return user_config if user_config.exists() else BUNDLED_CONFIG_DIR / "tables.yaml"


@lru_cache(maxsize=1)
def get_llm_settings() -> LLMSettings:
    return LLMSettings()


@lru_cache(maxsize=1)
def get_doris_settings() -> DorisSettings:
    return DorisSettings()


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()


def reset_settings_cache() -> None:
    """清空配置缓存（测试或动态改配置后调用）。"""
    get_llm_settings.cache_clear()
    get_doris_settings.cache_clear()
    get_runtime_settings.cache_clear()
