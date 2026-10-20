"""Integration: a real socket against ``tools/mock_server.py``.

Still no network — the server is spawned on loopback, on a free port, and torn
down afterwards. This is the only place the stdlib ``urllib`` transport and the
``httpx`` ones are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from conftest import REPO_ROOT
from hl_peers import (
    GossipConfig,
    PeerApiClient,
    PeerQuery,
    RateLimited,
    not_full,
    sentries,
    tier1,
    update_gossip_config,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def mock_server() -> Iterator[str]:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tools" / "mock_server.py"), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = "http://127.0.0.1:%d" % port
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if process.poll() is not None:
                raise RuntimeError(
                    "mock_server exited: %s" % process.stderr.read().decode()
                ) from None
            time.sleep(0.05)
    else:  # pragma: no cover
        process.kill()
        raise RuntimeError("mock_server did not start")
    try:
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


@pytest.fixture()
def live_client(mock_server: str) -> PeerApiClient:
    """The stdlib-urllib client, against the spawned server."""
    return PeerApiClient(mock_server, env={})


def test_stdlib_client_against_the_mock(live_client: PeerApiClient) -> None:
    page = live_client.list_peers()
    assert page.count == 5 and page.available is True
    assert page.rate_limit.limit == 20


def test_every_owner_preset_against_the_mock(live_client: PeerApiClient) -> None:
    assert live_client.list_peers(PeerQuery().cc("DE")).ips() == ["192.0.2.10", "192.0.2.11"]
    assert live_client.list_peers(not_full(cc="SG")).ips() == ["198.51.100.7"]
    assert live_client.list_peers(tier1()).peers[0].tier == 1
    assert live_client.list_peers(sentries()).peers[0].id == "192.0.2.200"


def test_pagination_and_conditional_polling_against_the_mock(live_client: PeerApiClient) -> None:
    peers = list(live_client.iter_peers(PeerQuery().limit(2).sort("tier")))
    assert len(peers) == 5
    first = live_client.list_peers()
    second = live_client.list_peers()  # the mock synthesises the 304
    assert second.from_cache is True and second.status == 304
    assert second.ips() == first.ips()


def test_get_peer_and_facets_against_the_mock(live_client: PeerApiClient) -> None:
    detail = live_client.get_peer("192.0.2.10")
    assert detail.peer.cc == "DE" and detail.peer.mempool.serves is True
    assert detail.api_version == "1"
    facets = live_client.facets()
    assert facets.facet_available("asn") is False
    assert facets.facet_available("serves_mempool") is True


def test_network_map_and_gossip_config_against_the_mock(
    live_client: PeerApiClient, tmp_path: Path
) -> None:
    graph = live_client.network_map_graph(max_tier=2)
    assert graph.edge_count == 3 and graph.edges[0].from_ == "192.0.2.200"
    assert live_client.network_map_summary().tier_count == 3
    result = live_client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
    path = tmp_path / "override_gossip_config.json"
    assert result.save(str(path)).written is True
    assert path.read_text(encoding="utf-8") == result.to_json()


def test_retry_against_the_mock_fail_first_n() -> None:
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "mock_server.py"),
            "--port",
            str(port),
            "--fail-first-n",
            "2",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        sleeps = []
        client = PeerApiClient(
            "http://127.0.0.1:%d" % port,
            env={},
            sleep=sleeps.append,
            max_retries=3,  # retries are OFF by default; opt in
        )
        page = client.list_peers()
        assert page.count == 5
        assert len(sleeps) == 2  # two 503s, two backoffs, then the answer
    finally:
        process.terminate()
        process.wait(timeout=10)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def test_update_gossip_config_end_to_end(live_client: PeerApiClient, tmp_path: Path) -> None:
    path = str(tmp_path / "override_gossip_config.json")
    result = update_gossip_config(live_client, PeerQuery().cc("DE"), path)
    assert result.written_path == path
    on_disk = GossipConfig.load(path)
    assert on_disk.root_ips() == ["192.0.2.10", "192.0.2.11"]
    assert on_disk.chain() == "Mainnet"


# ---------------------------------------------------------------------------
# the optional httpx extra
# ---------------------------------------------------------------------------
httpx = pytest.importorskip("httpx", reason="the [httpx] extra is not installed")


def test_httpx_sync_transport(mock_server: str) -> None:
    from hl_peers.httpx_transport import HttpxTransport

    with HttpxTransport() as transport:
        client = PeerApiClient(mock_server, transport=transport, env={})
        assert client.list_peers(tier1()).count == 1


def test_httpx_async_client(mock_server: str) -> None:
    from hl_peers.aio import AsyncPeerApiClient

    async def run() -> None:
        async with AsyncPeerApiClient(mock_server, env={}) as client:
            page = await client.list_peers(PeerQuery().cc("DE"))
            assert page.ips() == ["192.0.2.10", "192.0.2.11"]
            seen = [peer.id async for peer in client.iter_peers(PeerQuery().limit(2).sort("tier"))]
            assert len(seen) == 5
            detail = await client.get_peer("192.0.2.10")
            assert detail.peer.cc == "DE"
            facets = await client.facets()
            assert facets.facet_available("cc") is True
            assert facets.api_version == "1"
            graph = await client.network_map_graph(max_tier=2)
            assert graph.node_count == 4 and len(graph.nodes) == 4
            summary = await client.network_map_summary()
            assert summary.connectable == {"yes": 3, "no": 1, "unknown": 0}
            gossip = await client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
            assert gossip.config.chain() == "Mainnet"

    asyncio.run(run())


def test_httpx_async_client_maps_a_429(mock_server: str) -> None:
    from hl_peers.aio import AsyncPeerApiClient

    class Scenario:
        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self.inner = inner

        async def request(self, method, url, headers, timeout):  # type: ignore[no-untyped-def]
            headers = dict(headers)
            headers["X-Mock-Scenario"] = "rate-limited"
            return await self.inner.request(method, url, headers, timeout)

        async def aclose(self) -> None:
            await self.inner.aclose()

    async def run() -> None:
        from hl_peers.aio import HttpxAsyncTransport

        sleeps = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        async with AsyncPeerApiClient(
            mock_server,
            transport=Scenario(HttpxAsyncTransport()),
            sleep=sleep,
            env={},
        ) as client:
            with pytest.raises(RateLimited) as excinfo:
                await client.list_peers()
            assert excinfo.value.retry_after == 42
            assert excinfo.value.reset_at is not None
            assert sleeps == []  # retries are OFF by default
            client.max_retries = 3
            with pytest.raises(RateLimited):
                await client.list_peers()
            assert sleeps == [42.0, 42.0, 42.0]

    asyncio.run(run())


def test_the_public_host_is_spelled_once() -> None:
    """The default host lives in one constant; nothing else hardcodes a host."""
    src = Path(__file__).resolve().parents[1] / "src" / "hl_peers"
    spelled = [
        path.name
        for path in src.glob("*.py")
        if '"https://api.hypervisor.gg"' in path.read_text(encoding="utf-8")
    ]
    assert spelled == ["_http.py"]
    for path in src.glob("*.py"):
        assert "hyperliquid.xyz" not in path.read_text(encoding="utf-8")
    assert "HL_PEER_API_BASE_URL" in (src / "client.py").read_text(encoding="utf-8")


def test_default_run_needs_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "HL_PEER_API_BASE_URL" not in os.environ
    assert json is not None
