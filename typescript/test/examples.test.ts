/**
 * The example programs, executed against the fixtures.
 *
 * 🪤 These are the programs the README and `docs/typescript.md` were written
 * FROM; the prose blocks there are illustrative fragments, not verbatim
 * quotations, so running these does not prove those are correct. What does
 * check the prose is `docs.test.ts`, which resolves every documented import,
 * method and option against the real module.
 */
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { PeerApiClient, RateLimitedError, peerQuery, tier1 } from '../src/index.ts';
import * as quickstart from '../examples/01-quickstart.ts';
import * as ownerQueries from '../examples/02-owner-queries.ts';
import * as gossipRecipes from '../examples/03-gossip-recipes.ts';
import * as errors from '../examples/04-errors.ts';
import * as peerMgmt from '../examples/05-peer-management.ts';
import { TEST_BASE_URL, fixtureFetch } from './fixture-fetch.ts';

const MINIMAL =
  '{\n  "chain": "Mainnet",\n  "root_node_ips": [\n    {\n      "Ip": "192.0.2.10"\n' +
  '    }\n  ],\n  "try_new_peers": false\n}\n';

function client(scenario?: string): PeerApiClient {
  return new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    fetch: scenario === undefined ? fixtureFetch() : fixtureFetch({ scenario }),
    env: {},
    now: () => Date.parse('2026-09-21T02:36:00Z'),
    sleep: () => Promise.resolve(),
  });
}

async function withTmp<T>(fn: (path: string) => Promise<T>): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), 'hl-peers-ex-'));
  try {
    return await fn(join(dir, 'override_gossip_config.json'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('example 01: quickstart', async () => {
  const lines = await quickstart.run({
    baseUrl: TEST_BASE_URL,
    fetch: fixtureFetch(),
    env: {},
  });
  assert.equal(lines.length, 3);
  assert.match(lines[0]!, /^192\.0\.2\.10 tier=1 cc=DE connectable=yes$/);
  assert.match(lines[2]!, /^quota: 17\/20 resets in 42s$/);
});

test('example 02: the owner queries', async () => {
  const out = await ownerQueries.run(client());
  assert.equal(out['inDe'], 2);
  assert.deepEqual(out['mempoolInDe'], ['192.0.2.10:probe']);
  // 🪤 the owner-query fixtures are single-page on purpose: total == count
  assert.equal(out['notFullSg'], 1);
  assert.equal(out['tier1Total'], 1);
  assert.equal(out['sentriesTotal'], 1);
  assert.deepEqual(out['pagedIds'], [
    '203.0.113.1',
    '203.0.113.2',
    '203.0.113.3',
    '203.0.113.4',
    '203.0.113.5',
  ]);
  assert.equal(out['asnUsable'], false);
  assert.equal(out['asnAnswer'], 'filter-unavailable: asn');
});

test('example 03: gossip recipes', async () => {
  const json = await gossipRecipes.toStdout(client());
  assert.ok(json.startsWith('{\n  "chain": "Mainnet",\n  "root_node_ips": ['));
  assert.ok(json.endsWith('}\n'));

  await withTmp(async (path) => {
    writeFileSync(path, MINIMAL);
    const lines = await gossipRecipes.updateInPlace(client(), path);
    assert.match(lines[0]!, /^changed=true written=/);
    const written = readFileSync(path, 'utf8');
    assert.ok(written.includes('203.0.113.7'), 'the pinned peer leads the list');
    assert.ok(!written.includes('198.51.100.7'), 'the excluded peer never appears');

    const diff = await gossipRecipes.dryRun(client(), path);
    assert.ok(diff.startsWith('--- '));
    assert.equal(readFileSync(path, 'utf8'), written, 'a dry run writes nothing');

    const summary = gossipRecipes.addAndRemoveRoots(path);
    assert.match(summary, /^written=true/);
    assert.ok(readFileSync(path, 'utf8').includes('203.0.113.42'));
  });

  assert.equal(
    gossipRecipes.fromScratch(),
    '{\n  "chain": "Mainnet",\n  "root_node_ips": [\n    {\n      "Ip": "192.0.2.10"\n' +
      '    },\n    {\n      "Ip": "192.0.2.11"\n    }\n  ],\n  "try_new_peers": true\n}\n',
  );
});

test('example 04: error handling', async () => {
  assert.equal(await errors.describeFailure(client(), tier1()), '1 peers');
  assert.equal(
    await errors.describeFailure(client(), peerQuery().asn(['AS24940'])),
    'filter asn is not collected; call facets() first',
  );
  assert.equal(
    await errors.describeFailure(client(), peerQuery().cursor('cursor-from-a-previous-sweep')),
    'cursor expired: start the walk again from page one',
  );
  assert.match(
    await errors.describeFailure(client('rate-limited'), peerQuery()),
    /^rate limited until 2026-09-21T02:36:42\.000Z: retry after 42s, 0\/20 left; raise it at https:\/\/hypervisor\.gg\/docs\/peer-api\/authentication$/,
  );
  assert.equal(
    await errors.describeFailure(client('unavailable'), peerQuery()),
    'the producer could not build a graph',
  );
  assert.equal(
    await errors.describeFailure(client('producer-unavailable'), peerQuery()),
    'the producer is down; try later',
  );
});

test('example 05: peer management', async () => {
  // (a) refresh: every peer in the default fixture is live and close to tip;
  // an excluded IP never comes back.
  const fresh = await peerMgmt.refreshCandidates(client(), peerQuery(), {
    exclude: ['192.0.2.200'],
  });
  assert.deepEqual(
    fresh.map((p) => p.id),
    ['192.0.2.10', '192.0.2.11', '198.51.100.7', '198.51.100.20'],
  );
  assert.equal(
    (await peerMgmt.refreshCandidates(client(), peerQuery(), { maxBlocksBehind: 0 })).length,
    4,
    'blocks_behind=1 is dropped at maxBlocksBehind=0',
  );
  await assert.rejects(
    peerMgmt.refreshCandidates(client('unavailable'), peerQuery()),
    /keep the current list/,
  );

  // (c) diversity: one per ASN keeps the first AS64500 and the AS64501 peer.
  assert.deepEqual(
    peerMgmt.diversify(fresh).map((p) => p.id),
    ['192.0.2.10', '198.51.100.7'],
  );
  assert.equal(peerMgmt.diversify(fresh, { perAsn: 3, perCountry: 1 }).length, 3);

  await withTmp(async (path) => {
    writeFileSync(
      path,
      '{\n  "chain": "Mainnet",\n  "root_node_ips": [\n' +
        '    {\n      "Ip": "192.0.2.10"\n    },\n' +
        '    {\n      "Ip": "198.51.100.254"\n    }\n  ],\n  "try_new_peers": true\n}\n',
    );

    // (b) the map's view of each root: one healthy, one never published.
    assert.deepEqual(await peerMgmt.checkRoots(client(), path), {
      '192.0.2.10': 'ok',
      '198.51.100.254': 'not_published',
    });

    // (e) the floor holds: two roots, floor 3, nothing written.
    const before = readFileSync(path, 'utf8');
    assert.throws(
      () => peerMgmt.rotateOut(path, '198.51.100.254', [], { floor: 3 }),
      /refusing to rotate/,
    );
    assert.equal(readFileSync(path, 'utf8'), before);

    // (b) + (e): one swap, previous file kept as a backup.
    const r = peerMgmt.rotateOut(path, '198.51.100.254', ['192.0.2.10', '198.51.100.7'], {
      floor: 2,
    });
    assert.equal(r.added, '198.51.100.7', 'an IP already in the file is skipped');
    assert.deepEqual(r.roots, ['192.0.2.10', '198.51.100.7']);
    assert.equal(r.written, true);
    assert.ok(r.backupPath !== undefined && readFileSync(r.backupPath, 'utf8') === before);
  });

  // (d) polling: just after next_sweep_at (02:40Z) from 02:36Z = 4 min, no jitter.
  const page = await client().listPeers(peerQuery());
  assert.ok(!('notModified' in page));
  const now = Date.parse('2026-09-21T02:36:00Z');
  assert.equal(peerMgmt.nextPollDelayMs({ page }, now, () => 0), 240_000);
  try {
    await client('rate-limited').listPeers(peerQuery());
    assert.fail('expected a 429');
  } catch (error) {
    assert.ok(error instanceof RateLimitedError);
    assert.equal(peerMgmt.nextPollDelayMs({ error, attempt: 0 }, now, () => 0), 42_000);
  }
  assert.equal(peerMgmt.nextPollDelayMs({ error: new Error('x'), attempt: 2 }, now, () => 0), 120_000);
  assert.equal(peerMgmt.nextPollDelayMs({ error: new Error('x'), attempt: 9 }, now, () => 0), 900_000);
});
