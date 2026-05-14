from dataclasses import dataclass
from typing import Any

from src.config import get_required_config


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def build_database_config(config: dict[str, str]) -> DatabaseConfig:
    """从环境配置中构建数据库连接配置。"""
    return DatabaseConfig(
        host=get_required_config(config, "DB_HOST"),
        port=_get_int_config(config, "DB_PORT", default=3306, min_value=1),
        user=get_required_config(config, "DB_USER"),
        password=config.get("DB_PASSWORD", ""),
        database=get_required_config(config, "DB_NAME"),
    )


def create_mysql_connection(config: DatabaseConfig) -> Any:
    """创建 MySQL 连接；依赖缺失时给出明确错误，避免任务静默失败。"""
    try:
        import mysql.connector
    except ImportError as exc:
        raise RuntimeError(
            "缺少 MySQL 驱动，请先安装 mysql-connector-python。"
        ) from exc

    return mysql.connector.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
    )


def _get_int_config(
    config: dict[str, str],
    key: str,
    default: int,
    min_value: int | None = None,
) -> int:
    """读取整数配置，并做最小值校验，避免关键参数无效。"""
    value = config.get(key)
    if value is None or not value.strip():
        return default

    try:
        parsed_value = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer config {key}: {value}") from exc

    if min_value is not None and parsed_value < min_value:
        raise ValueError(f"Config {key} must be greater than or equal to {min_value}")

    return parsed_value
