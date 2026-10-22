"""Pure helpers shared by the sync and the async client.

Nothing here touches a socket: URL building, error mapping, response decoding,
the retry decision and the backoff formula live here so that the two clients
cannot drift apart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote

from .errors import (
    BadRequest,
    ChainNotServed,
    CursorExpired,
    DecodeError,
    FilterUnavailable,
    HlPeersError,
    Invalid,
    NotFound,
    RateLimited,
    ServerError,
    Unauthorized,
    Unavailable,
    excerpt,
)
from .models import (
    Envelope,
    Facet,
    Facets,
    FacetValue,
    GossipConfigResult,
    NetworkMapGraph,
    NetworkMapSummary,
    Peer,
    PeerDetail,
    PeerPage,
    RateLimit,
    compute_reset_at,
)
from .query import CLOSED_ENUMS, _nullable_tokens, check_network

__all__ = [
    "DEFAULT_BASE_PATH",
    "DEFAULT_BASE_URL",
    "FACETS_PATH",
    "NETWORK_MAP_GRAPH_PATH",
    "NETWORK_MAP_SUMMARY_PATH",
    "PEERS_PATH",
    "USER_AGENT",
    "VERSION",
    "RawResult",
    "backoff_delay",
    "build_url",
    "decode_facets",
    "decode_gossip_config",
    "decode_network_map_graph",
    "decode_network_map_summary",
    "decode_page",
    "decode_peer_detail",
    "map_error",
    "network_map_query_string",
    "normalize_base_path",
    "retry_delay",
]

#: The library version, sent in the default ``User-Agent``.
VERSION = "1.1.0"
USER_AGENT = "hl-peers-python/%s" % VERSION

#: The public API host (CLIENT_SPEC §1). Resolution order, evaluated once at
#: construction: explicit argument -> ``HL_PEER_API_BASE_URL`` -> this.
DEFAULT_BASE_URL = "https://api.hypervisor.gg"

#: The default route prefix — the ONE place it is spelled. The public API uses
#: plain paths at the host root: no
#: ``/api``, no ``/v1`` — the version is in every body and in the
#: ``API-Version`` header. The ``base_path`` option stays for a proxy that
#: mounts the API under a prefix (CLIENT_SPEC §1, D-N).
DEFAULT_BASE_PATH = ""

#: Route suffixes under ``base_path``.
PEERS_PATH = "/peers"
FACETS_PATH = "/peers/facets"
NETWORK_MAP_GRAPH_PATH = "/network-map/graph"
NETWORK_MAP_SUMMARY_PATH = "/network-map/summary"


def normalize_base_path(path: str) -> str:
    """CLIENT_SPEC §1: strip every leading/trailing ``/``; empty means no
    prefix, anything else is ``/`` + the trimmed value.

    >>> normalize_base_path("peer-api/v1/")
    '/peer-api/v1'
    >>> normalize_base_path("/")
    ''
    """
    trimmed = path.strip("/")
    return "/" + trimmed if trimmed else ""


LOGGER = logging.getLogger("hl_peers")

BASE_BACKOFF = 0.5
MAX_BACKOFF = 30.0


@dataclass(frozen=True)
class RawResult:
    """One decoded HTTP answer, before it becomes a typed result."""

    status: int
    body: Any
    rate_limit: RateLimit
    etag: Optional[str] = None
    from_cache: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def api_version(self) -> Optional[str]:
        """The ``API-Version`` header of the response actually received (for a
        cache hit, the ``304``'s). Informational: never validated."""
        return self.headers.get("api-version")

    def meta(self) -> Dict[str, Any]:
        return {
            "rate_limit": self.rate_limit,
            "etag": self.etag,
            "from_cache": self.from_cache,
            "status": self.status,
            "api_version": self.api_version,
        }


def build_url(base_url: str, path: str, query_string: str) -> str:
    url = base_url.rstrip("/") + path
    return url + "?" + query_string if query_string else url


def backoff_delay(attempt: int, rand: float) -> float:
    """``min(base * 2**n, max_delay) * (0.5 + random()/2)`` — full-width jitter in
    the lower half, so two clients started together diverge."""
    return float(min(BASE_BACKOFF * (2.0**attempt), MAX_BACKOFF) * (0.5 + rand / 2))


def retry_delay(
    status: Optional[int],
    error: Optional[HlPeersError],
    attempt: int,
    rand: float,
    max_retry_after: float,
) -> Optional[float]:
    """How long to wait before re-attempting, or ``None`` to not retry at all.

    🔴 Never retry a 4xx other than 429: a 400, 401 or 404 is deterministic and
    retrying it burns quota to receive the same answer.
    """
    if status is None:  # a transport failure
        return backoff_delay(attempt, rand)
    if status == 429:
        retry_after = getattr(error, "retry_after", None)
        if retry_after is None:
            return backoff_delay(attempt, rand)
        if retry_after > max_retry_after:
            # D-I: raise rather than block the caller's thread for minutes.
            return None
        return float(retry_after)
    if status >= 500:
        return backoff_delay(attempt, rand)
    return None


def _problem_of(headers: Mapping[str, str], body: bytes) -> Optional[Dict[str, Any]]:
    """Parse ``application/problem+json``. D-K: any other body shape falls back
    to status-only mapping, so a legacy ``{"error": …}`` body still produces the
    right variant."""
    content_type = headers.get("content-type", "")
    if "problem+json" not in content_type:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _type_suffix(problem: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The last path segment of ``problem.type``. Discrimination is on this,
    never on ``title`` or ``detail`` — prose is not an API."""
    if not problem:
        return None
    value = problem.get("type")
    if not isinstance(value, str):
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def map_error(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    rate_limit: RateLimit,
    now: Optional[datetime] = None,
) -> HlPeersError:
    """Status + ``problem.type`` -> the CLIENT_SPEC §5.1 variant.

    ``now`` is the client's injectable clock, read at response time; it anchors
    :attr:`RateLimited.reset_at`.
    """
    problem = _problem_of(headers, body)
    suffix = _type_suffix(problem)
    detail = problem.get("detail") if problem else None
    title = (problem or {}).get("title") or "HTTP %d" % status
    parameter = problem.get("parameter") if problem else None
    allowed = problem.get("allowed") if problem else None
    common: Dict[str, Any] = {
        "rate_limit": rate_limit,
        "problem": problem,
        "status": status,
        "api_version": headers.get("api-version"),
    }
    message = "%s%s" % (title, ": %s" % detail if detail else "")

    if status == 429:
        signup_url = (problem or {}).get("signup_url")
        return RateLimited(
            message,
            retry_after=rate_limit.retry_after,
            signup_url=signup_url if isinstance(signup_url, str) else None,
            reset_at=(
                compute_reset_at(now, rate_limit.retry_after, rate_limit.reset)
                if now is not None
                else None
            ),
            **common,
        )
    if status == 400:
        kw: Dict[str, Any] = dict(common, parameter=parameter, allowed=allowed, detail=detail)
        if suffix == "filter-unavailable":
            return FilterUnavailable(message, **kw)
        if suffix == "chain-not-served":
            return ChainNotServed(message, **kw)
        if suffix == "cursor-expired":
            return CursorExpired(message, **kw)
        return BadRequest(message, **kw)
    if status == 401:
        return Unauthorized(message, detail=detail, **common)
    if status == 404:
        return NotFound(message, detail=detail, **common)
    if status == 503:
        return Unavailable(message, detail=detail, **common)
    if status >= 500:
        return ServerError(
            "HTTP %d from the peer API" % status, body_excerpt=excerpt(body), **common
        )
    if 400 <= status < 500:
        return BadRequest(message, parameter=parameter, allowed=allowed, detail=detail, **common)
    return HlPeersError(message, **common)


def decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DecodeError(
            "response body is not JSON", cause=exc, body_excerpt=excerpt(body)
        ) from exc


def _require_mapping(body: Any) -> Mapping[str, Any]:
    if not isinstance(body, Mapping):
        raise DecodeError("expected a JSON object, got %s" % type(body).__name__)
    return body


def decode_page(raw: RawResult) -> PeerPage:
    body = _require_mapping(raw.body)
    peers = body.get("peers")
    if not isinstance(peers, list):
        raise DecodeError("peers is missing or not an array")
    envelope = Envelope.from_json(body, skip=("peers", "count", "total", "next_cursor"))
    return PeerPage(
        envelope=envelope,
        peers=[Peer.from_json(p) for p in peers],
        count=int(body.get("count", len(peers))),
        total=body.get("total"),
        next_cursor=body.get("next_cursor"),
        raw=body,
        **raw.meta(),
    )


def decode_peer_detail(raw: RawResult) -> PeerDetail:
    body = _require_mapping(raw.body)
    peer = body.get("peer")
    if not isinstance(peer, Mapping):
        raise DecodeError("peer is missing or not an object")
    return PeerDetail(
        envelope=Envelope.from_json(body, skip=("peer",)),
        peer=Peer.from_json(peer),
        raw=body,
        **raw.meta(),
    )


def decode_facets(raw: RawResult) -> Facets:
    body = _require_mapping(raw.body)
    facets_body = body.get("facets")
    if not isinstance(facets_body, Mapping):
        raise DecodeError("facets is missing or not an object")
    facets: Dict[str, Facet] = {}
    for name, facet in facets_body.items():
        values: List[FacetValue] = []
        for item in (facet or {}).get("values") or []:
            values.append(FacetValue(value=str(item.get("value")), count=int(item.get("count", 0))))
        facets[str(name)] = Facet(available=bool((facet or {}).get("available")), values=values)
    geo_coverage = body.get("geo_coverage")
    return Facets(
        envelope=Envelope.from_json(body, skip=("facets", "total", "geo_coverage")),
        total=body.get("total"),
        facets=facets,
        geo_coverage=geo_coverage if isinstance(geo_coverage, Mapping) else None,
        raw=body,
        **raw.meta(),
    )


def decode_gossip_config(raw: RawResult) -> GossipConfigResult:
    """A ``format=gossip_config`` body -> a :class:`GossipConfig` plus metadata.

    The body is the server's rendering of an ``override_gossip_config.json``;
    it is handed to ``GossipConfig`` exactly as ``GossipConfig.parse`` would
    (unknown keys preserved, nothing validated — ``save`` validates).
    """
    from .gossip import GossipConfig

    body = raw.body
    if not isinstance(body, Mapping):
        raise DecodeError(
            "format=gossip_config: expected a JSON object, got %s" % type(body).__name__
        )
    config = GossipConfig.parse(json.dumps(body, ensure_ascii=False))
    return GossipConfigResult(config=config, raw=body, **raw.meta())


def decode_network_map_graph(raw: RawResult) -> NetworkMapGraph:
    body = _require_mapping(raw.body)
    return NetworkMapGraph.from_json(body, raw=body, **raw.meta())


def decode_network_map_summary(raw: RawResult) -> NetworkMapSummary:
    body = _require_mapping(raw.body)
    return NetworkMapSummary.from_json(body, raw=body, **raw.meta())


#: ``connectable`` on the network map is the same closed enum as on ``/peers``.
_MAP_CONNECTABLE = ("yes", "no", "unknown")

MapCsv = Union[str, Sequence[str], None]
#: ``serves_mempool`` / ``auction`` on the map: like the ``/peers`` builder — a
#: pre-joined CSV string, or a list whose ``None`` member is the literal ``null``
#: (``True``/``False`` spell ``true``/``false``). ``None`` itself means unset.
MapNullableCsv = Union[str, Sequence[Any], None]


def _map_csv(value: MapCsv) -> Optional[List[str]]:
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else [str(v) for v in value]
    items = [i for i in items if i != ""]
    return items or None


def network_map_query_string(
    *,
    max_tier: Optional[int] = None,
    show_unreached: Optional[bool] = None,
    hide_unreachable: Optional[bool] = None,
    connectable: MapCsv = None,
    cc: MapCsv = None,
    operator: Optional[str] = None,
    label: MapCsv = None,
    serves_mempool: MapNullableCsv = None,
    auction: MapNullableCsv = None,
    network: Optional[str] = None,
) -> str:
    """The canonical query string for ``/network-map/*`` (CLIENT_SPEC §4.8).

    Unset is omitted; names are lexicographic; values are RFC 3986 encoded with
    the CSV ``,`` as ``%2C`` — exactly as §2.3 serializes ``/peers``. The string
    filters ``cc``, ``operator`` and ``label`` are sent exactly as given (no
    upper-casing); a list is joined with ``,``.

    >>> network_map_query_string(max_tier=2, connectable=["yes", "no"])
    'connectable=yes%2Cno&max_tier=2'
    >>> network_map_query_string(serves_mempool=[True, None], auction="holds_slot")
    'auction=holds_slot&serves_mempool=true%2Cnull'
    >>> network_map_query_string(network="testnet")
    'network=testnet'
    """
    params: Dict[str, str] = {}
    if max_tier is not None:
        if isinstance(max_tier, bool) or not isinstance(max_tier, int) or max_tier < 0:
            raise Invalid("max_tier", "must be an integer >= 0, got %r" % (max_tier,))
        params["max_tier"] = str(max_tier)
    for name, flag in (("show_unreached", show_unreached), ("hide_unreachable", hide_unreachable)):
        if flag is not None:
            params[name] = "true" if flag else "false"
    buckets = _map_csv(connectable)
    if buckets is not None:
        for bucket in buckets:
            if bucket not in _MAP_CONNECTABLE:
                raise Invalid(
                    "connectable", "%r is not one of %s" % (bucket, ", ".join(_MAP_CONNECTABLE))
                )
        params["connectable"] = ",".join(buckets)
    countries = _map_csv(cc)
    if countries is not None:
        # Sent exactly as given — no upper-casing on /network-map/* (unlike /peers).
        params["cc"] = ",".join(countries)
    if operator is not None and operator != "":
        params["operator"] = operator
    labels = _map_csv(label)
    if labels is not None:
        params["label"] = ",".join(labels)
    # 1.1.0: the same closed tri-state / auction enums as the /peers builder.
    for name, value in (("serves_mempool", serves_mempool), ("auction", auction)):
        if value is None:
            continue
        tokens = _nullable_tokens([value])
        allowed = CLOSED_ENUMS[name]
        for token in tokens:
            if token not in allowed:
                raise Invalid(name, "%r is not one of %s" % (token, ", ".join(allowed)))
        if tokens:
            params[name] = ",".join(tokens)
    # Unreleased (testnet beta, §10 D-U): the same closed enum as the builder.
    if check_network(network) is not None:
        params["network"] = str(network)
    return "&".join(
        "%s=%s" % (quote(k, safe=""), quote(v, safe="")) for k, v in sorted(params.items())
    )


def request_headers(
    user_agent: str,
    api_key: Optional[str],
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": user_agent}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    if extra:
        headers.update({k: v for k, v in extra.items() if v is not None})
    return headers


def redact_key(api_key: Optional[str]) -> str:
    """A key is never logged, never put in a query string, never in an error."""
    if not api_key:
        return "<none>"
    return api_key[:9] + "…" if len(api_key) > 9 else "…"


def split_cache_control(headers: Mapping[str, str]) -> Tuple[Optional[int], str]:
    raw = headers.get("cache-control", "")
    max_age: Optional[int] = None
    for part in raw.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                max_age = int(part.split("=", 1)[1])
            except ValueError:
                max_age = None
    return max_age, raw
