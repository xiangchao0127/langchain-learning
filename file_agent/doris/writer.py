"""Doris 数据写入。

优先使用 **Stream Load**（HTTP，高吞吐、事务性、支持失败回滚），
在不满足条件时自动回退到 MySQL 协议批量 INSERT。

Doris 的 Stream Load 是两跳流程：
    客户端 ──PUT──▶ FE(8030) ──307 Location──▶ BE(8040)
FE 只做路由，真正的数据写入发生在 BE。因此这里禁止 requests 自动跟随重定向
（自动跟随会丢掉 Authorization 头），改为手动跟随并重新附带认证信息。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
import uuid
from decimal import Decimal
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse, urlunparse

import requests

from ..config import DorisSettings, get_doris_settings
from ..schemas import LoadMode, LoadResult
from .client import DorisClient, DorisError

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DorisLoadError(DorisError):
    """数据导入失败。"""


def _chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def strip_userinfo(url: str) -> str:
    """去掉 URL 中的 ``user:pass@``，认证改由 Authorization 头承载。"""
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def normalize_value(value: Any) -> Any:
    """把 Python 值转换为 Doris 可接受的标量。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def normalize_records(
    records: Iterable[dict[str, Any]],
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """规范化记录：裁剪字段、转换取值并丢弃空记录。"""
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        keys = columns or list(record.keys())
        row = {key: normalize_value(record.get(key)) for key in keys}
        if any(value is not None for value in row.values()):
            normalized.append(row)
    return normalized


class DorisWriter:
    """把记录写入 Doris。"""

    # 进程级熔断：一旦确认 Stream Load 在当前环境不可用（通常是 BE 网络不通），
    # 后续批次直接走 INSERT，避免每条数据都浪费一次失败的网络往返。
    _stream_load_disabled_reason: str | None = None

    def __init__(
        self,
        settings: DorisSettings | None = None,
        client: DorisClient | None = None,
    ) -> None:
        self.settings = settings or get_doris_settings()
        self.client = client or DorisClient(self.settings)

    @classmethod
    def reset_stream_load_circuit(cls) -> None:
        """重置 Stream Load 熔断状态（Stream Load 恢复可用后调用）。"""
        cls._stream_load_disabled_reason = None

    @classmethod
    def stream_load_disabled_reason(cls) -> str | None:
        return cls._stream_load_disabled_reason

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def write(
        self,
        records: list[dict[str, Any]],
        table: str,
        *,
        columns: list[str] | None = None,
        mode: LoadMode | str = LoadMode.AUTO,
    ) -> LoadResult:
        """写入记录。

        Args:
            records: 记录列表
            table: 目标表名
            columns: 需要写入的列；None 表示使用记录自身的键
            mode: ``auto`` / ``stream_load`` / ``insert``
        """
        mode = LoadMode(mode) if not isinstance(mode, LoadMode) else mode
        payload = normalize_records(records, columns)

        if not payload:
            return LoadResult(table=table, mode=mode, total=0, message="没有可写入的有效记录")

        target_columns = columns or list(payload[0].keys())
        use_stream_load = (
            self.settings.stream_load_enabled
            and mode in (LoadMode.AUTO, LoadMode.STREAM_LOAD)
            and self.__class__._stream_load_disabled_reason is None
        )

        if use_stream_load:
            try:
                return self._stream_load(payload, table, target_columns)
            except Exception as exc:  # noqa: BLE001 - 需要降级而非中断
                if mode is LoadMode.STREAM_LOAD:
                    return LoadResult(
                        table=table,
                        mode=LoadMode.STREAM_LOAD,
                        total=len(payload),
                        failed=len(payload),
                        message=f"Stream Load 失败: {exc}",
                    )
                self.__class__._stream_load_disabled_reason = str(exc)
                result = self._insert(payload, table, target_columns)
                result.message = (
                    f"Stream Load 不可用，本次及后续写入改用 INSERT（{exc}）。{result.message}".strip()
                )
                return result

        return self._insert(payload, table, target_columns)

    # ------------------------------------------------------------------ #
    # Stream Load
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_label(table: str) -> str:
        return f"{table}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

    def _send_stream_load(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> requests.Response:
        """发送一次 Stream Load 请求（不自动跟随重定向）。"""
        request_headers = dict(headers)
        token = base64.b64encode(
            f"{self.settings.user}:{self.settings.password}".encode()
        ).decode()
        request_headers["Authorization"] = f"Basic {token}"
        return requests.put(
            url,
            data=body,
            headers=request_headers,
            allow_redirects=False,
            timeout=self.settings.load_timeout,
        )

    def _stream_load(
        self,
        records: list[dict[str, Any]],
        table: str,
        columns: list[str],
    ) -> LoadResult:
        settings = self.settings
        url = settings.stream_load_url(table)
        body = json.dumps(records, ensure_ascii=False, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
        label = self._new_label(table)

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "format": "json",
            "strip_outer_array": "true",
            # Doris 的 FE 与 BE 均要求该头部，缺失会直接返回失败
            "Expect": "100-continue",
            "label": label,
            "columns": ",".join(columns),
            "timeout": str(settings.load_timeout),
        }

        try:
            response = self._send_stream_load(url, body, headers)
        except requests.RequestException as exc:
            raise DorisLoadError(
                f"请求 Stream Load 失败: {exc}。"
                f"请确认 FE({settings.host}:{settings.http_port}) 的 HTTP 端口可从本机访问。"
            ) from exc

        # FE 会把请求 307 重定向到某个 BE，需手动跟随并重新附带认证信息
        if response.status_code in _REDIRECT_STATUSES:
            location = response.headers.get("Location", "")
            if not location:
                raise DorisLoadError(
                    f"Stream Load 被重定向但未返回 Location（HTTP {response.status_code}）"
                )
            target = strip_userinfo(location)
            try:
                response = self._send_stream_load(target, body, headers)
            except requests.RequestException as exc:
                raise DorisLoadError(
                    f"跟随重定向到 BE 失败: {exc}。"
                    f"FE 指向的 BE 为 {target}，请确认该 BE 的 HTTP 端口可从本机访问。"
                ) from exc

            if response.status_code in _REDIRECT_STATUSES:
                raise DorisLoadError(
                    f"Stream Load 仍被重定向（HTTP {response.status_code}），"
                    f"目标 BE {strip_userinfo(response.headers.get('Location', ''))} 疑似不可达"
                )

        try:
            result = response.json()
        except ValueError as exc:
            raise DorisLoadError(
                f"Stream Load 返回非 JSON 响应（HTTP {response.status_code}）: {response.text[:300]}"
            ) from exc

        status = result.get("Status")
        if status != "Success":
            raise DorisLoadError(
                f"Stream Load 状态异常: {status} / {result.get('Message')} "
                f"（URL={strip_userinfo(url)}, label={label}）"
            )

        loaded = int(result.get("NumberLoadedRows", len(records)))
        filtered = int(result.get("NumberFilteredRows", 0))
        return LoadResult(
            table=table,
            mode=LoadMode.STREAM_LOAD,
            total=len(records),
            loaded=loaded,
            failed=filtered,
            message=f"label={label}, 耗时 {result.get('LoadTimeMs', '-')}ms",
        )

    # ------------------------------------------------------------------ #
    # INSERT 兜底
    # ------------------------------------------------------------------ #
    def _insert(
        self,
        records: list[dict[str, Any]],
        table: str,
        columns: list[str],
    ) -> LoadResult:
        settings = self.settings
        db = settings.database
        column_sql = ", ".join(f"`{c}`" for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO `{db}`.`{table}` ({column_sql}) VALUES ({placeholders})"

        loaded = 0
        failed = 0
        try:
            with self.client.connection() as conn, conn.cursor() as cursor:
                for batch in _chunked(records, settings.batch_size):
                    rows = [tuple(record.get(c) for c in columns) for record in batch]
                    try:
                        cursor.executemany(sql, rows)
                        loaded += len(rows)
                    except Exception:  # noqa: BLE001 - 批量失败后逐行定位
                        for row in rows:
                            try:
                                cursor.execute(sql, row)
                                loaded += 1
                            except Exception:  # noqa: BLE001
                                failed += 1
        except DorisError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DorisLoadError(f"INSERT 写入失败: {exc}") from exc

        return LoadResult(
            table=table,
            mode=LoadMode.INSERT,
            total=len(records),
            loaded=loaded,
            failed=failed,
        )


__all__ = ["DorisWriter", "DorisLoadError", "normalize_value", "normalize_records", "strip_userinfo"]
