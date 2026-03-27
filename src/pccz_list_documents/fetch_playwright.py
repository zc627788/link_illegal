from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Browser, sync_playwright

from .extractors import parse_detail_html
from .utils import clean_url, make_preview


@dataclass(slots=True)
class RenderedDetail:
    body_text: str
    attachment_urls: list[str]
    network_attachment_urls: list[str]
    is_challenge: bool
    has_viewer_link: bool


def _extract_file_url_from_viewer(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    file_values = query.get("file") or []
    if not file_values:
        return None
    value = unquote(file_values[0])
    return value if "filedownloads/" in value else None


def _normalize_file_url(url: str) -> str:
    return unquote(url or "")


def _pick_url(row: dict[str, str]) -> str:
    candidates = [
        row.get("清洗后文书详情链接", ""),
        row.get("文书详情链接", ""),
    ]
    for key, value in row.items():
        if value and ("详情链接" in key or key.lower().endswith("url")):
            candidates.append(value)
    for candidate in candidates:
        cleaned = clean_url(candidate)
        if cleaned.startswith("http"):
            return cleaned
    return ""


def render_detail(
    browser: Browser,
    url: str,
    wait_ms: int,
) -> RenderedDetail:
    page = browser.new_page()
    network_urls: set[str] = set()

    def on_request(request) -> None:
        request_url = request.url
        if "viewer.html?file=" in request_url:
            extracted = _extract_file_url_from_viewer(request_url)
            if extracted:
                network_urls.add(extracted)
        elif "filedownloads/" in request_url:
            network_urls.add(request_url)

    page.on("request", on_request)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        detail = parse_detail_html(page.content())
        merged_map = {
            _normalize_file_url(item): item
            for item in list(detail.attachment_urls) + list(network_urls)
        }
        return RenderedDetail(
            body_text=detail.body_text,
            attachment_urls=sorted(merged_map.values()),
            network_attachment_urls=sorted(network_urls),
            is_challenge=detail.is_challenge,
            has_viewer_link=detail.has_viewer_link,
        )
    finally:
        page.remove_listener("request", on_request)
        page.close()


def run_repair(input_csv: str, output_csv: str, wait_ms: int, proxy_server: str | None = None) -> dict[str, int]:
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"processed": 0, "recovered_body": 0, "recovered_attachment": 0}

    with open(input_csv, "r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = list(reader.fieldnames or [])
        for extra in [
            "Playwright正文长度",
            "Playwright附件数量",
            "Playwright网络附件数量",
            "Playwright是否命中反爬",
            "Playwright内容组合类别",
            "Playwright正文预览",
            "Playwright附件链接列表",
        ]:
            if extra not in fieldnames:
                fieldnames.append(extra)

        with output_path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()

            with sync_playwright() as playwright:
                launch_args: dict[str, object] = {"headless": True}
                if proxy_server:
                    launch_args["proxy"] = {"server": proxy_server}
                browser = playwright.chromium.launch(**launch_args)
                for row in reader:
                    url = _pick_url(row)
                    if not url:
                        row["Playwright正文长度"] = 0
                        row["Playwright附件数量"] = 0
                        row["Playwright网络附件数量"] = 0
                        row["Playwright是否命中反爬"] = "否"
                        row["Playwright正文预览"] = ""
                        row["Playwright附件链接列表"] = "[]"
                        row["Playwright内容组合类别"] = "无可用URL"
                        writer.writerow(row)
                        counts["processed"] += 1
                        continue

                    detail = render_detail(browser, url, wait_ms)
                    row["Playwright正文长度"] = len(detail.body_text)
                    row["Playwright附件数量"] = len(detail.attachment_urls)
                    row["Playwright网络附件数量"] = len(detail.network_attachment_urls)
                    row["Playwright是否命中反爬"] = "是" if detail.is_challenge else "否"
                    row["Playwright正文预览"] = make_preview(detail.body_text)
                    row["Playwright附件链接列表"] = json.dumps(detail.attachment_urls, ensure_ascii=False)
                    if detail.body_text:
                        row["Playwright内容组合类别"] = "恢复正文"
                        counts["recovered_body"] += 1
                    elif detail.attachment_urls:
                        row["Playwright内容组合类别"] = "恢复附件"
                        counts["recovered_attachment"] += 1
                    else:
                        row["Playwright内容组合类别"] = "未恢复"
                    writer.writerow(row)
                    counts["processed"] += 1
                browser.close()
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repair unresolved records with Playwright")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--proxy-server", default=None)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_repair(args.input_csv, args.output_csv, args.wait_ms, args.proxy_server)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
