/**
 * The closed error taxonomy of CLIENT_SPEC.md §5.1, as a discriminated union of
 * `Error` subclasses. Discriminate on {@link PeerApiError.kind} (or with
 * `instanceof`); never on a message.
 *
 * 🔴 `FilterUnavailableError`, `ChainNotServedError` and `CursorExpiredError`
 * extend {@link BadRequestError}, so a caller catching the 400 family
 * generically catches them too.
 */
import type { Problem, RateLimit } from './types.ts';

/** The `kind` discriminator; the taxonomy names of CLIENT_SPEC §5.1. */
export type ErrorKind =
  | 'RateLimited'
  | 'FilterUnavailable'
  | 'ChainNotServed'
  | 'CursorExpired'
  | 'BadRequest'
  | 'Unauthorized'
  | 'NotFound'
  | 'Unavailable'
  | 'Server'
  | 'Transport'
  | 'Decode'
  | 'Invalid'
  | 'Truncated';

/** An empty rate-limit block: every header absent. Never invents a value. */
export const NO_RATE_LIMIT: RateLimit = Object.freeze({
  limit: null,
  remaining: null,
  reset: null,
  policy: null,
  retryAfter: null,
  retryAfterRaw: null,
});

export interface PeerApiErrorOptions {
  rateLimit?: RateLimit;
  problem?: Problem | null;
  cause?: unknown;
  /** The `API-Version` header of the failed response, if any. */
  apiVersion?: string | null;
  /**
   * The client's (injectable) clock at response time, in epoch ms. Used by
   * {@link RateLimitedError} to compute `resetAt`; absent ⇒ `resetAt` is null.
   */
  now?: number;
}

/** Base class for every error this library raises. */
export abstract class PeerApiError extends Error {
  abstract readonly kind: ErrorKind;
  /** Parsed from **every** response — 200, 304 and every error alike. */
  readonly rateLimit: RateLimit;
  /** The raw problem document, or `null` for a non-problem+json body. */
  readonly problem: Problem | null;
  /**
   * The `API-Version` header of the failed response (`"1"` today), or `null`
   * when there was no response (client-side validation, transport) or no header.
   */
  readonly apiVersion: string | null;

  constructor(message: string, opts: PeerApiErrorOptions = {}) {
    super(message, opts.cause === undefined ? undefined : { cause: opts.cause });
    this.name = new.target.name;
    this.rateLimit = opts.rateLimit ?? NO_RATE_LIMIT;
    this.problem = opts.problem ?? null;
    this.apiVersion = opts.apiVersion ?? null;
  }
}

/**
 * `429`. Carries everything needed to decide whether to wait.
 *
 * Auto-retry is **off by default**, so a 429 reaches the caller at
 * once: sleep until {@link RateLimitedError.resetAt}, or opt in to retries
 * with `maxRetries`.
 */
export class RateLimitedError extends PeerApiError {
  readonly kind = 'RateLimited' as const;
  /** Delay in seconds, resolved from either form of `Retry-After` (§5.2). */
  readonly retryAfter: number | null;
  /** The raw `Retry-After` header, for diagnostics. */
  readonly retryAfterRaw: string | null;
  readonly limit: number | null;
  readonly remaining: number | null;
  readonly reset: number | null;
  readonly policy: string | null;
  /** Where to raise this limit, from the problem body (`null` if absent). */
  readonly signupUrl: string | null;
  /**
   * The absolute instant to wait until, from the client's injectable clock at
   * response time: `now + retryAfter` if `Retry-After` was usable, else
   * `now + reset` if `RateLimit-Reset` was, else `null`. Nothing needs to
   * parse a header.
   */
  readonly resetAt: Date | null;

  constructor(message: string, opts: PeerApiErrorOptions = {}) {
    super(message, opts);
    const rl = this.rateLimit;
    const wait = rl.retryAfter ?? rl.reset;
    this.resetAt = opts.now === undefined || wait === null ? null : new Date(opts.now + wait * 1000);
    this.retryAfter = rl.retryAfter;
    this.limit = rl.limit;
    this.remaining = rl.remaining;
    this.reset = rl.reset;
    this.policy = rl.policy;
    this.retryAfterRaw = rl.retryAfterRaw;
    const url = this.problem?.signup_url;
    this.signupUrl = typeof url === 'string' ? url : null;
  }
}

/** Any `400`. Parent of the three named 400 variants. */
export class BadRequestError extends PeerApiError {
  readonly kind: ErrorKind = 'BadRequest';
  readonly parameter: string | null;
  readonly allowed: readonly string[] | null;
  readonly detail: string | null;

  constructor(message: string, opts: PeerApiErrorOptions = {}) {
    super(message, opts);
    const p = this.problem;
    this.parameter = typeof p?.parameter === 'string' ? p.parameter : null;
    this.allowed = Array.isArray(p?.allowed) ? (p.allowed as string[]) : null;
    this.detail = typeof p?.detail === 'string' ? p.detail : null;
  }
}

/**
 * `400 …/filter-unavailable` — the attribute is **not collected**. 🔴 Never
 * swallow this into an empty list: an empty list would say "there are none",
 * and the truth is "we do not know".
 */
export class FilterUnavailableError extends BadRequestError {
  override readonly kind = 'FilterUnavailable' as const;
  /** The unavailable filter, from `problem.parameter`. */
  readonly filter: string | null;

  constructor(message: string, opts: PeerApiErrorOptions = {}) {
    super(message, opts);
    this.filter = this.parameter;
  }
}

/** `400 …/chain-not-served`. */
export class ChainNotServedError extends BadRequestError {
  override readonly kind = 'ChainNotServed' as const;
}

/**
 * `400 …/cursor-expired` — the cursor was issued in an earlier graph
 * generation. 🔴 A client never auto-restarts the walk; that would mix two
 * generations into one result set, which is exactly what the server refused to
 * do. Catch it and start over if that is what you want.
 */
export class CursorExpiredError extends BadRequestError {
  override readonly kind = 'CursorExpired' as const;
}

/** `401` — a key was supplied and is invalid. Omitting a key is NOT a 401. */
export class UnauthorizedError extends PeerApiError {
  readonly kind = 'Unauthorized' as const;
  readonly detail: string | null;
  constructor(message: string, opts: PeerApiErrorOptions = {}) {
    super(message, opts);
    this.detail = typeof this.problem?.detail === 'string' ? this.problem.detail : null;
  }
}

/** `404`, and a missing local file in the `gossip-fs` `load()` helper. */
export class NotFoundError extends PeerApiError {
  readonly kind = 'NotFound' as const;
  readonly detail: string | null;
  /** Set when the 404 is a missing local file rather than an HTTP answer. */
  readonly path: string | null;
  constructor(message: string, opts: PeerApiErrorOptions & { path?: string } = {}) {
    super(message, opts);
    this.detail = typeof this.problem?.detail === 'string' ? this.problem.detail : null;
    this.path = opts.path ?? null;
  }
}

/**
 * `503`, **and** an `available: false` envelope where a config would otherwise
 * be built (CLIENT_SPEC §10, D-G). An unbuildable graph is not an empty peer
 * set, and turning it into one would empty an operator's root list.
 */
export class UnavailableError extends PeerApiError {
  readonly kind = 'Unavailable' as const;
  readonly detail: string | null;
  constructor(message: string, opts: PeerApiErrorOptions = {}) {
    super(message, opts);
    this.detail = typeof this.problem?.detail === 'string' ? this.problem.detail : null;
  }
}

/** Any other `5xx`. */
export class ServerError extends PeerApiError {
  readonly kind = 'Server' as const;
  readonly status: number;
  /** At most 512 bytes, truncated on a UTF-8 boundary. */
  readonly bodyExcerpt: string;
  constructor(
    message: string,
    status: number,
    bodyExcerpt: string,
    opts: PeerApiErrorOptions = {},
  ) {
    super(message, opts);
    this.status = status;
    this.bodyExcerpt = bodyExcerpt;
  }
}

/** Connect/read timeout, DNS, TLS, connection reset. */
export class TransportError extends PeerApiError {
  readonly kind = 'Transport' as const;
}

/** The body is not JSON, or does not match the schema. */
export class DecodeError extends PeerApiError {
  readonly kind = 'Decode' as const;
  readonly bodyExcerpt: string;
  constructor(message: string, bodyExcerpt = '', opts: PeerApiErrorOptions = {}) {
    super(message, opts);
    this.bodyExcerpt = bodyExcerpt;
  }
}

/** Client-side validation: CLIENT_SPEC §2.2, §4.3, §4.4, §6.4. */
export class InvalidError extends PeerApiError {
  readonly kind = 'Invalid' as const;
  /** The offending parameter, e.g. `root_node_ips[0]`, `chain`, `$`. */
  readonly parameter: string;
  readonly reason: string;
  constructor(parameter: string, reason: string, opts: PeerApiErrorOptions = {}) {
    super(`${parameter}: ${reason}`, opts);
    this.parameter = parameter;
    this.reason = reason;
  }
}

/** `iterPeers` hit `maxItems` with `onTruncate: 'raise'`. */
export class TruncatedError extends PeerApiError {
  readonly kind = 'Truncated' as const;
  readonly seen: number;
  constructor(seen: number) {
    super(
      `iterPeers stopped at the maxItems guard after ${seen} peers. ` +
        `Raise maxItems deliberately, or narrow the query.`,
    );
    this.seen = seen;
  }
}

/** Every error this library raises. */
export type AnyPeerApiError =
  | RateLimitedError
  | BadRequestError
  | FilterUnavailableError
  | ChainNotServedError
  | CursorExpiredError
  | UnauthorizedError
  | NotFoundError
  | UnavailableError
  | ServerError
  | TransportError
  | DecodeError
  | InvalidError
  | TruncatedError;
