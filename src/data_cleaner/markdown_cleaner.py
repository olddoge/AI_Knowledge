import html
import re
from pathlib import Path
from typing import Any


IMAGE_PATTERN = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
HTML_IMAGE_PATTERN = re.compile(r"<img\b[^>]*\bsrc=[\"']?([^\"'\s>]+)[^>]*>", flags=re.IGNORECASE)
TOC_ANCHOR_PATTERN = re.compile(r'<a\s+[^>]*(?:id|name)=["\']?_Toc[^>]*>\s*</a>', flags=re.IGNORECASE)
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", flags=re.DOTALL)
NOISE_HTML_TAG_PATTERN = re.compile(
    r"</?(?:a|span|div|u|font|strong|b|em|i|section|article|header|footer|center)\b[^>]*>",
    flags=re.IGNORECASE,
)
PARAGRAPH_TAG_PATTERN = re.compile(r"</?p\b[^>]*>", flags=re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
ALLOWED_HTML_TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "colgroup", "col"}
ZERO_WIDTH_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
PAGE_LINE_PATTERN = re.compile(
    r"^\s*(?:第\s*\d+\s*页|page\s+\d+(?:\s+of\s+\d+)?|\d+\s*/\s*\d+|-+\s*\d+\s*-+|\d+)\s*$",
    flags=re.IGNORECASE,
)


def clean_markdown_file(
    input_path: str | Path,
    file_record: dict[str, Any],
    output_path: str | Path | None = None,
) -> tuple[Path, list[str]]:
    """读取解析后的 Markdown，清洗内容后以 UTF-8 写回文件。"""
    source_path = Path(input_path)
    target_path = Path(output_path) if output_path else source_path
    raw_content = source_path.read_bytes().decode("utf-8", errors="replace")
    cleaned_content = clean_markdown_content(raw_content, file_record)
    image_names = extract_image_names(raw_content)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(cleaned_content, encoding="utf-8", newline="\n")
    return target_path, image_names


def clean_markdown_content(content: str, file_record: dict[str, Any]) -> str:
    """按知识库入库规则清洗 Markdown 文本并补充源文件元信息。"""
    normalized_content = _normalize_line_endings(content)
    normalized_content = _remove_existing_metadata_header(normalized_content)
    normalized_content = _decode_html_entities(normalized_content)
    normalized_content = _remove_noise_html_tags(normalized_content)
    normalized_content = _replace_garbage_characters(normalized_content)
    normalized_content = _normalize_image_references(normalized_content)
    normalized_content = _normalize_heading_lines(normalized_content)
    normalized_content = _remove_obvious_page_lines(normalized_content)
    normalized_content = _collapse_blank_lines(normalized_content)
    normalized_content = normalized_content.strip()
    return f"{_build_metadata_header(file_record)}\n{normalized_content}\n"


def extract_image_names(content: str) -> list[str]:
    """提取 Markdown 图片引用中的图片文件名，用于写入 rag_image。"""
    image_names: list[str] = []
    image_paths = [*IMAGE_PATTERN.findall(content), *HTML_IMAGE_PATTERN.findall(content)]
    for image_path in image_paths:
        image_name = Path(image_path.split("#", 1)[0].split("?", 1)[0]).name.strip()
        if image_name and image_name not in image_names:
            image_names.append(image_name)
    return image_names


def _normalize_line_endings(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _remove_noise_html_tags(content: str) -> str:
    # 去除解析器产生的目录锚点和展示型标签，保留 table/tr/td/th 等结构标签。
    cleaned_content = HTML_COMMENT_PATTERN.sub("", content)
    cleaned_content = TOC_ANCHOR_PATTERN.sub("", cleaned_content)
    cleaned_content = _normalize_html_image_references(cleaned_content)
    cleaned_content = NOISE_HTML_TAG_PATTERN.sub("", cleaned_content)
    cleaned_content = _normalize_paragraph_tags(cleaned_content)
    cleaned_content = _normalize_allowed_table_tags(cleaned_content)
    return _remove_unallowed_html_tags(cleaned_content)


def _decode_html_entities(content: str) -> str:
    decoded_content = html.unescape(content)
    return decoded_content.replace("\xa0", " ")


def _normalize_paragraph_tags(content: str) -> str:
    # 表格单元格里的 p 标签通常只是排版残留，转换为空格以免破坏表格结构。
    content = re.sub(r"(<t[dh]\b[^>]*>)\s*<p\b[^>]*>", r"\1", content, flags=re.IGNORECASE)
    content = re.sub(r"</p>\s*(</t[dh]>)", r"\1", content, flags=re.IGNORECASE)
    content = PARAGRAPH_TAG_PATTERN.sub("\n", content)
    return content


def _normalize_html_image_references(content: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        image_name = Path(match.group(1).split("#", 1)[0].split("?", 1)[0]).name.strip()
        return f"> [图片引用]{image_name}" if image_name else ""

    return HTML_IMAGE_PATTERN.sub(replace_match, content)


def _normalize_allowed_table_tags(content: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        raw_tag = match.group(0)
        tag_name = match.group(1).lower()
        if tag_name not in ALLOWED_HTML_TABLE_TAGS:
            return raw_tag
        return f"</{tag_name}>" if raw_tag.startswith("</") else f"<{tag_name}>"

    return HTML_TAG_PATTERN.sub(replace_match, content)


def _remove_unallowed_html_tags(content: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        tag_name = match.group(1).lower()
        if tag_name in ALLOWED_HTML_TABLE_TAGS:
            return match.group(0)
        return ""

    return HTML_TAG_PATTERN.sub(replace_match, content)


def _remove_existing_metadata_header(content: str) -> str:
    if not content.startswith("---\n"):
        return content

    end_index = content.find("\n---\n", 4)
    if end_index == -1:
        return content

    header = content[: end_index + len("\n---\n")]
    if "source_file_id:" not in header:
        return content
    return content[end_index + len("\n---\n") :]


def _replace_garbage_characters(content: str) -> str:
    replacements = {
        "\x00": "",
        "\ufffd": "",
        "\xa0": " ",
        "�": "",
        "□": "",
        "■": "",
        "●": "",
        "◆": "",
        "◇": "",
        "▪": "",
        "¤": "",
        "\\_": "",
    }
    cleaned_content = ZERO_WIDTH_PATTERN.sub("", content)
    for old_value, new_value in replacements.items():
        cleaned_content = cleaned_content.replace(old_value, new_value)
    return cleaned_content


def _normalize_image_references(content: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        image_name = Path(match.group(1).split("#", 1)[0].split("?", 1)[0]).name.strip()
        return f"> [图片引用]{image_name}" if image_name else ""

    return IMAGE_PATTERN.sub(replace_match, content)


def _normalize_heading_lines(content: str) -> str:
    normalized_lines: list[str] = []
    for line in content.split("\n"):
        stripped_line = line.strip()
        if stripped_line.startswith("＃"):
            stripped_line = "#" + stripped_line.lstrip("＃").strip()
        if re.match(r"^#{1,6}\S", stripped_line):
            stripped_line = re.sub(r"^(#{1,6})(\S)", r"\1 \2", stripped_line)
        normalized_lines.append(stripped_line if stripped_line.startswith("#") else line.rstrip())
    return "\n".join(normalized_lines)


def _remove_obvious_page_lines(content: str) -> str:
    lines = [line for line in content.split("\n") if not PAGE_LINE_PATTERN.match(line)]
    return "\n".join(lines)


def _collapse_blank_lines(content: str) -> str:
    content = re.sub(r"[ \t]+\n", "\n", content)
    content = re.sub(r"\n{2,}", "\n", content)
    return content


def _build_metadata_header(file_record: dict[str, Any]) -> str:
    return "\n".join(
        [
            "---",
            f"文件编号: {file_record.get('id', '')}",
            "---",
        ]
    )
