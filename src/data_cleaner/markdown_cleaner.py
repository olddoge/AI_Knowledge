import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote


IMAGE_PATTERN = re.compile(r"!\[([^\]]*)]\(([^)]+)\)")
HTML_IMAGE_PATTERN = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\"'\s>]+))[^>]*>",
    flags=re.IGNORECASE,
)
TOC_ANCHOR_PATTERN = re.compile(r'<a\s+[^>]*(?:id|name)=["\']?_Toc[^>]*>\s*</a>', flags=re.IGNORECASE)
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", flags=re.DOTALL)
NOISE_HTML_TAG_PATTERN = re.compile(
    r"</?(?:a|span|div|u|font|strong|b|em|i|section|article|header|footer|center)\b[^>]*>",
    flags=re.IGNORECASE,
)
PARAGRAPH_TAG_PATTERN = re.compile(r"</?p\b[^>]*>", flags=re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
HTML_TABLE_PATTERN = re.compile(r"<table\b[^>]*>.*?</table>", flags=re.IGNORECASE | re.DOTALL)
HTML_TABLE_ROW_PATTERN = re.compile(r"<tr\b[^>]*>.*?</tr>", flags=re.IGNORECASE | re.DOTALL)
HTML_TABLE_CELL_PATTERN = re.compile(r"<(td|th)\b[^>]*>(.*?)</\1>", flags=re.IGNORECASE | re.DOTALL)
HTML_LINE_BREAK_TAG_PATTERN = re.compile(r"<br\b[^>]*>|</p>|</div>|</section>|</article>", flags=re.IGNORECASE)
ZERO_WIDTH_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
PAGE_LINE_PATTERN = re.compile(
    r"^\s*(?:page\s+\d+(?:\s+of\s+\d+)?|\d+\s*/\s*\d+|-+\s*\d+\s*-+|\d+)\s*$",
    flags=re.IGNORECASE,
)


def clean_markdown_file(
    input_path: str | Path,
    file_record: dict[str, Any],
    output_path: str | Path | None = None,
) -> tuple[Path, list[str]]:
    """读取解析后的 Markdown，清洗后以 UTF-8 写回文件。

    output_path 为空时直接覆盖 input_path，符合“清洗后保存到原本 markdown 文件”
    的流程要求。返回值中的 image_names 用于日志追踪图片引用情况。
    """
    source_path = Path(input_path)
    target_path = Path(output_path) if output_path else source_path

    raw_content = source_path.read_bytes().decode("utf-8", errors="replace")
    cleaned_content = clean_markdown_content(raw_content, file_record, source_path)
    image_names = extract_image_names(raw_content)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(cleaned_content, encoding="utf-8", newline="\n")
    return target_path, image_names


def clean_markdown_content(
    content: str,
    file_record: dict[str, Any],
    markdown_path: str | Path | None = None,
) -> str:
    """按知识库入库规则清洗 Markdown 文本。

    清洗目标是减少解析服务带来的噪声，而不是改变业务内容：
    去掉无意义 HTML、目录锚点、零宽字符、明显页码行，并规范标题、图片引用和空行。
    """
    normalized_content = _normalize_line_endings(content)
    normalized_content = _remove_existing_metadata_header(normalized_content)
    normalized_content = _decode_html_entities(normalized_content)
    normalized_content = _convert_html_tables_to_markdown(normalized_content)
    normalized_content = _remove_noise_html_tags(normalized_content)
    normalized_content = _replace_garbage_characters(normalized_content)
    normalized_content = _remove_markdown_styles(normalized_content)
    normalized_content = _normalize_image_references(normalized_content, file_record, markdown_path)
    normalized_content = _normalize_heading_lines(normalized_content)
    normalized_content = _remove_obvious_page_lines(normalized_content)
    normalized_content = _collapse_extra_blank_lines(normalized_content)
    normalized_content = normalized_content.strip()

    metadata_header = _build_metadata_header(file_record)
    if metadata_header:
        return f"{metadata_header}\n{normalized_content}\n"
    return f"{normalized_content}\n"


def extract_image_names(content: str) -> list[str]:
    """提取 Markdown 和 HTML 图片引用中的文件名，用于日志和后续追踪。"""
    image_names: list[str] = []
    image_paths = [
        *[match[1] for match in IMAGE_PATTERN.findall(content)],
        *[_get_html_image_path(match) for match in HTML_IMAGE_PATTERN.finditer(content)],
    ]
    for image_path in image_paths:
        image_name = _get_image_name(image_path)
        if image_name and image_name not in image_names:
            image_names.append(image_name)
    return image_names


def _normalize_line_endings(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _remove_noise_html_tags(content: str) -> str:
    # Convert useful HTML fragments before removing all remaining tags.
    cleaned_content = HTML_COMMENT_PATTERN.sub("", content)
    cleaned_content = TOC_ANCHOR_PATTERN.sub("", cleaned_content)
    cleaned_content = _normalize_html_image_references(cleaned_content)
    cleaned_content = HTML_LINE_BREAK_TAG_PATTERN.sub("\n", cleaned_content)
    cleaned_content = NOISE_HTML_TAG_PATTERN.sub("", cleaned_content)
    cleaned_content = _normalize_paragraph_tags(cleaned_content)
    return _remove_all_html_tags(cleaned_content)


def _decode_html_entities(content: str) -> str:
    decoded_content = html.unescape(content)
    return decoded_content.replace("\xa0", " ")


def _normalize_paragraph_tags(content: str) -> str:
    # 表格单元格里的 p 标签通常只是排版残留，直接移除可避免破坏表格结构。
    content = re.sub(r"(<t[dh]\b[^>]*>)\s*<p\b[^>]*>", r"\1", content, flags=re.IGNORECASE)
    content = re.sub(r"</p>\s*(</t[dh]>)", r"\1", content, flags=re.IGNORECASE)
    content = PARAGRAPH_TAG_PATTERN.sub("\n", content)
    return content


def _normalize_html_image_references(content: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        image_path = _clean_image_path(_get_html_image_path(match))
        image_name = _get_image_name(image_path)
        return f"![{image_name}]({image_path})" if image_path else ""

    return HTML_IMAGE_PATTERN.sub(replace_match, content)


def _get_html_image_path(match: re.Match[str]) -> str:
    return next((group for group in match.groups() if group), "")


def _convert_html_tables_to_markdown(content: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        table_markdown = _html_table_to_markdown(match.group(0))
        return f"\n{table_markdown}\n" if table_markdown else ""

    return HTML_TABLE_PATTERN.sub(replace_match, content)


def _html_table_to_markdown(table_html: str) -> str:
    rows: list[list[str]] = []

    for row_match in HTML_TABLE_ROW_PATTERN.finditer(table_html):
        row_html = row_match.group(0)
        cells: list[str] = []
        for cell_match in HTML_TABLE_CELL_PATTERN.finditer(row_html):
            cells.append(_clean_html_table_cell(cell_match.group(2)))
        if cells:
            rows.append(cells)

    if not rows:
        return ""

    column_count = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (column_count - len(row)) for row in rows]
    header = normalized_rows[0]
    data_rows = normalized_rows[1:]

    markdown_lines = [
        _format_markdown_table_row(header),
        _format_markdown_table_row(["---"] * column_count),
    ]
    markdown_lines.extend(_format_markdown_table_row(row) for row in data_rows)
    return "\n".join(markdown_lines)


def _clean_html_table_cell(cell_html: str) -> str:
    cell_content = _normalize_html_image_references(cell_html)
    cell_content = HTML_LINE_BREAK_TAG_PATTERN.sub(" ", cell_content)
    cell_content = HTML_TAG_PATTERN.sub("", cell_content)
    cell_content = re.sub(r"\s+", " ", cell_content).strip()
    return cell_content.replace("|", r"\|")


def _format_markdown_table_row(cells: list[str]) -> str:
    return f"| {' | '.join(cells)} |"


def _remove_all_html_tags(content: str) -> str:
    return HTML_TAG_PATTERN.sub("", content)


def _remove_existing_metadata_header(content: str) -> str:
    if not content.startswith("---\n"):
        return content

    end_index = content.find("\n---\n", 4)
    if end_index == -1:
        return content

    header = content[: end_index + len("\n---\n")]
    if "source_file_id:" not in header and "file_name:" not in header:
        return content
    return content[end_index + len("\n---\n") :]


def _replace_garbage_characters(content: str) -> str:
    replacements = {
        "\x00": "",
        "\ufffd": "",
        "\xa0": " ",
        "\\_": "",
        "text_image": "",
    }
    cleaned_content = ZERO_WIDTH_PATTERN.sub("", content)
    for old_value, new_value in replacements.items():
        cleaned_content = cleaned_content.replace(old_value, new_value)
    return cleaned_content


def _remove_markdown_styles(content: str) -> str:
    """移除加粗、斜体、删除线等展示样式，只保留文本内容。"""
    cleaned_content = re.sub(r"(\*\*\*|___)(.+?)\1", r"\2", content, flags=re.DOTALL)
    cleaned_content = re.sub(r"(\*\*|__)(.+?)\1", r"\2", cleaned_content, flags=re.DOTALL)
    cleaned_content = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", cleaned_content, flags=re.DOTALL)
    cleaned_content = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", cleaned_content, flags=re.DOTALL)
    cleaned_content = re.sub(r"~~(.+?)~~", r"\1", cleaned_content, flags=re.DOTALL)
    return cleaned_content


def _normalize_image_references(
    content: str,
    file_record: dict[str, Any],
    markdown_path: str | Path | None,
) -> str:
    """规范 Markdown 图片引用为 /images/<file_id>/<图片名>。

    图片文件实际保存在 MARKDOWN_IMAGE_PATH/<file_id>/ 下，这里只调整 Markdown
    引用，不再修改磁盘图片文件名。
    """

    def replace_match(match: re.Match[str]) -> str:
        image_path = _clean_image_path(match.group(2))
        normalized_path = _build_markdown_image_path(image_path, file_record, markdown_path)
        return f"![]({normalized_path})" if normalized_path else ""

    return IMAGE_PATTERN.sub(replace_match, content)


def _clean_image_path(image_path: str) -> str:
    # 只清理引用字符串本身，保留相对路径、绝对路径、查询参数和锚点。
    return image_path.strip().replace("\\", "/").replace(" ", "%20")


def _get_image_name(image_path: str) -> str:
    return unquote(Path(image_path.split("#", 1)[0].split("?", 1)[0]).name.strip())


def _build_markdown_image_path(
    image_path: str,
    file_record: dict[str, Any],
    markdown_path: str | Path | None,
) -> str:
    path_part, suffix_part = _split_image_path_suffix(image_path)
    image_name = _get_image_name(path_part)
    if not image_name:
        return image_path

    file_id = _get_image_file_id(path_part, file_record, markdown_path)
    return f"/images/{file_id}/{image_name}{suffix_part}" if file_id else f"/images/{image_name}{suffix_part}"


def _split_image_path_suffix(image_path: str) -> tuple[str, str]:
    query_index = image_path.find("?")
    fragment_index = image_path.find("#")
    suffix_candidates = [index for index in (query_index, fragment_index) if index != -1]
    if not suffix_candidates:
        return image_path, ""

    suffix_start = min(suffix_candidates)
    return image_path[:suffix_start], image_path[suffix_start:]


def _get_image_file_id(
    image_path: str,
    file_record: dict[str, Any],
    markdown_path: str | Path | None,
) -> str:
    decoded_parts = [unquote(part) for part in Path(image_path).parts]
    if len(decoded_parts) >= 2 and decoded_parts[-2].isdigit():
        return decoded_parts[-2]

    record_id = str(file_record.get("id") or "").strip()
    if record_id:
        return record_id

    if markdown_path is not None:
        parse_stem = Path(markdown_path).stem.strip()
        if parse_stem.isdigit():
            return parse_stem

    return ""


def _normalize_heading_lines(content: str) -> str:
    normalized_lines: list[str] = []
    for line in content.split("\n"):
        stripped_line = line.strip()
        if re.match(r"^#{1,6}\S", stripped_line):
            stripped_line = re.sub(r"^(#{1,6})(\S)", r"\1 \2", stripped_line)
        normalized_lines.append(stripped_line if stripped_line.startswith("#") else line.rstrip())
    return "\n".join(normalized_lines)


def _remove_obvious_page_lines(content: str) -> str:
    lines = [line for line in content.split("\n") if not PAGE_LINE_PATTERN.match(line)]
    return "\n".join(lines)


def _collapse_extra_blank_lines(content: str) -> str:
    """保留单个空行，连续多个空行统一压缩为一个空行。"""
    stripped_trailing_spaces = re.sub(r"[ \t]+\n", "\n", content)
    return re.sub(r"\n{3,}", "\n\n", stripped_trailing_spaces)


def _build_metadata_header(file_record: dict[str, Any]) -> str:
    file_name = _get_metadata_file_name(file_record)
    if not file_name:
        return ""
    escaped_file_name = file_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'---\nfile_name: "{escaped_file_name}"\n---'


def _get_metadata_file_name(file_record: dict[str, Any]) -> str:
    file_name = str(file_record.get("file_name") or "").strip()
    if file_name:
        return file_name

    original_path = str(file_record.get("original_path") or "").strip()
    if original_path:
        return Path(original_path).name

    parse_path = str(file_record.get("parse_path") or "").strip()
    if parse_path:
        return Path(parse_path).name

    return ""
