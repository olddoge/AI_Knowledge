# 工程结构说明

本项目面向企业内部知识库入库流程：先从 `nas_files` 扫描可处理文件，登记到 `rag_files`，再解析为 Markdown / 图片资源，清洗后上传到 LightRAG / RAG 系统。工程设计重点是批处理、状态可追踪、失败可重试，以及不破坏原始文件。

```text
ai_knowledge_rag/
|-- AGENTS.md
|   `-- 项目 AI Coding Agent 的角色、原则和协作规范。
|-- README.md
|   `-- 项目说明、环境配置、运行入口和处理流程。
|-- PROJECT_STRUCTURE.md
|   `-- 当前工程目录结构与模块职责说明。
|-- .env.example
|   `-- 环境变量模板，包含数据库、NAS SSH、MinerU、LightRAG 等配置项。
|-- .env
|   `-- 本地运行配置文件，通常包含敏感信息，不应提交到 Git。
|-- .gitignore
|   `-- Git 忽略规则。
|-- main.py
|   `-- 兼容流水线入口，同时启动解析、清洗和 LightRAG 上传相关流程。
|-- scan_main.py
|   `-- 独立扫描入口：读取 nas_files.status=3，访问 NAS 元数据并写入 rag_files。
|-- parse_main.py
|   `-- 独立解析入口：领取 rag_files 待解析记录，调用 MinerU 并保存 Markdown / 图片。
|-- clean_main.py
|   `-- 独立清洗入口：多进程领取已解析 Markdown，清洗后上传 LightRAG。
|-- markdown/
|   `-- 独立解析入口默认保存 Markdown 的目录，通常按 file_hash 命名。
|-- images/
|   `-- MinerU 返回图片的默认保存目录，按 file_id 建子目录。
|-- logs/
|   `-- 日志输出目录。
|-- sql/
|   `-- 数据库建表 SQL。
`-- src/
    |-- __init__.py
    |-- config.py
    |   `-- .env 读取、必填配置、布尔值和整数配置解析。
    |-- database.py
    |   `-- MySQL 连接配置和连接创建。
    |-- pipeline.py
    |   `-- 兼容流水线编排，供 main.py 使用。
    |-- file_scanner/
    |   |-- __init__.py
    |   `-- nas_scanner.py
    |       `-- NAS 扫描服务：读取 nas_files、SSH/SFTP 获取文件信息、写入 rag_files。
    |-- parse_requester/
    |   |-- __init__.py
    |   |-- mineru_parser.py
    |   |   `-- 独立 MinerU 解析 worker：下载 NAS 文件、请求 /file_parse、保存 Markdown 和图片。
    |   `-- parse_task.py
    |       `-- 兼容流水线解析任务：解析后立即清洗并上传 LightRAG。
    |-- data_cleaner/
    |   |-- __init__.py
    |   |-- clean_task.py
    |   |   `-- 清洗任务 worker：原子领取任务、多进程执行、上传 LightRAG、更新状态。
    |   `-- markdown_cleaner.py
    |       `-- Markdown 清洗规则：HTML、乱码字符、目录锚点、页码行、多余空行等。
    |-- lightrag_ingest/
    |   |-- __init__.py
    |   |-- client.py
    |   |   `-- LightRAG /documents/texts 上传客户端。
    |   `-- upload_task.py
    |       `-- 兼容流水线中的 LightRAG 上传占位任务。
    |-- logging_module/
    |   |-- __init__.py
    |   `-- logger.py
    |       `-- 日志初始化与模块日志配置。
    `-- repositories/
        |-- __init__.py
        |-- rag_file_repository.py
        |   `-- rag_files 表读写封装，集中管理任务领取和状态更新。
        `-- rag_image_repository.py
            `-- 图片记录仓储预留模块；当前主流程未依赖对应表。
```

## 主要模块关系

```text
scan_main.py
  -> src.file_scanner.nas_scanner.NasFileScanner
  -> nas_files(status = 3)
  -> NAS SSH/SFTP
  -> MySQL rag_files

parse_main.py
  -> src.parse_requester.mineru_parser.MineruParseWorker
  -> rag_files(parse_status = 0)
  -> NAS SFTP 临时下载
  -> MinerU /file_parse
  -> markdown/
  -> images/<file_id>/
  -> rag_files(parse_status, parse_path)

clean_main.py
  -> src.data_cleaner.clean_task.CleanTaskWorker
  -> rag_files(parse_status = 2, clean_status = 0)
  -> src.data_cleaner.markdown_cleaner
  -> src.lightrag_ingest.client
  -> LightRAG /documents/texts
  -> rag_files(clean_status)

main.py
  -> src.pipeline
  -> src.parse_requester.parse_task
  -> src.data_cleaner.clean_task
  -> src.lightrag_ingest.upload_task
```

## 数据表

### rag_files

当前核心任务表，建表 SQL 位于 `sql/rag_files_table.sql`。

主要字段：

- `id`：任务主键。
- `file_name`：原始文件名。
- `file_uid`：基于文件 hash 和原始路径生成的稳定唯一值。
- `file_ext`：扩展名，不带点号。
- `file_size`：文件大小，单位字节。
- `file_hash`：SHA-256 文件 hash。
- `original_path`：原始文件路径，保留来源追踪。
- `parse_path`：解析后 Markdown 路径；`txt` 直通时可能指向原始路径。
- `parse_status`：解析状态。
- `clean_status`：清洗状态。
- `created_at` / `updated_at`：Unix 时间戳。

### nas_files

扫描入口依赖外部已有的 `nas_files` 表，至少需要包含：

- `id`
- `full_path`
- `status`

当前扫描条件为 `status = 3`。

## 状态流转

`parse_status`：

- `0`：未解析
- `1`：正在解析
- `2`：解析完成
- `-1`：解析失败

`clean_status`：

- `0`：未清洗
- `1`：正在清洗
- `2`：已清洗并上传
- `-1`：清洗失败

典型流转：

```text
扫描登记:
  parse_status=0, clean_status=0

解析成功:
  parse_status=2, parse_path=<markdown path>

清洗并上传成功:
  clean_status=2

任一阶段失败:
  parse_status=-1 或 clean_status=-1
```

## 维护说明

- 扫描阶段只记录元数据和任务状态，不修改、不删除原始文件。
- 解析和清洗失败时必须更新状态并记录日志，避免静默吞掉异常。
- 新增文件类型时，需要同时考虑扫描支持、解析策略、清洗规则和状态流转。
- 多进程/多实例领取任务依赖 MySQL 8 `FOR UPDATE SKIP LOCKED`，数据库版本不应低于 MySQL 8。
- 敏感操作，尤其是删除原始文件或批量清理输出目录，执行前必须确认。
