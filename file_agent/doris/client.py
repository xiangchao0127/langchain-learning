"""Apache Doris 客户端（基于 MySQL 协议）。

负责：连通性检测、建库建表、元数据查询、SQL 执行。
高性能数据导入由 ``writer.DorisWriter`` 通过 Stream Load 完成。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from ..config import DorisSettings, get_doris_settings
from ..schemas import TableSpec


_READONLY_PREFIXES = ("select", "show", "desc", "describe", "explain", "with")


class DorisError(RuntimeError):
    """Doris 操作异常。"""


def ensure_readonly_sql(sql: str) -> str:
    """校验 SQL 为只读语句，否则抛出 ``ValueError``；返回去除首尾空白后的语句。"""
    statement = (sql or "").strip()
    head = statement.split(maxsplit=1)[0].lower() if statement else ""
    if head not in _READONLY_PREFIXES:
        raise ValueError("出于安全考虑，仅允许只读查询（SELECT / SHOW / DESC / EXPLAIN）")
    return statement


class DorisClient:
    """轻量 Doris 客户端。"""

    def __init__(self, settings: DorisSettings | None = None, *, database: str | None = None) -> None:
        self.settings = settings or get_doris_settings()
        self._database = database

    @property
    def database(self) -> str:
        return self._database or self.settings.database

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def _connect(self, database: str | None) -> pymysql.connections.Connection:
        try:
            return pymysql.connect(
                host=self.settings.host,
                port=self.settings.mysql_port,
                user=self.settings.user,
                password=self.settings.password,
                database=database,
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=True,
                connect_timeout=10,
            )
        except pymysql.MySQLError as exc:
            raise DorisError(f"连接 Doris 失败: {exc}") from exc

    @contextmanager
    def connection(self) -> Iterator[pymysql.connections.Connection]:
        """带 database 的连接。"""
        conn = self._connect(self.database)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def server_connection(self) -> Iterator[pymysql.connections.Connection]:
        """不带 database 的连接，用于建库。"""
        conn = self._connect(None)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # 基础操作
    # ------------------------------------------------------------------ #
    def query(self, sql: str, params: tuple | list | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall() or [])

    def execute(self, sql: str, params: tuple | list | None = None) -> int:
        with self.connection() as conn, conn.cursor() as cursor:
            return cursor.execute(sql, params)

    def ping(self) -> bool:
        """连通性检测（不抛异常）。"""
        try:
            self.query("SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001 - 健康检查需要吞掉异常
            return False

    # ------------------------------------------------------------------ #
    # 元数据
    # ------------------------------------------------------------------ #
    def list_databases(self) -> list[str]:
        rows = self.query("SHOW DATABASES")
        return [str(list(row.values())[0]) for row in rows]

    def list_tables(self, database: str | None = None) -> list[str]:
        db = database or self.database
        rows = self.query("SHOW TABLES FROM `%s`" % db)
        return [str(list(row.values())[0]) for row in rows]

    def table_exists(self, table: str, database: str | None = None) -> bool:
        db = database or self.database
        rows = self.query(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (db, table),
        )
        return bool(rows and int(rows[0]["cnt"]) > 0)

    def describe_table(self, table: str, database: str | None = None) -> list[dict[str, Any]]:
        """返回字段名 / 类型 / 是否可空 / 注释。"""
        db = database or self.database
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT "
            "FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (db, table),
        )
        return [
            {
                "name": row["COLUMN_NAME"],
                "type": row["COLUMN_TYPE"],
                "nullable": str(row["IS_NULLABLE"]).upper() == "YES",
                "comment": row["COLUMN_COMMENT"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # 建库建表
    # ------------------------------------------------------------------ #
    def create_database(self, database: str | None = None) -> str:
        db = database or self.database
        with self.server_connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db}`")
        return db

    def create_table(
        self,
        spec: TableSpec,
        database: str | None = None,
        *,
        drop_if_exists: bool = False,
    ) -> str:
        """按表定义建表，返回实际执行的 DDL。"""
        db = self.create_database(database)
        if drop_if_exists:
            self.execute(f"DROP TABLE IF EXISTS `{db}`.`{spec.name}`")

        ddl = spec.ddl(database=db)
        try:
            self.execute(ddl)
        except DorisError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DorisError(f"建表失败: {exc}\nDDL:\n{ddl}") from exc
        return ddl

    def drop_table(self, table: str, database: str | None = None) -> None:
        db = database or self.database
        self.execute(f"DROP TABLE IF EXISTS `{db}`.`{table}`")

    def count_rows(self, table: str, database: str | None = None) -> int:
        db = database or self.database
        rows = self.query(f"SELECT COUNT(*) AS cnt FROM `{db}`.`{table}`")
        return int(rows[0]["cnt"]) if rows else 0


__all__ = ["DorisClient", "DorisError"]
