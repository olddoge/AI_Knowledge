import json
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.logging_module import setup_module_logger


RETURN_IMAGES = False

PDF_PARSE_MODULE_NAME = "pdf_parse"
FILE_PARSE_PATH = "/file_parse"
REQUEST_TIMEOUT_SECONDS = 300
PDF_PARSE_LANG_LIST = ("ch", "en")
PDF_PARSE_METHOD = "auto"


def request_pdf_parse(
    files: list[dict[str, str]],
    mineru_server_url: str,
    parse_output_path: str,
    enable_logging: bool = True,
    parse_request_concurrency: int = 3,
    parse_request_batch_size: int = 2,
) -> list[dict[str, object]]:
    """Request MinerU /file_parse and return extracted markdown text in memory."""
    logger = setup_module_logger(PDF_PARSE_MODULE_NAME, enable_logging=enable_logging)

    if not files:
        logger.info("No files pending parse; skip MinerU request.")
        return []

    max_workers = max(1, parse_request_concurrency)
    batch_size = max(1, parse_request_batch_size)
    file_batches = _chunk_files(files, batch_size)
    logger.info("Parse request concurrency=%s, batch_size=%s", max_workers, batch_size)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(
                _request_pdf_parse_batch,
                file_batch,
                mineru_server_url,
                parse_output_path,
                logger,
                index + 1,
                len(file_batches),
            ): index
            for index, file_batch in enumerate(file_batches)
        }

        indexed_results: list[tuple[int, list[dict[str, object]]]] = []
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            indexed_results.append((index, future.result()))

    parse_results: list[dict[str, object]] = []
    for _, batch_results in sorted(indexed_results, key=lambda item: item[0]):
        parse_results.extend(batch_results)

    return parse_results


def _request_pdf_parse_batch(
    files: list[dict[str, str]],
    mineru_server_url: str,
    parse_output_path: str,
    logger,
    batch_index: int,
    batch_count: int,
) -> list[dict[str, object]]:
    """Batch request /file_parse; logs only file names and response success summary."""
    print(f"正在解析文件批次 {batch_index}/{batch_count}，文件数：{len(files)}")
    endpoint = _build_file_parse_url(mineru_server_url)
    request_fields = _build_parse_fields()
    request_files = _build_request_files(files)
    file_names = _get_file_names(files)

    logger.info(
        "PDF parse request files: %s",
        json.dumps(
            {
                "batch_index": batch_index,
                "batch_count": batch_count,
                "file_names": file_names,
            },
            ensure_ascii=False,
        ),
    )

    try:
        status_code, response_text = _post_multipart(
            endpoint,
            fields=request_fields,
            files=request_files,
        )
        response_json = _try_load_json(response_text)
        response_success = _is_response_success(status_code)
        logger.info(
            "PDF parse response result: %s",
            json.dumps(
                {
                    "batch_index": batch_index,
                    "status_code": status_code,
                    "success": response_success,
                },
                ensure_ascii=False,
            ),
        )

        return [
            _build_parse_result(
                file_info,
                "success",
                status_code,
                response_success,
                _get_markdown_content_from_response(response_json, file_info, logger),
            )
            for file_info in files
        ]
    except (OSError, HTTPError, URLError, ValueError) as exc:
        logger.exception("PDF parse request failed: file_names=%s, error=%s", file_names, exc)
        return [_build_parse_result(file_info, "failed", None, False, None) for file_info in files]


def _chunk_files(files: list[dict[str, str]], batch_size: int) -> list[list[dict[str, str]]]:
    return [files[index : index + batch_size] for index in range(0, len(files), batch_size)]


def _get_file_names(files: list[dict[str, str]]) -> list[str]:
    return [str(file_info.get("file_name") or Path(file_info["absolute_path"]).name) for file_info in files]


def _is_response_success(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def _get_markdown_content_from_response(
    response: object,
    file_info: dict[str, str],
    logger,
) -> str | None:
    markdown_content = _extract_markdown_content(response, file_info)
    if not markdown_content:
        logger.warning(
            "PDF markdown not found: %s",
            json.dumps({"file_name": file_info.get("file_name", "")}, ensure_ascii=False),
        )
        return None
    return markdown_content


def _extract_markdown_content(response: object, file_info: dict[str, str]) -> str | None:
    if not isinstance(response, dict):
        return None

    results = response.get("results")
    if isinstance(results, dict):
        result_item = _find_result_item_from_dict(results, file_info)
        return _get_markdown_from_result_item(result_item)

    if isinstance(results, list):
        result_item = _find_result_item_from_list(results, file_info)
        return _get_markdown_from_result_item(result_item)

    return None


def _find_result_item_from_dict(results: dict[str, object], file_info: dict[str, str]) -> object:
    file_path = Path(file_info["absolute_path"])
    candidate_keys = (
        file_info.get("file_uid", ""),
        file_info["file_name"],
        file_path.name,
        file_path.stem,
        str(file_path),
    )

    for key in candidate_keys:
        if key in results:
            return results[key]

    if len(results) == 1:
        return next(iter(results.values()))

    return None


def _find_result_item_from_list(results: list[object], file_info: dict[str, str]) -> object:
    file_name = file_info["file_name"]
    file_uid = file_info.get("file_uid", "")
    file_stem = Path(file_name).stem

    for item in results:
        if not isinstance(item, dict):
            continue

        item_name = str(
            item.get("file_name")
            or item.get("filename")
            or item.get("name")
            or item.get("file_uid")
            or item.get("uid")
            or item.get("original_file_name")
            or ""
        )
        if item_name in {file_uid, file_name, file_stem}:
            return item

    if len(results) == 1:
        return results[0]

    return None


def _get_markdown_from_result_item(result_item: object) -> str | None:
    if isinstance(result_item, str):
        return result_item

    if not isinstance(result_item, dict):
        return None

    for key in ("md_content", "markdown", "markdown_content", "md", "content"):
        value = result_item.get(key)
        if isinstance(value, str) and value.strip():
            return value

    return None


def _build_parse_fields() -> list[tuple[str, str]]:
    fields = [
        ("backend", "hybrid-auto-engine"),
        ("parse_method", PDF_PARSE_METHOD),
        ("formula_enable", "true"),
        ("table_enable", "true"),
        ("image_analysis", "true"),
        ("return_md", "true"),
        ("return_middle_json", "false"),
        ("return_model_output", "false"),
        ("return_content_list", "false"),
        ("return_images", str(RETURN_IMAGES).lower()),
        ("response_format_zip", "false"),
        ("return_original_file", "false"),
        ("start_page_id", "0"),
        ("end_page_id", "99999"),
    ]

    for lang in PDF_PARSE_LANG_LIST:
        fields.append(("lang_list", lang))

    return fields


def _build_request_files(files: list[dict[str, str]]) -> list[tuple[str, Path]]:
    return [("files", Path(file_info["absolute_path"])) for file_info in files]


def _post_multipart(
    url: str,
    fields: list[tuple[str, str]],
    files: list[tuple[str, Path]],
) -> tuple[int, str]:
    boundary = f"----ai-knowledge-rag-{uuid.uuid4().hex}"
    body = _build_multipart_body(boundary, fields, files)
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )

    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        response_text = response.read().decode("utf-8", errors="replace")
        return response.status, response_text


def _build_multipart_body(
    boundary: str,
    fields: list[tuple[str, str]],
    files: list[tuple[str, Path]],
) -> bytes:
    body = bytearray()

    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{value}\r\n".encode("utf-8"))

    for field_name, file_path in files:
        if not file_path.exists():
            raise FileNotFoundError(f"PDF file does not exist: {file_path}")

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
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


def _build_file_parse_url(mineru_server_url: str) -> str:
    return f"{mineru_server_url.rstrip('/')}{FILE_PARSE_PATH}"


def _try_load_json(response_text: str) -> object:
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return response_text


def _build_parse_result(
    file_info: dict[str, str],
    parse_status: str,
    status_code: int | None,
    response_success: bool,
    markdown_content: str | None,
) -> dict[str, object]:
    return {
        "file_type": file_info.get("file_ext", "pdf"),
        "id": file_info.get("id"),
        "file_uid": file_info.get("file_uid"),
        "file_name": file_info["file_name"],
        "absolute_path": file_info["absolute_path"],
        "parse_status": parse_status,
        "status_code": status_code,
        "response_success": response_success,
        "markdown_content": markdown_content,
    }
