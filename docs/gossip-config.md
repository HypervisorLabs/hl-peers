# `override_gossip_config.json`

The file these libraries generate, and everything about it that will bite you.

Normative schema: [`../spec/gossip-config.schema.json`](../spec/gossip-config.schema.json).
Normative client behaviour: [`../CLIENT_SPEC.md`](../CLIENT_SPEC.md) §6.

Where the node's behaviour is unverified, this page says so and the libraries treat
it as unknown — which usually means refusing rather than guessing, because the
failure mode on this path is a crash loop.

---

## 🔴 The four rules

### 1. An empty `root_node_ips` takes the node down

The node cannot start without roots, and there is **no built-in fallback**: an
empty or missing list is a crash loop, not a warning, not a degraded mode.

So these libraries **refuse to serialize or save an empty list**, in `validate()`,
in `save()`, in `merge_peers()` and in `from_peers()`. In particular, when your
filter matches no peers, `update_gossip_config` raises — it does not write an empty
file, and it does not silently fall back to your previous roots. Widen the filter,
or pass `keep_existing` with IPs you want pinned regardless.

This is the single most important guard in the project, and four separate
conformance cases pin it.

### 2. The file is read when the node starts

Rewriting the file does nothing to a running node; the change takes effect at the
node's next start.

- These libraries will never restart, signal, or otherwise disturb your node.
  There is no `--restart` flag and no `apply()` that does one.
- Restarting a node **to apply a config** while it is behind can make a lagging
  box worse.

`update_gossip_config` returning successfully has not peered your node. It wrote a
file. When to restart is yours to schedule.

### 3. Two entry shapes in one file

```json
{
  "root_node_ips": [ { "Ip": "192.0.2.10" } ],
  "reserved_peer_ips": [ "198.51.100.20" ]
}
```

`root_node_ips` entries are **objects with a single key `Ip`** — capital `I`,
lowercase `p`. Not a bare string, not `ip`, and **no port field**.
`reserved_peer_ips` entries are **bare strings**.

A library that round-trips one shape through the other silently produces a file the
node will not accept. Both shapes, and the rejection of each in the other's place,
are pinned by fixtures.

The libraries accept bare IPv4 strings as constructor **input** for convenience and
always **store and emit** the object shape. A bare string found inside a *parsed*
file is rejected.

### 4. Keep the root list short — `max_roots` defaults to 8

A shorter root list starts the node faster, and a longer one buys nothing. Upstream
guidance is "≤ 24, warn above ~64"; 8 sits inside that. Pass `max_roots: 24` if you
want the upstream default; the libraries warn above 24 and escalate above 64.

`root_node_ips = [one peer]` with `try_new_peers: false` pins the node to that
peer. 🪤 **Unverified:** whether `try_new_peers: false` with a *multi*-entry list
restricts the node to exactly that list. The libraries emit a
`try_new_peers_false_multi_root` warning rather than let you assume it does.

---

## Where the file lives

`$HOME/override_gossip_config.json`, in the node user's home. There is **no**
command-line flag or environment variable that selects it.

🪤 **Unverified:** whether the node resolves the file against `$HOME` or against its
working directory; the two coincide on a standard install. The libraries default to
`$HOME/override_gossip_config.json`, let you override it, and **never** inspect a
running process, systemd unit or `/proc` to guess a path.

Emitting to stdout and letting you place the file is the safest option of all, and
it is a first-class path: `--stdout`, or just use the returned `json` and never
pass a `path`.

Mainnet and Testnet use the **same file, same path, same schema**. Only the `chain`
value differs.

---

## The schema — six fields

| key | type | meaning |
|---|---|---|
| `root_node_ips` | array of `{"Ip": "<ipv4>"}` | the peers your node starts from. 🔴 must be non-empty |
| `try_new_peers` | bool | allow peers beyond the roots |
| `chain` | `"Mainnet"` \| `"Testnet"` | must match the node's own `--chain` |
| `reserved_peer_ips` | array of bare strings | peers your node always admits |
| `n_gossip_peers` | integer, `[8, 100]` | how many inbound peers your node accepts. Default 8 |
| `split_client_blocks` | bool | optional; `true` makes the node request the transaction (mempool) stream from its peers. Omitted when false |

All keys except `root_node_ips` are optional in practice.

**Mempool configs.** `/peers?serves_mempool=true&format=gossip_config` emits
`"split_client_blocks": true` and `"try_new_peers": false` by default, so the node
requests the transaction stream and stays on the listed peers. Override either
with the `split_client_blocks` / `try_new_peers` query parameters; with
`split_client_blocks=false` the key is left out of the file.

🪤 `n_gossip_peers` lives in **this** file, not a separate one. That is the "accept
more inbound peers" knob.

An absent `split_client_blocks` means `false`, so `set_split_client_blocks(false)`
removes the key. An explicit `false` already present in a file you loaded is
**preserved** — round-tripping must not rewrite your file.

🪤 **Unknown keys are preserved.** The libraries never drop a key they do not
recognise; a future node field you added by hand survives a `load` → `merge` →
`save` untouched, and is re-emitted after the known keys, sorted.

### Chain

Exactly `"Mainnet"` and `"Testnet"`, with that capitalization. A `chain` that
disagrees with the node's `--chain` stops the node from starting.

The libraries **never default** the chain. `from_peers` without one raises, because
guessing `Mainnet` is precisely how a Testnet operator gets a crash loop.

### IPv4 only

The peer API is IPv4-only. Whether the node would accept an IPv6 literal in this
file is **unverified**. The libraries **reject** IPv6 with an explicit message
rather than write an untested value into a file whose failure mode is a crash loop.

---

## Byte-exact output

All four languages emit **identical bytes** for identical input, so a config under
version control produces clean diffs no matter who regenerated it:

- fixed key order — `chain`, `root_node_ips`, `try_new_peers`,
  `reserved_peer_ips`, `n_gossip_peers`, `split_client_blocks`, then unknown keys
  sorted by code point;
- 2-space indent, `": "` between key and value, empty arrays inline as `[]`;
- literal UTF-8, never `\u`-escaped;
- exactly one trailing `\n`, never `\r\n`.

`save()` is **atomic and idempotent**: validate → render → compare with what is on
disk → if identical, *do nothing at all* (no write, no `.bak`, no mtime change) →
otherwise write a temp file in the same directory, `fsync`, rename over the target,
preserving the existing file mode (`0600` for a new one). `dry_run` returns a
unified diff and writes nothing.

---

## A worked example

```bash
# what would change, without touching anything
hl-peers gossip update --path ~/override_gossip_config.json \
    --cc DE --tier 1 --accepting --max-roots 8 --dry-run

# generate to stdout and place it yourself — the safest option
hl-peers gossip update --cc DE --max-roots 8 --stdout

# write it, keeping two peers you have pinned by hand
hl-peers gossip update --path ~/override_gossip_config.json \
    --cc DE --max-roots 8 \
    --keep 192.0.2.10,192.0.2.11 --backup
```

🪤 One last trap, and it is the reason this API exists. The info API's
`gossipRootIps` is a small directory of **foundation sentries**, the hardest tier to
get a slot with. `--tier 1 --accepting` is usually a much better seed set than the
published root list.
