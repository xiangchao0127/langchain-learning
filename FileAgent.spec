# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

构建：
    pyinstaller FileAgent.spec --noconfirm

产物：
    dist/FileAgent/FileAgent.exe   （目录模式，启动快，适合直接分发或压缩成 zip）

要点说明：
    - 前端静态文件、表结构配置、默认连接配置会一并打进包里；
      首次运行时由 file_agent.config.ensure_user_config() 释放到 exe 同级目录，
      用户可直接编辑 .env 与 config/tables.yaml 后重启生效。
    - uvicorn 在运行期按字符串动态加载事件循环与 HTTP 协议实现，
      PyInstaller 静态分析看不到，必须写进 hiddenimports。
"""

from PyInstaller.utils.hooks import collect_submodules

# ── 随包分发的资源 ────────────────────────────────────────────────────────────
datas = [
    # 前端页面（index.html / style.css / app.js）
    ("file_agent/web/static", "file_agent/web/static"),
    # 目标表结构定义
    ("config", "config"),
    # 默认连接配置（数据库 + 大模型），运行时复制为 exe 同级的 .env
    (".env", "."),
    # 内置示例数据，便于界面上「快速体验」
    ("examples/sample_data", "examples/sample_data"),
]

# ── 动态导入的模块 ────────────────────────────────────────────────────────────
hiddenimports = [
    # uvicorn：按名字符串加载协议与事件循环实现
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # anyio 的后端也是运行期选择的
    "anyio._backends._asyncio",
]

# langchain / langgraph 内部存在较多按需导入，整体收集更稳妥
hiddenimports += collect_submodules("langchain")
hiddenimports += collect_submodules("langgraph")

# ── 明确排除：未使用且体积大 ──────────────────────────────────────────────────
excludes = [
    "tkinter",
    "matplotlib",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "numpy",
    "langchain_community",
    "sqlalchemy",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FileAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FileAgent",
)
