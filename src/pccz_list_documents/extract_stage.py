from __future__ import annotations

import argparse
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from time import perf_counter

from playwright.sync_api import sync_playwright

from .attachments import AttachmentExtraction, build_attachment_text, extract_attachment_text, summarize_attachment_statuses
from .fetch_playwright import render_detail
from .fetch_requests import RequestsFetcher
from .stage_common import FINAL_COLUMNS, detect_combination, detail_type_from_url, iter_csv_rows
from .utils import clean_url, json_dumps, make_preview
from .writer import CsvShardWriter


class ExtractResult:
    def __init__(self, row: dict[str, object], category: str) -> None:
        self.row = row
        self.category = category


def _proxy_mapping(args: argparse.Namespace) -> dict[str, str]:
    proxies: dict[str, str] = {}
    if args.http_proxy:
        proxies["http"] = args.http_proxy
    if args.https_proxy:
        proxies["https"] = args.https_proxy
    return proxies


def _json_load_list(value: str) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _combine_content(body_text: str, attachment_text: str) -> tuple[str, str]:
    if body_text and attachment_text:
        return "body+attachment", f"{body_text}\n\n{attachment_text}"
    if body_text:
        return "body", body_text
    if attachment_text:
        return "attachment", attachment_text
    return "none", ""


def _attachment_type_counts(attachments: list[AttachmentExtraction]) -> dict[str, int]:
    attachment_types = [item.attachment_type for item in attachments]
    return {
        "pdf": attachment_types.count("pdf"),
        "word": sum(1 for item in attachment_types if item in {"word", "rtf", "legacy_word"}),
        "other": sum(1 for item in attachment_types if item not in {"pdf", "word", "rtf", "legacy_word"}),
    }


def _extract_row(
    row: dict[str, str],
    args: argparse.Namespace,
    browser=None,
) -> ExtractResult:
    cleaned_url = row.get("清洗后文书详情链接") or clean_url(row.get("文书详情链接", ""))
    fetcher = RequestsFetcher(timeout=args.request_timeout, proxies=_proxy_mapping(args))
    body_text = row.get("正文文本", "")
    attachment_urls = _json_load_list(row.get("附件链接列表", ""))
    detection_source = row.get("检测来源", "requests") or "requests"
    has_viewer_link = row.get("是否有viewer链路", "否") == "是"
    needs_playwright = row.get("是否需要Playwright", "否") == "是"

    if browser is not None and needs_playwright:
        rendered = render_detail(browser, cleaned_url, args.playwright_wait_ms)
        if rendered.body_text:
            body_text = rendered.body_text
        if rendered.attachment_urls:
            attachment_urls = rendered.attachment_urls
        has_viewer_link = has_viewer_link or rendered.has_viewer_link
        detection_source = "playwright"
        needs_playwright = False if (rendered.body_text or rendered.attachment_urls) else True

    attachment_results: list[AttachmentExtraction] = []
    if args.extract_attachments:
        for attachment_url in attachment_urls:
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

    attachment_text = build_attachment_text(attachment_results)
    content_source, content_text = _combine_content(body_text, attachment_text)
    attachment_types = [item.attachment_type for item in attachment_results]
    category = detect_combination(body_text, attachment_types, has_viewer_link)
    attachment_counts = _attachment_type_counts(attachment_results)

    output_row = dict(row)
    output_row.update(
        {
            "清洗后文书详情链接": cleaned_url,
            "详情页类型": row.get("详情页类型") or detail_type_from_url(cleaned_url),
            "检测来源": detection_source,
            "是否需要Playwright": "是" if needs_playwright else "否",
            "正文文本长度": len(body_text),
            "正文预览": make_preview(body_text),
            "正文文本": body_text,
            "附件数量": len(attachment_urls),
            "附件文件名列表": json_dumps([item.filename for item in attachment_results]),
            "附件类型列表": json_dumps([item.attachment_type for item in attachment_results]),
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
            "附件格式统计": json_dumps(attachment_counts),
            "附件链接列表": json_dumps(attachment_urls),
            "附件提取状态统计": json_dumps(summarize_attachment_statuses(attachment_results)),
            "附件文本长度": len(attachment_text),
            "附件文本预览": make_preview(attachment_text),
            "内容来源": content_source,
            "content_text": content_text,
            "内容组合类别": category,
        }
    )
    return ExtractResult(output_row, category)


def run_extract(args: argparse.Namespace) -> dict[str, object]:
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    final_writer = CsvShardWriter(
        output_dir=args.output_dir,
        basename="documents",
        fieldnames=FINAL_COLUMNS,
        rows_per_part=args.rows_per_part,
        flush_every=args.flush_every,
    )

    counts: dict[str, int] = {}
    processed = 0
    started = perf_counter()

    input_iter = iter(iter_csv_rows(args.input_path, "discover.part*.csv"))
    exhausted = False
    playwright = sync_playwright().start() if args.playwright_on_needs else None
    browser = None
    if playwright:
        launch_args: dict[str, object] = {"headless": True}
        if args.playwright_proxy_server:
            launch_args["proxy"] = {"server": args.playwright_proxy_server}
        browser = playwright.chromium.launch(**launch_args)

    try:
        with ThreadPoolExecutor(max_workers=args.extract_workers) as executor:
            in_flight: dict[Future[ExtractResult], str] = {}

            while True:
                while len(in_flight) < args.max_in_flight and not exhausted:
                    try:
                        row = next(input_iter)
                    except StopIteration:
                        exhausted = True
                        break
                    if row.get("是否需要Playwright", "否") == "是" and browser is not None:
                        result = _extract_row(row, args, browser=browser)
                        final_writer.write_row(result.row)
                        counts[result.category] = counts.get(result.category, 0) + 1
                        processed += 1
                        if processed % args.progress_every == 0:
                            elapsed = perf_counter() - started
                            rate = processed / elapsed if elapsed > 0 else 0.0
                            print(f"extract processed={processed} rate={rate:.2f}/s")
                        continue
                    in_flight[executor.submit(_extract_row, row, args)] = row.get("输入行号", "")

                if not in_flight:
                    if exhausted:
                        break
                    continue

                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    in_flight.pop(future, None)
                    result = future.result()
                    final_writer.write_row(result.row)
                    counts[result.category] = counts.get(result.category, 0) + 1
                    processed += 1
                    if processed % args.progress_every == 0:
                        elapsed = perf_counter() - started
                        rate = processed / elapsed if elapsed > 0 else 0.0
                        print(f"extract processed={processed} rate={rate:.2f}/s")
    finally:
        final_writer.close()
        if browser:
            browser.close()
        if playwright:
            playwright.stop()

    elapsed = perf_counter() - started
    summary = {
        "stage": "extract",
        "input_path": args.input_path,
        "output_dir": args.output_dir,
        "processed": processed,
        "elapsed_seconds": round(elapsed, 2),
        "rows_per_second": round(processed / elapsed, 4) if elapsed > 0 else 0.0,
        "category_counts": counts,
    }
    (Path(args.output_dir) / "extract_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 2: download attachments and extract full text")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--extract-workers", type=int, default=4)
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
    parser.add_argument("--playwright-on-needs", action="store_true", default=False)
    parser.add_argument("--playwright-wait-ms", type=int, default=5000)
    parser.add_argument("--playwright-proxy-server", default=None)
    parser.add_argument("--http-proxy", default=None)
    parser.add_argument("--https-proxy", default=None)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = run_extract(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
