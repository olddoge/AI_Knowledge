from logging import Logger
from pathlib import Path

from src.lightrag_ingest.client import LightRAGUploadError, upload_document_text


def upload_text_to_lightrag(
    lightrag_server_url: str,
    text: str,
    file_source: str,
    logger: Logger | None = None,
) -> bool:
    """Upload parsed markdown text to LightRAG; log request content only on failure."""
    try:
        upload_document_text(lightrag_server_url, text, file_source)
        if logger:
            logger.info("LightRAG text upload succeeded: file_source=%s", file_source)
        return True
    except LightRAGUploadError as exc:
        if logger:
            logger.exception(
                "LightRAG text upload failed: file_source=%s, status_code=%s, "
                "response=%s, upload_text=%s",
                exc.file_source,
                exc.status_code,
                exc.response_body,
                exc.text,
            )
        return False
    except Exception as exc:
        if logger:
            logger.exception(
                "LightRAG text upload failed: file_source=%s, error=%s, upload_text=%s",
                file_source,
                exc,
                text,
            )
        return False


def upload_file_to_lightrag(
    lightrag_server_url: str,
    file_path: str | Path,
    file_source: str | None = None,
    logger: Logger | None = None,
) -> bool:
    """Compatibility wrapper for legacy clean tasks; upload file content as text."""
    path = Path(file_path)
    text = path.read_text(encoding="utf-8")
    return upload_text_to_lightrag(lightrag_server_url, text, file_source or str(path), logger)


def run_lightrag_upload_task() -> dict[str, object]:
    """Placeholder for a future standalone LightRAG retry/upload queue."""
    return {
        "task": "lightrag_upload",
        "status": "placeholder",
        "message": "LightRAG upload is currently triggered after parsing and cleaning.",
    }
