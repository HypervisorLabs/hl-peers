/** IPv4 shape checking, shared by the query surface and the file algebra. */

/** The dotted-quad pattern of `spec/gossip-config.schema.json` `$defs/Ipv4`. */
const IPV4 =
  /^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$/;

/** `true` for a dotted-quad IPv4 literal with no leading zeros. */
export function isIpv4(value: unknown): value is string {
  return typeof value === 'string' && IPV4.test(value);
}

/** A cheap "this looks like an IPv6 literal" test, for a better message. */
export function looksLikeIpv6(value: string): boolean {
  return value.includes(':');
}

/**
 * CLIENT_SPEC §6.4(3) / §10 D-B. The refusal message is normative in substance:
 * IPv6 is out of contract and we will not write an untested value into a file
 * whose failure mode is a crash loop.
 */
export const IPV6_REASON =
  'IPv6 is out of contract: the peer API is IPv4-only and the node’s support for an ' +
  'IPv6 literal here is unverified. Refusing rather than writing an untested value into ' +
  'a file whose failure mode is a crash loop.';

/** The reason string for a value that is neither IPv4 nor IPv6-shaped. */
export function notIpv4Reason(value: string): string {
  return looksLikeIpv6(value) ? IPV6_REASON : `${value} is not an IPv4 address`;
}
