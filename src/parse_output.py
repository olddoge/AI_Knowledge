import json
from datetime import datetime
from pathlib import Path


def save_parse_result(
    parse_result: dict[str, list[dict[str, object]]],
    parse_output_path: str,
) -> Path:
    """将解析结果保存为 JSON 文件，供后续清洗和入库流程继续处理。"""
    output_dir = Path(parse_output_path).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 文件名带时间戳，避免多次批处理时覆盖历史解析结果。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"parse_result_{timestamp}.json"
    output_file.write_text(
        json.dumps(parse_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return output_file
