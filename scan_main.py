import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from src.config import get_bool_config, get_required_config, load_env
from src.database import build_database_config
from src.file_scanner import scan_files_to_database
from src.logging_module import setup_logger


def main() -> None:
    config = load_env()
    logger = setup_logger(enable_logging=get_bool_config(config, "ENABLE_LOGGING", True))
    stop_event = Event()

    logger.info("扫描入口启动")
    print("扫描入口启动，准备扫描文件并写入数据库...")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            scan_files_to_database,
            get_required_config(config, "SCAN_INPUT_PATH"),
            db_config=build_database_config(config),
            ignored_file_types=_parse_file_types(config.get("IGNORE_FILE_TYPES", "")),
            stop_event=stop_event,
        )

        try:
            while not future.done():
                time.sleep(0.2)
            result = future.result()
        except KeyboardInterrupt:
            logger.warning("收到手动终止信号，正在通知扫描任务停止。")
            print("收到手动终止信号，正在通知扫描任务停止...")
            stop_event.set()
            result = future.result()
        except Exception as exc:
            logger.exception("扫描任务执行失败：%s", exc)
            print(f"扫描任务执行失败：{exc}")
            raise

    status = "stopped" if result.get("stopped") else "success"
    logger.info("扫描任务结束，状态：%s，结果：%s", status, json.dumps(result, ensure_ascii=False))
    print(f"扫描任务结束，状态：{status}，结果：{json.dumps(result, ensure_ascii=False)}")


def _parse_file_types(value: str) -> set[str]:
    """将逗号分隔的文件类型配置转换为小写集合。"""
    file_types: set[str] = set()

    for raw_file_type in value.split(","):
        # 支持 pdf、PDF、.docx 等写法，统一规范化后再比较。
        file_type = raw_file_type.strip().lower().lstrip(".")
        if file_type:
            file_types.add(file_type)

    return file_types


if __name__ == "__main__":
    main()
