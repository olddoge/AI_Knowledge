from logging import Logger
from pathlib import Path

from src.lightrag_ingest.client import LightRAGUploadError, upload_document_texts


def upload_text_to_lightrag(
    lightrag_server_url: str,
    text: str,
    file_source: str,
    logger: Logger | None = None,
) -> bool:
    """Upload parsed markdown text to LightRAG; log request content only on failure."""
    return upload_texts_to_lightrag(lightrag_server_url, [text], [file_source], logger)


def upload_texts_to_lightrag(
    lightrag_server_url: str,
    texts: list[str],
    file_sources: list[str],
    logger: Logger | None = None,
) -> bool:
    """Upload parsed markdown texts to LightRAG; file_sources and texts are positional pairs."""
    try:
        upload_document_texts(lightrag_server_url, texts, file_sources)
        if logger:
            logger.info("LightRAG texts upload succeeded: file_sources=%s", file_sources)
        return True
    except LightRAGUploadError as exc:
        if logger:
            logger.exception(
                "LightRAG texts upload failed: file_sources=%s, status_code=%s, "
                "response=%s, upload_texts=%s",
                exc.file_sources,
                exc.status_code,
                exc.response_body,
                exc.texts,
            )
        return False
    except Exception as exc:
        if logger:
            logger.exception(
                "LightRAG texts upload failed: file_sources=%s, error=%s, upload_texts=%s",
                file_sources,
                exc,
                texts,
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
