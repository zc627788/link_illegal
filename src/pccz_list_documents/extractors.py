from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .utils import collapse_text_lines


BODY_SELECTORS = [
    "#printDiv",
    ".detail_text",
    ".detail_wrapper",
    ".article",
    ".content",
    ".news_show",
    ".cont",
]

ATTACHMENT_RE = re.compile(r"https://pccz\.court\.gov\.cn/filedownloads/[^\s\"'<>]+")
CHALLENGE_MARKER = "Please enable JavaScript and refresh the page."
EMPTY_TEMPLATE_MARKERS = [
    'var ggcBh = ""',
    'var fjL = ""',
    'var wslx = ""',
    "loading-mask",
]


@dataclass(slots=True)
class DetailExtraction:
    body_text: str
    body_selector: str | None
    attachment_urls: list[str]
    is_challenge: bool
    is_empty_template: bool
    has_viewer_link: bool


def extract_body_text(html: str, min_lines: int = 2) -> tuple[str, str | None]:
    soup = BeautifulSoup(html or "", "html.parser")
    best_text = ""
    best_selector = None
    for selector in BODY_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue
        lines = node.get_text("\n").splitlines()
        text = collapse_text_lines(lines)
        if text and len(text.splitlines()) >= min_lines and len(text) > len(best_text):
            best_text = text
            best_selector = selector
    return best_text, best_selector


def extract_attachment_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if "filedownloads/" in href:
            urls.add(href)
    for match in ATTACHMENT_RE.finditer(html or ""):
        urls.add(match.group(0).rstrip("]},);"))
    return sorted(urls)


def is_empty_template(html: str, body_text: str, attachment_urls: list[str]) -> bool:
    if body_text or attachment_urls or not html:
        return False
    marker_hits = sum(1 for marker in EMPTY_TEMPLATE_MARKERS if marker in html)
    return marker_hits >= 2


def parse_detail_html(html: str) -> DetailExtraction:
    body_text, body_selector = extract_body_text(html)
    attachment_urls = extract_attachment_urls(html)
    challenge = CHALLENGE_MARKER in (html or "")
    empty_template = is_empty_template(html, body_text, attachment_urls)
    has_viewer_link = "viewer.html?file=" in (html or "")
    return DetailExtraction(
        body_text=body_text,
        body_selector=body_selector,
        attachment_urls=attachment_urls,
        is_challenge=challenge,
        is_empty_template=empty_template,
        has_viewer_link=has_viewer_link,
    )
