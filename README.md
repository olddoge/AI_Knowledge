# AI Knowledge RAG

企业内部文件入库处理项目，用于将原始文件扫描入库、解析为 Markdown / 文本、清洗规范化，并上传到 AI 知识库 / RAG / LightRAG 系统。

项目目标优先级：

- 稳定、可追踪、可恢复
- 支持批处理与失败重试
- 保留原始文件路径、文件哈希和处理状态
- 入库前统一执行清洗流程
- 不删除原始文件

## 当前能力

- 扫描指定目录下的文件并写入 MySQL `rag_files` 表。
- 根据文件哈希去重，避免重复处理同一文件。
- 支持扫描文件类型：`pdf`、`docx`、`xlsx`、`txt`、`md`、`markdown`。
- 解析任务轮询 `rag_files` 中未解析记录，锁定后批量请求 MinerU `/file_parse`。
- 从解析结果中提取 Markdown，并保存返回图片到本地图片目录。
- 清洗 Markdown 中的 HTML 标签、乱码字符、目录锚点、页码行和多余空行。
- 将清洗后的文本上传到 LightRAG `/documents/texts`。
- 解析中 / 清洗中 / 失败状态可在重启后恢复或重试。
- 支持日志输出到 `logs/`。

## 目录结构

```text
ai_knowledge_rag/
|-- AGENTS.md
|-- README.md
|-- PROJECT_STRUCTURE.md
|-- .env.example
|-- .env
|-- main.py
|-- scan_main.py
|-- sql/
|-- src/
|-- original/
|-- output/
`-- logs/
```

更完整的目录说明见 [PROJECT_STRUCTURE.md](./PROJECT_STRUCTURE.md)。

## 环境要求

- Python 3.13
- MySQL 8
- MinerU 解析服务
- LightRAG 服务

Python 依赖当前未提供 `requirements.txt`，至少需要安装：

```bash
pip install pymysql
```

## 初始化配置

1. 复制环境变量模板：

```bash
copy .env.example .env
```

2. 修改 `.env`：

```env
LIGHTRAG_SERVER_URL=http://localhost:9621
MINERU_SERVER_URL=http://localhost:8000

DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=ai_knowledge_rag

SCAN_INPUT_PATH=./original
PARSE_OUTPUT_PATH=./output
IMAGE_OUTPUT_PATH=./output/images
SAVE_PARSE_MARKDOWN=false

ENABLE_LOGGING=true
IGNORE_FILE_TYPES=

PARSE_REQUEST_TIMEOUT_SECONDS=300
PARSE_TASK_POLL_INTERVAL_SECONDS=10
PARSE_TASK_BATCH_SIZE=5
CLEAN_TASK_POLL_INTERVAL_SECONDS=10
CLEAN_TASK_BATCH_SIZE=5
```

3. 初始化数据库表：

```bash
mysql -h 127.0.0.1 -u root -p ai_knowledge_rag < sql/rag_files_table.sql
mysql -h 127.0.0.1 -u root -p ai_knowledge_rag < sql/rag_image_table.sql
```

## 使用方式

### 1. 扫描原始文件

将待入库文件放入 `.env` 中 `SCAN_INPUT_PATH` 指向的目录，然后执行：

```bash
python scan_main.py
```

扫描任务会：

- 递归扫描目录
- 跳过不支持的文件类型
- 跳过 `IGNORE_FILE_TYPES` 中配置的类型
- 跳过 Office 临时文件，如 `~$xxx.docx`
- 计算 SHA-256 文件哈希
- 写入 `rag_files` 表
- 已存在相同 `file_hash` 的文件不会重复插入

### 2. 启动处理流程

```bash
python main.py
```

主流程会同时启动：

- 解析任务
- 清洗任务
- LightRAG 上传占位任务

目前主链路中，解析任务拿到 MinerU 返回的 Markdown 后会立即执行清洗并上传 LightRAG。清洗任务主要用于兼容历史上已经落盘到 `parse_path` 的 Markdown 文件。

## 状态说明

`rag_files.parse_status`：

- `0`：未解析
- `1`：正在解析
- `2`：解析完成
- `-1`：解析失败

`rag_files.clean_status`：

- `0`：未清洗
- `1`：正在清洗
- `2`：已清洗
- `-1`：清洗失败

任务启动时会自动恢复部分异常中断状态：

- `parse_status = 1` 会恢复为 `0`
- 原始文件仍存在的 `parse_status = -1` 会恢复为 `0`
- `clean_status = 1` 或 `-1` 会恢复为 `0`

## 输出目录

- `output/images/<file_id>/`：MinerU 返回的图片资源。
- `output/markdown/<file_id>.md`：当 `SAVE_PARSE_MARKDOWN=true` 时保存的清洗后 Markdown。
- `logs/`：按模块输出的处理日志。

## 关键配置

| 配置项 | 说明 |
| --- | --- |
| `LIGHTRAG_SERVER_URL` | LightRAG 服务地址 |
| `MINERU_SERVER_URL` | MinerU 服务地址 |
| `SCAN_INPUT_PATH` | 原始文件扫描目录 |
| `PARSE_OUTPUT_PATH` | Markdown 等解析结果输出目录 |
| `IMAGE_OUTPUT_PATH` | 解析图片输出目录 |
| `SAVE_PARSE_MARKDOWN` | 是否保存清洗后的 Markdown 文件 |
| `IGNORE_FILE_TYPES` | 扫描时忽略的扩展名，逗号分隔 |
| `PARSE_TASK_BATCH_SIZE` | 每轮解析任务从数据库取出的文件数 |
| `PARSE_REQUEST_TIMEOUT_SECONDS` | MinerU 请求超时时间 |
| `CLEAN_TASK_BATCH_SIZE` | 每轮清洗任务取出的文件数 |
| `ENABLE_LOGGING` | 是否启用日志 |

## 注意事项

- `.env` 可能包含数据库密码，不应提交到 Git。
- 原始文件不会被流程删除。
- MinerU 和 LightRAG 服务需要先启动并保证接口可访问。
- 当前 `docx` / `xlsx` 也会进入解析任务，但实现上统一请求 MinerU `/file_parse`；如果后续需要专门的 Word / Excel 解析策略，应在 `src/parse_requester/dispatcher.py` 或对应 parser 中扩展。
- 仓库中部分历史中文注释和控制台输出存在编码损坏，文档已按当前代码行为重新整理。
