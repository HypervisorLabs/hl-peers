"""The HTTP seam.

The default transport is stdlib ``urllib`` — node runners' boxes often have a
bare system Python, so the zero-dependency path is first-class. Anything with a
``request()`` of this shape can be injected instead, which is how the test suite
replays ``fixtures/http`` without a socket.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Protocol, Tuple, runtime_checkable

from .errors import TransportError

__all__ = ["HttpResponse", "Transport", "UrllibTransport", "normalize_headers"]


def normalize_headers(pairs: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Lower-case header names, so lookups are case-insensitive everywhere."""
    return {str(k).lower(): str(v) for k, v in pairs}


@dataclass(frozen=True)
class HttpResponse:
    """A raw response. ``headers`` keys are lower-cased."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@runtime_checkable
class Transport(Protocol):
    """The injectable HTTP seam.

    An implementation returns an :class:`HttpResponse` for **every** HTTP status
    — a 4xx or 5xx is a response, not an exception — and raises
    :class:`~hl_peers.errors.TransportError` only for connect/read timeouts,
    DNS, TLS and resets.
    """

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...  # pragma: no cover - protocol


class UrllibTransport:
    """The default, zero-dependency transport."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=normalize_headers(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:  # a status, not a failure
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - body already consumed
                pass
            return HttpResponse(
                status=int(exc.code),
                headers=normalize_headers(exc.headers.items() if exc.headers else []),
                body=body,
            )
        except urllib.error.URLError as exc:
            raise TransportError(
                "%s %s failed: %s" % (method, _redact(url), exc.reason), cause=exc
            ) from exc
        except OSError as exc:  # timeouts, resets
            raise TransportError(
                "%s %s failed: %s" % (method, _redact(url), exc), cause=exc
            ) from exc


def _redact(url: str) -> str:
    """A key never reaches a log or an error message."""
    return url.split("?", 1)[0]
