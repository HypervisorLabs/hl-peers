/**
 * Gossip-config recipes: build from a query, update a file in place, add and
 * remove a root, dry-run a diff, and pin peers with keepExisting.
 *
 * 🔴 The node reads `override_gossip_config.json` **when it
 * starts**. Nothing here restarts, signals or otherwise disturbs a node: when to
 * restart is the operator's decision, at the operator's moment.
 */
import { GossipConfig, type PeerApiClient, peerQuery } from '../src/index.ts';
import { load, save, updateGossipConfig } from '../src/gossip-fs.ts';

/**
 * Build a config from a query and print it — never writing anything.
 *
 * 🪤 `tier1().accepting(true)` is a materially better seed set than the info
 * API's `gossipRootIps`, which is a 25-entry foundation-sentry directory with
 * an observed acceptance rate around 0.9% against roughly 86% for tier 1.
 */
export async function toStdout(client: PeerApiClient): Promise<string> {
  const { json } = await updateGossipConfig(client, peerQuery().cc(['DE']), undefined, {
    chain: 'Mainnet',
    maxRoots: 8,
  });
  return json; // `hl-peers gossip update --stdout > override_gossip_config.json`
}

/** Update an operator's file in place, keeping two peers pinned. */
export async function updateInPlace(client: PeerApiClient, path: string): Promise<string[]> {
  const result = await updateGossipConfig(client, peerQuery().cc(['DE']), path, {
    maxRoots: 8,
    // 🪤 exclude beats keepExisting, and the conflict comes back as a warning.
    keepExisting: ['203.0.113.7'],
    exclude: ['198.51.100.7'],
    backup: true,
  });
  return [
    `changed=${String(result.changed)} written=${result.writtenPath ?? 'nothing'}`,
    ...result.warnings.map((w) => `warning ${w.code}: ${w.message}`),
  ];
}

/** See what would change, and write nothing. */
export async function dryRun(client: PeerApiClient, path: string): Promise<string> {
  const result = await updateGossipConfig(client, peerQuery().cc(['DE']), path, {
    dryRun: true,
  });
  return result.diff;
}

/** Offline file algebra: no network, no clock, no environment. */
export function addAndRemoveRoots(path: string): string {
  const config = load(path);
  config.addRootIp('203.0.113.42'); // a duplicate add is a no-op, not an error
  config.removeRootIp('203.0.113.9'); // removing an absent IP is a no-op too
  config.setReservedPeerIps(['198.51.100.20']); // 🪤 bare strings here
  config.setTryNewPeers(false);

  // validate() returns warnings; save() runs it first, so an invalid config
  // never reaches the filesystem.
  const { warnings } = config.validate();
  const result = save(config, path, { backup: true });
  return [
    `written=${String(result.written)}`,
    ...warnings.map((w) => w.code),
  ].join(' ');
}

/** Build one from scratch, with no API at all. */
export function fromScratch(): string {
  const { config, warnings } = GossipConfig.fromPeers(['192.0.2.10', '192.0.2.11'], {
    chain: 'Mainnet', // 🔴 required: never guessed
    tryNewPeers: true,
    maxRoots: 8,
  });
  for (const w of warnings) process.stderr.write(`warning [${w.code}] ${w.message}\n`);
  return config.toJson();
}
