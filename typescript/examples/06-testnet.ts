/**
 * Testnet (beta): list testnet peers and their hardfork versions.
 *
 * `network` picks which network's graph is read. Unset sends nothing, so the
 * server answers for mainnet. Set it per call (`peerQuery().network(...)`, or
 * the `network` option of `getPeer` and the network map), or once on the
 * client; a per-call value wins.
 *
 * 🪤 Testnet is beta: a small graph, no session data yet, an approximate
 * `blocks_behind`, and a testnet reset means `available: false` for a while.
 */
import {
  BadRequestError,
  PeerApiClient,
  peerQuery,
  type PeerApiClientOptions,
} from '../src/index.ts';

export async function run(options: PeerApiClientOptions = {}): Promise<string[]> {
  const client = new PeerApiClient({ ...options });

  let result;
  try {
    result = await client.listPeers(peerQuery().network('testnet'));
  } catch (err) {
    // A deployment that does not serve testnet answers 400 network-not-served:
    // a plain BadRequest whose parameter is "network", never a mainnet answer.
    if (err instanceof BadRequestError && err.parameter === 'network') {
      return ['testnet is not served by this deployment'];
    }
    throw err;
  }
  if ('notModified' in result) return [];

  // A testnet reset leaves the graph unavailable for a while; that is not a
  // finding about the network.
  if (!result.envelope.available) return ['the testnet graph is rebuilding; try again later'];

  const lines = [`network=${result.envelope.network ?? 'mainnet'} chain=${result.envelope.chain ?? '?'}`];
  for (const p of result.peers) {
    // `version` is the node's reported hardfork version: an opaque string,
    // null when the node did not report one.
    lines.push(`${p.id} version=${p.version ?? 'unreported'} cc=${p.cc ?? '-'} asn=${p.asn ?? '-'}`);
  }
  return lines;
}

/** The same question with a client-level default: every call reads testnet. */
export async function viaClientDefault(options: PeerApiClientOptions = {}): Promise<string[]> {
  const client = new PeerApiClient({ ...options, network: 'testnet' });
  const peer = await client.getPeer('192.0.2.50');
  if ('notModified' in peer) return [];
  return [`${peer.peer.id} version=${peer.peer.version ?? 'unreported'}`];
}
