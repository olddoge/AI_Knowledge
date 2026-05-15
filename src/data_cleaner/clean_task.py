import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from src.data_cleaner.markdown_cleaner import clean_markdown_file
from src.database import DatabaseConfig
from src.lightrag_ingest.client import upload_document
from src.logging_module import setup_module_logger
from src.repositories import RagFileRepository, RagImageRepository


CLEAN_TASK_MODULE_NAME = "clean_task"


@dataclass(frozen=True)
class CleanTaskConfig:
    db_config: DatabaseConfig
    lightrag_server_url: str
    poll_interval_seconds: int = 10
    batch_size: int = 5
    enable_logging: bool = True


def run_clean_task(config: CleanTaskConfig, stop_event: Event | None = None) -> dict[str, object]:
    """定时查询待清洗文件并执行 Markdown 清洗；该任务会持续运行。"""
    stop_signal = stop_event or Event()
    logger = setup_module_logger(CLEAN_TASK_MODULE_NAME, enable_logging=config.enable_logging)
    recovered_count = _recover_processing_files(config)
    logger.info(
        "清洗任务启动，轮询间隔：%s 秒，单批数量：%s，恢复清洗中记录：%s",
        config.poll_interval_seconds,
        config.batch_size,
        recovered_count,
    )
    print(
        f"清洗任务已启动，轮询间隔：{config.poll_interval_seconds} 秒，"
        f"单批数量：{config.batch_size}，恢复清洗中记录：{recovered_count}"
    )

    while not stop_signal.is_set():
        try:
            cycle_result = _run_clean_cycle(config, logger, stop_signal)
            logger.info("清洗任务本轮结果：%s", json.dumps(cycle_result, ensure_ascii=False))
            print(f"清洗任务本轮状态：{cycle_result['message']}")
        except Exception as exc:
            logger.exception("清洗任务本轮执行失败：%s", exc)
            print(f"清洗任务本轮执行失败：{exc}")

        if stop_signal.wait(config.poll_interval_seconds):
            break

    logger.info("清洗任务收到停止信号，已退出轮询。")
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
            "result": {"picked": 0, "success": 0, "failed": 0, "images": 0, "uploaded": 0},
        }
    if stop_event.is_set():
        _reset_files_to_pending(pending_files, config.db_config)
        return {
            "task": "clean",
            "status": "stopped",
            "message": "清洗任务停止，已将本轮锁定记录恢复为未清洗。",
            "result": {"picked": len(pending_files), "success": 0, "failed": 0, "images": 0, "uploaded": 0},
        }

    success_count = 0
    failed_count = 0
    image_count = 0
    uploaded_count = 0

    for file_record in pending_files:
        if stop_event.is_set():
            _reset_files_to_pending([file_record], config.db_config)
            continue

        try:
            clean_path, image_names = _clean_one_file(file_record)
            inserted_image_count = _save_image_records(
                config.db_config,
                str(file_record["file_uid"]),
                image_names,
            )
            image_count += inserted_image_count
            _mark_file_clean_success(config.db_config, int(file_record["id"]))
            success_count += 1
            if _try_upload_to_lightrag(config.lightrag_server_url, clean_path, logger):
                uploaded_count += 1
            logger.info(
                "文件清洗完成：%s，图片新增：%s",
                clean_path,
                inserted_image_count,
            )
        except Exception as exc:
            failed_count += 1
            _mark_file_clean_failed(config.db_config, int(file_record["id"]))
            logger.exception("文件清洗失败：%s，错误：%s", json.dumps(file_record, ensure_ascii=False), exc)

    return {
        "task": "clean",
        "status": "running",
        "message": (
            f"本轮取出 {len(pending_files)} 条，成功 {success_count} 条，"
            f"失败 {failed_count} 条，新增图片 {image_count} 条，上传 {uploaded_count} 条。"
        ),
        "result": {
            "picked": len(pending_files),
            "success": success_count,
            "failed": failed_count,
            "images": image_count,
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


def _recover_processing_files(config: CleanTaskConfig) -> int:
    repository = RagFileRepository(config.db_config)
    try:
        recovered_count = repository.recover_processing_clean_files()
        repository.commit()
        return recovered_count
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
        raise FileNotFoundError(f"解析后的 Markdown 文件不存在：{parse_path}")

    return clean_markdown_file(parse_path, file_record)


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


def _try_upload_to_lightrag(lightrag_server_url: str, clean_path: Path, logger) -> bool:
    try:
        upload_document(lightrag_server_url, clean_path)
        return True
    except Exception as exc:
        # LightRAG 上传不参与清洗状态判定，但必须记录，便于后续排查和补传。
        logger.exception("清洗文件上传 LightRAG 失败：%s，错误：%s", clean_path, exc)
        return False


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
