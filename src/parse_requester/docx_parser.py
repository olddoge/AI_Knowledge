def request_docx_parse(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """DOCX 请求解析器占位；后续在这里接入 Word 文档解析逻辑。"""
    return [_build_placeholder_result(file_info, "docx") for file_info in files]


def _build_placeholder_result(file_info: dict[str, str], file_type: str) -> dict[str, str]:
    """构造占位解析结果，先保留文件信息和解析状态。"""
    return {
        "file_type": file_type,
        "file_name": file_info["file_name"],
        "absolute_path": file_info["absolute_path"],
        "parse_status": "pending",
    }
