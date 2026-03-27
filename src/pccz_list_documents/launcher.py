from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .discover import run_discover
from .extract_stage import run_extract


def _build_shards(start_row: int, limit: int, shard_size: int) -> list[tuple[int, int]]:
    shards: list[tuple[int, int]] = []
    remaining = limit
    cursor = start_row
    while remaining > 0:
        chunk = min(shard_size, remaining)
        shards.append((cursor, chunk))
        cursor += chunk
        remaining -= chunk
    return shards


def _run_one_shard(config: dict[str, object]) -> dict[str, object]:
    shard_name = str(config["shard_name"])
    shard_root = Path(str(config["output_root"])) / shard_name
    discover_dir = shard_root / "discover"
    extract_dir = shard_root / "extract"

    discover_summary = run_discover(
        argparse.Namespace(
            input_csv=str(config["input_csv"]),
            output_dir=str(discover_dir),
            start_row=int(config["start_row"]),
            limit=int(config["limit"]),
            detail_workers=int(config["detail_workers"]),
            max_in_flight=int(config["discover_max_in_flight"]),
            request_timeout=int(config["request_timeout"]),
            rows_per_part=int(config["rows_per_part"]),
            flush_every=int(config["flush_every"]),
            progress_every=int(config["discover_progress_every"]),
            http_proxy=config.get("http_proxy"),
            https_proxy=config.get("https_proxy"),
        )
    )
    extract_summary = run_extract(
        argparse.Namespace(
            input_path=str(discover_dir),
            output_dir=str(extract_dir),
            extract_workers=int(config["extract_workers"]),
            max_in_flight=int(config["extract_max_in_flight"]),
            request_timeout=int(config["request_timeout"]),
            rows_per_part=int(config["rows_per_part"]),
            flush_every=int(config["flush_every"]),
            progress_every=int(config["extract_progress_every"]),
            max_attachment_mb=int(config["max_attachment_mb"]),
            max_pdf_pages=int(config["max_pdf_pages"]),
            enable_ocr=bool(config["enable_ocr"]),
            ocr_max_pages=int(config["ocr_max_pages"]),
            pdf_render_scale=float(config["pdf_render_scale"]),
            extract_attachments=bool(config["extract_attachments"]),
            playwright_on_needs=bool(config["playwright_on_needs"]),
            playwright_wait_ms=int(config["playwright_wait_ms"]),
            playwright_proxy_server=config.get("playwright_proxy_server"),
            http_proxy=config.get("http_proxy"),
            https_proxy=config.get("https_proxy"),
        )
    )
    result = {
        "shard_name": shard_name,
        "start_row": config["start_row"],
        "limit": config["limit"],
        "discover_summary": discover_summary,
        "extract_summary": extract_summary,
    }
    (shard_root / "shard_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def run_launcher(args: argparse.Namespace) -> dict[str, object]:
    total_limit = args.limit
    if total_limit is None:
        raise ValueError("launcher 需要明确 --limit，避免误起全量任务")

    shards = _build_shards(args.start_row, total_limit, args.shard_size)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, object]] = []

    configs: list[dict[str, object]] = []
    for shard_start, shard_limit in shards:
        shard_end = shard_start + shard_limit - 1
        shard_name = f"shard_{shard_start:09d}_{shard_end:09d}"
        configs.append(
            {
                "shard_name": shard_name,
                "output_root": str(output_root),
                "input_csv": args.input_csv,
                "start_row": shard_start,
                "limit": shard_limit,
                "detail_workers": args.detail_workers,
                "discover_max_in_flight": args.discover_max_in_flight,
                "request_timeout": args.request_timeout,
                "rows_per_part": args.rows_per_part,
                "flush_every": args.flush_every,
                "discover_progress_every": args.discover_progress_every,
                "extract_workers": args.extract_workers,
                "extract_max_in_flight": args.extract_max_in_flight,
                "extract_progress_every": args.extract_progress_every,
                "max_attachment_mb": args.max_attachment_mb,
                "max_pdf_pages": args.max_pdf_pages,
                "enable_ocr": args.enable_ocr,
                "ocr_max_pages": args.ocr_max_pages,
                "pdf_render_scale": args.pdf_render_scale,
                "extract_attachments": args.extract_attachments,
                "playwright_on_needs": args.playwright_on_needs,
                "playwright_wait_ms": args.playwright_wait_ms,
                "playwright_proxy_server": args.playwright_proxy_server,
                "http_proxy": args.http_proxy,
                "https_proxy": args.https_proxy,
            }
        )

    with ProcessPoolExecutor(max_workers=args.parallel_shards) as executor:
        futures = [executor.submit(_run_one_shard, config) for config in configs]
        for future in as_completed(futures):
            result = future.result()
            summaries.append(result)
            print(
                f"completed {result['shard_name']} "
                f"discover={result['discover_summary']['processed']} "
                f"extract={result['extract_summary']['processed']}"
            )

    final_summary = {
        "input_csv": args.input_csv,
        "output_root": str(output_root),
        "shard_count": len(configs),
        "summaries": summaries,
    }
    (output_root / "launcher_summary.json").write_text(
        json.dumps(final_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return final_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shard launcher for large-scale extraction")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--shard-size", type=int, default=50000)
    parser.add_argument("--parallel-shards", type=int, default=2)
    parser.add_argument("--detail-workers", type=int, default=4)
    parser.add_argument("--discover-max-in-flight", type=int, default=16)
    parser.add_argument("--extract-workers", type=int, default=4)
    parser.add_argument("--extract-max-in-flight", type=int, default=16)
    parser.add_argument("--request-timeout", type=int, default=30)
    parser.add_argument("--rows-per-part", type=int, default=50000)
    parser.add_argument("--flush-every", type=int, default=1)
    parser.add_argument("--discover-progress-every", type=int, default=100)
    parser.add_argument("--extract-progress-every", type=int, default=20)
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
    summary = run_launcher(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
