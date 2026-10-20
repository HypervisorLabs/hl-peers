/**
 * The `hl-peers` CLI (CLIENT_SPEC.md §8), driven end to end against
 * `tools/mock_server.py` where a network answer is needed and offline
 * otherwise. Asserts the exit codes, the loud warnings and 🔴 the fact that a
 * successful write prints the "reads this file only at start" note.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

const CLI = fileURLToPath(new URL('../src/cli.ts', import.meta.url));
const MOCK_SERVER = fileURLToPath(new URL('../../tools/mock_server.py', import.meta.url));
const hasPython = spawnSync('python3', ['--version'], { stdio: 'ignore' }).status === 0;

const MINIMAL =
  '{\n  "chain": "Mainnet",\n  "root_node_ips": [\n    {\n      "Ip": "192.0.2.10"\n' +
  '    }\n  ],\n  "try_new_peers": false\n}\n';

interface Run {
  code: number;
  stdout: string;
  stderr: string;
}

function hlPeers(args: string[]): Run {
  const res = spawnSync(process.execPath, [CLI, ...args], {
    encoding: 'utf8',
    env: { ...process.env, HL_PEER_API_BASE_URL: '', HL_PEER_API_KEY: '' },
  });
  return { code: res.status ?? -1, stdout: res.stdout, stderr: res.stderr };
}

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

describe('hl-peers CLI', { skip: !hasPython ? 'python3 not available' : false }, () => {
  let proc: ReturnType<typeof spawn> | undefined;
  let base = '';
  let dir = '';

  before(async () => {
    const port = await freePort();
    base = `http://127.0.0.1:${port}`;
    proc = spawn('python3', [MOCK_SERVER, '--port', String(port), '--host', '127.0.0.1'], {
      stdio: 'ignore',
    });
    for (let i = 0; i < 100; i++) {
      try {
        await (await fetch(`${base}/peers`)).text();
        break;
      } catch {
        await new Promise((r) => setTimeout(r, 50));
      }
    }
    dir = mkdtempSync(join(tmpdir(), 'hl-peers-cli-'));
  });

  after(() => {
    proc?.kill('SIGTERM');
    rmSync(dir, { recursive: true, force: true });
  });

  it('query --output ips prints one IP per line', () => {
    const run = hlPeers(['query', '--tier', '1', '--base-url', base, '--output', 'ips']);
    assert.equal(run.code, 0, run.stderr);
    assert.equal(run.stdout.trim(), '198.51.100.20');
  });

  it('peer <ip> prints the dossier; an unknown IP exits 5', () => {
    const ok = hlPeers(['peer', '192.0.2.10', '--base-url', base]);
    assert.equal(ok.code, 0);
    assert.match(ok.stdout, /"id": "192\.0\.2\.10"/);

    const missing = hlPeers(['peer', '198.51.100.254', '--base-url', base]);
    assert.equal(missing.code, 5, 'NotFound is exit 5');
  });

  it('an unavailable filter exits 1 and names the problem', () => {
    const run = hlPeers(['query', '--asn', 'AS24940', '--base-url', base]);
    assert.equal(run.code, 1);
    assert.match(run.stderr, /FilterUnavailable/);
  });

  it('--json-errors emits a machine-readable failure', () => {
    const run = hlPeers(['peer', 'not-an-ip', '--base-url', base, '--json-errors']);
    assert.equal(run.code, 1);
    const parsed = JSON.parse(run.stderr.trim()) as { kind: string; exit_code: number };
    assert.equal(parsed.kind, 'Invalid');
    assert.equal(parsed.exit_code, 1);
  });

  it('gossip validate reports warnings on stderr and ok on stdout', () => {
    const path = join(dir, 'warns.json');
    writeFileSync(
      path,
      JSON.stringify({
        root_node_ips: [{ Ip: '192.0.2.10' }, { Ip: '192.0.2.11' }],
        try_new_peers: false,
      }),
    );
    const run = hlPeers(['gossip', 'validate', '--path', path]);
    assert.equal(run.code, 0);
    assert.match(run.stdout, /ok$/m);
    assert.match(run.stderr, /warning \[chain_absent\]/);
    assert.match(run.stderr, /warning \[try_new_peers_false_multi_root\]/);
  });

  it('gossip update --dry-run prints a diff, writes nothing, exits 0', () => {
    const path = join(dir, 'dry.json');
    writeFileSync(path, MINIMAL);
    const run = hlPeers([
      'gossip', 'update', '--path', path, '--base-url', base,
      '--cc', 'DE', '--dry-run',
    ]);
    assert.equal(run.code, 0, run.stderr);
    assert.match(run.stdout, /^--- /);
    assert.equal(readFileSync(path, 'utf8'), MINIMAL);
  });

  it('gossip update --stdout writes the config to stdout only', () => {
    const run = hlPeers([
      'gossip', 'update', '--base-url', base, '--cc', 'DE',
      '--chain', 'Mainnet', '--stdout',
    ]);
    assert.equal(run.code, 0, run.stderr);
    assert.match(run.stdout, /^\{\n {2}"chain": "Mainnet",/);
    assert.ok(run.stdout.endsWith('}\n'));
  });

  it('🔴 a successful write prints the "read only at start" note', () => {
    const path = join(dir, 'written.json');
    writeFileSync(path, MINIMAL);
    const run = hlPeers([
      'gossip', 'update', '--path', path, '--base-url', base,
      '--cc', 'DE', '--backup',
    ]);
    assert.equal(run.code, 0, run.stderr);
    assert.match(run.stderr, /The node reads this file when it starts/);
    assert.match(run.stderr, /yours\s+to schedule/);
    assert.ok(readFileSync(path, 'utf8').includes('192.0.2.11'));
  });

  it('--file-mode sets the bits, and --chain reaches ChainNotServed', () => {
    const path = join(dir, 'moded.json');
    const write = hlPeers([
      'gossip', 'update', '--path', path, '--base-url', base,
      '--cc', 'DE', '--chain', 'Mainnet', '--file-mode', '0640',
    ]);
    assert.equal(write.code, 0, write.stderr);
    assert.equal(statSync(path).mode & 0o777, 0o640);

    // 🔴 --chain on `query` is the only way to reach ChainNotServed from a CLI.
    const testnet = hlPeers(['query', '--chain', 'Testnet', '--base-url', base]);
    assert.equal(testnet.code, 1);
    assert.match(testnet.stderr, /ChainNotServed/);
  });

  it('--serves-mempool, --auction and --country reach the server', () => {
    const q1 = hlPeers([
      'query', '--serves-mempool', 'true', '--cc', 'DE', '--base-url', base, '--output', 'ips',
    ]);
    assert.equal(q1.code, 0, q1.stderr);
    assert.equal(q1.stdout.trim(), '192.0.2.10');

    const unknown = hlPeers(['query', '--serves-mempool', 'null', '--base-url', base, '--output', 'ips']);
    assert.equal(unknown.code, 0, unknown.stderr);
    assert.deepEqual(unknown.stdout.trim().split('\n'), ['198.51.100.20', '192.0.2.200']);

    const auction = hlPeers([
      'query', '--auction', 'holds_slot,bidding', '--base-url', base, '--output', 'ips',
    ]);
    assert.equal(auction.code, 0, auction.stderr);
    assert.deepEqual(auction.stdout.trim().split('\n'), ['192.0.2.10', '198.51.100.7']);

    // closed enums are refused locally: exit 1, Invalid
    const bad = hlPeers(['query', '--auction', 'holding', '--base-url', base, '--json-errors']);
    assert.equal(bad.code, 1);
    assert.equal((JSON.parse(bad.stderr.trim()) as { kind: string }).kind, 'Invalid');

    // --country is its own wire parameter; no fixture serves it, so a 404
    const country = hlPeers(['query', '--country', 'jp', '--base-url', base, '--json-errors']);
    assert.equal(country.code, 5);
    assert.match(country.stderr, /country=JP/);
  });

  it('query --format gossip-config prints the canonical config', () => {
    const run = hlPeers(['query', '--cc', 'DE', '--format', 'gossip-config', '--base-url', base]);
    assert.equal(run.code, 0, run.stderr);
    assert.match(run.stdout, /^\{\n {2}"chain": "Mainnet",\n {2}"root_node_ips": \[/);
  });

  it('🔴 there is no --restart or --apply flag', () => {
    const restart = hlPeers(['gossip', 'update', '--restart']);
    assert.equal(restart.code, 2, 'unknown flag is a usage error');
    const help = hlPeers(['--help']);
    assert.equal(help.code, 0);
    assert.ok(!/--restart|--apply/.test(help.stdout));
  });

  it('no command is a usage error', () => {
    const run = hlPeers([]);
    assert.equal(run.code, 2);
  });
});
