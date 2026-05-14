import json

from src.config import get_required_config, load_env
from src.file_scanner import scan_files_by_type
from src.logging_module import setup_logger
from src.parse_output import save_parse_result
from src.parse_requester import dispatch_parse_requests


def main() -> None:
    # 主入口只负责串联流程：读取配置 -> 扫描文件 -> 分发解析请求 -> 输出阶段结果。
    config = load_env()
    enable_logging = _get_bool_config(config, "ENABLE_LOGGING", default=True)
    logger = setup_logger(enable_logging=enable_logging)
    scan_input_path = get_required_config(config, "SCAN_INPUT_PATH")
    mineru_server_url = get_required_config(config, "MINERU_SERVER_URL")
    parse_output_path = get_required_config(config, "PARSE_OUTPUT_PATH")
    parse_request_concurrency = _get_int_config(
        config,
        "PARSE_REQUEST_CONCURRENCY",
        default=3,
        min_value=1,
    )
    parse_request_batch_size = _get_int_config(
        config,
        "PARSE_REQUEST_BATCH_SIZE",
        default=2,
        min_value=1,
    )
    ignored_file_types = _parse_file_types(config.get("IGNORE_FILE_TYPES", ""))

    print("正在扫描文件...")
    scan_result = scan_files_by_type(
        scan_input_path,
        ignored_file_types=ignored_file_types,
    )

    logger.info("扫描结果：%s", json.dumps(scan_result, ensure_ascii=False))

    print("正在请求解析文件...")
    parse_result = dispatch_parse_requests(
        scan_result,
        mineru_server_url=mineru_server_url,
        parse_output_path=parse_output_path,
        enable_logging=enable_logging,
        parse_request_concurrency=parse_request_concurrency,
        parse_request_batch_size=parse_request_batch_size,
    )
    parse_result_path = save_parse_result(parse_result, parse_output_path)
    logger.info("解析结果已保存：%s", parse_result_path)

    print("解析任务完成。")
    print(f"解析结果已保存：{parse_result_path}")


def _parse_file_types(value: str) -> set[str]:
    """将逗号分隔的文件类型配置转换为小写集合，便于扫描时快速判断。"""
    file_types: set[str] = set()

    for raw_file_type in value.split(","):
        # 支持用户配置 pdf、.PDF、 Docx 等写法，统一规范化后再比较。
        file_type = raw_file_type.strip().lower().lstrip(".")
        if file_type:
            file_types.add(file_type)

    return file_types


def _get_bool_config(config: dict[str, str], key: str, default: bool = False) -> bool:
    """读取布尔配置，支持 true/false、yes/no、on/off、1/0。"""
    value = config.get(key)
    if value is None or not value.strip():
        return default

    normalized_value = value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"Invalid boolean config {key}: {value}")


def _get_int_config(
    config: dict[str, str],
    key: str,
    default: int,
    min_value: int | None = None,
) -> int:
    """读取整数配置，并做最小值校验，避免并发数等关键参数无效。"""
    value = config.get(key)
    if value is None or not value.strip():
        return default

    try:
        parsed_value = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer config {key}: {value}") from exc

    if min_value is not None and parsed_value < min_value:
        raise ValueError(f"Config {key} must be greater than or equal to {min_value}")

    return parsed_value


if __name__ == "__main__":
    main()
