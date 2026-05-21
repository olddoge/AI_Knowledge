# 工程结构说明

本项目用于企业内部文件入库流程：扫描原始文件，记录文件元数据，调用解析服务生成 Markdown / 文本 / 图片资源，清洗内容后上传到 AI 知识库 / RAG / LightRAG 系统。

```text
ai_knowledge_rag/
|-- AGENTS.md
|   `-- 项目 AI Coding Agent 的角色、原则和协作规范。
|-- README.md
|   `-- 项目说明、配置方式、运行入口和处理流程。
|-- PROJECT_STRUCTURE.md
|   `-- 当前工程目录结构与模块职责说明。
|-- .env.example
|   `-- 环境变量模板，包含数据库、MinerU、LightRAG、扫描目录等配置项。
|-- .env
|   `-- 本地运行配置文件，通常包含敏感信息，不应提交到 Git。
|-- .gitignore
|   `-- Git 忽略规则。
|-- main.py
|   `-- 主处理入口：启动解析任务、清洗任务和 LightRAG 上传相关流程。
|-- scan_main.py
|   `-- 扫描入口：扫描原始文件目录，将文件元数据写入 MySQL。
|-- original/
|   `-- 原始文件目录，可作为 SCAN_INPUT_PATH 使用。
|-- output/
|   |-- .gitkeep
|   |-- images/
|   |   `-- MinerU 返回图片的默认保存目录，按 file_id 分目录存放。
|   `-- markdown/
|       `-- SAVE_PARSE_MARKDOWN=true 时保存清洗后的 Markdown。
|-- logs/
|   `-- 日志输出目录。
|-- sql/
|   |-- rag_files_table.sql
|   |   `-- 文件扫描、解析、清洗状态表结构。
|   `-- rag_image_table.sql
|       `-- Markdown 图片引用记录表结构。
`-- src/
    |-- __init__.py
    |-- config.py
    |   `-- .env 读取和配置值校验。
    |-- database.py
    |   `-- MySQL 连接配置和连接创建。
    |-- pipeline.py
    |   `-- 主流程编排，负责并发启动各类后台任务。
    |-- parse_output.py
    |   `-- 解析结果相关的辅助模块。
    |-- file_scanner/
    |   |-- __init__.py
    |   `-- scanner.py
    |       `-- 递归扫描文件、过滤类型、计算哈希、写入 rag_files。
    |-- parse_requester/
    |   |-- __init__.py
    |   |-- dispatcher.py
    |   |-- docx_parser.py
    |   |-- parse_task.py
    |   |-- pdf_parser.py
    |   `-- xlsx_parser.py
    |       `-- 解析任务和各文件类型解析器；当前主链路通过 MinerU /file_parse 获取 Markdown。
    |-- data_cleaner/
    |   |-- __init__.py
    |   |-- clean_task.py
    |   `-- markdown_cleaner.py
    |       `-- Markdown 清洗任务与清洗规则。
    |-- lightrag_ingest/
    |   |-- __init__.py
    |   |-- client.py
    |   `-- upload_task.py
    |       `-- LightRAG /documents/texts 上传客户端与兼容上传任务。
    |-- logging_module/
    |   |-- __init__.py
    |   `-- logger.py
    |       `-- 日志初始化与模块日志配置。
    `-- repositories/
        |-- __init__.py
        |-- rag_file_repository.py
        `-- rag_image_repository.py
            `-- 数据库表读写封装，集中管理任务状态更新。
```

## 模块关系

```text
scan_main.py
  -> src.file_scanner.scanner
  -> src.repositories.rag_file_repository
  -> MySQL rag_files

main.py
  -> src.pipeline
  -> src.parse_requester.parse_task
  -> src.data_cleaner.markdown_cleaner
  -> src.lightrag_ingest.client
  -> LightRAG /documents/texts
```

## 处理状态

- `parse_status = 0`：未解析
- `parse_status = 1`：正在解析
- `parse_status = 2`：解析完成
- `parse_status = -1`：解析失败
- `clean_status = 0`：未清洗
- `clean_status = 1`：正在清洗
- `clean_status = 2`：已清洗
- `clean_status = -1`：清洗失败

## 维护说明

- 文件扫描阶段只记录元数据和状态，不修改或删除原始文件。
- 解析、清洗、上传失败时应更新状态，避免静默吞错。
- 新增文件类型时，优先补齐扫描支持、解析器、清洗规则和状态流转。
- 敏感操作，尤其是删除原始文件或清理输出目录，执行前必须确认。
