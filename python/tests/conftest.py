"""Shared test plumbing. No network is used anywhere in the default test run."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
HTTP_FIXTURES = FIXTURES / "http"
GOSSIP_CASES = FIXTURES / "gossip" / "cases"
TOOLS = REPO_ROOT / "tools"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hl_peers.errors import TransportError  # noqa: E402
from hl_peers.transport import HttpResponse  # noqa: E402


def load_http_fixtures() -> List[Dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(HTTP_FIXTURES.glob("*.json"))
    ]


def _query_key(pairs: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in pairs.items()))


class FixtureTransport:
    """Replays ``fixtures/http`` exactly as ``tools/mock_server.py`` would.

    A fixture that names request headers outranks a header-free one for the same
    method/path/query — that is how one query has both a 200 and a 429.
    """

    def __init__(
        self,
        fixtures: Optional[List[Dict[str, Any]]] = None,
        *,
        fail_first_n: int = 0,
        raise_transport_first_n: int = 0,
        scenario: Optional[str] = None,
    ) -> None:
        self.fixtures = fixtures if fixtures is not None else load_http_fixtures()
        #: ``X-Mock-Scenario`` is a fixture affordance, not part of the API: real
        #: clients never send it, fixture-driven tests do, to select an otherwise
        #: unreachable response for the same query.
        self.scenario = scenario
        self.fail_first_n = fail_first_n
        self.raise_transport_first_n = raise_transport_first_n
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []

    def by_name(self, name: str) -> Dict[str, Any]:
        for fixture in self.fixtures:
            if fixture["name"] == name:
                return fixture
        raise KeyError(name)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        from urllib.parse import parse_qsl, urlparse

        headers = dict(headers)
        if self.scenario:
            headers["X-Mock-Scenario"] = self.scenario
        self.requests.append((method, url, dict(headers)))
        n = len(self.requests)
        if n <= self.raise_transport_first_n:
            raise TransportError("injected transport failure %d" % n)
        if n <= self.fail_first_n:
            return HttpResponse(
                503,
                {"content-type": "application/problem+json"},
                json.dumps(
                    {
                        "type": "https://example.invalid/problems/producer-unavailable",
                        "title": "Peer graph unavailable",
                        "status": 503,
                        "detail": "Injected failure %d." % n,
                    }
                ).encode(),
            )

        parsed = urlparse(url)
        query = tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        exact, loose = [], []
        for fixture in self.fixtures:
            request = fixture["request"]
            if request["method"] != method or request["path"] != parsed.path:
                continue
            if _query_key(request.get("query", {})) != query:
                continue
            wanted = request.get("headers") or {}
            if wanted:
                if all(headers.get(k) == v for k, v in wanted.items()):
                    exact.append(fixture)
            else:
                loose.append(fixture)
        matches = exact + loose
        if not matches:
            return HttpResponse(
                404,
                {"content-type": "application/problem+json"},
                json.dumps(
                    {
                        "type": "https://example.invalid/problems/no-fixture",
                        "title": "No fixture matches this request",
                        "status": 404,
                        "detail": "%s %s" % (method, url),
                    }
                ).encode(),
            )

        fixture = matches[0]
        response = fixture["response"]
        response_headers = {
            str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()
        }

        inm = headers.get("If-None-Match")
        if inm and response.get("status") == 200 and response_headers.get("etag") == inm:
            keep = {
                k: v
                for k, v in response_headers.items()
                # A real 304 carries API-Version like every other response
                # (CLIENT_SPEC §1), so the replay keeps it.
                if k in ("etag", "cache-control", "api-version") or k.startswith("ratelimit-")
            }
            return HttpResponse(304, keep, b"")

        if "body_text" in response:
            body = response["body_text"].encode("utf-8")
        elif response.get("body") is None:
            body = b""
        else:
            body = json.dumps(response["body"], ensure_ascii=False).encode("utf-8")
        return HttpResponse(int(response["status"]), response_headers, body)


class Recorder:
    """A deterministic stand-in for ``time.sleep`` and ``random.random``."""

    def __init__(self, rand: float = 0.5) -> None:
        self.sleeps: List[float] = []
        self._rand = rand

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def random(self) -> float:
        return self._rand


@pytest.fixture()
def transport() -> FixtureTransport:
    return FixtureTransport()


@pytest.fixture()
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture()
def client(transport: FixtureTransport, recorder: Recorder):
    from hl_peers import PeerApiClient

    return PeerApiClient(
        base_url="https://peers.test.invalid",
        transport=transport,
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
    )


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def tools_dir() -> Path:
    return TOOLS


os.environ.pop("HL_PEER_API_BASE_URL", None)
os.environ.pop("HL_PEER_API_KEY", None)
