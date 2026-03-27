from __future__ import annotations

import threading
import warnings
from dataclasses import dataclass
from typing import Mapping

warnings.filterwarnings("ignore", message="urllib3 .* doesn't match a supported version!")

import requests
from requests.adapters import HTTPAdapter

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

_thread_local = threading.local()


@dataclass(slots=True)
class FetchResponse:
    url: str
    final_url: str
    status_code: int | None
    content_type: str
    text: str
    content: bytes
    error: str | None


def _proxy_key(proxies: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not proxies:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in proxies.items()))


def _build_session(pool_size: int = 20, proxies: Mapping[str, str] | None = None) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    if proxies:
        session.proxies.update(dict(proxies))
    return session


def get_session(proxies: Mapping[str, str] | None = None) -> requests.Session:
    sessions = getattr(_thread_local, "sessions", None)
    if sessions is None:
        sessions = {}
        _thread_local.sessions = sessions
    key = _proxy_key(proxies)
    session = sessions.get(key)
    if session is None:
        session = _build_session(proxies=proxies)
        sessions[key] = session
    return session


class RequestsFetcher:
    def __init__(
        self,
        timeout: int = 30,
        verify_ssl: bool = True,
        proxies: Mapping[str, str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.proxies = dict(proxies or {})

    def fetch_text(self, url: str) -> FetchResponse:
        session = get_session(self.proxies)
        try:
            response = session.get(url, timeout=self.timeout, verify=self.verify_ssl)
            response.encoding = response.apparent_encoding or response.encoding
            return FetchResponse(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                text=response.text or "",
                content=response.content or b"",
                error=None,
            )
        except Exception as exc:
            return FetchResponse(
                url=url,
                final_url=url,
                status_code=None,
                content_type="",
                text="",
                content=b"",
                error=f"{type(exc).__name__}: {exc}",
            )

    def fetch_binary(self, url: str) -> FetchResponse:
        session = get_session(self.proxies)
        try:
            response = session.get(url, timeout=self.timeout, verify=self.verify_ssl)
            return FetchResponse(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                text="",
                content=response.content or b"",
                error=None,
            )
        except Exception as exc:
            return FetchResponse(
                url=url,
                final_url=url,
                status_code=None,
                content_type="",
                text="",
                content=b"",
                error=f"{type(exc).__name__}: {exc}",
            )
