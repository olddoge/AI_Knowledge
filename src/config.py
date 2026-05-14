from pathlib import Path


def load_env(env_path: str | Path = ".env") -> dict[str, str]:
    """读取简单的 KEY=VALUE 格式 .env 配置文件。"""
    config: dict[str, str] = {}
    path = Path(env_path)

    if not path.exists():
        raise FileNotFoundError(f"Environment config file not found: {path.resolve()}")

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        # 空行和注释行不参与配置解析，方便后续维护 .env。
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: {raw_line}")

        key, value = line.split("=", 1)
        key = key.strip()
        # 去掉首尾引号，兼容 Windows 路径这类常见配置写法。
        value = value.strip().strip('"').strip("'")

        if not key:
            raise ValueError(f"Invalid empty .env key on line {line_number}")

        config[key] = value

    return config


def get_required_config(config: dict[str, str], key: str) -> str:
    """读取必填配置，缺失时直接报错，避免后续流程静默使用空值。"""
    value = config.get(key, "").strip()
    if not value:
        raise ValueError(f"Missing required config: {key}")
    return value


def get_bool_config(config: dict[str, str], key: str, default: bool = False) -> bool:
    """读取布尔配置，支持 true/false、yes/no、on/off、1/0。"""
    value = config.get(key)
    if value is None or not value.strip():
        return default

    normalized_value = value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"Invalid boolean config {key}: {value}")


def get_int_config(
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
