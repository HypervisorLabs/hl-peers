/**
 * `GossipConfig` — pure, offline, deterministic file algebra over
 * `override_gossip_config.json` (CLIENT_SPEC.md §6).
 *
 * **No network, no clock, no environment lookups, no restart of anything.**
 * Given the same input it produces the same bytes on every platform, in every
 * language, forever. Nothing in this module imports the HTTP layer: an operator
 * must be able to edit a config on a box with no API access at all.
 *
 * ---
 *
 * 🔴 **The node reads `override_gossip_config.json` when it
 * starts.** Writing this file does **not** steer a running node: the change
 * takes effect at the node's next start. This library will **never** restart,
 * signal, or otherwise disturb your node — that is the operator's decision, at
 * the operator's moment. Restarting a node *to apply a config* while it is
 * behind can make a lagging box worse.
 *
 * The node's file is `$HOME/override_gossip_config.json`.
 *
 * @module
 */
import { DecodeError, InvalidError } from './errors.ts';
import { isIpv4, notIpv4Reason } from './ipv4.ts';
import { renderGossipConfig } from './serialize.ts';
import type { Chain, PeerLike } from './types.ts';

/** The stable warning identifiers of CLIENT_SPEC §6.4. */
export type WarningCode =
  | 'many_roots_gt_24'
  | 'many_roots_gt_64'
  | 'chain_absent'
  | 'try_new_peers_false_multi_root'
  | 'truncated'
  | 'exclude_overrides_keep_existing';

/**
 * A warning. The `code` is stable and asserted by the conformance suite; the
 * `message` is prose and is not. Warnings are never fatal and never swallowed.
 */
export interface Warning {
  readonly code: WarningCode;
  readonly message: string;
  readonly detail?: string;
}

/** What `validate()` returns when it does not throw. */
export interface ValidationResult {
  readonly ok: true;
  readonly warnings: readonly Warning[];
}

/** What `mergePeers` / `fromPeers` return. */
export interface MergeResult {
  readonly config: GossipConfig;
  readonly warnings: readonly Warning[];
}

/** How new roots combine with the roots already in the file. */
export type MergeMode = 'replace' | 'append' | 'prepend';

export interface MergeOptions {
  /**
   * Hard cap on the resulting `root_node_ips`. **Defaults to 8**, not the
   * upstream contract's ≤ 24 (CLIENT_SPEC §10, D-D): a short root list makes
   * the node start faster, and a longer one does not improve peering. Pass
   * `maxRoots: 24` for the upstream default.
   */
  maxRoots?: number;
  mode?: MergeMode;
  /** Drop repeats, first occurrence wins. Default `true`. */
  dedupe?: boolean;
  /** IPs the operator pins: always retained and placed first. */
  keepExisting?: readonly string[];
  /** IPs never to include. Applied last, and 🪤 it beats `keepExisting`. */
  exclude?: readonly string[];
  /** `fromPeers` only: required, and never defaulted to `Mainnet`. */
  chain?: Chain;
  /** `fromPeers` only: defaults to `true`. */
  tryNewPeers?: boolean;
}

/** A peer, a bare IPv4 string, or anything else carrying an `id`. */
export type PeerInput = string | PeerLike;

const EMPTY_ROOTS_REASON =
  'refusing to emit an empty root_node_ips: hl-node asserts on a non-empty list at ' +
  'start and will crash-loop; there is no built-in fallback';

const MERGE_EMPTY_REASON =
  EMPTY_ROOTS_REASON + ' — the filter matched no peers: widen it, or pass keepExisting';

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function candidateIp(p: PeerInput, index: number): string {
  if (typeof p === 'string') return p;
  if (isPlainObject(p) && typeof p['id'] === 'string') return p['id'];
  throw new InvalidError(
    `peers[${index}]`,
    'expected an IPv4 string or a peer object carrying an `id`',
  );
}

/**
 * A parsed or constructed `override_gossip_config.json`.
 *
 * Unknown keys are preserved verbatim through parse, mutation and round trip,
 * and are re-emitted after every known key, sorted by code point.
 */
export class GossipConfig {
  /**
   * The decoded document. Mutating it directly is allowed but unchecked;
   * prefer the accessors, which keep the two entry shapes straight.
   */
  readonly data: Record<string, unknown>;

  /**
   * Warnings raised by the last {@link GossipConfig.mergePeers} /
   * {@link GossipConfig.fromPeers} (CLIENT_SPEC §6.5). They arise during the
   * merge rather than from the resulting state, so they are retained here and
   * re-reported by `validate()`, `save()` and `updateGossipConfig()` — a
   * warning raised during a merge cannot be lost by a caller who ignored what
   * the merge returned. The next merge **replaces** them; nothing else drops
   * them, and in particular a successful `save` does **not**.
   */
  private pending: readonly Warning[] = [];

  constructor(data: Record<string, unknown> = {}) {
    this.data = data;
  }

  // -- constructors ------------------------------------------------------

  /**
   * Decode config text. Unknown keys are preserved verbatim and **nothing is
   * validated** — `validate()` and `save()` do that. Invalid JSON raises
   * `Decode`; a non-object top level raises `Invalid{parameter: "$"}`.
   */
  static parse(jsonText: string): GossipConfig {
    let decoded: unknown;
    try {
      decoded = JSON.parse(jsonText) as unknown;
    } catch (cause) {
      throw new DecodeError(
        `override_gossip_config.json is not valid JSON: ${(cause as Error).message}`,
        jsonText.slice(0, 512),
        { cause },
      );
    }
    if (!isPlainObject(decoded)) {
      throw new InvalidError('$', 'expected a JSON object');
    }
    return new GossipConfig(decoded);
  }

  /**
   * Build from scratch. `rootIps` accepts bare IPv4 strings **or**
   * `{"Ip": …}` entries and always stores the object shape.
   */
  static new(chain: Chain, rootIps: readonly (string | { Ip: string })[]): GossipConfig {
    const cfg = new GossipConfig({});
    cfg.data['chain'] = chain;
    cfg.setRootIps(rootIps.map((e) => (typeof e === 'string' ? e : e.Ip)));
    return cfg;
  }

  /** Alias for {@link GossipConfig.new}, for callers who dislike `.new(`. */
  static create(chain: Chain, rootIps: readonly (string | { Ip: string })[]): GossipConfig {
    return GossipConfig.new(chain, rootIps);
  }

  /**
   * Build a fresh config from an API result (or bare IPv4 strings).
   *
   * 🔴 `opts.chain` is **required**: a `chain` that disagrees with the node's
   * `--chain` stops the node from starting, and guessing `Mainnet` is how that
   * happens.
   */
  static fromPeers(peers: readonly PeerInput[], opts: MergeOptions = {}): MergeResult {
    if (opts.chain === undefined) {
      throw new InvalidError(
        'chain',
        'fromPeers needs an explicit chain: this library never defaults it, because a ' +
          "chain that disagrees with the node's --chain stops the node from starting",
      );
    }
    const cfg = new GossipConfig({ chain: opts.chain });
    const merged = cfg.mergePeers(peers, { ...opts, mode: opts.mode ?? 'replace' });
    cfg.data['try_new_peers'] = opts.tryNewPeers ?? true;
    return merged;
  }

  /** A deep-ish copy, so a caller can branch without disturbing the original. */
  clone(): GossipConfig {
    const copy = new GossipConfig(structuredClone(this.data));
    copy.pending = [...this.pending];
    return copy;
  }

  /** The pending merge warnings (§6.5). */
  pendingWarnings(): readonly Warning[] {
    return this.pending;
  }

  /** Drop the pending merge warnings. The only thing that clears them. */
  clearWarnings(): this {
    this.pending = [];
    return this;
  }

  // -- serialization -----------------------------------------------------

  /**
   * The normative bytes (§6.3). Does **not** validate — `save()` does.
   * 🔴 The result already ends in exactly one `\n`; do not add another.
   */
  toJson(): string {
    return renderGossipConfig(this.data);
  }

  /** `toJson()`, but refusing to render a config `validate()` rejects. */
  toJsonValidated(): string {
    this.validate();
    return this.toJson();
  }

  /** The decoded document, as a copy. */
  toObject(): Record<string, unknown> {
    return structuredClone(this.data);
  }

  // -- roots -------------------------------------------------------------

  /**
   * The root IPs as **bare strings**, in file order. The `{"Ip": …}` wrapper is
   * an encoding detail and does not leak into this accessor.
   */
  rootIps(): string[] {
    const raw = this.data['root_node_ips'];
    if (!Array.isArray(raw)) return [];
    const out: string[] = [];
    for (const entry of raw) {
      if (typeof entry === 'string') out.push(entry);
      else if (isPlainObject(entry) && typeof entry['Ip'] === 'string') out.push(entry['Ip']);
    }
    return out;
  }

  hasRootIp(ip: string): boolean {
    return this.rootIps().includes(ip);
  }

  /** Appends. A duplicate is a **no-op**, not an error. */
  addRootIp(ip: string): this {
    if (this.hasRootIp(ip)) return this;
    const raw = this.data['root_node_ips'];
    const list = Array.isArray(raw) ? (raw as unknown[]) : [];
    list.push({ Ip: ip });
    this.data['root_node_ips'] = list;
    return this;
  }

  /** Removes by bare string. An absent IP is a no-op. */
  removeRootIp(ip: string): this {
    const raw = this.data['root_node_ips'];
    if (!Array.isArray(raw)) return this;
    this.data['root_node_ips'] = (raw as unknown[]).filter((entry) => {
      if (typeof entry === 'string') return entry !== ip;
      if (isPlainObject(entry) && typeof entry['Ip'] === 'string') return entry['Ip'] !== ip;
      return true;
    });
    return this;
  }

  /**
   * Replaces wholesale, preserving the given order and de-duplicating
   * first-wins. Always stores the `{"Ip": …}` shape.
   */
  setRootIps(ips: readonly (string | { Ip: string })[]): this {
    const seen = new Set<string>();
    const out: { Ip: string }[] = [];
    for (const entry of ips) {
      const ip = typeof entry === 'string' ? entry : entry.Ip;
      if (seen.has(ip)) continue;
      seen.add(ip);
      out.push({ Ip: ip });
    }
    this.data['root_node_ips'] = out;
    return this;
  }

  // -- reserved peers: 🪤 BARE STRINGS -----------------------------------

  /**
   * Inbound capacity exemption list. 🪤 Entries are **bare strings** here,
   * unlike `root_node_ips`, whose entries are `{"Ip": …}` objects. Round-
   * tripping one shape through the other silently produces a file the node
   * will not accept.
   */
  reservedPeerIps(): string[] {
    const raw = this.data['reserved_peer_ips'];
    if (!Array.isArray(raw)) return [];
    return (raw as unknown[]).filter((e): e is string => typeof e === 'string');
  }

  addReservedPeerIp(ip: string): this {
    const raw = this.data['reserved_peer_ips'];
    const list = Array.isArray(raw) ? (raw as unknown[]) : [];
    if (!list.includes(ip)) list.push(ip);
    this.data['reserved_peer_ips'] = list;
    return this;
  }

  removeReservedPeerIp(ip: string): this {
    const raw = this.data['reserved_peer_ips'];
    if (!Array.isArray(raw)) return this;
    this.data['reserved_peer_ips'] = (raw as unknown[]).filter((e) => e !== ip);
    return this;
  }

  setReservedPeerIps(ips: readonly string[] | null): this {
    if (ips === null) {
      delete this.data['reserved_peer_ips'];
      return this;
    }
    const seen = new Set<string>();
    const out: string[] = [];
    for (const ip of ips) {
      if (seen.has(ip)) continue;
      seen.add(ip);
      out.push(ip);
    }
    this.data['reserved_peer_ips'] = out;
    return this;
  }

  // -- the remaining contract fields --------------------------------------

  chain(): Chain | null {
    const v = this.data['chain'];
    return typeof v === 'string' ? (v as Chain) : null;
  }

  /** Exact spelling: `Mainnet` or `Testnet`. Case matters. */
  setChain(chain: Chain): this {
    this.data['chain'] = chain;
    return this;
  }

  tryNewPeers(): boolean | null {
    const v = this.data['try_new_peers'];
    return typeof v === 'boolean' ? v : null;
  }

  /** `null` removes the key. */
  setTryNewPeers(value: boolean | null): this {
    if (value === null) delete this.data['try_new_peers'];
    else this.data['try_new_peers'] = value;
    return this;
  }

  nGossipPeers(): number | null {
    const v = this.data['n_gossip_peers'];
    return typeof v === 'number' ? v : null;
  }

  /**
   * Caps **accepted inbound** peers. 🔴 Refuses anything outside `[8, 100]`
   * rather than clamping (CLIENT_SPEC §10, D-C): silently clamping would write
   * a number the operator did not choose into a file they will read back later.
   * `null` removes the key.
   */
  setNGossipPeers(value: number | null): this {
    if (value === null) {
      delete this.data['n_gossip_peers'];
      return this;
    }
    if (!Number.isInteger(value) || value < 8 || value > 100) {
      throw new InvalidError(
        'n_gossip_peers',
        `${String(value)} is outside the valid range [8, 100]; refusing rather than clamping`,
      );
    }
    this.data['n_gossip_peers'] = value;
    return this;
  }

  splitClientBlocks(): boolean | null {
    const v = this.data['split_client_blocks'];
    return typeof v === 'boolean' ? v : null;
  }

  /**
   * `true` writes the key; **`false` removes it**, because the node's own
   * serializer omits it when false. An explicit `false` already present in a
   * parsed file is preserved by a round trip — only this setter removes it.
   */
  setSplitClientBlocks(value: boolean | null): this {
    if (value === true) this.data['split_client_blocks'] = true;
    else delete this.data['split_client_blocks'];
    return this;
  }

  /** Removes the key in either state. */
  unsetSplitClientBlocks(): this {
    delete this.data['split_client_blocks'];
    return this;
  }

  // -- validation ---------------------------------------------------------

  /**
   * Check the document. Throws the **first** error in the order of
   * CLIENT_SPEC §6.4 (deterministic, so all four languages report the same
   * one), or returns `{ok: true, warnings}`.
   */
  validate(): ValidationResult {
    const roots = this.data['root_node_ips'];

    // (1) 🔴 the single most important guard in the library
    if (roots === undefined || roots === null) {
      throw new InvalidError('root_node_ips', EMPTY_ROOTS_REASON);
    }
    if (!Array.isArray(roots)) {
      throw new InvalidError(
        'root_node_ips',
        'expected an array of {"Ip": "<ipv4>"} entries, got ' + typeof roots,
      );
    }
    if (roots.length === 0) {
      throw new InvalidError('root_node_ips', EMPTY_ROOTS_REASON);
    }

    // (2) entry shapes — 🪤 the capitalisation of `Ip` is load-bearing, and
    //     reserved_peer_ips entries are bare strings instead.
    roots.forEach((entry, i) => {
      if (!isPlainObject(entry)) {
        throw new InvalidError(
          `root_node_ips[${i}]`,
          'expected an object with the single key `Ip` (capital I, lowercase p); a bare ' +
            'string is accepted as constructor input only, never inside a parsed file',
        );
      }
      const keys = Object.keys(entry);
      if (keys.length !== 1 || keys[0] !== 'Ip') {
        throw new InvalidError(
          `root_node_ips[${i}]`,
          'expected exactly the single key `Ip` (capital I, lowercase p) and no other — ' +
            `got {${keys.join(', ')}}; the entry carries no port field`,
        );
      }
      if (typeof entry['Ip'] !== 'string') {
        throw new InvalidError(`root_node_ips[${i}]`, '`Ip` must be a dotted-quad IPv4 string');
      }
    });

    const reserved = this.data['reserved_peer_ips'];
    if (reserved !== undefined && reserved !== null) {
      if (!Array.isArray(reserved)) {
        throw new InvalidError(
          'reserved_peer_ips',
          'expected an array of bare IPv4 strings, got ' + typeof reserved,
        );
      }
      reserved.forEach((entry, i) => {
        if (typeof entry !== 'string') {
          throw new InvalidError(
            `reserved_peer_ips[${i}]`,
            'expected a bare IPv4 string; the {"Ip": …} wrapper belongs to root_node_ips ' +
              'only, and the two shapes are not interchangeable',
          );
        }
      });
    }

    // (3) IPv4 validity, IPv6 refused explicitly
    roots.forEach((entry, i) => {
      const ip = (entry as { Ip: string }).Ip;
      if (!isIpv4(ip)) throw new InvalidError(`root_node_ips[${i}]`, notIpv4Reason(ip));
    });
    this.reservedPeerIps().forEach((ip, i) => {
      if (!isIpv4(ip)) throw new InvalidError(`reserved_peer_ips[${i}]`, notIpv4Reason(ip));
    });

    // (4) duplicates, per list; an IP MAY appear in both
    firstDuplicate(this.rootIps(), 'root_node_ips');
    firstDuplicate(this.reservedPeerIps(), 'reserved_peer_ips');

    // (5) chain spelling
    const chain = this.data['chain'];
    if (chain !== undefined && chain !== null && chain !== 'Mainnet' && chain !== 'Testnet') {
      throw new InvalidError(
        'chain',
        `expected exactly "Mainnet" or "Testnet" (case matters), got ${JSON.stringify(chain)}`,
      );
    }

    // (6) n_gossip_peers
    const n = this.data['n_gossip_peers'];
    if (n !== undefined && n !== null) {
      if (typeof n !== 'number' || !Number.isInteger(n) || n < 8 || n > 100) {
        throw new InvalidError(
          'n_gossip_peers',
          `expected an integer in [8, 100], got ${JSON.stringify(n)}`,
        );
      }
    }

    // (7) booleans
    for (const key of ['try_new_peers', 'split_client_blocks'] as const) {
      const v = this.data[key];
      if (v !== undefined && v !== null && typeof v !== 'boolean') {
        throw new InvalidError(key, `expected a boolean, got ${JSON.stringify(v)}`);
      }
    }

    return { ok: true, warnings: this.warnings() };
  }

  /**
   * The non-fatal warnings: §6.4's state-derived ones unioned with the pending
   * merge warnings of §6.5, **de-duplicated by code**.
   *
   * 🔴 The codes are **independent**. Every condition that holds fires; a
   * `many_roots_*` warning does not suppress, absorb or outrank
   * `try_new_peers_false_multi_root`. The one exception is the pair
   * `many_roots_gt_24` / `many_roots_gt_64`, which are mutually exclusive.
   */
  warnings(): Warning[] {
    const out: Warning[] = [];
    const roots = this.rootIps();

    if (roots.length > 64) {
      out.push({
        code: 'many_roots_gt_64',
        message:
          `${roots.length} roots: a long root list makes the node slower to start and buys ` +
          'nothing; prefer a short list. This is far past the upstream ceiling of 24.',
      });
    } else if (roots.length > 24) {
      out.push({
        code: 'many_roots_gt_24',
        message:
          `${roots.length} roots: a long root list makes the node slower to start and buys ` +
          'nothing; prefer a short list',
      });
    }

    if (this.data['chain'] === undefined) {
      out.push({
        code: 'chain_absent',
        message:
          'emit `chain` matching the node’s `--chain`; a mismatch stops the node from ' +
          'starting, and this library cannot tell which chain your node runs',
      });
    }

    // Independent of the roots-length warnings: a long list being slow to boot
    // and a list that may or may not pin are two unrelated facts, and hiding
    // one behind the other would hide the harder problem.
    if (this.data['try_new_peers'] === false && roots.length > 1) {
      out.push({
        code: 'try_new_peers_false_multi_root',
        message:
          'whether `try_new_peers: false` restricts a multi-entry list to exactly that list ' +
          'is unverified; only a single root is known to pin',
      });
    }

    // §6.5: the pending merge warnings, unioned and de-duplicated by code.
    const seen = new Set(out.map((w) => w.code));
    for (const w of this.pending) {
      if (!seen.has(w.code)) {
        seen.add(w.code);
        out.push(w);
      }
    }
    return out;
  }

  // -- merging ------------------------------------------------------------

  /**
   * Fold API peers into this config's `root_node_ips`, deterministically and
   * without sorting: the API already ranked them.
   *
   * 🔴 If the result would be empty this raises rather than writing the
   * crash-loop shape, and it never falls back to the previous roots silently.
   */
  mergePeers(peers: readonly PeerInput[], opts: MergeOptions = {}): MergeResult {
    const maxRoots = opts.maxRoots ?? 8;
    const mode: MergeMode = opts.mode ?? 'replace';
    const dedupe = opts.dedupe ?? true;
    const exclude = opts.exclude ?? [];
    const warnings: Warning[] = [];

    if (!Number.isInteger(maxRoots) || maxRoots < 1) {
      throw new InvalidError('max_roots', `expected an integer >= 1, got ${String(maxRoots)}`);
    }

    // (1)+(2) candidates in the order given; an invalid one is an error, never
    // a silent drop — dropping would shrink the root list without telling the
    // operator.
    const candidates = peers.map((p, i) => {
      const ip = candidateIp(p, i);
      if (!isIpv4(ip)) throw new InvalidError(`peers[${i}]`, notIpv4Reason(ip));
      return ip;
    });

    // (4) keep_existing leads, filtered to valid IPv4
    const keep = (opts.keepExisting ?? []).filter((ip) => isIpv4(ip));
    const existing = this.rootIps();

    let merged: string[];
    switch (mode) {
      case 'replace':
        merged = [...keep, ...candidates];
        break;
      case 'append':
        merged = [...keep, ...existing, ...candidates];
        break;
      case 'prepend':
        merged = [...keep, ...candidates, ...existing];
        break;
      default:
        throw new InvalidError(
          'mode',
          `expected "replace", "append" or "prepend", got ${JSON.stringify(mode)}`,
        );
    }

    // (5) 🪤 exclude beats keep_existing, and the conflict is named.
    if (exclude.length > 0) {
      const excluded = new Set(exclude);
      const clashes = keep.filter((ip) => excluded.has(ip));
      if (clashes.length > 0) {
        warnings.push({
          code: 'exclude_overrides_keep_existing',
          message:
            `excluded despite being in keepExisting: ${clashes.join(', ')}. An operator who ` +
            'asks for an IP to be excluded means it.',
          detail: clashes.join(','),
        });
      }
      merged = merged.filter((ip) => !excluded.has(ip));
    }

    // (6) dedupe, first wins. With `dedupe: false` a surviving duplicate is
    // refused — but by the `validate()` at the end of this method, **after**
    // truncation (7), because a duplicate that truncation removes never reaches
    // the file and must not fail the merge.
    if (dedupe) {
      const seen = new Set<string>();
      merged = merged.filter((ip) => (seen.has(ip) ? false : (seen.add(ip), true)));
    }

    // (7) truncate to the head
    if (merged.length > maxRoots) {
      const dropped = merged.length - maxRoots;
      merged = merged.slice(0, maxRoots);
      warnings.push({
        code: 'truncated',
        message:
          `dropped ${dropped} candidate(s) at maxRoots=${maxRoots}; the head was kept in the ` +
          "API's ranking order",
        detail: String(dropped),
      });
    }

    // (8) 🔴 never write, never return, a zero-root config
    if (merged.length === 0) {
      throw new InvalidError('root_node_ips', MERGE_EMPTY_REASON);
    }

    // (9) every other field, and every unknown key, is left exactly as it was.
    // 🪤 Written directly, NOT through `setRootIps`, which de-duplicates: with
    // `dedupe: false` the surviving duplicate has to reach `validate()` so it
    // is refused rather than quietly dropped.
    this.data['root_node_ips'] = merged.map((ip) => ({ Ip: ip }));
    // §6.5: the next merge REPLACES the pending warnings; it never appends.
    this.pending = warnings;
    // 🔴 The merged result is validated here, not left for the caller: a merge
    // that produced a config hl-node would reject — a duplicate left by
    // `dedupe: false`, a bad `reserved_peer_ips` entry the file already
    // carried — must fail at the merge, where the caller can still act on it,
    // rather than at some later `save()`.
    this.validate();
    return { config: this, warnings };
  }
}

function firstDuplicate(ips: readonly string[], parameter: string): void {
  const seen = new Set<string>();
  for (const ip of ips) {
    if (seen.has(ip)) {
      throw new InvalidError(parameter, `duplicate ${ip}`);
    }
    seen.add(ip);
  }
}
