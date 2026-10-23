"""The typed, immutable query builder and its four presets.

Every setter returns a **new** query; nothing mutates in place.

Client-side validation is deliberately thin (CLIENT_SPEC §2.2): closed enums,
the duration pattern, the numeric bounds, and nothing else. ``cc``, ``region``,
``asn``, ``role``, ``label``, ``version`` and ``fields`` are **open** sets that
may grow additively within v1 — validating them here would turn a server 400
that names the problem into a client-side error that guesses at it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union, cast

from .errors import Invalid

try:  # pragma: no cover - typing only
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing_extensions import Literal

__all__ = [
    "FACET_REFUSED_PARAMS",
    "Network",
    "PeerQuery",
    "check_network",
    "mempool_servers",
    "not_full",
    "sentries",
    "tier1",
]

Chain = Literal["Mainnet", "Testnet"]
#: Which network's peer graph to read (CLIENT_SPEC §10 D-U, testnet beta).
#: Lower-case and case-sensitive, so it never blurs with the capitalised ``Chain``.
Network = Literal["mainnet", "testnet"]
Sort = Literal["tier", "blocks_behind", "out_deg", "last_seen", "random"]
Format = Literal["json", "gossip_config"]
Connectable = Literal["yes", "no", "unknown"]
NodeState = Literal["responsive", "open", "full", "syncing", "listening", "unreachable", "unknown"]
AuctionStatus = Literal["holds_slot", "bidding", "bid_before"]

#: The closed enums the spec defines with ``enum:``. Everything else is open.
CLOSED_ENUMS: Dict[str, Tuple[str, ...]] = {
    "chain": ("Mainnet", "Testnet"),
    "network": ("mainnet", "testnet"),
    "sort": ("tier", "blocks_behind", "out_deg", "last_seen", "random"),
    "format": ("json", "gossip_config"),
    "connectable": ("yes", "no", "unknown"),
    "state": ("responsive", "open", "full", "syncing", "listening", "unreachable", "unknown"),
    "serves_mempool": ("true", "false", "null"),
    "auction": ("holds_slot", "bidding", "bid_before", "null"),
}

#: ``facets()`` takes every *filter*; these are not filters, and the endpoint
#: would ignore them while the caller believed they applied (CLIENT_SPEC §4.4).
FACET_REFUSED_PARAMS: Tuple[str, ...] = (
    "cursor",
    "fields",
    "format",
    "limit",
    "sort",
    "try_new_peers",
    "split_client_blocks",
)

_SEEN_WITHIN = re.compile(r"^[0-9]+[mhd]$")

CsvValue = Union[str, Sequence[str], None]


def check_network(value: Optional[str]) -> Optional[Network]:
    """Validate a ``network`` value (closed, case-sensitive) and return it.

    ``None`` means unset: nothing is sent and the server's default (mainnet)
    governs. ``"Testnet"`` is refused: that spelling is the ``chain`` enum.
    """
    allowed = CLOSED_ENUMS["network"]
    if value is not None and value not in allowed:
        raise Invalid("network", "%r is not one of %s" % (value, ", ".join(allowed)))
    return cast("Optional[Network]", value)


def _flatten(values: Sequence[Any]) -> List[str]:
    """Accept ``("DE", "NL")``, ``(["DE", "NL"],)`` and ``("DE,NL",)`` alike."""
    out: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            out.extend(part for part in value.split(",") if part != "")
        elif isinstance(value, Iterable):
            out.extend(_flatten(list(value)))
        else:
            out.append(str(value))
    return out


def _number(value: float) -> str:
    """The canonical wire form of a number (CLIENT_SPEC §2.3).

    🪤 Shortest round-trip, with no trailing ``.0``: ``1`` and ``1.0`` are the
    same bound to the server but two different CDN cache keys, so all four
    clients spell an integral value without a fractional part.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def _tristate_token(value: Any) -> str:
    """``True``/``False``/``None`` (or their wire spellings) -> ``true``/``false``/``null``."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _nullable_tokens(values: Sequence[Any]) -> List[str]:
    """Flatten a nullable-member CSV argument: ``None`` is a selectable member
    (the literal ``null``), and a pre-joined string is split on ``,``."""
    out: List[str] = []
    for value in values:
        if isinstance(value, str):
            out.extend(part for part in value.split(",") if part != "")
        elif isinstance(value, (list, tuple)):
            out.extend(_nullable_tokens(list(value)))
        else:
            out.append(_tristate_token(value))
    return out


def _wire(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (list, tuple)):
        return ",".join("null" if v is None else _wire(v) for v in value)
    if isinstance(value, float):
        return _number(value)
    return str(value)


@dataclass(frozen=True)
class PeerQuery:
    """An immutable ``GET /peers`` query.

    >>> PeerQuery().cc("DE", "NL").accepting(True).to_query_string()
    'accepting=true&cc=DE%2CNL'

    🪤 ``tier`` is **exact** and ``max_tier`` is a **ceiling**; they are
    different questions and this library never maps one to the other.
    """

    params: Mapping[str, Any] = field(default_factory=dict)

    # -- internals ---------------------------------------------------------
    def _with(self, name: str, value: Any) -> PeerQuery:
        params = dict(self.params)
        if value is None or (isinstance(value, (list, tuple)) and not value):
            params.pop(name, None)
        else:
            params[name] = value
        return PeerQuery(params)

    def _csv(self, name: str, values: Sequence[Any], *, upper: bool = False) -> PeerQuery:
        if len(values) == 1 and values[0] is None:
            return self._with(name, None)
        items = _flatten(values)
        if upper:
            items = [i.upper() for i in items]
        return self._with(name, items)

    def _enum_csv(self, name: str, values: Sequence[Any]) -> PeerQuery:
        if len(values) == 1 and values[0] is None:
            return self._with(name, None)
        items = _flatten(values)
        allowed = CLOSED_ENUMS[name]
        for item in items:
            if item not in allowed:
                raise Invalid(
                    name,
                    "%r is not one of %s" % (item, ", ".join(allowed)),
                )
        return self._with(name, items)

    def _nullable_enum_csv(self, name: str, values: Sequence[Any]) -> PeerQuery:
        """A CSV over a closed enum whose ``null`` member is selectable, so a
        lone ``None`` is a *value* here, never "unset" (use :meth:`unset`)."""
        items = _nullable_tokens(values)
        allowed = CLOSED_ENUMS[name]
        for item in items:
            if item not in allowed:
                raise Invalid(name, "%r is not one of %s" % (item, ", ".join(allowed)))
        return self._with(name, items)

    def _enum(self, name: str, value: Optional[str]) -> PeerQuery:
        if value is not None and value not in CLOSED_ENUMS[name]:
            raise Invalid(name, "%r is not one of %s" % (value, ", ".join(CLOSED_ENUMS[name])))
        return self._with(name, value)

    def _non_negative(self, name: str, value: Optional[int]) -> PeerQuery:
        if value is not None and value < 0:
            raise Invalid(name, "must be >= 0, got %r" % (value,))
        return self._with(name, value)

    # -- setters, one per wire parameter -----------------------------------
    def chain(self, value: Optional[Chain]) -> PeerQuery:
        """🪤 Only ``Mainnet`` is served today; ``Testnet`` answers an explicit
        ``400 chain-not-served`` rather than silently Mainnet data."""
        return self._enum("chain", value)

    def network(self, value: Optional[Network]) -> PeerQuery:
        """Which network's peer graph to read: ``mainnet`` or ``testnet`` (beta).

        Unset sends no ``network`` key, so the request is byte-identical to a
        mainnet one and the server's default (mainnet) governs. When unset here,
        the client's own ``network=`` default applies. Closed and
        case-sensitive: ``"Testnet"`` raises :class:`~hl_peers.errors.Invalid`.

        🪤 Not the same question as :meth:`chain`: ``network`` picks the graph,
        ``chain`` filters on what the server states (a testnet body states
        ``chain: "Testnet"``).
        """
        return self._with("network", check_network(value))

    def cc(self, *values: CsvValue) -> PeerQuery:
        """ISO-3166-1 alpha-2 country codes. Upper-cased on the wire.

        🪤 ``cc`` is the **broad geo key**. ``region`` is published only for cloud-hosted peers.
        """
        return self._csv("cc", values, upper=True)

    def region(self, *values: CsvValue) -> PeerQuery:
        """``provider:region`` values such as ``aws:eu-central-1``.

        🪤 ``region`` is published **only for cloud-hosted peers** (the cloud
        provider's region), and answers ``400 filter-unavailable`` while no
        geo data is loaded — which this library raises as
        :class:`~hl_peers.errors.FilterUnavailable`, never swallowing it into
        an empty list. :meth:`cc` is the broad geo key."""
        return self._csv("region", values)

    def asn(self, *values: CsvValue) -> PeerQuery:
        """``AS…`` numbers. Answers ``400 filter-unavailable`` while the ASN
        data is not loaded."""
        return self._csv("asn", values)

    def tier(self, *values: Union[int, str, Sequence[Optional[int]], None]) -> PeerQuery:
        """**Exact** tier match. ``None`` is a selectable member meaning *this node
        was not placed* and serializes as the string ``null``.

        ``tier(None)`` alone therefore means "select unplaced nodes"; use
        ``unset("tier")`` to remove the parameter.
        """
        items: List[Optional[int]] = []
        for value in values:
            if isinstance(value, str):
                items.extend(None if p == "null" else int(p) for p in value.split(","))
            elif isinstance(value, (list, tuple)):
                items.extend(value)
            elif value is None or isinstance(value, int):
                items.append(value)
            else:  # pragma: no cover - defensive
                raise Invalid("tier", "%r is not an integer, null, or a list" % (value,))
        if not items:
            return self._with("tier", None)
        return PeerQuery(dict(self.params, tier=items))

    def max_tier(self, value: Optional[int]) -> PeerQuery:
        """Tier **ceiling**, inclusive. ``max_tier(1)`` also returns tier 0."""
        return self._non_negative("max_tier", value)

    def role(self, *values: CsvValue) -> PeerQuery:
        """🪤 A third-party hint, never truth. Prefer :meth:`label` for sentries."""
        return self._csv("role", values)

    def label(self, *values: CsvValue) -> PeerQuery:
        return self._csv("label", values)

    def connectable(self, *values: Union[Connectable, Sequence[str], None]) -> PeerQuery:
        return self._enum_csv("connectable", values)

    def accepting(self, value: Optional[bool] = True) -> PeerQuery:
        """Exactly ``connectable=yes``.

        🪤 An **estimate** of whether the peer has room for another inbound peer,
        not a guarantee; it can be wrong in both directions.
        """
        return self._with("accepting", value)

    def state(self, *values: Union[NodeState, Sequence[str], None]) -> PeerQuery:
        """``responsive``, ``open``, ``full``, ``syncing``, ``listening``,
        ``unreachable``, ``unknown`` — a closed enum on the filter, case-sensitive.

        ``listening``: the peer accepts inbound connections, other
        details unknown. ``full``: not accepting new inbound peers right now.
        """
        return self._enum_csv("state", values)

    def country(self, *values: CsvValue) -> PeerQuery:
        """ISO-3166-1 alpha-2 codes, upper-cased on the wire.

        An alias of :meth:`cc` server-side (both given means their union). It is
        a separate field so the URL says what you wrote.
        """
        return self._csv("country", values, upper=True)

    def serves_mempool(
        self, *values: Union[bool, str, Sequence[Optional[bool]], None]
    ) -> PeerQuery:
        """Filter on ``mempool.serves``: any of ``True``, ``False``, ``None``.

        🔴 ``None`` (the wire literal ``null``) selects peers whose ``serves`` is
        **unknown**, which is **not** ``False``. ``serves_mempool(True)``
        therefore excludes them: unknown is not "does not serve".

        Pre-joined strings (``"true,null"``) are accepted. Anything outside
        ``true``/``false``/``null`` is refused locally with ``Invalid``.
        """
        return self._nullable_enum_csv("serves_mempool", values)

    def auction(
        self, *values: Union[AuctionStatus, str, Sequence[Optional[str]], None]
    ) -> PeerQuery:
        """Filter on ``auction.status``: ``holds_slot``, ``bidding``,
        ``bid_before``, or ``None`` (never seen in the auction). Closed and
        case-sensitive."""
        return self._nullable_enum_csv("auction", values)

    def try_new_peers(self, value: Optional[bool] = True) -> PeerQuery:
        """The emitted file's ``try_new_peers``. Only meaningful with
        ``format=gossip_config`` — see
        :meth:`~hl_peers.client.PeerApiClient.list_peers_as_gossip_config`."""
        return self._with("try_new_peers", value)

    def split_client_blocks(self, value: Optional[bool] = True) -> PeerQuery:
        """Emit the node setting that requests the transaction stream
        (``"split_client_blocks": true``) in the ``format=gossip_config`` file.
        The server defaults it to ``True`` (and ``try_new_peers`` to ``False``)
        with ``serves_mempool=true``; an explicit value wins. When ``False`` the
        key is omitted from the file."""
        return self._with("split_client_blocks", value)

    def max_blocks_behind(self, value: Optional[int]) -> PeerQuery:
        return self._non_negative("max_blocks_behind", value)

    def min_uptime(self, value: Optional[float]) -> PeerQuery:
        """A fraction in ``[0, 1]``."""
        if value is not None and not (0.0 <= value <= 1.0):
            raise Invalid("min_uptime", "must be within [0, 1], got %r" % (value,))
        return self._with("min_uptime", value)

    def max_latency_ms(self, value: Optional[int]) -> PeerQuery:
        """Requires :meth:`vantage`. 🪤 Not published today."""
        return self._non_negative("max_latency_ms", value)

    def vantage(self, value: Optional[str]) -> PeerQuery:
        return self._with("vantage", value)

    def seen_within(self, value: Optional[str]) -> PeerQuery:
        """``5m``, ``1h``, ``24h``, ``7d``. Below ``10m`` selects on refresh timing."""
        if value is not None and not _SEEN_WITHIN.match(value):
            raise Invalid("seen_within", "%r does not match ^[0-9]+[mhd]$" % (value,))
        return self._with("seen_within", value)

    def version(self, *values: CsvValue) -> PeerQuery:
        """🪤 Published only on ``network=testnet`` (the hardfork version);
        always null on mainnet."""
        return self._csv("version", values)

    def sort(self, value: Optional[Sort]) -> PeerQuery:
        """No ``sort`` is sent unless you set one, so the server's default
        governs (D-J)."""
        return self._enum("sort", value)

    def limit(self, value: Optional[int]) -> PeerQuery:
        """Page size. Never clamped client-side: the server clamps by tier and
        clamping here would hide that ceiling from you."""
        if value is not None and value < 1:
            raise Invalid("limit", "must be >= 1, got %r" % (value,))
        return self._with("limit", value)

    def cursor(self, value: Optional[str]) -> PeerQuery:
        """🪤 A cursor is bound to the graph generation that issued it."""
        return self._with("cursor", value)

    def fields(self, *values: CsvValue) -> PeerQuery:
        return self._csv("fields", values)

    def format(self, value: Optional[Format]) -> PeerQuery:
        return self._enum("format", value)

    # -- generic helpers ---------------------------------------------------
    def set(self, **kwargs: Any) -> PeerQuery:
        """Set several parameters at once, by wire name, with the same checks."""
        query = self
        for name, value in kwargs.items():
            setter = getattr(query, name, None)
            if setter is None or not callable(setter):
                raise Invalid(name, "unknown query parameter")
            query = setter(value) if not isinstance(value, (list, tuple)) else setter(*value)
        return query

    def unset(self, *names: str) -> PeerQuery:
        params = dict(self.params)
        for name in names:
            params.pop(name, None)
        return PeerQuery(params)

    # -- serialization -----------------------------------------------------
    def to_params(self) -> Dict[str, str]:
        """Wire parameters, validated. Unset and empty-list values are omitted."""
        if "max_latency_ms" in self.params and not self.params.get("vantage"):
            raise Invalid("vantage", "vantage is required when max_latency_ms is set")
        return {name: _wire(value) for name, value in sorted(self.params.items())}

    def to_query_string(self) -> str:
        """Canonical query string: lexicographic by name, RFC 3986 encoded,
        the CSV ``,`` encoded as ``%2C`` — so two clients issuing the same query
        produce the same URL and the same CDN cache key."""
        from urllib.parse import quote

        return "&".join(
            "%s=%s" % (quote(k, safe=""), quote(v, safe="")) for k, v in self.to_params().items()
        )

    def __bool__(self) -> bool:
        return bool(self.params)


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------
def tier1() -> PeerQuery:
    """Tier 1: one hop from the sentry identity set.

    🪤 **Exact**, not a ceiling — ``max_tier(1)`` would also return tier 0.
    """
    return PeerQuery().tier(1)


def sentries() -> PeerQuery:
    """The sentry identity set, by ``label=sentry``.

    🪤 Deliberately **not** ``tier=0``. They are different questions and may
    give different answers.
    """
    return PeerQuery().label("sentry")


def mempool_servers() -> PeerQuery:
    """Peers confirmed to serve the mempool (``serves_mempool=true``).

    "Serves the mempool AND in country X" is
    ``mempool_servers().cc("DE")``.

    🔴 Peers with ``mempool.serves: null`` — **unknown** — are **excluded**.
    Unknown is not "does not serve": to include them, use
    ``PeerQuery().serves_mempool(True, None)``.

    >>> mempool_servers().cc("DE").to_query_string()
    'cc=DE&serves_mempool=true'
    """
    return PeerQuery().serves_mempool(True)


def not_full(*, cc: CsvValue = None, region: CsvValue = None) -> PeerQuery:
    """Peers with a free inbound gossip slot (``accepting=true``).

    🪤 **This is an estimate of whether the peer has room for another inbound
    peer, not a guarantee; it can be wrong in both directions.**

    The geo argument is tagged — pass exactly one of ``cc=`` or ``region=``.
    🪤 ``region`` is published **only for cloud-hosted peers**
    (``provider:region``, the cloud provider's region) and answers ``400
    filter-unavailable`` while no geo data is loaded; that error is raised,
    never swallowed into an empty list. ``cc`` is the broad geo key.
    """
    if cc is None and region is None:
        raise Invalid("geo", "pass exactly one of cc= or region=")
    if cc is not None and region is not None:
        raise Invalid("geo", "pass exactly one of cc= or region=, not both")
    query = PeerQuery().accepting(True)
    return query.cc(cc) if cc is not None else query.region(region)
