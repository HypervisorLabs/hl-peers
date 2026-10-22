"""The asynchronous client — the optional ``[httpx]`` extra.

    pip install "hl-peers[httpx]"

Identical observable behaviour to :class:`~hl_peers.client.PeerApiClient`:
the same query serialization, the same error variants, the same retry decisions
(off by default) and the same ETag cache. Only the I/O is different.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random as _random
import time
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, List, Mapping, Optional, Union

from ._http import (
    DEFAULT_BASE_PATH,
    DEFAULT_BASE_URL,
    FACETS_PATH,
    NETWORK_MAP_GRAPH_PATH,
    NETWORK_MAP_SUMMARY_PATH,
    PEERS_PATH,
    USER_AGENT,
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
from .client import _refuse_gossip_config_format, _refuse_non_filters, _require_query_ipv4
from .errors import HlPeersError, Invalid, TransportError, Truncated
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
from .query import Network, PeerQuery, check_network
from .transport import HttpResponse, normalize_headers

__all__ = ["AsyncPeerApiClient", "HttpxAsyncTransport"]

LOGGER = logging.getLogger("hl_peers")


class HttpxAsyncTransport:
    """The default async transport: an ``httpx.AsyncClient``."""

    def __init__(self, client: Optional[Any] = None, **client_kwargs: Any) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "AsyncPeerApiClient needs the optional extra: pip install 'hl-peers[httpx]'"
            ) from exc
        self._httpx = httpx
        self._owned = client is None
        self._client = client if client is not None else httpx.AsyncClient(**client_kwargs)

    async def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        try:
            response = await self._client.request(
                method, url, headers=dict(headers), timeout=timeout
            )
        except self._httpx.HTTPError as exc:
            raise TransportError(
                "%s %s failed: %s" % (method, url.split("?", 1)[0], exc), cause=exc
            ) from exc
        return HttpResponse(
            status=int(response.status_code),
            headers=normalize_headers(response.headers.items()),
            body=response.content,
        )

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()


class AsyncPeerApiClient:
    """The async twin of :class:`~hl_peers.client.PeerApiClient`.

    Same constructor, same defaults: ``https://api.hypervisor.gg``, plain paths,
    ``max_retries=0`` (auto-retry off; opt in), ETag cache on.
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
        transport: Optional[Any] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
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
        self.transport: Any = transport if transport is not None else HttpxAsyncTransport()
        self._sleep = sleep or asyncio.sleep
        self._random = random
        self.clock = clock
        #: The default ``network`` for every call that sets none (CLIENT_SPEC §1,
        #: §10 D-U). ``None`` sends nothing, so the server's default (mainnet)
        #: governs; a per-call or per-query value wins.
        self.network: Optional[Network] = check_network(network)
        del suppress_placeholder_warning  # has no effect
        base_agent = user_agent or USER_AGENT
        self.user_agent = base_agent + (" " + user_agent_suffix if user_agent_suffix else "")

    async def __aenter__(self) -> AsyncPeerApiClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        closer = getattr(self.transport, "aclose", None)
        if closer is not None:
            await closer()

    # -- public API --------------------------------------------------------
    async def list_peers(
        self,
        query: Optional[PeerQuery] = None,
        *,
        if_none_match: Optional[str] = None,
    ) -> Union[PeerPage, NotModified]:
        query = self._with_network(query)
        _refuse_gossip_config_format(query)
        raw = await self._get(self.base_path + PEERS_PATH, query, if_none_match=if_none_match)
        if isinstance(raw, NotModified):
            return raw
        return decode_page(raw)

    async def list_peers_as_gossip_config(
        self,
        query: Optional[PeerQuery] = None,
    ) -> GossipConfigResult:
        """See :meth:`PeerApiClient.list_peers_as_gossip_config`. Does not write;
        write with ``result.config.save(path)``."""
        query = self._with_network(query).format("gossip_config")
        raw = await self._get(self.base_path + PEERS_PATH, query)
        assert not isinstance(raw, NotModified)
        return decode_gossip_config(raw)

    async def iter_peers(
        self,
        query: Optional[PeerQuery] = None,
        *,
        max_items: int = 10_000,
        on_truncate: str = "stop",
    ) -> AsyncIterator[Peer]:
        """Async generator over the cursor walk. Same guards as the sync one:
        stops on a null cursor, an empty page, ``available == false`` or
        ``max_items`` (default 10 000)."""
        if on_truncate not in ("stop", "raise"):
            raise Invalid("on_truncate", "%r is not one of stop, raise" % (on_truncate,))
        seen = 0
        current = query or PeerQuery()
        while True:
            page = await self.list_peers(current)
            assert isinstance(page, PeerPage)
            if not page.available or not page.peers:
                return
            for peer in page.peers:
                if seen >= max_items:
                    if on_truncate == "raise":
                        raise Truncated(seen)
                    return
                seen += 1
                yield peer
            if seen >= max_items:
                if on_truncate == "raise":
                    raise Truncated(seen)
                return
            if not page.next_cursor:
                return
            current = current.cursor(page.next_cursor)

    async def list_all_peers(
        self,
        query: Optional[PeerQuery] = None,
        *,
        max_items: int = 10_000,
    ) -> List[Peer]:
        return [peer async for peer in self.iter_peers(query, max_items=max_items)]

    async def get_peer(self, ip: str, *, network: Optional[Network] = None) -> PeerDetail:
        _require_query_ipv4(ip)
        from urllib.parse import quote

        raw = await self._get(
            self.base_path + PEERS_PATH + "/" + quote(ip, safe=""),
            PeerQuery().network(self._network_for(network)),
        )
        assert not isinstance(raw, NotModified)
        return decode_peer_detail(raw)

    async def facets(self, query: Optional[PeerQuery] = None) -> Facets:
        """Every filter is accepted; ``sort``/``limit``/``cursor``/``fields``/
        ``format``/``try_new_peers`` are refused with ``Invalid``."""
        query = self._with_network(query)
        _refuse_non_filters(query)
        raw = await self._get(self.base_path + FACETS_PATH, query)
        assert not isinstance(raw, NotModified)
        return decode_facets(raw)

    async def network_map_graph(
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
        """See :meth:`PeerApiClient.network_map_graph`."""
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
        raw = await self._get(self.base_path + NETWORK_MAP_GRAPH_PATH, qs)
        assert not isinstance(raw, NotModified)
        return decode_network_map_graph(raw)

    async def network_map_summary(
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
        """See :meth:`PeerApiClient.network_map_summary`."""
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
        raw = await self._get(self.base_path + NETWORK_MAP_SUMMARY_PATH, qs)
        assert not isinstance(raw, NotModified)
        return decode_network_map_summary(raw)

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

    # -- plumbing ----------------------------------------------------------
    async def _get(
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

        response = await self._send(url, headers)
        rate_limit = RateLimit.from_headers(response.headers, self.clock)
        etag = response.headers.get("etag")

        if response.status == 304:
            if caller_conditional or entry is None:
                return NotModified(
                    etag=etag or if_none_match,
                    rate_limit=rate_limit,
                    api_version=response.headers.get("api-version"),
                )
            return RawResult(304, entry.body, rate_limit, entry.etag, True, response.headers)

        body = decode_json(response.body) if response.body else None
        if etag and self.cache is not None and not caller_conditional:
            self.cache.put(cache_key, CacheEntry(etag=etag, body=body, stored_at=time.time()))
        return RawResult(response.status, body, rate_limit, etag, False, response.headers)

    async def _send(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        attempt = 0
        while True:
            error: Optional[HlPeersError] = None
            status: Optional[int] = None
            response: Optional[HttpResponse] = None
            try:
                response = await self.transport.request("GET", url, headers, self.timeout)
            except HlPeersError as exc:
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
                raise error
            await self._sleep(delay)
            attempt += 1
