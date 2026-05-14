import json
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.logging_module import setup_module_logger


# 是否返回解析图片，放在文件顶端便于后续统一调整。
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
    """请求 MinerU /file_parse 接口解析扫描出的 PDF 文件。"""
    logger = setup_module_logger(PDF_PARSE_MODULE_NAME, enable_logging=enable_logging)

    if not files:
        logger.info("没有扫描到 PDF 文件，跳过 PDF 请求解析。")
        return []

    max_workers = max(1, parse_request_concurrency)
    batch_size = max(1, parse_request_batch_size)
    file_batches = _chunk_files(files, batch_size)
    logger.info("PDF 解析并发请求数量：%s，单次请求文件数量：%s", max_workers, batch_size)

    # PDF 解析接口需要等待返回，按批次并发处理，兼顾吞吐量和服务端压力。
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

    # 并发完成顺序不稳定，这里按扫描顺序还原，方便后续追踪和对账。
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
    """批量请求 /file_parse，一个请求中可同时携带多个 PDF 文件。"""
    print(f"正在解析 PDF 批次 {batch_index}/{batch_count}，文件数：{len(files)}")
    endpoint = _build_file_parse_url(mineru_server_url)
    request_fields = _build_parse_fields()
    request_files = _build_request_files(files)
    request_summary = {
        "endpoint": endpoint,
        "fields": request_fields,
        "files": files,
        "batch_index": batch_index,
        "batch_count": batch_count,
    }

    logger.info("PDF 解析请求信息：%s", json.dumps(request_summary, ensure_ascii=False))

    try:
        status_code, response_text = _post_multipart(
            endpoint,
            fields=request_fields,
            files=request_files,
        )
        response_json = _try_load_json(response_text)
        logger.info(
            "PDF 解析响应：%s",
            json.dumps(
                {
                    "batch_index": batch_index,
                    "status_code": status_code,
                    "response": response_json,
                },
                ensure_ascii=False,
            ),
        )

        return [
            _build_parse_result(
                file_info,
                "success",
                status_code,
                response_json,
                _save_markdown_from_response(response_json, file_info, parse_output_path, logger),
            )
            for file_info in files
        ]
    except (OSError, HTTPError, URLError, ValueError) as exc:
        logger.exception("PDF 解析请求失败：%s", exc)
        return [_build_parse_result(file_info, "failed", None, str(exc), None) for file_info in files]


def _chunk_files(files: list[dict[str, str]], batch_size: int) -> list[list[dict[str, str]]]:
    """按配置的批量大小切分 PDF 文件列表。"""
    return [files[index : index + batch_size] for index in range(0, len(files), batch_size)]


def _save_markdown_from_response(
    response: object,
    file_info: dict[str, str],
    parse_output_path: str,
    logger,
) -> str | None:
    """从 response.results 中提取 markdown，并按原文件名保存为 .md。"""
    markdown_content = _extract_markdown_content(response, file_info)
    if not markdown_content:
        logger.warning("未找到 PDF markdown 内容：%s", json.dumps(file_info, ensure_ascii=False))
        return None

    output_dir = Path(parse_output_path).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{Path(file_info['file_name']).stem}.md"
    output_file.write_text(markdown_content, encoding="utf-8")
    logger.info("PDF markdown 已保存：%s", output_file)
    return str(output_file)


def _extract_markdown_content(response: object, file_info: dict[str, str]) -> str | None:
    """兼容常见 results 结构，提取当前文件对应的 markdown 文本。"""
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
    """按原文件名、文件 stem、绝对路径等常见 key 查找解析结果。"""
    file_path = Path(file_info["absolute_path"])
    candidate_keys = (
        file_info["file_name"],
        file_path.name,
        file_path.stem,
        str(file_path),
    )

    for key in candidate_keys:
        if key in results:
            return results[key]

    # 如果只有一个文件结果，接口可能不会使用原文件名作为 key，兜底取唯一结果。
    if len(results) == 1:
        return next(iter(results.values()))

    return None


def _find_result_item_from_list(results: list[object], file_info: dict[str, str]) -> object:
    """从列表结构中按文件名字段匹配当前文件的解析结果。"""
    file_name = file_info["file_name"]
    file_stem = Path(file_name).stem

    for item in results:
        if not isinstance(item, dict):
            continue

        item_name = str(
            item.get("file_name")
            or item.get("filename")
            or item.get("name")
            or item.get("original_file_name")
            or ""
        )
        if item_name in {file_name, file_stem}:
            return item

    if len(results) == 1:
        return results[0]

    return None


def _get_markdown_from_result_item(result_item: object) -> str | None:
    """从单个解析结果中提取 markdown 字段。"""
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
    """构造 /file_parse 接口需要的 multipart 表单参数。"""
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

    # FastAPI 对数组字段支持重复同名字段，这里按 lang_list=ch&lang_list=en 传递。
    for lang in PDF_PARSE_LANG_LIST:
        fields.append(("lang_list", lang))

    return fields


def _build_request_files(files: list[dict[str, str]]) -> list[tuple[str, Path]]:
    """从扫描结果中提取 PDF 文件路径，上传字段名固定为 files。"""
    return [("files", Path(file_info["absolute_path"])) for file_info in files]


def _post_multipart(
    url: str,
    fields: list[tuple[str, str]],
    files: list[tuple[str, Path]],
) -> tuple[int, str]:
    """使用标准库发送 multipart/form-data 请求，避免新增第三方依赖。"""
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
    """按 multipart/form-data 格式组装字段和文件内容。"""
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
    """拼接 MinerU 服务地址和 /file_parse 接口路径。"""
    return f"{mineru_server_url.rstrip('/')}{FILE_PARSE_PATH}"


def _try_load_json(response_text: str) -> object:
    """优先将响应解析为 JSON，失败时保留原始文本，方便排查接口问题。"""
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return response_text


def _build_parse_result(
    file_info: dict[str, str],
    parse_status: str,
    status_code: int | None,
    response: object,
    markdown_path: str | None,
) -> dict[str, object]:
    """构造 PDF 解析结果，保留源文件信息和接口响应摘要。"""
    return {
        "file_type": "pdf",
        "file_name": file_info["file_name"],
        "absolute_path": file_info["absolute_path"],
        "parse_status": parse_status,
        "status_code": status_code,
        "markdown_path": markdown_path,
        "response": response,
    }
