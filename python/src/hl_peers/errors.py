"""The error taxonomy of CLIENT_SPEC.md §5.1, as an exception hierarchy.

Every error carries the parsed rate-limit block and, where the body was
``application/problem+json``, the raw problem object.

``FilterUnavailable``, ``ChainNotServed`` and ``CursorExpired`` are **subclasses
of** :class:`BadRequest`: catching ``BadRequest`` catches all four.

🔴 Discrimination is on ``problem.type`` by suffix, never on ``title`` or
``detail``. The numbers quoted in a 429's ``detail`` are prose and disagree with
the plan's own tier table — this library never parses them (D-A).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Type

from .models import RateLimit

__all__ = [
    "ERROR_KINDS",
    "BadRequest",
    "ChainNotServed",
    "CursorExpired",
    "DecodeError",
    "FilterUnavailable",
    "HlPeersError",
    "Invalid",
    "NotFound",
    "RateLimited",
    "ServerError",
    "TransportError",
    "Truncated",
    "Unauthorized",
    "Unavailable",
]


class HlPeersError(Exception):
    """Base of every error this library raises.

    ``api_version`` is the response's ``API-Version`` header (``"1"`` today),
    or ``None`` for an error raised before or without a response. It is
    informational: the library never fails on a different value.
    """

    kind = "Error"

    def __init__(
        self,
        message: str,
        *,
        rate_limit: Optional[RateLimit] = None,
        problem: Optional[Mapping[str, Any]] = None,
        status: Optional[int] = None,
        api_version: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.rate_limit: RateLimit = rate_limit if rate_limit is not None else RateLimit()
        self.problem: Optional[Mapping[str, Any]] = problem
        self.status = status
        self.api_version = api_version


class RateLimited(HlPeersError):
    """``429``. ``signup_url`` comes from the problem body, never from prose.

    ``reset_at`` is the absolute UTC instant to wait until — a timezone-aware
    :class:`~datetime.datetime` computed from the client's injectable clock at
    response time: ``now + retry_after`` when ``Retry-After`` is known, else
    ``now + reset`` when ``RateLimit-Reset`` is known, else ``None``. Nothing
    needs to parse a header.

    With the default ``max_retries=0`` the client never sleeps for you: a 429
    raises this at once and the caller decides whether to wait.
    """

    kind = "RateLimited"

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[int] = None,
        signup_url: Optional[str] = None,
        reset_at: Optional[datetime] = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        rl = self.rate_limit
        self.retry_after = retry_after if retry_after is not None else rl.retry_after
        self.limit = rl.limit
        self.remaining = rl.remaining
        self.reset = rl.reset
        self.policy = rl.policy
        self.signup_url = signup_url
        self.reset_at = reset_at


class BadRequest(HlPeersError):
    """Any ``400``. Never retried: a 400 is deterministic."""

    kind = "BadRequest"

    def __init__(
        self,
        message: str,
        *,
        parameter: Optional[str] = None,
        allowed: Optional[List[str]] = None,
        detail: Optional[str] = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.parameter = parameter
        self.allowed = allowed
        self.detail = detail


class FilterUnavailable(BadRequest):
    """``400 …/filter-unavailable`` — the attribute is **not collected**.

    🔴 Never swallow this into an empty list: an empty list would read to an
    operator as a finding about the network.
    """

    kind = "FilterUnavailable"

    @property
    def filter(self) -> Optional[str]:
        return self.parameter


class ChainNotServed(BadRequest):
    """``400 …/chain-not-served``."""

    kind = "ChainNotServed"


class CursorExpired(BadRequest):
    """``400 …/cursor-expired`` — the cursor belongs to an older graph generation.

    Intended handling: catch it and start the walk over. The library will never
    restart it for you, because that would mix two generations into one result
    set, which is exactly what the server refused to do.
    """

    kind = "CursorExpired"


class Unauthorized(HlPeersError):
    """``401``. Omitting a key is *not* a 401 — anonymous is a supported tier."""

    kind = "Unauthorized"

    def __init__(self, message: str, *, detail: Optional[str] = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.detail = detail


class NotFound(HlPeersError):
    """``404``, or a missing config file in :meth:`GossipConfig.load`."""

    kind = "NotFound"

    def __init__(
        self,
        message: str,
        *,
        detail: Optional[str] = None,
        path: Optional[str] = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.detail = detail
        self.path = path


class Unavailable(HlPeersError):
    """``503``, **and** an ``available: false`` envelope where a config would
    otherwise be built (D-G). An unbuildable graph is not an empty peer set."""

    kind = "Unavailable"

    def __init__(self, message: str, *, detail: Optional[str] = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.detail = detail


class ServerError(HlPeersError):
    """Any other ``5xx``."""

    kind = "Server"

    def __init__(self, message: str, *, body_excerpt: Optional[str] = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.body_excerpt = body_excerpt


class TransportError(HlPeersError):
    """Connect/read timeout, DNS, TLS, connection reset."""

    kind = "Transport"

    def __init__(self, message: str, *, cause: Optional[BaseException] = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.cause = cause


class DecodeError(HlPeersError):
    """The body is not JSON, or does not match the schema."""

    kind = "Decode"

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[BaseException] = None,
        body_excerpt: Optional[str] = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.cause = cause
        self.body_excerpt = body_excerpt


class Invalid(HlPeersError):
    """Client-side validation: a query parameter, an IP, or a config field."""

    kind = "Invalid"

    def __init__(self, parameter: str, reason: str, **kw: Any) -> None:
        super().__init__("%s: %s" % (parameter, reason), **kw)
        self.parameter = parameter
        self.reason = reason


class Truncated(HlPeersError):
    """``iter_peers`` hit ``max_items`` with ``on_truncate="raise"``."""

    kind = "Truncated"

    def __init__(self, seen: int, **kw: Any) -> None:
        super().__init__(
            "iteration stopped at max_items after %d peers; raise max_items or narrow "
            "the query" % seen,
            **kw,
        )
        self.seen = seen


#: The CLIENT_SPEC §5.1 variant name -> exception class. The conformance runner
#: maps ``expect.error.kind`` through this table.
ERROR_KINDS: Dict[str, Type[HlPeersError]] = {
    "RateLimited": RateLimited,
    "FilterUnavailable": FilterUnavailable,
    "ChainNotServed": ChainNotServed,
    "CursorExpired": CursorExpired,
    "BadRequest": BadRequest,
    "Unauthorized": Unauthorized,
    "NotFound": NotFound,
    "Unavailable": Unavailable,
    "Server": ServerError,
    "Transport": TransportError,
    "Decode": DecodeError,
    "Invalid": Invalid,
    "Truncated": Truncated,
}


def excerpt(body: bytes, limit: int = 512) -> str:
    """At most ``limit`` bytes of a body, truncated on a UTF-8 boundary."""
    if len(body) <= limit:
        return body.decode("utf-8", "replace")
    return body[:limit].decode("utf-8", "ignore")
