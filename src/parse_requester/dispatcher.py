from src.parse_requester.docx_parser import request_docx_parse
from src.parse_requester.pdf_parser import request_pdf_parse
from src.parse_requester.xlsx_parser import request_xlsx_parse


def dispatch_parse_requests(
    scan_result: dict[str, list[dict[str, str]]],
    mineru_server_url: str,
    parse_output_path: str,
    enable_logging: bool = True,
    parse_request_concurrency: int = 3,
    parse_request_batch_size: int = 2,
) -> dict[str, list[dict[str, object]]]:
    """根据扫描结果中的文件类型，将文件分发给对应的请求解析器。"""
    return {
        "pdf": request_pdf_parse(
            scan_result.get("pdf", []),
            mineru_server_url=mineru_server_url,
            parse_output_path=parse_output_path,
            enable_logging=enable_logging,
            parse_request_concurrency=parse_request_concurrency,
            parse_request_batch_size=parse_request_batch_size,
        ),
        "docx": request_docx_parse(scan_result.get("docx", [])),
        "xlsx": request_xlsx_parse(scan_result.get("xlsx", [])),
    }
