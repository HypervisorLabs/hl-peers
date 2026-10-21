/**
 * Node-only filesystem helpers for {@link GossipConfig}, plus the one helper
 * that joins the HTTP and file namespaces (CLIENT_SPEC.md §6.1, §6.6, §7).
 *
 * This is a **separate entry point** (`hl-peers/gossip-fs`)
 * so the core stays runtime-agnostic and browser-safe: importing the core
 * never pulls in `node:fs`.
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
import {
  closeSync,
  copyFileSync,
  existsSync,
  fchmodSync,
  fsyncSync,
  lstatSync,
  openSync,
  readFileSync,
  readlinkSync,
  renameSync,
  statSync,
  unlinkSync,
  writeSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join } from 'node:path';

import type { PeerApiClient, IterOptions } from './client.ts';
import { unifiedDiff } from './diff.ts';
import { InvalidError, NotFoundError, UnavailableError } from './errors.ts';
import { GossipConfig, type MergeOptions, type PeerInput, type Warning } from './gossip.ts';
import { PeerQuery } from './query.ts';
import type { Chain, Envelope } from './types.ts';

export interface SaveOptions {
  /** Copy the target to `<path>.<UTC %Y%m%dT%H%M%SZ>.bak` first. Default false. */
  backup?: boolean;
  /** Render and diff, write nothing. */
  dryRun?: boolean;
  /**
   * Permission bits for a **new** file. Default `0o600`; an existing file's
   * mode is preserved and never widened.
   *
   * 🔴 `fileMode`, never `mode`: `mode` is the **merge strategy** of §6.5, and
   * the two would collide the moment {@link updateGossipConfig} takes a single
   * options bag.
   */
  fileMode?: number;
  /** Injected clock for the backup timestamp — the only clock this module reads. */
  now?: () => Date;
}

export interface SaveResult {
  readonly written: boolean;
  readonly changed: boolean;
  /** The path the caller passed, symlink and all. */
  readonly path: string;
  /**
   * Where the bytes actually landed. It differs from {@link SaveResult.path}
   * only when that path was a symlink, which `save` **follows** rather than
   * replacing (CLIENT_SPEC §6.6).
   */
  readonly resolvedPath: string;
  readonly diff: string;
  readonly backupPath?: string;
  readonly warnings: readonly Warning[];
}

/** How many links `save` will follow before it refuses (CLIENT_SPEC §6.6). */
export const SYMLINK_DEPTH = 32;

/**
 * Follow a symlinked config path to the file it names.
 *
 * 🔴 `save` replaces the **target**, never the link: renaming over a symlink
 * swaps an operator's deliberate arrangement for a regular file, and from that
 * moment the node and their tooling read two different files with nothing to
 * announce it.
 */
function resolveSymlink(path: string): string {
  // The end of the walk: `current` is not a link (or is not there at all). A
  // dangling link is written through — but only into a directory that exists,
  // because creating one would be guessing at the operator's layout.
  const settle = (current: string): string => {
    if (current !== path && !existsSync(dirname(current))) {
      throw new InvalidError(
        'path',
        `${path} is a symbolic link to ${current}, whose directory does not exist; ` +
          "refusing to create one rather than guess at the operator's layout",
      );
    }
    return current;
  };

  let current = path;
  for (let depth = 0; depth < SYMLINK_DEPTH; depth += 1) {
    let link: string;
    try {
      if (!lstatSync(current).isSymbolicLink()) return settle(current);
      link = readlinkSync(current);
    } catch (err) {
      if (err instanceof InvalidError) throw err;
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') return settle(current);
      throw err;
    }
    current = link.startsWith('/') ? link : join(dirname(current), link);
  }
  throw new InvalidError(
    'path',
    `${path} resolves through more than ${SYMLINK_DEPTH} symbolic links; ` +
      'refusing to follow further',
  );
}

/**
 * `$HOME/override_gossip_config.json`.
 *
 * 🪤 It is a **default, never a discovery**: this library will not inspect a
 * running process, a systemd unit or `/proc` to guess the path. The upstream
 * evidence cannot distinguish `$HOME`-relative from cwd-relative, because on
 * every observed box cwd == `$HOME`; `$HOME`-relative is the labelled
 * **hypothesis**. Emitting to stdout and placing the file yourself is the
 * safest option, and it is a first-class path.
 */
export function defaultConfigPath(): string {
  return join(homedir(), 'override_gossip_config.json');
}

/**
 * Read and parse a config.
 *
 * 🔴 A missing file raises {@link NotFoundError}: this library never invents an
 * empty config for a missing file, because an empty config is the crash-loop
 * shape. Use {@link loadOrNew} to opt into that explicitly.
 */
export function load(path: string): GossipConfig {
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (cause) {
    if ((cause as NodeJS.ErrnoException).code === 'ENOENT') {
      throw new NotFoundError(`no config at ${path}`, { path, cause });
    }
    throw cause;
  }
  return GossipConfig.parse(text);
}

/** {@link load}, falling back to an explicitly empty new config. */
export function loadOrNew(path: string, chain: Chain): GossipConfig {
  try {
    return load(path);
  } catch (err) {
    if (err instanceof NotFoundError) return GossipConfig.new(chain, []);
    throw err;
  }
}

function backupSuffix(d: Date): string {
  const p = (n: number, w = 2): string => String(n).padStart(w, '0');
  return (
    `${p(d.getUTCFullYear(), 4)}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}` +
    `T${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}Z`
  );
}

function readIfExists(path: string): string | null {
  try {
    return readFileSync(path, 'utf8');
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw err;
  }
}

/**
 * Validate, render, and atomically write — or do nothing at all.
 *
 * The normative order (§6.6): `validate()` first, so an invalid config never
 * reaches the filesystem; then render; then, if the bytes on disk are already
 * identical, a **true no-op** — no write, no `.bak`, no mtime change; then the
 * optional backup; then a temp file **in the same directory**, `fsync`, and an
 * atomic rename over the target. A temp file in `/tmp` would be wrong: a rename
 * across filesystems is not atomic.
 *
 * 🔴 `save` never restarts, signals, or reloads anything. The node reads this
 * file when it starts; the change takes effect on the node's next
 * start, which is yours to schedule.
 */
export function save(config: GossipConfig, path: string, opts: SaveOptions = {}): SaveResult {
  const { warnings } = config.validate();
  const rendered = config.toJson();

  // 🔴 Step 0: follow a symlinked path to its target, so the atomic rename
  // replaces the file the operator pointed at rather than the link itself.
  const resolvedPath = resolveSymlink(path);
  const existing = readIfExists(resolvedPath);

  if (existing === rendered) {
    return { written: false, changed: false, path, resolvedPath, diff: '', warnings };
  }

  const diff = unifiedDiff(existing ?? '', rendered, path, path);

  if (opts.dryRun === true) {
    return { written: false, changed: true, path, resolvedPath, diff, warnings };
  }

  let mode = opts.fileMode ?? 0o600;
  if (existing !== null) {
    // Preserve the target's mode; never widen it.
    mode = statSync(resolvedPath).mode & 0o777;
  }

  let backupPath: string | undefined;
  if (opts.backup === true && existing !== null) {
    const now = (opts.now ?? (() => new Date()))();
    backupPath = `${resolvedPath}.${backupSuffix(now)}.bak`;
    copyFileSync(resolvedPath, backupPath);
  }

  const dir = dirname(resolvedPath);
  const tmp = join(
    dir,
    `.${basename(resolvedPath)}.${Math.random().toString(36).slice(2, 10)}.tmp`,
  );
  let fd: number | null = null;
  try {
    fd = openSync(tmp, 'wx', mode);
    writeSync(fd, rendered, null, 'utf8');
    fsyncSync(fd);
    // `open` applies the process umask to its mode argument, so a preserved
    // mode has to be re-applied explicitly or a group-readable file silently
    // narrows on save.
    fchmodSync(fd, mode);
    closeSync(fd);
    fd = null;
    renameSync(tmp, resolvedPath);
  } catch (err) {
    if (fd !== null) {
      try {
        closeSync(fd);
      } catch {
        /* the original is untouched by construction */
      }
    }
    try {
      unlinkSync(tmp);
    } catch {
      /* already gone */
    }
    throw err;
  }

  // fsync the directory so the rename itself is durable, where supported.
  try {
    const dirFd = openSync(dir, 'r');
    try {
      fsyncSync(dirFd);
    } finally {
      closeSync(dirFd);
    }
  } catch {
    /* not supported on this platform */
  }

  const result: SaveResult = { written: true, changed: true, path, resolvedPath, diff, warnings };
  return backupPath === undefined ? result : { ...result, backupPath };
}

export interface UpdateOptions extends MergeOptions, SaveOptions {
  /** Fetch `maxRoots * overfetch` candidates. Default 4. */
  overfetch?: number;
  /** Build and validate, but never touch the filesystem. */
  noWrite?: boolean;
  /** Passed through to `iterPeers`. */
  iter?: IterOptions;
}

export interface UpdateResult {
  readonly config: GossipConfig;
  /** Always present, so "generate to stdout and place it yourself" is first-class. */
  readonly json: string;
  readonly changed: boolean;
  readonly diff: string;
  readonly writtenPath?: string;
  readonly warnings: readonly Warning[];
  readonly envelope: Envelope | null;
}

/**
 * Query the API and produce (or update) an `override_gossip_config.json`.
 * Exactly one round of work.
 *
 * 🔴 An `available: false` response raises {@link UnavailableError} **before**
 * any file is touched: an unbuildable graph is not an empty peer set, and
 * turning it into one would empty an operator's root list — the crash-loop
 * shape.
 *
 * 🔴 The `chain` for a fresh config comes from `opts.chain`, else the response
 * envelope, else it raises. It is never defaulted to `Mainnet`.
 */
export async function updateGossipConfig(
  client: PeerApiClient,
  queryOrPeers: PeerQuery | readonly PeerInput[],
  path?: string,
  opts: UpdateOptions = {},
): Promise<UpdateResult> {
  const maxRoots = opts.maxRoots ?? 8;
  const overfetch = opts.overfetch ?? 4;
  let peers: PeerInput[];
  let envelope: Envelope | null = null;

  if (queryOrPeers instanceof PeerQuery) {
    const walk = client.iterPeers(queryOrPeers, {
      ...opts.iter,
      maxItems: maxRoots * overfetch,
    });
    peers = [];
    for await (const peer of walk) peers.push(peer);
    envelope = walk.envelope;
    if (envelope !== null && !envelope.available) {
      throw new UnavailableError(
        'the producer could not build a graph (available: false). Refusing to treat that ' +
          'as an empty peer set: writing it would empty root_node_ips and crash-loop the node.',
      );
    }
  } else {
    peers = [...queryOrPeers];
  }

  const existing = path === undefined ? null : readIfExists(path);
  let config: GossipConfig;
  if (path !== undefined && existing !== null) {
    config = GossipConfig.parse(existing);
  } else {
    const chain = opts.chain ?? (envelope?.chain ?? undefined);
    if (chain === undefined) {
      throw new InvalidError(
        'chain',
        'no chain to write: pass opts.chain. This library never defaults it, because a ' +
          "chain that disagrees with the node's --chain stops the node from starting",
      );
    }
    config = GossipConfig.new(chain, []);
    if (opts.tryNewPeers !== undefined) config.setTryNewPeers(opts.tryNewPeers);
  }

  config.mergePeers(peers, opts);
  // §6.5: validate() already unions the pending merge warnings with the
  // state-derived ones, de-duplicated by code.
  const { warnings } = config.validate();
  const json = config.toJson();

  if (path !== undefined && opts.noWrite !== true) {
    const saved = save(config, path, opts);
    const result: UpdateResult = {
      config,
      json,
      changed: saved.changed,
      diff: saved.diff,
      warnings: saved.warnings,
      envelope,
    };
    return saved.written ? { ...result, writtenPath: saved.path } : result;
  }

  const diff =
    path === undefined || existing === null ? '' : unifiedDiff(existing, json, path, path);
  return {
    config,
    json,
    changed: existing !== json,
    diff,
    warnings,
    envelope,
  };
}

export { GossipConfig };
