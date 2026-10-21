"""The documentation is executable.

``pycon`` blocks in ``README.md`` and ``docs/python.md`` run as doctests; every
fenced ``python`` block tagged ``# runnable: mock`` is executed against the
spawned mock server, with ``$HOME`` pointed at a throwaway directory so the
gossip examples write where they say they do.
"""

from __future__ import annotations

import doctest
import os
import re
from pathlib import Path
from typing import List, Tuple

import pytest

from conftest import REPO_ROOT
from test_integration_mock import mock_server  # noqa: F401

README = Path(__file__).resolve().parents[1] / "README.md"
GUIDE = REPO_ROOT / "docs" / "python.md"
DOCS = [README, GUIDE]

BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def runnable_blocks(path: Path) -> List[Tuple[str, str]]:
    blocks = []
    for index, body in enumerate(BLOCK.findall(path.read_text(encoding="utf-8"))):
        if "# runnable: mock" in body:
            blocks.append(("%s#%d" % (path.name, index), body))
    return blocks


ALL_BLOCKS = [b for path in DOCS if path.exists() for b in runnable_blocks(path)]


@pytest.mark.parametrize("name,source", ALL_BLOCKS, ids=[b[0] for b in ALL_BLOCKS])
def test_doc_example_runs(
    name: str, source: str, mock_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HL_PEER_API_BASE_URL", mock_server)
    monkeypatch.delenv("HL_PEER_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    namespace: dict = {"__name__": "__doc_example__"}
    exec(compile(source, name, "exec"), namespace)


def test_there_are_doc_examples() -> None:
    assert len(ALL_BLOCKS) >= 8, "the docs lost their runnable examples"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_doctests_in_the_docs(path: Path) -> None:
    if not path.exists():
        pytest.skip("%s not written yet" % path)
    result = doctest.testfile(
        str(path),
        module_relative=False,
        optionflags=doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS,
        verbose=False,
    )
    assert result.failed == 0, "%d doctest failures in %s" % (result.failed, path.name)


@pytest.mark.parametrize(
    "module_name",
    ["hl_peers.query", "hl_peers.gossip", "hl_peers.models", "hl_peers.client"],
)
def test_doctests_in_the_package(module_name: str) -> None:
    import importlib

    module = importlib.import_module(module_name)
    result = doctest.testmod(module, verbose=False)
    assert result.failed == 0


def test_the_loud_note_is_in_every_required_place() -> None:
    """CLIENT_SPEC §6 requires the 'read when it starts' note in
    the GossipConfig module docstring, in save's docstring, and in the README."""
    from hl_peers import GossipConfig
    from hl_peers import gossip as gossip_module

    flat = lambda text: " ".join((text or "").replace("\n>", "\n").split())  # noqa: E731
    module_doc = flat(gossip_module.__doc__)
    save_doc = flat(GossipConfig.save.__doc__)
    readme = flat(README.read_text(encoding="utf-8"))
    for text in (module_doc, save_doc, readme):
        assert "when it starts" in text
    assert "never** restart" in module_doc and "never** restart" in readme
    assert "never restarts, signals or reloads anything" in save_doc
    assert "$HOME/override_gossip_config.json" in module_doc
    assert "$HOME/override_gossip_config.json" in readme
    assert os.path.basename(str(README)) == "README.md"
