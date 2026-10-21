#!/usr/bin/env python3
"""Drive all four clients through the same programmes and diff the answers.

This is the differential test across the four clients. Each client ships a tiny
JSON-in / JSON-out harness under ``<lang>/cross_check/``; this driver streams the
same NDJSON programmes into all four and asserts that every answer is identical —
**byte for byte** on ``json_text``, and equal on error kind, warning-code sets,
canonical request URLs, parsed peer lists and rate-limit fields.

    python3 tools/cross_check/run.py                 # everything
    python3 tools/cross_check/run.py --only gossip   # one family
    python3 tools/cross_check/run.py --langs python,rust

Pure stdlib. Exit status is 0 only when all four agree on every programme.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
GENERATE = REPO / "tools" / "cross_check" / "generate.py"
MOCK = REPO / "tools" / "mock_server.py"
HTTP_FIXTURES = REPO / "fixtures" / "http"

# How to start each harness. Every one of them speaks the protocol in
# tools/cross_check/README.md.
HARNESSES: Dict[str, Dict[str, Any]] = {
    "python": {
        "cwd": REPO / "python",
        "argv": [
            str(REPO / "python" / ".venv" / "bin" / "python")
            if (REPO / "python" / ".venv" / "bin" / "python").exists()
            else sys.executable,
            "cross_check/harness.py",
        ],
    },
    "typescript": {
        "cwd": REPO / "typescript",
        "argv": ["node", "cross_check/harness.ts"],
    },
    "rust": {
        "cwd": REPO / "rust",
        "argv": ["./target/debug/cross-check"],
        "build": ["cargo", "build", "--features", "blocking", "--bin", "cross-check"],
    },
    "elixir": {
        "cwd": REPO / "elixir",
        "argv": ["mix", "run", "cross_check/harness.exs"],
        "env": {"MIX_ENV": "dev", "MIX_QUIET": "1"},
    },
}


class Harness:
    """One long-lived client subprocess, one NDJSON line in, one line out."""

    def __init__(self, name: str, spec: Dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        env = dict(os.environ)
        env.pop("HL_PEER_API_BASE_URL", None)
        env.pop("HL_PEER_API_KEY", None)
        env.update(spec.get("env", {}))
        if spec.get("build"):
            subprocess.run(spec["build"], cwd=spec["cwd"], env=env, check=True)
        self.proc = subprocess.Popen(
            spec["argv"],
            cwd=spec["cwd"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def ask(self, programme: Dict[str, Any]) -> Dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(programme, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            return {"id": programme["id"], "ok": False, "harness_error": "harness exited"}
        return json.loads(line)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            self.proc.kill()


# ---------------------------------------------------------------------------
# the mock server, one instance per http programme
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockServer:
    """`tools/mock_server.py` over a subset of `fixtures/http`.

    Serving exactly one fixture, with its ``X-Mock-Scenario`` header stripped, is
    how an otherwise-unreachable response is reached: that header is a mock
    affordance and no real client sends it.
    """

    def __init__(self, spec: Dict[str, Any]) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="hlp-xcheck-fixtures-"))
        only = spec.get("only")
        for path in sorted(HTTP_FIXTURES.glob("*.json")):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            if only is not None and fixture["name"] not in only:
                continue
            if spec.get("strip_headers"):
                fixture["request"].pop("headers", None)
            (self.dir / path.name).write_text(
                json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
            )
        self.port = free_port()
        argv = [
            sys.executable,
            str(MOCK),
            "--port",
            str(self.port),
            "--fixtures",
            str(self.dir),
        ]
        if spec.get("fail_first_n"):
            argv += ["--fail-first-n", str(spec["fail_first_n"])]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._wait()

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def _wait(self) -> None:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return
            except OSError:
                time.sleep(0.03)
        raise RuntimeError("the mock server did not come up on port %d" % self.port)

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

# `--fail-first-n` counts requests server-side, so a client that retried a
# different number of times still sees the same final answer but a different
# request count; the answer is what the spec pins, and delays are explicitly
# not assertable (CLIENT_SPEC §5.2). Nothing volatile is compared.
IGNORED_KEYS = {"id"}


def normalise(answer: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in answer.items() if k not in IGNORED_KEYS}


def describe(name: str, value: Any, limit: int = 220) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        text = text[:limit] + "…"
    return "    %-11s %s" % (name + ":", text)


def compare(
    programme: Dict[str, Any], answers: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    normalised = {lang: normalise(a) for lang, a in answers.items()}
    langs = sorted(normalised)
    first = normalised[langs[0]]
    if all(normalised[lang] == first for lang in langs[1:]):
        return None

    keys = sorted({k for a in normalised.values() for k in a})
    lines = ["--- %s" % programme["id"]]
    for key in keys:
        values = {lang: normalised[lang].get(key) for lang in langs}
        distinct = {json.dumps(v, sort_keys=True, ensure_ascii=False) for v in values.values()}
        if len(distinct) > 1:
            lines.append("  %s" % key)
            for lang in langs:
                lines.append(describe(lang, values[lang]))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["gossip", "query_url", "http"], default=None)
    parser.add_argument("--langs", default=",".join(HARNESSES))
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--random-ops", type=int, default=240)
    parser.add_argument("--random-queries", type=int, default=120)
    parser.add_argument("--max-failures", type=int, default=25, help="how many to print")
    args = parser.parse_args()

    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    for lang in langs:
        if lang not in HARNESSES:
            print("unknown language %r" % lang, file=sys.stderr)
            return 2

    generate = [
        sys.executable,
        str(GENERATE),
        "--seed",
        str(args.seed),
        "--random-ops",
        str(args.random_ops),
        "--random-queries",
        str(args.random_queries),
    ]
    if args.only:
        generate += ["--only", args.only]
    raw = subprocess.run(generate, capture_output=True, text=True, check=True).stdout
    programmes = [json.loads(line) for line in raw.splitlines() if line.strip()]

    print("cross_check: %d programmes x %d clients" % (len(programmes), len(langs)))
    harnesses = {lang: Harness(lang, HARNESSES[lang]) for lang in langs}

    failures: List[str] = []
    counts: Dict[str, int] = {}
    try:
        for programme in programmes:
            counts[programme["kind"]] = counts.get(programme["kind"], 0) + 1
            request = dict(programme)
            server_spec = request.pop("server", None) or {}
            if programme["kind"] != "http":
                answers = {lang: harnesses[lang].ask(request) for lang in langs}
            elif server_spec.get("fail_first_n"):
                # 🪤 `--fail-first-n` counts requests server-side, so one shared
                # server would hand the first client every injected failure and
                # the next client a clean 200. Each client gets its own.
                answers = {}
                for lang in langs:
                    server = MockServer(server_spec)
                    try:
                        answers[lang] = harnesses[lang].ask(
                            dict(request, base_url=server.base_url)
                        )
                    finally:
                        server.close()
            else:
                server = MockServer(server_spec)
                try:
                    request["base_url"] = server.base_url
                    answers = {lang: harnesses[lang].ask(request) for lang in langs}
                finally:
                    server.close()

            broken = [
                "%s: %s" % (lang, a["harness_error"])
                for lang, a in answers.items()
                if "harness_error" in a
            ]
            if broken:
                failures.append("--- %s\n  harness error\n    %s" % (programme["id"], "\n    ".join(broken)))
                continue
            problem = compare(programme, answers)
            if problem:
                failures.append(problem)
    finally:
        for harness in harnesses.values():
            harness.close()

    for kind, n in sorted(counts.items()):
        print("  %-10s %d" % (kind, n))
    if not failures:
        print("cross_check: all %d programmes agree across %s" % (len(programmes), ", ".join(langs)))
        return 0

    print("\ncross_check: %d disagreement(s)\n" % len(failures))
    for failure in failures[: args.max_failures]:
        print(failure)
    if len(failures) > args.max_failures:
        print("… and %d more" % (len(failures) - args.max_failures))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
