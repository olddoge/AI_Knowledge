import base64
import binascii
import hashlib
import json
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.logging_module import setup_module_logger


RETURN_IMAGES = True

PDF_PARSE_MODULE_NAME = "pdf_parse"
FILE_PARSE_PATH = "/file_parse"
REQUEST_TIMEOUT_SECONDS = 300
PDF_PARSE_LANG_LIST = ("ch", "en")
PDF_PARSE_METHOD = "auto"


def request_pdf_parse(
    files: list[dict[str, str]],
    mineru_server_url: str,
    parse_output_path: str,
    image_output_path: str,
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
                image_output_path,
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
    image_output_path: str,
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
                _get_markdown_content_from_response(
                    response_json,
                    file_info,
                    image_output_path,
                    logger,
                ),
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
    image_output_path: str,
    logger,
) -> str | None:
    result_item = _extract_result_item(response, file_info)
    markdown_content = _get_markdown_from_result_item(result_item)
    if not markdown_content:
        logger.warning(
            "PDF markdown not found: %s",
            json.dumps({"file_name": file_info.get("file_name", "")}, ensure_ascii=False),
        )
        return None
    image_name_map = _save_images_from_result_item(result_item, file_info, image_output_path, logger)
    return _replace_markdown_image_names(markdown_content, image_name_map)


def _extract_markdown_content(response: object, file_info: dict[str, str]) -> str | None:
    return _get_markdown_from_result_item(_extract_result_item(response, file_info))


def _extract_result_item(response: object, file_info: dict[str, str]) -> object:
    if not isinstance(response, dict):
        return None

    results = response.get("results") or response.get("result")
    if isinstance(results, dict):
        return _find_result_item_from_dict(results, file_info)

    if isinstance(results, list):
        return _find_result_item_from_list(results, file_info)

    return None


def _save_images_from_result_item(
    result_item: object,
    file_info: dict[str, str],
    image_output_path: str,
    logger,
) -> dict[str, str]:
    images = _get_images_from_result_item(result_item)
    if not images:
        return {}

    file_id = str(file_info["id"])
    target_dir = Path(image_output_path).expanduser().resolve() / file_id
    target_dir.mkdir(parents=True, exist_ok=True)

    image_name_map: dict[str, str] = {}
    for image_name, image_content in images.items():
        original_name = Path(str(image_name)).name
        if not original_name:
            continue
        if isinstance(image_content, dict):
            image_content = (
                image_content.get("content")
                or image_content.get("base64")
                or image_content.get("data")
                or image_content.get("image_base64")
                or ""
            )

        try:
            image_bytes = _decode_base64_image(str(image_content))
        except ValueError as exc:
            logger.warning(
                "PDF image decode failed: %s",
                json.dumps(
                    {
                        "file_name": file_info.get("file_name", ""),
                        "image_name": original_name,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
            )
            continue

        new_name = _build_hashed_image_name(original_name)
        (target_dir / new_name).write_bytes(image_bytes)
        image_name_map[original_name] = f"{file_id}/{new_name}"

    if image_name_map:
        logger.info(
            "PDF images saved: %s",
            json.dumps(
                {
                    "file_name": file_info.get("file_name", ""),
                    "image_count": len(image_name_map),
                    "output_dir": str(target_dir),
                },
                ensure_ascii=False,
            ),
        )
    return image_name_map


def _get_images_from_result_item(result_item: object) -> dict[str, object]:
    if not isinstance(result_item, dict):
        return {}

    images = result_item.get("images")
    if isinstance(images, dict):
        return images
    if isinstance(images, list):
        normalized_images: dict[str, object] = {}
        for index, item in enumerate(images, start=1):
            if isinstance(item, dict):
                image_name = (
                    item.get("image_name")
                    or item.get("file_name")
                    or item.get("filename")
                    or item.get("name")
                    or f"image_{index}.png"
                )
                image_content = (
                    item.get("content")
                    or item.get("base64")
                    or item.get("data")
                    or item.get("image_base64")
                )
                if image_content:
                    normalized_images[str(image_name)] = image_content
            elif isinstance(item, str):
                normalized_images[f"image_{index}.png"] = item
        return normalized_images
    return {}


def _decode_base64_image(image_content: str) -> bytes:
    content = image_content.strip()
    if "," in content and content.lower().startswith("data:"):
        content = content.split(",", 1)[1]
    try:
        normalized_content = "".join(content.split())
        return base64.b64decode(normalized_content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image content") from exc


def _build_hashed_image_name(image_name: str) -> str:
    path = Path(image_name)
    suffix = path.suffix
    stem = path.stem or image_name
    hashed_stem = hashlib.md5(stem.encode("utf-8")).hexdigest()
    return f"{hashed_stem}{suffix}"


def _replace_markdown_image_names(markdown_content: str, image_name_map: dict[str, str]) -> str:
    replaced_content = markdown_content
    for old_name, new_name in image_name_map.items():
        replaced_content = replaced_content.replace(old_name, new_name)
    return replaced_content


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
