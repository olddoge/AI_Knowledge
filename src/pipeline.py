import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from logging import Logger
from typing import Callable

from src.config import get_required_config
from src.data_cleaner.clean_task import run_clean_task
from src.database import DatabaseConfig, build_database_config
from src.file_scanner import scan_files_to_database
from src.lightrag_ingest import run_lightrag_upload_task
from src.parse_requester import run_parse_task


TaskRunner = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class PipelineConfig:
    scan_input_path: str
    db_config: DatabaseConfig
    ignored_file_types: set[str]


@dataclass(frozen=True)
class PipelineTask:
    name: str
    runner: TaskRunner


def build_pipeline_config(config: dict[str, str]) -> PipelineConfig:
    """从环境配置构建主流程运行参数。"""
    return PipelineConfig(
        scan_input_path=get_required_config(config, "SCAN_INPUT_PATH"),
        db_config=build_database_config(config),
        ignored_file_types=_parse_file_types(config.get("IGNORE_FILE_TYPES", "")),
    )


def run_pipeline(config: PipelineConfig, logger: Logger) -> dict[str, dict[str, object]]:
    """同时启动扫描、解析、清洗和 LightRAG 上传任务，并统一记录任务状态。"""
    tasks = _build_tasks(config)

    logger.info("主流程启动，任务数量：%s", len(tasks))
    print("主流程启动，准备同时执行任务...")

    task_results = _run_tasks(tasks, logger)

    logger.info("主流程结束，任务结果：%s", json.dumps(task_results, ensure_ascii=False))
    print("主流程执行结束。")
    return task_results


def _build_tasks(config: PipelineConfig) -> list[PipelineTask]:
    return [
        PipelineTask(
            name="扫描任务",
            runner=lambda: _run_scan_task(config),
        ),
        PipelineTask(name="解析任务", runner=run_parse_task),
        PipelineTask(name="清洗任务", runner=run_clean_task),
        PipelineTask(name="上传 LightRAG 任务", runner=run_lightrag_upload_task),
    ]


def _run_scan_task(config: PipelineConfig) -> dict[str, object]:
    result = scan_files_to_database(
        config.scan_input_path,
        db_config=config.db_config,
        ignored_file_types=config.ignored_file_types,
    )

    return {
        "task": "scan",
        "status": "success",
        "message": "扫描任务完成，扫描结果已写入数据库。",
        "result": result,
    }


def _run_tasks(tasks: list[PipelineTask], logger: Logger) -> dict[str, dict[str, object]]:
    task_results: dict[str, dict[str, object]] = {}

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_task = {}
        for task in tasks:
            logger.info("%s 已启动", task.name)
            print(f"{task.name} 已启动")
            future_to_task[executor.submit(task.runner)] = task

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            task_results[task.name] = _collect_task_result(task, future, logger)

    return task_results


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


def _parse_file_types(value: str) -> set[str]:
    """将逗号分隔的文件类型配置转换为小写集合，便于扫描时快速判断。"""
    file_types: set[str] = set()

    for raw_file_type in value.split(","):
        # 支持用户配置 pdf、PDF、Docx 等写法，统一规范化后再比较。
        file_type = raw_file_type.strip().lower().lstrip(".")
        if file_type:
            file_types.add(file_type)

    return file_types
