"""The synchronous client.

Zero required dependencies: the default transport is stdlib ``urllib``. Install
the ``httpx`` extra only if you want the async client or an httpx-backed pool.

``base_url`` defaults to the public host, ``https://api.hypervisor.gg``, and
every route is a plain path at the host root (``/peers``, ``/network-map/…``).
Automatic retry is **off** by default (``max_retries=0``); opt in explicitly.
"""

from __future__ import annotations

import logging
import os
import random as _random
import time
from datetime import datetime
from typing import Any, Callable, Iterator, List, Mapping, Optional, Union

from ._http import (
    DEFAULT_BASE_PATH,
    DEFAULT_BASE_URL,
    FACETS_PATH,
    NETWORK_MAP_GRAPH_PATH,
    NETWORK_MAP_SUMMARY_PATH,
    PEERS_PATH,
    USER_AGENT,
    VERSION,  # noqa: F401 - re-exported for 0.x imports
    MapCsv,
    MapNullableCsv,
    RawResult,
    build_url,
    decode_facets,
    decode_gossip_config,
    decode_json,
    decode_network_map_graph,
    decode_network_map_summary,
    decode_page,
    decode_peer_detail,
    map_error,
    network_map_query_string,
    normalize_base_path,
    request_headers,
    retry_delay,
)
from .cache import CacheEntry, EtagCache
from .errors import HlPeersError, Invalid, RateLimited, Truncated
from .gossip import is_ipv4, looks_like_ipv6
from .models import (
    Facets,
    GossipConfigResult,
    NetworkMapGraph,
    NetworkMapSummary,
    NotModified,
    Peer,
    PeerDetail,
    PeerPage,
    RateLimit,
    utcnow,
)
from .query import FACET_REFUSED_PARAMS, Network, PeerQuery, check_network
from .transport import HttpResponse, Transport, UrllibTransport

__all__ = ["PeerApiClient", "PeerIterator"]

LOGGER = logging.getLogger("hl_peers")


class PeerApiClient:
    """A client for one deployment of the Hyperliquid Peer API.

    ``base_url`` resolves ``explicit argument -> HL_PEER_API_BASE_URL ->
    https://api.hypervisor.gg``; ``api_key`` resolves ``explicit argument ->
    HL_PEER_API_KEY -> None``. **Anonymous is a supported tier, not an error
    state.**

    🔴 ``max_retries`` defaults to **0**: a ``429`` raises
    :class:`~hl_peers.errors.RateLimited` (with ``reset_at``) and a ``5xx``
    or transport failure raises at once, so the library never sleeps behind
    your back. Pass e.g. ``max_retries=3`` to opt in to the CLIENT_SPEC §5.2
    policy (``Retry-After`` honoured up to ``max_retry_after``, exponential
    backoff for ``5xx`` and transport failures, never a retry of another 4xx).

    ``suppress_placeholder_warning`` is accepted and has no effect.

    The key is sent as ``Authorization: Bearer <key>`` and is never logged, put
    in a query string, or included in an error message.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        base_path: str = DEFAULT_BASE_PATH,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
        user_agent: Optional[str] = None,
        user_agent_suffix: Optional[str] = None,
        max_retries: int = 0,
        max_retry_after: float = 60.0,
        min_poll_interval: float = 60.0,
        cache: Optional[EtagCache] = None,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = _random.random,
        clock: Callable[[], datetime] = utcnow,
        suppress_placeholder_warning: bool = False,
        env: Optional[Mapping[str, str]] = None,
        network: Optional[Network] = None,
    ) -> None:
        environ = env if env is not None else os.environ
        self.base_url = base_url or environ.get("HL_PEER_API_BASE_URL") or DEFAULT_BASE_URL
        #: Route prefix, normalized per CLIENT_SPEC §1; ``""`` = host root.
        self.base_path = normalize_base_path(base_path)
        self.api_key = api_key if api_key is not None else environ.get("HL_PEER_API_KEY")
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after
        self.min_poll_interval = min_poll_interval
        self.cache: Optional[EtagCache] = EtagCache() if cache is None else cache
        self.transport: Transport = transport if transport is not None else UrllibTransport()
        self._sleep = sleep
        self._random = random
        #: The injectable clock. It resolves an HTTP-date ``Retry-After``
        #: (§5.2) and stamps a ``.bak`` file (§6.6), so a test never depends on
        #: wall-clock time.
        self.clock = clock
        #: The default ``network`` for every call that sets none (CLIENT_SPEC §1,
        #: §10 D-U). ``None`` sends nothing, so the server's default (mainnet)
        #: governs; a per-call or per-query value wins.
        self.network: Optional[Network] = check_network(network)
        del suppress_placeholder_warning  # has no effect
        base_agent = user_agent or USER_AGENT
        self.user_agent = base_agent + (" " + user_agent_suffix if user_agent_suffix else "")
        # A per-instance jitter for the polling helper, so two pollers started
        # together do not align on the same refresh.
        self._poll_jitter = 5.0 + self._random() * 10.0

    # -- public API --------------------------------------------------------
    def list_peers(
        self,
        query: Optional[PeerQuery] = None,
        *,
        if_none_match: Optional[str] = None,
    ) -> Union[PeerPage, NotModified]:
        """One page of ``GET <base_path>/peers``.

        🔴 An ``available: false`` envelope is returned as-is, never as an
        error: the empty ``peers`` list that comes with it is not a finding
        about the network.
        """
        query = self._with_network(query)
        _refuse_gossip_config_format(query)
        raw = self._get(self.base_path + PEERS_PATH, query, if_none_match=if_none_match)
        if isinstance(raw, NotModified):
            return raw
        return decode_page(raw)

    def iter_peers(
        self,
        query: Optional[PeerQuery] = None,
        *,
        max_items: int = 10_000,
        on_truncate: str = "stop",
    ) -> PeerIterator:
        """Walk the cursor to exhaustion, lazily.

        The whole directory is ~900 nodes, which is 18 anonymous pages at
        ``limit=50``.

        Stops on a null ``next_cursor``, an empty page, ``available == false``
        (without raising — inspect ``iterator.page.available``), or
        ``max_items``, which **defaults to 10 000** and is a hard guard.

        🪤 A cursor is bound to the graph generation that issued it. Paging
        across a data refresh raises :class:`~hl_peers.errors.CursorExpired`
        and the walk is **not** restarted for you — that would mix two
        generations into one result set, which is exactly what the server
        refused to do. Catch it and start over.
        """
        if on_truncate not in ("stop", "raise"):
            raise Invalid("on_truncate", "%r is not one of stop, raise" % (on_truncate,))
        return PeerIterator(self, query or PeerQuery(), max_items, on_truncate)

    def list_all_peers(
        self,
        query: Optional[PeerQuery] = None,
        *,
        max_items: int = 10_000,
        on_truncate: str = "stop",
    ) -> List[Peer]:
        """Eager convenience over :meth:`iter_peers`, same guard."""
        return list(self.iter_peers(query, max_items=max_items, on_truncate=on_truncate))

    def list_peers_as_gossip_config(
        self,
        query: Optional[PeerQuery] = None,
    ) -> GossipConfigResult:
        """The server's own ``override_gossip_config.json`` for ``query``.

        Sends ``query`` with ``format=gossip_config`` (a ``format`` you set is
        overridden; a ``try_new_peers`` you set is sent). The body is parsed
        into a :class:`~hl_peers.gossip.GossipConfig` and returned with the
        response metadata (``rate_limit``, ``etag``, ``from_cache``,
        ``api_version``, ``status``).

        It does **not** write. Write with ``result.config.save(path)`` — the one
        safe write path: validate, atomic rename, mode ``0600`` for a new file,
        optional ``.bak``. The server never emits an empty root list; it answers
        ``400 empty-root-list``, raised as
        :class:`~hl_peers.errors.BadRequest`.

        🔴 The node reads ``override_gossip_config.json`` when it
        starts: writing it does not steer a running node, and this library never
        restarts anything.
        """
        query = self._with_network(query).format("gossip_config")
        raw = self._get(self.base_path + PEERS_PATH, query)
        assert not isinstance(raw, NotModified)
        return decode_gossip_config(raw)

    def network_map_graph(
        self,
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
        network: Optional[Network] = None,
    ) -> NetworkMapGraph:
        """``GET <base_path>/network-map/graph`` — nodes, edges and the tier
        histogram. Nodes decode exactly like a ``/peers`` row.

        Unset options are omitted. ``connectable`` is the closed enum
        ``yes``/``no``/``unknown`` (checked locally); ``max_tier`` must be
        ``>= 0``. ``serves_mempool`` and ``auction`` take exactly what
        :meth:`PeerQuery.serves_mempool` / :meth:`PeerQuery.auction` take — a
        list (``None`` = the literal ``null``) or a pre-joined CSV — and are
        checked against the same closed enums. Where the producer serves city
        positions the result carries ``cities`` and ``attribution`` (else
        ``None``). 🔴 ``available: false`` is returned as-is, never as an error.
        """
        qs = network_map_query_string(
            max_tier=max_tier,
            show_unreached=show_unreached,
            hide_unreachable=hide_unreachable,
            connectable=connectable,
            cc=cc,
            operator=operator,
            label=label,
            serves_mempool=serves_mempool,
            auction=auction,
            network=self._network_for(network),
        )
        raw = self._get(self.base_path + NETWORK_MAP_GRAPH_PATH, qs)
        assert not isinstance(raw, NotModified)
        return decode_network_map_graph(raw)

    def network_map_summary(
        self,
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
        network: Optional[Network] = None,
    ) -> NetworkMapSummary:
        """``GET <base_path>/network-map/summary`` — the graph's counts without
        nodes or edges, plus ``connectable``, ``serves_mempool`` and ``auction``
        bucket counts (and ``cities`` / ``attribution`` when served). Same options as
        :meth:`network_map_graph`."""
        qs = network_map_query_string(
            max_tier=max_tier,
            show_unreached=show_unreached,
            hide_unreachable=hide_unreachable,
            connectable=connectable,
            cc=cc,
            operator=operator,
            label=label,
            serves_mempool=serves_mempool,
            auction=auction,
            network=self._network_for(network),
        )
        raw = self._get(self.base_path + NETWORK_MAP_SUMMARY_PATH, qs)
        assert not isinstance(raw, NotModified)
        return decode_network_map_summary(raw)

    def get_peer(self, ip: str, *, network: Optional[Network] = None) -> PeerDetail:
        """``GET <base_path>/peers/{ip}``.

        🔴 A malformed IPv4 is a ``400`` and a well-formed IPv4 in no source is
        a ``404``; the two never collapse into one error here. The shape is
        checked locally first so a typo does not spend a quota unit.
        """
        _require_query_ipv4(ip)
        from urllib.parse import quote

        raw = self._get(
            self.base_path + PEERS_PATH + "/" + quote(ip, safe=""),
            PeerQuery().network(self._network_for(network)),
        )
        assert not isinstance(raw, NotModified)
        return decode_peer_detail(raw)

    def facets(self, query: Optional[PeerQuery] = None) -> Facets:
        """``GET <base_path>/peers/facets`` — counts over the filtered set.

        Accepts every **filter** of the query builder. ``sort``, ``limit``,
        ``cursor``, ``fields``, ``format`` and ``try_new_peers`` are refused
        with :class:`~hl_peers.errors.Invalid`: the endpoint does not take
        them, and you would believe they applied.
        """
        query = self._with_network(query)
        _refuse_non_filters(query)
        raw = self._get(self.base_path + FACETS_PATH, query)
        assert not isinstance(raw, NotModified)
        return decode_facets(raw)

    def poll_peers(
        self,
        query: Optional[PeerQuery],
        interval: float,
        callback: Callable[[PeerPage], Any],
        *,
        max_polls: Optional[int] = None,
    ) -> None:
        """Poll, conditionally, aligned to the data's own clock.

        Refuses an ``interval`` below ``min_poll_interval`` (60 s): the
        data does not change that often, so faster polling returns identical
        bytes and spends quota. Uses the ETag cache, so an unchanged poll costs
        no quota unit, and prefers ``next_sweep_at`` (when the data is next
        refreshed) when the response carries it.
        """
        if interval < self.min_poll_interval:
            raise Invalid(
                "interval",
                "must be >= min_poll_interval (%.0fs): the data does not change that "
                "often, so a faster poll returns identical bytes and spends quota"
                % self.min_poll_interval,
            )
        polls = 0
        while max_polls is None or polls < max_polls:
            page = self.list_peers(query)
            assert isinstance(page, PeerPage)
            callback(page)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            self._sleep(self._next_poll_delay(page, interval))

    def _next_poll_delay(self, page: PeerPage, interval: float) -> float:
        return interval if page.next_sweep_at is None else interval + self._poll_jitter

    # -- network (testnet beta, CLIENT_SPEC §10 D-U) ------------------------
    def _network_for(self, network: Optional[str]) -> Optional[Network]:
        """A per-call ``network`` wins; else the client default; else unset."""
        checked = check_network(network)
        return checked if checked is not None else self.network

    def _with_network(self, query: Optional[PeerQuery]) -> PeerQuery:
        """The query, with the client's default ``network`` when it sets none."""
        query = query or PeerQuery()
        if "network" in query.params or self.network is None:
            return query
        return query.network(self.network)

    # -- request plumbing --------------------------------------------------
    def _get(
        self,
        path: str,
        query: Union[PeerQuery, str],
        *,
        if_none_match: Optional[str] = None,
    ) -> Union[RawResult, NotModified]:
        query_string = query if isinstance(query, str) else query.to_query_string()
        url = build_url(self.base_url, path, query_string)

        caller_conditional = if_none_match is not None
        cache_key = EtagCache.key("GET", path, query_string, bool(self.api_key))
        entry = None if caller_conditional or self.cache is None else self.cache.get(cache_key)
        conditional = if_none_match or (entry.etag if entry else None)
        headers = request_headers(
            self.user_agent,
            self.api_key,
            {"If-None-Match": conditional} if conditional else None,
        )

        response = self._send(url, headers)
        rate_limit = RateLimit.from_headers(response.headers, self.clock)
        etag = response.headers.get("etag")

        if response.status == 304:
            if caller_conditional or entry is None:
                # The caller asked the conditional question itself.
                return NotModified(
                    etag=etag or if_none_match,
                    rate_limit=rate_limit,
                    api_version=response.headers.get("api-version"),
                )
            return RawResult(
                status=304,
                body=entry.body,
                rate_limit=rate_limit,  # from the 304, never the stored ones
                etag=entry.etag,
                from_cache=True,
                headers=response.headers,
            )

        body = decode_json(response.body) if response.body else None
        if etag and self.cache is not None and not caller_conditional:
            self.cache.put(cache_key, CacheEntry(etag=etag, body=body, stored_at=time.time()))
        return RawResult(
            status=response.status,
            body=body,
            rate_limit=rate_limit,
            etag=etag,
            from_cache=False,
            headers=response.headers,
        )

    def _send(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        """Attempt, retry per CLIENT_SPEC §5.2, and map the final failure."""
        attempt = 0
        while True:
            error: Optional[HlPeersError] = None
            status: Optional[int] = None
            response: Optional[HttpResponse] = None
            try:
                response = self.transport.request("GET", url, headers, self.timeout)
            except HlPeersError as exc:  # TransportError
                error = exc
            if response is not None:
                if response.status in (200, 304):
                    return response
                status = response.status
                now = self.clock()
                error = map_error(
                    response.status,
                    response.headers,
                    response.body,
                    RateLimit.from_headers(response.headers, now=now),
                    now,
                )
            assert error is not None
            delay = retry_delay(status, error, attempt, self._random(), self.max_retry_after)
            if delay is None or attempt >= self.max_retries:
                if isinstance(error, RateLimited) and delay is None and error.retry_after:
                    LOGGER.debug(
                        "hl-peers: Retry-After %ss exceeds max_retry_after %ss; raising "
                        "rather than blocking",
                        error.retry_after,
                        self.max_retry_after,
                    )
                raise error
            self._sleep(delay)
            attempt += 1


class PeerIterator:
    """The handle :meth:`PeerApiClient.iter_peers` returns.

    Iterating yields :class:`~hl_peers.models.Peer`. Afterwards,
    ``truncated`` says whether ``max_items`` cut the walk short and ``page``
    holds the last page, so ``page.available`` distinguishes *the producer could
    not build a graph* from *nothing matched*.
    """

    def __init__(
        self,
        client: PeerApiClient,
        query: PeerQuery,
        max_items: int,
        on_truncate: str,
    ) -> None:
        self._client = client
        self._query = query
        self._max_items = max_items
        self._on_truncate = on_truncate
        self.truncated = False
        self.seen = 0
        self.pages = 0
        self.page: Optional[PeerPage] = None
        self._iterator = self._walk()

    def __iter__(self) -> PeerIterator:
        return self

    def __next__(self) -> Peer:
        return next(self._iterator)

    def _walk(self) -> Iterator[Peer]:
        query = self._query
        while True:
            page = self._client.list_peers(query)
            assert isinstance(page, PeerPage)
            self.page = page
            self.pages += 1
            if not page.available:
                # 🔴 Not an error, and not "no peers matched". Stop and let the
                # caller inspect the flag.
                return
            if not page.peers:
                return
            for peer in page.peers:
                if self.seen >= self._max_items:
                    self.truncated = True
                    if self._on_truncate == "raise":
                        raise Truncated(self.seen)
                    return
                self.seen += 1
                yield peer
            if self.seen >= self._max_items:
                self.truncated = True
                if self._on_truncate == "raise":
                    raise Truncated(self.seen)
                return
            if not page.next_cursor:
                return
            query = query.cursor(page.next_cursor)


def _refuse_gossip_config_format(query: PeerQuery) -> None:
    """🔴 CLIENT_SPEC §4.1: ``format=gossip_config`` has no ``peers`` array.

    Decoding it as a page would hand the caller an empty peer list that is not a
    finding about the network — the one mistake §3.1 exists to prevent — so the
    request is refused before it is sent rather than after.
    """
    if query.to_params().get("format") == "gossip_config":
        raise Invalid(
            "format",
            "format=gossip_config does not return a peer list, so list_peers cannot "
            "decode it; use list_peers_as_gossip_config(query) instead",
        )


def _refuse_non_filters(query: PeerQuery) -> None:
    """CLIENT_SPEC §4.4: ``facets`` takes every filter and nothing else."""
    for name in sorted(query.params):
        if name in FACET_REFUSED_PARAMS:
            raise Invalid(
                name,
                "facets() takes filters only; %s is not one, the endpoint ignores it and "
                "the counts would not be what you think" % name,
            )


def _require_query_ipv4(ip: str) -> None:
    if looks_like_ipv6(ip):
        raise Invalid(
            "ip",
            "the peer API is IPv4-only; %s is an IPv6 literal" % ip,
        )
    if not is_ipv4(ip):
        raise Invalid("ip", "%r is not an IPv4 address" % (ip,))
