/**
 * HTTP behaviour against `fixtures/http/*.json` through an injected `fetch`
 * (CLIENT_SPEC.md §9(2)). 🔴 No network, no mock-server process, no clock.
 */
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { save } from '../src/gossip-fs.ts';

import {
  BadRequestError,
  ChainNotServedError,
  CursorExpiredError,
  FilterUnavailableError,
  GossipConfig,
  InvalidError,
  NotFoundError,
  PeerApiClient,
  RateLimitedError,
  ServerError,
  TruncatedError,
  UnauthorizedError,
  UnavailableError,
  DEFAULT_BASE_PATH,
  DEFAULT_BASE_URL,
  NO_RATE_LIMIT,
  PeerQuery,
  decodePeer,
  mempoolServers,
  networkMapQueryString,
  normalizeBasePath,
  notFull,
  peerQuery,
  sentries,
  tier1,
  type PeerApiClientOptions,
  type PeerListResult,
} from '../src/index.ts';
import {
  TEST_BASE_URL,
  fixtureByName,
  fixtureFetch,
  loadHttpFixtures,
  type FixtureFetchOptions,
} from './fixture-fetch.ts';

function client(
  fetchOpts: FixtureFetchOptions = {},
  clientOpts: Partial<PeerApiClientOptions> = {},
): PeerApiClient {
  return new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    fetch: fixtureFetch(fetchOpts),
    sleep: () => Promise.resolve(),
    now: () => Date.parse('2026-09-21T02:36:00Z'),
    random: () => 0.5,
    env: {},
    ...clientOpts,
  });
}

function page(result: PeerListResult | { notModified: true }): PeerListResult {
  assert.ok(!('notModified' in result), 'expected a body, got a 304 result');
  return result;
}

// -- the query surface ------------------------------------------------------

test('listPeers decodes the unfiltered page, the envelope and the rate-limit block', async () => {
  const res = page(await client().listPeers());
  assert.equal(res.count, 5);
  assert.equal(res.total, 5);
  assert.equal(res.envelope.available, true);
  assert.equal(res.envelope.stale, false);
  assert.equal(res.envelope.chain, 'Mainnet');
  assert.equal(res.envelope.next_sweep_at, '2026-09-21T02:40:00Z');
  assert.equal(res.status, 200);
  assert.equal(res.fromCache, false);
  assert.match(res.etag ?? '', /^"sha256-/);
  // §3.3: parsed off every response; a missing header is null, never 0.
  assert.deepEqual(res.rateLimit, {
    limit: 20,
    remaining: 17,
    reset: 42,
    policy: '"anon-min";q=20;w=60, "anon-day";q=2000;w=86400',
    retryAfter: null,
    retryAfterRaw: null,
  });
});

test('a peer decodes with three-state enums unfolded and unknown keys kept', async () => {
  const res = page(await client().listPeers());
  const sentry = res.peers.find((p) => p.id === '192.0.2.200');
  assert.ok(sentry !== undefined);
  assert.equal(sentry.tier, 0);
  assert.equal(sentry.connectable, 'no'); // a STRING, never a boolean
  assert.deepEqual([...sentry.labels], ['tier0', 'sentry', 'sentry:foundation']);
  const sg = res.peers.find((p) => p.id === '198.51.100.7');
  assert.equal(sg?.connectable, 'yes');
  assert.equal(sg?.region, null); // "not provided", never a value
  assert.deepEqual(sg?.extra, {});
  // geo fields
  const de = res.peers.find((p) => p.id === '192.0.2.10');
  assert.equal(de?.cc_confidence, 'high');
  assert.equal(de?.cc_source, 'db+rtt-confirmed');
  assert.equal(de?.org, 'Example Hosting');
  assert.equal(de?.uptime, 0.97);
  assert.deepEqual(de?.feeders, []);
  assert.equal(res.envelope.attribution, 'IP address data is powered by IPinfo (https://ipinfo.io), CC BY-SA 4.0');
  // 🔴 an all-null mempool/auction object is UNKNOWN, never false
  assert.deepEqual(sentry.mempool, {
    serves: null,
    last_checked: null,
    verdict: null,
    first_frame_ms: null,
    source: null,
  });
  assert.deepEqual(sentry.auction, { status: null, slot: null, since: null, last_bid_at: null });
});

test('API-Version is exposed on every result and every error, and never validated', async () => {
  const res = page(await client().listPeers());
  assert.equal(res.apiVersion, '1');
  const peer = await client().getPeer('192.0.2.10');
  assert.ok(!('notModified' in peer));
  assert.equal(peer.apiVersion, '1');
  await assert.rejects(client().getPeer('198.51.100.254'), (err: unknown) => {
    assert.ok(err instanceof NotFoundError);
    assert.equal(err.apiVersion, '1');
    return true;
  });
  // a client-side refusal had no response, so no header
  await assert.rejects(client().getPeer('nope'), (err: unknown) => {
    assert.ok(err instanceof InvalidError);
    assert.equal(err.apiVersion, null);
    return true;
  });
  // a different value is informational, never an error
  const v2 = new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    env: {},
    fetch: (() =>
      Promise.resolve(
        new Response(JSON.stringify({ version: 'v1', available: true, stale: false, peers: [] }), {
          status: 200,
          headers: { 'API-Version': '2' },
        }),
      )) as typeof fetch,
  });
  assert.equal(page(await v2.listPeers()).apiVersion, '2');
});

test('a peer that omits mempool/auction decodes them to the all-null objects', () => {
  const p = decodePeer({ id: '192.0.2.1', legacy_field: 7 });
  assert.deepEqual(p.mempool, {
    serves: null,
    last_checked: null,
    verdict: null,
    first_frame_ms: null,
    source: null,
  });
  assert.deepEqual(p.auction, { status: null, slot: null, since: null, last_bid_at: null });
  // a key this version does not model lands in extra
  assert.deepEqual(p.extra, { legacy_field: 7 });
  assert.equal(p.feeders, null);
  // unknown keys inside the sub-objects are ignored, not carried
  const q = decodePeer({ id: '192.0.2.1', mempool: { serves: false, novel: 1 }, auction: 'junk' });
  assert.equal(q.mempool.serves, false);
  assert.ok(!('novel' in q.mempool));
  assert.equal(q.auction.status, null);
});

test('owner query 1: peers by cc', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(peerQuery().cc(['DE'])));
  assert.deepEqual(calls, ['/peers?cc=DE']);
  assert.equal(res.peers.length, 2);
});

test('basePath: the default is empty (plain paths at the host root), held in one constant', () => {
  assert.equal(DEFAULT_BASE_PATH, '');
  assert.equal(client().basePath, '');
  assert.equal(DEFAULT_BASE_URL, 'https://api.hypervisor.gg');
  assert.equal(new PeerApiClient({ env: {} }).baseUrl, 'https://api.hypervisor.gg');
});

test('basePath: normalized per CLIENT_SPEC §1', () => {
  assert.equal(normalizeBasePath('peer-api/v1/'), '/peer-api/v1');
  assert.equal(normalizeBasePath('//peer-api/v1//'), '/peer-api/v1');
  assert.equal(normalizeBasePath('/'), '');
  assert.equal(normalizeBasePath(''), '');
});

test('basePath: a custom prefix is used by every route', async () => {
  const seen: string[] = [];
  const inner = fixtureFetch();
  // Record the real path, then map the custom prefix back onto the fixtures'.
  const rewriting: typeof fetch = (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url);
    seen.push(url.pathname);
    url.pathname = url.pathname.replace(/^\/peer-api\/v1/, '');
    return inner(url.href, init);
  };
  const c = new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    basePath: '/peer-api/v1/',
    fetch: rewriting,
    cache: null,
    env: {},
  });
  assert.equal(c.basePath, '/peer-api/v1');
  await c.listPeers(peerQuery().cc(['DE']));
  await c.getPeer('192.0.2.10');
  await c.facets();
  await c.listPeersAsGossipConfig(peerQuery().cc(['DE']));
  await c.networkMapGraph({ maxTier: 2 });
  await c.networkMapSummary();
  assert.deepEqual(seen, [
    '/peer-api/v1/peers',
    '/peer-api/v1/peers/192.0.2.10',
    '/peer-api/v1/peers/facets',
    '/peer-api/v1/peers',
    '/peer-api/v1/network-map/graph',
    '/peer-api/v1/network-map/summary',
  ]);
});

test('owner query 2: notFull({cc}) is accepting=true, an estimate not a guarantee', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(notFull({ cc: ['SG'] })));
  assert.deepEqual(calls, ['/peers?accepting=true&cc=SG']);
  assert.equal(res.total, 1);
  // 🪤 single-page on purpose: a next_cursor no fixture serves is a dead end,
  // and the cursor walk lives in peers_page_{1,2,3}_of_3.
  assert.equal(res.nextCursor, null);
});

test('owner query 3: tier1() is EXACT, never mapped onto max_tier', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(tier1()));
  assert.deepEqual(calls, ['/peers?tier=1']);
  assert.equal(res.total, 1);
  assert.equal(res.nextCursor, null);
});

test('owner query 4: sentries() uses label=sentry, not tier=0', async () => {
  const calls: string[] = [];
  page(await client({ calls }).listPeers(sentries()));
  assert.deepEqual(calls, ['/peers?label=sentry']);
});

test('query parameters are lexicographic and the CSV comma is percent-encoded', () => {
  const q = peerQuery().tier([1, null]).cc(['de', 'nl']).sort('random').limit(50);
  assert.equal(q.toQueryString(), 'cc=DE%2CNL&limit=50&sort=random&tier=1%2Cnull');
  // immutability: the original is untouched
  assert.equal(peerQuery().cc(['DE']).cc(['NL']).toQueryString(), 'cc=NL');
});

test('listPeers refuses format=gossip_config and names the helper (D-R)', async () => {
  const calls: string[] = [];
  await assert.rejects(
    client({ calls }).listPeers(peerQuery().cc(['DE']).format('gossip_config')),
    (err: unknown) =>
      err instanceof InvalidError &&
      err.parameter === 'format' &&
      /listPeersAsGossipConfig/.test(err.reason),
  );
  assert.deepEqual(calls, []);
});

test('🔴 golden: listPeersAsGossipConfig renders and saves exactly the expected bytes', async () => {
  const fx = fixtureByName('peers_format_gossip_config');
  const golden = fx.expect?.gossip_config_json_text;
  assert.ok(typeof golden === 'string');
  const calls: string[] = [];
  // the caller's format is overridden; a json format is not an error here
  const res = await client({ calls }).listPeersAsGossipConfig(
    peerQuery().cc(['DE']).format('json'),
  );
  assert.ok(!('notModified' in res));
  assert.deepEqual(calls, ['/peers?cc=DE&format=gossip_config']);
  assert.ok(res.config instanceof GossipConfig);
  assert.deepEqual(res.config.rootIps(), ['192.0.2.10', '192.0.2.11', '198.51.100.7']);
  assert.equal(res.apiVersion, '1');
  assert.equal(res.status, 200);
  assert.match(res.etag ?? '', /^"sha256-/);
  assert.equal(res.rateLimit.limit, 20);
  // the server emitted root_node_ips, try_new_peers, chain; we re-render in §6.3 order
  assert.equal(res.config.toJson(), golden);

  const dir = mkdtempSync(join(tmpdir(), 'hl-peers-golden-'));
  try {
    const path = join(dir, 'override_gossip_config.json');
    const saved = save(res.config, path);
    assert.equal(saved.written, true);
    assert.equal(readFileSync(path, 'utf8'), golden);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('listPeersAsGossipConfig sends a caller-set tryNewPeers', async () => {
  const calls: string[] = [];
  // no fixture serves this query; the URL is what is asserted
  await assert.rejects(
    client({ calls }).listPeersAsGossipConfig(peerQuery().cc(['DE']).tryNewPeers(false)),
  );
  assert.deepEqual(calls, ['/peers?cc=DE&format=gossip_config&try_new_peers=false']);
});

test('the returned config is a copy: mutating it never corrupts the ETag cache', async () => {
  const c = client();
  const first = await c.listPeersAsGossipConfig(peerQuery().cc(['DE']));
  assert.ok(!('notModified' in first));
  first.config.addRootIp('203.0.113.99');
  const second = await c.listPeersAsGossipConfig(peerQuery().cc(['DE']));
  assert.ok(!('notModified' in second));
  assert.equal(second.fromCache, true);
  assert.equal(second.config.hasRootIp('203.0.113.99'), false);
});

// -- mempool / auction ---------------------------------------------------

test('owner query 1 whole: mempoolServers().cc(DE) decodes typed mempool/auction', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(mempoolServers().cc(['DE'])));
  assert.deepEqual(calls, ['/peers?cc=DE&serves_mempool=true']);
  assert.deepEqual(
    res.peers.map((p) => p.id),
    ['192.0.2.10'],
  );
  const p = res.peers[0]!;
  assert.deepEqual(p.mempool, {
    serves: true,
    last_checked: '2026-09-21T02:30:00Z',
    verdict: 'serves',
    first_frame_ms: 412,
    source: 'probe',
  });
  assert.deepEqual(p.auction, {
    status: 'holds_slot',
    slot: 3,
    since: '2026-09-20T11:00:00Z',
    last_bid_at: '2026-09-20T10:59:12Z',
  });
});

test('serves_mempool=null selects the peers whose serves is unknown (null, not false)', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(peerQuery().servesMempool([null])));
  assert.deepEqual(calls, ['/peers?serves_mempool=null']);
  assert.equal(res.peers.length, 2);
  for (const p of res.peers) {
    assert.equal(p.mempool.serves, null);
    assert.notEqual(p.mempool.serves, false);
  }
});

test('auction=holds_slot,bidding decodes both standings', async () => {
  const calls: string[] = [];
  const res = page(
    await client({ calls }).listPeers(peerQuery().auction(['holds_slot', 'bidding'])),
  );
  assert.deepEqual(calls, ['/peers?auction=holds_slot%2Cbidding']);
  assert.deepEqual(
    res.peers.map((p) => [p.id, p.auction.status, p.auction.slot, p.mempool.source]),
    [
      ['192.0.2.10', 'holds_slot', 3, 'probe'],
      ['198.51.100.7', 'bidding', null, 'relay'],
    ],
  );
});

test('400 filter-unavailable on serves_mempool is FilterUnavailable, never an empty list', async () => {
  await assert.rejects(
    client({ scenario: 'mempool-unavailable' }).listPeers(peerQuery().servesMempool('true')),
    (err: unknown) => {
      assert.ok(err instanceof FilterUnavailableError);
      assert.equal(err.filter, 'serves_mempool');
      assert.equal(err.apiVersion, '1');
      return true;
    },
  );
});

test('the removed mempool= filter is a plain BadRequest (filter-removed)', async () => {
  // The builder does not model `mempool` (D-O); send it as a raw parameter.
  class Raw extends PeerQuery {
    override toParams(): [string, string][] {
      return [['mempool', 'serving']];
    }
  }
  await assert.rejects(client().listPeers(new Raw()), (err: unknown) => {
    assert.ok(err instanceof BadRequestError);
    assert.ok(!(err instanceof FilterUnavailableError));
    assert.equal(err.kind, 'BadRequest');
    assert.equal(err.parameter, 'mempool');
    return true;
  });
});

test('builder: country, serves_mempool, auction, try_new_peers serialize per §2.1', () => {
  assert.equal(
    peerQuery()
      .country(['jp', 'de'])
      .servesMempool([true, null])
      .auction(['bid_before', null])
      .tryNewPeers(true)
      .state(['responsive', 'open'])
      .toQueryString(),
    'auction=bid_before%2Cnull&country=JP%2CDE&serves_mempool=true%2Cnull' +
      '&state=responsive%2Copen&try_new_peers=true',
  );
  assert.equal(peerQuery().servesMempool('true,null').toQueryString(), 'serves_mempool=true%2Cnull');
  assert.equal(peerQuery().servesMempool([false]).toQueryString(), 'serves_mempool=false');
  assert.equal(mempoolServers().toQueryString(), 'serves_mempool=true');
});

test('builder: serves_mempool, auction and state are closed enums, case-sensitive', () => {
  for (const [q, param] of [
    [peerQuery().servesMempool('yes'), 'serves_mempool'],
    [peerQuery().servesMempool('True'), 'serves_mempool'],
    [peerQuery().auction(['holding' as never]), 'auction'],
    [peerQuery().auction('HOLDS_SLOT'), 'auction'],
    [peerQuery().state(['FULL']), 'state'],
  ] as const) {
    assert.throws(
      () => {
        q.validate();
      },
      (err: unknown) => err instanceof InvalidError && err.parameter === param,
    );
  }
  peerQuery().state(['responsive', 'open', 'full', 'syncing', 'unreachable', 'unknown']).validate();
  peerQuery().auction(['holds_slot', 'bidding', 'bid_before', null]).validate();
  peerQuery().servesMempool([true, false, null]).validate();
});

// -- network map ------------------------------------------------------------

test('networkMapGraph decodes the graph; nodes use the Peer decoder', async () => {
  const calls: string[] = [];
  const res = await client({ calls }).networkMapGraph({ maxTier: 2 });
  assert.ok(!('notModified' in res));
  assert.deepEqual(calls, ['/network-map/graph?max_tier=2']);
  assert.equal(res.envelope.available, true);
  assert.equal(res.envelope.generated_at, '2026-09-21T02:35:00Z');
  assert.equal(res.nodeCount, 4);
  assert.equal(res.edgeCount, 3);
  assert.equal(res.tierCount, 3);
  assert.equal(res.unreached, 0);
  assert.equal(res.truncated, false);
  assert.deepEqual(res.operatorNodes, { count: null });
  assert.deepEqual(res.tiers, [
    { tier: 0, count: 1 },
    { tier: 1, count: 2 },
    { tier: 2, count: 1 },
  ]);
  assert.deepEqual(res.edges[0], { from: '192.0.2.200', to: '192.0.2.10', span: 1 });
  assert.equal(res.nodes.length, 4);
  assert.equal(res.nodes[0]?.id, '192.0.2.200');
  assert.equal(res.nodes[0]?.mempool.serves, null);
  assert.deepEqual([...(res.nodes[0]?.labels ?? [])], ['tier0', 'sentry', 'sentry:foundation']);
  assert.equal(res.apiVersion, '1');
  assert.equal(res.rateLimit.remaining, 17);
  // 1.1.0: city clusters + the combined attribution; a node carries its cluster id
  assert.deepEqual(res.cities, [
    {
      id: 'DE-frankfurt-am-main',
      name: 'Frankfurt am Main',
      cc: 'DE',
      lat: 50.1,
      lon: 8.7,
      peers: 2,
      tiers: { '1': 1, '2': 1 },
    },
  ]);
  assert.match(res.attribution ?? '', /IPinfo.*; IP Geolocation by DB-IP/);
  assert.deepEqual(
    res.nodes.map((n) => n.city),
    [null, 'DE-frankfurt-am-main', null, 'DE-frankfurt-am-main'],
  );
});

test('networkMapGraph: mempool/auction filters on the wire; absent cities stay null, not []', async () => {
  const calls: string[] = [];
  const res = await client({ calls }).networkMapGraph({
    servesMempool: [true],
    auction: ['holds_slot'],
  });
  assert.ok(!('notModified' in res));
  assert.deepEqual(calls, ['/network-map/graph?auction=holds_slot&serves_mempool=true']);
  assert.deepEqual(res.nodes.map((n) => n.id), ['192.0.2.10']);
  assert.deepEqual(res.edges, []);
  assert.equal(res.cities, null, 'no cities key = null, distinct from an empty list');
  assert.equal(res.attribution, null);
  assert.equal(res.nodes[0]?.city, null);
  // pre-joined CSV is accepted too
  const again: string[] = [];
  await client({ calls: again }).networkMapGraph({ servesMempool: 'true', auction: 'holds_slot' });
  assert.deepEqual(again, calls);
});

test('network-map serves_mempool/auction are closed enums, refused before any request', async () => {
  for (const [opts, parameter] of [
    [{ servesMempool: 'yes' }, 'serves_mempool'],
    [{ auction: 'holding' }, 'auction'],
    [{ auction: ['HOLDS_SLOT'] }, 'auction'],
  ] as const) {
    const calls: string[] = [];
    const c = client({ calls });
    await assert.rejects(
      c.networkMapGraph(opts as never),
      (err: unknown) => err instanceof InvalidError && err.parameter === parameter,
    );
    await assert.rejects(
      c.networkMapSummary(opts as never),
      (err: unknown) => err instanceof InvalidError && err.parameter === parameter,
    );
    assert.deepEqual(calls, [], 'nothing was sent');
  }
});

test('peers_state_listening: `listening` is selectable and decodes as-is', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(peerQuery().state(['listening'])));
  assert.deepEqual(calls, ['/peers?state=listening']);
  const [p] = res.peers;
  assert.equal(p?.state, 'listening');
  assert.equal(p?.tier, null, 'never placed: null, not 0');
  assert.equal(p?.connectable, 'unknown');
  assert.equal(p?.latest_height, null);
  assert.equal(p?.city, null);
});

test('the per-address flood 429 is problem+json and maps to RateLimited like the tier 429', async () => {
  const calls: string[] = [];
  const c = client({ scenario: 'rate-limited-flood', calls }, { maxRetries: 0 });
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.retryAfter, 1);
    assert.equal(err.rateLimit.limit, 100);
    assert.equal(err.rateLimit.remaining, 0);
    assert.equal(err.rateLimit.policy, '100;w=1');
    assert.equal(err.problem?.title, 'Too many requests');
    assert.equal(err.signupUrl, 'https://hypervisor.gg/docs/peer-api/authentication');
    return true;
  });
  assert.equal(calls.length, 1);
});

test('networkMapSummary carries the connectable buckets and no nodes/edges', async () => {
  const res = await client().networkMapSummary();
  assert.ok(!('notModified' in res));
  assert.deepEqual(res.connectable, { yes: 3, no: 1, unknown: 0 });
  assert.deepEqual(res.servesMempool, { true: 2, false: 1, null: 1 });
  assert.deepEqual(res.auction, { holds_slot: 1, bidding: 1, bid_before: 1, null: 1 });
  assert.equal(res.cities?.[0]?.id, 'DE-frankfurt-am-main');
  assert.match(res.attribution ?? '', /DB-IP/);
  assert.equal(res.nodeCount, 4);
  assert.ok(!('nodes' in res));
  assert.ok(!('edges' in res));
  assert.ok(!('truncated' in res));
  // the ETag cache covers the map too
  const c = client();
  await c.networkMapSummary();
  const again = await c.networkMapSummary();
  assert.ok(!('notModified' in again));
  assert.equal(again.fromCache, true);
});

test('network-map options serialize sorted and validate the closed connectable enum', () => {
  assert.equal(
    networkMapQueryString({
      showUnreached: false,
      hideUnreachable: true,
      connectable: ['yes', 'unknown'],
      cc: 'DE',
      operator: 'Example Operator',
      label: 'sentry',
      maxTier: 0,
    }),
    'cc=DE&connectable=yes%2Cunknown&hide_unreachable=true&label=sentry&max_tier=0' +
      '&operator=Example%20Operator&show_unreached=false',
  );
  assert.equal(networkMapQueryString(), '');
  assert.equal(
    networkMapQueryString({ servesMempool: [true, null], auction: ['holds_slot', 'bidding'] }),
    'auction=holds_slot%2Cbidding&serves_mempool=true%2Cnull',
  );
  assert.equal(networkMapQueryString({ servesMempool: [], auction: [] }), '');
  assert.throws(
    () => networkMapQueryString({ connectable: 'maybe' }),
    (err: unknown) => err instanceof InvalidError && err.parameter === 'connectable',
  );
  assert.throws(
    () => networkMapQueryString({ maxTier: -1 }),
    (err: unknown) => err instanceof InvalidError && err.parameter === 'max_tier',
  );
});

test('network map: available:false is returned as-is, never an error', async () => {
  const c = new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    env: {},
    fetch: (() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ version: 'v1', available: false, stale: false, generated_at: null }),
          { status: 200 },
        ),
      )) as typeof fetch,
  });
  const res = await c.networkMapGraph();
  assert.ok(!('notModified' in res));
  assert.equal(res.envelope.available, false);
  assert.deepEqual(res.nodes, []);
  assert.equal(res.nodeCount, null, 'absent is null, never 0');
});

// -- pagination -------------------------------------------------------------

test('iterPeers walks the 3-page cursor fixture and stops on a null next_cursor', async () => {
  const calls: string[] = [];
  const walk = client({ calls }).iterPeers(peerQuery().limit(2).sort('tier'));
  const ids: string[] = [];
  for await (const peer of walk) ids.push(peer.id);
  assert.deepEqual(ids, [
    '203.0.113.1',
    '203.0.113.2',
    '203.0.113.3',
    '203.0.113.4',
    '203.0.113.5',
  ]);
  assert.equal(calls.length, 3);
  assert.ok(calls[1]?.includes('cursor=cursor-page-2'));
  assert.equal(walk.truncated, false);
  assert.equal(walk.seen, 5);
});

test('iterPeers honours maxItems and reports truncation, or raises on demand', async () => {
  const walk = client().iterPeers(peerQuery().limit(2).sort('tier'), { maxItems: 3 });
  const ids: string[] = [];
  for await (const peer of walk) ids.push(peer.id);
  assert.deepEqual(ids, ['203.0.113.1', '203.0.113.2', '203.0.113.3']);
  assert.equal(walk.truncated, true);

  const raising = client().iterPeers(peerQuery().limit(2).sort('tier'), {
    maxItems: 3,
    onTruncate: 'raise',
  });
  await assert.rejects(
    (async () => {
      const drained: unknown[] = [];
      for await (const peer of raising) drained.push(peer);
    })(),
    (err: unknown) => err instanceof TruncatedError && err.seen === 3,
  );
});

test('available:false stops iterPeers without raising and is never an error', async () => {
  const c = client({ scenario: 'unavailable' });
  const res = page(await c.listPeers());
  assert.equal(res.envelope.available, false);
  assert.deepEqual([...res.peers], []);

  const walk = c.iterPeers();
  const seen = [];
  for await (const peer of walk) seen.push(peer);
  assert.equal(seen.length, 0);
  assert.equal(walk.envelope?.available, false);
});

test('stale:true is surfaced, and available is checked before it', async () => {
  const res = page(await client({ scenario: 'stale' }).listPeers());
  assert.equal(res.envelope.available, true);
  assert.equal(res.envelope.stale, true);
});

// -- single peer and facets -------------------------------------------------

test('getPeer returns the dossier, and a 404 stays distinct from a 400', async () => {
  const res = await client().getPeer('192.0.2.10');
  assert.ok(!('notModified' in res));
  assert.equal(res.peer.id, '192.0.2.10');
  assert.deepEqual([...(res.peer.feeders ?? [])], ['192.0.2.200']);

  await assert.rejects(
    client().getPeer('198.51.100.254'),
    (err: unknown) => err instanceof NotFoundError && err.kind === 'NotFound',
  );
});

test('a malformed IP is refused locally, so a typo spends no quota', async () => {
  const calls: string[] = [];
  await assert.rejects(
    client({ calls }).getPeer('not-an-ip'),
    (err: unknown) => err instanceof InvalidError && err.parameter === 'ip',
  );
  await assert.rejects(
    client({ calls }).getPeer('2001:db8::1'),
    (err: unknown) => err instanceof InvalidError && /IPv4-only/.test(err.message),
  );
  assert.deepEqual(calls, [], 'nothing may be sent for a locally-invalid IP');
});

test('facets keeps "not collected" distinct from "collected and empty"', async () => {
  const res = await client().facets();
  assert.ok(!('notModified' in res));
  assert.equal(res.total, 912);
  assert.equal(res.isAvailable('serves_mempool'), true);
  assert.equal(res.isAvailable('auction'), true);
  assert.deepEqual(
    res.facets['serves_mempool']?.values.map((v) => v.value),
    ['true', 'false', 'null'],
  );
  assert.equal(res.geoCoverage?.['source'], 'dbip-lite');
  assert.equal(res.geoCoverage?.['peers'], 912);
  assert.equal(res.envelope.attribution, 'IP address data is powered by IPinfo (https://ipinfo.io), CC BY-SA 4.0');
  assert.equal(res.apiVersion, '1');
  assert.equal(res.isAvailable('cc'), true);
  assert.equal(res.isAvailable('asn'), false);
  assert.equal(res.facets['asn']?.available, false);
  assert.deepEqual([...(res.facets['asn']?.values ?? [])], []);
  // the selectable absent-value member is the literal string "null"
  assert.ok(res.facets['tier']?.values.some((v) => v.value === 'null' && v.count === 110));
});

test('facets refuses exactly the parameters it would silently ignore', async () => {
  for (const [q, param] of [
    [peerQuery().cc(['DE']).sort('tier'), 'sort'],
    [peerQuery().limit(5), 'limit'],
    [peerQuery().cursor('c'), 'cursor'],
    [peerQuery().fields(['id']), 'fields'],
    [peerQuery().format('json'), 'format'],
    [peerQuery().tryNewPeers(true), 'try_new_peers'],
  ] as const) {
    const calls: string[] = [];
    await assert.rejects(
      client({ calls }).facets(q),
      (err: unknown) => err instanceof InvalidError && err.parameter === param,
    );
    assert.deepEqual(calls, []);
  }
});

test('facets accepts every filter and sends it', async () => {
  const calls: string[] = [];
  const q = peerQuery()
    .cc(['DE'])
    .label(['sentry'])
    .servesMempool([true])
    .auction(['holds_slot'])
    .country(['DE'])
    .state(['responsive']);
  // no fixture serves this combination: the URL is what is asserted
  await assert.rejects(client({ calls }).facets(q), (err: unknown) => err instanceof NotFoundError);
  assert.deepEqual(calls, [
    '/peers/facets?auction=holds_slot&cc=DE&country=DE&label=sentry&serves_mempool=true&state=responsive',
  ]);
});

// -- caching ----------------------------------------------------------------

test('a cached ETag makes the second call a 304 that returns the stored body', async () => {
  const calls: string[] = [];
  const c = client({ calls });
  const first = page(await c.listPeers());
  assert.equal(first.fromCache, false);

  const second = page(await c.listPeers());
  assert.equal(second.status, 304);
  assert.equal(second.fromCache, true);
  assert.equal(second.peers.length, 5);
  // §4.5: the rate-limit block comes from the 304 response, not the stored one
  assert.equal(second.rateLimit.limit, 20);
  assert.equal(calls.length, 2, 'the 304 was a real request, just a free one');
});

test('a caller-supplied If-None-Match answers NotModified instead of a body', async () => {
  const etag = fixtureByName('peers_304_not_modified').response.headers?.['ETag'];
  assert.ok(etag !== undefined);
  const res = await client().listPeers(peerQuery(), { ifNoneMatch: etag });
  assert.ok('notModified' in res);
  assert.equal(res.status, 304);
  assert.equal(res.etag, etag);
});

test('the cache key separates anonymous from keyed calls', async () => {
  const c = client();
  const keyed = new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    apiKey: 'hlp_live_aaaaaaaaaaaaaaaaaaaaaaaaaa',
    fetch: fixtureFetch(),
    env: {},
  });
  page(await c.listPeers());
  assert.equal(c.cache?.size, 1);
  assert.equal(keyed.cache?.size, 0, 'each client has its own cache, never a global one');
});

// -- the error taxonomy -----------------------------------------------------

test('400 filter-unavailable is its own variant and is never an empty list', async () => {
  await assert.rejects(client().listPeers(peerQuery().asn(['AS24940'])), (err: unknown) => {
    assert.ok(err instanceof FilterUnavailableError);
    assert.ok(err instanceof BadRequestError, 'the 400 family must catch it');
    assert.equal(err.kind, 'FilterUnavailable');
    assert.equal(err.filter, 'asn');
    assert.equal(err.rateLimit.limit, 20);
    return true;
  });
  await assert.rejects(
    client().listPeers(peerQuery().region(['aws:eu-central-1'])),
    (err: unknown) => err instanceof FilterUnavailableError && err.filter === 'region',
  );
});

test('the other 400s discriminate on problem.type, never on prose', async () => {
  await assert.rejects(
    client().listPeers(peerQuery().chain('Testnet')),
    (err: unknown) =>
      err instanceof ChainNotServedError && err.allowed?.[0] === 'Mainnet',
  );
  await assert.rejects(
    client().listPeers(peerQuery().cursor('cursor-from-a-previous-sweep')),
    (err: unknown) => err instanceof CursorExpiredError && err.kind === 'CursorExpired',
  );
  await assert.rejects(
    client().listPeers(peerQuery().connectable('yess')),
    // a closed-enum member is refused locally before it can spend a unit
    (err: unknown) => err instanceof InvalidError && err.parameter === 'connectable',
  );
});

test('an unrecognised enum that reaches the server is a plain BadRequest', async () => {
  // The builder refuses `connectable=yess` locally (§2.2(5)), so to exercise the
  // 400 mapping itself we hand the client a query that skips that check — the
  // shape a *future* enum member would have against today's client.
  const raw = {
    validate(): void {},
    toQueryString: (): string => 'connectable=yess',
    toObject: (): Record<string, string> => ({ connectable: 'yess' }),
    parameterNames: (): string[] => ['connectable'],
  } as unknown as ReturnType<typeof peerQuery>;
  await assert.rejects(client().listPeers(raw), (err: unknown) => {
    assert.ok(err instanceof BadRequestError);
    assert.equal(err.kind, 'BadRequest');
    assert.equal(err.parameter, 'connectable');
    assert.deepEqual([...(err.allowed ?? [])], ['yes', 'no', 'unknown']);
    return true;
  });
});

test('401 means a key was supplied and is invalid; anonymous is not a 401', async () => {
  const c = new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    apiKey: 'hlp_live_invalidkey0000000000',
    fetch: fixtureFetch(),
    env: {},
  });
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof UnauthorizedError);
    // 🔴 the key itself never appears in the error
    assert.ok(!err.message.includes('invalidkey'));
    return true;
  });
});

test('🔴 retries are OFF by default: one 429 is one request, with resetAt from the clock', async () => {
  const calls: string[] = [];
  const now = Date.parse('2026-09-21T02:36:00Z');
  const c = client({ scenario: 'rate-limited', calls }, { now: () => now });
  assert.equal(c.maxRetries, 0);
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.retryAfter, 42);
    assert.equal(err.resetAt?.toISOString(), '2026-09-21T02:36:42.000Z', 'now + retry_after');
    assert.equal(err.signupUrl, 'https://hypervisor.gg/docs/peer-api/authentication');
    assert.equal(err.apiVersion, '1');
    return true;
  });
  assert.equal(calls.length, 1);
});

test('resetAt falls back to now + RateLimit-Reset, else null', () => {
  const rl = { ...NO_RATE_LIMIT, reset: 30 };
  const now = Date.parse('2026-09-21T00:00:00Z');
  assert.equal(
    new RateLimitedError('x', { rateLimit: rl, now }).resetAt?.toISOString(),
    '2026-09-21T00:00:30.000Z',
  );
  assert.equal(new RateLimitedError('x', { rateLimit: NO_RATE_LIMIT, now }).resetAt, null);
  assert.equal(new RateLimitedError('x', { rateLimit: rl }).resetAt, null, 'no clock, no guess');
});

test('429 carries every header, the signup url, and is retried within the cap when opted in', async () => {
  const calls: string[] = [];
  const c = client({ scenario: 'rate-limited', calls }, { maxRetries: 3 });
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.retryAfter, 42);
    assert.equal(err.retryAfterRaw, '42');
    assert.equal(err.remaining, 0);
    assert.equal(err.reset, 42);
    assert.equal(err.limit, 20);
    assert.equal(err.signupUrl, 'https://hypervisor.gg/docs/peer-api/authentication');
    // 🔴 the prose figures in `detail` are never parsed
    assert.match(err.problem?.detail ?? '', /120\/minute/);
    return true;
  });
  assert.equal(calls.length, 4, '1 attempt + 3 retries, all honouring Retry-After');
});

test('D-I: a Retry-After above the cap raises at once rather than blocking', async () => {
  const calls: string[] = [];
  const c = client({ scenario: 'rate-limited-long', calls });
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.retryAfter, 3600);
    return true;
  });
  assert.equal(calls.length, 1, 'no sleep, no retry: the caller decides whether to wait');
});

test('🔴 503 is Unavailable, and by default one 503 is exactly one request', async () => {
  const calls: string[] = [];
  const c = client({ scenario: 'producer-unavailable', calls });
  await assert.rejects(c.listPeers(), (err: unknown) => err instanceof UnavailableError);
  assert.equal(calls.length, 1);
});

test('503 is retried when maxRetries is set explicitly', async () => {
  const calls: string[] = [];
  const c = client({ scenario: 'producer-unavailable', calls }, { maxRetries: 3 });
  await assert.rejects(c.listPeers(), (err: unknown) => err instanceof UnavailableError);
  assert.equal(calls.length, 4);
});

test('D-K: a 500 with a non-problem body maps on status alone', async () => {
  const c = client({ scenario: 'server-error' });
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof ServerError);
    assert.equal(err.status, 500);
    assert.equal(err.problem, null);
    assert.equal(err.bodyExcerpt, 'Internal Server Error');
    return true;
  });
});

test('§5.2: every 429 fixture resolves Retry-After to the seconds it expects', async () => {
  const rateLimited = loadHttpFixtures().filter((f) => f.response.status === 429);
  assert.equal(rateLimited.length, 5, 'five 429 fixtures: numeric, over-cap, date, junk, flood');

  for (const fx of rateLimited) {
    const scenario = fx.request.headers?.['X-Mock-Scenario'];
    assert.ok(scenario !== undefined, `${fx.name} needs a scenario header`);
    const expected = fx.expect?.retry_after_seconds;
    assert.ok(expected !== undefined, `${fx.name} must state expect.retry_after_seconds`);

    // 🔴 The clock is injected, never read from the wall: an HTTP-date fixture
    // says exactly which instant to pretend it is.
    const nowIso = fx.clock?.now ?? '2026-09-21T02:36:00Z';
    const c = client({ scenario }, { now: () => Date.parse(nowIso), maxRetries: 0 });

    await assert.rejects(c.listPeers(), (err: unknown) => {
      assert.ok(err instanceof RateLimitedError, fx.name);
      assert.equal(err.retryAfter, expected, `${fx.name} retry_after_seconds`);
      assert.equal(err.retryAfterRaw, fx.response.headers?.['Retry-After'] ?? null);
      assert.equal(err.signupUrl, 'https://hypervisor.gg/docs/peer-api/authentication');
      // resetAt = injected now + retry_after, else + RateLimit-Reset
      const reset = Number(fx.response.headers?.['RateLimit-Reset']);
      const wait = expected ?? reset;
      assert.equal(err.resetAt?.getTime(), Date.parse(nowIso) + wait * 1000, `${fx.name} resetAt`);
      return true;
    });
  }
});

test('an unparseable Retry-After is absent, so the 429 backs off instead', async () => {
  const calls: string[] = [];
  const c = client({ scenario: 'rate-limited-bad-retry-after', calls }, { maxRetries: 3 });
  await assert.rejects(c.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.retryAfter, null, 'treated as absent, never guessed');
    assert.equal(err.retryAfterRaw, 'soon-ish');
    return true;
  });
  assert.equal(calls.length, 4, 'backoff still retries: 1 attempt + 3 retries');
});

test('an HTTP-date Retry-After is honoured as a delay, not raised on', async () => {
  const calls: string[] = [];
  const sleeps: number[] = [];
  const c = client(
    { scenario: 'rate-limited-http-date', calls },
    {
      now: () => Date.parse('2026-09-21T02:35:00Z'),
      sleep: (ms) => (sleeps.push(ms), Promise.resolve()),
      maxRetries: 3,
    },
  );
  await assert.rejects(c.listPeers(), (err: unknown) => err instanceof RateLimitedError);
  assert.equal(calls.length, 4);
  assert.deepEqual(sleeps, [60_000, 60_000, 60_000], '60 s from the date, under the cap');
});

// -- network=testnet (beta), CLIENT_SPEC §10 D-U ------------------------------

/** `[id, version, cc, asn, org, region]` of the four testnet rows. */
const TESTNET_ROWS = [
  ['192.0.2.50', '1550', 'JP', 'AS64502', 'Example Cloud', 'aws:ap-northeast-1'],
  ['198.51.100.60', '1550', 'US', 'AS64503', 'Example Hosting US', null],
  ['203.0.113.70', '1549', 'FI', 'AS64500', 'Example Hosting', null],
  ['203.0.113.71', null, null, null, null, null],
];

function rowsOf(peers: readonly { id: string; version: string | null; cc: string | null; asn: string | null; org: string | null; region: string | null }[]): unknown[] {
  return peers.map((p) => [p.id, p.version, p.cc, p.asn, p.org, p.region]);
}

test('testnet: listPeers sends network=testnet and decodes network, chain, version and geo', async () => {
  const calls: string[] = [];
  const res = page(await client({ calls }).listPeers(peerQuery().network('testnet')));
  assert.deepEqual(calls, ['/peers?network=testnet']);
  assert.equal(res.envelope.network, 'testnet');
  assert.equal(res.envelope.chain, 'Testnet');
  assert.equal(res.envelope.available, true);
  assert.deepEqual(rowsOf(res.peers), TESTNET_ROWS);
  // a hardfork version is an opaque string, never a number
  assert.equal(typeof res.peers[0]?.version, 'string');
  assert.equal(res.peers[0]?.cc_confidence, 'high');
  assert.equal(res.peers[0]?.cc_source, 'cloud-range');
  assert.equal(res.peers[3]?.state, 'listening');
  assert.equal(res.peers[3]?.tier, null);
});

test('testnet: iterPeers carries network on every page', async () => {
  const calls: string[] = [];
  const peers = await client({ calls }).listAllPeers(peerQuery().network('testnet'));
  assert.deepEqual(calls, ['/peers?network=testnet']);
  assert.equal(peers.length, 4);
});

test('testnet: getPeer takes network as a per-call option', async () => {
  const calls: string[] = [];
  const res = await client({ calls }).getPeer('192.0.2.50', { network: 'testnet' });
  assert.ok(!('notModified' in res));
  assert.deepEqual(calls, ['/peers/192.0.2.50?network=testnet']);
  assert.equal(res.envelope.network, 'testnet');
  assert.equal(res.envelope.chain, 'Testnet');
  assert.deepEqual(rowsOf([res.peer]), [TESTNET_ROWS[0]]);
});

test('testnet: facets accepts network as a filter and decodes the version facet', async () => {
  const calls: string[] = [];
  const res = await client({ calls }).facets(peerQuery().network('testnet'));
  assert.ok(!('notModified' in res));
  assert.deepEqual(calls, ['/peers/facets?network=testnet']);
  assert.equal(res.envelope.network, 'testnet');
  assert.equal(res.envelope.chain, 'Testnet');
  assert.deepEqual(
    res.facets['version']?.values.map((v) => [v.value, v.count]),
    [['1550', 2], ['1549', 1], ['null', 1]],
  );
  assert.equal(res.isAvailable('label'), false);
});

test('testnet: the network map takes network as an option and decodes versions', async () => {
  const calls: string[] = [];
  const c = client({ calls });
  const graph = await c.networkMapGraph({ network: 'testnet' });
  assert.ok(!('notModified' in graph));
  assert.equal(graph.envelope.network, 'testnet');
  assert.deepEqual(rowsOf(graph.nodes), TESTNET_ROWS);
  assert.equal(graph.cities, null);
  const summary = await c.networkMapSummary({ network: 'testnet' });
  assert.ok(!('notModified' in summary));
  assert.equal(summary.envelope.network, 'testnet');
  assert.equal(summary.nodeCount, 4);
  assert.deepEqual(calls, ['/network-map/graph?network=testnet', '/network-map/summary?network=testnet']);
});

test('testnet: network-not-served is a BadRequest with parameter "network", not a new error', async () => {
  await assert.rejects(
    client({ scenario: 'network-not-served' }).listPeers(peerQuery().network('testnet')),
    (err: unknown) => {
      assert.ok(err instanceof BadRequestError);
      assert.equal(err.kind, 'BadRequest');
      assert.equal(err.parameter, 'network');
      assert.deepEqual([...(err.allowed ?? [])], ['mainnet', 'testnet']);
      return true;
    },
  );
});

test('testnet: an out-of-enum network is Invalid before any request, on every route', async () => {
  for (const bad of ['Testnet', 'TESTNET', 'devnet', '']) {
    const calls: string[] = [];
    const c = client({ calls });
    const isInvalid = (err: unknown): boolean =>
      err instanceof InvalidError && err.parameter === 'network';
    await assert.rejects(c.listPeers(peerQuery().network(bad as never)), isInvalid);
    await assert.rejects(c.listAllPeers(peerQuery().network(bad as never)), isInvalid);
    await assert.rejects(c.facets(peerQuery().network(bad as never)), isInvalid);
    await assert.rejects(c.listPeersAsGossipConfig(peerQuery().network(bad as never)), isInvalid);
    await assert.rejects(c.getPeer('192.0.2.50', { network: bad as never }), isInvalid);
    await assert.rejects(c.networkMapGraph({ network: bad as never }), isInvalid);
    await assert.rejects(c.networkMapSummary({ network: bad as never }), isInvalid);
    assert.deepEqual(calls, [], `${JSON.stringify(bad)} reached the wire`);
    assert.throws(
      () => client({}, { network: bad as never }),
      (err: unknown) => err instanceof InvalidError && err.parameter === 'network',
    );
  }
});

test('testnet: the client-level default applies to every route, and a per-call value wins', async () => {
  const calls: string[] = [];
  const c = client({ calls }, { network: 'testnet' });
  assert.equal(c.network, 'testnet');
  const res = page(await c.listPeers());
  assert.equal(res.envelope.network, 'testnet');
  await c.getPeer('192.0.2.50');
  await c.facets();
  await c.networkMapGraph();
  await c.networkMapSummary();
  assert.equal((await c.listAllPeers()).length, 4);
  // no testnet gossip_config fixture: only the URL matters here
  await c.listPeersAsGossipConfig().catch(() => undefined);
  assert.deepEqual(calls, [
    '/peers?network=testnet',
    '/peers/192.0.2.50?network=testnet',
    '/peers/facets?network=testnet',
    '/network-map/graph?network=testnet',
    '/network-map/summary?network=testnet',
    '/peers?network=testnet',
    '/peers?format=gossip_config&network=testnet',
  ]);

  // per-call override back to mainnet: sent explicitly, as the caller wrote it
  calls.length = 0;
  await c.listPeers(peerQuery().network('mainnet')).catch(() => undefined);
  await c.getPeer('192.0.2.10', { network: 'mainnet' }).catch(() => undefined);
  await c.facets(peerQuery().network('mainnet')).catch(() => undefined);
  await c.networkMapGraph({ network: 'mainnet' }).catch(() => undefined);
  await c.networkMapSummary({ network: 'mainnet' }).catch(() => undefined);
  assert.deepEqual(calls, [
    '/peers?network=mainnet',
    '/peers/192.0.2.10?network=mainnet',
    '/peers/facets?network=mainnet',
    '/network-map/graph?network=mainnet',
    '/network-map/summary?network=mainnet',
  ]);

  // and the reverse: a mainnet-default client asked for testnet per call
  const calls2: string[] = [];
  const m = client({ calls: calls2 }, { network: 'mainnet' });
  const t = page(await m.listPeers(peerQuery().network('testnet')));
  assert.equal(t.envelope.network, 'testnet');
  assert.deepEqual(calls2, ['/peers?network=testnet']);

  // an explicit `network: null` is the same as unset
  assert.equal(client({}, { network: null }).network, null);
});

test('testnet: network sorts lexicographically among the other parameters', () => {
  assert.equal(
    peerQuery().version(['1550']).network('testnet').cc(['jp']).maxTier(1).toQueryString(),
    'cc=JP&max_tier=1&network=testnet&version=1550',
  );
  assert.equal(
    networkMapQueryString({ operator: 'x', network: 'testnet', maxTier: 1, label: 'sentry' }),
    'label=sentry&max_tier=1&network=testnet&operator=x',
  );
});

test('🔴 mainnet golden: with network unset, no route sends a network key', async () => {
  const calls: string[] = [];
  const c = client({ calls });
  assert.equal(c.network, null);
  page(await c.listPeers());
  page(await c.listPeers(peerQuery().cc(['DE'])));
  await c.listAllPeers(peerQuery().limit(2).sort('tier'));
  await c.getPeer('192.0.2.10');
  await c.facets();
  await c.listPeersAsGossipConfig(peerQuery().cc(['DE']));
  await c.networkMapGraph({ maxTier: 2 });
  await c.networkMapSummary();
  assert.deepEqual(calls, [
    '/peers',
    '/peers?cc=DE',
    '/peers?limit=2&sort=tier',
    '/peers?cursor=cursor-page-2&limit=2&sort=tier',
    '/peers?cursor=cursor-page-3&limit=2&sort=tier',
    '/peers/192.0.2.10',
    '/peers/facets',
    '/peers?cc=DE&format=gossip_config',
    '/network-map/graph?max_tier=2',
    '/network-map/summary',
  ]);
  const res = page(await c.listPeers(peerQuery().cc(['DE'])));
  assert.equal(res.envelope.network, null, 'a mainnet body has no network: null');
});

test('every http fixture is reachable through the injected fetch', () => {
  // A guard against a fixture being added upstream that no test exercises.
  const names = new Set(
    [
      'peers_default',
      'peers_owner_q1_cc_de',
      'peers_owner_q2_not_full_cc_sg',
      'peers_owner_q3_tier1',
      'peers_owner_q4_sentries',
      'peers_unavailable',
      'peers_stale',
      'peers_format_gossip_config',
      'peers_page_1_of_3',
      'peers_page_2_of_3',
      'peers_page_3_of_3',
      'peers_304_not_modified',
      'peer_found',
      'peer_not_found',
      'peer_bad_ip',
      'facets',
      'error_400_filter_unavailable',
      'error_400_filter_unavailable_region',
      'error_400_invalid_enum',
      'error_400_chain_not_served',
      'error_400_cursor_expired',
      'error_401_invalid_key',
      'error_429_rate_limited',
      'error_429_rate_limited_long_retry_after',
      'error_429_retry_after_http_date',
      'error_429_retry_after_unparseable',
      'error_503_producer_unavailable',
      'error_500_non_problem_body',
      'error_400_filter_removed',
      'error_400_filter_unavailable_mempool',
      'peers_owner_q1_mempool_cc_de',
      'peers_serves_mempool_unknown',
      'peers_auction_holds_slot_or_bidding',
      'network_map_graph',
      'network_map_summary',
      'network_map_graph_mempool_auction',
      'peers_state_listening',
      'error_429_ip_flood',
      'peers_testnet',
      'peer_found_testnet',
      'facets_testnet',
      'network_map_graph_testnet',
      'network_map_summary_testnet',
      'error_400_network_not_served',
    ].map((n) => n),
  );
  assert.equal(names.size, 44, 'the fixture set is 44 cases; update the tests if it grows');
  for (const fx of loadHttpFixtures()) {
    assert.equal(fx.response.headers?.['API-Version'], '1', `${fx.name} carries API-Version`);
  }
  const onDisk = new Set(loadHttpFixtures().map((f) => f.name));
  assert.deepEqual([...onDisk].sort(), [...names].sort(), 'no fixture goes unexercised');
});
