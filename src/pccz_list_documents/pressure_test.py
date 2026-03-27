from __future__ import annotations

import argparse
import csv
import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

from .extractors import parse_detail_html
from .fetch_requests import RequestsFetcher
from .stage_common import iter_input_rows
from .utils import clean_url


def _proxy_mapping(args: argparse.Namespace) -> dict[str, str]:
    proxies: dict[str, str] = {}
    if args.http_proxy:
        proxies["http"] = args.http_proxy
    if args.https_proxy:
        proxies["https"] = args.https_proxy
    return proxies


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = int(round((len(values) - 1) * p))
    return values[index]


def _worker(raw: tuple[int, dict[str, str]], args: argparse.Namespace) -> dict[str, object]:
    row_number, row = raw
    cleaned_url = clean_url(row.get("文书详情链接", ""))
    fetcher = RequestsFetcher(timeout=args.request_timeout, proxies=_proxy_mapping(args))
    started = perf_counter()
    response = fetcher.fetch_text(cleaned_url)
    latency_ms = (perf_counter() - started) * 1000
    detail = parse_detail_html(response.text)
    return {
        "row_number": row_number,
        "title": row.get("文书标题", ""),
        "url": cleaned_url,
        "status_code": response.status_code or "",
        "latency_ms": round(latency_ms, 2),
        "error": response.error or "",
        "challenge": detail.is_challenge,
        "empty_template": detail.is_empty_template,
        "has_body": bool(detail.body_text),
        "has_attach": bool(detail.attachment_urls),
        "has_viewer": detail.has_viewer_link,
    }


def run_scenario(
    rows: list[tuple[int, dict[str, str]]],
    workers: int,
    round_index: int,
    args: argparse.Namespace,
    detail_writer: csv.DictWriter | None,
    detail_handle,
) -> dict[str, object]:
    started = perf_counter()
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_worker, row, args) for row in rows]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if detail_writer is not None:
                detail_writer.writerow(
                    {
                        "round": round_index,
                        "workers": workers,
                        **result,
                    }
                )
                if detail_handle:
                    detail_handle.flush()

    elapsed = perf_counter() - started
    latencies = [float(item["latency_ms"]) for item in results]
    total = len(results)
    challenge_count = sum(1 for item in results if item["challenge"])
    error_count = sum(1 for item in results if item["error"])
    body_count = sum(1 for item in results if item["has_body"])
    attach_count = sum(1 for item in results if item["has_attach"])
    viewer_count = sum(1 for item in results if item["has_viewer"])
    empty_count = sum(1 for item in results if item["empty_template"])

    return {
        "round": round_index,
        "workers": workers,
        "requests": total,
        "elapsed_seconds": round(elapsed, 2),
        "requests_per_second": round(total / elapsed, 4) if elapsed > 0 else 0.0,
        "challenge_count": challenge_count,
        "challenge_rate": round(challenge_count / total, 4) if total else 0.0,
        "error_count": error_count,
        "error_rate": round(error_count / total, 4) if total else 0.0,
        "body_count": body_count,
        "attach_count": attach_count,
        "viewer_count": viewer_count,
        "empty_template_count": empty_count,
        "latency_avg_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "latency_p50_ms": round(_percentile(latencies, 0.5), 2),
        "latency_p95_ms": round(_percentile(latencies, 0.95), 2),
    }


def run_pressure_test(args: argparse.Namespace) -> dict[str, object]:
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    rows = list(iter_input_rows(args.input_csv, args.start_row, args.limit))
    worker_values = [int(item.strip()) for item in args.workers.split(",") if item.strip()]
    summary_rows: list[dict[str, object]] = []

    detail_path = Path(args.output_dir) / "pressure_details.csv"
    detail_handle = detail_path.open("w", encoding="utf-8-sig", newline="") if args.write_detail_csv else None
    detail_writer = None
    if detail_handle is not None:
        detail_writer = csv.DictWriter(
            detail_handle,
            fieldnames=[
                "round",
                "workers",
                "row_number",
                "title",
                "url",
                "status_code",
                "latency_ms",
                "error",
                "challenge",
                "empty_template",
                "has_body",
                "has_attach",
                "has_viewer",
            ],
        )
        detail_writer.writeheader()
        detail_handle.flush()

    try:
        for round_index in range(1, args.rounds + 1):
            for workers in worker_values:
                print(f"pressure round={round_index} workers={workers} rows={len(rows)}")
                summary = run_scenario(rows, workers, round_index, args, detail_writer, detail_handle)
                summary_rows.append(summary)
                print(json.dumps(summary, ensure_ascii=False))
    finally:
        if detail_handle is not None:
            detail_handle.close()

    summary_csv_path = Path(args.output_dir) / "pressure_summary.csv"
    with summary_csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0].keys()) if summary_rows else [])
        if summary_rows:
            writer.writeheader()
            writer.writerows(summary_rows)

    result = {
        "input_csv": args.input_csv,
        "output_dir": args.output_dir,
        "start_row": args.start_row,
        "limit": args.limit,
        "workers": worker_values,
        "rounds": args.rounds,
        "summaries": summary_rows,
    }
    (Path(args.output_dir) / "pressure_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pressure test the detail-page stage")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--workers", default="2,4,8")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--request-timeout", type=int, default=20)
    parser.add_argument("--write-detail-csv", action="store_true", default=True)
    parser.add_argument("--no-write-detail-csv", dest="write_detail_csv", action="store_false")
    parser.add_argument("--http-proxy", default=None)
    parser.add_argument("--https-proxy", default=None)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = run_pressure_test(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
