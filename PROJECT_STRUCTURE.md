# 工程结构说明

本项目用于企业内部文件入库流程，将原始文件解析、清洗并发送到 AI 知识库 / RAG / LightRAG 系统。

```text
ai_knowledge_rag/
|-- AGENTS.md
|   `-- 项目 AI Coding Agent 的角色、原则和协作规范说明。
|-- PROJECT_STRUCTURE.md
|   `-- 当前工程目录结构与各模块功能说明。
|-- .env
|   `-- 环境配置文件，用于保存数据库、解析服务、LightRAG 接口等运行配置。
|-- main.py
|   `-- 项目主入口文件，后续通过调用该文件串联完整入库任务流程。
`-- src/
    |-- __init__.py
    |   `-- Python 包初始化文件。
    |-- file_scanner/
    |   |-- __init__.py
    |   `-- 扫描文件模块：负责扫描指定目录，识别待处理文件，并保留原始文件路径与基础元数据。
    |-- parse_requester/
    |   |-- __init__.py
    |   `-- 请求解析模块：负责根据文件类型请求解析服务，将 PDF、Word、Excel 等文件转换为 Markdown / 文本 / 图片资源。
    |-- data_cleaner/
    |   |-- __init__.py
    |   `-- 清洗数据模块：负责清理 HTML 标签、乱码、重复内容和无效文本，生成标准化入库文本。
    |-- lightrag_ingest/
    |   |-- __init__.py
    |   `-- LightRAG 入库模块：负责将清洗后的内容和元数据发送到 LightRAG / RAG 知识库系统。
    `-- logging_module/
        |-- __init__.py
        `-- 日志模块：负责记录任务状态、错误日志、重试信息和关键处理轨迹。
```

