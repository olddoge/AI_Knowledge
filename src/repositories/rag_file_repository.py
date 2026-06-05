from typing import Any
import time

from src.database import DatabaseConfig, create_mysql_connection


class RagFileRepository:
    """集中处理 rag_files 表的数据读写。"""

    def __init__(self, db_config: DatabaseConfig) -> None:
        self._connection = create_mysql_connection(db_config)

    def close(self) -> None:
        self._connection.close()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def file_hash_exists(self, file_hash: str) -> bool:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM rag_files WHERE file_hash = %s LIMIT 1",
                (file_hash,),
            )
            return cursor.fetchone() is not None

    def insert_file(self, file_record: dict[str, Any]) -> None:
        with self._connection.cursor() as cursor:
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

    def fetch_pending_parse_files(self, limit: int) -> list[dict[str, Any]]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    file_name,
                    file_uid,
                    file_ext,
                    file_size,
                    original_path
                FROM rag_files
                WHERE parse_status = 0
                ORDER BY id ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cursor.fetchall())

    def claim_pending_parse_files(self, limit: int) -> list[dict[str, Any]]:
        """原子领取一批待解析文件。

        MySQL 8 的 SKIP LOCKED 可以让多个解析进程同时运行时跳过彼此已经锁定的行，
        避免同一条 rag_files 记录被重复解析。调用方需要在成功后 commit，失败时 rollback。
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    file_name,
                    file_uid,
                    file_ext,
                    file_size,
                    file_hash,
                    original_path
                FROM rag_files
                WHERE parse_status = 0
                ORDER BY id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            rows = list(cursor.fetchall())

            if not rows:
                return []

            file_ids = [int(row["id"]) for row in rows]
            placeholders = ", ".join(["%s"] * len(file_ids))
            cursor.execute(
                f"""
                UPDATE rag_files
                SET parse_status = 1, updated_at = %s
                WHERE id IN ({placeholders})
                """,
                (int(time.time()), *file_ids),
            )
            return rows

    def fetch_failed_parse_files(self) -> list[dict[str, Any]]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    original_path
                FROM rag_files
                WHERE parse_status = -1
                ORDER BY id ASC
                """
            )
            return list(cursor.fetchall())

    def update_parse_status(self, file_ids: list[int], parse_status: int) -> None:
        if not file_ids:
            return

        placeholders = ", ".join(["%s"] * len(file_ids))
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE rag_files
                SET parse_status = %s, updated_at = %s
                WHERE id IN ({placeholders})
                """,
                (parse_status, int(time.time()), *file_ids),
            )

    def recover_processing_parse_files(self) -> int:
        """将上次中断遗留的解析中记录恢复为未解析，便于重启后继续处理。"""
        with self._connection.cursor() as cursor:
            affected_rows = cursor.execute(
                """
                UPDATE rag_files
                SET parse_status = 0, updated_at = %s
                WHERE parse_status = 1
                """,
                (int(time.time()),),
            )
            return int(affected_rows)

    def recover_failed_parse_files(self) -> int:
        """将上次解析失败的记录恢复为未解析，重启后自动进入重试队列。"""
        with self._connection.cursor() as cursor:
            affected_rows = cursor.execute(
                """
                UPDATE rag_files
                SET parse_status = 0, updated_at = %s
                WHERE parse_status = -1
                """,
                (int(time.time()),),
            )
            return int(affected_rows)

    def update_parse_success(self, file_id: int, parse_path: str = "") -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_files
                SET parse_status = 2, parse_path = %s, updated_at = %s
                WHERE id = %s
                """,
                (parse_path, int(time.time()), file_id),
            )

    def update_parse_failed(self, file_id: int) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_files
                SET parse_status = -1, updated_at = %s
                WHERE id = %s
                """,
                (int(time.time()), file_id),
            )

    def fetch_pending_clean_files(self, limit: int) -> list[dict[str, Any]]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    file_name,
                    file_uid,
                    file_ext,
                    file_hash,
                    original_path,
                    parse_path
                FROM rag_files
                WHERE clean_status = 0
                    AND parse_status = 2
                    AND parse_path <> ''
                ORDER BY file_size ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cursor.fetchall())

    def fetch_parsed_markdown_files(self, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        """只读分页获取已经解析出 Markdown 的记录，用于不改库的测试上传入口。"""
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    file_name,
                    file_uid,
                    file_ext,
                    file_hash,
                    original_path,
                    parse_path
                FROM rag_files
                WHERE parse_status = 2
                    AND parse_path <> ''
                ORDER BY id ASC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall())

    def claim_pending_clean_files(self, limit: int) -> list[dict[str, Any]]:
        """原子领取一批待清洗记录，支持多个清洗进程同时运行。

        MySQL 8 的 FOR UPDATE SKIP LOCKED 会跳过其他事务已经锁定的行，
        多个清洗进程并发执行时不会领取到同一条 rag_files 记录。
        调用方需要在成功后 commit，异常时 rollback。
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    file_name,
                    file_uid,
                    file_ext,
                    file_hash,
                    original_path,
                    parse_path
                FROM rag_files
                WHERE clean_status = 0
                    AND parse_status = 2
                    AND parse_path <> ''
                ORDER BY file_size ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            rows = list(cursor.fetchall())

            if not rows:
                return []

            file_ids = [int(row["id"]) for row in rows]
            placeholders = ", ".join(["%s"] * len(file_ids))
            cursor.execute(
                f"""
                UPDATE rag_files
                SET clean_status = 1, updated_at = %s
                WHERE id IN ({placeholders})
                """,
                (int(time.time()), *file_ids),
            )
            return rows

    def update_clean_status(self, file_ids: list[int], clean_status: int) -> None:
        if not file_ids:
            return

        placeholders = ", ".join(["%s"] * len(file_ids))
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE rag_files
                SET clean_status = %s, updated_at = %s
                WHERE id IN ({placeholders})
                """,
                (clean_status, int(time.time()), *file_ids),
            )

    def recover_processing_clean_files(self, stale_seconds: int | None = None) -> int:
        """恢复异常中断后遗留的清洗中记录。

        stale_seconds 为空时沿用旧逻辑，恢复全部 clean_status=1 的记录。
        传入 stale_seconds 时只恢复超过该时长未更新的记录，避免多个清洗入口
        同时运行时误重置其他进程正在处理的数据。
        """
        with self._connection.cursor() as cursor:
            if stale_seconds is not None:
                now = int(time.time())
                affected_rows = cursor.execute(
                    """
                    UPDATE rag_files
                    SET clean_status = 0, updated_at = %s
                    WHERE clean_status = 1 AND updated_at < %s
                    """,
                    (now, now - stale_seconds),
                )
                return int(affected_rows)

            affected_rows = cursor.execute(
                """
                UPDATE rag_files
                SET clean_status = 0, updated_at = %s
                WHERE clean_status = 1
                """,
                (int(time.time()),),
            )
            return int(affected_rows)

    def recover_failed_clean_files(self) -> int:
        """将上次清洗失败的记录恢复为未清洗，重启后自动进入重试队列。"""
        with self._connection.cursor() as cursor:
            affected_rows = cursor.execute(
                """
                UPDATE rag_files
                SET clean_status = 0, updated_at = %s
                WHERE clean_status = -1
                """,
                (int(time.time()),),
            )
            return int(affected_rows)

    def update_clean_success(self, file_id: int) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_files
                SET clean_status = 2, updated_at = %s
                WHERE id = %s
                """,
                (int(time.time()), file_id),
            )

    def update_clean_failed(self, file_id: int) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_files
                SET clean_status = -1, updated_at = %s
                WHERE id = %s
                """,
                (int(time.time()), file_id),
            )
