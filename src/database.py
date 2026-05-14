from dataclasses import dataclass
from typing import Any

from src.config import get_int_config, get_required_config


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
        port=get_int_config(config, "DB_PORT", default=3306, min_value=1),
        user=get_required_config(config, "DB_USER"),
        password=config.get("DB_PASSWORD", ""),
        database=get_required_config(config, "DB_NAME"),
    )


def create_mysql_connection(config: DatabaseConfig) -> Any:
    """使用 PyMySQL 创建 MySQL 连接；依赖缺失时给出明确错误。"""
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("缺少 MySQL 驱动，请先安装 pymysql。") from exc

    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
