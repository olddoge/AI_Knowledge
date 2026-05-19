import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from src.database import DatabaseConfig
from src.logging_module import setup_module_logger
from src.parse_requester.pdf_parser import request_pdf_parse
from src.repositories import RagFileRepository


PARSE_TASK_MODULE_NAME = "parse_task"
SUPPORTED_PARSE_FILE_TYPES = {"pdf", "docx", "xlsx"}


@dataclass(frozen=True)
class ParseTaskConfig:
    db_config: DatabaseConfig
    mineru_server_url: str
    parse_output_path: str
    poll_interval_seconds: int = 10
    batch_size: int = 5
    enable_logging: bool = True


def run_parse_task(config: ParseTaskConfig, stop_event: Event | None = None) -> dict[str, object]:
    """定时查询待解析文件并调度解析器；该任务会持续运行。"""
    stop_signal = stop_event or Event()
    logger = setup_module_logger(PARSE_TASK_MODULE_NAME, enable_logging=config.enable_logging)
    recovered_processing_count, recovered_failed_count = _recover_parse_files(config)
    logger.info(
        "解析任务启动，轮询间隔：%s 秒，单批数量：%s，恢复解析中记录：%s，恢复解析失败记录：%s",
        config.poll_interval_seconds,
        config.batch_size,
        recovered_processing_count,
        recovered_failed_count,
    )
    print(
        f"解析任务已启动，轮询间隔：{config.poll_interval_seconds} 秒，"
        f"单批数量：{config.batch_size}，恢复解析中记录：{recovered_processing_count}，"
        f"恢复解析失败记录：{recovered_failed_count}"
    )

    while not stop_signal.is_set():
        try:
            cycle_result = _run_parse_cycle(config, logger, stop_signal)
            logger.info("解析任务本轮结果：%s", json.dumps(cycle_result, ensure_ascii=False))
            print(f"解析任务本轮状态：{cycle_result['message']}")
        except Exception as exc:
            logger.exception("解析任务本轮执行失败：%s", exc)
            print(f"解析任务本轮执行失败：{exc}")

        if stop_signal.wait(config.poll_interval_seconds):
            break

    logger.info("解析任务收到停止信号，已退出轮询。")
    return {
        "task": "parse",
        "status": "stopped",
        "message": "解析任务已停止，下次启动会继续处理未完成记录。",
    }


def _run_parse_cycle(config: ParseTaskConfig, logger, stop_event: Event) -> dict[str, object]:
    pending_files = _fetch_and_lock_pending_files(config)
    if not pending_files:
        return {
            "task": "parse",
            "status": "idle",
            "message": "没有待解析文件。",
            "result": {"picked": 0, "success": 0, "failed": 0, "unsupported": 0},
        }
    if stop_event.is_set():
        _reset_files_to_pending(pending_files, config.db_config)
        return {
            "task": "parse",
            "status": "stopped",
            "message": "解析任务停止，已将本轮锁定记录恢复为未解析。",
            "result": {"picked": len(pending_files), "success": 0, "failed": 0, "unsupported": 0},
        }

    grouped_files = _group_files_by_ext(pending_files)
    success_count = 0
    failed_count = 0
    unsupported_count = 0

    parse_files = _collect_supported_parse_files(grouped_files)
    if parse_files:
        try:
            parse_results = request_pdf_parse(
                _build_parse_request_files(parse_files),
                mineru_server_url=config.mineru_server_url,
                parse_output_path=config.parse_output_path,
                enable_logging=config.enable_logging,
                parse_request_concurrency=1,
                parse_request_batch_size=max(1, len(parse_files)),
            )
            success_count, failed_count = _save_parse_results(
                parse_results,
                config.db_config,
                logger,
            )
        except Exception:
            logger.exception("文件解析批次异常，当前批次文件将标记为解析失败。")
            _mark_files_failed(parse_files, config.db_config)
            failed_count += len(parse_files)

    unsupported_files = [
        file_record
        for file_ext, files in grouped_files.items()
        if file_ext not in SUPPORTED_PARSE_FILE_TYPES
        for file_record in files
    ]
    if unsupported_files:
        unsupported_count = len(unsupported_files)
        failed_count += unsupported_count
        _mark_files_failed(unsupported_files, config.db_config)

    return {
        "task": "parse",
        "status": "running",
        "message": (
            f"本轮取出 {len(pending_files)} 条，成功 {success_count} 条，"
            f"失败 {failed_count} 条。"
        ),
        "result": {
            "picked": len(pending_files),
            "success": success_count,
            "failed": failed_count,
            "unsupported": unsupported_count,
        },
    }


def _fetch_and_lock_pending_files(config: ParseTaskConfig) -> list[dict[str, Any]]:
    repository = RagFileRepository(config.db_config)
    try:
        pending_files = repository.fetch_pending_parse_files(config.batch_size)
        if pending_files:
            repository.update_parse_status(
                [int(file_record["id"]) for file_record in pending_files],
                parse_status=1,
            )
            repository.commit()
        return pending_files
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _recover_parse_files(config: ParseTaskConfig) -> tuple[int, int]:
    repository = RagFileRepository(config.db_config)
    try:
        recovered_processing_count = repository.recover_processing_parse_files()
        failed_files = repository.fetch_failed_parse_files()
        recoverable_failed_files = [
            file_record
            for file_record in failed_files
            if _original_file_exists(file_record.get("original_path"))
        ]
        missing_failed_files = [
            file_record
            for file_record in failed_files
            if not _original_file_exists(file_record.get("original_path"))
        ]
        repository.update_parse_status(
            [int(file_record["id"]) for file_record in recoverable_failed_files],
            parse_status=0,
        )
        repository.update_parse_status(
            [int(file_record["id"]) for file_record in missing_failed_files],
            parse_status=-1,
        )
        recovered_failed_count = len(recoverable_failed_files)
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
        repository.update_parse_status(
            [int(file_record["id"]) for file_record in files],
            parse_status=0,
        )
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _group_files_by_ext(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped_files: dict[str, list[dict[str, Any]]] = {}
    for file_record in files:
        file_ext = str(file_record.get("file_ext") or "").lower().lstrip(".")
        grouped_files.setdefault(file_ext, []).append(file_record)
    return grouped_files


def _collect_supported_parse_files(
    grouped_files: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    parse_files: list[dict[str, Any]] = []
    for file_ext in SUPPORTED_PARSE_FILE_TYPES:
        parse_files.extend(grouped_files.get(file_ext, []))
    return parse_files


def _build_parse_request_files(files: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "id": str(file_record["id"]),
            "file_uid": str(file_record["file_uid"]),
            "file_name": str(file_record["file_name"]),
            "file_ext": str(file_record["file_ext"]),
            "absolute_path": str(file_record["original_path"]),
        }
        for file_record in files
    ]


def _save_parse_results(
    parse_results: list[dict[str, object]],
    db_config: DatabaseConfig,
    logger,
) -> tuple[int, int]:
    success_count = 0
    failed_count = 0

    repository = RagFileRepository(db_config)
    try:
        for result in parse_results:
            file_id = int(result["id"])
            markdown_path = result.get("markdown_path")
            if result.get("parse_status") == "success" and _markdown_file_exists(markdown_path):
                repository.update_parse_success(file_id, str(markdown_path))
                success_count += 1
            else:
                repository.update_parse_failed(file_id)
                failed_count += 1
                logger.warning("文件解析失败或未生成 markdown：%s", json.dumps(result, ensure_ascii=False))
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()

    return success_count, failed_count


def _mark_files_failed(files: list[dict[str, Any]], db_config: DatabaseConfig) -> None:
    if not files:
        return

    repository = RagFileRepository(db_config)
    try:
        for file_record in files:
            repository.update_parse_failed(int(file_record["id"]))
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _markdown_file_exists(markdown_path: object) -> bool:
    if not isinstance(markdown_path, str) or not markdown_path.strip():
        return False
    return Path(markdown_path).exists()


def _original_file_exists(original_path: object) -> bool:
    if not isinstance(original_path, str) or not original_path.strip():
        return False
    return Path(original_path).is_file()
