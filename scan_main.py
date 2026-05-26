import json

from src.config import get_bool_config, get_int_config, load_env
from src.database import build_database_config
from src.file_scanner import DEFAULT_SCAN_BATCH_SIZE, NasFileScanner, build_ssh_config
from src.logging_module import setup_logger


def main() -> None:
    """扫描模块独立入口，只负责组装配置并启动 NAS 扫描服务。"""
    config = load_env()
    logger = setup_logger(enable_logging=get_bool_config(config, "ENABLE_LOGGING", True))

    try:
        scanner = NasFileScanner(
            db_config=build_database_config(config),
            ssh_config=build_ssh_config(config),
            batch_size=get_int_config(
                config,
                "SCAN_BATCH_SIZE",
                default=DEFAULT_SCAN_BATCH_SIZE,
                min_value=1,
            ),
            logger=logger,
        )

        logger.info("扫描模块启动，batch_size=%s", scanner.batch_size)
        print(f"扫描模块启动，批量大小：{scanner.batch_size}")

        result = scanner.scan_to_rag_files()
    except KeyboardInterrupt:
        logger.warning("扫描模块收到手动终止信号")
        print("扫描模块已手动终止。")
        return
    except Exception as exc:
        logger.exception("扫描模块执行失败：%s", exc)
        print(f"扫描模块执行失败：{exc}")
        raise

    logger.info("扫描模块结束：%s", json.dumps(result, ensure_ascii=False))
    print(f"扫描模块结束：{json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
