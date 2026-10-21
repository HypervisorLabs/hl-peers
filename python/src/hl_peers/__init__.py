"""hl-peers — a client for the Hyperliquid Peer API and an offline editor for
hl-node's ``override_gossip_config.json``.

Two namespaces, always separate:

* :class:`PeerApiClient` — the HTTP query surface. Needs the network.
* :class:`GossipConfig` — pure, offline, deterministic file algebra. Needs
  nothing at all: no network, no clock, no environment lookups. It imports only
  the standard library, so it works on a box with no API access.

Only :func:`update_gossip_config` joins them.

🔴 **The node reads ``override_gossip_config.json`` when it
starts.** Writing this file does **not** steer a running node: the change takes
effect at the node's next start. This library will **never** restart, signal, or
otherwise disturb your node — that is the operator's decision, at the operator's
moment. Restarting a node *to apply a config* while it is behind can make a
lagging box worse.

The client talks to ``https://api.hypervisor.gg`` by default (override with
``base_url=`` or ``HL_PEER_API_BASE_URL``). Automatic retry is **off** by
default: pass ``max_retries=`` to opt in.
"""

from __future__ import annotations

from ._http import DEFAULT_BASE_PATH, DEFAULT_BASE_URL, normalize_base_path
from .cache import CacheEntry, EtagCache
from .client import PeerApiClient, PeerIterator
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
    TransportError,
    Truncated,
    Unauthorized,
    Unavailable,
)
from .gossip import (
    ConfigWarning,
    GossipConfig,
    SaveResult,
    default_config_path,
    render_config,
)
from .models import (
    CityCluster,
    Envelope,
    Facet,
    Facets,
    FacetValue,
    GossipConfigResult,
    NetworkMapEdge,
    NetworkMapGraph,
    NetworkMapSummary,
    NetworkMapTier,
    NotModified,
    OperatorNodes,
    Peer,
    PeerAuction,
    PeerDetail,
    PeerMempool,
    PeerPage,
    RateLimit,
)
from .query import PeerQuery, mempool_servers, not_full, sentries, tier1
from .transport import HttpResponse, Transport, UrllibTransport
from .update import UpdateResult, update_gossip_config

__version__ = "1.1.0"

#: Short aliases, for callers who prefer them.
Client = PeerApiClient
Query = PeerQuery
Page = PeerPage
PeerList = PeerPage

__all__ = [
    "DEFAULT_BASE_PATH",
    "DEFAULT_BASE_URL",
    "BadRequest",
    "CacheEntry",
    "ChainNotServed",
    "CityCluster",
    "Client",
    "ConfigWarning",
    "CursorExpired",
    "DecodeError",
    "Envelope",
    "EtagCache",
    "Facet",
    "FacetValue",
    "Facets",
    "FilterUnavailable",
    "GossipConfig",
    "GossipConfigResult",
    "HlPeersError",
    "HttpResponse",
    "Invalid",
    "NetworkMapEdge",
    "NetworkMapGraph",
    "NetworkMapSummary",
    "NetworkMapTier",
    "NotFound",
    "NotModified",
    "OperatorNodes",
    "Page",
    "Peer",
    "PeerApiClient",
    "PeerAuction",
    "PeerDetail",
    "PeerIterator",
    "PeerList",
    "PeerMempool",
    "PeerPage",
    "PeerQuery",
    "Query",
    "RateLimit",
    "RateLimited",
    "SaveResult",
    "ServerError",
    "Transport",
    "TransportError",
    "Truncated",
    "Unauthorized",
    "Unavailable",
    "UpdateResult",
    "UrllibTransport",
    "__version__",
    "default_config_path",
    "mempool_servers",
    "normalize_base_path",
    "not_full",
    "render_config",
    "sentries",
    "tier1",
    "update_gossip_config",
]


def __getattr__(name: str) -> object:
    """``AsyncPeerApiClient`` and ``HttpxTransport`` need the optional extra, so
    they are imported lazily and a bare system Python never pays for them."""
    if name == "AsyncPeerApiClient":
        from .aio import AsyncPeerApiClient

        return AsyncPeerApiClient
    if name == "HttpxTransport":
        from .httpx_transport import HttpxTransport

        return HttpxTransport
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
