"""Decoded response types.

Every type here is a frozen dataclass and every one of them keeps the whole
answer: nothing the server sent is hidden from the caller, and any key added by
a later server release survives in ``extra``.

🔴 ``connectable`` is three-state and is never folded. ``unknown`` means *we
never observed this*; it is not ``no``, and not ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
)

if TYPE_CHECKING:  # pragma: no cover - the HTTP layer never imports gossip at runtime here
    from .gossip import GossipConfig, SaveResult

__all__ = [
    "CityCluster",
    "Envelope",
    "Facet",
    "FacetValue",
    "Facets",
    "GossipConfigResult",
    "NetworkMapEdge",
    "NetworkMapGraph",
    "NetworkMapSummary",
    "NetworkMapTier",
    "NotModified",
    "OperatorNodes",
    "Peer",
    "PeerAuction",
    "PeerDetail",
    "PeerMempool",
    "PeerPage",
    "RateLimit",
    "compute_reset_at",
    "parse_retry_after",
    "utcnow",
]


def utcnow() -> datetime:
    """The library's default clock. Injectable everywhere it matters."""
    return datetime.now(timezone.utc)


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    """CLIENT_SPEC §5.2: digits are a delay in seconds; anything else is parsed
    as an RFC 9110 IMF-fixdate and converted with ``max(0, date - now)`` against
    the **injectable** clock; a value that parses as neither is **absent**.

    A date in the past yields ``0`` — retry immediately — which is not the same
    answer as ``None``.

    >>> from datetime import datetime, timezone
    >>> parse_retry_after("42")
    42
    >>> parse_retry_after("Mon, 21 Sep 2026 02:36:00 GMT",
    ...                   datetime(2026, 9, 21, 2, 35, tzinfo=timezone.utc))
    60
    >>> parse_retry_after("soon-ish") is None
    True
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lstrip("+").isdigit():
        return int(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # pragma: no cover - py<3.10 returns None on failure
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0, round((when - reference).total_seconds()))


def compute_reset_at(
    now: datetime, retry_after: Optional[int], reset: Optional[int]
) -> Optional[datetime]:
    """CLIENT_SPEC §5.1: ``now + retry_after``, else ``now + reset``, else ``None``.

    >>> from datetime import datetime, timezone
    >>> compute_reset_at(datetime(2026, 10, 1, tzinfo=timezone.utc), 42, 7).isoformat()
    '2026-10-01T00:00:42+00:00'
    >>> compute_reset_at(datetime(2026, 10, 1, tzinfo=timezone.utc), None, None) is None
    True
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    seconds = retry_after if retry_after is not None else reset
    if seconds is None:
        return None
    return now + timedelta(seconds=seconds)


def _as_int(value: Any) -> Optional[int]:
    """Parse a header value as an integer. Anything else is ``None``, never 0."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RateLimit:
    """The ``RateLimit-*`` block, parsed from **every** response — 200, 304 and
    every error alike.

    Each member is nullable: a missing header is ``None``, never ``0``. The
    library hardcodes no quota numbers (CLIENT_SPEC D-A); all limit knowledge
    comes from these headers at runtime.
    """

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset: Optional[int] = None  # seconds from now, NOT an epoch
    policy: Optional[str] = None  # raw, unparsed
    retry_after: Optional[int] = None  # seconds, already resolved from an HTTP-date
    retry_after_raw: Optional[str] = None  # the header as sent, for diagnostics

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, str],
        clock: Optional[Callable[[], datetime]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> RateLimit:
        get = headers.get
        raw = get("retry-after")
        return cls(
            limit=_as_int(get("ratelimit-limit")),
            remaining=_as_int(get("ratelimit-remaining")),
            reset=_as_int(get("ratelimit-reset")),
            policy=get("ratelimit-policy"),
            retry_after=parse_retry_after(raw, now if now is not None else (clock or utcnow)()),
            retry_after_raw=raw,
        )


@dataclass(frozen=True)
class Envelope:
    """The standing fields on every successful body.

    🔴 ``available is False`` means *the producer could not build a graph*. The
    empty ``peers`` list that comes with it is **not** a finding about the
    network. Check ``available`` **before** ``stale``: a dark subsystem reports
    both as ``False``.

    ``attribution`` is the licence attribution for the geo/ASN data in the
    response (for example the DB-IP credit); show it wherever you show that data.

    ``network`` is ``"testnet"`` on a ``network=testnet`` (beta) body and
    ``None`` otherwise, which means mainnet. An open string, decoded as-is.
    """

    version: Optional[str] = None
    available: bool = True
    stale: bool = False
    chain: Optional[str] = None
    generated_at: Optional[str] = None
    next_sweep_at: Optional[str] = None
    attribution: Optional[str] = None
    network: Optional[str] = None  # "testnet" on a testnet body; None means mainnet
    extra: Mapping[str, Any] = field(default_factory=dict)

    _KNOWN = (
        "version",
        "available",
        "stale",
        "chain",
        "generated_at",
        "next_sweep_at",
        "attribution",
        "network",
    )

    @classmethod
    def from_json(cls, body: Mapping[str, Any], *, skip: Sequence[str] = ()) -> Envelope:
        ignored = set(cls._KNOWN) | set(skip)
        return cls(
            version=body.get("version"),
            available=bool(body.get("available", True)),
            stale=bool(body.get("stale", False)),
            chain=body.get("chain"),
            generated_at=body.get("generated_at"),
            next_sweep_at=body.get("next_sweep_at"),
            attribution=body.get("attribution"),
            network=body.get("network"),
            extra={k: v for k, v in body.items() if k not in ignored},
        )


def _sub_object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class PeerMempool:
    """Does this peer serve the mempool?

    Every field is nullable and an all-``None`` instance means **unknown**.

    🔴 ``serves is None`` means *unknown*. It is **not** ``False``: unknown is
    not "does not serve", and the library never folds one into the other.
    ``source`` is ``"probe"`` (observed by our network probing) or ``None``;
    an open set, decoded as-is. ``verdict`` is an open
    machine token (e.g. ``"serves"``, or ``"served_previously"``: a serve was
    confirmed, but not within the last 48 hours): render an unknown one as
    plain text. ``serves is True`` means a serve confirmed within the last 48
    hours. ``last_confirmed`` is when a serve was last confirmed;
    ``last_attempted`` when the peer's mempool was last checked (any outcome);
    ``last_checked`` is kept for compatibility and equals ``last_confirmed``
    when there is a confirmation.
    ``first_frame_ms`` is the time to the first mempool message.
    Timestamps are ISO-8601 strings, like every other timestamp on
    :class:`Peer`.
    """

    serves: Optional[bool] = None
    last_checked: Optional[str] = None
    verdict: Optional[str] = None  # open set, e.g. "serves"
    first_frame_ms: Optional[int] = None
    source: Optional[str] = None  # open: "probe"
    last_confirmed: Optional[str] = None
    last_attempted: Optional[str] = None

    @classmethod
    def from_json(cls, body: Any) -> PeerMempool:
        """A missing key or a non-object decodes to the all-``None`` instance.
        Unknown keys inside the object are ignored."""
        data = _sub_object(body)
        return cls(
            serves=data.get("serves"),
            last_checked=data.get("last_checked"),
            verdict=data.get("verdict"),
            first_frame_ms=data.get("first_frame_ms"),
            source=data.get("source"),
            last_confirmed=data.get("last_confirmed"),
            last_attempted=data.get("last_attempted"),
        )

    @property
    def is_unknown(self) -> bool:
        """``True`` when nothing is known about this peer's mempool."""
        return self.serves is None


@dataclass(frozen=True)
class PeerAuction:
    """Standing in the on-chain gossip read-priority auction (public chain data).

    ``status`` is ``"holds_slot"``, ``"bidding"`` or ``"bid_before"`` (an open
    set, decoded as-is); ``None`` means *never seen in the auction*. ``slot``
    and ``since`` are set only for ``holds_slot``. Timestamps are ISO-8601
    strings.
    """

    status: Optional[str] = None
    slot: Optional[int] = None
    since: Optional[str] = None
    last_bid_at: Optional[str] = None

    @classmethod
    def from_json(cls, body: Any) -> PeerAuction:
        """A missing key or a non-object decodes to the all-``None`` instance.
        Unknown keys inside the object are ignored."""
        data = _sub_object(body)
        return cls(
            status=data.get("status"),
            slot=data.get("slot"),
            since=data.get("since"),
            last_bid_at=data.get("last_bid_at"),
        )


@dataclass(frozen=True)
class Peer:
    """One peer as published.

    ``tier`` is the distance from the network's sentry nodes in the peer graph
    (tier 0 = sentries). ``tier is None`` means **this node was not placed**. It
    is *not* tier 0, and the library never substitutes 0, -1 or any other
    sentinel.

    ``null`` on the wire is always ``None`` here — "not provided", never zero,
    never false. ``state``, ``role``, ``labels``, ``cc_confidence``,
    ``cc_source`` and ``org`` are **open** sets, decoded as-is. ``mempool`` and
    ``auction`` are always present: a peer from a server that omits them
    decodes to the all-``None`` (unknown) instances. ``state`` ``listening``
    = the peer accepts inbound connections, other details unknown.
    ``city`` is the id of the peer's city cluster where the producer
    serves city positions, else ``None`` — never a coordinate. Any key a later
    server adds lands in ``extra``.

    ``version`` is the node's reported **hardfork version** on
    ``network="testnet"`` (beta), an opaque string such as ``"1550"``, or
    ``None`` when the node did not report one. On mainnet it is always ``None``
    (not published).
    """

    id: str
    tier: Optional[int] = None
    # open: responsive | open | full | syncing | listening | unreachable | unknown
    state: Optional[str] = None
    role: Optional[str] = None  # open set
    cc: Optional[str] = None
    cc_confidence: Optional[str] = None  # open: high | medium | low
    cc_source: Optional[str] = None  # open: cloud-range | db
    region: Optional[str] = None  # only for cloud-hosted peers (provider:region)
    asn: Optional[str] = None
    org: Optional[str] = None
    city: Optional[str] = None  # city-cluster id; None unless city positions are served
    operator: Optional[str] = None
    name: Optional[str] = None
    in_deg: Optional[int] = None
    out_deg: Optional[int] = None
    feeders: List[str] = field(default_factory=list)
    latest_height: Optional[int] = None
    blocks_behind: Optional[int] = None
    live: Optional[bool] = None
    connectable: Optional[str] = None  # "yes" | "no" | "unknown" — strings, not bools
    labels: List[str] = field(default_factory=list)  # open set
    last_seen: Optional[str] = None
    uptime: Optional[float] = None
    version: Optional[str] = None  # testnet: hardfork version (opaque str); mainnet: None
    mempool: PeerMempool = field(default_factory=PeerMempool)
    auction: PeerAuction = field(default_factory=PeerAuction)
    extra: Mapping[str, Any] = field(default_factory=dict)

    _KNOWN = (
        "id",
        "tier",
        "state",
        "role",
        "cc",
        "cc_confidence",
        "cc_source",
        "region",
        "asn",
        "org",
        "city",
        "operator",
        "name",
        "in_deg",
        "out_deg",
        "feeders",
        "latest_height",
        "blocks_behind",
        "live",
        "connectable",
        "labels",
        "last_seen",
        "uptime",
        "version",
        "mempool",
        "auction",
    )

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> Peer:
        return cls(
            id=str(body.get("id", "")),
            tier=body.get("tier"),
            state=body.get("state"),
            role=body.get("role"),
            cc=body.get("cc"),
            cc_confidence=body.get("cc_confidence"),
            cc_source=body.get("cc_source"),
            region=body.get("region"),
            asn=body.get("asn"),
            org=body.get("org"),
            city=body.get("city"),
            operator=body.get("operator"),
            name=body.get("name"),
            in_deg=body.get("in_deg"),
            out_deg=body.get("out_deg"),
            feeders=list(body.get("feeders") or []),
            latest_height=body.get("latest_height"),
            blocks_behind=body.get("blocks_behind"),
            live=body.get("live"),
            connectable=body.get("connectable"),
            labels=list(body.get("labels") or []),
            last_seen=body.get("last_seen"),
            uptime=body.get("uptime"),
            version=body.get("version"),
            mempool=PeerMempool.from_json(body.get("mempool")),
            auction=PeerAuction.from_json(body.get("auction")),
            extra={k: v for k, v in body.items() if k not in cls._KNOWN},
        )

    @property
    def is_connectable(self) -> bool:
        """``True`` only for ``connectable == "yes"``, excluding ``unknown``.

        🪤 ``connectable`` is an **estimate** of whether the peer has room for
        another inbound peer, not a guarantee; it can be wrong in both
        directions.
        """
        return self.connectable == "yes"


@dataclass(frozen=True)
class PeerPage:
    """One page of ``GET /peers``, with everything the response carried."""

    envelope: Envelope
    peers: List[Peer]
    count: int
    total: Optional[int]
    next_cursor: Optional[str]
    rate_limit: RateLimit = field(default_factory=RateLimit)
    etag: Optional[str] = None
    from_cache: bool = False
    status: int = 200
    api_version: Optional[str] = None  # the API-Version response header
    raw: Mapping[str, Any] = field(default_factory=dict)

    # -- envelope passthrough, so a caller never has to reach for it ----------
    @property
    def available(self) -> bool:
        return self.envelope.available

    @property
    def stale(self) -> bool:
        return self.envelope.stale

    @property
    def chain(self) -> Optional[str]:
        return self.envelope.chain

    @property
    def generated_at(self) -> Optional[str]:
        return self.envelope.generated_at

    @property
    def next_sweep_at(self) -> Optional[str]:
        return self.envelope.next_sweep_at

    @property
    def attribution(self) -> Optional[str]:
        return self.envelope.attribution

    @property
    def network(self) -> Optional[str]:
        """``"testnet"`` on a testnet body, else ``None`` (mainnet)."""
        return self.envelope.network

    def __iter__(self) -> Iterator[Peer]:
        return iter(self.peers)

    def __len__(self) -> int:
        return len(self.peers)

    def ips(self) -> List[str]:
        return [p.id for p in self.peers]


@dataclass(frozen=True)
class PeerDetail:
    """``GET /peers/{ip}`` — one peer's public dossier."""

    envelope: Envelope
    peer: Peer
    rate_limit: RateLimit = field(default_factory=RateLimit)
    etag: Optional[str] = None
    from_cache: bool = False
    status: int = 200
    api_version: Optional[str] = None  # the API-Version response header
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.envelope.available

    @property
    def network(self) -> Optional[str]:
        """``"testnet"`` on a testnet body, else ``None`` (mainnet)."""
        return self.envelope.network


@dataclass(frozen=True)
class FacetValue:
    value: str  # always a string; ``"null"`` is the selectable absent-value member
    count: int


@dataclass(frozen=True)
class Facet:
    """One facet.

    🔴 ``available is False`` means the attribute is **not collected**. An empty
    ``values`` under ``available is True`` means it *is* collected and nothing
    matches. The two are different answers and the library keeps them apart.
    """

    available: bool
    values: List[FacetValue]

    def counts(self) -> Dict[str, int]:
        return {v.value: v.count for v in self.values}


@dataclass(frozen=True)
class Facets:
    """``GET /peers/facets``: counts per attribute over the filtered set.

    ``facets`` is an **open** map (``tier``, ``cc``, ``region``, ``asn``,
    ``role``, ``label``, ``state``, ``connectable``, ``version``, ``uptime``,
    ``serves_mempool``, ``auction`` today); any name the server adds is decoded.
    ``geo_coverage`` is kept loosely typed (a plain mapping) or ``None``.
    """

    envelope: Envelope
    total: Optional[int]
    facets: Dict[str, Facet]
    geo_coverage: Optional[Mapping[str, Any]] = None
    rate_limit: RateLimit = field(default_factory=RateLimit)
    etag: Optional[str] = None
    from_cache: bool = False
    status: int = 200
    api_version: Optional[str] = None  # the API-Version response header
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.envelope.available

    @property
    def network(self) -> Optional[str]:
        """``"testnet"`` on a testnet body, else ``None`` (mainnet)."""
        return self.envelope.network

    def facet_available(self, name: str) -> bool:
        """Pre-check a filter instead of discovering ``filter-unavailable`` on a 400.

        An unknown facet name answers ``False``.
        """
        facet = self.facets.get(name)
        return bool(facet and facet.available)

    def values(self, name: str) -> List[FacetValue]:
        facet = self.facets.get(name)
        return list(facet.values) if facet else []


@dataclass(frozen=True)
class NotModified:
    """The result of a **caller-supplied** ``If-None-Match`` that matched.

    The library's own cache turns a 304 into the stored body; this type is only
    returned when the caller asked the conditional question itself.
    """

    etag: Optional[str]
    rate_limit: RateLimit = field(default_factory=RateLimit)
    status: int = 304
    api_version: Optional[str] = None


@dataclass(frozen=True)
class GossipConfigResult:
    """The result of :meth:`~hl_peers.client.PeerApiClient.list_peers_as_gossip_config`.

    ``config`` is an ordinary :class:`~hl_peers.gossip.GossipConfig`,
    parsed from the server's ``format=gossip_config`` rendering. It is **not
    written anywhere**: write it with ``result.config.save(path)`` (or the
    :meth:`save` shortcut, which is the same call) — the one safe write path,
    which validates, renames atomically, keeps an existing file's mode and
    creates a new one ``0600``.

    🔴 The node reads ``override_gossip_config.json`` when it
    starts. Writing it does not steer a running node, and this library never
    restarts anything.
    """

    config: GossipConfig
    rate_limit: RateLimit = field(default_factory=RateLimit)
    etag: Optional[str] = None
    from_cache: bool = False
    status: int = 200
    api_version: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """The canonical rendering (CLIENT_SPEC §6.3) — not the server's key order."""
        return self.config.to_json()

    def save(self, path: str, **opts: Any) -> SaveResult:
        """Exactly ``self.config.save(path, **opts)``; there is no second writer."""
        return self.config.save(path, **opts)


@dataclass(frozen=True)
class NetworkMapTier:
    tier: int
    count: int


@dataclass(frozen=True)
class NetworkMapEdge:
    """One directed edge. ``from_`` carries the wire key ``from`` (a Python
    keyword). ``span`` is ``tier(to) - tier(from)``, or ``None`` if either end
    is unplaced."""

    from_: str
    to: str
    span: Optional[int] = None


@dataclass(frozen=True)
class OperatorNodes:
    """``count is None`` when the producer publishes its own nodes as ordinary
    rows."""

    count: Optional[int] = None


@dataclass(frozen=True)
class CityCluster:
    """One server-side city cluster. ``lat``/``lon`` are the city's own
    point, rounded to 0.1° — the only coordinates in the API. ``tiers`` maps a
    published tier (as a string) or ``"null"`` (unreached) to a peer count."""

    id: str
    name: Optional[str] = None
    cc: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    peers: Optional[int] = None
    tiers: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> CityCluster:
        tiers = body.get("tiers")
        return cls(
            id=str(body.get("id", "")),
            name=body.get("name"),
            cc=body.get("cc"),
            lat=body.get("lat"),
            lon=body.get("lon"),
            peers=body.get("peers"),
            tiers={
                str(k): int(v)
                for k, v in (tiers.items() if isinstance(tiers, Mapping) else [])
                if v is not None
            },
        )


def _cities(value: Any) -> Optional[List[CityCluster]]:
    """``None`` when the key is absent (no city positions served) — kept
    distinct from an empty list."""
    if not isinstance(value, list):
        return None
    return [CityCluster.from_json(item) for item in value if isinstance(item, Mapping)]


def _counts(value: Any) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): int(v) for k, v in value.items() if v is not None}


def _tiers(value: Any) -> List[NetworkMapTier]:
    return [
        NetworkMapTier(tier=int(item.get("tier", 0)), count=int(item.get("count", 0)))
        for item in (value or [])
        if isinstance(item, Mapping)
    ]


@dataclass(frozen=True)
class NetworkMapSummary:
    """``GET /network-map/summary`` — the graph envelope and bucket counts,
    without nodes or edges, plus zero-filled ``connectable``, ``serves_mempool``
    and ``auction`` counts (the last two empty when an older server omits them).

    🔴 ``available is False`` is returned as-is, never as an error: the producer
    could not build a graph, and the zero counts are not a finding.
    """

    version: Optional[str] = None
    available: bool = True
    stale: bool = False
    #: ``"testnet"`` on a ``network=testnet`` (beta) body, else ``None`` (mainnet).
    network: Optional[str] = None
    generated_at: Optional[str] = None
    operator_nodes: OperatorNodes = field(default_factory=OperatorNodes)
    node_count: Optional[int] = None
    edge_count: Optional[int] = None
    tier_count: Optional[int] = None
    unreached: Optional[int] = None
    tiers: List[NetworkMapTier] = field(default_factory=list)
    connectable: Dict[str, int] = field(default_factory=dict)
    serves_mempool: Dict[str, int] = field(default_factory=dict)
    auction: Dict[str, int] = field(default_factory=dict)
    #: Served on the summary although upstream's schema omits it (CLIENT_SPEC §10 D-S).
    cities: Optional[List[CityCluster]] = None
    attribution: Optional[str] = None
    rate_limit: RateLimit = field(default_factory=RateLimit)
    etag: Optional[str] = None
    from_cache: bool = False
    status: int = 200
    api_version: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, body: Mapping[str, Any], **meta: Any) -> NetworkMapSummary:
        return cls(
            connectable=_counts(body.get("connectable")),
            serves_mempool=_counts(body.get("serves_mempool")),
            auction=_counts(body.get("auction")),
            **_map_common(body),
            **meta,
        )


@dataclass(frozen=True)
class NetworkMapGraph:
    """``GET /network-map/graph`` — the whole published graph.

    ``nodes`` decode with the same :class:`Peer` decoder as a ``/peers`` row.
    🔴 ``available is False`` is returned as-is, never as an error.
    """

    version: Optional[str] = None
    available: bool = True
    stale: bool = False
    #: ``"testnet"`` on a ``network=testnet`` (beta) body, else ``None`` (mainnet).
    network: Optional[str] = None
    generated_at: Optional[str] = None
    operator_nodes: OperatorNodes = field(default_factory=OperatorNodes)
    node_count: Optional[int] = None
    edge_count: Optional[int] = None
    tier_count: Optional[int] = None
    unreached: Optional[int] = None
    truncated: bool = False
    tiers: List[NetworkMapTier] = field(default_factory=list)
    nodes: List[Peer] = field(default_factory=list)
    edges: List[NetworkMapEdge] = field(default_factory=list)
    #: City clusters, biggest first; ``None`` unless the producer serves city positions.
    cities: Optional[List[CityCluster]] = None
    #: Credits for every geo source in use; present with ``cities``.
    attribution: Optional[str] = None
    rate_limit: RateLimit = field(default_factory=RateLimit)
    etag: Optional[str] = None
    from_cache: bool = False
    status: int = 200
    api_version: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, body: Mapping[str, Any], **meta: Any) -> NetworkMapGraph:
        return cls(
            truncated=bool(body.get("truncated", False)),
            nodes=[Peer.from_json(n) for n in (body.get("nodes") or []) if isinstance(n, Mapping)],
            edges=[
                NetworkMapEdge(from_=str(e.get("from")), to=str(e.get("to")), span=e.get("span"))
                for e in (body.get("edges") or [])
                if isinstance(e, Mapping)
            ],
            **_map_common(body),
            **meta,
        )

    def ips(self) -> List[str]:
        return [n.id for n in self.nodes]


def _map_common(body: Mapping[str, Any]) -> Dict[str, Any]:
    operator_nodes = body.get("operator_nodes")
    return {
        "version": body.get("version"),
        "available": bool(body.get("available", True)),
        "stale": bool(body.get("stale", False)),
        "network": body.get("network"),
        "generated_at": body.get("generated_at"),
        "operator_nodes": OperatorNodes(
            count=operator_nodes.get("count") if isinstance(operator_nodes, Mapping) else None
        ),
        "node_count": body.get("node_count"),
        "edge_count": body.get("edge_count"),
        "tier_count": body.get("tier_count"),
        "unreached": body.get("unreached"),
        "tiers": _tiers(body.get("tiers")),
        "cities": _cities(body.get("cities")),
        "attribution": body.get("attribution"),
    }
