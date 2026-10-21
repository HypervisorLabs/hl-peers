/**
 * The documentation names only things that exist.
 *
 * `examples.test.ts` executes `examples/*.ts`, which is what the README and
 * `docs/typescript.md` were written from — but the prose blocks are illustrative
 * fragments, not verbatim quotations of those files, so executing them is not
 * possible and pretending otherwise would be worse than this. What *is* checkable,
 * and what actually breaks a reader, is a documented call that does not exist:
 * a renamed method, an import that was never exported, an option spelled the way
 * an earlier draft spelled it.
 *
 * So: every named import, every `client.x(...)`, every `GossipConfig.x(...)` and
 * every builder method in the two documents is resolved against the real module.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import * as api from '../src/index.ts';
import * as gossipFs from '../src/gossip-fs.ts';
import { GossipConfig, PeerApiClient, PeerQuery } from '../src/index.ts';

const HERE = dirname(fileURLToPath(import.meta.url));
const DOCS: [string, string][] = [
  ['typescript/README.md', join(HERE, '..', 'README.md')],
  ['docs/typescript.md', join(HERE, '..', '..', 'docs', 'typescript.md')],
];

function blocks(text: string): string[] {
  return [...text.matchAll(/```ts\n([\s\S]*?)```/g)].map((m) => m[1] ?? '');
}

function documented(): { name: string; source: string; body: string }[] {
  return DOCS.flatMap(([name, path]) =>
    blocks(readFileSync(path, 'utf8')).map((body, i) => ({
      name: `${name}#${i}`,
      source: name,
      body,
    })),
  );
}

test('every documented block is non-empty and there are some', () => {
  const all = documented();
  assert.ok(all.length >= 6, `expected several documented ts blocks, found ${all.length}`);
  for (const b of all) assert.ok(b.body.trim().length > 0, `${b.name} is empty`);
});

test('every name imported from the package in the docs is exported by it', () => {
  const missing: string[] = [];
  for (const { name, body } of documented()) {
    const imports = [
      ...body.matchAll(
        /import\s*\{([\s\S]*?)\}\s*from\s*'hl-peers(\/gossip-fs)?'/g,
      ),
      ...body.matchAll(
        /const\s*\{([\s\S]*?)\}\s*=\s*require\('hl-peers(\/gossip-fs)?'\)/g,
      ),
    ];
    for (const m of imports) {
      const surface: Record<string, unknown> = m[2] === undefined ? api : gossipFs;
      const where = m[2] === undefined ? 'the package root' : '/gossip-fs';
      for (const raw of (m[1] ?? '').split(',')) {
        const ident = raw.trim().replace(/^type\s+/, '').split(/\s+as\s+/)[0]?.trim();
        if (!ident) continue;
        // Types are erased at runtime; they are checked by `tsc` instead.
        if (!(ident in surface) && !/^[A-Z]/.test(ident)) {
          missing.push(`${name}: ${ident} is not exported from ${where}`);
        } else if (!(ident in surface) && /Error$/.test(ident)) {
          missing.push(`${name}: ${ident} is not exported from ${where}`);
        }
      }
    }
  }
  assert.deepEqual(missing, []);
});

test('every documented client method exists on PeerApiClient', () => {
  const missing: string[] = [];
  for (const { name, body } of documented()) {
    for (const m of body.matchAll(/\bclient\.([A-Za-z_$][\w$]*)\s*\(/g)) {
      const method = m[1] ?? '';
      if (typeof (PeerApiClient.prototype as never)[method as never] !== 'function') {
        missing.push(`${name}: client.${method}() does not exist`);
      }
    }
  }
  assert.deepEqual(missing, []);
});

test('every documented GossipConfig member exists', () => {
  const missing: string[] = [];
  for (const { name, body } of documented()) {
    for (const m of body.matchAll(/\bGossipConfig\.([A-Za-z_$][\w$]*)\s*\(/g)) {
      const member = m[1] ?? '';
      if (typeof (GossipConfig as unknown as Record<string, unknown>)[member] !== 'function') {
        missing.push(`${name}: GossipConfig.${member}() does not exist`);
      }
    }
    for (const m of body.matchAll(/\bcfg\.([A-Za-z_$][\w$]*)\s*\(/g)) {
      const member = m[1] ?? '';
      if (typeof (GossipConfig.prototype as never)[member as never] !== 'function') {
        missing.push(`${name}: GossipConfig#${member}() does not exist`);
      }
    }
  }
  assert.deepEqual(missing, []);
});

test('every documented query-builder method exists on PeerQuery', () => {
  const missing: string[] = [];
  // `tier1().accepting(true).maxBlocksBehind(5)` and friends: the chain after a
  // preset or `peerQuery()` is all PeerQuery methods.
  for (const { name, body } of documented()) {
    for (const m of body.matchAll(
      /\b(?:peerQuery\(\)|tier1\(\)|sentries\(\)|notFull\([^)]*\))((?:\.[A-Za-z_$][\w$]*\([^)]*\))+)/g,
    )) {
      for (const call of (m[1] ?? '').matchAll(/\.([A-Za-z_$][\w$]*)\(/g)) {
        const method = call[1] ?? '';
        if (typeof (PeerQuery.prototype as never)[method as never] !== 'function') {
          missing.push(`${name}: PeerQuery#${method}() does not exist`);
        }
      }
    }
  }
  assert.deepEqual(missing, []);
});

test('the documented client options are the ones the client accepts', () => {
  // `docs/typescript.md` lists the option bag in full; a stale key there is a
  // reader following an option that does nothing.
  const accepted = new Set([
    'baseUrl',
    'basePath',
    'apiKey',
    'timeoutMs',
    'userAgent',
    'userAgentSuffix',
    'maxRetries',
    'maxRetryAfterMs',
    'minPollIntervalMs',
    'cache',
    'fetch',
    'now',
    'random',
    'sleep',
    'logger',
    'suppressPlaceholderWarning',
    'env',
  ]);
  const text = readFileSync(join(HERE, '..', '..', 'docs', 'typescript.md'), 'utf8');
  const block = blocks(text).find((b) => b.includes('new PeerApiClient({'));
  assert.ok(block, 'docs/typescript.md no longer documents the option bag');
  const documentedKeys = [...block.matchAll(/^\s{2}([A-Za-z_$][\w$]*)[,:]/gm)].map((m) => m[1]);
  assert.ok(documentedKeys.length >= 10, 'the documented option bag shrank unexpectedly');
  for (const key of documentedKeys) {
    assert.ok(accepted.has(key ?? ''), `docs/typescript.md documents an unknown option: ${key}`);
  }
});
