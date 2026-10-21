defmodule HlPeers.RetryTest do
  @moduledoc """
  The retry policy (`CLIENT_SPEC.md` §5.2, §9(3)), asserted on the **decisions**
  — which statuses retry, how many attempts, that `Retry-After` was read — and
  **never** on wall-clock delays. The sleep function is injected, so this file
  runs in microseconds and the jitter is irrelevant to it.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{Client, Query}
  alias HlPeers.Test.{FixtureAdapter, Fixtures}

  setup do
    {:ok, recorder} = FixtureAdapter.start_link()
    {:ok, sleeps} = Agent.start_link(fn -> [] end)

    client =
      Client.new(
        base_url: "http://fixtures.invalid",
        adapter: FixtureAdapter,
        adapter_opts: [recorder: recorder],
        # Retries are OFF by default; this file tests the policy
        # once a caller has opted in.
        max_retries: 3,
        sleep_fun: fn ms -> Agent.update(sleeps, &[ms | &1]) end,
        rand_fun: fn -> 1.0 end
      )

    %{client: client, recorder: recorder, sleeps: sleeps}
  end

  test "every 429 fixture's Retry-After parses to the seconds it expects", ctx do
    # §5.2: digits are seconds; an IMF-fixdate is max(0, date - now) against the
    # INJECTABLE clock; anything else is absent and the backoff takes over.
    for fixture <- Fixtures.http(), fixture["response"]["status"] == 429 do
      now =
        case get_in(fixture, ["clock", "now"]) do
          nil -> ~U[2026-09-21 02:35:00Z]
          iso -> elem(DateTime.from_iso8601(iso), 1)
        end

      client = %{ctx.client | clock_fun: fn -> now end}
      scenario = fixture["request"]["headers"]["X-Mock-Scenario"]

      {:error, error} =
        HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", scenario}])

      assert error.kind == :rate_limited
      assert error.retry_after == fixture["expect"]["retry_after_seconds"], fixture["name"]
      assert error.rate_limit.retry_after == fixture["expect"]["retry_after_seconds"]
      assert error.rate_limit.retry_after_raw == fixture["response"]["headers"]["Retry-After"]
    end
  end

  test "an HTTP-date Retry-After inside the cap is slept, not guessed", ctx do
    client = %{ctx.client | clock_fun: fn -> ~U[2026-09-21 02:35:00Z] end}

    {:error, error} =
      HlPeers.list_peers(client, %Query{},
        headers: [{"X-Mock-Scenario", "rate-limited-http-date"}]
      )

    assert error.retry_after == 60
    assert FixtureAdapter.request_count(ctx.recorder) == 4
    assert slept(ctx.sleeps) == [60_000, 60_000, 60_000]
  end

  test "an unparseable Retry-After is absent and the backoff takes over", ctx do
    {:error, error} =
      HlPeers.list_peers(ctx.client, %Query{},
        headers: [{"X-Mock-Scenario", "rate-limited-bad-retry-after"}]
      )

    assert error.kind == :rate_limited
    assert error.retry_after == nil
    assert error.rate_limit.retry_after_raw == "soon-ish"
    assert FixtureAdapter.request_count(ctx.recorder) == 4
    assert slept(ctx.sleeps) == [500, 1000, 2000]
  end

  test "a 429 inside the cap is retried, sleeping Retry-After", ctx do
    {:error, error} =
      HlPeers.list_peers(ctx.client, %Query{}, headers: [{"X-Mock-Scenario", "rate-limited"}])

    assert error.kind == :rate_limited
    assert error.retry_after == 42
    assert error.signup_url == "https://hypervisor.gg/docs/peer-api/authentication"
    # The 429 detail is prose (D-A) and is surfaced verbatim, never parsed.
    assert error.detail =~ "120/minute"
    assert error.rate_limit.remaining == 0

    # first attempt + max_retries
    assert FixtureAdapter.request_count(ctx.recorder) == 4
    assert slept(ctx.sleeps) == [42_000, 42_000, 42_000]
  end

  test "🔴 D-I: a Retry-After above the cap raises instead of sleeping", ctx do
    {:error, error} =
      HlPeers.list_peers(ctx.client, %Query{},
        headers: [{"X-Mock-Scenario", "rate-limited-long"}]
      )

    assert error.kind == :rate_limited
    assert error.retry_after == 3600
    assert FixtureAdapter.request_count(ctx.recorder) == 1
    assert slept(ctx.sleeps) == [], "must not block the caller for an hour, nor sleep the cap"
  end

  test "5xx is retried with exponential backoff", ctx do
    {:error, error} =
      HlPeers.list_peers(ctx.client, %Query{}, headers: [{"X-Mock-Scenario", "server-error"}])

    assert error.kind == :server
    assert FixtureAdapter.request_count(ctx.recorder) == 4
    # min(500 * 2^n, 30_000) with rand_fun pinned to 1.0
    assert slept(ctx.sleeps) == [500, 1000, 2000]
  end

  test "503 is retried and then reported as Unavailable", ctx do
    {:error, error} =
      HlPeers.list_peers(ctx.client, %Query{},
        headers: [{"X-Mock-Scenario", "producer-unavailable"}]
      )

    assert error.kind == :unavailable
    assert FixtureAdapter.request_count(ctx.recorder) == 4
  end

  test "a 503 that clears is not an error at all", ctx do
    FixtureAdapter.fail_first_n(ctx.recorder, 2)

    {:ok, page} = HlPeers.list_peers(ctx.client)

    assert page.count == 5
    assert FixtureAdapter.request_count(ctx.recorder) == 3
  end

  test "a transport failure is retried", ctx do
    FixtureAdapter.transport_error(ctx.recorder, :econnrefused)

    {:error, error} = HlPeers.list_peers(ctx.client)

    assert error.kind == :transport
    assert error.cause == :econnrefused
    assert FixtureAdapter.request_count(ctx.recorder) == 4
  end

  test "🔴 a 4xx other than 429 is never retried", ctx do
    {:error, error} = HlPeers.list_peers(ctx.client, Query.new(asn: ["AS24940"]))

    assert error.kind == :filter_unavailable
    assert FixtureAdapter.request_count(ctx.recorder) == 1
    assert slept(ctx.sleeps) == []
  end

  describe "🔴 retries are off by default" do
    setup ctx do
      client =
        Client.new(
          base_url: "http://fixtures.invalid",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: ctx.recorder],
          sleep_fun: fn ms -> Agent.update(ctx.sleeps, &[ms | &1]) end
        )

      %{default_client: client}
    end

    test "one 503 is exactly one request, and the caller's answer", ctx do
      FixtureAdapter.fail_first_n(ctx.recorder, 1)

      assert {:error, %{kind: :unavailable}} = HlPeers.list_peers(ctx.default_client)
      assert FixtureAdapter.request_count(ctx.recorder) == 1
      assert slept(ctx.sleeps) == []
    end

    test "a 429 returns at once, carrying reset_at and signup_url", ctx do
      {:error, error} =
        HlPeers.list_peers(ctx.default_client, %Query{},
          headers: [{"X-Mock-Scenario", "rate-limited"}]
        )

      assert error.kind == :rate_limited
      assert %DateTime{} = error.reset_at
      assert error.signup_url == "https://hypervisor.gg/docs/peer-api/authentication"
      assert FixtureAdapter.request_count(ctx.recorder) == 1
      assert slept(ctx.sleeps) == []
    end

    test "a transport failure returns at once", ctx do
      FixtureAdapter.transport_error(ctx.recorder, :econnrefused)

      assert {:error, %{kind: :transport}} = HlPeers.list_peers(ctx.default_client)
      assert FixtureAdapter.request_count(ctx.recorder) == 1
    end

    test "an explicit max_retries still opts in", ctx do
      FixtureAdapter.fail_first_n(ctx.recorder, 1)

      client =
        Client.new(
          base_url: "http://fixtures.invalid",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: ctx.recorder],
          sleep_fun: fn _ms -> :ok end,
          max_retries: 2
        )

      assert {:ok, page} = HlPeers.list_peers(client)
      assert page.status == 200
      assert FixtureAdapter.request_count(ctx.recorder) == 2
    end
  end

  describe "RateLimited.reset_at (§5.1)" do
    @now ~U[2026-09-21 02:35:00Z]

    test "is now + Retry-After from the injected clock", ctx do
      client = %{ctx.client | max_retries: 0, clock_fun: fn -> @now end}

      {:error, error} =
        HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", "rate-limited"}])

      assert error.reset_at == ~U[2026-09-21 02:35:42Z]
      assert error.api_version == "1"
    end

    test "an HTTP-date Retry-After lands on that instant", ctx do
      client = %{ctx.client | max_retries: 0, clock_fun: fn -> @now end}

      {:error, error} =
        HlPeers.list_peers(client, %Query{},
          headers: [{"X-Mock-Scenario", "rate-limited-http-date"}]
        )

      assert error.reset_at == ~U[2026-09-21 02:36:00Z]
    end

    test "falls back to now + RateLimit-Reset when Retry-After is unusable", ctx do
      client = %{ctx.client | max_retries: 0, clock_fun: fn -> @now end}

      {:error, error} =
        HlPeers.list_peers(client, %Query{},
          headers: [{"X-Mock-Scenario", "rate-limited-bad-retry-after"}]
        )

      assert error.retry_after == nil
      assert error.rate_limit.reset == 42
      assert error.reset_at == ~U[2026-09-21 02:35:42Z]
    end

    test "is nil when neither header is present" do
      response = %{
        status: 429,
        headers: [{"content-type", "application/problem+json"}],
        body: ~s({"type":"https://example.invalid/problems/rate-limited"})
      }

      error = Client.error_for(response, @now)
      assert error.reset_at == nil
      assert error.signup_url == nil
    end
  end

  test "max_retries: 0 means one attempt", ctx do
    client = %{ctx.client | max_retries: 0}

    {:error, _} =
      HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", "server-error"}])

    assert FixtureAdapter.request_count(ctx.recorder) == 1
  end

  test "the backoff is capped at 30 s and jittered into the lower half", ctx do
    high = Client.backoff(ctx.client, 20)
    assert high == 30_000

    low = Client.backoff(%{ctx.client | rand_fun: fn -> 0.0 end}, 0)
    assert low == 250
  end

  defp slept(agent), do: agent |> Agent.get(& &1) |> Enum.reverse()
end
