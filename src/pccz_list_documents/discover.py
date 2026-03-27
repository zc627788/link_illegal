from __future__ import annotations

import argparse
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from time import perf_counter

from .attachments import classify_attachment
from .extractors import parse_detail_html
from .fetch_requests import RequestsFetcher
from .stage_common import DISCOVERY_COLUMNS, INPUT_COLUMNS, detail_type_from_url, detect_combination, iter_input_rows
from .utils import clean_url, json_dumps, make_preview
from .writer import CsvShardWriter


class DiscoverResult:
    def __init__(self, row: dict[str, object], needs_playwright: bool, category: str) -> None:
        self.row = row
        self.needs_playwright = needs_playwright
        self.category = category


def _proxy_mapping(args: argparse.Namespace) -> dict[str, str]:
    proxies: dict[str, str] = {}
    if args.http_proxy:
        proxies["http"] = args.http_proxy
    if args.https_proxy:
        proxies["https"] = args.https_proxy
    return proxies


def process_row(row_number: int, row: dict[str, str], args: argparse.Namespace) -> DiscoverResult:
    cleaned_url = clean_url(row.get("文书详情链接", ""))
    fetcher = RequestsFetcher(timeout=args.request_timeout, proxies=_proxy_mapping(args))
    detail_response = fetcher.fetch_text(cleaned_url)
    detail = parse_detail_html(detail_response.text)
    attachment_types = [classify_attachment(url, "") for url in detail.attachment_urls]
    needs_playwright = detail.is_challenge or (
        not detail.body_text and not detail.attachment_urls and not detail.is_empty_template
    )
    category = detect_combination(detail.body_text, attachment_types, detail.has_viewer_link)

    output_row: dict[str, object] = {column: row.get(column, "") for column in INPUT_COLUMNS}
    output_row.update(
        {
            "输入行号": row_number,
            "清洗后文书详情链接": cleaned_url,
            "详情页状态码": detail_response.status_code or "",
            "详情页类型": detail_type_from_url(cleaned_url),
            "检测来源": "requests",
            "是否命中反爬": "是" if detail.is_challenge else "否",
            "是否空模板": "是" if detail.is_empty_template else "否",
            "是否需要Playwright": "是" if needs_playwright else "否",
            "是否有viewer链路": "是" if detail.has_viewer_link else "否",
            "正文选择器": detail.body_selector or "",
            "正文文本长度": len(detail.body_text),
            "正文预览": make_preview(detail.body_text),
            "正文文本": detail.body_text,
            "附件数量": len(detail.attachment_urls),
            "附件格式统计": json_dumps(
                {
                    "pdf": attachment_types.count("pdf"),
                    "word": sum(1 for item in attachment_types if item in {"word", "rtf", "legacy_word"}),
                    "other": sum(1 for item in attachment_types if item not in {"pdf", "word", "rtf", "legacy_word"}),
                }
            ),
            "附件链接列表": json_dumps(detail.attachment_urls),
            "内容组合类别": category,
            "处理错误": detail_response.error or "",
        }
    )
    return DiscoverResult(output_row, needs_playwright, category)


def run_discover(args: argparse.Namespace) -> dict[str, object]:
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    discover_writer = CsvShardWriter(
        output_dir=args.output_dir,
        basename="discover",
        fieldnames=DISCOVERY_COLUMNS,
        rows_per_part=args.rows_per_part,
        flush_every=args.flush_every,
    )
    needs_writer = CsvShardWriter(
        output_dir=args.output_dir,
        basename="needs_playwright",
        fieldnames=DISCOVERY_COLUMNS,
        rows_per_part=args.rows_per_part,
        flush_every=args.flush_every,
    )

    counts: dict[str, int] = {}
    needs_playwright_count = 0
    processed = 0
    started = perf_counter()

    row_iter = iter_input_rows(args.input_csv, args.start_row, args.limit)
    with ThreadPoolExecutor(max_workers=args.detail_workers) as executor:
        in_flight: dict[Future[DiscoverResult], int] = {}
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
                discover_writer.write_row(result.row)
                if result.needs_playwright:
                    needs_writer.write_row(result.row)
                    needs_playwright_count += 1
                counts[result.category] = counts.get(result.category, 0) + 1
                processed += 1
                if processed % args.progress_every == 0:
                    elapsed = perf_counter() - started
                    rate = processed / elapsed if elapsed > 0 else 0.0
                    print(
                        f"discover processed={processed} last_row={row_number} "
                        f"rate={rate:.2f}/s needs_playwright={needs_playwright_count}"
                    )

    discover_writer.close()
    needs_writer.close()

    elapsed = perf_counter() - started
    summary = {
        "stage": "discover",
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
    (Path(args.output_dir) / "discover_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 1: discover detail/attachment URLs")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--detail-workers", type=int, default=4)
    parser.add_argument("--max-in-flight", type=int, default=16)
    parser.add_argument("--request-timeout", type=int, default=30)
    parser.add_argument("--rows-per-part", type=int, default=50000)
    parser.add_argument("--flush-every", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--http-proxy", default=None)
    parser.add_argument("--https-proxy", default=None)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = run_discover(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
