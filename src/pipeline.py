import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from logging import Logger
from threading import Event
from typing import Callable

from src.config import get_bool_config, get_int_config, get_required_config
from src.data_cleaner.clean_task import CleanTaskConfig, run_clean_task
from src.database import DatabaseConfig, build_database_config
from src.lightrag_ingest import run_lightrag_upload_task
from src.parse_requester import ParseTaskConfig, run_parse_task


TaskRunner = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class PipelineConfig:
    db_config: DatabaseConfig
    parse_task_config: ParseTaskConfig
    clean_task_config: CleanTaskConfig


@dataclass(frozen=True)
class PipelineTask:
    name: str
    runner: TaskRunner


def build_pipeline_config(config: dict[str, str]) -> PipelineConfig:
    """从环境配置构建解析、清洗和 LightRAG 上传流程参数。"""
    db_config = build_database_config(config)
    return PipelineConfig(
        db_config=db_config,
        parse_task_config=ParseTaskConfig(
            db_config=db_config,
            mineru_server_url=get_required_config(config, "MINERU_SERVER_URL"),
            parse_output_path=get_required_config(config, "PARSE_OUTPUT_PATH"),
            poll_interval_seconds=get_int_config(
                config,
                "PARSE_TASK_POLL_INTERVAL_SECONDS",
                default=10,
                min_value=1,
            ),
            batch_size=get_int_config(
                config,
                "PARSE_TASK_BATCH_SIZE",
                default=5,
                min_value=1,
            ),
            enable_logging=get_bool_config(config, "ENABLE_LOGGING", True),
        ),
        clean_task_config=CleanTaskConfig(
            db_config=db_config,
            lightrag_server_url=get_required_config(config, "LIGHTRAG_SERVER_URL"),
            poll_interval_seconds=get_int_config(
                config,
                "CLEAN_TASK_POLL_INTERVAL_SECONDS",
                default=10,
                min_value=1,
            ),
            batch_size=get_int_config(
                config,
                "CLEAN_TASK_BATCH_SIZE",
                default=5,
                min_value=1,
            ),
            enable_logging=get_bool_config(config, "ENABLE_LOGGING", True),
        ),
    )


def run_pipeline(config: PipelineConfig, logger: Logger) -> dict[str, dict[str, object]]:
    """同时启动解析、清洗和 LightRAG 上传任务，并统一记录任务状态。"""
    stop_event = Event()
    tasks = _build_tasks(config, stop_event)

    logger.info("主流程启动，任务数量：%s", len(tasks))
    print("主流程启动，准备同时执行解析、清洗和 LightRAG 上传任务...")

    task_results = _run_tasks(tasks, logger, stop_event)

    logger.info("主流程结束，任务结果：%s", json.dumps(task_results, ensure_ascii=False))
    print("主流程执行结束。")
    return task_results


def _build_tasks(config: PipelineConfig, stop_event: Event) -> list[PipelineTask]:
    return [
        PipelineTask(
            name="解析任务",
            runner=lambda: run_parse_task(config.parse_task_config, stop_event),
        ),
        PipelineTask(
            name="清洗任务",
            runner=lambda: run_clean_task(config.clean_task_config, stop_event),
        ),
        PipelineTask(name="上传 LightRAG 任务", runner=run_lightrag_upload_task),
    ]


def _run_tasks(
    tasks: list[PipelineTask],
    logger: Logger,
    stop_event: Event,
) -> dict[str, dict[str, object]]:
    task_results: dict[str, dict[str, object]] = {}
    executor = ThreadPoolExecutor(max_workers=len(tasks))
    future_to_task: dict[Future, PipelineTask] = {}

    try:
        for task in tasks:
            logger.info("%s 已启动", task.name)
            print(f"{task.name} 已启动")
            future_to_task[executor.submit(task.runner)] = task

        while len(task_results) < len(future_to_task):
            for future, task in list(future_to_task.items()):
                if future.done() and task.name not in task_results:
                    task_results[task.name] = _collect_task_result(task, future, logger)
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.warning("收到手动终止信号，正在通知任务停止。")
        print("收到手动终止信号，正在通知任务停止...")
        stop_event.set()
        for future in future_to_task:
            future.cancel()
        _wait_running_tasks(future_to_task, task_results, logger)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return task_results


def _wait_running_tasks(
    future_to_task: dict[Future, PipelineTask],
    task_results: dict[str, dict[str, object]],
    logger: Logger,
) -> None:
    for future, task in future_to_task.items():
        if task.name in task_results:
            continue
        if future.cancelled():
            task_results[task.name] = {
                "task": task.name,
                "status": "cancelled",
                "message": "任务已取消。",
            }
            continue

        task_results[task.name] = _collect_task_result(task, future, logger)


def _collect_task_result(task: PipelineTask, future, logger: Logger) -> dict[str, object]:
    try:
        result = future.result()
        logger.info(
            "%s 状态：%s，详情：%s",
            task.name,
            result.get("status"),
            json.dumps(result, ensure_ascii=False),
        )
        print(f"{task.name} 状态：{result.get('status')}，{result.get('message')}")
        return result
    except Exception as exc:
        failure_result = {
            "task": task.name,
            "status": "failed",
            "message": str(exc),
        }
        logger.exception("%s 执行失败：%s", task.name, exc)
        print(f"{task.name} 状态：failed，{exc}")
        return failure_result
