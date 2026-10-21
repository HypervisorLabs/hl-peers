/**
 * Four common questions, plus discovery and pagination.
 */
import {
  FilterUnavailableError,
  PeerApiClient,
  mempoolServers,
  notFull,
  peerQuery,
  sentries,
  tier1,
  type Peer,
} from '../src/index.ts';

export async function run(client: PeerApiClient): Promise<Record<string, unknown>> {
  // 1. peers in a geography that serve the mempool. `cc` is the broad geo
  //    key; `region` is set only for cloud-hosted peers. 🔴 mempoolServers()
  //    is serves_mempool=true, which EXCLUDES peers whose serves is unknown
  //    (`mempool.serves === null`): unknown is not "does not serve".
  const de = await client.listPeers(peerQuery().cc(['DE']));
  const q1 = await client.listPeers(mempoolServers().cc(['DE']));

  // 2. "not full" — accepting=true, exactly connectable=yes. 🪤 An estimate of
  //    whether the peer has room for another inbound peer, NOT a guarantee.
  const sg = await client.listPeers(notFull({ cc: ['SG'] }));

  // 3. tier 1 — EXACT. `maxTier(1)` is a ceiling and would also return tier 0.
  const t1 = await client.listPeers(tier1());

  // 4. sentries — label=sentry, our own identity set, not tier=0.
  const s = await client.listPeers(sentries());

  // A preset refines like any other query — `tier1().accepting(true)
  // .maxBlocksBehind(5)` is a better seed set than the published root list.
  // Here we page a plain query lazily; the walk stops on a null next_cursor.
  const walk = client.iterPeers(peerQuery().limit(2).sort('tier'), { maxItems: 100 });
  const paged: Peer[] = [];
  for await (const peer of walk) paged.push(peer);

  // Discovery: ask which filters exist before spending a request on one that
  // cannot answer.
  const facets = await client.facets();
  const asnUsable = 'notModified' in facets ? false : facets.isAvailable('asn');

  // 🔴 An unavailable filter is an error, never an empty list: an empty list
  // would say "there are none in that AS", and the truth is "we do not know".
  let asnAnswer = 'not attempted';
  try {
    await client.listPeers(peerQuery().asn(['AS24940']));
  } catch (err) {
    if (err instanceof FilterUnavailableError) {
      asnAnswer = `filter-unavailable: ${err.filter ?? '?'}`;
    } else {
      throw err;
    }
  }

  return {
    inDe: 'notModified' in de ? 0 : de.peers.length,
    mempoolInDe: 'notModified' in q1 ? [] : q1.peers.map((p) => `${p.id}:${p.mempool.source ?? '?'}`),
    notFullSg: 'notModified' in sg ? 0 : sg.total,
    tier1Total: 'notModified' in t1 ? 0 : t1.total,
    sentriesTotal: 'notModified' in s ? 0 : s.total,
    pagedIds: paged.map((p) => p.id),
    truncated: walk.truncated,
    asnUsable,
    asnAnswer,
  };
}
