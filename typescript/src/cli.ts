#!/usr/bin/env node
/**
 * `hl-peers` — the optional CLI of CLIENT_SPEC.md §8, with the same flags in
 * all four languages so one runbook works against any of them. Zero
 * dependencies: `node:util`'s `parseArgs` and nothing else.
 *
 * 🔴 It **never** restarts a node and has no `--restart` / `--apply` flag.
 */
import { parseArgs } from 'node:util';

import { PeerApiClient } from './client.ts';
import { peerQuery, type PeerQuery } from './query.ts';
import {
  BadRequestError,
  InvalidError,
  NotFoundError,
  PeerApiError,
  RateLimitedError,
  UnauthorizedError,
} from './errors.ts';
import { GossipConfig, type Warning } from './gossip.ts';
import { defaultConfigPath, load, save, updateGossipConfig } from './gossip-fs.ts';
import type { Peer } from './types.ts';

const USAGE = `hl-peers — query the Hyperliquid Peer API and maintain override_gossip_config.json

  hl-peers query   [--chain Mainnet|Testnet] [--network mainnet|testnet]
                   [filter flags] [--all]
                   [--format json|gossip-config] [--output json|table|ips]
  hl-peers facets  [--chain …] [--network …] [filter flags]
  hl-peers peer <ip> [--network mainnet|testnet]
  hl-peers gossip show     --path ~/override_gossip_config.json
  hl-peers gossip validate --path …
  hl-peers gossip update   --path … [filter flags] [--max-roots 8]
                           [--mode replace|append|prepend] [--no-dedupe]
                           [--keep IPs] [--exclude IPs] [--chain Mainnet]
                           [--network mainnet|testnet]
                           [--try-new-peers|--no-try-new-peers] [--n-gossip-peers 20]
                           [--file-mode 0600] [--backup] [--dry-run] [--stdout]

Filters: --cc --country --region --asn --tier --max-tier --role --label
         --connectable --accepting --state
         --serves-mempool true,false,null --auction holds_slot,bidding,bid_before,null
         --max-blocks-behind --min-uptime --max-latency-ms --vantage
         --seen-within --version --sort --limit --max-items --fields --format

Network: --network mainnet|testnet (unset sends nothing: mainnet; testnet is beta)

Global:  --base-url URL --api-key KEY --timeout SECS --max-retries N
         --no-cache --quiet --verbose --json-errors --help

Exit: 0 ok · 1 client/validation error · 2 usage · 3 rate limited ·
      4 unauthorized · 5 not found · 6 unavailable/server/transport`;

const OPTIONS = {
  // global
  'base-url': { type: 'string' },
  'api-key': { type: 'string' },
  timeout: { type: 'string' },
  'max-retries': { type: 'string' },
  'no-cache': { type: 'boolean' },
  quiet: { type: 'boolean' },
  verbose: { type: 'boolean' },
  'json-errors': { type: 'boolean' },
  help: { type: 'boolean', short: 'h' },
  // filters
  cc: { type: 'string' },
  region: { type: 'string' },
  asn: { type: 'string' },
  tier: { type: 'string' },
  'max-tier': { type: 'string' },
  role: { type: 'string' },
  label: { type: 'string' },
  connectable: { type: 'string' },
  accepting: { type: 'boolean' },
  state: { type: 'string' },
  country: { type: 'string' },
  'serves-mempool': { type: 'string' },
  auction: { type: 'string' },
  'max-blocks-behind': { type: 'string' },
  'min-uptime': { type: 'string' },
  'max-latency-ms': { type: 'string' },
  vantage: { type: 'string' },
  'seen-within': { type: 'string' },
  version: { type: 'string' },
  sort: { type: 'string' },
  limit: { type: 'string' },
  fields: { type: 'string' },
  format: { type: 'string' },
  all: { type: 'boolean' },
  'max-items': { type: 'string' },
  output: { type: 'string' },
  // gossip
  path: { type: 'string' },
  'max-roots': { type: 'string' },
  mode: { type: 'string' },
  'no-dedupe': { type: 'boolean' },
  keep: { type: 'string' },
  exclude: { type: 'string' },
  chain: { type: 'string' },
  network: { type: 'string' },
  'try-new-peers': { type: 'boolean' },
  'no-try-new-peers': { type: 'boolean' },
  'n-gossip-peers': { type: 'string' },
  // 🔴 --file-mode, never --mode: `mode` is the merge strategy (§6.6).
  'file-mode': { type: 'string' },
  backup: { type: 'boolean' },
  'dry-run': { type: 'boolean' },
  stdout: { type: 'boolean' },
} as const;

type Values = Partial<Record<keyof typeof OPTIONS, string | boolean>>;

class UsageError extends Error {}

function str(v: string | boolean | undefined): string | undefined {
  return typeof v === 'string' ? v : undefined;
}

function int(v: string | boolean | undefined, name: string): number | undefined {
  const s = str(v);
  if (s === undefined) return undefined;
  const n = Number(s);
  if (!Number.isFinite(n)) throw new UsageError(`--${name} expects a number, got ${s}`);
  return n;
}

function list(v: string | boolean | undefined): string[] | undefined {
  const s = str(v);
  return s === undefined ? undefined : s.split(',').filter((x) => x !== '');
}

/**
 * `includeChain` is false for `gossip update`, where `--chain` names the chain
 * written into the config file rather than a query filter.
 */
function buildQuery(v: Values, includeChain = true): PeerQuery {
  let q = peerQuery();
  const cc = list(v.cc);
  if (cc !== undefined) q = q.cc(cc);
  const region = list(v.region);
  if (region !== undefined) q = q.region(region);
  const asn = list(v.asn);
  if (asn !== undefined) q = q.asn(asn);
  const tier = list(v.tier);
  if (tier !== undefined) q = q.tier(tier.map((t) => (t === 'null' ? null : Number(t))));
  const maxTier = int(v['max-tier'], 'max-tier');
  if (maxTier !== undefined) q = q.maxTier(maxTier);
  const role = list(v.role);
  if (role !== undefined) q = q.role(role);
  const label = list(v.label);
  if (label !== undefined) q = q.label(label);
  const conn = str(v.connectable);
  if (conn !== undefined) q = q.connectable(conn);
  if (v.accepting === true) q = q.accepting(true);
  const state = str(v.state);
  if (state !== undefined) q = q.state(state);
  const country = list(v.country);
  if (country !== undefined) q = q.country(country);
  // Passed as the CSV the operator typed; the builder validates the closed enums.
  const servesMempool = str(v['serves-mempool']);
  if (servesMempool !== undefined) q = q.servesMempool(servesMempool);
  const auction = str(v.auction);
  if (auction !== undefined) q = q.auction(auction);
  const mbb = int(v['max-blocks-behind'], 'max-blocks-behind');
  if (mbb !== undefined) q = q.maxBlocksBehind(mbb);
  const mu = int(v['min-uptime'], 'min-uptime');
  if (mu !== undefined) q = q.minUptime(mu);
  const ml = int(v['max-latency-ms'], 'max-latency-ms');
  if (ml !== undefined) q = q.maxLatencyMs(ml);
  const vantage = str(v.vantage);
  if (vantage !== undefined) q = q.vantage(vantage);
  const seen = str(v['seen-within']);
  if (seen !== undefined) q = q.seenWithin(seen);
  const version = list(v.version);
  if (version !== undefined) q = q.version(version);
  const sort = str(v.sort);
  if (sort !== undefined) q = q.sort(sort as never);
  const limit = int(v.limit, 'limit');
  if (limit !== undefined) q = q.limit(limit);
  const fields = list(v.fields);
  if (fields !== undefined) q = q.fields(fields);
  const format = str(v.format);
  if (format !== undefined) q = q.format(format === 'gossip-config' ? 'gossip_config' : (format as never));
  const chain = str(v.chain);
  if (includeChain && chain !== undefined) q = q.chain(chain as never);
  // Passed as typed; the builder refuses anything but mainnet|testnet.
  const network = str(v.network);
  if (network !== undefined) q = q.network(network as never);
  return q;
}

function makeClient(v: Values): PeerApiClient {
  const timeout = int(v.timeout, 'timeout');
  return new PeerApiClient({
    ...(str(v['base-url']) === undefined ? {} : { baseUrl: str(v['base-url'])! }),
    ...(str(v['api-key']) === undefined ? {} : { apiKey: str(v['api-key'])! }),
    ...(timeout === undefined ? {} : { timeoutMs: timeout * 1000 }),
    ...(int(v['max-retries'], 'max-retries') === undefined
      ? {}
      : { maxRetries: int(v['max-retries'], 'max-retries')! }),
    ...(v['no-cache'] === true ? { cache: null } : {}),
  });
}

/** 🔴 Never suppressed, even under --quiet. */
const LOUD_WARNINGS = new Set([
  'many_roots_gt_24',
  'many_roots_gt_64',
  'truncated',
  'exclude_overrides_keep_existing',
]);

function printWarnings(warnings: readonly Warning[], quiet: boolean): void {
  for (const w of warnings) {
    if (quiet && !LOUD_WARNINGS.has(w.code)) continue;
    process.stderr.write(`warning [${w.code}] ${w.message}\n`);
  }
}

function renderPeers(peers: readonly Peer[], output: string): string {
  switch (output) {
    case 'ips':
      return peers.map((p) => p.id).join('\n');
    case 'table':
      return peers
        .map(
          (p) =>
            [
              p.id.padEnd(15),
              `tier=${p.tier === null ? '-' : p.tier}`.padEnd(8),
              `cc=${p.cc ?? '-'}`.padEnd(7),
              `connectable=${p.connectable ?? 'unknown'}`,
            ].join(' '),
        )
        .join('\n');
    case 'json':
      return JSON.stringify(peers, null, 2);
    default:
      throw new UsageError(`--output expects json, table or ips, got ${output}`);
  }
}

function gossipOpts(v: Values): Record<string, unknown> {
  const opts: Record<string, unknown> = {};
  const maxRoots = int(v['max-roots'], 'max-roots');
  if (maxRoots !== undefined) opts['maxRoots'] = maxRoots;
  const mode = str(v.mode);
  if (mode !== undefined) opts['mode'] = mode;
  if (v['no-dedupe'] === true) opts['dedupe'] = false;
  const keep = list(v.keep);
  if (keep !== undefined) opts['keepExisting'] = keep;
  const exclude = list(v.exclude);
  if (exclude !== undefined) opts['exclude'] = exclude;
  const chain = str(v.chain);
  if (chain !== undefined) opts['chain'] = chain;
  if (v['try-new-peers'] === true) opts['tryNewPeers'] = true;
  if (v['no-try-new-peers'] === true) opts['tryNewPeers'] = false;
  if (v.backup === true) opts['backup'] = true;
  if (v['dry-run'] === true) opts['dryRun'] = true;
  const fileMode = str(v['file-mode']);
  if (fileMode !== undefined) {
    const parsed = Number.parseInt(fileMode, 8);
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 0o7777) {
      throw new UsageError(`--file-mode expects octal permission bits, got ${fileMode}`);
    }
    opts['fileMode'] = parsed;
  }
  return opts;
}

async function runCommand(argv: string[]): Promise<number> {
  let parsed;
  try {
    parsed = parseArgs({ args: argv, options: OPTIONS, allowPositionals: true });
  } catch (err) {
    // An unknown flag is a usage error (exit 2), not a transport failure. There
    // is deliberately no --restart and no --apply, so both land here.
    throw new UsageError(err instanceof Error ? err.message : String(err));
  }
  const v = parsed.values as Values;
  const [command, sub, ...rest] = parsed.positionals;

  if (v.help === true || command === undefined) {
    process.stdout.write(USAGE + '\n');
    return command === undefined && v.help !== true ? 2 : 0;
  }
  const quiet = v.quiet === true;

  switch (command) {
    case 'query': {
      const client = makeClient(v);
      let query = buildQuery(v);
      const output = str(v.output) ?? 'table';
      // `--format gossip-config` goes through the typed helper (§4.7): the body
      // is a config file, not a peer list. The canonical bytes go to stdout.
      if (query.toObject()['format'] === 'gossip_config') {
        if (v['try-new-peers'] === true) query = query.tryNewPeers(true);
        if (v['no-try-new-peers'] === true) query = query.tryNewPeers(false);
        const res = await client.listPeersAsGossipConfig(query);
        if ('notModified' in res) return 0;
        printWarnings(res.config.validate().warnings, quiet);
        process.stdout.write(res.config.toJson());
        return 0;
      }
      if (v.all === true) {
        const walk = client.iterPeers(query, {
          maxItems: int(v['max-items'], 'max-items') ?? 10_000,
        });
        const peers: Peer[] = [];
        for await (const peer of walk) peers.push(peer);
        if (walk.truncated) {
          process.stderr.write(`warning [truncated] stopped at the max-items guard\n`);
        }
        process.stdout.write(renderPeers(peers, output) + '\n');
        return 0;
      }
      const res = await client.listPeers(query);
      if ('notModified' in res) return 0;
      if (!res.envelope.available && !quiet) {
        process.stderr.write(
          'warning: available=false — the producer could not build a graph. The empty list ' +
            'is NOT a finding about the network.\n',
        );
      }
      if (res.envelope.stale && !quiet) {
        process.stderr.write(`warning: stale snapshot (generated_at ${res.envelope.generated_at ?? '?'})\n`);
      }
      process.stdout.write(renderPeers(res.peers, output) + '\n');
      return 0;
    }

    case 'facets': {
      const res = await makeClient(v).facets(buildQuery(v));
      if ('notModified' in res) return 0;
      process.stdout.write(JSON.stringify({ total: res.total, facets: res.facets }, null, 2) + '\n');
      return 0;
    }

    case 'peer': {
      if (sub === undefined) throw new UsageError('hl-peers peer <ip>');
      const network = str(v.network);
      const res = await makeClient(v).getPeer(
        sub,
        network === undefined ? {} : { network: network as never },
      );
      if ('notModified' in res) return 0;
      process.stdout.write(JSON.stringify(res.peer, null, 2) + '\n');
      return 0;
    }

    case 'gossip': {
      const path = str(v.path) ?? defaultConfigPath();
      switch (sub) {
        case 'show': {
          process.stdout.write(load(path).toJson());
          return 0;
        }
        case 'validate': {
          const { warnings } = load(path).validate();
          printWarnings(warnings, quiet);
          if (!quiet) process.stdout.write(`${path}: ok\n`);
          return 0;
        }
        case 'update': {
          const client = makeClient(v);
          const opts = gossipOpts(v);
          const target = v.stdout === true ? undefined : path;
          const nGossip = int(v['n-gossip-peers'], 'n-gossip-peers');
          // With --n-gossip-peers we build first, set the knob, then save, so
          // the value the operator chose is the one that reaches the file.
          const res = await updateGossipConfig(client, buildQuery(v, false), target, {
            ...opts,
            ...(nGossip === undefined ? {} : { noWrite: true }),
          });
          printWarnings(res.warnings, quiet);
          if (nGossip !== undefined) {
            res.config.setNGossipPeers(nGossip);
            if (v.stdout === true) {
              process.stdout.write(res.config.toJson());
              return 0;
            }
            const saved = save(res.config, path, opts);
            if (saved.written) {
              process.stderr.write(
                `wrote ${saved.path}. The node reads this file when it starts ` +
                  `— the change takes effect on the node's next start, which is ` +
                  `yours to schedule.\n`,
              );
            } else if (v['dry-run'] === true && saved.changed) {
              process.stdout.write(saved.diff);
            }
            return 0;
          }
          if (v.stdout === true) {
            process.stdout.write(res.json);
            return 0;
          }
          if (v['dry-run'] === true) {
            if (res.changed) process.stdout.write(res.diff);
            return 0;
          }
          if (res.writtenPath !== undefined) {
            process.stderr.write(
              `wrote ${res.writtenPath}. The node reads this file when it starts ` +
                `— the change takes effect on the node's next start, which is yours ` +
                `to schedule.\n`,
            );
          } else if (!quiet) {
            process.stderr.write(`${path} is already up to date; nothing was written.\n`);
          }
          return 0;
        }
        default:
          throw new UsageError('hl-peers gossip show|validate|update');
      }
    }

    default:
      throw new UsageError(`unknown command ${command}${rest.length > 0 ? '' : ''}`);
  }
}

/** Map an error to the §8 exit code, and say why on stderr. */
export function exitCodeFor(err: unknown): number {
  if (err instanceof UsageError) return 2;
  if (err instanceof RateLimitedError) return 3;
  if (err instanceof UnauthorizedError) return 4;
  if (err instanceof NotFoundError) return 5;
  if (err instanceof InvalidError || err instanceof BadRequestError) return 1;
  if (err instanceof PeerApiError) return 6;
  return 6;
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const jsonErrors = argv.includes('--json-errors');
  try {
    return await runCommand(argv);
  } catch (err) {
    const code = exitCodeFor(err);
    const kind = err instanceof PeerApiError ? err.kind : 'Usage';
    const message = err instanceof Error ? err.message : String(err);
    if (jsonErrors) {
      process.stderr.write(JSON.stringify({ kind, message, exit_code: code }) + '\n');
    } else {
      process.stderr.write(`hl-peers: ${kind}: ${message}\n`);
      if (code === 2) process.stderr.write(USAGE + '\n');
    }
    return code;
  }
}

const isEntrypoint =
  typeof process !== 'undefined' &&
  process.argv[1] !== undefined &&
  /\bcli\.(ts|js|cjs|mjs)$/.test(process.argv[1]);

if (isEntrypoint) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`hl-peers: ${String(err)}\n`);
      process.exitCode = 6;
    },
  );
}

export { GossipConfig, save };
