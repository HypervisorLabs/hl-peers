"""``GossipConfig`` — a pure, offline, deterministic editor for hl-node's
``override_gossip_config.json``.

**No network, no clock, no environment lookups, no restart of anything.** Given
the same input it produces the same bytes on every platform and in every one of
the four client languages. This module imports nothing outside the standard
library, so it works on a box with no API access and no packages at all.

🔴 **The node reads ``override_gossip_config.json`` when it
starts.** Writing this file does **not** steer a running node: the change takes
effect at the node's next start. This library will **never** restart, signal, or
otherwise disturb your node — that is the operator's decision, at the operator's
moment. Restarting a node *to apply a config* while it is behind can make a
lagging box worse.

The node's file is ``$HOME/override_gossip_config.json``.
"""

from __future__ import annotations

import difflib
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from .errors import DecodeError, Invalid, NotFound

__all__ = [
    "KNOWN_KEY_ORDER",
    "ConfigWarning",
    "GossipConfig",
    "SaveResult",
    "default_config_path",
    "render_config",
]

#: CLIENT_SPEC §6.3(1) / D-F. The live minimal file's order, extended by the
#: upstream reference example. Absent keys are skipped, never emitted as null.
KNOWN_KEY_ORDER = (
    "chain",
    "root_node_ips",
    "try_new_peers",
    "reserved_peer_ips",
    "n_gossip_peers",
    "split_client_blocks",
)

CHAINS = ("Mainnet", "Testnet")

EMPTY_ROOTS_REASON = (
    "refusing to emit an empty root_node_ips: hl-node asserts on a non-empty list at "
    "start and will crash-loop; there is no built-in fallback"
)

IPV6_REASON = (
    "IPv6 is out of contract: the peer API is IPv4-only and the node's support for an "
    "IPv6 literal here is unverified. Refusing rather than writing an untested value into "
    "a file whose failure mode is a crash loop."
)

PeerLike = Union[str, Mapping[str, Any], Any]


# ---------------------------------------------------------------------------
# IPv4
# ---------------------------------------------------------------------------
def is_ipv4(value: Any) -> bool:
    """Strict dotted quad. No leading zeros, no ranges, no host names."""
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part[0] == "0") or len(part) > 3:
            return False
        if int(part) > 255:
            return False
    return True


def looks_like_ipv6(value: Any) -> bool:
    return isinstance(value, str) and ":" in value


def _ip_reason(value: Any) -> str:
    if looks_like_ipv6(value):
        return IPV6_REASON
    return "%s is not an IPv4 address" % (value,)


def _require_ipv4(parameter: str, value: Any) -> str:
    if not is_ipv4(value):
        raise Invalid(parameter, _ip_reason(value))
    return str(value)


# ---------------------------------------------------------------------------
# warnings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigWarning:
    """A non-fatal finding. ``code`` is stable and asserted by the conformance
    suite; ``message`` is prose and is not."""

    code: str
    message: str
    detail: Optional[str] = None

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


WARNING_CODES = (
    "many_roots_gt_24",
    "many_roots_gt_64",
    "chain_absent",
    "try_new_peers_false_multi_root",
    "truncated",
    "exclude_overrides_keep_existing",
)

_MANY_ROOTS_MESSAGE = (
    "a long root list makes the node slower to start and buys nothing; prefer a short list"
)


@dataclass(frozen=True)
class SaveResult:
    """What :meth:`GossipConfig.save` did — and, on a no-op, what it did not do."""

    written: bool
    changed: bool
    path: str
    diff: str = ""
    backup_path: Optional[str] = None
    warnings: List[ConfigWarning] = field(default_factory=list)
    #: Where the bytes actually landed. It differs from :attr:`path` only when
    #: ``path`` was a symlink, which ``save`` follows rather than replacing
    #: (CLIENT_SPEC §6.6).
    resolved_path: Optional[str] = None

    @property
    def warning_codes(self) -> List[str]:
        return [w.code for w in self.warnings]


def default_config_path() -> str:
    """``$HOME/override_gossip_config.json``.

    🪤 A *default*, never a discovery: this library never inspects a running
    process, a systemd unit or ``/proc`` to guess the path. The upstream
    evidence cannot distinguish ``$HOME``-relative from cwd-relative, because on
    every observed box ``cwd == $HOME``; ``$HOME``-relative is the labelled
    **hypothesis**. If you are unsure, render to stdout with ``to_json()`` and
    place the file yourself — that is the safest option.
    """
    return os.path.join(os.path.expanduser("~"), "override_gossip_config.json")


# ---------------------------------------------------------------------------
# serializer — CLIENT_SPEC §6.3, byte-exact across four languages
# ---------------------------------------------------------------------------
def _sort_unknown(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_unknown(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sort_unknown(v) for v in value]
    return value


def _ordered(data: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in KNOWN_KEY_ORDER:
        if key in data:
            out[key] = data[key]
    for key in sorted(k for k in data if k not in KNOWN_KEY_ORDER):
        out[key] = _sort_unknown(data[key])
    return out


def render_config(data: Mapping[str, Any]) -> str:
    """The normative rendering: fixed key order, unknown keys sorted after the
    known ones, 2-space indent, ``": "`` after a colon, literal UTF-8, exactly
    one trailing newline."""
    return json.dumps(_ordered(data), indent=2, ensure_ascii=False, separators=(",", ": ")) + "\n"


def _peer_ip(peer: PeerLike) -> Any:
    if isinstance(peer, str):
        return peer
    if isinstance(peer, Mapping):
        if "id" in peer:
            return peer["id"]
        if "Ip" in peer:
            return peer["Ip"]
        return peer
    return getattr(peer, "id", peer)


# ---------------------------------------------------------------------------
# GossipConfig
# ---------------------------------------------------------------------------
class GossipConfig:
    """An ``override_gossip_config.json`` in memory.

    Unknown keys are preserved verbatim through parse, mutation and render, so
    editing an operator's file never silently drops a field a later hl-node
    release added.

    >>> cfg = GossipConfig.new("Mainnet", ["192.0.2.10"]).set_try_new_peers(False)
    >>> print(cfg.to_json(), end="")
    {
      "chain": "Mainnet",
      "root_node_ips": [
        {
          "Ip": "192.0.2.10"
        }
      ],
      "try_new_peers": false
    }
    """

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        self._data: Dict[str, Any] = dict(data or {})
        self._pending: List[ConfigWarning] = []

    # -- constructors ------------------------------------------------------
    @classmethod
    def parse(cls, json_text: Union[str, bytes]) -> GossipConfig:
        """Decode. Unknown keys are preserved verbatim and nothing is validated
        — :meth:`validate` and :meth:`save` do that."""
        try:
            data = json.loads(json_text)
        except ValueError as exc:
            raise DecodeError(
                "override_gossip_config.json is not valid JSON: %s" % exc, cause=exc
            ) from exc
        if not isinstance(data, dict):
            raise Invalid("$", "expected a JSON object")
        return cls(data)

    @classmethod
    def load(cls, path: str) -> GossipConfig:
        """Read and :meth:`parse`.

        A missing file raises :class:`~hl_peers.errors.NotFound`: an empty
        config is the crash-loop shape, so this library will not invent one.
        Use :meth:`load_or_new` to opt in explicitly.
        """
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError as exc:
            raise NotFound(
                "no gossip config at %s (refusing to invent an empty one: an empty "
                "root_node_ips crash-loops the node)" % path,
                path=path,
            ) from exc
        return cls.parse(text)

    @classmethod
    def load_or_new(cls, path: str, chain: Optional[str] = None) -> GossipConfig:
        """:meth:`load`, or a fresh empty config when the file does not exist."""
        try:
            return cls.load(path)
        except NotFound:
            return cls.new(chain, [])

    @classmethod
    def new(
        cls,
        chain: Optional[str] = None,
        root_ips: Optional[Sequence[PeerLike]] = None,
    ) -> GossipConfig:
        """Build from scratch. ``root_ips`` accepts bare IPv4 strings **or**
        ``{"Ip": …}`` entries and always stores the object shape."""
        config = cls()
        if chain is not None:
            config.set_chain(chain)
        config._data["root_node_ips"] = [
            {"Ip": _require_ipv4("root_node_ips", _peer_ip(ip))} for ip in (root_ips or [])
        ]
        return config

    @classmethod
    def from_peers(
        cls,
        peers: Sequence[PeerLike],
        *,
        chain: Optional[str] = None,
        try_new_peers: Optional[bool] = True,
        **opts: Any,
    ) -> GossipConfig:
        """Build a new config from an API result (or bare IPv4 strings).

        The order is the API's ranking; this library never re-sorts. ``chain``
        is **required** — never defaulted to ``Mainnet``, because a chain that
        disagrees with the node's ``--chain`` stops the node from starting.

        ``opts`` is the same bag :meth:`merge_peers` takes, so a caller — or
        :func:`hl_peers.update_gossip_config`, which passes one options bag
        to both — may hand it ``mode``. There is nothing to append to on a
        fresh config, so all three modes coincide here; the option is accepted
        rather than rejected so the two entry points stay interchangeable.
        """
        if chain is None:
            raise Invalid(
                "chain",
                "chain is required: a chain that disagrees with the node's --chain stops "
                "the node from starting, and this library will not guess",
            )
        config = cls.new(chain, [])
        config._data.pop("root_node_ips", None)
        opts.setdefault("mode", "replace")
        config.merge_peers(peers, **opts)  # warnings stay pending
        if try_new_peers is not None:
            config.set_try_new_peers(try_new_peers)
        return config

    # -- data --------------------------------------------------------------
    @property
    def data(self) -> Dict[str, Any]:
        """The underlying mapping, unknown keys and all."""
        return self._data

    @property
    def pending_warnings(self) -> List[ConfigWarning]:
        """Warnings raised by :meth:`merge_peers` / :meth:`from_peers` that
        :meth:`validate` and :meth:`save` will also report."""
        return list(self._pending)

    def clear_warnings(self) -> GossipConfig:
        """Drop the pending merge warnings. Nothing else clears them — in
        particular a successful ``save`` does not, so ``save(); validate()``
        reports the same set twice rather than a shrinking one."""
        self._pending = []
        return self

    def copy(self) -> GossipConfig:
        return GossipConfig(json.loads(json.dumps(self._data)))

    # -- root ips ----------------------------------------------------------
    def root_ips(self) -> List[str]:
        """Bare IPv4 strings, in file order. The ``{"Ip": …}`` wrapper is an
        encoding detail and does not leak into this accessor."""
        return [ip for ip in (_entry_ip(e) for e in self._raw_roots()) if ip is not None]

    def _raw_roots(self) -> List[Any]:
        roots = self._data.get("root_node_ips")
        return list(roots) if isinstance(roots, list) else []

    def has_root_ip(self, ip: str) -> bool:
        return ip in self.root_ips()

    def add_root_ip(self, ip: str) -> GossipConfig:
        """Append. A duplicate is a **no-op**, not an error.

        🪤 The mutators do **not** validate: `validate()` owns IP shape
        (CLIENT_SPEC §6.4) and reports it as ``root_node_ips[<n>]``, so an
        operator editing a config sees every bad entry at once with its index,
        not the first one with no index.
        """
        if not self.has_root_ip(ip):
            roots = self._raw_roots()
            roots.append({"Ip": ip})
            self._data["root_node_ips"] = roots
        return self

    def remove_root_ip(self, ip: str) -> GossipConfig:
        """Remove. An absent IP is a no-op — including on a config that has no
        ``root_node_ips`` key at all, which is left without one rather than
        gaining an empty list."""
        if "root_node_ips" not in self._data:
            return self
        self._data["root_node_ips"] = [e for e in self._raw_roots() if _entry_ip(e) != ip]
        return self

    def set_root_ips(self, ips: Sequence[PeerLike]) -> GossipConfig:
        """Replace wholesale, preserving the given order, de-duplicating
        first-wins."""
        out: List[Dict[str, str]] = []
        seen = set()
        for raw in ips:
            ip = str(_peer_ip(raw))
            if ip not in seen:
                seen.add(ip)
                out.append({"Ip": ip})
        self._data["root_node_ips"] = out
        return self

    # -- reserved peers: BARE STRINGS -------------------------------------
    def reserved_peer_ips(self) -> List[str]:
        """🪤 ``reserved_peer_ips`` entries are **bare strings** while
        ``root_node_ips`` entries are ``{"Ip": …}`` objects. Round-tripping one
        shape through the other silently produces a file the node will not
        accept."""
        raw = self._data.get("reserved_peer_ips")
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def add_reserved_peer_ip(self, ip: str) -> GossipConfig:
        current = self.reserved_peer_ips()
        if ip not in current:
            current.append(ip)
        self._data["reserved_peer_ips"] = current
        return self

    def remove_reserved_peer_ip(self, ip: str) -> GossipConfig:
        """Remove. An absent IP is a no-op, and a config with no
        ``reserved_peer_ips`` key does not gain an empty one."""
        if "reserved_peer_ips" not in self._data:
            return self
        self._data["reserved_peer_ips"] = [x for x in self.reserved_peer_ips() if x != ip]
        return self

    def set_reserved_peer_ips(self, ips: Sequence[str]) -> GossipConfig:
        out: List[str] = []
        for raw in ips:
            ip = str(_peer_ip(raw))
            if ip not in out:
                out.append(ip)
        self._data["reserved_peer_ips"] = out
        return self

    # -- remaining contract fields ----------------------------------------
    def chain(self) -> Optional[str]:
        value = self._data.get("chain")
        return str(value) if isinstance(value, str) else None

    def set_chain(self, chain: str) -> GossipConfig:
        """Spelling is exact: ``Mainnet`` or ``Testnet``.

        🪤 Like every mutator except :meth:`set_n_gossip_peers`, this one stores
        without checking; :meth:`validate` owns the spelling rule (CLIENT_SPEC
        §6.4(5)) so all four clients raise at the same point in a sequence.
        """
        self._data["chain"] = chain
        return self

    def try_new_peers(self) -> Optional[bool]:
        value = self._data.get("try_new_peers")
        return value if isinstance(value, bool) else None

    def set_try_new_peers(self, value: Optional[bool]) -> GossipConfig:
        """``None`` removes the key.

        🪤 hypothesis: whether ``try_new_peers: false`` restricts a MULTI-entry
        root list to exactly that list is explicitly **not established**. Only
        the single-root case is evidenced as pinning.
        """
        return self._set_or_remove("try_new_peers", value, bool)

    def n_gossip_peers(self) -> Optional[int]:
        value = self._data.get("n_gossip_peers")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def set_n_gossip_peers(self, value: Optional[int]) -> GossipConfig:
        """Refuses anything outside ``[8, 100]`` at **both** ends (D-C).

        The node clamps; this library refuses, because silently clamping would
        write a number the operator did not choose into a file they will read
        back later. ``None`` removes the key.
        """
        if value is None:
            self._data.pop("n_gossip_peers", None)
            return self
        if isinstance(value, bool) or not isinstance(value, int):
            raise Invalid("n_gossip_peers", "must be an integer, got %r" % (value,))
        if not 8 <= value <= 100:
            raise Invalid(
                "n_gossip_peers",
                "must be within [8, 100], got %d — refusing rather than silently clamping" % value,
            )
        self._data["n_gossip_peers"] = value
        return self

    def split_client_blocks(self) -> Optional[bool]:
        value = self._data.get("split_client_blocks")
        return value if isinstance(value, bool) else None

    def set_split_client_blocks(self, value: Optional[bool]) -> GossipConfig:
        """``True`` writes ``true``; ``False`` **removes the key** (the node's
        serializer omits it when false); ``None`` removes it too.

        An explicit ``false`` already present in a parsed file is preserved by a
        round trip — only this setter removes it.
        """
        if value:
            self._data["split_client_blocks"] = True
        else:
            self._data.pop("split_client_blocks", None)
        return self

    def unset_split_client_blocks(self) -> GossipConfig:
        self._data.pop("split_client_blocks", None)
        return self

    def _set_or_remove(self, key: str, value: Any, expected: type) -> GossipConfig:
        if value is None:
            self._data.pop(key, None)
            return self
        if not isinstance(value, expected):
            raise Invalid(key, "must be a %s, got %r" % (expected.__name__, value))
        self._data[key] = value
        return self

    # -- merge -------------------------------------------------------------
    def merge_peers(
        self,
        peers: Sequence[PeerLike],
        *,
        max_roots: int = 8,
        mode: str = "replace",
        dedupe: bool = True,
        keep_existing: Sequence[str] = (),
        exclude: Sequence[str] = (),
        chain: Optional[str] = None,
        try_new_peers: Optional[bool] = None,
    ) -> List[ConfigWarning]:
        """Fold ranked peers into this config's ``root_node_ips``.

        Mutates in place and **returns its warnings** (CLIENT_SPEC §6.5), which
        are also retained on the config as :attr:`pending_warnings` so a caller
        who ignored the return value still sees them from ``validate()``,
        ``save()`` and ``update_gossip_config()``.

        Deterministic, order-preserving and never re-ranked: the API already
        ranked them. ``max_roots`` defaults to **8**, not the upstream's ≤ 24
        (D-D): a short root list makes the node start faster, and a longer one
        does not improve peering.

        🪤 ``exclude`` beats ``keep_existing`` and the conflict is reported as
        an ``exclude_overrides_keep_existing`` warning.
        """
        if mode not in ("replace", "append", "prepend"):
            raise Invalid("mode", "%r is not one of replace, append, prepend" % (mode,))
        if max_roots < 1:
            raise Invalid("max_roots", "must be >= 1, got %r" % (max_roots,))

        warnings: List[ConfigWarning] = []
        # 🔴 An invalid candidate is an error, never a silent drop — dropping
        # would shrink the root list without telling the operator. It is
        # reported as `peers[<n>]` so the caller can find the offending entry
        # in what they passed, not in the config it never reached.
        candidates = [
            _require_ipv4("peers[%d]" % index, _peer_ip(peer)) for index, peer in enumerate(peers)
        ]
        pinned = [
            _require_ipv4("keep_existing[%d]" % index, ip) for index, ip in enumerate(keep_existing)
        ]
        existing = self.root_ips()

        if mode == "replace":
            merged = pinned + candidates
        elif mode == "append":
            merged = pinned + existing + candidates
        else:  # prepend
            merged = pinned + candidates + existing

        excluded = {_require_ipv4("exclude[%d]" % index, ip) for index, ip in enumerate(exclude)}
        clobbered = [ip for ip in pinned if ip in excluded]
        if clobbered:
            warnings.append(
                ConfigWarning(
                    "exclude_overrides_keep_existing",
                    "exclude wins over keep_existing for %s — an operator who asks for an "
                    "IP to be excluded means it" % ", ".join(clobbered),
                    detail=",".join(clobbered),
                )
            )
        merged = [ip for ip in merged if ip not in excluded]

        if dedupe:
            merged = _dedupe(merged)

        if len(merged) > max_roots:
            warnings.append(
                ConfigWarning(
                    "truncated",
                    "dropped %d candidate(s) at max_roots=%d"
                    % (len(merged) - max_roots, max_roots),
                    detail=str(len(merged) - max_roots),
                )
            )
            merged = merged[:max_roots]

        if not merged:
            raise Invalid(
                "root_node_ips",
                EMPTY_ROOTS_REASON + "; the filter matched no peers — widen it, or pass "
                "keep_existing",
            )

        # dedupe:false must not silently write a duplicate — validate says so.
        self._data["root_node_ips"] = [{"Ip": ip} for ip in merged]
        # 🔴 §6.5(9): a merge touches `root_node_ips` and nothing else.
        # `chain` and `try_new_peers` are accepted here so the options bag is
        # the same one `from_peers` and `update_gossip_config` take, but only
        # `from_peers` — which is building a config rather than folding into
        # one — applies them. Silently rewriting the chain of a file an operator
        # already has is exactly the startup failure this library refuses to
        # risk elsewhere.
        self._pending = warnings
        self.validate()  # refuses duplicates left behind by dedupe=False
        return list(warnings)

    # -- validate ----------------------------------------------------------
    def validate(self) -> List[ConfigWarning]:
        """Raise the **first** error, in the fixed order of CLIENT_SPEC §6.4, so
        all four languages report the same one. Returns the warnings."""
        data = self._data

        # 1. the single most important guard in the library
        if "root_node_ips" not in data:
            raise Invalid("root_node_ips", EMPTY_ROOTS_REASON)
        roots = data["root_node_ips"]
        if not isinstance(roots, list):
            raise Invalid("root_node_ips", 'expected an array of {"Ip": …} objects')
        if not roots:
            raise Invalid("root_node_ips", EMPTY_ROOTS_REASON)

        # 2. entry shape — capital `Ip`, single key, no port
        for index, entry in enumerate(roots):
            parameter = "root_node_ips[%d]" % index
            if not isinstance(entry, dict) or list(entry.keys()) != ["Ip"]:
                raise Invalid(
                    parameter,
                    'each entry must be an object with the single key "Ip" (capital I, '
                    "lowercase p); got %r" % (entry,),
                )

        # 3. IPv4 validity, roots then reserved
        for index, entry in enumerate(roots):
            value = entry["Ip"]
            if not is_ipv4(value):
                raise Invalid("root_node_ips[%d]" % index, _ip_reason(value))
        reserved = data.get("reserved_peer_ips")
        if reserved is not None:
            if not isinstance(reserved, list):
                raise Invalid("reserved_peer_ips", "expected an array of bare IPv4 strings")
            for index, value in enumerate(reserved):
                if not isinstance(value, str):
                    raise Invalid(
                        "reserved_peer_ips[%d]" % index,
                        'entries are bare IPv4 strings here, not {"Ip": …} objects; got %r'
                        % (value,),
                    )
                if not is_ipv4(value):
                    raise Invalid("reserved_peer_ips[%d]" % index, _ip_reason(value))

        # 4. duplicates, each list independently (an IP MAY appear in both)
        _refuse_duplicates("root_node_ips", self.root_ips())
        _refuse_duplicates("reserved_peer_ips", self.reserved_peer_ips())

        # 5. chain spelling
        chain = data.get("chain")
        if chain is not None and chain not in CHAINS:
            raise Invalid(
                "chain", "%r is not one of %s (case matters)" % (chain, ", ".join(CHAINS))
            )

        # 6. n_gossip_peers
        if "n_gossip_peers" in data:
            value = data["n_gossip_peers"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise Invalid("n_gossip_peers", "must be an integer, got %r" % (value,))
            if not 8 <= value <= 100:
                raise Invalid("n_gossip_peers", "must be within [8, 100], got %d" % value)

        # 7. booleans
        for key in ("try_new_peers", "split_client_blocks"):
            if key in data and not isinstance(data[key], bool):
                raise Invalid(key, "must be a boolean, got %r" % (data[key],))

        return _dedupe_warnings(self._pending + self._warnings(roots))

    def _warnings(self, roots: Sequence[Any]) -> List[ConfigWarning]:
        out: List[ConfigWarning] = []
        if len(roots) > 64:
            out.append(
                ConfigWarning(
                    "many_roots_gt_64",
                    "%d roots: %s" % (len(roots), _MANY_ROOTS_MESSAGE),
                    detail=str(len(roots)),
                )
            )
        elif len(roots) > 24:
            out.append(
                ConfigWarning(
                    "many_roots_gt_24",
                    "%d roots: %s" % (len(roots), _MANY_ROOTS_MESSAGE),
                    detail=str(len(roots)),
                )
            )
        if "chain" not in self._data:
            out.append(
                ConfigWarning(
                    "chain_absent",
                    "emit `chain` matching the node's `--chain`; a mismatch stops the node "
                    "from starting, and this library cannot tell which chain your node runs",
                )
            )
        if self._data.get("try_new_peers") is False and len(roots) > 1:
            out.append(
                ConfigWarning(
                    "try_new_peers_false_multi_root",
                    "whether `try_new_peers: false` restricts a multi-entry list to exactly "
                    "that list is unverified; only a single root is known to pin",
                )
            )
        return out

    # -- rendering ---------------------------------------------------------
    def to_json(self) -> str:
        """Serialize per CLIENT_SPEC §6.3. Does **not** validate — ``save`` does."""
        return render_config(self._data)

    def to_json_validated(self) -> str:
        self.validate()
        return self.to_json()

    def __str__(self) -> str:
        return self.to_json()

    def __repr__(self) -> str:
        return "GossipConfig(roots=%r, chain=%r)" % (self.root_ips(), self.chain())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GossipConfig) and other._data == self._data

    def __hash__(self) -> int:  # pragma: no cover - configs are mutable
        raise TypeError("GossipConfig is mutable and not hashable")

    # -- save --------------------------------------------------------------
    def save(
        self,
        path: str,
        *,
        dry_run: bool = False,
        backup: bool = False,
        file_mode: Optional[int] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> SaveResult:
        """Validate, render, and write **atomically** — or report that nothing
        needed writing.

        🔴 ``save`` never restarts, signals or reloads anything. The node reads
        ``override_gossip_config.json`` when it starts: the
        change takes effect at the node's next start, which is yours to
        schedule.

        The temp file is created in the **same directory** as the target
        (a rename across filesystems is not atomic), fsynced, then renamed over
        it; the directory is fsynced afterwards. An existing file's mode is
        preserved and never widened; a new file is ``0600`` unless
        ``file_mode`` says otherwise.

        🪤 The permission option is ``file_mode``, never ``mode`` — ``mode`` is
        the merge strategy of :meth:`merge_peers` and nothing else.

        🪤 A symlinked ``path`` is **followed** and its target replaced
        (§6.6): renaming over the link would swap an operator's deliberate
        symlink for a regular file and split the node's view of the config from
        their tooling's. ``result.path`` stays the path you passed;
        ``result.resolved_path`` is where the bytes landed.
        """
        warnings = self.validate()
        rendered = self.to_json()

        # 🔴 Step 0: follow a symlinked path to its target, so the atomic
        # rename replaces the file the operator pointed at rather than the link.
        target = _resolve_symlink(path)

        existing: Optional[str] = None
        if os.path.exists(target):
            with open(target, encoding="utf-8") as handle:
                existing = handle.read()

        if existing == rendered:
            return SaveResult(False, False, path, "", None, warnings, target)

        diff = _unified_diff(existing or "", rendered, path)
        if dry_run:
            return SaveResult(False, True, path, diff, None, warnings, target)

        backup_path: Optional[str] = None
        directory = os.path.dirname(os.path.abspath(target)) or "."
        if backup and existing is not None:
            stamp = (now or _utcnow)().strftime("%Y%m%dT%H%M%SZ")
            backup_path = "%s.%s.bak" % (target, stamp)
            with open(backup_path, "w", encoding="utf-8") as handle:
                handle.write(existing)
            os.chmod(backup_path, _mode_of(target, file_mode))

        target_mode = _mode_of(target, file_mode)
        tmp_name = ".%s.%06x.tmp" % (os.path.basename(target), random.getrandbits(24))
        tmp_path = os.path.join(directory, tmp_name)
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, target_mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, target_mode)
            os.replace(tmp_path, target)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        _fsync_dir(directory)
        return SaveResult(True, True, path, diff, backup_path, warnings, target)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _entry_ip(entry: Any) -> Optional[str]:
    if isinstance(entry, dict) and "Ip" in entry:
        value = entry["Ip"]
        return str(value) if isinstance(value, str) else None
    if isinstance(entry, str):
        return entry
    return None


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _dedupe_warnings(warnings: Sequence[ConfigWarning]) -> List[ConfigWarning]:
    """A code never appears twice; order is not significant to a comparison."""
    seen = set()
    out: List[ConfigWarning] = []
    for warning in warnings:
        if warning.code not in seen:
            seen.add(warning.code)
            out.append(warning)
    return out


def _refuse_duplicates(parameter: str, values: Sequence[str]) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise Invalid(
                parameter,
                "duplicate %s (a duplicate root is written as-is by dedupe=False; the two "
                "lists are validated independently)" % value,
            )
        seen.add(value)


def _unified_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=3,
        )
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mode_of(path: str, override: Optional[int]) -> int:
    if override is not None:
        return override
    try:
        return os.stat(path).st_mode & 0o7777
    except OSError:
        # This file names infrastructure you peer with: default-private.
        return 0o600


#: How many links `save` will follow before it refuses (CLIENT_SPEC §6.6).
SYMLINK_DEPTH = 32


def _resolve_symlink(path: str) -> str:
    """Follow a symlinked config path to the file it names.

    🔴 ``save`` replaces the **target**, never the link: renaming over a
    symlink swaps an operator's deliberate arrangement for a regular file, and
    from that moment the node and their tooling read two different files with
    nothing to announce it.
    """
    current = path
    for _ in range(SYMLINK_DEPTH):
        if not os.path.islink(current):
            if current != path and not os.path.isdir(
                os.path.dirname(os.path.abspath(current)) or "."
            ):
                raise Invalid(
                    "path",
                    "%s is a symbolic link to %s, whose directory does not exist; refusing "
                    "to create one rather than guess at the operator's layout" % (path, current),
                )
            return current
        target = os.readlink(current)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        current = target
    raise Invalid(
        "path",
        "%s resolves through more than %d symbolic links; refusing to follow further"
        % (path, SYMLINK_DEPTH),
    )


def _fsync_dir(directory: str) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)
