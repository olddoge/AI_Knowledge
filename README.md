# AI Knowledge RAG

企业内部文件入库处理项目，用于将 NAS / 数据库中登记的原始文件扫描到任务表，调用 MinerU 解析为 Markdown 和图片资源，再经过清洗后上传到 LightRAG / RAG 知识库。

项目优先级：

- 稳定、可追踪、可恢复
- 支持批处理和失败重试
- 保留原始文件路径、文件 hash 和处理状态
- 入库前必须经过清洗流程
- 不修改、不删除原始文件

## 当前能力

- 从 `nas_files` 表读取 `status = 3` 的文件记录。
- 通过 SSH/SFTP 只读访问 NAS，获取文件大小、文件名和 SHA-256 hash。
- 按 `original_path` 和 `file_hash` 去重，避免重复登记同一文件。
- 支持扫描文件类型：`pdf`、`docx`、`xlsx`、`pptx`、`txt`、`md`、`markdown`。
- 跳过 Office 临时文件，例如 `~$xxx.docx`。
- 将可处理文件写入 MySQL `rag_files` 表，并维护解析、清洗状态。
- 独立 MinerU 解析入口会下载 NAS 文件到本地临时目录，调用 MinerU `/file_parse`，保存 Markdown 到 `markdown/`，保存图片到 `images/<file_id>/`。
- `txt` 文件在独立解析入口中会直接通过，`parse_path` 指向原始文件路径。
- 独立清洗入口会领取已解析文件，清洗 Markdown，并上传到 LightRAG `/documents/texts`。
- 解析、清洗任务使用 MySQL 8 `FOR UPDATE SKIP LOCKED` 领取任务，支持多进程/多实例并发时避免重复领取。
- 日志输出到 `logs/`。

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
|-- parse_main.py
|-- clean_main.py
|-- sql/
|-- src/
|-- markdown/
|-- images/
`-- logs/
```

更完整的模块说明见 [PROJECT_STRUCTURE.md](./PROJECT_STRUCTURE.md)。

## 环境要求

- Python 3.13
- MySQL 8
- 可通过 SSH/SFTP 访问的 NAS 或远程文件主机
- MinerU 解析服务
- LightRAG 服务

当前仓库未提供 `requirements.txt`，至少需要安装：

```bash
pip install pymysql
pip install paramiko
```

## 初始化配置

复制环境变量模板：

```bash
copy .env.example .env
```

按实际环境修改 `.env`。当前代码会用到的主要配置如下：

```env
MINERU_SERVER_URL=http://localhost:8000
LIGHTRAG_SERVER_URL=http://localhost:9621

DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=ai_knowledge_rag

SSH_HOST=127.0.0.1
SSH_PORT=22
SSH_USER=
SSH_PASSWORD=
SSH_TIMEOUT=10

SCAN_BATCH_SIZE=1000

MARKDOWN_OUTPUT_PATH=./markdown
MARKDOWN_IMAGE_PATH=./images
PARSE_TASK_BATCH_SIZE=5
PARSE_REQUEST_CONCURRENCY=2
PARSE_REQUEST_BATCH_SIZE=2
PARSE_REQUEST_TIMEOUT_SECONDS=600

CLEAN_TASK_BATCH_SIZE=5
CLEAN_TASK_WORKER_PROCESSES=2
CLEAN_TASK_STALE_SECONDS=3600
KEEP_MARKDOWN_FILE=true

ENABLE_LOGGING=true
```

`main.py` 兼容流水线还会读取这些旧配置：

```env
PARSE_OUTPUT_PATH=.
IMAGE_OUTPUT_PATH=./images
SAVE_PARSE_MARKDOWN=false
PARSE_TASK_POLL_INTERVAL_SECONDS=10
CLEAN_TASK_POLL_INTERVAL_SECONDS=10
```

## 初始化数据库

```bash
mysql -h 127.0.0.1 -u root -p ai_knowledge_rag < sql/rag_files_table.sql
```

`rag_files` 是当前核心任务表。代码中还保留了 `rag_image_repository.py`，但当前仓库没有对应建表 SQL，主流程也没有依赖该表。

## 使用方式

### 1. 扫描原始文件

```bash
python scan_main.py
```

扫描入口会：

- 分批读取 `nas_files.status = 3` 的记录。
- 兼容 `/volume1/...`、`/share/...`、`share/...` 等 NAS 路径视角。
- 过滤不支持的扩展名和 Office 临时文件。
- 读取远程文件元数据并计算 SHA-256。
- 写入 `rag_files`，初始 `parse_status = 0`、`clean_status = 0`。

### 2. 独立执行 MinerU 解析

```bash
python parse_main.py
```

解析入口会持续领取 `parse_status = 0` 的记录，处理到没有可领取任务后自动退出。

- `pdf`、`docx`、`xlsx`、`pptx` 会通过 MinerU `/file_parse` 解析。
- Markdown 保存到 `MARKDOWN_OUTPUT_PATH`，文件名默认为 `<file_hash>.md`。
- MinerU 返回的图片保存到 `MARKDOWN_IMAGE_PATH/<file_id>/`。
- `txt` 文件不调用 MinerU，直接标记解析完成。
- 不支持的文件类型会标记为解析失败。

### 3. 独立执行清洗和入库

```bash
python clean_main.py
```

清洗入口会启动 `CLEAN_TASK_WORKER_PROCESSES` 个进程，并发领取满足以下条件的记录：

- `parse_status = 2`
- `clean_status = 0`
- `parse_path <> ''`

每条记录会读取 `parse_path` 指向的 Markdown，执行清洗，然后上传到 LightRAG `/documents/texts`。上传成功后将 `clean_status` 更新为 `2`；失败则更新为 `-1`，便于后续重试。

### 4. 兼容流水线入口

```bash
python main.py
```

`main.py` 会同时启动解析任务、清洗任务和 LightRAG 上传占位任务。当前更推荐使用 `scan_main.py`、`parse_main.py`、`clean_main.py` 分阶段运行，便于批处理调度、日志定位和失败恢复。

## 状态说明

`rag_files.parse_status`：

- `0`：未解析
- `1`：正在解析
- `2`：解析完成
- `-1`：解析失败

`rag_files.clean_status`：

- `0`：未清洗
- `1`：正在清洗
- `2`：已清洗并上传
- `-1`：清洗失败

恢复策略：

- 独立解析入口使用原子领取任务；已领取后进程异常退出的任务需要结合状态和日志排查后重试。
- 独立清洗入口启动时会恢复超时停留在 `clean_status = 1` 的记录，并恢复 `clean_status = -1` 的失败记录为 `0`。
- 旧兼容流水线会在启动时恢复部分解析中、清洗中或失败状态。

## 输出目录

- `markdown/`：独立解析入口保存的 Markdown 文件。
- `images/<file_id>/`：MinerU 返回图片资源的保存目录。
- `logs/`：按模块输出的运行日志。

## 注意事项

- `.env` 可能包含数据库、SSH 等敏感信息，不应提交到 Git。
- 流程只读访问原始文件，不会删除 NAS 上的原始文件。
- 删除输出文件、清理目录等敏感操作执行前需要人工确认。
- MinerU 和 LightRAG 服务需要先启动，并保证网络可访问。
- 当前代码和部分历史注释存在中文编码损坏问题；文档已按当前代码行为重新整理。
