/**
 * Handling the error taxonomy, including the two traps: a rate limit you should
 * not sleep through, and a cursor you must not silently restart.
 */
import {
  BadRequestError,
  CursorExpiredError,
  FilterUnavailableError,
  PeerApiError,
  RateLimitedError,
  UnavailableError,
  type PeerApiClient,
  type PeerQuery,
} from '../src/index.ts';

export async function describeFailure(client: PeerApiClient, query: PeerQuery): Promise<string> {
  try {
    const res = await client.listPeers(query);
    if ('notModified' in res) return 'not modified';
    // 🔴 available:false is not an error and not an empty result.
    return res.envelope.available
      ? `${res.peers.length} peers`
      : 'the producer could not build a graph';
  } catch (err) {
    if (err instanceof RateLimitedError) {
      // Retries are off by default, so the decision to wait is yours: sleep
      // until `err.resetAt`, or construct the client with `maxRetries` to opt in.
      return (
        `rate limited until ${err.resetAt?.toISOString() ?? '?'}: retry after ${err.retryAfter ?? '?'}s, ` +
        `${err.remaining ?? '?'}/${err.limit ?? '?'} left; raise it at ${err.signupUrl ?? 'n/a'}`
      );
    }
    if (err instanceof FilterUnavailableError) {
      // Not "no peers matched": the attribute is not collected at all.
      return `filter ${err.filter ?? '?'} is not collected; call facets() first`;
    }
    if (err instanceof CursorExpiredError) {
      // 🔴 Never auto-restart the walk: that mixes two graph generations, which
      // is exactly what the server refused to do. Start over deliberately.
      return 'cursor expired: start the walk again from page one';
    }
    if (err instanceof BadRequestError) {
      return `bad request on ${err.parameter ?? '?'}: ${err.detail ?? err.message}`;
    }
    if (err instanceof UnavailableError) {
      return 'the producer is down; try later';
    }
    if (err instanceof PeerApiError) {
      return `${err.kind}: ${err.message}`;
    }
    throw err;
  }
}
