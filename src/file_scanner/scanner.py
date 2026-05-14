import hashlib
import time
import uuid
from pathlib import Path
from threading import Event
from typing import Any

from src.database import DatabaseConfig
from src.repositories import RagFileRepository


SUPPORTED_FILE_TYPES = ("pdf", "docx", "xlsx", "txt", "md", "markdown")
FILE_HASH_CHUNK_SIZE = 1024 * 1024

# Office 打开文档时会生成 ~$ 开头的临时文件，这类文件不是有效入库源文件，固定跳过。
OFFICE_TEMP_PREFIX = "~$"
OFFICE_FILE_TYPES = ("docx", "xlsx")


def scan_files_by_type(
    scan_input_path: str,
    ignored_file_types: set[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """扫描指定目录，并按支持的文件类型归集文件名称和绝对路径。"""
    scan_root = Path(scan_input_path).expanduser().resolve()
    ignored_types = ignored_file_types or set()

    if not scan_root.exists():
        raise FileNotFoundError(f"Scan input path does not exist: {scan_root}")
    if not scan_root.is_dir():
        raise NotADirectoryError(f"Scan input path is not a directory: {scan_root}")

    scan_result: dict[str, list[dict[str, str]]] = {
        file_type: [] for file_type in SUPPORTED_FILE_TYPES
    }

    # 使用递归扫描，保证子目录中的企业文件也能纳入批处理范围。
    for path in sorted(scan_root.rglob("*")):
        if not path.is_file():
            continue

        file_type = path.suffix.lower().lstrip(".")
        # 只采集当前流程支持的文件类型，其他文件先忽略。
        if file_type not in scan_result:
            continue
        # 配置中声明忽略的文件类型不进入后续流程。
        if file_type in ignored_types:
            continue
        if _is_office_temp_file(path, file_type):
            continue

        scan_result[file_type].append(
            {
                "file_name": path.name,
                "absolute_path": str(path.resolve()),
            }
        )

    return scan_result


def scan_files_to_database(
    scan_input_path: str,
    db_config: DatabaseConfig,
    ignored_file_types: set[str] | None = None,
    stop_event: Event | None = None,
) -> dict[str, int]:
    """扫描文件并写入 rag_files 表；已存在相同 file_hash 的文件不重复插入。"""
    scan_root = Path(scan_input_path).expanduser().resolve()
    ignored_types = ignored_file_types or set()

    if not scan_root.exists():
        raise FileNotFoundError(f"Scan input path does not exist: {scan_root}")
    if not scan_root.is_dir():
        raise NotADirectoryError(f"Scan input path is not a directory: {scan_root}")

    result = {
        "scanned": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_unsupported": 0,
        "skipped_ignored": 0,
        "skipped_temp": 0,
    }

    repository = RagFileRepository(db_config)
    try:
        try:
            for path in sorted(scan_root.rglob("*")):
                if stop_event and stop_event.is_set():
                    result["stopped"] = 1
                    break
                if not path.is_file():
                    continue

                file_ext = path.suffix.lower().lstrip(".")
                if file_ext not in SUPPORTED_FILE_TYPES:
                    result["skipped_unsupported"] += 1
                    continue
                if file_ext in ignored_types:
                    result["skipped_ignored"] += 1
                    continue
                if _is_office_temp_file(path, file_ext):
                    result["skipped_temp"] += 1
                    continue

                result["scanned"] += 1
                file_record = _build_file_record(path, file_ext)
                if repository.file_hash_exists(file_record["file_hash"]):
                    result["skipped_existing"] += 1
                    continue

                repository.insert_file(file_record)
                result["inserted"] += 1

            repository.commit()
        except Exception:
            repository.rollback()
            raise
    finally:
        repository.close()

    return result


def _is_office_temp_file(path: Path, file_type: str) -> bool:
    """判断是否为 Office 临时文件；该规则强制生效，不依赖配置开关。"""
    return file_type in OFFICE_FILE_TYPES and path.name.startswith(OFFICE_TEMP_PREFIX)


def _build_file_record(path: Path, file_ext: str) -> dict[str, Any]:
    absolute_path = path.resolve()
    file_hash = _calculate_file_hash(absolute_path)
    current_timestamp = int(time.time())

    return {
        "file_name": path.name,
        "file_uid": _generate_file_uid(file_hash, str(absolute_path)),
        "file_ext": file_ext,
        "file_size": path.stat().st_size,
        "file_hash": file_hash,
        "original_path": str(absolute_path),
        "created_at": current_timestamp,
        "updated_at": current_timestamp,
    }


def _calculate_file_hash(path: Path) -> str:
    hash_builder = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(FILE_HASH_CHUNK_SIZE), b""):
            hash_builder.update(chunk)
    return hash_builder.hexdigest()


def _generate_file_uid(file_hash: str, original_path: str) -> str:
    """基于文件内容和原始路径生成稳定唯一值，便于后续建立一对一绑定关系。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{file_hash}:{original_path}").hex
