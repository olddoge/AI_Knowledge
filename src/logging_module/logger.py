import logging
from pathlib import Path
from time import localtime, strftime


LOG_FILE_PATH = Path("logs/app.log")
LOG_MAX_BYTES = 5 * 1024 * 1024


class ChunkedFileHandler(logging.Handler):
    """按大小切分日志文件，文件名包含功能、分块序号和时间戳。"""

    def __init__(
        self,
        module_name: str,
        max_bytes: int = LOG_MAX_BYTES,
        log_dir: str | Path = "logs",
    ) -> None:
        super().__init__()
        self.module_name = module_name
        self.max_bytes = max_bytes
        self.log_dir = Path(log_dir)
        self.chunk_index = 1
        self.stream = None
        self.current_path: Path | None = None

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._open_next_log_file()

    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            self._open_next_log_file()

        message = self.format(record) + "\n"
        encoded_message = message.encode("utf-8")

        if self.current_path and self.current_path.exists():
            current_size = self.current_path.stat().st_size
            if current_size + len(encoded_message) > self.max_bytes:
                self._open_next_log_file()

        self.stream.write(message)
        self.flush()

    def flush(self) -> None:
        if self.stream:
            self.stream.flush()

    def close(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        super().close()

    def _open_next_log_file(self) -> None:
        if self.stream:
            self.stream.close()

        timestamp = strftime("%Y%m%d_%H%M%S", localtime())
        self.current_path = self.log_dir / (
            f"{self.module_name}_{self.chunk_index:04d}_{timestamp}.log"
        )
        self.chunk_index += 1
        self.stream = self.current_path.open("a", encoding="utf-8")


def setup_logger(enable_logging: bool = True) -> logging.Logger:
    """初始化项目日志器，将运行过程写入 logs/app.log。"""
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ai_knowledge_rag")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not enable_logging:
        _reset_logger_handlers(logger)
        logger.addHandler(logging.NullHandler())
        return logger

    if logger.handlers:
        return logger

    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )

    logger.addHandler(file_handler)
    return logger


def setup_module_logger(module_name: str, enable_logging: bool = True) -> logging.Logger:
    """初始化指定功能模块日志器，用独立文件记录该模块请求和响应。"""
    logger = logging.getLogger(f"ai_knowledge_rag.{module_name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    _reset_logger_handlers(logger)

    if not enable_logging:
        logger.addHandler(logging.NullHandler())
        return logger

    handler = ChunkedFileHandler(module_name=module_name)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


def _reset_logger_handlers(logger: logging.Logger) -> None:
    """重新初始化日志器时关闭旧 handler，避免重复写入。"""
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
