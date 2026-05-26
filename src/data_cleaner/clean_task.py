import json
from dataclasses import dataclass
from multiprocessing import Queue, get_context
from pathlib import Path
from queue import Empty
from typing import Any

from src.config import get_bool_config, get_int_config, get_required_config
from src.data_cleaner.markdown_cleaner import clean_markdown_file
from src.database import DatabaseConfig, build_database_config
from src.lightrag_ingest import upload_text_to_lightrag
from src.logging_module import setup_single_file_module_logger
from src.repositories import RagFileRepository


CLEAN_TASK_MODULE_NAME = "clean_task"
DEFAULT_CLEAN_TASK_BATCH_SIZE = 5
DEFAULT_CLEAN_TASK_WORKER_PROCESSES = 2
DEFAULT_CLEAN_TASK_STALE_SECONDS = 3600


@dataclass(frozen=True)
class CleanTaskConfig:
    """清洗模块运行配置。

    clean_main.py 会按这份配置独立启动清洗模块，不依赖扫描、解析等入口。
    batch_size 固定表达“每个清洗进程每次从数据库领取多少条记录”，默认 5。
    worker_processes 表达同时启动多少个清洗进程。
    stale_seconds 用于恢复异常中断后长期停留在 clean_status=1 的旧任务。
    """

    db_config: DatabaseConfig
    lightrag_server_url: str
    keep_markdown_file: bool = True
    batch_size: int = DEFAULT_CLEAN_TASK_BATCH_SIZE
    worker_processes: int = DEFAULT_CLEAN_TASK_WORKER_PROCESSES
    stale_seconds: int = DEFAULT_CLEAN_TASK_STALE_SECONDS
    enable_logging: bool = True


class CleanTaskWorker:
    """单个清洗进程的执行器。

    每个 worker 都创建自己的数据库连接和日志对象，避免多进程共享连接导致
    事务互相影响。领取任务时使用 FOR UPDATE SKIP LOCKED，保证并发安全。
    """

    def __init__(self, config: CleanTaskConfig, worker_index: int = 1) -> None:
        self.config = config
        self.worker_index = worker_index
        self.worker_name = f"clean-{worker_index}"
        self.logger = setup_single_file_module_logger(
            CLEAN_TASK_MODULE_NAME,
            enable_logging=config.enable_logging,
        )

    def run_until_idle(self) -> dict[str, object]:
        """持续领取并处理清洗任务，直到数据库中没有可领取记录后退出。"""
        result = {
            "worker": self.worker_name,
            "picked": 0,
            "success": 0,
            "failed": 0,
            "uploaded": 0,
        }
        self.logger.info(
            "Clean worker started: worker=%s, batch_size=%s",
            self.worker_name,
            self.config.batch_size,
        )

        while True:
            file_records = self._claim_pending_files()
            if not file_records:
                break

            result["picked"] = int(result["picked"]) + len(file_records)
            print(f"{self.worker_name} 领取 {len(file_records)} 条清洗任务")

            for file_record in file_records:
                one_result = self._process_one_file(file_record)
                if one_result["success"]:
                    result["success"] = int(result["success"]) + 1
                    result["uploaded"] = int(result["uploaded"]) + 1
                else:
                    result["failed"] = int(result["failed"]) + 1

        self.logger.info("Clean worker finished: %s", json.dumps(result, ensure_ascii=False))
        print(
            f"{self.worker_name} 结束：领取 {result['picked']}，"
            f"成功 {result['success']}，失败 {result['failed']}"
        )
        return result

    def _claim_pending_files(self) -> list[dict[str, Any]]:
        """从 rag_files 原子领取 clean_status=0 的记录，并立即标记为清洗中。"""
        repository = RagFileRepository(self.config.db_config)
        try:
            file_records = repository.claim_pending_clean_files(self.config.batch_size)
            repository.commit()
            return file_records
        except Exception:
            repository.rollback()
            raise
        finally:
            repository.close()

    def _process_one_file(self, file_record: dict[str, Any]) -> dict[str, object]:
        """清洗单条记录，写回 Markdown，然后上传到 LightRAG。

        成功路径：
        1. 读取 parse_path 指向的 Markdown；
        2. 复用 markdown_cleaner 中的文本、HTML、图片引用清洗逻辑；
        3. 将清洗后的内容写回原 Markdown 文件；
        4. 把清洗后的文本和 original_path 发送到 LightRAG；
        5. 将 clean_status 更新为 2。

        任一步失败都会记录完整异常，并将 clean_status 更新为 -1，便于后续重试。
        """
        file_id = int(file_record["id"])
        parse_path = Path(str(file_record.get("parse_path") or "")).expanduser()
        original_path = str(file_record.get("original_path") or "")

        print(f"{self.worker_name} 清洗中：id={file_id}, file={file_record.get('file_name', '')}")
        try:
            if not parse_path.is_file():
                raise FileNotFoundError(f"Parsed Markdown file does not exist: {parse_path}")

            clean_path, image_names = clean_markdown_file(parse_path, file_record)
            cleaned_text = clean_path.read_text(encoding="utf-8")

            if not upload_text_to_lightrag(
                self.config.lightrag_server_url,
                cleaned_text,
                original_path,
                self.logger,
            ):
                raise RuntimeError("LightRAG upload returned failure")

            self._delete_markdown_after_success(clean_path, file_id)
            self._mark_clean_success(file_id)
            self.logger.info(
                "File cleaned and uploaded: file_id=%s, clean_path=%s, images=%s",
                file_id,
                clean_path,
                image_names,
            )
            print(f"{self.worker_name} 完成：id={file_id}")
            return {"success": True, "file_id": file_id}
        except Exception as exc:
            self._mark_clean_failed(file_id)
            self.logger.exception(
                "File clean failed: file_record=%s, error=%s",
                json.dumps(file_record, ensure_ascii=False),
                exc,
            )
            print(f"{self.worker_name} 失败：id={file_id}, error={exc}")
            return {"success": False, "file_id": file_id, "error": str(exc)}

    def _delete_markdown_after_success(self, clean_path: Path, file_id: int) -> None:
        """按 KEEP_MARKDOWN_FILE 配置决定是否删除清洗后的 Markdown 文件。

        删除发生在 LightRAG 上传成功之后。删除失败时记录完整异常并提示终端，
        但不把已上传成功的任务改成失败，避免下次重试导致重复入库。
        """
        if self.config.keep_markdown_file:
            return

        try:
            if clean_path.exists():
                clean_path.unlink()
                self.logger.info("Cleaned markdown deleted: file_id=%s, clean_path=%s", file_id, clean_path)
                print(f"{self.worker_name} 已删除 Markdown：id={file_id}, path={clean_path}")
            else:
                self.logger.warning(
                    "Cleaned markdown already missing before delete: file_id=%s, clean_path=%s",
                    file_id,
                    clean_path,
                )
        except Exception as exc:
            self.logger.exception(
                "Cleaned markdown delete failed after upload: file_id=%s, clean_path=%s, error=%s",
                file_id,
                clean_path,
                exc,
            )
            print(f"{self.worker_name} 删除 Markdown 失败：id={file_id}, error={exc}")

    def _mark_clean_success(self, file_id: int) -> None:
        repository = RagFileRepository(self.config.db_config)
        try:
            repository.update_clean_success(file_id)
            repository.commit()
        except Exception:
            repository.rollback()
            raise
        finally:
            repository.close()

    def _mark_clean_failed(self, file_id: int) -> None:
        repository = RagFileRepository(self.config.db_config)
        try:
            repository.update_clean_failed(file_id)
            repository.commit()
        except Exception:
            repository.rollback()
            raise
        finally:
            repository.close()


def build_clean_task_config(config: dict[str, str]) -> CleanTaskConfig:
    """从 .env 配置构建清洗模块配置。"""
    return CleanTaskConfig(
        db_config=build_database_config(config),
        lightrag_server_url=get_required_config(config, "LIGHTRAG_SERVER_URL"),
        keep_markdown_file=get_bool_config(config, "KEEP_MARKDOWN_FILE", True),
        batch_size=get_int_config(
            config,
            "CLEAN_TASK_BATCH_SIZE",
            default=DEFAULT_CLEAN_TASK_BATCH_SIZE,
            min_value=1,
        ),
        worker_processes=get_int_config(
            config,
            "CLEAN_TASK_WORKER_PROCESSES",
            default=DEFAULT_CLEAN_TASK_WORKER_PROCESSES,
            min_value=1,
        ),
        stale_seconds=get_int_config(
            config,
            "CLEAN_TASK_STALE_SECONDS",
            default=DEFAULT_CLEAN_TASK_STALE_SECONDS,
            min_value=1,
        ),
        enable_logging=get_bool_config(config, "ENABLE_LOGGING", True),
    )


def run_clean_task(config: CleanTaskConfig) -> dict[str, object]:
    """兼容旧调用方式：在当前进程中执行清洗，直到无任务后退出。"""
    _recover_clean_files(config)
    return CleanTaskWorker(config, worker_index=1).run_until_idle()


def run_clean_task_processes(config: CleanTaskConfig) -> dict[str, object]:
    """启动多个清洗进程并等待它们全部结束。

    父进程只负责恢复旧任务、创建子进程和汇总结果；实际数据库领取、文件清洗、
    LightRAG 上传都在子进程内完成，避免共享连接和共享状态。
    """
    recovered_processing_count, recovered_failed_count = _recover_clean_files(config)
    print(
        "清洗模块启动："
        f"进程数={config.worker_processes}，每批={config.batch_size}，"
        f"恢复清洗中={recovered_processing_count}，恢复失败={recovered_failed_count}"
    )

    if config.worker_processes == 1:
        worker_result = CleanTaskWorker(config, worker_index=1).run_until_idle()
        return _build_total_result([worker_result], recovered_processing_count, recovered_failed_count)

    context = get_context("spawn")
    result_queue: Queue = context.Queue()
    processes = [
        context.Process(
            target=_run_worker_process,
            args=(worker_index, config, result_queue),
            name=f"clean-worker-{worker_index}",
        )
        for worker_index in range(1, config.worker_processes + 1)
    ]

    try:
        for process in processes:
            process.start()

        for process in processes:
            process.join()
    except KeyboardInterrupt:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join()
        raise

    worker_results: list[dict[str, object]] = []
    while True:
        try:
            worker_results.append(result_queue.get_nowait())
        except Empty:
            break

    failed_processes = [
        process.name
        for process in processes
        if process.exitcode not in (0, None)
    ]
    total_result = _build_total_result(
        worker_results,
        recovered_processing_count,
        recovered_failed_count,
    )
    total_result["failed_processes"] = failed_processes

    if failed_processes:
        raise RuntimeError(f"Clean worker process failed: {failed_processes}")

    return total_result


def _run_worker_process(worker_index: int, config: CleanTaskConfig, result_queue: Queue) -> None:
    """子进程入口。异常会写入队列后继续抛出，让父进程感知非 0 exitcode。"""
    try:
        result_queue.put(CleanTaskWorker(config, worker_index).run_until_idle())
    except Exception as exc:
        result_queue.put(
            {
                "worker": f"clean-{worker_index}",
                "picked": 0,
                "success": 0,
                "failed": 0,
                "uploaded": 0,
                "error": str(exc),
            }
        )
        raise


def _recover_clean_files(config: CleanTaskConfig) -> tuple[int, int]:
    """恢复可重试任务。

    clean_status=1 只恢复超过 stale_seconds 未更新的记录，避免抢正在运行的任务。
    clean_status=-1 的记录全部恢复为 0，满足失败任务可重试的要求。
    """
    repository = RagFileRepository(config.db_config)
    try:
        recovered_processing_count = repository.recover_processing_clean_files(
            stale_seconds=config.stale_seconds
        )
        recovered_failed_count = repository.recover_failed_clean_files()
        repository.commit()
        return recovered_processing_count, recovered_failed_count
    except Exception:
        repository.rollback()
        raise
    finally:
        repository.close()


def _build_total_result(
    worker_results: list[dict[str, object]],
    recovered_processing_count: int,
    recovered_failed_count: int,
) -> dict[str, object]:
    return {
        "task": "clean",
        "status": "finished",
        "message": "清洗模块已处理到空闲并退出",
        "recovered_processing": recovered_processing_count,
        "recovered_failed": recovered_failed_count,
        "picked": sum(int(item.get("picked", 0)) for item in worker_results),
        "success": sum(int(item.get("success", 0)) for item in worker_results),
        "failed": sum(int(item.get("failed", 0)) for item in worker_results),
        "uploaded": sum(int(item.get("uploaded", 0)) for item in worker_results),
        "workers": worker_results,
    }
