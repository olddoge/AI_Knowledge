import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from src.data_cleaner.markdown_cleaner import clean_markdown_content, extract_image_names
from src.database import DatabaseConfig
from src.lightrag_ingest import upload_text_to_lightrag
from src.logging_module import setup_module_logger
from src.parse_requester.pdf_parser import request_pdf_parse
from src.repositories import RagFileRepository, RagImageRepository


PARSE_TASK_MODULE_NAME = "parse_task"
SUPPORTED_PARSE_FILE_TYPES = {"pdf", "docx", "xlsx"}


@dataclass(frozen=True)
class ParseTaskConfig:
    db_config: DatabaseConfig
    mineru_server_url: str
    parse_output_path: str
    lightrag_server_url: str
    poll_interval_seconds: int = 10
    batch_size: int = 5
    enable_logging: bool = True


def run_parse_task(config: ParseTaskConfig, stop_event: Event | None = None) -> dict[str, object]:
    """Poll pending files, parse them, clean markdown, and upload text to LightRAG."""
    stop_signal = stop_event or Event()
    logger = setup_module_logger(PARSE_TASK_MODULE_NAME, enable_logging=config.enable_logging)
    recovered_processing_count, recovered_failed_count = _recover_parse_files(config)
    logger.info(
        "Parse task started: interval=%s, batch_size=%s, recovered_processing=%s, recovered_failed=%s",
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
            logger.info("Parse task cycle result: %s", json.dumps(cycle_result, ensure_ascii=False))
            print(f"解析任务本轮状态：{cycle_result['message']}")
        except Exception as exc:
            logger.exception("Parse task cycle failed: %s", exc)
            print(f"解析任务本轮执行失败：{exc}")

        if stop_signal.wait(config.poll_interval_seconds):
            break

    logger.info("Parse task received stop signal and exited.")
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
                config.lightrag_server_url,
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
        "message": f"本轮取出 {len(pending_files)} 条，成功 {success_count} 条，失败 {failed_count} 条。",
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
    lightrag_server_url: str,
    logger,
) -> tuple[int, int]:
    success_count = 0
    failed_count = 0
    repository = RagFileRepository(db_config)
    try:
        for result in parse_results:
            file_id = int(result["id"])
            markdown_content = result.get("markdown_content")
            if result.get("parse_status") != "success" or not _has_markdown_content(markdown_content):
                repository.update_parse_failed(file_id)
                failed_count += 1
                logger.warning(
                    "文件解析失败或未生成 markdown：%s",
                    json.dumps(
                        {
                            "file_name": result.get("file_name", ""),
                            "parse_status": result.get("parse_status", ""),
                            "status_code": result.get("status_code"),
                            "response_success": result.get("response_success"),
                        },
                        ensure_ascii=False,
                    ),
                )
                continue

            if _clean_and_upload_parse_result(result, db_config, lightrag_server_url, logger):
                repository.update_parse_success(file_id)
                repository.update_clean_success(file_id)
                success_count += 1
            else:
                repository.update_parse_failed(file_id)
                repository.update_clean_failed(file_id)
                failed_count += 1
        repository.commit()
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()

    return success_count, failed_count


def _clean_and_upload_parse_result(
    result: dict[str, object],
    db_config: DatabaseConfig,
    lightrag_server_url: str,
    logger,
) -> bool:
    raw_markdown = str(result["markdown_content"])
    file_record = _build_clean_file_record(result)
    try:
        cleaned_markdown = clean_markdown_content(raw_markdown, file_record)
        inserted_image_count = _save_image_records(
            db_config,
            str(file_record["file_uid"]),
            extract_image_names(raw_markdown),
        )
        if not upload_text_to_lightrag(
            lightrag_server_url,
            cleaned_markdown,
            str(file_record["original_path"]),
            logger,
        ):
            logger.warning("文件上传 LightRAG 失败，将重新进入解析重试：file_id=%s", file_record["id"])
            return False

        logger.info(
            "文件解析、清洗并上传完成：file_id=%s，图片新增：%s",
            file_record["id"],
            inserted_image_count,
        )
        return True
    except Exception as exc:
        logger.exception("文件解析后清洗或上传异常：file_id=%s，错误：%s", file_record["id"], exc)
        return False


def _build_clean_file_record(result: dict[str, object]) -> dict[str, object]:
    return {
        "id": result.get("id", ""),
        "file_uid": result.get("file_uid", ""),
        "file_name": result.get("file_name", ""),
        "file_ext": result.get("file_type", ""),
        "original_path": result.get("absolute_path", ""),
    }


def _save_image_records(db_config: DatabaseConfig, file_uid: str, image_names: list[str]) -> int:
    if not image_names:
        return 0

    repository = RagImageRepository(db_config)
    inserted_count = 0
    try:
        for image_name in image_names:
            if repository.insert_if_not_exists(file_uid, image_name):
                inserted_count += 1
        repository.commit()
        return inserted_count
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


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


def _has_markdown_content(markdown_content: object) -> bool:
    return isinstance(markdown_content, str) and bool(markdown_content.strip())


def _original_file_exists(original_path: object) -> bool:
    if not isinstance(original_path, str) or not original_path.strip():
        return False
    return Path(original_path).is_file()
