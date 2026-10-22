/**
 * The hand-shaped types cover exactly the spec's field sets.
 *
 * `spec/peer-api-v1.fields.json` is generated from the two OpenAPI documents
 * (`tools/gen_spec_fields.py`). It is read at test time, so a field the spec
 * adds or drops fails this suite instead of silently drifting past a
 * hand-written decoder. Both the declared key lists and the decoders'
 * *behaviour* are checked: a fully-populated object must decode with nothing
 * left over in `extra`, and the decoded value must carry exactly those keys.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
  ENVELOPE_FIELDS,
  NETWORK_MAP_CITY_FIELDS,
  NETWORK_MAP_EDGE_FIELDS,
  PEER_AUCTION_FIELDS,
  PEER_FIELDS,
  PEER_MEMPOOL_FIELDS,
  decodeCities,
  decodeEdges,
  decodeEnvelope,
  decodePeer,
} from '../src/decode.ts';
import { networkMapQueryString, peerQuery } from '../src/index.ts';

interface SpecFields {
  peer: string[];
  peer_mempool: string[];
  peer_auction: string[];
  envelope: string[];
  list_peers_params: string[];
  facets_params: string[];
  network_map_params: string[];
  network_map_edge: string[];
  network_map_city: string[];
  network_map_graph: string[];
  network_map_summary: string[];
  enums: Record<string, string[]>;
}

const SPEC = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../../spec/peer-api-v1.fields.json', import.meta.url)),
    'utf8',
  ),
) as SpecFields;

const sorted = (xs: Iterable<string>): string[] => [...xs].sort();

test('Peer decoder covers exactly spec.peer', () => {
  assert.deepEqual(sorted(PEER_FIELDS), sorted(SPEC.peer));
  // behaviour, not just the list: every spec key decodes into a typed field
  const full = Object.fromEntries(SPEC.peer.map((k) => [k, null])) as Record<string, unknown>;
  full['id'] = '192.0.2.1';
  const peer = decodePeer(full);
  assert.deepEqual(peer.extra, {}, 'no spec field may fall through to extra');
  const typed = Object.keys(peer).filter((k) => k !== 'extra');
  assert.deepEqual(sorted(typed), sorted(SPEC.peer));
});

test('mempool and auction decoders cover exactly their spec field sets', () => {
  const peer = decodePeer({ id: '192.0.2.1' });
  assert.deepEqual(sorted(PEER_MEMPOOL_FIELDS), sorted(SPEC.peer_mempool));
  assert.deepEqual(sorted(Object.keys(peer.mempool)), sorted(SPEC.peer_mempool));
  assert.deepEqual(sorted(PEER_AUCTION_FIELDS), sorted(SPEC.peer_auction));
  assert.deepEqual(sorted(Object.keys(peer.auction)), sorted(SPEC.peer_auction));
});

test('Envelope decoder covers exactly spec.envelope', () => {
  assert.deepEqual(sorted(ENVELOPE_FIELDS), sorted(SPEC.envelope));
  assert.deepEqual(sorted(Object.keys(decodeEnvelope({}))), sorted(SPEC.envelope));
});

test('network-map edge decoder covers exactly spec.network_map_edge', () => {
  assert.deepEqual(sorted(NETWORK_MAP_EDGE_FIELDS), sorted(SPEC.network_map_edge));
  const [edge] = decodeEdges([{ from: '192.0.2.1', to: '192.0.2.2', span: 1 }]);
  assert.deepEqual(sorted(Object.keys(edge ?? {})), sorted(SPEC.network_map_edge));
});

test('network-map city decoder covers exactly spec.network_map_city', () => {
  assert.deepEqual(sorted(NETWORK_MAP_CITY_FIELDS), sorted(SPEC.network_map_city));
  const [city] =
    decodeCities([{ id: 'DE-x', name: 'X', cc: 'DE', lat: 1, lon: 2, peers: 1, tiers: {} }]) ?? [];
  assert.deepEqual(sorted(Object.keys(city ?? {})), sorted(SPEC.network_map_city));
});

/** Wire key -> result key, for the network-map results (camelCase, envelope split out). */
const MAP_RESULT_KEY: Record<string, string> = {
  version: 'envelope',
  available: 'envelope',
  stale: 'envelope',
  network: 'envelope',
  generated_at: 'envelope',
  operator_nodes: 'operatorNodes',
  node_count: 'nodeCount',
  edge_count: 'edgeCount',
  tier_count: 'tierCount',
  serves_mempool: 'servesMempool',
};

test('network-map graph/summary results cover exactly the spec field sets', async () => {
  const { PeerApiClient } = await import('../src/index.ts');
  const body = (keys: string[]): string =>
    JSON.stringify(Object.fromEntries(keys.map((k) => [k, null])));
  for (const [kind, keys] of [
    ['graph', SPEC.network_map_graph],
    ['summary', SPEC.network_map_summary],
  ] as const) {
    const c = new PeerApiClient({
      baseUrl: 'http://127.0.0.1:1',
      env: {},
      fetch: (() => Promise.resolve(new Response(body(keys), { status: 200 }))) as typeof fetch,
    });
    const res = kind === 'graph' ? await c.networkMapGraph() : await c.networkMapSummary();
    for (const k of keys) {
      const rk = MAP_RESULT_KEY[k] ?? k;
      assert.ok(rk in res, `${kind}: spec field ${k} has no result member ${rk}`);
    }
  }
});

/** Every builder setter at once, so every wire parameter is emitted. */
function everyParam(): ReturnType<typeof peerQuery> {
  return peerQuery()
    .chain('Mainnet')
    .network('testnet')
    .cc(['DE'])
    .country(['DE'])
    .region(['aws:eu-central-1'])
    .asn(['AS64500'])
    .tier([1])
    .maxTier(2)
    .role(['sentry'])
    .label(['sentry'])
    .connectable(['yes'])
    .accepting(true)
    .state(['responsive'])
    .servesMempool([true])
    .auction(['holds_slot'])
    .tryNewPeers(true)
    .maxBlocksBehind(5)
    .minUptime(0.9)
    .maxLatencyMs(80)
    .vantage('eu')
    .seenWithin('1h')
    .version(['1.0.0'])
    .sort('tier')
    .limit(10)
    .cursor('c')
    .fields(['id'])
    .format('json');
}

test('the query builder emits exactly spec.list_peers_params', () => {
  assert.deepEqual(sorted(everyParam().parameterNames()), sorted(SPEC.list_peers_params));
});

test('facets refuses exactly list_peers_params minus spec.facets_params', () => {
  const refused = SPEC.list_peers_params.filter((p) => !SPEC.facets_params.includes(p));
  assert.deepEqual(sorted(refused), sorted(['sort', 'limit', 'cursor', 'fields', 'format', 'try_new_peers']));
});

test('network-map options emit exactly spec.network_map_params', () => {
  const qs = networkMapQueryString({
    maxTier: 1,
    showUnreached: true,
    hideUnreachable: false,
    connectable: ['yes'],
    cc: 'DE',
    operator: 'x',
    label: 'sentry',
    servesMempool: [true],
    auction: ['holds_slot'],
    network: 'testnet',
  });
  const names = qs.split('&').map((kv) => kv.split('=')[0] ?? '');
  assert.deepEqual(sorted(names), sorted(SPEC.network_map_params));
});

test('the closed enums the builder validates are exactly the spec enums', () => {
  for (const [name, members] of Object.entries(SPEC.enums)) {
    // every spec member is accepted…
    const setter: Record<string, (v: string) => ReturnType<typeof peerQuery>> = {
      state: (v) => peerQuery().state(v),
      connectable: (v) => peerQuery().connectable(v),
      serves_mempool: (v) => peerQuery().servesMempool(v),
      auction: (v) => peerQuery().auction(v),
      sort: (v) => peerQuery().sort(v as never),
      format: (v) => peerQuery().format(v as never),
      chain: (v) => peerQuery().chain(v as never),
      network: (v) => peerQuery().network(v as never),
    };
    const set = setter[name];
    assert.ok(set !== undefined, `no builder field for spec enum ${name}`);
    for (const m of members) set(m).validate();
    // …and a non-member is refused
    assert.throws(() => {
      set('not-a-member').validate();
    }, `${name} must be a closed enum`);
  }
});
