from logging import Logger
from pathlib import Path

from src.lightrag_ingest.client import upload_document


def upload_file_to_lightrag(
    lightrag_server_url: str,
    file_path: str | Path,
    logger: Logger | None = None,
) -> bool:
    """上传单个 Markdown 文件到 LightRAG，失败时记录日志并返回 False。"""
    path = Path(file_path)
    try:
        upload_document(lightrag_server_url, path)
        if logger:
            logger.info("文件已上传 LightRAG：%s", path)
        return True
    except Exception as exc:
        # LightRAG 上传失败不影响清洗状态，但必须记录，便于后续排查和补传。
        if logger:
            logger.exception("文件上传 LightRAG 失败：%s，错误：%s", path, exc)
        return False


def run_lightrag_upload_task() -> dict[str, object]:
    """LightRAG 独立批量上传任务占位入口，单文件上传动作已在本模块实现。"""
    return {
        "task": "lightrag_upload",
        "status": "placeholder",
        "message": "上传 LightRAG 批量任务占位；当前由清洗完成后调用单文件上传动作。",
    }
