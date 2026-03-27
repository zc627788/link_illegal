from __future__ import annotations

import argparse
import csv
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from time import perf_counter
from typing import Iterator

from .attachments import (
    AttachmentExtraction,
    build_attachment_text,
    classify_attachment,
    extract_attachment_text,
    summarize_attachment_statuses,
)
from .extractors import parse_detail_html
from .fetch_requests import RequestsFetcher
from .utils import clean_url, json_dumps, make_preview
from .writer import CsvShardWriter


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

OUTPUT_COLUMNS = INPUT_COLUMNS + [
    "输入行号",
    "清洗后文书详情链接",
    "详情页状态码",
    "详情页类型",
    "检测来源",
    "是否命中反爬",
    "是否空模板",
    "是否需要Playwright",
    "正文选择器",
    "正文文本长度",
    "正文预览",
    "附件数量",
    "附件文件名列表",
    "附件类型列表",
    "附件格式统计",
    "附件链接列表",
    "附件状态明细",
    "附件提取状态统计",
    "附件文本长度",
    "附件文本预览",
    "内容来源",
    "内容组合类别",
    "content_text",
    "处理错误",
]


class PipelineResult:
    def __init__(self, row: dict[str, object], needs_playwright: bool, category: str) -> None:
        self.row = row
        self.needs_playwright = needs_playwright
        self.category = category


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


def build_output_row(
    row_number: int,
    source_row: dict[str, str],
    cleaned_url: str,
    detail_status_code: int | None,
    detail_body_text: str,
    body_selector: str | None,
    attachment_urls: list[str],
    attachment_types: list[str],
    attachment_results: list[AttachmentExtraction],
    attachment_text: str,
    content_source: str,
    category: str,
    needs_playwright: bool,
    is_challenge: bool,
    is_empty_template: bool,
    detail_error: str | None,
) -> dict[str, object]:
    output_row: dict[str, object] = {column: source_row.get(column, "") for column in INPUT_COLUMNS}
    output_row.update(
        {
            "输入行号": row_number,
            "清洗后文书详情链接": cleaned_url,
            "详情页状态码": detail_status_code or "",
            "详情页类型": "wsxq" if "/pcws/wsxq" in cleaned_url else ("ggxq" if "/pcgg/ggxq" in cleaned_url else "other"),
            "检测来源": "requests",
            "是否命中反爬": "是" if is_challenge else "否",
            "是否空模板": "是" if is_empty_template else "否",
            "是否需要Playwright": "是" if needs_playwright else "否",
            "正文选择器": body_selector or "",
            "正文文本长度": len(detail_body_text),
            "正文预览": make_preview(detail_body_text),
            "附件数量": len(attachment_urls),
            "附件文件名列表": json_dumps([item.filename for item in attachment_results]),
            "附件类型列表": json_dumps([item.attachment_type for item in attachment_results] or attachment_types),
            "附件格式统计": json_dumps(
                {
                    "pdf": attachment_types.count("pdf"),
                    "word": sum(1 for item in attachment_types if item in {"word", "rtf", "legacy_word"}),
                    "other": sum(1 for item in attachment_types if item not in {"pdf", "word", "rtf", "legacy_word"}),
                }
            ),
            "附件链接列表": json_dumps(attachment_urls),
            "附件状态明细": json_dumps(
                [
                    {
                        "filename": item.filename,
                        "type": item.attachment_type,
                        "status": item.status,
                        "length": item.text_length,
                    }
                    for item in attachment_results
                ]
            ),
            "附件提取状态统计": json_dumps(summarize_attachment_statuses(attachment_results)),
            "附件文本长度": len(attachment_text),
            "附件文本预览": make_preview(attachment_text),
            "内容来源": content_source,
            "内容组合类别": category,
            "content_text": detail_body_text if detail_body_text else attachment_text,
            "处理错误": detail_error or "",
        }
    )
    return output_row


def process_row(row_number: int, row: dict[str, str], args: argparse.Namespace) -> PipelineResult:
    cleaned_url = clean_url(row.get("文书详情链接", ""))
    fetcher = RequestsFetcher(timeout=args.request_timeout)
    detail_response = fetcher.fetch_text(cleaned_url)
    detail = parse_detail_html(detail_response.text)

    attachment_results: list[AttachmentExtraction] = []
    if args.extract_attachments:
        for attachment_url in detail.attachment_urls:
            attachment_results.append(
                extract_attachment_text(
                    fetcher=fetcher,
                    url=attachment_url,
                    max_bytes=args.max_attachment_mb * 1024 * 1024,
                    max_pdf_pages=args.max_pdf_pages,
                    enable_ocr=args.enable_ocr,
                    ocr_max_pages=args.ocr_max_pages,
                    pdf_render_scale=args.pdf_render_scale,
                )
            )

    attachment_types = [item.attachment_type for item in attachment_results] or [
        classify_attachment(url, "") for url in detail.attachment_urls
    ]
    attachment_text = build_attachment_text(attachment_results)
    content_source = "body" if detail.body_text else ("attachment" if attachment_text else "none")
    needs_playwright = detail.is_challenge or (
        not detail.body_text and not detail.attachment_urls and not detail.is_empty_template
    )
    category = detect_combination(detail.body_text, attachment_types, detail.has_viewer_link)
    output_row = build_output_row(
        row_number=row_number,
        source_row=row,
        cleaned_url=cleaned_url,
        detail_status_code=detail_response.status_code,
        detail_body_text=detail.body_text,
        body_selector=detail.body_selector,
        attachment_urls=detail.attachment_urls,
        attachment_types=attachment_types,
        attachment_results=attachment_results,
        attachment_text=attachment_text,
        content_source=content_source,
        category=category,
        needs_playwright=needs_playwright,
        is_challenge=detail.is_challenge,
        is_empty_template=detail.is_empty_template,
        detail_error=detail_response.error,
    )
    return PipelineResult(output_row, needs_playwright, category)


def run_pipeline(args: argparse.Namespace) -> dict[str, object]:
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main_writer = CsvShardWriter(
        output_dir=args.output_dir,
        basename="documents",
        fieldnames=OUTPUT_COLUMNS,
        rows_per_part=args.rows_per_part,
        flush_every=args.flush_every,
    )
    needs_writer = CsvShardWriter(
        output_dir=args.output_dir,
        basename="needs_playwright",
        fieldnames=OUTPUT_COLUMNS,
        rows_per_part=max(args.rows_per_part, 1),
        flush_every=args.flush_every,
    )

    counts: dict[str, int] = {}
    needs_playwright_count = 0
    processed = 0
    started = perf_counter()

    row_iter = iter_input_rows(args.input_csv, args.start_row, args.limit)
    with ThreadPoolExecutor(max_workers=args.detail_workers) as executor:
        in_flight: dict[Future[PipelineResult], int] = {}

        while True:
            while len(in_flight) < args.max_in_flight:
                try:
                    row_number, row = next(row_iter)
                except StopIteration:
                    break
                in_flight[executor.submit(process_row, row_number, row, args)] = row_number

            if not in_flight:
                break

            done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                row_number = in_flight.pop(future)
                result = future.result()
                main_writer.write_row(result.row)
                if result.needs_playwright:
                    needs_writer.write_row(result.row)
                    needs_playwright_count += 1
                counts[result.category] = counts.get(result.category, 0) + 1
                processed += 1
                if processed % args.progress_every == 0:
                    elapsed = perf_counter() - started
                    rate = processed / elapsed if elapsed > 0 else 0.0
                    print(
                        f"processed={processed} last_row={row_number} "
                        f"rate={rate:.2f}/s needs_playwright={needs_playwright_count}"
                    )

    main_writer.close()
    needs_writer.close()

    elapsed = perf_counter() - started
    summary = {
        "input_csv": args.input_csv,
        "output_dir": args.output_dir,
        "start_row": args.start_row,
        "limit": args.limit,
        "processed": processed,
        "needs_playwright": needs_playwright_count,
        "elapsed_seconds": round(elapsed, 2),
        "rows_per_second": round(processed / elapsed, 4) if elapsed > 0 else 0.0,
        "category_counts": counts,
    }
    summary_path = Path(args.output_dir) / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Streaming extractor for list_documents.csv")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--detail-workers", type=int, default=4)
    parser.add_argument("--max-in-flight", type=int, default=16)
    parser.add_argument("--request-timeout", type=int, default=30)
    parser.add_argument("--rows-per-part", type=int, default=50000)
    parser.add_argument("--flush-every", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=20)
    parser.add_argument("--max-attachment-mb", type=int, default=50)
    parser.add_argument("--max-pdf-pages", type=int, default=0)
    parser.add_argument("--enable-ocr", action="store_true", default=True)
    parser.add_argument("--no-enable-ocr", dest="enable_ocr", action="store_false")
    parser.add_argument("--ocr-max-pages", type=int, default=2)
    parser.add_argument("--pdf-render-scale", type=float, default=2.0)
    parser.add_argument("--extract-attachments", action="store_true", default=True)
    parser.add_argument("--no-extract-attachments", dest="extract_attachments", action="store_false")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = run_pipeline(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
