import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from src.data_cleaner.markdown_cleaner import clean_markdown_file
from src.database import DatabaseConfig
from src.lightrag_ingest import upload_file_to_lightrag
from src.logging_module import setup_module_logger
from src.repositories import RagFileRepository


CLEAN_TASK_MODULE_NAME = "clean_task"


@dataclass(frozen=True)
class CleanTaskConfig:
    db_config: DatabaseConfig
    lightrag_server_url: str
    poll_interval_seconds: int = 10
    batch_size: int = 5
    enable_logging: bool = True


def run_clean_task(config: CleanTaskConfig, stop_event: Event | None = None) -> dict[str, object]:
    """Legacy clean task for records that still have a local parse_path."""
    stop_signal = stop_event or Event()
    logger = setup_module_logger(CLEAN_TASK_MODULE_NAME, enable_logging=config.enable_logging)
    recovered_processing_count, recovered_failed_count = _recover_clean_files(config)
    logger.info(
        "Clean task started: interval=%s, batch_size=%s, recovered_processing=%s, recovered_failed=%s",
        config.poll_interval_seconds,
        config.batch_size,
        recovered_processing_count,
        recovered_failed_count,
    )
    print(
        f"清洗任务已启动，轮询间隔：{config.poll_interval_seconds} 秒，"
        f"单批数量：{config.batch_size}，恢复清洗中记录：{recovered_processing_count}，"
        f"恢复清洗失败记录：{recovered_failed_count}"
    )

    while not stop_signal.is_set():
        try:
            cycle_result = _run_clean_cycle(config, logger, stop_signal)
            logger.info("Clean task cycle result: %s", json.dumps(cycle_result, ensure_ascii=False))
            print(f"清洗任务本轮状态：{cycle_result['message']}")
        except Exception as exc:
            logger.exception("Clean task cycle failed: %s", exc)
            print(f"清洗任务本轮执行失败：{exc}")

        if stop_signal.wait(config.poll_interval_seconds):
            break

    logger.info("Clean task received stop signal and exited.")
    return {
        "task": "clean",
        "status": "stopped",
        "message": "清洗任务已停止，下次启动会继续处理未完成记录。",
    }


def _run_clean_cycle(config: CleanTaskConfig, logger, stop_event: Event) -> dict[str, object]:
    pending_files = _fetch_and_lock_pending_files(config)
    if not pending_files:
        return {
            "task": "clean",
            "status": "idle",
            "message": "没有待清洗文件。",
            "result": {"picked": 0, "success": 0, "failed": 0, "uploaded": 0},
        }
    if stop_event.is_set():
        _reset_files_to_pending(pending_files, config.db_config)
        return {
            "task": "clean",
            "status": "stopped",
            "message": "清洗任务停止，已将本轮锁定记录恢复为未清洗。",
            "result": {"picked": len(pending_files), "success": 0, "failed": 0, "uploaded": 0},
        }

    success_count = 0
    failed_count = 0
    uploaded_count = 0

    for file_record in pending_files:
        if stop_event.is_set():
            _reset_files_to_pending([file_record], config.db_config)
            continue

        try:
            clean_path, _ = _clean_one_file(file_record)
            _mark_file_clean_success(config.db_config, int(file_record["id"]))
            success_count += 1
            if upload_file_to_lightrag(
                config.lightrag_server_url,
                clean_path,
                str(file_record.get("original_path") or ""),
                logger,
            ):
                uploaded_count += 1
            logger.info("File cleaned: %s", clean_path)
        except Exception as exc:
            failed_count += 1
            _mark_file_clean_failed(config.db_config, int(file_record["id"]))
            logger.exception("File clean failed: %s, error=%s", json.dumps(file_record, ensure_ascii=False), exc)

    return {
        "task": "clean",
        "status": "running",
        "message": (
            f"本轮取出 {len(pending_files)} 条，成功 {success_count} 条，"
            f"失败 {failed_count} 条，上传 {uploaded_count} 条。"
        ),
        "result": {
            "picked": len(pending_files),
            "success": success_count,
            "failed": failed_count,
            "uploaded": uploaded_count,
        },
    }


def _fetch_and_lock_pending_files(config: CleanTaskConfig) -> list[dict[str, Any]]:
    repository = RagFileRepository(config.db_config)
    try:
        pending_files = repository.fetch_pending_clean_files(config.batch_size)
        if pending_files:
            repository.update_clean_status(
                [int(file_record["id"]) for file_record in pending_files],
                clean_status=1,
            )
            repository.commit()
        return pending_files
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _recover_clean_files(config: CleanTaskConfig) -> tuple[int, int]:
    repository = RagFileRepository(config.db_config)
    try:
        recovered_processing_count = repository.recover_processing_clean_files()
        recovered_failed_count = repository.recover_failed_clean_files()
        repository.commit()
        return recovered_processing_count, recovered_failed_count
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _reset_files_to_pending(files: list[dict[str, Any]], db_config: DatabaseConfig) -> None:
    if not files:
        return

    repository = RagFileRepository(db_config)
    try:
        repository.update_clean_status(
            [int(file_record["id"]) for file_record in files],
            clean_status=0,
        )
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _clean_one_file(file_record: dict[str, Any]) -> tuple[Path, list[str]]:
    parse_path = Path(str(file_record.get("parse_path") or "")).expanduser()
    if not parse_path.exists():
        raise FileNotFoundError(f"Parsed Markdown file does not exist: {parse_path}")

    return clean_markdown_file(parse_path, file_record)


def _mark_file_clean_success(db_config: DatabaseConfig, file_id: int) -> None:
    repository = RagFileRepository(db_config)
    try:
        repository.update_clean_success(file_id)
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _mark_file_clean_failed(db_config: DatabaseConfig, file_id: int) -> None:
    repository = RagFileRepository(db_config)
    try:
        repository.update_clean_failed(file_id)
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()
