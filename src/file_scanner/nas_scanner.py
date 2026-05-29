import hashlib
import posixpath
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from src.config import get_int_config, get_required_config
from src.database import create_mysql_connection


DEFAULT_SCAN_BATCH_SIZE = 1000
SUPPORTED_FILE_TYPES = {"pdf", "docx", "xlsx", "pptx", "txt", "md", "markdown"}
OFFICE_TEMP_PREFIX = "~$"
OFFICE_FILE_TYPES = {"docx", "xlsx", "pptx"}


@dataclass(frozen=True)
class SshConfig:
    """NAS SSH 连接配置。

    扫描和解析模块都会使用这组配置连接群晖 NAS。这里不保存连接对象，
    只保存连接参数，避免长生命周期对象在多进程场景里被错误共享。
    """

    host: str
    port: int
    user: str
    password: str
    timeout: int


@dataclass(frozen=True)
class RemoteFileInfo:
    """NAS 上单个真实文件的元数据。

    resolved_path 是经过容错定位后实际可访问的路径；original_path 仍会按数据库
    nas_files.full_path 原样写入 rag_files，方便后续追踪来源。
    """

    resolved_path: str
    file_name: str
    file_ext: str
    file_size: int
    file_hash: str


class NasFileScanner:
    """扫描 nas_files 表，并把可入库文件登记到 rag_files 表。

    扫描模块只做“读取 NAS 元数据并登记任务”，不解析文件、不清洗内容、不调用知识库。
    这样每个阶段都有清晰边界，失败后也能通过数据库状态继续追踪。
    """

    def __init__(
        self,
        db_config: Any,
        ssh_config: SshConfig,
        batch_size: int,
        logger: Any,
    ) -> None:
        self.db_config = db_config
        self.ssh_config = ssh_config
        self.batch_size = batch_size
        self.logger = logger

    def scan_to_rag_files(self) -> dict[str, int]:
        """执行完整扫描流程。

        使用 id > last_id 的 keyset 分页方式读取 nas_files，避免大表 OFFSET 分页越往后
        越慢的问题。每一批处理完成后提交事务，保证长批处理过程中已有结果可以落库，
        也避免单个异常影响整轮扫描。
        """
        result = {
            "fetched": 0,
            "checked": 0,
            "inserted": 0,
            "skipped_existing": 0,
            "skipped_unsupported": 0,
            "skipped_temp": 0,
            "missing_remote": 0,
            "failed": 0,
        }

        connection = None
        ssh_client = None
        sftp_client = None

        try:
            connection = create_mysql_connection(self.db_config)
            ssh_client = self._create_ssh_client()
            sftp_client = ssh_client.open_sftp()

            last_id = 0
            while True:
                rows = self._fetch_nas_files_batch(connection, last_id)
                if not rows:
                    break

                result["fetched"] += len(rows)
                last_id = int(rows[-1]["id"])

                # 批量查询本批 full_path 是否已经进入 rag_files，减少逐条 SQL 查询。
                existing_original_paths = self._fetch_existing_original_paths(
                    connection,
                    [str(row.get("full_path") or "").strip() for row in rows],
                )

                for row in rows:
                    result["checked"] += 1
                    try:
                        status_name = self._process_one_nas_file(
                            connection,
                            ssh_client,
                            sftp_client,
                            row,
                            existing_original_paths,
                        )
                        result[status_name] += 1
                    except Exception as exc:
                        result["failed"] += 1
                        self.logger.exception(
                            "单文件扫描失败：nas_file_id=%s, path=%s, error=%s",
                            row.get("id"),
                            row.get("full_path"),
                            exc,
                        )

                connection.commit()
                self._print_progress(result)
                self.logger.info(
                    "扫描进度 fetched=%s checked=%s inserted=%s existing=%s missing=%s failed=%s",
                    result["fetched"],
                    result["checked"],
                    result["inserted"],
                    result["skipped_existing"],
                    result["missing_remote"],
                    result["failed"],
                )

        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if sftp_client is not None:
                sftp_client.close()
            if ssh_client is not None:
                ssh_client.close()
            if connection is not None:
                connection.close()

        return result

    def _process_one_nas_file(
        self,
        connection: Any,
        ssh_client: Any,
        sftp_client: Any,
        nas_file: dict[str, Any],
        existing_original_paths: set[str],
    ) -> str:
        """处理一条 nas_files 记录，并返回 result 字典中的统计键名。

        这里会先做本地可判断的过滤：空路径、不支持的扩展名、Office 临时文件、已入库路径。
        只有确实需要登记的新文件，才访问 NAS 读取 stat 和 hash，减少远程 IO。
        """
        original_path = str(nas_file.get("full_path") or "").strip()
        if not original_path:
            return "missing_remote"

        file_ext = _get_file_ext(original_path)
        if file_ext not in SUPPORTED_FILE_TYPES:
            return "skipped_unsupported"
        if _is_office_temp_file(original_path, file_ext):
            return "skipped_temp"
        if original_path in existing_original_paths:
            return "skipped_existing"

        remote_info = self._read_remote_file_info(ssh_client, sftp_client, original_path)
        if remote_info is None:
            return "missing_remote"

        # 保留当前项目按 file_hash 去重的规则，避免同一内容重复入库。
        if self._rag_file_exists_by_hash(connection, remote_info.file_hash):
            existing_original_paths.add(original_path)
            return "skipped_existing"

        now = int(time.time())
        self._insert_rag_file(
            connection,
            {
                "file_name": remote_info.file_name,
                "file_uid": _generate_file_uid(remote_info.file_hash, original_path),
                "file_ext": remote_info.file_ext,
                "file_size": remote_info.file_size,
                "file_hash": remote_info.file_hash,
                "original_path": original_path,
                "created_at": now,
                "updated_at": now,
            },
        )
        existing_original_paths.add(original_path)
        return "inserted"

    def _fetch_nas_files_batch(self, connection: Any, last_id: int) -> list[dict[str, Any]]:
        """从 nas_files 读取一批已完成采集的 NAS 文件路径。"""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, full_path
                FROM nas_files
                WHERE status = 3
                    AND id > %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (last_id, self.batch_size),
            )
            return list(cursor.fetchall())

    def _fetch_existing_original_paths(
        self,
        connection: Any,
        original_paths: list[str],
    ) -> set[str]:
        """批量查询 rag_files 已存在的 original_path。"""
        normalized_paths = _deduplicate([path for path in original_paths if path])
        if not normalized_paths:
            return set()

        placeholders = ", ".join(["%s"] * len(normalized_paths))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT original_path
                FROM rag_files
                WHERE original_path IN ({placeholders})
                """,
                tuple(normalized_paths),
            )
            return {str(row["original_path"]) for row in cursor.fetchall()}

    def _read_remote_file_info(
        self,
        ssh_client: Any,
        sftp_client: Any,
        original_path: str,
    ) -> RemoteFileInfo | None:
        """定位 NAS 文件并读取元数据。

        群晖数据库里的 full_path 常见形式是 /volume1/share/a.pdf，但 SSH 用户可能被限制
        在共享目录视角下，只能看到 /share/a.pdf 或 share/a.pdf。这里会生成多种候选路径，
        逐个 stat；任何候选是普通文件，就认为定位成功。
        """
        for candidate_path in build_remote_path_candidates(original_path):
            try:
                remote_stat = sftp_client.stat(candidate_path)
            except OSError:
                continue

            if not stat.S_ISREG(remote_stat.st_mode):
                continue

            file_hash = self._calculate_remote_sha256(ssh_client, sftp_client, candidate_path)
            path = PurePosixPath(candidate_path)
            return RemoteFileInfo(
                resolved_path=candidate_path,
                file_name=path.name,
                file_ext=_get_file_ext(path.name),
                file_size=int(remote_stat.st_size),
                file_hash=file_hash,
            )

        return None

    def _calculate_remote_sha256(
        self,
        ssh_client: Any,
        sftp_client: Any,
        remote_path: str,
    ) -> str:
        """计算远程文件 sha256。

        通过 SFTP 分块读取计算 hash。部分 NAS 账号会把共享目录映射为 SFTP 虚拟根目录，
        导致 SFTP 可见路径在 SSH shell 中并不存在，因此这里不再调用远程命令。
        """
        print(f"[scan] Hash via SFTP stream: {remote_path}", flush=True)
        return _calculate_sftp_sha256(sftp_client, remote_path)


    def _insert_rag_file(self, connection: Any, file_record: dict[str, Any]) -> None:
        """按 rag_files 表结构插入一条待解析记录。"""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO rag_files (
                    file_name,
                    file_uid,
                    file_ext,
                    file_size,
                    file_hash,
                    original_path,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    file_record["file_name"],
                    file_record["file_uid"],
                    file_record["file_ext"],
                    file_record["file_size"],
                    file_record["file_hash"],
                    file_record["original_path"],
                    file_record["created_at"],
                    file_record["updated_at"],
                ),
            )

    def _rag_file_exists_by_hash(self, connection: Any, file_hash: str) -> bool:
        """检查相同 hash 的文件是否已经登记过。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM rag_files WHERE file_hash = %s LIMIT 1",
                (file_hash,),
            )
            return cursor.fetchone() is not None

    def _create_ssh_client(self) -> Any:
        """创建 SSH 连接，缺少 paramiko 时给出明确错误。"""
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("缺少 SSH 依赖，请先安装 paramiko。") from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.ssh_config.host,
            port=self.ssh_config.port,
            username=self.ssh_config.user,
            password=self.ssh_config.password,
            timeout=self.ssh_config.timeout,
            banner_timeout=self.ssh_config.timeout,
            auth_timeout=self.ssh_config.timeout,
        )
        return client

    def _print_progress(self, result: dict[str, int]) -> None:
        """终端输出简短进度，便于长批次运行时观察。"""
        print(
            "进度："
            f"已读取 {result['fetched']}，"
            f"已检查 {result['checked']}，"
            f"已插入 {result['inserted']}，"
            f"已存在 {result['skipped_existing']}，"
            f"远程缺失 {result['missing_remote']}，"
            f"失败 {result['failed']}"
        )


def build_ssh_config(config: dict[str, str]) -> SshConfig:
    """从 .env 读取 SSH 配置。"""
    return SshConfig(
        host=get_required_config(config, "SSH_HOST"),
        port=get_int_config(config, "SSH_PORT", default=22, min_value=1),
        user=get_required_config(config, "SSH_USER"),
        password=config.get("SSH_PASSWORD", ""),
        timeout=get_int_config(config, "SSH_TIMEOUT", default=10, min_value=1),
    )


def build_remote_path_candidates(original_path: str) -> list[str]:
    """为群晖路径生成容错候选列表。

    示例：/volume1/share/a.pdf 会依次尝试：
    1. /volume1/share/a.pdf
    2. /share/a.pdf
    3. share/a.pdf
    4. volume1/share/a.pdf
    """
    normalized_path = _normalize_remote_path(original_path)
    candidates = [normalized_path]
    parts = [part for part in normalized_path.split("/") if part]

    if parts and parts[0].startswith("volume") and len(parts) > 1:
        candidates.append("/" + "/".join(parts[1:]))
        candidates.append("/".join(parts[1:]))

    if normalized_path.startswith("/"):
        candidates.append(normalized_path.lstrip("/"))

    return _deduplicate(candidates)


def _calculate_sftp_sha256(sftp_client: Any, remote_path: str) -> str:
    """通过 SFTP 分块读取文件并计算 sha256。"""
    hash_builder = hashlib.sha256()
    with sftp_client.open(remote_path, "rb") as remote_file:
        while True:
            chunk = remote_file.read(1024 * 1024)
            if not chunk:
                break
            hash_builder.update(chunk)
    return hash_builder.hexdigest()


def _parse_sha256_output(output: str) -> str:
    """兼容 sha256sum 与 openssl dgst -sha256 两种输出格式。"""
    tokens = output.replace("=", " ").split()
    for token in tokens:
        if len(token) == 64 and all(char in "0123456789abcdefABCDEF" for char in token):
            return token.lower()
    raise ValueError(f"无法解析 sha256 输出：{output}")


def _generate_file_uid(file_hash: str, original_path: str) -> str:
    """基于 hash 与原始路径生成稳定唯一值。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{file_hash}:{original_path}").hex


def _get_file_ext(path: str) -> str:
    """提取小写扩展名，不带点号。"""
    return PurePosixPath(path).suffix.lower().lstrip(".")


def _is_office_temp_file(path: str, file_ext: str) -> bool:
    """跳过 Office 打开文件时生成的临时文件。"""
    return file_ext in OFFICE_FILE_TYPES and PurePosixPath(path).name.startswith(OFFICE_TEMP_PREFIX)


def _normalize_remote_path(path: str) -> str:
    """统一为 NAS/Linux 可识别的 POSIX 路径。"""
    normalized = path.strip().replace("\\", "/")
    return posixpath.normpath(normalized)


def _deduplicate(values: list[str]) -> list[str]:
    """按出现顺序去重，避免重复 stat 同一路径。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
