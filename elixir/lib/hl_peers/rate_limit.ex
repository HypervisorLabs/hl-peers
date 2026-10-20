defmodule HlPeers.RateLimit do
  @moduledoc """
  The `RateLimit-*` / `Retry-After` block, parsed from **every** response — 200,
  304 and every error alike (`CLIENT_SPEC.md` §3.3).

  Every member is nullable. A missing or unparseable header is `nil`, **never**
  `0`: "no header" and "zero remaining" are different facts and this library
  never invents the second from the first.

  `reset` is **seconds from now**, not an epoch. `policy` is the raw header
  string, deliberately unparsed.

  `Retry-After` follows §5.2: an all-digits value is a delay in seconds;
  anything else is parsed as an RFC 9110 **IMF-fixdate** and converted with
  `max(0, date - now)` against the client's **injectable clock**, so a test never
  depends on wall time; a value that parses as neither is **absent** (`nil`) and
  the caller falls back to the backoff. The raw header is kept in
  `:retry_after_raw` for diagnostics.
  """

  @type t :: %__MODULE__{
          limit: non_neg_integer() | nil,
          remaining: non_neg_integer() | nil,
          reset: non_neg_integer() | nil,
          policy: String.t() | nil,
          retry_after: non_neg_integer() | nil,
          retry_after_raw: String.t() | nil
        }

  defstruct [:limit, :remaining, :reset, :policy, :retry_after, :retry_after_raw]

  @months %{
    "Jan" => 1,
    "Feb" => 2,
    "Mar" => 3,
    "Apr" => 4,
    "May" => 5,
    "Jun" => 6,
    "Jul" => 7,
    "Aug" => 8,
    "Sep" => 9,
    "Oct" => 10,
    "Nov" => 11,
    "Dec" => 12
  }

  @doc """
  Parses a header list (names case-insensitive; values either binaries or the
  `[binary]` lists Req hands back). `now` is the instant an HTTP-date
  `Retry-After` is measured against — inject it; never read wall time here.

      iex> rl = HlPeers.RateLimit.from_headers([{"ratelimit-limit", "20"}, {"Retry-After", "42"}])
      iex> {rl.limit, rl.retry_after, rl.remaining}
      {20, 42, nil}

      iex> HlPeers.RateLimit.from_headers([{"RateLimit-Remaining", "not-a-number"}]).remaining
      nil

      iex> headers = [{"Retry-After", "Mon, 21 Sep 2026 02:36:00 GMT"}]
      iex> HlPeers.RateLimit.from_headers(headers, ~U[2026-09-21 02:35:00Z]).retry_after
      60

      iex> headers = [{"Retry-After", "Mon, 21 Sep 2026 02:30:00 GMT"}]
      iex> HlPeers.RateLimit.from_headers(headers, ~U[2026-09-21 02:35:00Z]).retry_after
      0

      iex> rl = HlPeers.RateLimit.from_headers([{"Retry-After", "soon-ish"}])
      iex> {rl.retry_after, rl.retry_after_raw}
      {nil, "soon-ish"}
  """
  @spec from_headers(Enumerable.t(), DateTime.t() | nil) :: t()
  def from_headers(headers, now \\ nil) do
    h = normalize(headers)
    raw = h["retry-after"]

    %__MODULE__{
      limit: integer(h["ratelimit-limit"]),
      remaining: integer(h["ratelimit-remaining"]),
      reset: integer(h["ratelimit-reset"]),
      policy: h["ratelimit-policy"],
      retry_after: retry_after(raw, now),
      retry_after_raw: raw
    }
  end

  @doc """
  §5.2's `Retry-After` rule: digits are seconds, an IMF-fixdate is
  `max(0, date - now)`, anything else is absent.
  """
  @spec retry_after(String.t() | nil, DateTime.t() | nil) :: non_neg_integer() | nil
  def retry_after(nil, _now), do: nil

  def retry_after(value, now) when is_binary(value) do
    case integer(value) do
      nil -> http_date_delay(value, now)
      seconds -> seconds
    end
  end

  defp http_date_delay(value, now) do
    case imf_fixdate(String.trim(value)) do
      {:ok, at} -> max(DateTime.diff(at, now || DateTime.utc_now(), :second), 0)
      :error -> nil
    end
  end

  # RFC 9110 IMF-fixdate: "Mon, 21 Sep 2026 02:36:00 GMT". The obsolete formats
  # are not accepted; an unrecognised value is absent, never a guess.
  defp imf_fixdate(value) do
    with [_, day, month, year, hour, minute, second] <-
           Regex.run(
             ~r/^[A-Za-z]{3}, (\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/,
             value
           ),
         {:ok, month} <- Map.fetch(@months, month),
         {:ok, date} <- Date.new(String.to_integer(year), month, String.to_integer(day)),
         {:ok, time} <-
           Time.new(
             String.to_integer(hour),
             String.to_integer(minute),
             String.to_integer(second)
           ) do
      DateTime.new(date, time, "Etc/UTC")
    else
      _ -> :error
    end
  end

  @doc false
  @spec normalize(Enumerable.t()) :: %{optional(String.t()) => String.t()}
  def normalize(headers) do
    Map.new(headers, fn {k, v} ->
      {k |> to_string() |> String.downcase(), flatten(v)}
    end)
  end

  defp flatten([v | _]), do: to_string(v)
  defp flatten([]), do: nil
  defp flatten(v) when is_binary(v), do: v
  defp flatten(v), do: to_string(v)

  # A non-integer or absent header is nil. Never invent a value. An HTTP-date
  # Retry-After is *permitted* by §3.3 to be parsed; we treat it as absent
  # rather than introduce a clock into a value type.
  defp integer(nil), do: nil

  defp integer(value) when is_binary(value) do
    case Integer.parse(String.trim(value)) do
      {n, ""} when n >= 0 -> n
      _ -> nil
    end
  end
end
