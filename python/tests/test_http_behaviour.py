"""HTTP behaviour against ``fixtures/http``, replayed through an injected
transport. No socket is opened by this module."""

from __future__ import annotations

from typing import Any, List

import pytest

from conftest import FixtureTransport, Recorder, load_http_fixtures
from hl_peers import (
    BadRequest,
    ChainNotServed,
    CursorExpired,
    DecodeError,
    FilterUnavailable,
    Invalid,
    NotFound,
    NotModified,
    PeerApiClient,
    PeerQuery,
    RateLimited,
    ServerError,
    TransportError,
    Truncated,
    Unauthorized,
    Unavailable,
    not_full,
    sentries,
    tier1,
)
from hl_peers._http import (
    DEFAULT_BASE_PATH,
    DEFAULT_BASE_URL,
    normalize_base_path,
)
from hl_peers.models import PeerPage


def make_client(transport: FixtureTransport, recorder: Recorder, **kw: Any) -> PeerApiClient:
    return PeerApiClient(
        base_url="https://peers.test.invalid",
        transport=transport,
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
        **kw,
    )


# ---------------------------------------------------------------------------
# the query surface
# ---------------------------------------------------------------------------
def test_default_page_decodes_every_field(client: PeerApiClient) -> None:
    page = client.list_peers()
    assert isinstance(page, PeerPage)
    assert page.count == 5 and page.total == 5
    assert page.available is True and page.stale is False
    assert page.chain == "Mainnet"
    assert page.next_sweep_at == "2026-09-21T02:40:00Z"
    assert page.attribution and "IPinfo" in page.attribution
    first = page.peers[0]
    assert first.id == "192.0.2.10"
    assert first.labels == ["sentry"]
    assert first.region is None and first.version is None
    assert first.asn == "AS64500" and first.org == "Example Hosting"
    assert first.cc_confidence == "high" and first.cc_source == "db+rtt-confirmed"
    assert first.uptime == 0.97 and first.feeders == []
    assert first.state == "responsive"
    assert first.is_connectable is True
    assert first.extra == {}  # every published key is modelled
    assert page.rate_limit.limit == 20 and page.rate_limit.remaining == 17
    assert page.rate_limit.reset == 42
    assert page.rate_limit.policy.startswith('"anon-min"')
    assert page.rate_limit.retry_after is None  # a missing header is None, never 0
    assert page.etag and page.from_cache is False and page.status == 200


def test_three_state_fields_are_never_folded(client: PeerApiClient) -> None:
    page = client.list_peers()
    assert isinstance(page, PeerPage)
    sg = next(p for p in page.peers if p.id == "198.51.100.7")
    assert sg.mempool.serves is True and sg.mempool.source == "relay"
    assert sg.connectable == "yes"
    jp = next(p for p in page.peers if p.id == "198.51.100.20")
    assert jp.connectable == "no"  # a string, never a bool
    sentry = next(p for p in page.peers if p.id == "192.0.2.200")
    assert sentry.tier == 0
    assert "sentry:foundation" in sentry.labels  # an open set, decoded as-is


def test_owner_query_1_cc_de(transport: FixtureTransport, client: PeerApiClient) -> None:
    page = client.list_peers(PeerQuery().cc("DE"))
    assert isinstance(page, PeerPage)
    assert page.ips() == ["192.0.2.10", "192.0.2.11"]
    assert transport.requests[-1][1] == "https://peers.test.invalid/peers?cc=DE"


def test_base_path_default_is_the_one_constant(client: PeerApiClient) -> None:
    assert DEFAULT_BASE_PATH == ""  # plain paths at the host root (D-N)
    assert client.base_path == ""


def test_base_path_is_normalized() -> None:
    assert normalize_base_path("peer-api/v1/") == "/peer-api/v1"
    assert normalize_base_path("//peer-api/v1//") == "/peer-api/v1"
    assert normalize_base_path("/") == ""
    assert normalize_base_path("") == ""


def test_custom_base_path_reaches_every_route(recorder: Recorder) -> None:
    from urllib.parse import urlparse

    class Rewriting(FixtureTransport):
        def __init__(self) -> None:
            super().__init__()
            self.seen: List[str] = []

        def request(self, method: str, url: str, headers: Any, timeout: float) -> Any:
            self.seen.append(urlparse(url).path)
            return super().request(method, url.replace("/peer-api/v1/", "/"), headers, timeout)

    transport = Rewriting()
    client = PeerApiClient(
        base_url="https://peers.test.invalid",
        base_path="/peer-api/v1/",
        transport=transport,
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
    )
    assert client.base_path == "/peer-api/v1"
    client.list_peers(PeerQuery().cc("DE"))
    client.get_peer("192.0.2.10")
    client.facets()
    client.network_map_graph(max_tier=2)
    client.network_map_summary()
    client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
    assert transport.seen == [
        "/peer-api/v1/peers",
        "/peer-api/v1/peers/192.0.2.10",
        "/peer-api/v1/peers/facets",
        "/peer-api/v1/network-map/graph",
        "/peer-api/v1/network-map/summary",
        "/peer-api/v1/peers",
    ]


def test_owner_query_2_not_full_cc_sg(client: PeerApiClient) -> None:
    page = client.list_peers(not_full(cc="SG"))
    assert isinstance(page, PeerPage)
    assert page.ips() == ["198.51.100.7"]
    # single-page since ab9c95d: a dangling cursor was a dead end on the mock
    assert page.total == 1 and page.next_cursor is None


def test_owner_query_3_tier1_is_exact(client: PeerApiClient) -> None:
    page = client.list_peers(tier1())
    assert isinstance(page, PeerPage)
    assert page.total == 1 and page.next_cursor is None


def test_owner_query_4_sentries_uses_the_label(
    transport: FixtureTransport, client: PeerApiClient
) -> None:
    page = client.list_peers(sentries())
    assert isinstance(page, PeerPage)
    assert transport.requests[-1][1].endswith("?label=sentry")
    assert page.peers[0].id == "192.0.2.200"


def test_preset_geo_argument_is_tagged() -> None:
    with pytest.raises(Invalid):
        not_full()
    with pytest.raises(Invalid):
        not_full(cc="DE", region="eu-central")


def test_format_gossip_config_is_served(transport: FixtureTransport, client: PeerApiClient) -> None:
    result = client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
    assert result.config.root_ips() == ["192.0.2.10", "192.0.2.11", "198.51.100.7"]
    assert transport.requests[-1][1].endswith("/peers?cc=DE&format=gossip_config")


def test_available_false_is_not_an_error_and_not_an_empty_result(recorder: Recorder) -> None:
    client = make_client(FixtureTransport(scenario="unavailable"), recorder)
    page = client.list_peers()
    assert isinstance(page, PeerPage)
    assert page.available is False  # never an exception
    assert page.peers == []  # NOT a finding about the network
    assert page.generated_at is None


def test_iter_peers_stops_on_available_false_without_raising(recorder: Recorder) -> None:
    client = make_client(FixtureTransport(scenario="unavailable"), recorder)
    iterator = client.iter_peers()
    assert list(iterator) == []
    assert iterator.page is not None and iterator.page.available is False


def test_stale_true_is_surfaced(recorder: Recorder) -> None:
    client = make_client(FixtureTransport(scenario="stale"), recorder)
    page = client.list_peers()
    assert isinstance(page, PeerPage)
    assert page.available is True and page.stale is True


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------
def test_three_page_cursor_walk(transport: FixtureTransport, client: PeerApiClient) -> None:
    query = PeerQuery().limit(2).sort("tier")
    iterator = client.iter_peers(query)
    peers = list(iterator)
    assert [p.id for p in peers] == [
        "203.0.113.1",
        "203.0.113.2",
        "203.0.113.3",
        "203.0.113.4",
        "203.0.113.5",
    ]
    assert iterator.pages == 3 and iterator.truncated is False
    # every request but the first carries the previous page's cursor, everything
    # else byte-identical
    assert "cursor=cursor-page-2" in transport.requests[1][1]
    assert "cursor=cursor-page-3" in transport.requests[2][1]


def test_iter_peers_max_items_guard_stops_by_default(client: PeerApiClient) -> None:
    iterator = client.iter_peers(PeerQuery().limit(2).sort("tier"), max_items=3)
    peers = list(iterator)
    assert len(peers) == 3
    assert iterator.truncated is True


def test_iter_peers_max_items_can_raise(client: PeerApiClient) -> None:
    iterator = client.iter_peers(
        PeerQuery().limit(2).sort("tier"), max_items=3, on_truncate="raise"
    )
    with pytest.raises(Truncated) as excinfo:
        list(iterator)
    assert excinfo.value.seen == 3


def test_cursor_expired_is_surfaced_and_never_restarted(client: PeerApiClient) -> None:
    with pytest.raises(CursorExpired) as excinfo:
        client.list_peers(PeerQuery().cursor("cursor-from-a-previous-sweep"))
    assert isinstance(excinfo.value, BadRequest)  # a 400-family sibling


# ---------------------------------------------------------------------------
# single peer and facets
# ---------------------------------------------------------------------------
def test_get_peer(client: PeerApiClient) -> None:
    detail = client.get_peer("192.0.2.10")
    assert detail.peer.name == "Example Operator"
    assert detail.peer.feeders == ["192.0.2.200"]


def test_404_and_400_on_the_peer_path_do_not_collapse(client: PeerApiClient) -> None:
    with pytest.raises(NotFound):
        client.get_peer("198.51.100.254")
    # a malformed IPv4 never leaves the process: it would spend a quota unit
    with pytest.raises(Invalid) as excinfo:
        client.get_peer("not-an-ip")
    assert excinfo.value.parameter == "ip"


def test_ipv6_is_rejected_locally(client: PeerApiClient) -> None:
    with pytest.raises(Invalid) as excinfo:
        client.get_peer("2001:db8::1")
    assert "IPv4-only" in excinfo.value.reason


def test_facets_keep_not_collected_apart_from_nothing_matched(client: PeerApiClient) -> None:
    facets = client.facets()
    assert facets.total == 912
    assert facets.facet_available("cc") is True
    assert facets.facet_available("asn") is False  # not collected
    assert facets.facets["region"].values == []
    assert facets.facets["tier"].counts()["null"] == 110  # the absent-value member


def test_facets_refuses_a_non_filter_the_endpoint_would_ignore(client: PeerApiClient) -> None:
    with pytest.raises(Invalid) as excinfo:
        client.facets(PeerQuery().sort("tier"))
    assert excinfo.value.parameter == "sort"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
def test_400_filter_unavailable(client: PeerApiClient) -> None:
    with pytest.raises(FilterUnavailable) as excinfo:
        client.list_peers(PeerQuery().asn("AS24940"))
    error = excinfo.value
    assert error.filter == "asn"
    assert error.problem["type"].endswith("/filter-unavailable")
    assert error.rate_limit.limit == 20  # parsed from the error too


def test_400_filter_unavailable_on_region(client: PeerApiClient) -> None:
    with pytest.raises(FilterUnavailable) as excinfo:
        client.list_peers(PeerQuery().region("aws:eu-central-1"))
    assert excinfo.value.filter == "region"


def test_400_invalid_enum_is_caught_locally_before_the_wire(client: PeerApiClient) -> None:
    with pytest.raises(Invalid):
        PeerQuery().connectable("yess")


def test_400_invalid_enum_from_the_server(client: PeerApiClient) -> None:
    query = PeerQuery({"connectable": ["yess"]})  # bypass the local check
    with pytest.raises(BadRequest) as excinfo:
        client.list_peers(query)
    assert excinfo.value.allowed == ["yes", "no", "unknown"]


def test_400_chain_not_served(client: PeerApiClient) -> None:
    with pytest.raises(ChainNotServed) as excinfo:
        client.list_peers(PeerQuery().chain("Testnet"))
    assert excinfo.value.allowed == ["Mainnet"]


def test_401(transport: FixtureTransport, recorder: Recorder) -> None:
    client = make_client(transport, recorder, api_key="hlp_live_invalidkey0000000000")
    with pytest.raises(Unauthorized) as excinfo:
        client.list_peers()
    assert "anonymous" in (excinfo.value.detail or "")
    # the key is sent as a bearer token and never appears in the URL
    method, url, headers = transport.requests[-1]
    assert headers["Authorization"].startswith("Bearer ")
    assert "hlp_live" not in url


def test_500_non_problem_body_falls_back_to_status_only(recorder: Recorder) -> None:
    client = make_client(FixtureTransport(scenario="server-error"), recorder, max_retries=0)
    with pytest.raises(ServerError) as excinfo:
        client.list_peers()
    assert excinfo.value.problem is None
    assert excinfo.value.body_excerpt == "Internal Server Error"


def test_503_maps_to_unavailable(recorder: Recorder) -> None:
    client = make_client(FixtureTransport(scenario="producer-unavailable"), recorder, max_retries=0)
    with pytest.raises(Unavailable) as excinfo:
        client.list_peers()
    assert excinfo.value.problem["type"].endswith("/producer-unavailable")


def test_decode_error_on_a_non_json_200(recorder: Recorder) -> None:
    from hl_peers.transport import HttpResponse

    class Junk:
        def request(self, method, url, headers, timeout):  # type: ignore[no-untyped-def]
            return HttpResponse(200, {"content-type": "application/json"}, b"{not json")

    client = PeerApiClient(
        base_url="https://peers.test.invalid",
        transport=Junk(),
        env={},
    )
    with pytest.raises(DecodeError):
        client.list_peers()


# ---------------------------------------------------------------------------
# ETag / conditional requests
# ---------------------------------------------------------------------------
def test_304_returns_the_stored_body_with_fresh_rate_limit_headers(
    transport: FixtureTransport, client: PeerApiClient
) -> None:
    first = client.list_peers()
    assert isinstance(first, PeerPage) and first.from_cache is False
    second = client.list_peers()
    assert isinstance(second, PeerPage)
    assert second.from_cache is True and second.status == 304
    assert second.ips() == first.ips()
    assert "If-None-Match" in transport.requests[1][2]
    assert second.rate_limit.limit == 20


def test_a_caller_supplied_if_none_match_returns_not_modified(client: PeerApiClient) -> None:
    first = client.list_peers()
    assert isinstance(first, PeerPage) and first.etag
    result = client.list_peers(if_none_match=first.etag)
    assert isinstance(result, NotModified)
    assert result.status == 304


def test_the_cache_key_separates_anonymous_from_keyed(
    transport: FixtureTransport, recorder: Recorder
) -> None:
    from hl_peers.cache import EtagCache

    cache = EtagCache()
    anon = make_client(transport, recorder, cache=cache)
    anon.list_peers()
    make_client(transport, recorder, cache=cache, api_key="hlp_live_x")
    assert cache.get(EtagCache.key("GET", "/peers", "", True)) is None
    assert cache.get(EtagCache.key("GET", "/peers", "", False)) is not None


def test_cache_is_lru_bounded() -> None:
    from hl_peers.cache import CacheEntry, EtagCache

    cache = EtagCache(capacity=2)
    for i in range(3):
        cache.put(str(i), CacheEntry("etag%d" % i, {}, 0.0))
    assert len(cache) == 2
    assert cache.get("0") is None


# ---------------------------------------------------------------------------
# rate limiting and retries — decisions, never wall-clock delays
# ---------------------------------------------------------------------------
def test_429_parses_every_header_and_raises_at_once_by_default(recorder: Recorder) -> None:
    transport = FixtureTransport(scenario="rate-limited")
    client = make_client(transport, recorder)
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    error = excinfo.value
    assert error.retry_after == 42 and error.remaining == 0 and error.limit == 20
    assert error.signup_url == "https://hypervisor.gg/docs/peer-api/authentication"
    assert error.problem["detail"].startswith("Anonymous callers")  # surfaced, never parsed
    assert recorder.sleeps == []  # retries are OFF by default
    assert len(transport.requests) == 1


def test_429_with_explicit_max_retries_honours_retry_after(recorder: Recorder) -> None:
    transport = FixtureTransport(scenario="rate-limited")
    client = make_client(transport, recorder, max_retries=3)
    with pytest.raises(RateLimited):
        client.list_peers()
    assert recorder.sleeps == [42.0, 42.0, 42.0]  # max_retries attempts after the first
    assert len(transport.requests) == 4


def test_429_with_retry_after_over_the_cap_raises_immediately(recorder: Recorder) -> None:
    client = make_client(FixtureTransport(scenario="rate-limited-long"), recorder, max_retries=3)
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    assert excinfo.value.retry_after == 3600
    assert recorder.sleeps == []  # D-I: raise rather than block the caller for an hour


def test_503_is_retried_then_succeeds(transport: FixtureTransport, recorder: Recorder) -> None:
    transport.fail_first_n = 2
    client = make_client(transport, recorder, max_retries=3)
    page = client.list_peers()
    assert isinstance(page, PeerPage) and page.available is True
    assert len(recorder.sleeps) == 2
    assert recorder.sleeps[0] < recorder.sleeps[1]  # exponential


def test_transport_failures_are_retried(transport: FixtureTransport, recorder: Recorder) -> None:
    transport.raise_transport_first_n = 1
    client = make_client(transport, recorder, max_retries=3)
    assert isinstance(client.list_peers(), PeerPage)
    assert len(recorder.sleeps) == 1


def test_transport_failure_exhausts_retries(recorder: Recorder) -> None:
    transport = FixtureTransport(raise_transport_first_n=99)
    client = make_client(transport, recorder, max_retries=2)
    with pytest.raises(TransportError):
        client.list_peers()
    assert len(transport.requests) == 3


@pytest.mark.parametrize("scenario", ["error_400_filter_unavailable", "error_401_invalid_key"])
def test_4xx_other_than_429_is_never_retried(
    scenario: str, transport: FixtureTransport, recorder: Recorder
) -> None:
    client = make_client(
        transport, recorder, api_key="hlp_live_invalidkey0000000000", max_retries=3
    )
    query = PeerQuery().asn("AS24940") if "filter" in scenario else PeerQuery()
    with pytest.raises((FilterUnavailable, Unauthorized)):
        client.list_peers(query)
    assert len(transport.requests) == 1
    assert recorder.sleeps == []


def test_backoff_is_jittered_into_the_lower_half() -> None:
    from hl_peers._http import backoff_delay

    assert backoff_delay(0, 0.0) == 0.25
    assert backoff_delay(0, 1.0) == 0.5
    assert backoff_delay(10, 1.0) == 30.0


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def test_default_base_url_is_the_public_host_and_nothing_warns(
    transport: FixtureTransport, caplog: Any
) -> None:
    client = PeerApiClient(transport=transport, env={})
    assert client.base_url == DEFAULT_BASE_URL == "https://api.hypervisor.gg"
    assert client.max_retries == 0
    client.list_peers()
    assert transport.requests[-1][1] == "https://api.hypervisor.gg/peers"
    assert caplog.records == []  # no warning is logged
    # still accepted, as a no-op, so 0.x callers keep working
    PeerApiClient(transport=transport, env={}, suppress_placeholder_warning=True)


def test_env_resolution_order(transport: FixtureTransport) -> None:
    env = {"HL_PEER_API_BASE_URL": "https://env.invalid", "HL_PEER_API_KEY": "hlp_live_env"}
    client = PeerApiClient(transport=transport, env=env)
    assert client.base_url == "https://env.invalid"
    assert client.api_key == "hlp_live_env"
    explicit = PeerApiClient(
        "https://explicit.invalid",
        api_key="hlp_live_explicit",
        transport=transport,
        env=env,
    )
    assert explicit.base_url == "https://explicit.invalid"
    assert explicit.api_key == "hlp_live_explicit"


def test_user_agent_and_suffix(transport: FixtureTransport, recorder: Recorder) -> None:
    client = make_client(transport, recorder, user_agent_suffix="fleetctl/2.1")
    client.list_peers()
    assert transport.requests[-1][2]["User-Agent"].startswith("hl-peers-python/1.1.0")
    assert transport.requests[-1][2]["User-Agent"].endswith(" fleetctl/2.1")


def test_poll_peers_refuses_a_too_fast_interval(client: PeerApiClient) -> None:
    with pytest.raises(Invalid) as excinfo:
        client.poll_peers(None, 5, lambda page: None)
    assert excinfo.value.parameter == "interval"


def test_poll_peers_uses_the_cache_and_the_refresh_clock(
    transport: FixtureTransport, recorder: Recorder
) -> None:
    client = make_client(transport, recorder)
    seen = []
    client.poll_peers(None, 60, seen.append, max_polls=2)
    assert len(seen) == 2
    assert seen[1].from_cache is True  # the second poll cost no quota unit
    assert recorder.sleeps and recorder.sleeps[0] > 60  # next_sweep_at plus jitter


# ---------------------------------------------------------------------------
# Retry-After, in every form the fixtures carry (CLIENT_SPEC §5.2)
# ---------------------------------------------------------------------------
RATE_LIMIT_FIXTURES = [f for f in load_http_fixtures() if f["response"]["status"] == 429]


@pytest.mark.parametrize(
    "fixture", RATE_LIMIT_FIXTURES, ids=[f["name"] for f in RATE_LIMIT_FIXTURES]
)
def test_every_429_resolves_retry_after_as_the_fixture_says(
    fixture: Any, recorder: Recorder
) -> None:
    """`expect.retry_after_seconds` is the answer; `clock.now` is the instant to
    inject, so the assertion never depends on wall-clock time."""
    from datetime import datetime, timezone

    scenario = fixture["request"]["headers"]["X-Mock-Scenario"]
    now = fixture.get("clock", {}).get("now")
    instant = (
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now
        else datetime.now(timezone.utc)
    )
    client = make_client(
        FixtureTransport(scenario=scenario), recorder, clock=lambda: instant, max_retries=1
    )
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    expected = fixture["expect"]["retry_after_seconds"]
    assert excinfo.value.retry_after == expected
    assert excinfo.value.rate_limit.retry_after == expected
    assert excinfo.value.rate_limit.retry_after_raw == fixture["response"]["headers"]["Retry-After"]

    if expected is None:
        assert recorder.sleeps and recorder.sleeps[0] != 0  # fell back to backoff
    elif expected > client.max_retry_after:
        assert recorder.sleeps == []  # D-I: raise rather than block
    else:
        assert recorder.sleeps == [float(expected)]


def test_an_http_date_retry_after_in_the_past_means_retry_now(recorder: Recorder) -> None:
    from datetime import datetime, timezone

    client = make_client(
        FixtureTransport(scenario="rate-limited-http-date"),
        recorder,
        clock=lambda: datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc),  # after the date
        max_retries=1,
    )
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    assert excinfo.value.retry_after == 0  # 0 is "retry immediately", not "absent"
    assert recorder.sleeps == [0.0]
