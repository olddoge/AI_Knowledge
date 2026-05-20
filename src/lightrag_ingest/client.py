import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DOCUMENT_TEXTS_PATH = "/documents/texts"
LIGHTRAG_UPLOAD_TIMEOUT_SECONDS = 120


class LightRAGUploadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        texts: list[str],
        file_sources: list[str],
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.texts = texts
        self.file_sources = file_sources
        self.status_code = status_code
        self.response_body = response_body


def upload_document_text(lightrag_server_url: str, text: str, file_source: str) -> None:
    """Compatibility wrapper for uploading a single parsed text."""
    upload_document_texts(lightrag_server_url, [text], [file_source])


def upload_document_texts(lightrag_server_url: str, texts: list[str], file_sources: list[str]) -> None:
    """Post parsed markdown texts to LightRAG /documents/texts."""
    if not texts:
        raise ValueError("texts must not be empty")
    if len(texts) != len(file_sources):
        raise ValueError("texts and file_sources must have the same length")

    upload_url = f"{lightrag_server_url.rstrip('/')}{DOCUMENT_TEXTS_PATH}"
    payload = {"texts": texts, "file_sources": file_sources}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        upload_url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=LIGHTRAG_UPLOAD_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", None)
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise LightRAGUploadError(
            "LightRAG texts upload returned an HTTP error.",
            texts=texts,
            file_sources=file_sources,
            status_code=exc.code,
            response_body=response_body,
        ) from exc

    response_json = _try_load_json(response_text)
    if isinstance(response_json, dict) and response_json.get("status") in {"failure", "partial_success"}:
        raise LightRAGUploadError(
            "LightRAG texts upload returned a failure status.",
            texts=texts,
            file_sources=file_sources,
            status_code=status_code,
            response_body=response_text,
        )


def _try_load_json(response_text: str) -> object:
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return response_text
