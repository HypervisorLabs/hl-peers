/**
 * Quickstart: ask a question, read the answer honestly.
 *
 * Every example in this directory is executed by `test/examples.test.ts`
 * against the fixtures, so nothing in the README can rot.
 */
import { PeerApiClient, peerQuery, type PeerApiClientOptions } from '../src/index.ts';

export async function run(options: PeerApiClientOptions = {}): Promise<string[]> {
  const client = new PeerApiClient({
    // base_url resolves: argument -> HL_PEER_API_BASE_URL -> https://api.hypervisor.gg.
    // api_key resolves: argument -> HL_PEER_API_KEY -> null (anonymous, which
    // is a supported tier, not an error).
    ...options,
  });

  const result = await client.listPeers(peerQuery().cc(['DE']));
  if ('notModified' in result) return [];

  // 🔴 `available: false` is not "no peers matched" — it means the producer
  // could not build a graph at all. Check it before `stale`.
  if (!result.envelope.available) {
    throw new Error('the peer graph is unavailable; this is not a finding about the network');
  }

  const lines = result.peers.map(
    (p) =>
      `${p.id} tier=${p.tier === null ? 'unplaced' : p.tier} cc=${p.cc ?? '-'} ` +
      // three-state, never folded into a boolean
      `connectable=${p.connectable ?? 'unknown'}`,
  );

  lines.push(
    `quota: ${result.rateLimit.remaining ?? '?'}/${result.rateLimit.limit ?? '?'} ` +
      `resets in ${result.rateLimit.reset ?? '?'}s`,
  );
  return lines;
}
