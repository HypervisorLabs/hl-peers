"""The conditional-request cache.

On by default, per client instance, never global and never on disk. It exists to
make **polling free** — a ``304`` costs no quota unit — not to serve stale
bytes: ``Cache-Control: max-age`` is not honoured as a hard TTL and the client
always revalidates.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["CacheEntry", "EtagCache"]


@dataclass(frozen=True)
class CacheEntry:
    etag: str
    body: Any
    stored_at: float


class EtagCache:
    """A small in-memory LRU of ``{etag, body}`` keyed on the canonical request.

    The key includes **whether an API key was sent**, because the tier changes
    the page ceiling and therefore the body.
    """

    def __init__(self, capacity: int = 64) -> None:
        self.capacity = capacity
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()

    @staticmethod
    def key(method: str, path: str, query_string: str, authenticated: bool) -> str:
        return "\0".join((method, path, query_string, "k" if authenticated else "a"))

    def get(self, key: str) -> Optional[CacheEntry]:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
