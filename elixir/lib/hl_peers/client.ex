defmodule HlPeers.Client do
  @moduledoc """
  Client configuration, the request lifecycle, and the retry policy
  (`CLIENT_SPEC.md` §1, §4.5, §5).

  | option | default | notes |
  |---|---|---|
  | `:base_url` | `"https://api.hypervisor.gg"` | the public API host |
  | `:base_path` | `""` (none) | route prefix for every call — only for a proxy that mounts the API under one |
  | `:api_key` | `nil` | absent ⇒ anonymous, a **supported tier, not an error** |
  | `:timeout` | `10_000` ms | total **per attempt**, connect + read |
  | `:user_agent` | `"hl-peers-elixir/<vsn>"` | |
  | `:user_agent_suffix` | `nil` | appended after a space, for downstream apps |
  | `:max_retries` | **`0`** | attempts *after* the first. 🔴 Auto-retry is **off** by default; set e.g. `3` to opt in |
  | `:max_retry_after` | `60` s | over this, a `429` raises instead of sleeping (D-I) |
  | `:min_poll_interval` | `60_000` ms | guard on `HlPeers.Poller` only |
  | `:cache` | `true` (64 entries, LRU) | `false` disables it; an integer sets the capacity |
  | `:adapter` | `HlPeers.Adapter.Req` | injectable, which is how the suite runs offline |
  | `:clock_fun` | `&DateTime.utc_now/0` | the injectable clock: HTTP-date `Retry-After` and `RateLimited.reset_at` |
  | `:network` | `nil` | the default `network` (`"mainnet"` or `"testnet"`, or the atoms) for every call that sets none; a per-call value wins. `nil` sends nothing (mainnet). **Testnet is beta** |
  | `:suppress_placeholder_warning` | — | accepted and ignored |

  `base_url` resolution order, evaluated once at construction: explicit argument
  → `HL_PEER_API_BASE_URL` → `https://api.hypervisor.gg`. `api_key`: explicit
  argument → `HL_PEER_API_KEY` → `nil`.

  `network` is validated where it is used, like any other parameter: an
  out-of-enum default answers `Invalid{parameter: "network"}` on the first call,
  before any request is sent.

  `base_path` is the prefix every route is built under (`<base_path>/peers`,
  `<base_path>/peers/{ip}`, `<base_path>/peers/facets`,
  `<base_path>/network-map/graph`, `<base_path>/network-map/summary`),
  normalized by `normalize_base_path/1`. The public API serves **plain paths at
  the host root** (the version is in every body and in the `API-Version`
  header), so the default — spelled once, in `default_base_path/0` — is the
  empty prefix.

  The API key is sent as `Authorization: Bearer …` and is never logged, never
  put in a query string and never included in an error message; a diagnostic
  that must mention it redacts it to `hlp_live_…`.

  ## `API-Version`

  The server sends `API-Version: 1` on every response, errors included. It is
  exposed as `:api_version` (a string, or `nil`) on every result and every
  error. It is informational: this client never fails on a different value — a
  breaking change ships under new paths.

  ## Retries (§5.2) — off unless you ask

  🔴 With the default `max_retries: 0` a `429` returns
  `%HlPeers.Error{kind: :rate_limited}` (carrying `reset_at`, the instant to
  sleep until) and a `5xx` / transport failure returns at once: the library
  never sleeps behind your back. With `max_retries: n > 0`, up to `n` more
  attempts are made for `429` (sleeping `Retry-After`, **capped**), `5xx`
  including `503`, and transport failures — the last two with
  `min(500ms * 2^n, 30s) * (0.5 + rand()/2)` jitter. 🔴 **Never** a `4xx` other
  than `429`: a `400`, `401` or `404` is deterministic, and retrying it burns
  quota to receive the same answer.

  A `Retry-After` may be a delay in seconds **or** an RFC 9110 IMF-fixdate; both
  are handled (§5.2), the date against `:clock_fun` so a test never depends on
  wall time, and a value that is neither is treated as absent — the backoff takes
  over rather than the request failing over a bad header.

  🔴 D-I: a `Retry-After` **larger** than `:max_retry_after` raises
  `%HlPeers.Error{kind: :rate_limited}` immediately rather than blocking your
  process for minutes. Sleeping the cap and retrying anyway would just spend the
  attempt.
  """

  alias HlPeers.{Error, RateLimit, Store}

  @default_base_url "https://api.hypervisor.gg"
  @default_base_path ""
  @version Mix.Project.config()[:version]

  @type t :: %__MODULE__{}

  defstruct base_url: @default_base_url,
            base_path: @default_base_path,
            api_key: nil,
            timeout: 10_000,
            user_agent: "hl-peers-elixir/#{@version}",
            user_agent_suffix: nil,
            max_retries: 0,
            max_retry_after: 60,
            min_poll_interval: 60_000,
            adapter: HlPeers.Adapter.Req,
            adapter_opts: [],
            store: nil,
            cache?: true,
            sleep_fun: nil,
            rand_fun: nil,
            clock_fun: nil,
            network: nil

  @doc "The default `base_url`, `\"https://api.hypervisor.gg\"`."
  @spec default_base_url() :: String.t()
  def default_base_url, do: @default_base_url

  @doc """
  The default route prefix, `\"\"` — plain paths at the host root. The one place
  it is spelled.
  """
  @spec default_base_path() :: String.t()
  def default_base_path, do: @default_base_path

  @doc """
  `CLIENT_SPEC.md` §1: strip every leading/trailing `/`; empty means no prefix,
  anything else is `/` + the trimmed value.

      iex> HlPeers.Client.normalize_base_path("peer-api/v1/")
      "/peer-api/v1"
      iex> HlPeers.Client.normalize_base_path("/")
      ""
  """
  @spec normalize_base_path(String.t()) :: String.t()
  def normalize_base_path(path) when is_binary(path) do
    case String.trim(path, "/") do
      "" -> ""
      trimmed -> "/" <> trimmed
    end
  end

  @doc """
  Builds a client.

      iex> client = HlPeers.Client.new(base_url: "http://127.0.0.1:8931")
      iex> client.base_url
      "http://127.0.0.1:8931"

  Anonymous is a supported tier, so this is a complete, working client. A key
  only raises the page ceiling.
  """
  @spec new(keyword()) :: t()
  def new(opts \\ []) do
    base_url =
      opts[:base_url] || System.get_env("HL_PEER_API_BASE_URL") || @default_base_url

    api_key = opts[:api_key] || System.get_env("HL_PEER_API_KEY")
    cache? = Keyword.get(opts, :cache, true) != false
    capacity = if is_integer(opts[:cache]), do: opts[:cache], else: 64

    {:ok, store} = Store.start_link(capacity: capacity)

    %__MODULE__{
      base_url: String.trim_trailing(base_url, "/"),
      base_path: normalize_base_path(Keyword.get(opts, :base_path, @default_base_path)),
      api_key: api_key,
      timeout: Keyword.get(opts, :timeout, 10_000),
      user_agent: Keyword.get(opts, :user_agent, "hl-peers-elixir/#{@version}"),
      user_agent_suffix: opts[:user_agent_suffix],
      max_retries: Keyword.get(opts, :max_retries, 0),
      max_retry_after: Keyword.get(opts, :max_retry_after, 60),
      min_poll_interval: Keyword.get(opts, :min_poll_interval, 60_000),
      adapter: Keyword.get(opts, :adapter, HlPeers.Adapter.Req),
      adapter_opts: Keyword.get(opts, :adapter_opts, []),
      store: store,
      cache?: cache?,
      sleep_fun: Keyword.get(opts, :sleep_fun, &Process.sleep/1),
      rand_fun: Keyword.get(opts, :rand_fun, &:rand.uniform/0),
      clock_fun: Keyword.get(opts, :clock_fun, &DateTime.utc_now/0),
      network: opts[:network]
    }
  end

  @doc "Empties this client's ETag cache."
  @spec clear_cache(t()) :: :ok
  def clear_cache(%__MODULE__{store: store}), do: Store.clear(store)

  @doc """
  Issues one `GET`, with the ETag cache, the error taxonomy and the retry policy
  applied. Returns a raw response map; the typed decoding lives in `HlPeers`.

  The response map is `%{status:, headers:, body:, rate_limit:, api_version:,
  etag:, from_cache:, not_modified:}`; `not_modified: true` happens only when
  the **caller** supplied `If-None-Match`.
  """
  @spec get(t(), String.t(), [{String.t(), String.t()}], keyword()) ::
          {:ok, map()} | {:error, Error.t()}
  def get(%__MODULE__{} = client, path, params \\ [], opts \\ []) do
    query_string =
      Enum.map_join(params, "&", fn {k, v} ->
        HlPeers.Query.encode_component(k) <> "=" <> HlPeers.Query.encode_component(v)
      end)

    url = client.base_url <> path <> if(query_string == "", do: "", else: "?" <> query_string)
    caller_inm = caller_if_none_match(opts)
    key = Store.key("GET", path, query_string, not is_nil(client.api_key))

    cached =
      if client.cache? and is_nil(caller_inm), do: Store.fetch(client.store, key), else: nil

    headers = headers(client, opts, caller_inm || (cached && cached.etag))
    request = %{method: :get, url: url, headers: headers, timeout: client.timeout}

    with {:ok, response} <- attempt(client, request, 0) do
      finish(client, response, key, cached, caller_inm)
    end
  end

  defp caller_if_none_match(opts) do
    opts
    |> Keyword.get(:headers, [])
    |> Enum.find_value(fn {k, v} ->
      if String.downcase(to_string(k)) == "if-none-match", do: v
    end)
  end

  defp headers(client, opts, if_none_match) do
    extra =
      opts
      |> Keyword.get(:headers, [])
      |> Enum.reject(fn {k, _} -> String.downcase(to_string(k)) == "if-none-match" end)

    [
      {"accept", "application/json"},
      {"user-agent", user_agent(client)}
    ]
    |> maybe([{"authorization", "Bearer #{client.api_key}"}], client.api_key)
    |> maybe([{"if-none-match", if_none_match}], if_none_match)
    |> Kernel.++(extra)
  end

  defp maybe(headers, _extra, nil), do: headers
  defp maybe(headers, extra, _present), do: headers ++ extra

  defp user_agent(%{user_agent: ua, user_agent_suffix: nil}), do: ua
  defp user_agent(%{user_agent: ua, user_agent_suffix: suffix}), do: ua <> " " <> suffix

  # ------------------------------------------------------------------
  # attempts and retries
  # ------------------------------------------------------------------

  defp attempt(client, request, n) do
    case client.adapter.request(request, client.adapter_opts) do
      {:ok, %{status: status} = response} when status in 200..399 ->
        {:ok, response}

      {:ok, %{status: 429} = response} ->
        retry_429(client, request, response, n)

      {:ok, %{status: status} = response} when status >= 500 ->
        retry_or_fail(client, request, n, fn -> {:error, error_for(response, now(client))} end)

      {:ok, response} ->
        # 🔴 Never retry a 4xx other than 429 — it is deterministic.
        {:error, error_for(response, now(client))}

      {:error, reason} ->
        error = Error.new(:transport, cause: reason, rate_limit: %RateLimit{})
        retry_or_fail(client, request, n, fn -> {:error, error} end)
    end
  end

  defp retry_429(client, request, response, n) do
    rate_limit = RateLimit.from_headers(response.headers, now(client))
    error = error_for(response, now(client))

    cond do
      n >= client.max_retries ->
        {:error, error}

      is_nil(rate_limit.retry_after) ->
        # Absent or unparseable: fall back to the backoff.
        sleep(client, backoff(client, n))
        attempt(client, request, n + 1)

      rate_limit.retry_after > client.max_retry_after ->
        # D-I: do not sleep the cap and retry — let the caller decide whether to
        # wait ten minutes.
        {:error, error}

      true ->
        sleep(client, rate_limit.retry_after * 1000)
        attempt(client, request, n + 1)
    end
  end

  defp retry_or_fail(client, request, n, give_up) do
    if n >= client.max_retries do
      give_up.()
    else
      sleep(client, backoff(client, n))
      attempt(client, request, n + 1)
    end
  end

  @doc """
  The backoff of §5.2: `min(500ms * 2^n, 30s) * (0.5 + rand()/2)` — full-width
  jitter in the lower half, so two clients started together diverge.

  The randomness source is the language default and is **not** required to match
  across languages; conformance tests never assert on delays.
  """
  @spec backoff(t(), non_neg_integer()) :: non_neg_integer()
  def backoff(%__MODULE__{rand_fun: rand}, n) do
    base = min(500 * Integer.pow(2, n), 30_000)
    trunc(base * (0.5 + rand.() / 2))
  end

  defp sleep(%__MODULE__{sleep_fun: sleep}, ms), do: sleep.(ms)

  # The one place this client reads a clock on the HTTP path, and it is injected.
  defp now(%__MODULE__{clock_fun: clock}), do: clock.()

  # ------------------------------------------------------------------
  # response assembly
  # ------------------------------------------------------------------

  defp finish(client, %{status: 304} = response, key, cached, caller_inm) do
    rate_limit = RateLimit.from_headers(response.headers, now(client))

    cond do
      not is_nil(caller_inm) ->
        {:ok,
         %{
           status: 304,
           headers: response.headers,
           body: "",
           rate_limit: rate_limit,
           api_version: header(response, "api-version"),
           etag: header(response, "etag") || caller_inm,
           from_cache: false,
           not_modified: true
         }}

      cached ->
        Store.touch(client.store, key)

        {:ok,
         %{
           status: 304,
           headers: response.headers,
           body: cached.body,
           # §4.5: the rate-limit headers come from the 304, not the stored ones.
           rate_limit: rate_limit,
           api_version: header(response, "api-version"),
           etag: cached.etag,
           from_cache: true,
           not_modified: false
         }}

      true ->
        {:error,
         Error.new(:decode,
           cause: :unsolicited_304,
           rate_limit: rate_limit,
           api_version: header(response, "api-version"),
           body_excerpt: "the server answered 304 to a request that carried no If-None-Match"
         )}
    end
  end

  defp finish(client, response, key, _cached, _caller_inm) do
    etag = header(response, "etag")
    if client.cache? and etag, do: Store.put(client.store, key, etag, response.body)

    {:ok,
     %{
       status: response.status,
       headers: response.headers,
       body: response.body,
       rate_limit: RateLimit.from_headers(response.headers, now(client)),
       api_version: header(response, "api-version"),
       etag: etag,
       from_cache: false,
       not_modified: false
     }}
  end

  defp header(%{headers: headers}, name) do
    headers |> RateLimit.normalize() |> Map.get(name)
  end

  # ------------------------------------------------------------------
  # error mapping — §5.1
  # ------------------------------------------------------------------

  @doc false
  @spec error_for(map()) :: Error.t()
  def error_for(%{status: status} = response, now \\ nil) do
    now = now || DateTime.utc_now()
    rate_limit = RateLimit.from_headers(response.headers, now)
    problem = problem(response)

    status
    |> build_error(problem, rate_limit, response, now)
    |> Map.put(:api_version, header(response, "api-version"))
  end

  # D-K: problem+json is parsed; any other body shape falls back to status-only
  # mapping, so a legacy `{"error": …}` body still produces the right variant.
  defp problem(%{headers: headers, body: body}) do
    content_type = headers |> RateLimit.normalize() |> Map.get("content-type", "")

    if String.contains?(content_type, "problem+json") do
      case Jason.decode(body) do
        {:ok, map} when is_map(map) -> map
        _ -> nil
      end
    end
  end

  defp build_error(429, problem, rate_limit, response, now) do
    Error.new(:rate_limited,
      retry_after: rate_limit.retry_after,
      reset_at: reset_at(rate_limit, now),
      signup_url: problem && problem["signup_url"],
      detail: problem && problem["detail"],
      problem: problem,
      status: 429,
      rate_limit: rate_limit,
      body_excerpt: excerpt(response)
    )
  end

  defp build_error(400, problem, rate_limit, response, _now) do
    common = [
      problem: problem,
      status: 400,
      rate_limit: rate_limit,
      detail: problem && problem["detail"],
      allowed: problem && problem["allowed"],
      parameter: problem && problem["parameter"],
      body_excerpt: excerpt(response)
    ]

    # Discrimination is on problem.type, by suffix match on the path segment —
    # never on title or detail, because prose is not an API.
    case problem_suffix(problem) do
      "filter-unavailable" ->
        Error.new(:filter_unavailable, Keyword.put(common, :filter, problem["parameter"]))

      "chain-not-served" ->
        Error.new(:chain_not_served, common)

      "cursor-expired" ->
        Error.new(:cursor_expired, common)

      _ ->
        Error.new(:bad_request, common)
    end
  end

  defp build_error(401, problem, rate_limit, response, _now),
    do: simple(:unauthorized, 401, problem, rate_limit, response)

  defp build_error(404, problem, rate_limit, response, _now),
    do: simple(:not_found, 404, problem, rate_limit, response)

  defp build_error(503, problem, rate_limit, response, _now),
    do: simple(:unavailable, 503, problem, rate_limit, response)

  defp build_error(status, problem, rate_limit, response, _now) when status >= 500 do
    Error.new(:server,
      status: status,
      problem: problem,
      detail: problem && problem["detail"],
      rate_limit: rate_limit,
      body_excerpt: excerpt(response)
    )
  end

  defp build_error(status, problem, rate_limit, response, _now),
    do: simple(:bad_request, status, problem, rate_limit, response)

  # §5.1: the instant to sleep until — `now + retry_after`, else
  # `now + RateLimit-Reset`, else nil — from the injectable clock, so a caller
  # never parses a header and a test never reads wall time.
  defp reset_at(%RateLimit{retry_after: seconds}, now) when is_integer(seconds),
    do: DateTime.add(now, seconds, :second)

  defp reset_at(%RateLimit{reset: seconds}, now) when is_integer(seconds),
    do: DateTime.add(now, seconds, :second)

  defp reset_at(%RateLimit{}, _now), do: nil

  defp simple(kind, status, problem, rate_limit, response) do
    Error.new(kind,
      status: status,
      problem: problem,
      detail: problem && problem["detail"],
      parameter: problem && problem["parameter"],
      allowed: problem && problem["allowed"],
      rate_limit: rate_limit,
      body_excerpt: excerpt(response)
    )
  end

  defp problem_suffix(nil), do: nil

  defp problem_suffix(problem) do
    case problem["type"] do
      type when is_binary(type) -> type |> String.split("/") |> List.last()
      _ -> nil
    end
  end

  # At most 512 bytes, truncated on a UTF-8 boundary.
  defp excerpt(%{body: body}) when is_binary(body) do
    body
    |> binary_part(0, min(byte_size(body), 512))
    |> then(&String.chunk(&1, :valid))
    |> List.first()
    |> Kernel.||("")
  end

  defp excerpt(_), do: ""
end
