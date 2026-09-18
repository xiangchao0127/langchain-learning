"""Web 服务层：把文件解析入库能力暴露为 HTTP 接口并提供交互页面。"""

from .app import app, create_app

__all__ = ["app", "create_app"]
