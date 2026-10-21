/**
 * One integration test against the real `tools/mock_server.py`, on a free port,
 * with the process spawned and torn down here.
 *
 * 🔴 This is still not network access: the server is local, serves only
 * `fixtures/http`, and the suite skips itself if `python3` is unavailable.
 */
import assert from 'node:assert/strict';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

import { GossipConfig, PeerApiClient, peerQuery, tier1 } from '../src/index.ts';

const MOCK_SERVER = fileURLToPath(new URL('../../tools/mock_server.py', import.meta.url));

const hasPython = spawnSync('python3', ['--version'], { stdio: 'ignore' }).status === 0;

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      const port = typeof addr === 'object' && addr !== null ? addr.port : 0;
      srv.close(() => {
        resolve(port);
      });
    });
  });
}

async function waitForReady(url: string, attempts = 100): Promise<void> {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url);
      await res.text();
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 50));
    }
  }
  throw new Error(`mock server never came up at ${url}`);
}

describe('integration: tools/mock_server.py', { skip: !hasPython ? 'python3 not available' : false }, () => {
  let proc: ChildProcess | undefined;
  let baseUrl = '';

  before(async () => {
    const port = await freePort();
    baseUrl = `http://127.0.0.1:${port}`;
    proc = spawn('python3', [MOCK_SERVER, '--port', String(port), '--host', '127.0.0.1'], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    await waitForReady(`${baseUrl}/peers`);
  });

  after(() => {
    proc?.kill('SIGTERM');
  });

  const client = (): PeerApiClient =>
    new PeerApiClient({ baseUrl, env: {} });

  it('serves the owner queries, the cursor walk, a 304 and a 404', async () => {
    const c = client();

    const t1 = await c.listPeers(tier1());
    assert.ok(!('notModified' in t1));
    assert.equal(t1.total, 1);
    assert.equal(t1.nextCursor, null, 'single-page: no dangling cursor to chase');
    assert.equal(t1.peers[0]?.id, '198.51.100.20');
    assert.equal(t1.rateLimit.limit, 20);

    // a full cursor walk over three real HTTP responses
    const ids: string[] = [];
    for await (const peer of c.iterPeers(peerQuery().limit(2).sort('tier'))) ids.push(peer.id);
    assert.equal(ids.length, 5);

    // the second identical call revalidates and comes back from the cache
    const first = await c.listPeers();
    assert.ok(!('notModified' in first));
    const second = await c.listPeers();
    assert.ok(!('notModified' in second));
    assert.equal(second.status, 304);
    assert.equal(second.fromCache, true);
    assert.equal(second.peers.length, 5);

    await assert.rejects(c.getPeer('198.51.100.254'), (err: unknown) => {
      assert.equal((err as { kind: string }).kind, 'NotFound');
      return true;
    });
  });

  it('format=gossip_config round-trips into a byte-exact file', async () => {
    const res = await client().listPeersAsGossipConfig(peerQuery().cc(['DE']));
    assert.ok(!('notModified' in res));
    const cfg = res.config;
    assert.ok(cfg instanceof GossipConfig);
    assert.equal(res.apiVersion, '1');
    assert.equal(cfg.validate().ok, true);
    assert.equal(
      cfg.toJson(),
      '{\n  "chain": "Mainnet",\n  "root_node_ips": [\n    {\n      "Ip": "192.0.2.10"\n' +
        '    },\n    {\n      "Ip": "192.0.2.11"\n    },\n    {\n      "Ip": "198.51.100.7"\n' +
        '    }\n  ],\n  "try_new_peers": true\n}\n',
    );
  });

  it('serves the network map and the mempool/auction filters over real sockets', async () => {
    const c = client();
    const graph = await c.networkMapGraph({ maxTier: 2 });
    assert.ok(!('notModified' in graph));
    assert.equal(graph.nodes.length, 4);
    const summary = await c.networkMapSummary();
    assert.ok(!('notModified' in summary));
    assert.equal(summary.connectable['yes'], 3);
    const q1 = await c.listPeers(peerQuery().servesMempool([true]).cc(['DE']));
    assert.ok(!('notModified' in q1));
    assert.equal(q1.peers[0]?.mempool.serves, true);
  });

  it('--fail-first-n drives the retry path over real sockets', async () => {
    const port = await freePort();
    const failing = spawn(
      'python3',
      [MOCK_SERVER, '--port', String(port), '--host', '127.0.0.1', '--fail-first-n', '2'],
      { stdio: ['ignore', 'pipe', 'pipe'] },
    );
    try {
      const url = `http://127.0.0.1:${port}`;
      // the readiness probe itself consumes the injected failures
      await waitForReady(`${url}/peers`);
      const c = new PeerApiClient({
        baseUrl: url,
        env: {},
        sleep: () => Promise.resolve(),
        maxRetries: 3, // opt in: retries are off by default
      });
      const res = await c.listPeers();
      assert.ok(!('notModified' in res));
      assert.equal(res.envelope.available, true);
    } finally {
      failing.kill('SIGTERM');
    }
  });
});
