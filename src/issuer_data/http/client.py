"""A thin requests.Session wrapper with rate limiting and retry/backoff.

Honors HTTPS_PROXY (requests reads the environment) so it works behind the agent
proxy. Retries on 429/5xx/timeouts with exponential backoff via tenacity.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_settings
from ..logging import get_logger
from ..utils.ratelimit import RateLimiter

log = get_logger(__name__)


class RetryableStatus(Exception):
    """Raised for HTTP status codes worth retrying (429, 5xx)."""


class HttpClient:
    def __init__(
        self,
        rate_limit: float | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.max_retries = settings.max_retries
        self.limiter = RateLimiter(rate_limit or settings.default_rate_limit)
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
        )
        if headers:
            self.session.headers.update(headers)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.limiter.acquire()
        timeout = kwargs.pop("timeout", self.timeout)
        resp = self.session.request(method, url, timeout=timeout, **kwargs)
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise RetryableStatus(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        return resp

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        attempts = max(1, self.max_retries)

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception_type(
                (RetryableStatus, requests.exceptions.Timeout, requests.exceptions.ConnectionError)
            ),
        )
        def _do() -> requests.Response:
            return self._request(method, url, **kwargs)

        return _do()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        return self.get(url, **kwargs).content

    def close(self) -> None:
        self.session.close()
