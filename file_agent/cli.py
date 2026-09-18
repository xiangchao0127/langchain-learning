"""命令行入口。

用法示例：
    python main.py doctor
    python main.py detect examples/sample_data/orders.jsonl
    python main.py parse  examples/sample_data/app.log --limit 3
    python main.py schema examples/sample_data/devices.csv
    python main.py load   examples/sample_data/orders.jsonl --dry-run
    python main.py agent  "把 examples/sample_data 下的文件都解析并入库"
    python main.py chat
    python main.py serve --port 8000
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

from .config import (
    ensure_user_config,
    get_doris_settings,
    get_llm_settings,
    is_frozen,
)
from .loaders import file_meta
from .parsers import build_default_registry
from .pipeline import available_tables, resolve_table_spec, run_pipeline, table_name_from_path


def _configure_stdout() -> None:
    """Windows 控制台下保证中文正常输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_detect(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    registry = build_default_registry()
    parser = registry.detect(path)
    meta = file_meta(path)
    meta.update(
        {
            "recommended_parser": parser.name,
            "parser_description": parser.description,
            "available_parsers": registry.names(),
        }
    )
    print(_dumps(meta))
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    result = build_default_registry().parse(path, parser_name=args.parser or None)
    columns: list[str] = []
    seen: set[str] = set()
    for record in result.records[:50]:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    print(
        _dumps(
            {
                "source": result.source,
                "parser": result.parser,
                "record_count": result.record_count,
                "errors": result.errors,
                "columns": columns,
                "sample_records": result.records[: args.limit],
            }
        )
    )
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    result = build_default_registry().parse(path, parser_name=args.parser or None)
    if not result.records:
        print(f"解析结果为空，无法推断表结构。错误: {result.errors}", file=sys.stderr)
        return 1

    table_name = args.table or table_name_from_path(path)
    try:
        spec, inference = resolve_table_spec(
            result.records,
            table_name,
            source=path,
            explicit_table=bool(args.table),
            ignore_config=args.force_infer,
            use_llm=False if args.no_llm else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"-- 表结构来源: {inference}")
    print(_dumps(spec.model_dump()))
    print()
    print(spec.ddl(database=get_doris_settings().database))
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    try:
        report = run_pipeline(
            args.file,
            table=args.table or None,
            parser_name=args.parser or None,
            use_llm=False if args.no_llm else None,
            force_extraction=args.force_extract,
            dry_run=args.dry_run,
            mode=args.mode,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(report.summary())
    if report.ddl:
        print("\n-- 建表语句 --")
        print(report.ddl)
    return 0


def cmd_tables(args: argparse.Namespace) -> int:
    names = available_tables()
    if not names:
        print("config/tables.yaml 中尚未声明任何表。")
        return 0
    print(_dumps({"tables": names}))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doris import DorisClient

    llm = get_llm_settings()
    print("== LLM 配置 ==")
    print(f"  model     : {llm.model}")
    print(f"  base_url  : {llm.resolved_base_url() or '(官方默认地址)'}")
    print(f"  api_key   : {'已配置' if llm.is_configured() else '未配置'}")
    print(f"  temperature: {llm.temperature}")

    doris = get_doris_settings()
    print("\n== Doris 配置 ==")
    print(f"  target    : {doris.safe_repr()}")
    print(f"  stream_load: {'启用' if doris.stream_load_enabled else '禁用'}")

    client = DorisClient()
    print("\n== 连通性 ==")
    if client.ping():
        print("  Doris     : 连接成功")
        try:
            print(f"  数据库     : {client.list_databases()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  数据库列表获取失败: {exc}")
    else:
        print("  Doris     : 连接失败（请检查 DORIS_* 配置与网络）")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from .agent import build_agent, format_tool_calls, run_agent

    try:
        agent = build_agent()
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 无法初始化智能体: {exc}", file=sys.stderr)
        return 1

    run = run_agent(args.query, agent=agent)
    if args.verbose:
        print("== 工具调用轨迹 ==")
        print(format_tool_calls(run))
        print()
    print(run.answer)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from .agent import build_agent, run_agent

    try:
        agent = build_agent()
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 无法初始化智能体: {exc}", file=sys.stderr)
        return 1

    print("已进入智能体交互模式，输入 exit / quit 退出。\n")
    history: list = []
    while True:
        try:
            query = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.lower() in {"exit", "quit", "q"}:
            break
        if not query:
            continue
        run = run_agent(query, agent=agent, history=history)
        history = run.messages
        print(f"\n{run.answer}\n")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "未安装 Web 依赖，请先执行：\n"
            "    pip install fastapi \"uvicorn[standard]\" python-multipart",
            file=sys.stderr,
        )
        return 1

    host: str = args.host
    port: int = args.port
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}"

    print(f"Web 界面已启动：{url}")
    print(f"API 文档：      {url}/docs")
    print("按 Ctrl+C 停止\n")

    reload_enabled = bool(args.reload)
    if reload_enabled and is_frozen():
        print("[提示] 打包环境下 --reload 不可用，已忽略", file=sys.stderr)
        reload_enabled = False

    if not getattr(args, "no_browser", False):
        # 稍等片刻再打开，避免服务尚未就绪时访问被拒
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    if reload_enabled:
        # reload 模式必须传 import 字符串
        uvicorn.run("file_agent.web.app:app", host=host, port=port, reload=True)
    else:
        from .web.app import app as application

        uvicorn.run(application, host=host, port=port)
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-agent",
        description="半结构化文件解析 + Apache Doris 入库智能体",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="识别文件格式与推荐解析器")
    p_detect.add_argument("file", help="文件路径")
    p_detect.set_defaults(func=cmd_detect)

    p_parse = sub.add_parser("parse", help="解析文件并预览结构化结果")
    p_parse.add_argument("file", help="文件路径")
    p_parse.add_argument("--parser", default="", help="指定解析器: json/csv/yaml/log/key_value")
    p_parse.add_argument("--limit", type=int, default=5, help="预览记录条数")
    p_parse.set_defaults(func=cmd_parse)

    p_schema = sub.add_parser("schema", help="推断 Doris 目标表结构并输出 DDL")
    p_schema.add_argument("file", help="文件路径")
    p_schema.add_argument("--parser", default="", help="指定解析器")
    p_schema.add_argument("--table", default="", help="目标表名")
    p_schema.add_argument("--no-llm", action="store_true", help="禁用 LLM，仅用规则推断")
    p_schema.add_argument("--force-infer", action="store_true", help="忽略 config/tables.yaml 的声明")
    p_schema.set_defaults(func=cmd_schema)

    p_load = sub.add_parser("load", help="执行完整的解析 + 入库流程")
    p_load.add_argument("file", help="文件路径")
    p_load.add_argument("--table", default="", help="目标表名")
    p_load.add_argument("--parser", default="", help="指定解析器")
    p_load.add_argument(
        "--mode", default="auto", choices=["auto", "stream_load", "insert"], help="写入方式"
    )
    p_load.add_argument("--dry-run", action="store_true", help="只生成 DDL 与统计，不写库")
    p_load.add_argument("--force-extract", action="store_true", help="强制走一次 LLM 抽取")
    p_load.add_argument("--no-llm", action="store_true", help="禁用 LLM")
    p_load.set_defaults(func=cmd_load)

    p_tables = sub.add_parser("tables", help="列出 config/tables.yaml 中声明的表")
    p_tables.set_defaults(func=cmd_tables)

    p_doctor = sub.add_parser("doctor", help="检查 LLM / Doris 配置与连通性")
    p_doctor.set_defaults(func=cmd_doctor)

    p_agent = sub.add_parser("agent", help="用自然语言驱动智能体")
    p_agent.add_argument("query", help="自然语言指令")
    p_agent.add_argument("--verbose", action="store_true", help="打印工具调用轨迹")
    p_agent.set_defaults(func=cmd_agent)

    p_chat = sub.add_parser("chat", help="进入智能体交互模式")
    p_chat.set_defaults(func=cmd_chat)

    p_serve = sub.add_parser("serve", help="启动 Web 界面：上传文件 → 解析 → 入库 Doris")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    p_serve.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重启")
    p_serve.add_argument(
        "--no-browser", action="store_true", help="启动后不自动打开浏览器"
    )
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()

    # 打包运行时：首次启动把内置配置释放到 exe 同级目录，便于用户修改
    created = ensure_user_config()
    if created:
        print("[初始化] 已释放内置配置到程序目录：")
        for path in created:
            print(f"  - {path}")
        print()

    args_list = list(sys.argv[1:] if argv is None else argv)
    # 双击 exe（无任何参数）时默认启动 Web 界面
    if not args_list:
        args_list = ["serve"]

    parser = build_parser()
    args = parser.parse_args(args_list)
    return args.func(args)


__all__ = ["main", "build_parser"]
