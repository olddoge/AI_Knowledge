import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import get_bool_config, get_int_config, get_required_config
from src.database import DatabaseConfig, build_database_config
from src.lightrag_ingest import upload_text_to_lightrag
from src.logging_module import setup_single_file_module_logger
from src.repositories import RagFileRepository


MARKDOWN_UPLOAD_MODULE_NAME = "markdown_upload_task"
DEFAULT_MARKDOWN_UPLOAD_BATCH_SIZE = 5


@dataclass(frozen=True)
class MarkdownUploadTaskConfig:
    """从数据库读取已解析 Markdown 记录，并原文上传到 LightRAG 的测试入口配置。"""

    db_config: DatabaseConfig
    lightrag_server_url: str
    markdown_output_path: str
    batch_size: int = DEFAULT_MARKDOWN_UPLOAD_BATCH_SIZE
    enable_logging: bool = True


class MarkdownUploadWorker:
    """只上传 Markdown 原文，不清洗、不删除文件、不更新 rag_files 状态。"""

    def __init__(self, config: MarkdownUploadTaskConfig) -> None:
        self.config = config
        self.markdown_output_dir = Path(config.markdown_output_path).expanduser().resolve()
        self.logger = setup_single_file_module_logger(
            MARKDOWN_UPLOAD_MODULE_NAME,
            enable_logging=config.enable_logging,
        )

    def run_until_idle(self) -> dict[str, object]:
        result = {
            "task": "markdown_upload",
            "status": "finished",
            "message": "Markdown 原文上传入口已处理到空闲并退出",
            "picked": 0,
            "success": 0,
            "failed": 0,
            "uploaded": 0,
            "skipped": 0,
        }
        offset = 0

        self.logger.info(
            "Markdown upload worker started: batch_size=%s, markdown_output_path=%s",
            self.config.batch_size,
            self.markdown_output_dir,
        )

        while True:
            file_records = self._fetch_parsed_markdown_files(offset)
            if not file_records:
                break

            result["picked"] = int(result["picked"]) + len(file_records)
            print(f"领取 {len(file_records)} 条 Markdown 上传记录")

            for file_record in file_records:
                one_result = self._process_one_file(file_record)
                if one_result.get("success"):
                    result["success"] = int(result["success"]) + 1
                    result["uploaded"] = int(result["uploaded"]) + 1
                elif one_result.get("skipped"):
                    result["skipped"] = int(result["skipped"]) + 1
                else:
                    result["failed"] = int(result["failed"]) + 1

            offset += len(file_records)

        self.logger.info("Markdown upload worker finished: %s", json.dumps(result, ensure_ascii=False))
        print(
            f"Markdown 上传结束：领取 {result['picked']}，"
            f"成功 {result['success']}，失败 {result['failed']}，跳过 {result['skipped']}"
        )
        return result

    def _fetch_parsed_markdown_files(self, offset: int) -> list[dict[str, Any]]:
        repository = RagFileRepository(self.config.db_config)
        try:
            return repository.fetch_parsed_markdown_files(self.config.batch_size, offset=offset)
        finally:
            repository.close()

    def _process_one_file(self, file_record: dict[str, Any]) -> dict[str, object]:
        file_id = int(file_record["id"])
        parse_path = Path(str(file_record.get("parse_path") or "")).expanduser()
        original_path = str(file_record.get("original_path") or "")

        print(f"上传中：id={file_id}, file={file_record.get('file_name', '')}")
        try:
            markdown_path = self._resolve_markdown_path(parse_path)
            if markdown_path is None:
                self.logger.warning(
                    "Markdown skipped because parse_path is outside MARKDOWN_OUTPUT_PATH: "
                    "file_id=%s, parse_path=%s, markdown_output_path=%s",
                    file_id,
                    parse_path,
                    self.markdown_output_dir,
                )
                print(f"跳过：id={file_id}, parse_path 不在 MARKDOWN_OUTPUT_PATH 下")
                return {"success": False, "skipped": True, "file_id": file_id}

            if not markdown_path.is_file():
                raise FileNotFoundError(f"Markdown file does not exist: {markdown_path}")
            if markdown_path.suffix.lower() not in {".md", ".markdown"}:
                self.logger.warning(
                    "Markdown skipped because parse_path is not a markdown file: "
                    "file_id=%s, markdown_path=%s",
                    file_id,
                    markdown_path,
                )
                print(f"跳过：id={file_id}, parse_path 不是 Markdown 文件")
                return {"success": False, "skipped": True, "file_id": file_id}

            markdown_text = markdown_path.read_text(encoding="utf-8")
            if not upload_text_to_lightrag(
                self.config.lightrag_server_url,
                markdown_text,
                original_path,
                self.logger,
            ):
                raise RuntimeError("LightRAG upload returned failure")

            self.logger.info(
                "Markdown uploaded without cleaning: file_id=%s, markdown_path=%s, original_path=%s",
                file_id,
                markdown_path,
                original_path,
            )
            print(f"完成：id={file_id}")
            return {"success": True, "file_id": file_id}
        except Exception as exc:
            self.logger.exception(
                "Markdown upload failed: file_record=%s, error=%s",
                json.dumps(file_record, ensure_ascii=False),
                exc,
            )
            print(f"失败：id={file_id}, error={exc}")
            return {"success": False, "file_id": file_id, "error": str(exc)}

    def _resolve_markdown_path(self, parse_path: Path) -> Path | None:
        candidate_paths = [parse_path]
        if not parse_path.is_absolute():
            candidate_paths.append(self.markdown_output_dir / parse_path)

        for candidate_path in candidate_paths:
            markdown_path = candidate_path.resolve()
            if _is_relative_to(markdown_path, self.markdown_output_dir):
                return markdown_path

        return None


def build_markdown_upload_task_config(config: dict[str, str]) -> MarkdownUploadTaskConfig:
    return MarkdownUploadTaskConfig(
        db_config=build_database_config(config),
        lightrag_server_url=get_required_config(config, "LIGHTRAG_SERVER_URL"),
        markdown_output_path=get_required_config(config, "MARKDOWN_OUTPUT_PATH"),
        batch_size=get_int_config(
            config,
            "MARKDOWN_UPLOAD_BATCH_SIZE",
            default=get_int_config(
                config,
                "CLEAN_TASK_BATCH_SIZE",
                default=DEFAULT_MARKDOWN_UPLOAD_BATCH_SIZE,
                min_value=1,
            ),
            min_value=1,
        ),
        enable_logging=get_bool_config(config, "ENABLE_LOGGING", True),
    )


def run_markdown_upload_task(config: MarkdownUploadTaskConfig) -> dict[str, object]:
    return MarkdownUploadWorker(config).run_until_idle()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
