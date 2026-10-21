/**
 * A `fetch` that replays `fixtures/http/*.json`, with the same matching rules
 * as `tools/mock_server.py`: method, path and the FULL query string as a set of
 * pairs must be equal, every header named in `request.headers` must be present
 * with that value, and a fixture that names headers outranks one that does not.
 *
 * 🔴 No network. `X-Mock-Scenario` is a fixture affordance, not part of the
 * API: a real client never sends it, so tests pass it as `scenario` and this
 * shim adds it for matching only.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { FetchLike } from '../src/index.ts';

export const HTTP_DIR = fileURLToPath(new URL('../../fixtures/http/', import.meta.url));

export interface HttpFixture {
  name: string;
  description: string;
  request: {
    method: string;
    path: string;
    query: Record<string, string>;
    headers?: Record<string, string>;
  };
  response: {
    status: number;
    headers?: Record<string, string>;
    body?: unknown;
    body_text?: string;
  };
  /** The instant a test must inject, for a response the client must date-math. */
  clock?: { now: string };
  /** What the CLIENT must compute from the response. */
  expect?: { retry_after_seconds?: number | null; gossip_config_json_text?: string };
}

export function loadHttpFixtures(): HttpFixture[] {
  return readdirSync(HTTP_DIR)
    .filter((f) => f.endsWith('.json'))
    .sort()
    .map((f) => JSON.parse(readFileSync(join(HTTP_DIR, f), 'utf8')) as HttpFixture);
}

export function fixtureByName(name: string): HttpFixture {
  const found = loadHttpFixtures().find((f) => f.name === name);
  if (found === undefined) throw new Error(`no http fixture named ${name}`);
  return found;
}

function queryKey(pairs: Iterable<[string, string]>): string {
  return [...pairs]
    .map(([k, v]) => `${k}=${v}`)
    .sort()
    .join('&');
}

export interface FixtureFetchOptions {
  /** Selects an otherwise-unreachable response for the same query. */
  scenario?: string;
  /** Extra request headers used for matching (e.g. an Authorization probe). */
  headers?: Record<string, string>;
  /** Every URL this fetch was asked for, in order. */
  calls?: string[];
}

/** Build a `fetch` over the fixtures. */
export function fixtureFetch(opts: FixtureFetchOptions = {}): FetchLike {
  const fixtures = loadHttpFixtures();
  return ((input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(
      typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
    );
    opts.calls?.push(url.pathname + url.search);

    const headers = new Headers(init?.headers);
    if (opts.scenario !== undefined) headers.set('X-Mock-Scenario', opts.scenario);
    for (const [k, v] of Object.entries(opts.headers ?? {})) headers.set(k, v);

    const wantQuery = queryKey(url.searchParams.entries());
    const exact: HttpFixture[] = [];
    const loose: HttpFixture[] = [];
    for (const fx of fixtures) {
      if (fx.request.method !== (init?.method ?? 'GET')) continue;
      if (fx.request.path !== url.pathname) continue;
      if (queryKey(Object.entries(fx.request.query ?? {})) !== wantQuery) continue;
      const want = fx.request.headers ?? {};
      if (Object.keys(want).length > 0) {
        if (Object.entries(want).every(([k, v]) => headers.get(k) === v)) exact.push(fx);
      } else {
        loose.push(fx);
      }
    }
    const fx = [...exact, ...loose][0];
    if (fx === undefined) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            type: 'https://example.invalid/problems/no-fixture',
            title: 'No fixture matches this request',
            status: 404,
            detail: `${url.pathname}${url.search}`,
          }),
          { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      );
    }

    // The mock server synthesises a 304 for any fixture carrying an ETag when
    // a matching If-None-Match arrives.
    const etag = fx.response.headers?.['ETag'];
    const inm = headers.get('If-None-Match');
    if (inm !== null && fx.response.status === 200 && etag === inm) {
      const keep = new Headers();
      for (const [k, v] of Object.entries(fx.response.headers ?? {})) {
        if (k === 'ETag' || k === 'Cache-Control' || k.startsWith('RateLimit-')) keep.set(k, v);
      }
      return Promise.resolve(new Response(null, { status: 304, headers: keep }));
    }

    const body =
      fx.response.body_text !== undefined
        ? fx.response.body_text
        : fx.response.body === null || fx.response.body === undefined
          ? null
          : JSON.stringify(fx.response.body);
    return Promise.resolve(
      new Response(fx.response.status === 304 ? null : body, {
        status: fx.response.status,
        headers: fx.response.headers ?? {},
      }),
    );
  }) as FetchLike;
}

/** A client wired to the fixtures, with the retry sleep and warning silenced. */
export const TEST_BASE_URL = 'https://peer-api.test.invalid';
