from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator


INPUT_COLUMNS = [
    "案件链接",
    "文书标题",
    "文书类型",
    "文书发布时间",
    "文书发布机构",
    "文书详情链接",
    "docpubdate",
    "docpubyear",
]

DISCOVERY_COLUMNS = INPUT_COLUMNS + [
    "输入行号",
    "清洗后文书详情链接",
    "详情页状态码",
    "详情页类型",
    "检测来源",
    "是否命中反爬",
    "是否空模板",
    "是否需要Playwright",
    "是否有viewer链路",
    "正文选择器",
    "正文文本长度",
    "正文预览",
    "正文文本",
    "附件数量",
    "附件格式统计",
    "附件链接列表",
    "内容组合类别",
    "处理错误",
]

FINAL_COLUMNS = DISCOVERY_COLUMNS + [
    "附件文件名列表",
    "附件类型列表",
    "附件状态明细",
    "附件提取状态统计",
    "附件文本长度",
    "附件文本预览",
    "内容来源",
    "content_text",
]


def detail_type_from_url(cleaned_url: str) -> str:
    if "/pcws/wsxq" in cleaned_url:
        return "wsxq"
    if "/pcgg/ggxq" in cleaned_url:
        return "ggxq"
    return "other"


def detect_combination(body_text: str, attachment_types: list[str], has_viewer_link: bool) -> str:
    pdf_count = attachment_types.count("pdf")
    word_count = sum(1 for item in attachment_types if item in {"word", "rtf", "legacy_word"})
    other_count = sum(1 for item in attachment_types if item not in {"pdf", "word", "rtf", "legacy_word"})

    if body_text:
        if pdf_count and word_count:
            return "正文文本+PDF+Word"
        if pdf_count and other_count:
            return "正文文本+PDF+其他附件"
        if pdf_count:
            return "正文文本+PDF"
        if word_count:
            return "正文文本+Word"
        if other_count:
            return "正文文本+其他附件"
        return "仅正文文本"

    if pdf_count and not word_count and not other_count:
        return "可预览但需下载PDF" if has_viewer_link else "仅PDF附件"
    if word_count and not pdf_count and not other_count:
        return "仅Word附件"
    if other_count and not pdf_count and not word_count:
        return "仅其他附件"
    if pdf_count and word_count and not other_count:
        return "仅附件(PDF+Word)"
    if pdf_count and other_count and not word_count:
        return "仅附件(PDF+其他)"
    if pdf_count or word_count or other_count:
        return "仅附件(混合)"
    return "空白/待人工复核"


def iter_input_rows(path: str, start_row: int, limit: int | None) -> Iterator[tuple[int, dict[str, str]]]:
    emitted = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=1):
            if row_number < start_row:
                continue
            yield row_number, row
            emitted += 1
            if limit is not None and emitted >= limit:
                break


def iter_csv_rows(path_or_dir: str, pattern: str) -> Iterator[dict[str, str]]:
    target = Path(path_or_dir)
    paths = [target] if target.is_file() else sorted(target.glob(pattern))
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                yield row
