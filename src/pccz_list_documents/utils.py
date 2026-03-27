from __future__ import annotations

import json
import re
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse


WHITESPACE_RE = re.compile(r"\s+")


def clean_url(value: str) -> str:
    return WHITESPACE_RE.sub("", value or "")


def collapse_text_lines(lines: Iterable[str]) -> str:
    return "\n".join(line.strip() for line in lines if line and line.strip())


def make_preview(text: str, limit: int = 200) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = WHITESPACE_RE.sub(" ", text.replace("\n", " ")).strip()
    return text[:limit]


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "fn" in query and query["fn"]:
        return unquote(query["fn"][0])
    path_name = parsed.path.rsplit("/", 1)[-1]
    return unquote(path_name)


def extension_from_url(url: str) -> str:
    name = filename_from_url(url).lower()
    if "." in name:
        return "." + name.rsplit(".", 1)[-1]
    path = urlparse(url).path.lower()
    leaf = path.rsplit("/", 1)[-1]
    if "." in leaf:
        return "." + leaf.rsplit(".", 1)[-1]
    return ""
