/**
 * `save()` and the joined helper: atomicity, mode, `.bak`, `dry_run`, and the
 * 🔴 zero-roots refusal leaving an existing file untouched (CLIENT_SPEC §6.6,
 * §7). Everything runs in a fresh temp directory; nothing touches a real node.
 */
import assert from 'node:assert/strict';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import {
  GossipConfig,
  InvalidError,
  NotFoundError,
  PeerApiClient,
  UnavailableError,
  peerQuery,
} from '../src/index.ts';
import { defaultConfigPath, load, loadOrNew, save, updateGossipConfig } from '../src/gossip-fs.ts';
import { TEST_BASE_URL, fixtureFetch } from './fixture-fetch.ts';

const MINIMAL =
  '{\n  "chain": "Mainnet",\n  "root_node_ips": [\n    {\n      "Ip": "192.0.2.10"\n' +
  '    }\n  ],\n  "try_new_peers": false\n}\n';

function withTmp<T>(fn: (dir: string, path: string) => T): T {
  const dir = mkdtempSync(join(tmpdir(), 'hl-peers-fs-'));
  try {
    return fn(dir, join(dir, 'override_gossip_config.json'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function withTmpAsync<T>(fn: (dir: string, path: string) => Promise<T>): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), 'hl-peers-fs-'));
  try {
    return await fn(dir, join(dir, 'override_gossip_config.json'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function minimal(): GossipConfig {
  return GossipConfig.parse(MINIMAL);
}

test('save writes the normative bytes and leaves no temp file behind', () => {
  withTmp((dir, path) => {
    const res = save(minimal(), path);
    assert.equal(res.written, true);
    assert.equal(res.changed, true);
    assert.equal(readFileSync(path, 'utf8'), MINIMAL);
    assert.deepEqual(
      readdirSync(dir).filter((f) => f.endsWith('.tmp')),
      [],
      'the temp file is renamed, never left behind',
    );
  });
});

test('a new file is 0600: this names infrastructure you peer with', () => {
  withTmp((_dir, path) => {
    save(minimal(), path);
    assert.equal(statSync(path).mode & 0o777, 0o600);
  });
});

test('fileMode (never `mode`) sets the bits of a new file', () => {
  withTmp((_dir, path) => {
    // 🔴 `mode` is the MERGE strategy; the permission bits are `fileMode`.
    save(minimal(), path, { fileMode: 0o640 });
    assert.equal(statSync(path).mode & 0o777, 0o640);
  });
});

test('merge warnings stay pending and are re-reported by validate and save', () => {
  withTmp((_dir, path) => {
    const cfg = minimal().setTryNewPeers(true);
    const twelve = Array.from({ length: 12 }, (_v, i) => `203.0.113.${i + 1}`);
    const merged = cfg.mergePeers(twelve, { mode: 'replace' });
    assert.deepEqual(merged.warnings.map((w) => w.code), ['truncated']);

    // §6.5: validate() and save() re-report them, and a successful save does
    // NOT clear them.
    assert.deepEqual(cfg.validate().warnings.map((w) => w.code), ['truncated']);
    const first = save(cfg, path);
    assert.equal(first.written, true);
    assert.deepEqual(first.warnings.map((w) => w.code), ['truncated']);
    assert.deepEqual(cfg.validate().warnings.map((w) => w.code), ['truncated']);

    // the next merge REPLACES them; clearWarnings() is the only other way out
    const second = cfg.mergePeers(['192.0.2.10'], { mode: 'replace' });
    assert.deepEqual(second.warnings, []);
    assert.deepEqual(cfg.validate().warnings.map((w) => w.code), []);
    cfg.mergePeers(twelve, { mode: 'replace' });
    assert.deepEqual(cfg.pendingWarnings().map((w) => w.code), ['truncated']);
    cfg.clearWarnings();
    assert.deepEqual(cfg.validate().warnings.map((w) => w.code), []);
  });
});

test('🔴 warning codes are independent: many roots does not suppress pinning', () => {
  const roots = Array.from({ length: 25 }, (_v, i) => ({ Ip: `203.0.113.${i + 1}` }));
  const cfg = new GossipConfig({ chain: 'Mainnet', root_node_ips: roots, try_new_peers: false });
  assert.deepEqual(
    cfg.validate().warnings.map((w) => w.code).sort(),
    ['many_roots_gt_24', 'try_new_peers_false_multi_root'],
  );
  const big = Array.from({ length: 65 }, (_v, i) => ({ Ip: `203.0.113.${(i % 200) + 1}` }));
  const uniq = big.map((_e, i) => ({ Ip: `198.51.100.${i + 1}` }));
  const escalated = new GossipConfig({
    chain: 'Mainnet',
    root_node_ips: uniq,
    try_new_peers: false,
  });
  assert.deepEqual(
    escalated.validate().warnings.map((w) => w.code).sort(),
    ['many_roots_gt_64', 'try_new_peers_false_multi_root'],
    'gt_64 fires INSTEAD of gt_24, but never suppresses the pinning warning',
  );
});

test('an existing mode is preserved and never widened', () => {
  withTmp((_dir, path) => {
    writeFileSync(path, '{}\n');
    chmodSync(path, 0o640);
    save(minimal(), path);
    assert.equal(statSync(path).mode & 0o777, 0o640);
  });
});

test('save is idempotent: the second call writes nothing at all', () => {
  withTmp((dir, path) => {
    save(minimal(), path);
    const before = statSync(path).mtimeMs;
    const second = save(minimal(), path, { backup: true });
    assert.equal(second.written, false);
    assert.equal(second.changed, false);
    assert.equal(second.diff, '');
    assert.equal(statSync(path).mtimeMs, before, 'no mtime change');
    assert.deepEqual(
      readdirSync(dir).filter((f) => f.endsWith('.bak')),
      [],
      'a no-op never creates a backup either',
    );
  });
});

test('backup copies the target to a UTC-stamped .bak, preserving mode', () => {
  withTmp((_dir, path) => {
    save(minimal(), path);
    chmodSync(path, 0o640);
    const cfg = minimal().addRootIp('192.0.2.99');
    const res = save(cfg, path, {
      backup: true,
      now: () => new Date('2026-09-21T02:37:05Z'),
    });
    assert.equal(res.backupPath, `${path}.20260921T023705Z.bak`);
    assert.equal(readFileSync(res.backupPath!, 'utf8'), MINIMAL);
    assert.equal(statSync(res.backupPath!).mode & 0o777, 0o640);
    assert.ok(readFileSync(path, 'utf8').includes('192.0.2.99'));
  });
});

test('dryRun writes nothing and returns a unified diff', () => {
  withTmp((dir, path) => {
    save(minimal(), path);
    const res = save(minimal().addRootIp('192.0.2.99'), path, { dryRun: true });
    assert.equal(res.written, false);
    assert.equal(res.changed, true);
    assert.match(res.diff, /^--- .*override_gossip_config\.json\n\+\+\+ /);
    assert.ok(res.diff.includes('+      "Ip": "192.0.2.99"'));
    assert.equal(readFileSync(path, 'utf8'), MINIMAL, 'the file is untouched');
    assert.deepEqual(readdirSync(dir), ['override_gossip_config.json']);
  });
});

test('dryRun against a missing target diffs an empty file', () => {
  withTmp((_dir, path) => {
    const res = save(minimal(), path, { dryRun: true });
    assert.equal(res.changed, true);
    assert.ok(res.diff.includes('+  "chain": "Mainnet"'));
    assert.equal(existsSync(path), false);
  });
});

test('🔴 a zero-root config is refused and the existing file is untouched', () => {
  withTmp((dir, path) => {
    save(minimal(), path);
    const cfg = minimal().setRootIps([]);
    assert.throws(
      () => save(cfg, path),
      (err: unknown) =>
        err instanceof InvalidError &&
        err.parameter === 'root_node_ips' &&
        /empty root_node_ips/.test(err.reason),
    );
    assert.equal(readFileSync(path, 'utf8'), MINIMAL);
    assert.deepEqual(readdirSync(dir), ['override_gossip_config.json'], 'no temp, no .bak');
  });
});

test('load refuses to invent an empty config for a missing file', () => {
  withTmp((_dir, path) => {
    assert.throws(
      () => load(path),
      (err: unknown) => err instanceof NotFoundError && err.path === path,
    );
    // loadOrNew is the explicit opt-in — and what it returns still cannot be saved
    const cfg = loadOrNew(path, 'Mainnet');
    assert.deepEqual(cfg.rootIps(), []);
    assert.throws(() => save(cfg, path), (err: unknown) => err instanceof InvalidError);
  });
});

test('defaultConfigPath is $HOME-relative and is never a discovery', () => {
  assert.match(defaultConfigPath(), /override_gossip_config\.json$/);
});

// -- updateGossipConfig -----------------------------------------------------

function apiClient(scenario?: string): PeerApiClient {
  return new PeerApiClient({
    baseUrl: TEST_BASE_URL,
    fetch: scenario === undefined ? fixtureFetch() : fixtureFetch({ scenario }),
    env: {},
    sleep: () => Promise.resolve(),
  });
}

test('updateGossipConfig builds a file from a query and writes it atomically', async () => {
  await withTmpAsync(async (_dir, path) => {
    const res = await updateGossipConfig(apiClient(), peerQuery().cc(['DE']), path, {
      chain: 'Mainnet',
      tryNewPeers: false,
    });
    assert.equal(res.changed, true);
    assert.equal(res.writtenPath, path);
    assert.deepEqual(res.config.rootIps(), ['192.0.2.10', '192.0.2.11']);
    assert.equal(readFileSync(path, 'utf8'), res.json);
    assert.equal(res.envelope?.available, true);
  });
});

test('updateGossipConfig with no path returns json and touches nothing', async () => {
  const res = await updateGossipConfig(apiClient(), peerQuery().cc(['DE']), undefined, {
    chain: 'Mainnet',
  });
  assert.ok(res.json.endsWith('}\n'));
  assert.equal(res.writtenPath, undefined);
});

test('🔴 available:false raises before any file is touched', async () => {
  await withTmpAsync(async (_dir, path) => {
    writeFileSync(path, MINIMAL);
    await assert.rejects(
      updateGossipConfig(apiClient('unavailable'), peerQuery(), path, { chain: 'Mainnet' }),
      (err: unknown) => err instanceof UnavailableError,
    );
    assert.equal(readFileSync(path, 'utf8'), MINIMAL, 'the operator keeps their roots');
  });
});

test('updateGossipConfig pins keepExisting and honours exclude over it', async () => {
  await withTmpAsync(async (_dir, path) => {
    writeFileSync(path, MINIMAL);
    const res = await updateGossipConfig(apiClient(), peerQuery().cc(['DE']), path, {
      keepExisting: ['203.0.113.7'],
      exclude: ['192.0.2.11'],
    });
    assert.deepEqual(res.config.rootIps(), ['203.0.113.7', '192.0.2.10']);
    assert.equal(res.config.chain(), 'Mainnet', 'the chain came from the existing file');
  });
});

test('updateGossipConfig dryRun reports the diff and writes nothing', async () => {
  await withTmpAsync(async (_dir, path) => {
    writeFileSync(path, MINIMAL);
    const res = await updateGossipConfig(apiClient(), peerQuery().cc(['DE']), path, {
      dryRun: true,
    });
    assert.equal(res.changed, true);
    assert.ok(res.diff.includes('192.0.2.11'));
    assert.equal(readFileSync(path, 'utf8'), MINIMAL);
  });
});

test('updateGossipConfig takes the chain from the envelope when none is given', async () => {
  const res = await updateGossipConfig(apiClient(), peerQuery().cc(['DE']));
  assert.equal(res.config.chain(), 'Mainnet', 'from the response envelope, never guessed');
});

test('🔴 updateGossipConfig refuses to guess a chain when nothing supplies one', async () => {
  // A caller-supplied peer list means no envelope, so there is nothing to read
  // the chain from. Defaulting to Mainnet is how a Testnet operator gets a
  // startup failure.
  await assert.rejects(
    updateGossipConfig(apiClient(), ['192.0.2.10']),
    (err: unknown) => err instanceof InvalidError && err.parameter === 'chain',
  );
});

test('an explicit peer list skips the network entirely', async () => {
  const res = await updateGossipConfig(apiClient(), ['192.0.2.10', '192.0.2.11'], undefined, {
    chain: 'Testnet',
  });
  assert.deepEqual(res.config.rootIps(), ['192.0.2.10', '192.0.2.11']);
  assert.equal(res.config.chain(), 'Testnet');
  assert.equal(res.envelope, null);
});


// ---------------------------------------------------------------------------
// 🔴 symlinks — CLIENT_SPEC §6.6
// ---------------------------------------------------------------------------

test('🔴 save follows a symlink and replaces its target, not the link', () => {
  withTmp((dir, path) => {
    // Renaming over the link would swap the operator's arrangement for a plain
    // file, after which the node and their tooling read two different files.
    mkdirSync(join(dir, 'managed'));
    const real = join(dir, 'managed', 'gossip.json');
    writeFileSync(real, '{"chain": "Mainnet", "root_node_ips": []}\n');
    symlinkSync(real, path);

    const cfg = GossipConfig.parse(MINIMAL);
    const result = save(cfg, path);

    assert.ok(lstatSync(path).isSymbolicLink(), 'the link itself must survive');
    assert.equal(realpathSync(path), realpathSync(real));
    assert.equal(readFileSync(real, 'utf8'), MINIMAL);
    assert.equal(result.path, path);
    assert.equal(result.resolvedPath, real);
  });
});

test('save preserves the target mode through a symlink', () => {
  withTmp((dir, path) => {
    const real = join(dir, 'gossip.json');
    writeFileSync(real, '{}\n');
    chmodSync(real, 0o640);
    symlinkSync(real, path);

    save(GossipConfig.parse(MINIMAL), path);

    assert.equal(statSync(real).mode & 0o777, 0o640);
  });
});

test('save writes through a dangling symlink and leaves the link alone', () => {
  withTmp((dir, path) => {
    const real = join(dir, 'not-yet-there.json');
    symlinkSync(real, path);

    save(GossipConfig.parse(MINIMAL), path);

    assert.equal(readFileSync(real, 'utf8'), MINIMAL);
    assert.ok(lstatSync(path).isSymbolicLink());
  });
});

test('save refuses a symlink loop', () => {
  withTmp((dir) => {
    const a = join(dir, 'a.json');
    const b = join(dir, 'b.json');
    symlinkSync(b, a);
    symlinkSync(a, b);

    assert.throws(
      () => save(GossipConfig.parse(MINIMAL), a),
      (err: unknown) => err instanceof InvalidError && err.parameter === 'path',
    );
  });
});

test('save refuses a symlink into a directory that does not exist', () => {
  withTmp((dir, path) => {
    symlinkSync(join(dir, 'nowhere', 'gossip.json'), path);

    assert.throws(
      () => save(GossipConfig.parse(MINIMAL), path),
      (err: unknown) => err instanceof InvalidError && err.parameter === 'path',
    );
  });
});
