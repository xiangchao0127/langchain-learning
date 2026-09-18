# 半结构化文件解析 + Apache Doris 入库智能体

基于 **LangChain 1.x / LangGraph** 的智能体项目：把半结构化文件（JSONL、CSV、YAML、应用日志、键值对文本）
自动解析为规整数据，并写入 **Apache Doris**。

智能体自主完成「侦察文件 → 选择解析器 → 推断表结构 → 建表 → 入库 → 校验」全流程，
同时也提供一套**不依赖 LLM 的确定性流水线**，两条路径复用同解析器 / 抽取器 / 写入器。

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 多格式解析 | JSON / JSONL / CSV / TSV / YAML / 应用日志 / 键值对文本，支持按内容自动嗅探 |
| 编码自适应 | 自动探测编码（含 GB18030 中文兼容） |
| 日志结构化 | 时间 / 级别 / 来源 / 内容自动拆分，堆栈续行自动合并 |
| LLM 抽取 | 非结构化文本 → 规整记录，字段按目标表对齐 |
| 表结构推断 | 优先复用 `config/tables.yaml`，其次 LLM 推断，最后规则兜底 |
| 高性能入库 | Doris Stream Load（自动跟随 FE→BE 重定向）优先，失败熔断降级到 MySQL 批量 INSERT |
| 智能体 | 用自然语言驱动（基于 LangGraph 的 `create_agent`） |
| Web 界面 | 浏览器内拖拽上传 → 预览 → 入库；内置对话窗口，用自然语言驱动智能体 |

---

## 项目结构

```
langchain-learning/
├── main.py                    # 入口：python main.py ...
├── pyproject.toml             # 依赖与打包配置
├── requirements.txt           # 便于 pip install -r
├── .env.example               # 配置模板（复制为 .env）
├── config/
│   └── tables.yaml            # 「来源文件 → Doris 目标表」映射声明
├── examples/sample_data/      # 示例数据（JSONL / LOG / CSV）
├── tests/test_smoke.py        # 离线冒烟测试（18 项）
└── file_agent/
    ├── config.py              # 配置加载（LLM_* / DORIS_* / AGENT_*）
    ├── schemas.py             # 数据模型 + DDL 生成 + 类型推断
    ├── llm.py                 # LLM 工厂（OpenAI 兼容协议）
    ├── loaders.py             # 编码探测、预览、分块
    ├── extractor.py           # LLM 结构化抽取 / 表结构推断
    ├── table_config.py        # tables.yaml 加载与文件名路由
    ├── pipeline.py            # 端到端确定性流水线
    ├── parsers/               # 解析器体系（registry + 5 种解析器）
    ├── doris/                 # Doris 客户端 + 写入器
    ├── tools/                 # LangChain 工具集（11 个工具）
    ├── agent/graph.py         # LangGraph 智能体
    ├── web/                   # FastAPI 服务 + 交互式 Web 界面
    └── cli.py                 # 命令行入口
```

**数据流**

```
文件 ──▶ 解析器注册表(自动识别) ──▶ 记录列表
                                   │
                    字段未对齐时 ──▶ LLM 抽取（按目标表 schema）
                                   │
                 表结构来源 ──▶ config/tables.yaml │ LLM 推断 │ 规则推断
                                   │
                       DorisWriter ──▶ Stream Load（失败回退 INSERT）
```

---

## 快速开始

### 1. 环境要求

- Python ≥ 3.10（已在 3.13 验证）
- 可选：一个 OpenAI 兼容的模型服务（DeepSeek / 通义千问 / OpenAI / 本地 vLLM）
- 可选：Apache Doris 集群（FE MySQL 端口 9030、HTTP 端口 8030）

### 2. 安装依赖

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

或安装为可执行包（提供 `file-agent` 命令）：

```bash
pip install -e .
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
# 大模型（OpenAI 兼容协议）
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-xxxxxxxx
# 国内模型示例（按需取消注释其一）
# LLM_BASE_URL=https://api.deepseek.com/v1
# LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# Apache Doris
DORIS_HOST=127.0.0.1
DORIS_MYSQL_PORT=9030
DORIS_HTTP_PORT=8030
DORIS_USER=root
DORIS_PASSWORD=
DORIS_DATABASE=demo
```

> 未配置 `LLM_API_KEY` 时，除 `agent` / `chat` 外的命令仍可正常运行，
> 表结构会退化为**规则推断**（`--no-llm` 可显式禁用）。

### 4. 验证环境

```bash
python main.py doctor
```

---

## 命令行用法

```bash
# 检查配置与 Doris 连通性
python main.py doctor

# 识别文件格式（推荐的解析器、编码、大小）
python main.py detect examples/sample_data/orders.jsonl

# 解析文件并预览结构化结果
python main.py parse examples/sample_data/app.log --limit 3

# 推断/复用目标表结构，输出建表 DDL（不连库）
python main.py schema examples/sample_data/devices.csv

# 只演练不写库：生成 DDL 与统计
python main.py load examples/sample_data/orders.jsonl --dry-run

# 正式入库
python main.py load examples/sample_data/orders.jsonl --table ods_order_events

# 用时序日志验证：强制走一次 LLM 抽取
python main.py load examples/sample_data/app.log --force-extract

# 查看已声明的目标表
python main.py tables

# 自然语言驱动智能体
python main.py agent "把 examples/sample_data 下的文件全部解析并入库，完成后统计行数" --verbose

# 交互模式
python main.py chat

# 启动 Web 界面（浏览器内上传入库）
python main.py serve --port 8000
```

`load` 常用参数：

| 参数 | 说明 |
| --- | --- |
| `--table` | 指定目标表名（默认由文件名推导，如 `orders.jsonl` → `ods_orders`） |
| `--parser` | 强制指定解析器 `json/csv/yaml/log/key_value` |
| `--mode` | 写入方式：`auto`（默认）/ `stream_load` / `insert` |
| `--dry-run` | 只生成 DDL 与统计，不写库 |
| `--force-extract` | 即使字段已对齐也走一次 LLM 抽取 |
| `--no-llm` | 完全禁用 LLM，使用纯规则模式 |

---

## Web 界面

```bash
python main.py serve --port 8000
```

浏览器打开 `http://127.0.0.1:8000`，API 文档在 `http://127.0.0.1:8000/docs`。

界面按五步推进，每一步完成后才解锁下一步：

| 步骤 | 说明 |
| --- | --- |
| ① 上传文件 | 拖拽或点击选择，也可用内置示例数据一键体验 |
| ② 解析预览 | 自动识别格式，展示样例记录与列结构 |
| ③ 目标表结构 | 智能推断 / 规则推断可切换，可指定目标表，可展开查看 DDL |
| ④ 执行入库 | 选择写入方式；**默认「仅演练」**，取消勾选才真正写库 |
| ⑤ 执行结果 | 展示行数统计；非演练模式可一键查询前 10 行验证 |

行为说明：

- 上传文件保存在 `.output/uploads/`（已被 git 忽略），单文件上限 100 MB
- 仅接受 JSON / JSONL / CSV / TSV / YAML / LOG / TXT 等数据类扩展名
- 默认只监听 `127.0.0.1`；如需局域网访问用 `--host 0.0.0.0`，
  但那时**没有任何鉴权**，请只在可信网络中使用
- 查询接口仅放行 `SELECT / SHOW / DESC / EXPLAIN`，写操作会被拒绝

### 对话窗口

页面底部是智能助手对话区，直接用自然语言提需求即可，助手会自主决定调用哪些工具。
回答通过 **SSE 流式**返回并逐字显示；工具调用会实时出现在气泡下方；生成过程中按钮
变为「停止」，可随时中断。

常用指令示例：

```
列出 examples/sample_data 下有哪些文件
把 orders.jsonl 解析并写入 Doris，完成后统计行数
查询 ods_order_events 的前 5 行数据
```

- 每轮回复下方会展示**本轮调用的工具及参数**，便于核对模型到底做了什么
- 支持多轮上下文（例如接着追问「刚才一共找到几个文件？」）
- 会话保存在服务端内存，最多 50 个（`_MAX_SESSIONS`），重启即清空
- 点「清空对话」可重置当前会话上下文

> 对话会真实调用工具，**包括写库**。只想试水的话，在指令里明确说「先不要写入」。

### HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | LLM / Doris 状态与已声明目标表 |
| POST | `/api/chat` | 与智能体对话（一次性返回，模型自主调用工具） |
| POST | `/api/chat/stream` | SSE 流式对话，事件类型 `token` / `tool` / `done` / `error` |
| POST | `/api/chat/reset` | 清空指定会话的上下文 |
| GET | `/api/samples` | 内置示例数据列表 |
| POST | `/api/sample` | 直接使用示例文件（免上传） |
| POST | `/api/upload` | 上传文件（multipart） |
| POST | `/api/parse` | 解析并返回列与样例记录 |
| POST | `/api/schema` | 推断或复用目标表结构 + 建表 DDL |
| POST | `/api/ingest` | 执行入库（支持 dry-run） |
| GET | `/api/tables` | 已声明的目标表及其结构 |
| POST | `/api/query` | 只读查询（仅 SELECT / SHOW / DESC / EXPLAIN） |

---

## 目标表配置（config/tables.yaml）

用 `source_patterns` 让文件按名称自动路由到目标表，避免每次手写 `--table`：

```yaml
tables:
  - name: ods_order_events
    comment: "订单事件明细表"
    source_patterns: ["orders*.jsonl", "order_events*"]   # 文件名 glob
    columns:
      - { name: order_id,   dtype: VARCHAR(64),  nullable: false, comment: "订单号" }
      - { name: amount,     dtype: DECIMAL(18,2), nullable: true, comment: "金额" }
      - { name: created_at, dtype: DATETIME,     nullable: true, comment: "创建时间" }
    unique_key: [order_id]
    distributed_by: [order_id]
    buckets: 4
    properties:
      replication_allocation: "tag.location.default: 1"
```

匹配优先级：**显式 `--table`** → **`source_patterns` 文件名匹配** → **按表名查找** → **LLM/规则推断**。

`dtype` 可直接写 Doris 类型：`VARCHAR(n)` / `STRING` / `TEXT` / `INT` / `BIGINT` / `LARGEINT` /
`DOUBLE` / `DECIMAL(p,s)` / `DATE` / `DATETIME` / `BOOLEAN` / `JSON`。

> **字段务必用 block（缩进）写法，不要用 `{ }` 行内写法。**
> YAML 的 flow 集合以逗号分隔键值对，`DECIMAL(18,2)` 中的逗号会被截断成 `DECIMAL(18`，
> 该非法类型随后被降级为 `STRING`——不会报错，但精度丢失。示例配置已全部使用 block 写法。

---

## 智能体工具集

智能体通过以下工具自主决策（`file_agent/tools/`）：

**文件侧**

| 工具 | 用途 |
| --- | --- |
| `list_input_files` | 列出目录下待处理文件 |
| `preview_file` | 预览文件开头内容 |
| `detect_file_format` | 识别格式与推荐解析器 |
| `parse_file` | 解析为结构化记录 |
| `infer_target_schema` | 推断目标表结构 + 生成 DDL |

**Doris 侧**

| 工具 | 用途 |
| --- | --- |
| `inspect_doris_connection` | 连通性检查 |
| `list_configured_tables` | 列出已声明的表 |
| `describe_target_table` | 查看表结构 |
| `create_target_table` | 建表 |
| `ingest_file_into_doris` | 一站式解析 + 入库 |
| `query_doris` | 只读查询校验（仅允许 SELECT/SHOW/DESC/EXPLAIN） |

---

## 扩展指南

**新增一种文件格式**：在 `file_agent/parsers/` 下继承 `BaseParser`，
实现 `parse()`（必需）与 `sniff()`（可选），然后注册到 `parsers/registry.py` 的
`DEFAULT_PARSER_CLASSES`（顺序即优先级）。

**新增一个工具**：在 `file_agent/tools/` 下用 `@tool` 装饰函数并写好 docstring
（模型依赖 docstring 决策），再加入该模块的 `*_TOOLS` 列表。

**调整提示词**：抽取与建表提示词见 `file_agent/extractor.py`；
智能体行为准则见 `file_agent/agent/graph.py` 的 `SYSTEM_PROMPT`。

---

## 测试

```bash
pytest -q
```

18 项离线测试，覆盖解析器识别、日志堆栈合并、类型推断、DDL 生成、
Doris 值规范化、流水线路由，以及智能体「模型 → 工具 → 模型」闭环
（使用脚本化假模型，无需真实 LLM 与 Doris）。

---

## 打包成 Windows exe

```bash
pip install pyinstaller
pyinstaller FileAgent.spec --noconfirm
```

产物在 `dist/FileAgent/`（约 49 MB），把**整个目录**拷贝给他人即可运行：

```
dist/FileAgent/
├── FileAgent.exe     # 双击即启动服务并自动打开浏览器
├── .env              # 首次运行自动释放：数据库与大模型配置
├── config/           # 首次运行自动释放：目标表结构定义
└── _internal/        # 依赖运行库（不要删）
```

**运行方式**

| 场景 | 命令 |
| --- | --- |
| 普通使用 | 双击 `FileAgent.exe` |
| 指定端口 | `FileAgent.exe serve --port 9000` |
| 不自动开浏览器 | `FileAgent.exe serve --no-browser` |
| 命令行工具 | `FileAgent.exe doctor` / `load` / `parse` / `agent` |
| 查看帮助 | `FileAgent.exe --help` |

**配置优先级**：exe 同级目录的 `.env` 与 `config/tables.yaml` 优先；不存在时回落到随包内置的默认值。
首次启动会自动把内置配置释放到同级目录，编辑后重启即可生效。

> ⚠️ **安全提醒**
> 内置配置会随 exe 一起分发，其中的 API Key 与数据库口令对拿到文件的人完全可见。
> 建议：为分发单独申请**受限权限**的账号；分发完成后轮换密钥；不要把 exe 放到公网可下载的位置。

**关于体积**：`FileAgent.spec` 里排除了 `numpy`、`langchain_community`、`sqlalchemy` 等未使用的重依赖
（当前 49 MB）。如果后续功能用到了它们，从 `excludes` 中移除对应项重新打包即可。

---

## 常见问题

**Q：没有 Doris 环境能验证吗？**
可以。`--dry-run` 会跳过所有数据库操作，只输出建表语句与统计；`pytest` 也完全离线。

**Q：日志文件和 CSV 都是文本，会不会误判？**
不会。扩展名明确时按扩展名走；`.txt` / 无扩展名时按内容嗅探（日志行特征、分隔符一致性、键值对比例），
全部失败才退化为键值对解析器。

**Q：Stream Load 是怎么工作的？为什么可能不可用？**

Doris 的 Stream Load 是**两跳流程**：请求先发到 FE 的 HTTP 端口（默认 8030），
FE 返回 `307` 重定向到某个 BE 的 HTTP 端口（默认 8040），真正的写入发生在 BE。
因此 **FE 与 BE 的 HTTP 端口都必须能从运行代码的机器访问**。

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `ConnectionResetError` / 连接被重置 | BE 端口网络不通（TCP 握手成功但数据被阻断） | 打通网络，或设 `DORIS_STREAM_LOAD_ENABLED=false` |
| `There is no 100-continue header` | 缺少 `Expect: 100-continue` 头 | 已内置该头，若仍报错说明请求被中间件改写 |
| `Status: Fail` | 字段不匹配 / 权限不足 | 用 `--mode insert` 复现，错误信息更具体 |

`requests` 默认自动跟随重定向时会丢掉 `Authorization` 头，因此本项目**手动跟随**
并重新附带认证信息（见 `file_agent/doris/writer.py`）。

**Q：为什么 `load` 结果显示走了 INSERT？**

说明 Stream Load 被**熔断降级**了，结果消息里会带上具体原因。熔断是进程级的：
一旦确认当前环境不可用，后续批次直接走 INSERT，不会反复浪费时间重试。
确认 BE 网络打通后，把 `.env` 中的 `DORIS_STREAM_LOAD_ENABLED` 改回 `true` 即可。
