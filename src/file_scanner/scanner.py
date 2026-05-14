from pathlib import Path


SUPPORTED_FILE_TYPES = ("pdf", "docx", "xlsx", "txt")

# Office 打开文档时会生成 ~$ 开头的临时文件，这类文件不是有效入库源文件，固定跳过。
OFFICE_TEMP_PREFIX = "~$"
OFFICE_FILE_TYPES = ("docx", "xlsx")


def scan_files_by_type(
    scan_input_path: str,
    ignored_file_types: set[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """扫描指定目录，并按支持的文件类型归集文件名称和绝对路径。"""
    scan_root = Path(scan_input_path).expanduser().resolve()
    ignored_types = ignored_file_types or set()

    if not scan_root.exists():
        raise FileNotFoundError(f"Scan input path does not exist: {scan_root}")
    if not scan_root.is_dir():
        raise NotADirectoryError(f"Scan input path is not a directory: {scan_root}")

    scan_result: dict[str, list[dict[str, str]]] = {
        file_type: [] for file_type in SUPPORTED_FILE_TYPES
    }

    # 使用递归扫描，保证子目录中的企业文件也能被纳入批处理范围。
    for path in sorted(scan_root.rglob("*")):
        if not path.is_file():
            continue

        file_type = path.suffix.lower().lstrip(".")
        # 只采集当前流程支持的文件类型，其他文件先忽略。
        if file_type not in scan_result:
            continue
        # 配置中声明忽略的文件类型不进入后续解析流程。
        if file_type in ignored_types:
            continue
        if _is_office_temp_file(path, file_type):
            continue

        scan_result[file_type].append(
            {
                "file_name": path.name,
                "absolute_path": str(path.resolve()),
            }
        )

    return scan_result


def _is_office_temp_file(path: Path, file_type: str) -> bool:
    """判断是否为 Office 临时文件；该规则强制生效，不依赖配置开关。"""
    return file_type in OFFICE_FILE_TYPES and path.name.startswith(OFFICE_TEMP_PREFIX)
