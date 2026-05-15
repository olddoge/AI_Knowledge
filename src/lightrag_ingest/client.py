import mimetypes
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


DOCUMENT_UPLOAD_PATH = "/documents/upload"
LIGHTRAG_UPLOAD_TIMEOUT_SECONDS = 120


def upload_document(lightrag_server_url: str, file_path: str | Path) -> None:
    """将清洗后的 Markdown 文件上传到 LightRAG /documents/upload 接口。"""
    upload_url = f"{lightrag_server_url.rstrip('/')}{DOCUMENT_UPLOAD_PATH}"
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"待上传文件不存在：{path}")

    boundary = f"----ai-knowledge-rag-{uuid.uuid4().hex}"
    body = _build_single_file_multipart_body(boundary, "file", path)
    request = Request(
        upload_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )

    # 当前流程只负责投递文件；接口返回内容不参与本地状态判断。
    with urlopen(request, timeout=LIGHTRAG_UPLOAD_TIMEOUT_SECONDS) as response:
        response.read()


def _build_single_file_multipart_body(boundary: str, field_name: str, file_path: Path) -> bytes:
    body = bytearray()
    mime_type = mimetypes.guess_type(file_path.name)[0] or "text/markdown"

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body)
