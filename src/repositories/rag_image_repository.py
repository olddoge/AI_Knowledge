from src.database import DatabaseConfig, create_mysql_connection


class RagImageRepository:
    """集中处理 rag_image 表的图片引用写入。"""

    def __init__(self, db_config: DatabaseConfig) -> None:
        self._connection = create_mysql_connection(db_config)

    def close(self) -> None:
        self._connection.close()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def insert_if_not_exists(self, file_uid: str, image_name: str) -> bool:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM rag_image
                WHERE file_uid = %s AND image_file = %s
                LIMIT 1
                """,
                (file_uid, image_name),
            )
            if cursor.fetchone() is not None:
                return False

            affected_rows = cursor.execute(
                """
                INSERT INTO rag_image (file_uid, image_file)
                VALUES (%s, %s)
                """,
                (file_uid, image_name),
            )
            return int(affected_rows) > 0
