/**
 * JSON-in / JSON-out differential harness for the TypeScript client.
 *
 * One NDJSON request per line on stdin, one NDJSON answer per line on stdout.
 * `tools/cross_check/run.py` drives this and the three sibling harnesses side
 * by side and asserts all four answer identically.
 *
 * Run it with `node cross_check/harness.ts` (Node strips the types).
 */

import { createInterface } from 'node:readline';
import { mkdtempSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  DEFAULT_BASE_PATH,
  DEFAULT_BASE_URL,
  GossipConfig,
  PeerApiClient,
  PeerQuery,
  mempoolServers,
  notFull,
  sentries,
  tier1,
} from '../src/index.ts';
import { PeerApiError } from '../src/errors.ts';
import { save as saveConfig } from '../src/gossip-fs.ts';

type Json = any;

const BOOL_FIELDS = new Set(['accepting', 'try_new_peers']);
const INT_FIELDS = new Set(['max_tier', 'max_blocks_behind', 'max_latency_ms', 'limit']);
const FLOAT_FIELDS = new Set(['min_uptime']);

const SETTER: Record<string, string> = {
  chain: 'chain',
  cc: 'cc',
  region: 'region',
  asn: 'asn',
  tier: 'tier',
  max_tier: 'maxTier',
  role: 'role',
  label: 'label',
  connectable: 'connectable',
  accepting: 'accepting',
  state: 'state',
  country: 'country',
  serves_mempool: 'servesMempool',
  auction: 'auction',
  try_new_peers: 'tryNewPeers',
  max_blocks_behind: 'maxBlocksBehind',
  min_uptime: 'minUptime',
  max_latency_ms: 'maxLatencyMs',
  vantage: 'vantage',
  seen_within: 'seenWithin',
  version: 'version',
  sort: 'sort',
  limit: 'limit',
  cursor: 'cursor',
  fields: 'fields',
  format: 'format',
};

const MERGE_OPT: Record<string, string> = {
  max_roots: 'maxRoots',
  mode: 'mode',
  dedupe: 'dedupe',
  keep_existing: 'keepExisting',
  exclude: 'exclude',
  chain: 'chain',
  try_new_peers: 'tryNewPeers',
};

const METHOD: Record<string, string> = {
  to_json: 'toJson',
  root_ips: 'rootIps',
  has_root_ip: 'hasRootIp',
  add_root_ip: 'addRootIp',
  remove_root_ip: 'removeRootIp',
  set_root_ips: 'setRootIps',
  reserved_peer_ips: 'reservedPeerIps',
  add_reserved_peer_ip: 'addReservedPeerIp',
  remove_reserved_peer_ip: 'removeReservedPeerIp',
  set_reserved_peer_ips: 'setReservedPeerIps',
  set_chain: 'setChain',
  set_try_new_peers: 'setTryNewPeers',
  set_n_gossip_peers: 'setNGossipPeers',
  set_split_client_blocks: 'setSplitClientBlocks',
  unset_split_client_blocks: 'unsetSplitClientBlocks',
};

function errorPayload(err: unknown, atOp?: number): Json {
  const e = err as any;
  const out: Json = { kind: e.kind };
  if (e.parameter !== undefined && e.parameter !== null) out.parameter = e.parameter;
  if (e.filter !== undefined && e.filter !== null) out.filter = e.filter;
  if (e.retryAfter !== undefined && e.retryAfter !== null) out.retry_after = e.retryAfter;
  if (e.seen !== undefined && e.seen !== null) out.seen = e.seen;
  // `status` is deliberately not reported: CLIENT_SPEC §5.1 puts it on the
  // `Server` variant alone, so exposing it elsewhere is a per-client accident.
  if (atOp !== undefined) out.at_op = atOp;
  return out;
}

function subst(value: Json, tmp: string): Json {
  if (typeof value === 'string') return value.split('$TMPDIR').join(tmp);
  if (Array.isArray(value)) return value.map((v) => subst(v, tmp));
  if (value && typeof value === 'object') {
    const out: Json = {};
    for (const [k, v] of Object.entries(value)) out[k] = subst(v, tmp);
    return out;
  }
  return value;
}

function unsubst(value: Json, tmp: string): Json {
  if (typeof value === 'string') return value.split(tmp).join('$TMPDIR');
  if (Array.isArray(value)) return value.map((v) => unsubst(v, tmp));
  if (value && typeof value === 'object') {
    const out: Json = {};
    for (const [k, v] of Object.entries(value)) out[k] = unsubst(v, tmp);
    return out;
  }
  return value;
}

function mergeOpts(opts: Json): Json {
  const out: Json = {};
  for (const [k, v] of Object.entries(opts ?? {})) {
    const name = MERGE_OPT[k];
    if (name === undefined) throw new Error(`unknown merge option ${k}`);
    out[name] = v;
  }
  return out;
}

function codes(warnings: readonly { code: string }[]): string[] {
  return warnings.map((w) => w.code).sort();
}

function runGossip(req: Json): Json {
  const tmp = mkdtempSync(join(tmpdir(), 'hlp-xcheck-ts-'));
  try {
    return runGossipIn(req, tmp);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

function runGossipIn(req: Json, tmp: string): Json {
  let subject: GossipConfig | null = null;
  let value: Json = null;
  let opWarnings: string[] | null = null;
  let savedText: string | null = null;

  try {
    if (req.input_json_text !== undefined && req.input_json_text !== null) {
      subject = GossipConfig.parse(req.input_json_text);
    } else if (req.input_config !== undefined && req.input_config !== null) {
      subject = GossipConfig.parse(JSON.stringify(req.input_config));
    }
  } catch (err) {
    if (err instanceof PeerApiError) return { ok: false, error: errorPayload(err, -1) };
    throw err;
  }

  const operations: Json[] = req.operations ?? [];
  for (let index = 0; index < operations.length; index += 1) {
    const operation = operations[index];
    const name: string = operation.op;
    const args: Json[] = subst(operation.args ?? [], tmp);
    const opts: Json = subst(operation.opts ?? {}, tmp);
    try {
      if (name === 'new') {
        subject = GossipConfig.new(args[0], args[1] ?? []);
        value = null;
      } else if (name === 'from_peers') {
        const result = GossipConfig.fromPeers(args[0], mergeOpts(opts));
        subject = result.config;
        opWarnings = codes(result.warnings);
        value = null;
      } else if (name === 'merge_peers') {
        const result = subject!.mergePeers(args[0], mergeOpts(opts));
        subject = result.config;
        opWarnings = codes(result.warnings);
        value = null;
      } else if (name === 'save') {
        const saveOptions: Json = {};
        if (opts.dry_run !== undefined) saveOptions.dryRun = opts.dry_run;
        if (opts.backup !== undefined) saveOptions.backup = opts.backup;
        if (opts.file_mode !== undefined) saveOptions.fileMode = opts.file_mode;
        const result = saveConfig(subject!, args[0], saveOptions);
        value = {
          written: result.written,
          changed: result.changed,
          diff: unsubst(result.diff, tmp),
          backup: result.backupPath != null,
        };
        opWarnings = codes(result.warnings);
        savedText = existsSync(args[0]) ? readFileSync(args[0], 'utf8') : null;
      } else if (name === 'validate') {
        const result = subject!.validate();
        value = 'ok';
        opWarnings = codes(result.warnings);
      } else {
        const method = METHOD[name];
        if (method === undefined) throw new Error(`unknown operation ${name}`);
        const out = (subject as any)[method](...args);
        value = out instanceof GossipConfig ? null : (out ?? null);
      }
    } catch (err) {
      if (err instanceof PeerApiError) return { ok: false, error: errorPayload(err, index) };
      return { ok: false, harness_error: String(err) };
    }
  }

  const answer: Json = { ok: true, value: unsubst(value, tmp) };
  answer.json_text = subject === null ? null : subject.toJson();
  answer.op_warnings = opWarnings;
  answer.saved_text = savedText;
  if (subject === null) {
    answer.warnings = null;
  } else {
    try {
      answer.warnings = codes(subject.validate().warnings);
    } catch (err) {
      if (!(err instanceof PeerApiError)) throw err;
      answer.warnings = null;
      answer.validate_error = errorPayload(err);
    }
  }
  return answer;
}

/**
 * A query carrying parameters the builder deliberately does not model — e.g.
 * the removed `mempool=` filter, whose fixture exists to prove the server's
 * `400 filter-removed` maps to `BadRequest`. Harness-only: the public builder
 * sends exactly the documented parameters.
 */
class RawExtraQuery extends PeerQuery {
  private readonly inner: PeerQuery;
  private readonly extra: [string, string][];
  constructor(inner: PeerQuery, extra: [string, string][]) {
    super();
    this.inner = inner;
    this.extra = extra;
  }
  override validate(): void {
    this.inner.validate();
  }
  override toParams(): [string, string][] {
    return [...this.inner.toParams(), ...this.extra].sort((a, b) =>
      a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0,
    );
  }
}

function buildQuery(spec: Json, allowUnmodelled = false): PeerQuery {
  let query = new PeerQuery();
  const extra: [string, string][] = [];
  for (const name of Object.keys(spec ?? {}).sort()) {
    let value: Json = spec[name];
    if (BOOL_FIELDS.has(name) && typeof value === 'string') value = value === 'true';
    else if (INT_FIELDS.has(name)) value = Number.parseInt(String(value), 10);
    else if (FLOAT_FIELDS.has(name)) value = Number.parseFloat(String(value));
    const setter = SETTER[name];
    if (setter === undefined) {
      if (allowUnmodelled) {
        extra.push([name, String(value)]);
        continue;
      }
      throw new Error(`unknown query field ${name}`);
    }
    query = (query as any)[setter](value);
  }
  return extra.length === 0 ? query : new RawExtraQuery(query, extra);
}

function networkMapOptions(spec: Json): Json {
  const out: Json = {};
  for (const [name, value] of Object.entries(spec ?? {})) {
    if (name === 'max_tier') out.maxTier = Number.parseInt(String(value), 10);
    else if (name === 'show_unreached') out.showUnreached = value === true || value === 'true';
    else if (name === 'hide_unreachable') out.hideUnreachable = value === true || value === 'true';
    else if (name === 'connectable' || name === 'cc' || name === 'operator' || name === 'label') {
      out[name] = value;
    } else if (name === 'serves_mempool') out.servesMempool = value;
    else if (name === 'auction') out.auction = value;
    else throw new Error(`unknown network-map option ${name}`);
  }
  return out;
}

const PRESETS: Record<string, () => PeerQuery> = {
  tier1: () => tier1(),
  sentries: () => sentries(),
  not_full_cc: () => notFull({ cc: ['SG'] }),
  mempool_servers: () => mempoolServers(),
};

function runQueryUrl(req: Json): Json {
  try {
    const preset = req.preset === undefined ? undefined : PRESETS[req.preset];
    if (req.preset !== undefined && preset === undefined) {
      return { ok: false, harness_error: `unknown preset ${String(req.preset)}` };
    }
    const query = preset === undefined ? buildQuery(req.query) : preset();
    query.validate();
    const qs = query.toQueryString();
    return { ok: true, url: DEFAULT_BASE_URL + DEFAULT_BASE_PATH + '/peers' + (qs ? '?' + qs : '') };
  } catch (err) {
    if (err instanceof PeerApiError) return { ok: false, error: errorPayload(err) };
    return { ok: false, harness_error: String(err) };
  }
}

function rateLimitPayload(rl: Json): Json {
  if (!rl) return {};
  return {
    limit: rl.limit ?? null,
    remaining: rl.remaining ?? null,
    reset: rl.reset ?? null,
    policy: rl.policy ?? null,
    retry_after: rl.retryAfter ?? null,
  };
}

function envelopePayload(env: Json): Json {
  return {
    version: env.version ?? null,
    available: env.available,
    stale: env.stale,
    chain: env.chain ?? null,
    generated_at: env.generated_at ?? null,
    next_sweep_at: env.next_sweep_at ?? null,
    attribution: env.attribution ?? null,
  };
}

function mapEnvelopePayload(env: Json): Json {
  return {
    version: env.version ?? null,
    available: env.available,
    stale: env.stale,
    generated_at: env.generated_at ?? null,
  };
}

function signals(peers: Json[]): Json[] {
  return peers.map((p) => [
    p.id,
    p.mempool.serves,
    p.mempool.source,
    p.auction.status,
    p.auction.slot,
  ]);
}

/** 1.1.0: `[id, state, tier, city]` per peer, in order. */
function rows(peers: Json[]): Json[] {
  return peers.map((p) => [p.id, p.state, p.tier, p.city]);
}

function citiesPayload(cities: Json[] | null): Json {
  return cities === null ? null : cities.map((c) => [c.id, c.name, c.cc, c.lat, c.lon, c.peers]);
}

function isoSeconds(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function runHttp(req: Json): Promise<Json> {
  const options: Json = {
    baseUrl: req.base_url,
    sleep: async () => {},
    random: () => 0,
    env: {},
  };
  if (req.api_key) options.apiKey = req.api_key;
  if (req.max_retries !== undefined && req.max_retries !== null) options.maxRetries = req.max_retries;
  if (req.clock_now) {
    const ms = Date.parse(req.clock_now);
    options.now = () => ms;
  }
  const client = new PeerApiClient(options);
  const op: string = req.op;

  try {
    if (op === 'get_peer') {
      const result: Json = await client.getPeer(req.ip);
      return {
        ok: true,
        status: result.status,
        peer_ids: [result.peer.id],
        signals: signals([result.peer]),
        rows: rows([result.peer]),
        envelope: envelopePayload(result.envelope),
        rate_limit: rateLimitPayload(result.rateLimit),
        etag: result.etag ?? null,
        from_cache: result.fromCache,
        api_version: result.apiVersion,
      };
    }
    if (op === 'network_map_graph' || op === 'network_map_summary') {
      const options = networkMapOptions(req.query);
      const result: Json =
        op === 'network_map_graph'
          ? await client.networkMapGraph(options)
          : await client.networkMapSummary(options);
      const out: Json = { ok: true, status: result.status };
      if (op === 'network_map_graph') {
        out.peer_ids = result.nodes.map((n: Json) => n.id);
        out.edges = result.edges.map((e: Json) => [e.from, e.to, e.span]);
      }
      out.tiers = result.tiers.map((t: Json) => [t.tier, t.count]);
      out.node_count = result.nodeCount;
      out.edge_count = result.edgeCount;
      out.unreached = result.unreached;
      if (op === 'network_map_graph') {
        out.truncated = result.truncated;
        out.node_cities = result.nodes.map((n: Json) => n.city);
      } else {
        out.connectable = result.connectable;
        out.serves_mempool = result.servesMempool;
        out.auction = result.auction;
      }
      out.cities = citiesPayload(result.cities);
      out.attribution = result.attribution;
      out.operator_nodes_count = result.operatorNodes.count;
      out.envelope = mapEnvelopePayload(result.envelope);
      out.api_version = result.apiVersion;
      out.rate_limit = rateLimitPayload(result.rateLimit);
      out.etag = result.etag ?? null;
      out.from_cache = result.fromCache;
      return out;
    }
    if (op === 'list_peers_as_gossip_config') {
      const result: Json = await client.listPeersAsGossipConfig(buildQuery(req.query));
      return {
        ok: true,
        status: result.status,
        json_text: result.config.toJson(),
        api_version: result.apiVersion,
        rate_limit: rateLimitPayload(result.rateLimit),
        etag: result.etag ?? null,
        from_cache: result.fromCache,
      };
    }
    if (op === 'facets') {
      const result: Json = await client.facets(buildQuery(req.query));
      const facets: Json = {};
      for (const name of Object.keys(result.facets).sort()) {
        facets[name] = {
          available: result.facets[name].available,
          values: result.facets[name].values.map((v: Json) => [v.value, v.count]),
        };
      }
      return {
        ok: true,
        status: result.status,
        envelope: envelopePayload(result.envelope),
        total: result.total,
        facets,
        rate_limit: rateLimitPayload(result.rateLimit),
        etag: result.etag ?? null,
        from_cache: result.fromCache,
        api_version: result.apiVersion,
      };
    }
    if (op === 'list_peers' || op === 'list_peers_twice') {
      let result: Json = await client.listPeers(buildQuery(req.query, true));
      if (op === 'list_peers_twice') result = await client.listPeers(buildQuery(req.query, true));
      if (result.notModified) {
        return {
          ok: true,
          status: result.status,
          not_modified: true,
          etag: result.etag ?? null,
          rate_limit: rateLimitPayload(result.rateLimit),
          api_version: result.apiVersion,
        };
      }
      return {
        ok: true,
        status: result.status,
        peer_ids: result.peers.map((p: Json) => p.id),
        signals: signals(result.peers),
        rows: rows(result.peers),
        count: result.count,
        total: result.total,
        next_cursor: result.nextCursor,
        envelope: envelopePayload(result.envelope),
        rate_limit: rateLimitPayload(result.rateLimit),
        etag: result.etag ?? null,
        from_cache: result.fromCache,
        api_version: result.apiVersion,
      };
    }
    if (op === 'iter_peers') {
      const iterable = client.iterPeers(buildQuery(req.query), {
        maxItems: req.max_items ?? 10000,
      });
      const ids: string[] = [];
      for await (const peer of iterable) ids.push(peer.id);
      return { ok: true, peer_ids: ids, truncated: iterable.truncated };
    }
    return { ok: false, harness_error: `unknown op ${op}` };
  } catch (err) {
    if (err instanceof PeerApiError) {
      const error = errorPayload(err);
      if ((err as any).kind === 'RateLimited') {
        const e = err as any;
        if (req.clock_now) error.reset_at = e.resetAt === null ? null : isoSeconds(e.resetAt);
        error.signup_url = e.signupUrl ?? null;
      }
      return {
        ok: false,
        error,
        rate_limit: rateLimitPayload((err as any).rateLimit),
      };
    }
    return { ok: false, harness_error: String(err) };
  }
}

async function main(): Promise<void> {
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    const req = JSON.parse(line);
    let answer: Json;
    try {
      if (req.kind === 'gossip') answer = runGossip(req);
      else if (req.kind === 'query_url') answer = runQueryUrl(req);
      else if (req.kind === 'http') answer = await runHttp(req);
      else answer = { ok: false, harness_error: `unknown kind ${req.kind}` };
    } catch (err) {
      answer = { ok: false, harness_error: String(err) };
    }
    answer.id = req.id;
    process.stdout.write(JSON.stringify(answer) + '\n');
  }
}

await main();
