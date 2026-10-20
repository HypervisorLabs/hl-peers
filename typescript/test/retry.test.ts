/**
 * The retry policy (CLIENT_SPEC.md §5.2, §9(3)): asserted on the **decisions**
 * — which statuses retry, how many attempts, that `Retry-After` was read — and
 * 🔴 never on wall-clock delays. The clock, PRNG and sleep are all injected, so
 * this suite runs instantly and deterministically.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  BadRequestError,
  NotFoundError,
  PeerApiClient,
  RateLimitedError,
  ServerError,
  TransportError,
  UnauthorizedError,
  type FetchLike,
} from '../src/index.ts';

interface Recorder {
  attempts: number;
  sleeps: number[];
}

function scriptedClient(
  responses: (Response | Error)[],
  opts: { maxRetries?: number; maxRetryAfterMs?: number } = {},
): { client: PeerApiClient; rec: Recorder } {
  const rec: Recorder = { attempts: 0, sleeps: [] };
  const fetchImpl = ((): Promise<Response> => {
    const next = responses[Math.min(rec.attempts, responses.length - 1)]!;
    rec.attempts += 1;
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(next.clone());
  }) as FetchLike;
  const client = new PeerApiClient({
    baseUrl: 'https://peer-api.test.invalid',
    fetch: fetchImpl,
    env: {},
    // Retries are OFF by default; this suite tests the opt-in policy.
    maxRetries: 3,
    random: () => 0.5,
    now: () => 0,
    sleep: (ms: number) => {
      rec.sleeps.push(ms);
      return Promise.resolve();
    },
    ...opts,
  });
  return { client, rec };
}

const OK_BODY = JSON.stringify({
  version: 'v1',
  available: true,
  stale: false,
  chain: 'Mainnet',
  generated_at: null,
  next_sweep_at: null,
  count: 0,
  total: 0,
  next_cursor: null,
  peers: [],
});

const ok = (): Response => new Response(OK_BODY, { status: 200 });
const problem = (status: number, type: string, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify({ type, title: 't', status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json', ...headers },
  });

test('5xx is retried max_retries times, then raises', async () => {
  const { client, rec } = scriptedClient([
    problem(503, 'https://api.hypervisor.gg/problems/producer-unavailable'),
  ]);
  await assert.rejects(client.listPeers());
  assert.equal(rec.attempts, 4, '1 attempt + 3 retries');
  assert.equal(rec.sleeps.length, 3);
});

test('a 5xx that recovers returns the body, with no error surfaced', async () => {
  const { client, rec } = scriptedClient([
    new Response('boom', { status: 500 }),
    new Response('boom', { status: 500 }),
    ok(),
  ]);
  const res = await client.listPeers();
  assert.ok(!('notModified' in res));
  assert.equal(res.status, 200);
  assert.equal(rec.attempts, 3);
});

test('a non-recovering 500 with a non-problem body raises ServerError', async () => {
  const { client } = scriptedClient([new Response('boom', { status: 500 })], { maxRetries: 0 });
  await assert.rejects(client.listPeers(), (err: unknown) => err instanceof ServerError);
});

test('🔴 a 4xx other than 429 is never retried', async () => {
  for (const [status, type, Ctor] of [
    [400, 'https://api.hypervisor.gg/problems/invalid-filter-value', BadRequestError],
    [401, 'https://api.hypervisor.gg/problems/invalid-api-key', UnauthorizedError],
    [404, 'https://api.hypervisor.gg/problems/peer-not-found', NotFoundError],
  ] as const) {
    const { client, rec } = scriptedClient([problem(status, type)]);
    await assert.rejects(client.listPeers(), (err: unknown) => err instanceof Ctor);
    assert.equal(rec.attempts, 1, `status ${status} must not be retried`);
    assert.deepEqual(rec.sleeps, [], `status ${status} must not sleep`);
  }
});

test('429 sleeps for exactly the Retry-After it read, then retries', async () => {
  const { client, rec } = scriptedClient([
    problem(429, 'https://api.hypervisor.gg/problems/rate-limited', { 'Retry-After': '7' }),
    problem(429, 'https://api.hypervisor.gg/problems/rate-limited', { 'Retry-After': '7' }),
    ok(),
  ]);
  const res = await client.listPeers();
  assert.ok(!('notModified' in res));
  assert.equal(rec.attempts, 3);
  assert.deepEqual(rec.sleeps, [7000, 7000], 'Retry-After was read, in seconds');
});

test('429 without a usable Retry-After falls back to backoff', async () => {
  const { client, rec } = scriptedClient([
    problem(429, 'https://api.hypervisor.gg/problems/rate-limited', { 'Retry-After': 'soon' }),
    ok(),
  ]);
  await client.listPeers();
  assert.equal(rec.sleeps.length, 1);
  assert.ok(rec.sleeps[0]! > 0, 'a backoff delay, whose value is never asserted on');
});

test('D-I: Retry-After above the cap raises without sleeping', async () => {
  const { client, rec } = scriptedClient([
    problem(429, 'https://api.hypervisor.gg/problems/rate-limited', { 'Retry-After': '3600' }),
  ]);
  await assert.rejects(client.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.retryAfter, 3600);
    return true;
  });
  assert.equal(rec.attempts, 1);
  assert.deepEqual(rec.sleeps, []);
});

test('the cap is configurable, so a caller may opt into a longer wait', async () => {
  const { client, rec } = scriptedClient(
    [
      problem(429, 'https://api.hypervisor.gg/problems/rate-limited', { 'Retry-After': '120' }),
      ok(),
    ],
    { maxRetryAfterMs: 300_000 },
  );
  await client.listPeers();
  assert.deepEqual(rec.sleeps, [120_000]);
});

test('a transport fault is retried, then raises Transport', async () => {
  const { client, rec } = scriptedClient([new Error('ECONNRESET')]);
  await assert.rejects(client.listPeers(), (err: unknown) => err instanceof TransportError);
  assert.equal(rec.attempts, 4);
});

test('🔴 the default is no retry: one 503 is exactly one request, no sleep', async () => {
  let attempts = 0;
  const slept: number[] = [];
  const client = new PeerApiClient({
    baseUrl: 'https://peer-api.test.invalid',
    env: {},
    fetch: (() => {
      attempts += 1;
      return Promise.resolve(problem(503, 'https://api.hypervisor.gg/problems/producer-unavailable'));
    }) as FetchLike,
    sleep: (ms: number) => {
      slept.push(ms);
      return Promise.resolve();
    },
  });
  assert.equal(client.maxRetries, 0);
  await assert.rejects(client.listPeers());
  assert.equal(attempts, 1);
  assert.deepEqual(slept, []);
});

test('🔴 the default is no retry: a 429 raises at once, even under the cap', async () => {
  let attempts = 0;
  const client = new PeerApiClient({
    baseUrl: 'https://peer-api.test.invalid',
    env: {},
    now: () => 1_000_000,
    fetch: (() => {
      attempts += 1;
      return Promise.resolve(
        problem(429, 'https://api.hypervisor.gg/problems/rate-limited', { 'Retry-After': '7' }),
      );
    }) as FetchLike,
  });
  await assert.rejects(client.listPeers(), (err: unknown) => {
    assert.ok(err instanceof RateLimitedError);
    assert.equal(err.resetAt?.getTime(), 1_000_000 + 7_000);
    return true;
  });
  assert.equal(attempts, 1);
});

test('maxRetries: 0 means exactly one attempt', async () => {
  const { client, rec } = scriptedClient(
    [problem(503, 'https://api.hypervisor.gg/problems/producer-unavailable')],
    { maxRetries: 0 },
  );
  await assert.rejects(client.listPeers());
  assert.equal(rec.attempts, 1);
});

test('no warning is logged; suppressPlaceholderWarning is an accepted no-op', async () => {
  const warned: string[] = [];
  const client = new PeerApiClient({
    fetch: (() => Promise.resolve(ok())) as FetchLike,
    env: {},
    logger: { warn: (m) => warned.push(m) },
    suppressPlaceholderWarning: true,
  });
  await client.listPeers();
  assert.deepEqual(warned, []);
  assert.equal(client.baseUrl, 'https://api.hypervisor.gg');
});

test('the User-Agent is hl-peers-ts/1.1.0', async () => {
  let ua: string | null = null;
  const client = new PeerApiClient({
    env: {},
    userAgentSuffix: 'my-app/2',
    fetch: ((_url: string, init?: RequestInit) => {
      ua = new Headers(init?.headers).get('User-Agent');
      return Promise.resolve(ok());
    }) as FetchLike,
  });
  await client.listPeers();
  assert.equal(ua, 'hl-peers-ts/1.1.0 my-app/2');
});

test('base_url and api_key resolve argument -> env -> default', () => {
  const env = {
    HL_PEER_API_BASE_URL: 'https://from-env.invalid',
    HL_PEER_API_KEY: 'hlp_live_fromenv0000000000000000',
  };
  const fromEnv = new PeerApiClient({ env, fetch: (() => Promise.resolve(ok())) as FetchLike });
  assert.equal(fromEnv.baseUrl, 'https://from-env.invalid');
  assert.equal(fromEnv.hasApiKey, true);

  const explicit = new PeerApiClient({
    env,
    baseUrl: 'https://explicit.invalid/',
    apiKey: null,
    fetch: (() => Promise.resolve(ok())) as FetchLike,
  });
  assert.equal(explicit.baseUrl, 'https://explicit.invalid');
  assert.equal(explicit.hasApiKey, false, 'an explicit null beats the environment');

  const bare = new PeerApiClient({ env: {}, fetch: (() => Promise.resolve(ok())) as FetchLike });
  assert.equal(bare.baseUrl, 'https://api.hypervisor.gg');
  assert.equal(bare.hasApiKey, false, 'anonymous is a supported tier, not an error');
});
