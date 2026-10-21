defmodule HlPeers.Error do
  @moduledoc """
  The one closed error taxonomy of `CLIENT_SPEC.md` §5.1, as a single exception
  struct discriminated by `:kind`.

  Every public function in this library answers `{:ok, term()} | {:error, t()}`.
  The `!` variants raise this struct.

  | `:kind` | raised on |
  |---|---|
  | `:rate_limited` | `429` |
  | `:filter_unavailable` | a `400` whose `problem.type` ends `/filter-unavailable` |
  | `:chain_not_served` | a `400` … `/chain-not-served` |
  | `:cursor_expired` | a `400` … `/cursor-expired` |
  | `:bad_request` | any other `400` |
  | `:unauthorized` | `401` |
  | `:not_found` | `404`, and a missing config file in `HlPeers.GossipConfig.load/1` |
  | `:unavailable` | `503`, and `available: false` where a config would otherwise be built |
  | `:server` | any other `5xx` |
  | `:transport` | connect/read timeout, DNS, TLS, connection reset |
  | `:decode` | the body is not JSON, or does not match the schema |
  | `:invalid` | client-side validation |
  | `:truncated` | `stream_peers/2` hit `max_items` with `on_truncate: :raise` |

  `:filter_unavailable`, `:chain_not_served` and `:cursor_expired` are members of
  the **400 family**: `bad_request?/1` is true for all four, so a caller that
  wants to treat every 400 alike can, without losing the discriminator.

  Discrimination is on `problem.type` by suffix match, never on `title` or
  `detail` — prose is not an API. 🔴 The rate-limit figures quoted in a `429`
  `detail` are prose and disagree with the plan's own tier table (D-A); they are
  surfaced verbatim in `:detail` and never parsed.

  Every error carries `:rate_limit` (all-`nil` when no header was seen) and
  `:api_version` (the `API-Version` response header, `nil` for a locally raised
  error).

  A `:rate_limited` error carries `:reset_at` — an absolute UTC `DateTime`,
  computed from the client's injectable clock at response time as
  `now + retry_after` if `Retry-After` is known, else `now + RateLimit-Reset`,
  else `nil`. It is what you sleep until; nothing needs to parse a header. With
  the default `max_retries: 0` the client never sleeps for you:

      case HlPeers.list_peers(client, query) do
        {:error, %HlPeers.Error{kind: :rate_limited, reset_at: %DateTime{} = at}} ->
          Process.sleep(max(DateTime.diff(at, DateTime.utc_now(), :millisecond), 0))

        other ->
          other
      end

  `:signup_url` comes from the problem body (`nil` if absent).
  """

  @type kind ::
          :rate_limited
          | :filter_unavailable
          | :chain_not_served
          | :cursor_expired
          | :bad_request
          | :unauthorized
          | :not_found
          | :unavailable
          | :server
          | :transport
          | :decode
          | :invalid
          | :truncated

  @type t :: %__MODULE__{
          kind: kind(),
          message: String.t(),
          parameter: String.t() | nil,
          reason: String.t() | nil,
          detail: String.t() | nil,
          filter: String.t() | nil,
          allowed: [String.t()] | nil,
          signup_url: String.t() | nil,
          retry_after: non_neg_integer() | nil,
          reset_at: DateTime.t() | nil,
          status: non_neg_integer() | nil,
          path: String.t() | nil,
          seen: non_neg_integer() | nil,
          body_excerpt: String.t() | nil,
          cause: term(),
          problem: map() | nil,
          rate_limit: HlPeers.RateLimit.t() | nil,
          api_version: String.t() | nil
        }

  defexception [
    :kind,
    :message,
    :parameter,
    :reason,
    :detail,
    :filter,
    :allowed,
    :signup_url,
    :retry_after,
    :reset_at,
    :status,
    :path,
    :seen,
    :body_excerpt,
    :cause,
    :problem,
    :rate_limit,
    :api_version
  ]

  @four_hundred_family [:bad_request, :filter_unavailable, :chain_not_served, :cursor_expired]

  @doc """
  Builds an error. `:message` is derived from the other fields when not given.

      iex> err = HlPeers.Error.new(:invalid, parameter: "chain", reason: "case matters")
      iex> {err.kind, err.parameter, err.message}
      {:invalid, "chain", "invalid chain: case matters"}
  """
  @spec new(kind(), keyword()) :: t()
  def new(kind, fields \\ []) do
    fields = Map.new(fields)
    struct = struct!(__MODULE__, Map.put(fields, :kind, kind))

    # 🔴 §5.1: *every* error carries the rate-limit block, including one raised
    # locally before any request went out. An all-`nil` block says "no header
    # was seen"; a `nil` block would make a caller write two code paths for the
    # same field, and the four clients would not agree on which they got.
    %{
      struct
      | message: struct.message || describe(struct),
        rate_limit: struct.rate_limit || %HlPeers.RateLimit{}
    }
  end

  @doc """
  True for every member of the 400 family.

      iex> HlPeers.Error.bad_request?(HlPeers.Error.new(:cursor_expired))
      true
      iex> HlPeers.Error.bad_request?(HlPeers.Error.new(:not_found))
      false
  """
  @spec bad_request?(t()) :: boolean()
  def bad_request?(%__MODULE__{kind: kind}), do: kind in @four_hundred_family

  @doc false
  @spec invalid(String.t(), String.t()) :: t()
  def invalid(parameter, reason), do: new(:invalid, parameter: parameter, reason: reason)

  @impl true
  def message(%__MODULE__{message: m}) when is_binary(m), do: m
  def message(%__MODULE__{} = e), do: describe(e)

  defp describe(%__MODULE__{kind: :invalid, parameter: p, reason: r}) when is_binary(p),
    do: "invalid #{p}" <> if(r, do: ": #{r}", else: "")

  defp describe(%__MODULE__{kind: :rate_limited} = e),
    do:
      "rate limited" <>
        if(e.retry_after, do: " (retry after #{e.retry_after}s)", else: "") <>
        if(e.detail, do: ": #{e.detail}", else: "")

  defp describe(%__MODULE__{kind: :filter_unavailable} = e),
    do: "filter unavailable: #{e.filter}" <> if(e.detail, do: " — #{e.detail}", else: "")

  defp describe(%__MODULE__{kind: :transport, cause: cause}),
    do: "transport error: #{inspect(cause)}"

  defp describe(%__MODULE__{kind: :decode} = e),
    do: "decode error: #{inspect(e.cause)}" <> if(e.parameter, do: " at #{e.parameter}", else: "")

  defp describe(%__MODULE__{kind: :truncated, seen: seen}),
    do: "the peer stream hit max_items after #{seen} peers"

  defp describe(%__MODULE__{kind: :not_found, path: path}) when is_binary(path),
    do: "no such file: #{path}"

  defp describe(%__MODULE__{kind: kind} = e) do
    base = kind |> Atom.to_string() |> String.replace("_", " ")
    if e.detail, do: "#{base}: #{e.detail}", else: base
  end
end
