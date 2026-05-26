import base64
import binascii
import json
import mimetypes
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.config import get_bool_config, get_int_config, get_required_config
from src.database import DatabaseConfig, build_database_config
from src.file_scanner.nas_scanner import SshConfig, build_remote_path_candidates, build_ssh_config
from src.logging_module import setup_single_file_module_logger
from src.repositories import RagFileRepository


MINERU_PARSE_MODULE_NAME = "mineru_parse"
FILE_PARSE_PATH = "/file_parse"
DEFAULT_PARSE_TASK_BATCH_SIZE = 5
DEFAULT_PARSE_REQUEST_CONCURRENCY = 2
DEFAULT_PARSE_REQUEST_BATCH_SIZE = 2
DEFAULT_PARSE_REQUEST_TIMEOUT_SECONDS = 300
LARGE_FILE_THRESHOLD_BYTES = 100 * 1024 * 1024
MINERU_PARSE_LANG_LIST = ("ch", "en")
MINERU_PARSE_METHOD = "auto"
RETURN_IMAGES = True

SUPPORTED_MINERU_FILE_TYPES = {"pdf", "docx", "xlsx", "pptx"}
TEXT_PASSTHROUGH_FILE_TYPES = {"txt"}


@dataclass(frozen=True)
class MineruParseConfig:
    db_config: DatabaseConfig
    ssh_config: SshConfig
    mineru_server_url: str
    markdown_output_path: str
    markdown_image_path: str
    task_batch_size: int = DEFAULT_PARSE_TASK_BATCH_SIZE
    request_concurrency: int = DEFAULT_PARSE_REQUEST_CONCURRENCY
    request_batch_size: int = DEFAULT_PARSE_REQUEST_BATCH_SIZE
    request_timeout_seconds: int = DEFAULT_PARSE_REQUEST_TIMEOUT_SECONDS
    enable_logging: bool = True


def build_mineru_parse_config(config: dict[str, str]) -> MineruParseConfig:
    """从 .env 构建独立 MinerU 解析模块所需的全部配置。"""
    return MineruParseConfig(
        db_config=build_database_config(config),
        ssh_config=build_ssh_config(config),
        mineru_server_url=get_required_config(config, "MINERU_SERVER_URL"),
        markdown_output_path=get_required_config(config, "MARKDOWN_OUTPUT_PATH"),
        markdown_image_path=get_required_config(config, "MARKDOWN_IMAGE_PATH"),
        task_batch_size=get_int_config(config, "PARSE_TASK_BATCH_SIZE", default=5, min_value=1),
        request_concurrency=get_int_config(
            config,
            "PARSE_REQUEST_CONCURRENCY",
            default=DEFAULT_PARSE_REQUEST_CONCURRENCY,
            min_value=1,
        ),
        request_batch_size=get_int_config(
            config,
            "PARSE_REQUEST_BATCH_SIZE",
            default=DEFAULT_PARSE_REQUEST_BATCH_SIZE,
            min_value=1,
        ),
        request_timeout_seconds=get_int_config(
            config,
            "PARSE_REQUEST_TIMEOUT_SECONDS",
            default=DEFAULT_PARSE_REQUEST_TIMEOUT_SECONDS,
            min_value=1,
        ),
        enable_logging=get_bool_config(config, "ENABLE_LOGGING", True),
    )


class MineruParseWorker:
    """独立 MinerU 解析执行器。

    该类只负责解析阶段：领取 rag_files 待处理记录、通过 SSH/SFTP 只读获取 NAS 文件、
    调用 MinerU、保存 markdown 和图片、更新 parse_status/parse_path。
    """

    def __init__(self, config: MineruParseConfig) -> None:
        self.config = config
        self.logger = setup_single_file_module_logger(
            MINERU_PARSE_MODULE_NAME,
            enable_logging=config.enable_logging,
        )

    def run_until_idle(self) -> dict[str, int]:
        """持续执行解析批次，直到当前进程领不到可执行任务后自动退出。"""
        total_result = {
            "cycles": 0,
            "picked": 0,
            "downloaded": 0,
            "success": 0,
            "failed": 0,
            "unsupported": 0,
            "passthrough": 0,
        }

        while True:
            cycle_result = self.run_once()
            if cycle_result["picked"] == 0:
                break

            total_result["cycles"] += 1
            for key in ("picked", "downloaded", "success", "failed", "unsupported", "passthrough"):
                total_result[key] += cycle_result[key]

        print(
            "解析汇总："
            f"批次 {total_result['cycles']}，领取 {total_result['picked']}，"
            f"成功 {total_result['success']}，失败 {total_result['failed']}，"
            f"txt 直通 {total_result['passthrough']}，不支持 {total_result['unsupported']}"
        )
        self.logger.info("Parse worker exited because no executable data remains: %s", total_result)
        return total_result

    def run_once(self) -> dict[str, int]:
        """执行一轮解析，适合被命令行入口或外部调度器反复调用。"""
        result = {
            "picked": 0,
            "downloaded": 0,
            "success": 0,
            "failed": 0,
            "unsupported": 0,
            "passthrough": 0,
        }
        claimed_files = self._claim_parse_files()
        result["picked"] = len(claimed_files)

        if not claimed_files:
            print("解析进度：没有待解析文件")
            self.logger.info("No pending parse files.")
            return result

        print(f"解析进度：已领取 {len(claimed_files)} 条任务")
        self.logger.info("Claimed parse files: ids=%s", [item["id"] for item in claimed_files])

        mineru_files, text_files, unsupported_files = self._split_parse_files(claimed_files)
        if text_files:
            passthrough_success, passthrough_failed = self._mark_text_files_success(text_files)
            result["passthrough"] = passthrough_success
            result["success"] += passthrough_success
            result["failed"] += passthrough_failed

        if unsupported_files:
            result["unsupported"] = len(unsupported_files)
            result["failed"] += len(unsupported_files)
            self._mark_files_failed(unsupported_files, reason="unsupported file type")

        if not mineru_files:
            self._print_progress(result)
            return result

        with tempfile.TemporaryDirectory(prefix="mineru_parse_") as temp_dir:
            local_files = self._download_from_nas(mineru_files, Path(temp_dir))
            result["downloaded"] = len(local_files)

            missing_files = [
                file_record for file_record in mineru_files if int(file_record["id"]) not in local_files
            ]
            if missing_files:
                self._mark_files_failed(missing_files, reason="remote file missing")
                result["failed"] += len(missing_files)

            if local_files:
                success, failed = self._request_and_save(local_files)
                result["success"] += success
                result["failed"] += failed

        self._print_progress(result)
        self.logger.info("Parse run result: %s", json.dumps(result, ensure_ascii=False))
        return result

    def _claim_parse_files(self) -> list[dict[str, Any]]:
        repository = RagFileRepository(self.config.db_config)
        try:
            rows = repository.claim_pending_parse_files(self.config.task_batch_size)
            repository.commit()
            return rows
        except Exception:
            repository.rollback()
            self.logger.exception("Claim parse files failed.")
            raise
        finally:
            repository.close()

    def _split_parse_files(
        self,
        files: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        mineru_files: list[dict[str, Any]] = []
        text_files: list[dict[str, Any]] = []
        unsupported_files: list[dict[str, Any]] = []

        for file_record in files:
            file_ext = str(file_record.get("file_ext") or "").lower().lstrip(".")
            if file_ext in SUPPORTED_MINERU_FILE_TYPES:
                mineru_files.append(file_record)
            elif file_ext in TEXT_PASSTHROUGH_FILE_TYPES:
                text_files.append(file_record)
            else:
                unsupported_files.append(file_record)

        return mineru_files, text_files, unsupported_files

    def _mark_text_files_success(self, files: list[dict[str, Any]]) -> tuple[int, int]:
        """txt 文件不调用 MinerU，parse_path 直接指向 original_path。"""
        success = 0
        failed = 0
        repository = RagFileRepository(self.config.db_config)
        try:
            for file_record in files:
                file_id = int(file_record["id"])
                original_path = str(file_record.get("original_path") or "").strip()
                if original_path:
                    repository.update_parse_success(file_id, original_path)
                    success += 1
                else:
                    repository.update_parse_failed(file_id)
                    failed += 1
                    self.logger.warning("Text passthrough missing original_path: file_id=%s", file_id)
            repository.commit()
        except Exception:
            repository.rollback()
            self.logger.exception("Text passthrough update failed.")
            raise
        finally:
            repository.close()
        return success, failed

    def _download_from_nas(
        self,
        files: list[dict[str, Any]],
        temp_dir: Path,
    ) -> dict[int, dict[str, str]]:
        """只读下载 NAS 文件到本地临时目录，临时文件会在本轮结束后自动清理。"""
        ssh_client = None
        sftp_client = None
        local_files: dict[int, dict[str, str]] = {}

        try:
            ssh_client = self._create_ssh_client()
            sftp_client = ssh_client.open_sftp()

            for index, file_record in enumerate(files, start=1):
                file_id = int(file_record["id"])
                original_path = str(file_record.get("original_path") or "")
                try:
                    remote_path = self._resolve_remote_path(sftp_client, original_path)
                    local_path = self._download_one_file(sftp_client, file_record, remote_path, temp_dir)
                    local_files[file_id] = self._build_request_file(file_record, local_path)
                    print(f"解析进度：NAS 下载 {index}/{len(files)}，id={file_id}")
                except Exception as exc:
                    self.logger.exception(
                        "Download NAS file failed: file_id=%s, original_path=%s, error=%s",
                        file_id,
                        original_path,
                        exc,
                    )
        finally:
            if sftp_client is not None:
                sftp_client.close()
            if ssh_client is not None:
                ssh_client.close()

        return local_files

    def _resolve_remote_path(self, sftp_client: Any, original_path: str) -> str:
        for candidate_path in build_remote_path_candidates(original_path):
            try:
                sftp_client.stat(candidate_path)
                return candidate_path
            except OSError:
                continue
        raise FileNotFoundError(f"NAS file not found by any candidate path: {original_path}")

    def _download_one_file(
        self,
        sftp_client: Any,
        file_record: dict[str, Any],
        remote_path: str,
        temp_dir: Path,
    ) -> Path:
        file_id = int(file_record["id"])
        file_name = str(file_record.get("file_name") or PurePosixPath(remote_path).name)
        target_dir = temp_dir / str(file_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = target_dir / _safe_local_filename(file_name, file_id)
        sftp_client.get(remote_path, str(local_path))
        return local_path

    def _build_request_file(self, file_record: dict[str, Any], local_path: Path) -> dict[str, str]:
        return {
            "id": str(file_record["id"]),
            "file_uid": str(file_record.get("file_uid") or ""),
            "file_name": str(file_record.get("file_name") or local_path.name),
            "upload_name": str(file_record.get("file_name") or local_path.name),
            "file_ext": str(file_record.get("file_ext") or local_path.suffix.lstrip(".")),
            "file_size": str(file_record.get("file_size") or local_path.stat().st_size),
            "file_hash": str(file_record.get("file_hash") or ""),
            "original_path": str(file_record.get("original_path") or ""),
            "absolute_path": str(local_path),
        }

    def _request_and_save(self, files_by_id: dict[int, dict[str, str]]) -> tuple[int, int]:
        request_files = list(files_by_id.values())
        parse_results = request_mineru_parse(
            request_files,
            mineru_server_url=self.config.mineru_server_url,
            image_output_path=self.config.markdown_image_path,
            enable_logging=self.config.enable_logging,
            request_timeout_seconds=self.config.request_timeout_seconds,
            parse_request_concurrency=self.config.request_concurrency,
            parse_request_batch_size=self.config.request_batch_size,
        )

        success = 0
        failed = 0
        handled_ids: set[int] = set()
        repository = RagFileRepository(self.config.db_config)
        try:
            for parse_result in parse_results:
                file_id = int(parse_result["id"])
                handled_ids.add(file_id)
                markdown_content = parse_result.get("markdown_content")
                if parse_result.get("parse_status") != "success" or not _has_text(markdown_content):
                    repository.update_parse_failed(file_id)
                    failed += 1
                    self.logger.warning(
                        "MinerU parse failed: file_id=%s, file_name=%s, status_code=%s",
                        file_id,
                        parse_result.get("file_name"),
                        parse_result.get("status_code"),
                    )
                    continue

                file_info = files_by_id[file_id]
                try:
                    parse_path = self._save_markdown(str(markdown_content), file_info)
                    repository.update_parse_success(file_id, parse_path)
                    success += 1
                except Exception as exc:
                    repository.update_parse_failed(file_id)
                    failed += 1
                    self.logger.exception(
                        "Save markdown failed: file_id=%s, file_name=%s, error=%s",
                        file_id,
                        file_info.get("file_name"),
                        exc,
                    )

            missing_result_ids = set(files_by_id) - handled_ids
            for file_id in missing_result_ids:
                repository.update_parse_failed(file_id)
                failed += 1
                self.logger.warning("MinerU response missing file result: file_id=%s", file_id)

            repository.commit()
        except Exception:
            repository.rollback()
            self.logger.exception("Save parse result failed.")
            raise
        finally:
            repository.close()

        return success, failed

    def _save_markdown(self, markdown_content: str, file_info: dict[str, str]) -> str:
        output_dir = Path(self.config.markdown_output_path).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        file_hash = file_info.get("file_hash") or str(file_info["id"])
        output_file = output_dir / f"{file_hash}.md"
        output_file.write_text(markdown_content, encoding="utf-8")
        return str(output_file)

    def _mark_files_failed(self, files: list[dict[str, Any]], reason: str) -> None:
        if not files:
            return

        repository = RagFileRepository(self.config.db_config)
        try:
            for file_record in files:
                repository.update_parse_failed(int(file_record["id"]))
            repository.commit()
            self.logger.warning(
                "Marked parse files failed: reason=%s, ids=%s",
                reason,
                [item["id"] for item in files],
            )
        except Exception:
            repository.rollback()
            self.logger.exception("Mark parse files failed failed: reason=%s", reason)
            raise
        finally:
            repository.close()

    def _create_ssh_client(self) -> Any:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("缺少 SSH 依赖，请先安装 paramiko。") from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.ssh_config.host,
            port=self.config.ssh_config.port,
            username=self.config.ssh_config.user,
            password=self.config.ssh_config.password,
            timeout=self.config.ssh_config.timeout,
            banner_timeout=self.config.ssh_config.timeout,
            auth_timeout=self.config.ssh_config.timeout,
        )
        return client

    def _print_progress(self, result: dict[str, int]) -> None:
        print(
            "解析进度："
            f"领取 {result['picked']}，下载 {result['downloaded']}，"
            f"成功 {result['success']}，失败 {result['failed']}，"
            f"txt 直通 {result['passthrough']}，不支持 {result['unsupported']}"
        )


def request_mineru_parse(
    files: list[dict[str, str]],
    mineru_server_url: str,
    image_output_path: str,
    enable_logging: bool = True,
    request_timeout_seconds: int = DEFAULT_PARSE_REQUEST_TIMEOUT_SECONDS,
    parse_request_concurrency: int = DEFAULT_PARSE_REQUEST_CONCURRENCY,
    parse_request_batch_size: int = DEFAULT_PARSE_REQUEST_BATCH_SIZE,
) -> list[dict[str, object]]:
    """请求 MinerU /file_parse，并在内存中返回 markdown 内容。"""
    logger = setup_single_file_module_logger(
        MINERU_PARSE_MODULE_NAME,
        enable_logging=enable_logging,
    )

    if not files:
        logger.info("No files pending parse; skip MinerU request.")
        return []

    max_workers = max(1, parse_request_concurrency)
    batch_size = max(1, parse_request_batch_size)
    file_batches = _chunk_files(files, batch_size)
    logger.info(
        "MinerU request concurrency=%s, batch_size=%s, timeout_seconds=%s",
        max_workers,
        batch_size,
        request_timeout_seconds,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(
                _request_mineru_parse_batch,
                file_batch,
                mineru_server_url,
                image_output_path,
                logger,
                index + 1,
                len(file_batches),
                request_timeout_seconds,
            ): index
            for index, file_batch in enumerate(file_batches)
        }

        indexed_results: list[tuple[int, list[dict[str, object]]]] = []
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            indexed_results.append((index, future.result()))

    parse_results: list[dict[str, object]] = []
    for _, batch_results in sorted(indexed_results, key=lambda item: item[0]):
        parse_results.extend(batch_results)

    return parse_results


def _request_mineru_parse_batch(
    files: list[dict[str, str]],
    mineru_server_url: str,
    image_output_path: str,
    logger,
    batch_index: int,
    batch_count: int,
    request_timeout_seconds: int,
) -> list[dict[str, object]]:
    print(f"正在解析文件批次 {batch_index}/{batch_count}，文件数：{len(files)}")
    endpoint = _build_file_parse_url(mineru_server_url)
    request_fields = _build_parse_fields()
    request_files = _build_request_files(files)
    file_names = _get_file_names(files)

    logger.info(
        "MinerU request files: %s",
        json.dumps(
            {
                "batch_index": batch_index,
                "batch_count": batch_count,
                "file_names": file_names,
            },
            ensure_ascii=False,
        ),
    )

    try:
        status_code, response_text = _post_multipart(
            endpoint,
            fields=request_fields,
            files=request_files,
            timeout_seconds=request_timeout_seconds,
        )
        response_json = _try_load_json(response_text)
        response_success = _is_response_success(status_code)
        logger.info(
            "MinerU response result: %s",
            json.dumps(
                {
                    "batch_index": batch_index,
                    "status_code": status_code,
                    "success": response_success,
                },
                ensure_ascii=False,
            ),
        )

        return [
            _build_parse_result(
                file_info,
                "success",
                status_code,
                response_success,
                _get_markdown_content_from_response(
                    response_json,
                    file_info,
                    image_output_path,
                    logger,
                ),
            )
            for file_info in files
        ]
    except (OSError, HTTPError, URLError, ValueError) as exc:
        logger.exception("MinerU request failed: file_names=%s, error=%s", file_names, exc)
        return [_build_parse_result(file_info, "failed", None, False, None) for file_info in files]


def _chunk_files(files: list[dict[str, str]], batch_size: int) -> list[list[dict[str, str]]]:
    file_batches: list[list[dict[str, str]]] = []
    current_batch: list[dict[str, str]] = []

    for file_info in files:
        if _is_large_file(file_info):
            if current_batch:
                file_batches.append(current_batch)
                current_batch = []
            file_batches.append([file_info])
            continue

        current_batch.append(file_info)
        if len(current_batch) >= batch_size:
            file_batches.append(current_batch)
            current_batch = []

    if current_batch:
        file_batches.append(current_batch)

    return file_batches


def _is_large_file(file_info: dict[str, str]) -> bool:
    try:
        file_size = int(file_info.get("file_size") or 0)
    except (TypeError, ValueError):
        return False
    return file_size > LARGE_FILE_THRESHOLD_BYTES


def _get_file_names(files: list[dict[str, str]]) -> list[str]:
    return [str(file_info.get("file_name") or Path(file_info["absolute_path"]).name) for file_info in files]


def _is_response_success(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def _get_markdown_content_from_response(
    response: object,
    file_info: dict[str, str],
    image_output_path: str,
    logger,
) -> str | None:
    result_item = _extract_result_item(response, file_info)
    markdown_content = _get_markdown_from_result_item(result_item)
    if not markdown_content:
        logger.warning(
            "MinerU markdown not found: %s",
            json.dumps({"file_name": file_info.get("file_name", "")}, ensure_ascii=False),
        )
        return None
    image_name_map = _save_images_from_result_item(result_item, file_info, image_output_path, logger)
    return _replace_markdown_image_names(markdown_content, image_name_map)


def _extract_result_item(response: object, file_info: dict[str, str]) -> object:
    if not isinstance(response, dict):
        return None

    results = response.get("results") or response.get("result")
    if isinstance(results, dict):
        return _find_result_item_from_dict(results, file_info)

    if isinstance(results, list):
        return _find_result_item_from_list(results, file_info)

    return None


def _save_images_from_result_item(
    result_item: object,
    file_info: dict[str, str],
    image_output_path: str,
    logger,
) -> dict[str, str]:
    images = _get_images_from_result_item(result_item)
    if not images:
        return {}

    file_id = str(file_info["id"])
    target_dir = Path(image_output_path).expanduser().resolve() / file_id
    target_dir.mkdir(parents=True, exist_ok=True)

    image_name_map: dict[str, str] = {}
    for image_name, image_content in images.items():
        original_name = Path(str(image_name)).name
        if not original_name:
            continue
        if isinstance(image_content, dict):
            image_content = (
                image_content.get("content")
                or image_content.get("base64")
                or image_content.get("data")
                or image_content.get("image_base64")
                or ""
            )

        try:
            image_bytes = _decode_base64_image(str(image_content))
        except ValueError as exc:
            logger.warning(
                "MinerU image decode failed: %s",
                json.dumps(
                    {
                        "file_name": file_info.get("file_name", ""),
                        "image_name": original_name,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
            )
            continue

        safe_name = _build_safe_image_name(original_name)
        (target_dir / safe_name).write_bytes(image_bytes)
        image_name_map[original_name] = f"{file_id}/{safe_name}"

    if image_name_map:
        logger.info(
            "MinerU images saved: %s",
            json.dumps(
                {
                    "file_name": file_info.get("file_name", ""),
                    "image_count": len(image_name_map),
                    "output_dir": str(target_dir),
                },
                ensure_ascii=False,
            ),
        )
    return image_name_map


def _get_images_from_result_item(result_item: object) -> dict[str, object]:
    if not isinstance(result_item, dict):
        return {}

    images = result_item.get("images")
    if isinstance(images, dict):
        return images
    if isinstance(images, list):
        normalized_images: dict[str, object] = {}
        for index, item in enumerate(images, start=1):
            if isinstance(item, dict):
                image_name = (
                    item.get("image_name")
                    or item.get("file_name")
                    or item.get("filename")
                    or item.get("name")
                    or f"image_{index}.png"
                )
                image_content = (
                    item.get("content")
                    or item.get("base64")
                    or item.get("data")
                    or item.get("image_base64")
                )
                if image_content:
                    normalized_images[str(image_name)] = image_content
            elif isinstance(item, str):
                normalized_images[f"image_{index}.png"] = item
        return normalized_images
    return {}


def _decode_base64_image(image_content: str) -> bytes:
    content = image_content.strip()
    if "," in content and content.lower().startswith("data:"):
        content = content.split(",", 1)[1]
    try:
        normalized_content = "".join(content.split())
        return base64.b64decode(normalized_content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image content") from exc


def _build_safe_image_name(image_name: str) -> str:
    """保留 MinerU 返回的图片文件名，只去掉可能携带的目录部分。"""
    return Path(image_name).name


def _replace_markdown_image_names(markdown_content: str, image_name_map: dict[str, str]) -> str:
    replaced_content = markdown_content
    for old_name, new_name in image_name_map.items():
        replaced_content = replaced_content.replace(old_name, new_name)
    return replaced_content


def _find_result_item_from_dict(results: dict[str, object], file_info: dict[str, str]) -> object:
    file_path = Path(file_info["absolute_path"])
    candidate_keys = (
        file_info.get("file_uid", ""),
        file_info["file_name"],
        file_path.name,
        file_path.stem,
        str(file_path),
    )

    for key in candidate_keys:
        if key in results:
            return results[key]

    if len(results) == 1:
        return next(iter(results.values()))

    return None


def _find_result_item_from_list(results: list[object], file_info: dict[str, str]) -> object:
    file_name = file_info["file_name"]
    file_uid = file_info.get("file_uid", "")
    file_stem = Path(file_name).stem

    for item in results:
        if not isinstance(item, dict):
            continue

        item_name = str(
            item.get("file_name")
            or item.get("filename")
            or item.get("name")
            or item.get("file_uid")
            or item.get("uid")
            or item.get("original_file_name")
            or ""
        )
        if item_name in {file_uid, file_name, file_stem}:
            return item

    if len(results) == 1:
        return results[0]

    return None


def _get_markdown_from_result_item(result_item: object) -> str | None:
    if isinstance(result_item, str):
        return result_item

    if not isinstance(result_item, dict):
        return None

    for key in ("md_content", "markdown", "markdown_content", "md", "content"):
        value = result_item.get(key)
        if isinstance(value, str) and value.strip():
            return value

    return None


def _build_parse_fields() -> list[tuple[str, str]]:
    fields = [
        ("backend", "hybrid-auto-engine"),
        ("parse_method", MINERU_PARSE_METHOD),
        ("formula_enable", "true"),
        ("table_enable", "true"),
        ("image_analysis", "true"),
        ("return_md", "true"),
        ("return_middle_json", "false"),
        ("return_model_output", "false"),
        ("return_content_list", "false"),
        ("return_images", str(RETURN_IMAGES).lower()),
        ("response_format_zip", "false"),
        ("return_original_file", "false"),
        ("start_page_id", "0"),
        ("end_page_id", "99999"),
    ]

    for lang in MINERU_PARSE_LANG_LIST:
        fields.append(("lang_list", lang))

    return fields


def _build_request_files(files: list[dict[str, str]]) -> list[tuple[str, Path, str]]:
    return [
        (
            "files",
            Path(file_info["absolute_path"]),
            str(
                file_info.get("upload_name")
                or file_info.get("file_name")
                or Path(file_info["absolute_path"]).name
            ),
        )
        for file_info in files
    ]


def _post_multipart(
    url: str,
    fields: list[tuple[str, str]],
    files: list[tuple[str, Path, str]],
    timeout_seconds: int = DEFAULT_PARSE_REQUEST_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    boundary = f"----ai-knowledge-rag-{uuid.uuid4().hex}"
    body = _build_multipart_body(boundary, fields, files)
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout_seconds) as response:
        response_text = response.read().decode("utf-8", errors="replace")
        return response.status, response_text


def _build_multipart_body(
    boundary: str,
    fields: list[tuple[str, str]],
    files: list[tuple[str, Path, str]],
) -> bytes:
    body = bytearray()

    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{value}\r\n".encode("utf-8"))

    for field_name, file_path, upload_name in files:
        if not file_path.exists():
            raise FileNotFoundError(f"MinerU upload file does not exist: {file_path}")

        mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{upload_name}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body)


def _build_file_parse_url(mineru_server_url: str) -> str:
    return f"{mineru_server_url.rstrip('/')}{FILE_PARSE_PATH}"


def _try_load_json(response_text: str) -> object:
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return response_text


def _build_parse_result(
    file_info: dict[str, str],
    parse_status: str,
    status_code: int | None,
    response_success: bool,
    markdown_content: str | None,
) -> dict[str, object]:
    return {
        "file_type": file_info.get("file_ext", ""),
        "id": file_info.get("id"),
        "file_uid": file_info.get("file_uid"),
        "file_name": file_info["file_name"],
        "absolute_path": file_info["absolute_path"],
        "parse_status": parse_status,
        "status_code": status_code,
        "response_success": response_success,
        "markdown_content": markdown_content,
    }


def _safe_local_filename(file_name: str, file_id: int) -> str:
    """生成 Windows 本地临时文件名；上传给 MinerU 时仍使用原始 file_name。"""
    path = PurePosixPath(file_name)
    suffix = path.suffix
    stem = path.stem or f"file_{file_id}"
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or f"file_{file_id}"
    return f"{safe_stem}{suffix}"


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
