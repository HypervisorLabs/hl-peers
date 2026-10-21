/**
 * ONE generic runner over `fixtures/gossip/cases/*.json` (CLIENT_SPEC.md §9(1)).
 * Not one test per case — one runner, driven by the format in
 * `fixtures/README.md`, so a case added upstream is picked up with no change
 * here.
 */
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { GossipConfig, renderGossipConfig, type Warning } from '../src/index.ts';
import { save } from '../src/gossip-fs.ts';

const CASES_DIR = fileURLToPath(new URL('../../fixtures/gossip/cases/', import.meta.url));

interface CaseOp {
  op: string;
  args?: unknown[];
  opts?: Record<string, unknown>;
}

interface CaseExpect {
  json_text?: string;
  value?: unknown;
  error?: { kind: string; parameter?: string; at_op?: number; reason_contains?: string };
  warnings?: string[];
  diff_contains?: string[];
  file_after?: { path: string; json_text: string };
  file_absent?: string;
}

interface ConformanceCase {
  name: string;
  description: string;
  input_config?: Record<string, unknown> | null;
  input_json_text?: string;
  operations: CaseOp[];
  expect: CaseExpect;
}

/** `opts` arrive in the language-neutral snake_case of the fixtures. */
function camel(key: string): string {
  return key.replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase());
}

function camelOpts(opts: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(opts ?? {})) out[camel(k)] = v;
  return out;
}

function subst(value: unknown, tmp: string): unknown {
  if (typeof value === 'string') return value.replace('$TMPDIR', tmp);
  return value;
}

interface OpOutcome {
  value: unknown;
  warnings?: readonly Warning[];
  subject?: GossipConfig;
}

/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-unsafe-argument */
function applyOp(subject: GossipConfig | null, op: CaseOp, tmp: string): OpOutcome {
  const args = (op.args ?? []).map((a) => subst(a, tmp));
  const opts = camelOpts(op.opts);
  const need = (): GossipConfig => {
    assert.ok(subject !== null, `operation ${op.op} needs a subject`);
    return subject;
  };

  switch (op.op) {
    case 'new': {
      const cfg = GossipConfig.new(args[0] as any, args[1] as any);
      return { value: cfg, subject: cfg };
    }
    case 'from_peers': {
      const res = GossipConfig.fromPeers(args[0] as any, opts);
      return { value: res, warnings: res.warnings, subject: res.config };
    }
    case 'to_json':
      return { value: need().toJson() };
    case 'validate': {
      const res = need().validate();
      return { value: 'ok', warnings: res.warnings };
    }
    case 'save': {
      const res = save(need(), args[0] as string, opts);
      return { value: res, warnings: res.warnings };
    }
    case 'root_ips':
      return { value: need().rootIps() };
    case 'has_root_ip':
      return { value: need().hasRootIp(args[0] as string) };
    case 'add_root_ip':
      return { value: need().addRootIp(args[0] as string) };
    case 'remove_root_ip':
      return { value: need().removeRootIp(args[0] as string) };
    case 'set_root_ips':
      return { value: need().setRootIps(args[0] as any) };
    case 'reserved_peer_ips':
      return { value: need().reservedPeerIps() };
    case 'add_reserved_peer_ip':
      return { value: need().addReservedPeerIp(args[0] as string) };
    case 'remove_reserved_peer_ip':
      return { value: need().removeReservedPeerIp(args[0] as string) };
    case 'set_reserved_peer_ips':
      return { value: need().setReservedPeerIps(args[0] as any) };
    case 'set_chain':
      return { value: need().setChain(args[0] as any) };
    case 'set_try_new_peers':
      return { value: need().setTryNewPeers(args[0] as boolean) };
    case 'set_n_gossip_peers':
      return { value: need().setNGossipPeers(args[0] as number) };
    case 'set_split_client_blocks':
      return { value: need().setSplitClientBlocks(args[0] as boolean) };
    case 'unset_split_client_blocks':
      return { value: need().unsetSplitClientBlocks() };
    case 'merge_peers': {
      const res = need().mergePeers(args[0] as any, opts);
      return { value: res, warnings: res.warnings };
    }
    default:
      throw new Error(`the runner does not implement the operation ${op.op}`);
  }
}
/* eslint-enable @typescript-eslint/no-explicit-any, @typescript-eslint/no-unsafe-argument */

function subsetMatch(actual: unknown, expected: unknown, label: string): void {
  if (expected !== null && typeof expected === 'object' && !Array.isArray(expected)) {
    assert.ok(
      actual !== null && typeof actual === 'object',
      `${label}: expected an object result, got ${String(actual)}`,
    );
    for (const [k, v] of Object.entries(expected)) {
      assert.deepEqual((actual as Record<string, unknown>)[k], v, `${label}.${k}`);
    }
    return;
  }
  assert.deepEqual(actual, expected, label);
}

const files = readdirSync(CASES_DIR)
  .filter((f) => f.endsWith('.json'))
  .sort();

assert.ok(files.length > 0, `no conformance cases found in ${CASES_DIR}`);

for (const file of files) {
  const testCase = JSON.parse(readFileSync(join(CASES_DIR, file), 'utf8')) as ConformanceCase;

  test(`gossip conformance: ${testCase.name}`, () => {
    const tmp = mkdtempSync(join(tmpdir(), 'hl-peers-case-'));
    try {
      let subject: GossipConfig | null = null;
      let lastValue: unknown;
      let lastWarnings: readonly Warning[] = [];
      let thrown: unknown;
      let thrownAt = -1;

      try {
        // 1. build the subject
        if (testCase.input_json_text !== undefined) {
          subject = GossipConfig.parse(testCase.input_json_text);
        } else if (testCase.input_config != null) {
          // parse(render(config)) — equivalent to constructing directly, and it
          // exercises the serializer on the way in.
          subject = GossipConfig.parse(renderGossipConfig(testCase.input_config));
        }

        // 2. apply the operations in order
        for (const [index, op] of testCase.operations.entries()) {
          thrownAt = index;
          const outcome = applyOp(subject, op, tmp);
          lastValue = outcome.value;
          lastWarnings = outcome.warnings ?? [];
          if (outcome.subject !== undefined) subject = outcome.subject;
        }
        thrownAt = -1;
      } catch (err) {
        thrown = err;
      }

      // 3. compare against `expect`
      const expected = testCase.expect;

      if (expected.error !== undefined) {
        assert.ok(thrown !== undefined, `expected ${expected.error.kind}, nothing was raised`);
        const err = thrown as { kind?: string; parameter?: string; reason?: string; message: string };
        assert.equal(err.kind, expected.error.kind, 'error kind');
        if (expected.error.parameter !== undefined) {
          assert.equal(err.parameter, expected.error.parameter, 'error parameter');
        }
        if (expected.error.at_op !== undefined) {
          assert.equal(thrownAt, expected.error.at_op, 'raising operation index');
        }
        if (expected.error.reason_contains !== undefined) {
          const haystack = `${err.reason ?? ''} ${err.message}`.toLowerCase();
          assert.ok(
            haystack.includes(expected.error.reason_contains.toLowerCase()),
            `error reason should mention ${JSON.stringify(expected.error.reason_contains)}, got ${err.message}`,
          );
        }
      } else if (thrown !== undefined) {
        throw thrown instanceof Error ? thrown : new Error(JSON.stringify(thrown));
      }

      if (expected.json_text !== undefined) {
        assert.ok(subject !== null, 'expected a subject to render');
        assert.equal(subject.toJson(), expected.json_text, 'byte-exact to_json()');
      }

      if (expected.value !== undefined) {
        if (expected.value === 'ok') {
          assert.equal(lastValue, 'ok', 'validate() should have succeeded');
        } else {
          subsetMatch(lastValue, expected.value, 'value');
        }
      }

      if (expected.warnings !== undefined) {
        // fixtures/README.md: after the last operation the runner calls
        // validate() and compares the resulting warning SET — §6.4's
        // state-derived codes unioned with §6.5's pending merge warnings.
        assert.ok(subject !== null, 'expected a subject to validate');
        const codes = [...subject.validate().warnings.map((w) => w.code)].sort();
        assert.deepEqual(codes, [...expected.warnings].sort(), 'warning codes');
        // the merge warnings a caller saw are the same ones validate re-reports
        for (const w of lastWarnings) assert.ok(codes.includes(w.code), w.code);
      }

      if (expected.diff_contains !== undefined) {
        const diff = (lastValue as { diff?: string }).diff ?? '';
        for (const needle of expected.diff_contains) {
          assert.ok(diff.includes(needle), `diff should contain ${needle}\n${diff}`);
        }
      }

      if (expected.file_after !== undefined) {
        const path = subst(expected.file_after.path, tmp) as string;
        assert.ok(existsSync(path), `expected a file at ${path}`);
        assert.equal(readFileSync(path, 'utf8'), expected.file_after.json_text, 'file bytes');
      }

      if (expected.file_absent !== undefined) {
        const path = subst(expected.file_absent, tmp) as string;
        assert.equal(existsSync(path), false, `nothing may exist at ${path}`);
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
}
