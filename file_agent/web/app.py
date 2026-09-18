"""FastAPI Web 服务。

把「上传文件 → 解析预览 → 推断表结构 → 写入 Doris」这条链路暴露为 HTTP 接口，
并托管一个单页交互界面（``file_agent/web/static/``）。

启动：
    python main.py serve --port 8000
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import PROJECT_ROOT, get_doris_settings, get_llm_settings, get_runtime_settings
from ..doris import DorisClient, DorisWriter, ensure_readonly_sql
from ..loaders import file_meta
from ..parsers import build_default_registry
from ..pipeline import resolve_table_spec, run_pipeline, table_name_from_path
from ..schemas import LoadMode
from ..table_config import get_table_spec, list_table_names

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLE_DIR = PROJECT_ROOT / "examples" / "sample_data"

# 上传文件 ID 固定为 uuid4 的十六进制串，借此杜绝路径穿越
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ALLOWED_SUFFIXES = {
    ".json", ".jsonl", ".ndjson",
    ".csv", ".tsv", ".psv",
    ".yaml", ".yml",
    ".log",
    ".txt", ".kv", ".conf", ".ini", ".properties", ".dat",
}

_REGISTRY = build_default_registry()

# ── 智能体会话 ───────────────────────────────────────────────────────────────
# 会话消息保存在进程内存中，多 worker 部署时需要换成 Redis 等共享存储。
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_SESSION_STORE: dict[str, list[Any]] = {}
_MAX_SESSIONS = 50
_AGENT: Any = None
_LOCK = threading.Lock()


def get_agent():
    """惰性构建智能体并在进程内复用（首次调用需要有效的 LLM 配置）。"""
    global _AGENT
    if _AGENT is None:
        with _LOCK:
            if _AGENT is None:
                from ..agent import build_agent

                _AGENT = build_agent()
    return _AGENT


# --------------------------------------------------------------------------- #
# 上传文件管理
# --------------------------------------------------------------------------- #
def upload_root() -> Path:
    """上传文件的存放目录（位于 .output 下，已被 git 忽略）。"""
    root = get_runtime_settings().output_dir / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_upload(file_id: str) -> Path:
    """由 file_id 定位上传的文件，非法或不存在时抛出 HTTPException。"""
    if not _FILE_ID_RE.match(file_id or ""):
        raise HTTPException(status_code=400, detail="非法的 file_id")

    folder = upload_root() / file_id
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="文件不存在或已过期，请重新上传")

    for entry in sorted(folder.iterdir()):
        if entry.is_file():
            return entry
    raise HTTPException(status_code=404, detail="文件不存在或已过期，请重新上传")


def store_file(file_id: str, name: str, source: Path) -> Path:
    """把文件复制到该 file_id 的目录中。"""
    folder = upload_root() / file_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    shutil.copy2(source, target)
    return target


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class ParseRequest(BaseModel):
    file_id: str
    parser: str = ""
    limit: int = Field(default=20, ge=1, le=200)


class SchemaRequest(BaseModel):
    file_id: str
    parser: str = ""
    table: str = ""
    use_llm: bool | None = None
    force_infer: bool = False


class IngestRequest(BaseModel):
    file_id: str
    parser: str = ""
    table: str = ""
    mode: str = "auto"
    dry_run: bool = False
    use_llm: bool | None = None
    force_extract: bool = False


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    limit: int = Field(default=50, ge=1, le=500)


class SampleRequest(BaseModel):
    name: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = ""


class ChatResetRequest(BaseModel):
    session_id: str = ""


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    """环境体检：LLM 是否配置、Doris 是否连通、已声明哪些目标表。"""
    llm = get_llm_settings()
    doris = get_doris_settings()

    doris_info: dict[str, Any] = {"host": doris.host, "database": doris.database}
    try:
        DorisClient().query("SELECT 1 AS ok")
        doris_info["connected"] = True
    except Exception as exc:  # noqa: BLE001 - 体检需要返回而非抛出
        doris_info["connected"] = False
        doris_info["error"] = str(exc)

    return {
        "llm": {"configured": llm.is_configured(), "model": llm.model},
        "doris": doris_info,
        "tables": list_table_names(),
        "stream_load": {
            "enabled": doris.stream_load_enabled,
            "disabled_reason": DorisWriter.stream_load_disabled_reason(),
        },
    }


@router.get("/samples")
def list_samples() -> dict[str, Any]:
    """列出内置示例数据，便于快速体验。"""
    if not SAMPLE_DIR.is_dir():
        return {"files": []}
    files = [
        {"name": entry.name, "size": entry.stat().st_size}
        for entry in sorted(SAMPLE_DIR.iterdir())
        if entry.is_file()
    ]
    return {"files": files}


@router.post("/sample")
def use_sample(req: SampleRequest) -> dict[str, Any]:
    """直接使用内置示例文件，省去手工上传。"""
    source = SAMPLE_DIR / Path(req.name).name
    if not source.is_file():
        raise HTTPException(status_code=404, detail=f"示例文件不存在：{req.name}")

    file_id = uuid.uuid4().hex
    target = store_file(file_id, source.name, source)
    return {"file_id": file_id, "meta": file_meta(target)}


@router.post("/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传文件（流式落盘，带类型与大小校验）。"""
    name = Path(file.filename or "upload.dat").name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix or '（无扩展名）'}")

    file_id = uuid.uuid4().hex
    folder = upload_root() / file_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name

    size = 0
    try:
        with target.open("wb") as sink:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 限制",
                    )
                sink.write(chunk)
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()

    return {"file_id": file_id, "meta": file_meta(target)}


@router.post("/parse")
def parse(req: ParseRequest) -> dict[str, Any]:
    """解析文件并返回列名与样例记录。"""
    path = resolve_upload(req.file_id)
    result = _REGISTRY.parse(path, parser_name=req.parser or None)

    columns: list[str] = []
    seen: set[str] = set()
    for record in result.records[:100]:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    return {
        "parser": result.parser,
        "record_count": result.record_count,
        "errors": result.errors,
        "columns": columns,
        "records": result.records[: req.limit],
        "available_parsers": _REGISTRY.names(),
    }


@router.post("/schema")
def schema(req: SchemaRequest) -> dict[str, Any]:
    """推断（或复用配置中的）目标表结构，并给出建表 DDL。"""
    path = resolve_upload(req.file_id)
    parsed = _REGISTRY.parse(path, parser_name=req.parser or None)
    if not parsed.records:
        raise HTTPException(
            status_code=400,
            detail=f"解析结果为空，无法推断表结构：{parsed.errors}",
        )

    table_name = req.table or table_name_from_path(path)
    try:
        spec, inference = resolve_table_spec(
            parsed.records,
            table_name,
            source=path,
            explicit_table=bool(req.table),
            ignore_config=req.force_infer,
            use_llm=req.use_llm,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return {
        "table": spec.name,
        "inference": inference,
        "parser": parsed.parser,
        "record_count": parsed.record_count,
        "spec": spec.model_dump(),
        "ddl": spec.ddl(database=get_doris_settings().database),
    }


@router.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    """执行完整的解析 + 入库流程（支持 dry-run）。"""
    if req.mode not in {mode.value for mode in LoadMode}:
        raise HTTPException(status_code=400, detail=f"非法的写入方式：{req.mode}")

    path = resolve_upload(req.file_id)
    try:
        report = run_pipeline(
            path,
            table=req.table or None,
            parser_name=req.parser or None,
            use_llm=req.use_llm,
            force_extraction=req.force_extract,
            dry_run=req.dry_run,
            mode=req.mode,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    payload = report.model_dump()
    payload["summary"] = report.summary()
    payload["success"] = report.load.ok if report.load else True
    return payload


@router.get("/tables")
def tables() -> dict[str, Any]:
    """列出 config/tables.yaml 中声明的目标表及其结构。"""
    specs: list[dict[str, Any]] = []
    for name in list_table_names():
        spec = get_table_spec(name)
        if spec is None:
            continue
        specs.append(
            {
                "name": spec.name,
                "comment": spec.comment,
                "columns": [column.model_dump() for column in spec.columns],
                "ddl": spec.ddl(database=get_doris_settings().database),
            }
        )
    return {"tables": [spec["name"] for spec in specs], "specs": specs}


@router.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    """只读查询，用于入库后校验数据。"""
    try:
        sql = ensure_readonly_sql(req.sql)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        rows = DorisClient().query(sql)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"row_count": len(rows), "rows": rows[: req.limit]}


@router.post("/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    """与智能体对话，由模型自主决定调用哪些工具。"""
    from ..agent import run_agent

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = (
        req.session_id if _SESSION_RE.match(req.session_id or "") else uuid.uuid4().hex
    )
    history = list(_SESSION_STORE.get(session_id, []))

    try:
        run = run_agent(message, agent=get_agent(), history=history)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    # 只提取本轮新增的工具调用
    new_messages = run.messages[len(history) :] if len(run.messages) >= len(history) else run.messages
    steps = [
        {"name": call.get("name"), "args": call.get("args")}
        for msg in new_messages
        for call in (getattr(msg, "tool_calls", None) or [])
    ]

    with _LOCK:
        _SESSION_STORE[session_id] = run.messages
        while len(_SESSION_STORE) > _MAX_SESSIONS:
            _SESSION_STORE.pop(next(iter(_SESSION_STORE)))

    return {"session_id": session_id, "answer": run.answer, "steps": steps}


@router.post("/chat/reset")
def chat_reset(req: ChatResetRequest) -> dict[str, Any]:
    """清空指定会话的上下文。"""
    _SESSION_STORE.pop(req.session_id, None)
    return {"ok": True}


def _sse(event: str, data: Any) -> str:
    """按 SSE 规范格式化一条事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """流式对话：逐 token 推送回答，并实时上报工具调用。

    事件类型：
        token  模型生成的正文片段
        tool   一次工具调用（名称 + 参数）
        done   结束，携带完整回答与本次所有工具调用
        error  执行失败
    """
    from langchain_core.messages import AIMessageChunk, HumanMessage

    from ..extractor import message_text
    from ..schemas import to_jsonable

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = (
        req.session_id if _SESSION_RE.match(req.session_id or "") else uuid.uuid4().hex
    )
    history = list(_SESSION_STORE.get(session_id, []))

    def generate() -> Iterator[str]:
        try:
            agent = get_agent()
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            return

        answer_parts: list[str] = []
        steps: list[dict[str, Any]] = []
        produced: list[Any] = []

        try:
            stream = agent.stream(
                {"messages": [*history, HumanMessage(content=message)]},
                stream_mode=["messages", "updates"],
            )
            for mode, payload in stream:
                if mode == "messages":
                    chunk, _meta = payload
                    # 只推送模型生成的正文，工具返回的消息要过滤掉
                    if not isinstance(chunk, AIMessageChunk):
                        continue
                    piece = message_text(chunk)
                    if piece:
                        answer_parts.append(piece)
                        yield _sse("token", {"text": piece})
                elif mode == "updates":
                    for update in (payload or {}).values():
                        if not isinstance(update, dict):
                            continue
                        for msg in update.get("messages") or []:
                            produced.append(msg)
                            for call in getattr(msg, "tool_calls", None) or []:
                                step = {
                                    "name": call.get("name"),
                                    "args": to_jsonable(call.get("args") or {}),
                                }
                                steps.append(step)
                                yield _sse("tool", step)
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            return

        with _LOCK:
            _SESSION_STORE[session_id] = [*history, HumanMessage(content=message), *produced]
            while len(_SESSION_STORE) > _MAX_SESSIONS:
                _SESSION_STORE.pop(next(iter(_SESSION_STORE)))

        yield _sse(
            "done",
            {
                "session_id": session_id,
                "answer": "".join(answer_parts),
                "steps": steps,
            },
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 应用组装
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    app = FastAPI(
        title="半结构化文件入库平台",
        description="上传半结构化文件，自动解析并写入 Apache Doris",
        version="0.1.0",
    )
    app.include_router(router, prefix="/api")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()

__all__ = ["app", "create_app", "router", "upload_root", "resolve_upload"]
