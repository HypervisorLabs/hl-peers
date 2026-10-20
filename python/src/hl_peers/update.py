"""``update_gossip_config`` — the one helper that joins the two namespaces.

Everything else in this package is either pure HTTP (:mod:`hl_peers.client`)
or pure offline file algebra (:mod:`hl_peers.gossip`). This function does one
round of work: query, merge, validate, and — only if you gave it a path — write.

🔴 It never restarts, signals or reloads anything. The node reads
``override_gossip_config.json`` when it starts: a successful
update has **not** peered your node, it wrote a file. When to restart is yours
to schedule.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from .errors import Unavailable
from .gossip import ConfigWarning, GossipConfig, PeerLike, SaveResult
from .models import Envelope, PeerPage
from .query import PeerQuery

__all__ = ["UpdateResult", "update_gossip_config"]


@dataclass(frozen=True)
class UpdateResult:
    """``json`` is always present, even when nothing was written, so "generate
    to stdout and let the operator place it" is a first-class path."""

    config: GossipConfig
    json: str
    changed: bool
    diff: str = ""
    written_path: Optional[str] = None
    warnings: List[ConfigWarning] = field(default_factory=list)
    envelope: Optional[Envelope] = None
    save_result: Optional[SaveResult] = None

    @property
    def warning_codes(self) -> List[str]:
        return [w.code for w in self.warnings]


def update_gossip_config(
    client: Any = None,
    query: Optional[PeerQuery] = None,
    path: Optional[str] = None,
    *,
    peers: Optional[Sequence[PeerLike]] = None,
    chain: Optional[str] = None,
    max_roots: int = 8,
    overfetch: int = 4,
    mode: str = "replace",
    dedupe: bool = True,
    keep_existing: Sequence[str] = (),
    exclude: Sequence[str] = (),
    try_new_peers: Optional[bool] = None,
    n_gossip_peers: Optional[int] = None,
    backup: bool = False,
    file_mode: Optional[int] = None,
    dry_run: bool = False,
    no_write: bool = False,
) -> UpdateResult:
    """Build or refresh an ``override_gossip_config.json`` from a peer query.

    Pass ``peers=`` instead of ``client``/``query`` to skip the network
    entirely — the whole :class:`~hl_peers.gossip.GossipConfig` half of this
    package works on a box with no API access.

    🪤 ``file_mode`` is the permission-bits option (§6.6); ``mode`` is the
    merge strategy (``replace`` | ``append`` | ``prepend``) and nothing else.

    🔴 If the response carries ``available: false`` this raises
    :class:`~hl_peers.errors.Unavailable` **before touching any file** (D-G).
    An unbuildable graph is not an empty peer set, and turning it into one would
    empty an operator's root list — the crash-loop shape.
    """
    envelope: Optional[Envelope] = None
    candidates: Sequence[PeerLike]

    if peers is not None:
        candidates = list(peers)
    else:
        if client is None:
            raise ValueError("pass either a client (with a query) or an explicit peers list")
        iterator = client.iter_peers(query or PeerQuery(), max_items=max_roots * overfetch)
        collected = list(iterator)
        page: Optional[PeerPage] = getattr(iterator, "page", None)
        if page is not None:
            envelope = page.envelope
            if not page.available:
                raise Unavailable(
                    "the producer could not build a graph (available: false); refusing to "
                    "build a config from it, because an empty root_node_ips crash-loops "
                    "the node",
                )
        candidates = collected

    resolved_chain = chain or (envelope.chain if envelope else None)

    if path is not None and os.path.exists(path):
        config = GossipConfig.load(path)
    else:
        if resolved_chain is None:
            from .errors import Invalid

            raise Invalid(
                "chain",
                "no chain given and none in the response envelope; refusing to default to "
                "Mainnet, because a chain that disagrees with the node's --chain stops the "
                "node from starting",
            )
        config = GossipConfig.new(resolved_chain, [])

    config.merge_peers(
        candidates,
        max_roots=max_roots,
        mode=mode,
        dedupe=dedupe,
        keep_existing=keep_existing,
        exclude=exclude,
        chain=chain,
        try_new_peers=try_new_peers,
    )
    if n_gossip_peers is not None:
        config.set_n_gossip_peers(n_gossip_peers)

    warnings = config.validate()
    rendered = config.to_json()

    if path is None or no_write:
        changed = True
        diff = ""
        if path is not None and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                changed = handle.read() != rendered
        return UpdateResult(config, rendered, changed, diff, None, warnings, envelope)

    result = config.save(
        path,
        dry_run=dry_run,
        backup=backup,
        file_mode=file_mode,
        now=getattr(client, "clock", None) if client is not None else None,
    )
    return UpdateResult(
        config=config,
        json=rendered,
        changed=result.changed,
        diff=result.diff,
        written_path=path if result.written else None,
        warnings=result.warnings,
        envelope=envelope,
        save_result=result,
    )
