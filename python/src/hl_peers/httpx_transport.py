"""A synchronous transport backed by ``httpx`` — the optional ``[httpx]`` extra.

The stdlib path is first-class and this one is opt-in: use it when you already
run httpx and want its connection pooling, proxies or HTTP/2.

    pip install "hl-peers[httpx]"
    client = PeerApiClient(transport=HttpxTransport())
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .errors import TransportError
from .transport import HttpResponse, normalize_headers

__all__ = ["HttpxTransport"]


def _import_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "the httpx transport needs the optional extra: pip install 'hl-peers[httpx]'"
        ) from exc
    return httpx


class HttpxTransport:
    """Wraps an ``httpx.Client``. Closing it is the caller's business unless
    this transport created it."""

    def __init__(self, client: Optional[Any] = None, **client_kwargs: Any) -> None:
        httpx = _import_httpx()
        self._owned = client is None
        self._client = client if client is not None else httpx.Client(**client_kwargs)
        self._httpx = httpx

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        try:
            response = self._client.request(method, url, headers=dict(headers), timeout=timeout)
        except self._httpx.HTTPError as exc:
            raise TransportError(
                "%s %s failed: %s" % (method, url.split("?", 1)[0], exc), cause=exc
            ) from exc
        return HttpResponse(
            status=int(response.status_code),
            headers=normalize_headers(response.headers.items()),
            body=response.content,
        )

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def __enter__(self) -> HttpxTransport:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
