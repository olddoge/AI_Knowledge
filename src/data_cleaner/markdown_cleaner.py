import hashlib
import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote


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
ALLOWED_HTML_TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "colgroup", "col"}
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
    normalized_content = _remove_noise_html_tags(normalized_content)
    normalized_content = _replace_garbage_characters(normalized_content)
    normalized_content = _remove_markdown_styles(normalized_content)
    normalized_content = _normalize_image_references(normalized_content, markdown_path)
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
    # 保留 table/tr/td/th 等结构性标签，删除解析器常见的展示型 HTML 标签。
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


def _normalize_image_references(content: str, markdown_path: str | Path | None) -> str:
    """规范 Markdown 图片引用，并把图片文件改成 md5 短文件名。

    Markdown 中的图片 alt 统一置空，实际路径文件名与磁盘文件同步改名。
    如果磁盘文件不存在，仍会更新 Markdown 引用，便于后续资源补齐时保持规则一致。
    """

    def replace_match(match: re.Match[str]) -> str:
        image_path = _clean_image_path(match.group(2))
        shortened_path = _shorten_image_path(image_path, markdown_path)
        return f"![]({shortened_path})" if shortened_path else ""

    return IMAGE_PATTERN.sub(replace_match, content)


def _clean_image_path(image_path: str) -> str:
    # 只清理引用字符串本身，保留相对路径、绝对路径、查询参数和锚点。
    return image_path.strip().replace("\\", "/").replace(" ", "%20")


def _get_image_name(image_path: str) -> str:
    return unquote(Path(image_path.split("#", 1)[0].split("?", 1)[0]).name.strip())


def _shorten_image_path(image_path: str, markdown_path: str | Path | None) -> str:
    path_part, suffix_part = _split_image_path_suffix(image_path)
    image_name = _get_image_name(path_part)
    if not image_name:
        return image_path

    short_name = _build_short_image_name(image_name)
    path_without_name = path_part[: len(path_part) - len(Path(path_part).name)]
    shortened_path = f"{path_without_name}{quote(short_name)}{suffix_part}"

    source_file = _find_existing_image_file(path_part, markdown_path)
    if source_file is None:
        return shortened_path

    target_file = source_file.with_name(short_name)
    if source_file.resolve() == target_file.resolve():
        return shortened_path
    if target_file.exists():
        return shortened_path

    target_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.rename(target_file)
    return shortened_path


def _split_image_path_suffix(image_path: str) -> tuple[str, str]:
    query_index = image_path.find("?")
    fragment_index = image_path.find("#")
    suffix_candidates = [index for index in (query_index, fragment_index) if index != -1]
    if not suffix_candidates:
        return image_path, ""

    suffix_start = min(suffix_candidates)
    return image_path[:suffix_start], image_path[suffix_start:]


def _build_short_image_name(image_name: str) -> str:
    original_name = unquote(image_name)
    image_suffix = Path(original_name).suffix.lower()
    short_stem = hashlib.md5(original_name.encode("utf-8")).hexdigest()
    return f"{short_stem}{image_suffix}"


def _find_existing_image_file(image_path: str, markdown_path: str | Path | None) -> Path | None:
    path_part, _ = _split_image_path_suffix(image_path)
    decoded_path = Path(unquote(path_part))
    candidates: list[Path] = []

    if decoded_path.is_absolute():
        candidates.append(decoded_path)
    else:
        if markdown_path is not None:
            candidates.append(Path(markdown_path).expanduser().resolve().parent / decoded_path)
        candidates.append(Path.cwd() / decoded_path)
        candidates.append(decoded_path)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


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
    # 当前入库接口只需要正文和 original_path，暂不写入 YAML 头，避免影响检索文本。
    return ""
