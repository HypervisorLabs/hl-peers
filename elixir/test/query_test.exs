defmodule HlPeers.QueryTest do
  @moduledoc "Query serialization and the deliberately thin local validation (§2)."

  use ExUnit.Case, async: true

  alias HlPeers.Query

  describe "serialization" do
    test "parameters are lexicographic by name, so two clients share a CDN cache key" do
      query = Query.new(tier: [1], cc: ["DE"], accepting: true, sort: "tier")

      assert Query.to_query_string(query) == "accepting=true&cc=DE&sort=tier&tier=1"
    end

    test "a CSV comma is percent-encoded — no special case" do
      assert Query.to_query_string(Query.new(cc: ["DE", "NL"])) == "cc=DE%2CNL"
    end

    test "cc is upper-cased" do
      assert Query.to_query_string(Query.new(cc: ["de"])) == "cc=DE"
    end

    test "a pre-joined string is accepted too" do
      assert Query.to_query_string(Query.new(cc: "DE,NL")) == "cc=DE%2CNL"
    end

    test "unset values and empty lists are omitted entirely, never ?cc=" do
      assert Query.to_query_string(Query.new(cc: [], region: nil)) == ""
    end

    test "the absent-tier member serializes as the string null" do
      assert Query.to_query_string(Query.new(tier: [nil])) == "tier=null"
      assert Query.to_query_string(Query.new(tier: [0, nil])) == "tier=0%2Cnull"
    end

    test "tier and max_tier are different questions and both may be sent" do
      query = Query.new(tier: [1], max_tier: 2)

      assert Query.to_query_string(query) == "max_tier=2&tier=1"
    end

    test "setters are immutable and chain" do
      base = Query.new(tier: [1])
      derived = base |> Query.cc(["DE"]) |> Query.limit(10)

      assert Query.to_query_string(base) == "tier=1"
      assert Query.to_query_string(derived) == "cc=DE&limit=10&tier=1"
    end
  end

  describe "validation" do
    test "seen_within must match ^[0-9]+[mhd]$" do
      assert :ok = Query.validate(Query.new(seen_within: "90m"))
      assert {:error, %{parameter: "seen_within"}} = Query.validate(Query.new(seen_within: "90"))
    end

    test "min_uptime is a fraction, not a percentage" do
      assert :ok = Query.validate(Query.new(min_uptime: 0.95))
      assert {:error, %{parameter: "min_uptime"}} = Query.validate(Query.new(min_uptime: 95))
    end

    test "negative ceilings and limit < 1 are refused" do
      assert {:error, %{parameter: "max_tier"}} = Query.validate(Query.new(max_tier: -1))
      assert {:error, %{parameter: "limit"}} = Query.validate(Query.new(limit: 0))
    end

    test "max_latency_ms without a vantage is refused" do
      assert {:error, %{parameter: "vantage"}} = Query.validate(Query.new(max_latency_ms: 80))
      assert :ok = Query.validate(Query.new(max_latency_ms: 80, vantage: "eu"))
    end

    test "closed enums are checked locally" do
      assert {:error, %{parameter: "connectable"}} =
               Query.validate(Query.new(connectable: ["yess"]))

      assert {:error, %{parameter: "chain"}} = Query.validate(Query.new(chain: "mainnet"))
      assert :ok = Query.validate(Query.new(state: ["full", "syncing"]))
    end

    test "🔴 open sets are NOT validated locally — the server names the problem" do
      # cc, region, asn, role, label, version and fields may grow additively
      # within v1; guessing here would replace a 400 that names the problem with
      # a client-side error that does not.
      assert :ok = Query.validate(Query.new(cc: ["XX"], role: ["something-new"]))
      assert :ok = Query.validate(Query.new(asn: ["AS1"], label: ["a-future-label"]))
    end

    test "limit is not clamped locally" do
      assert Query.to_query_string(Query.new(limit: 100_000)) == "limit=100000"
    end
  end

  describe "presets" do
    test "tier1 is exact tier 1" do
      assert Query.to_query_string(Query.tier1()) == "tier=1"
    end

    test "sentries is label=sentry, not tier=0" do
      assert Query.to_query_string(Query.sentries()) == "label=sentry"
    end

    test "not_full takes a tagged geo argument" do
      assert Query.to_query_string(Query.not_full(region: ["aws:eu-central-1"])) ==
               "accepting=true&region=aws%3Aeu-central-1"
    end

    test "not_full is accepting=true" do
      assert Query.to_query_string(Query.not_full(cc: ["SG"])) == "accepting=true&cc=SG"
    end

    test "a bare geo string is refused; the argument is tagged" do
      assert_raise ArgumentError, fn -> Query.not_full(country: "DE") end
    end
  end

  describe "facets" do
    test "takes every filter, and refuses the six non-filters" do
      assert :ok =
               Query.validate_for_facets(
                 Query.new(cc: ["DE"], tier: [1], label: ["s"], serves_mempool: [nil])
               )

      for field <- ~w(sort limit cursor fields format try_new_peers)a do
        value =
          %{sort: "tier", limit: 1, cursor: "c", fields: ["id"], format: "json"}
          |> Map.get(field, true)

        assert {:error, %{kind: :invalid, parameter: p}} =
                 Query.validate_for_facets(Query.new([{field, value}]))

        assert p == Atom.to_string(field)
      end
    end
  end

  describe "country, serves_mempool, auction, try_new_peers fields" do
    test "country is upper-cased like cc and kept separate on the wire" do
      assert Query.to_query_string(Query.new(country: ["jp", "de"], cc: ["nl"])) ==
               "cc=NL&country=JP%2CDE"

      assert Query.to_query_string(Query.new(country: "jp")) == "country=JP"
    end

    test "serves_mempool serializes true/false/nil as true/false/null" do
      assert Query.to_query_string(Query.new(serves_mempool: [true, nil])) ==
               "serves_mempool=true%2Cnull"

      assert Query.to_query_string(Query.new(serves_mempool: [false])) == "serves_mempool=false"
      assert Query.to_query_string(Query.new(serves_mempool: [nil])) == "serves_mempool=null"

      assert Query.to_query_string(Query.new(serves_mempool: "true,null")) ==
               "serves_mempool=true%2Cnull"
    end

    test "auction serializes its members and nil as null" do
      assert Query.to_query_string(Query.new(auction: ["bid_before", nil])) ==
               "auction=bid_before%2Cnull"

      assert Query.to_query_string(Query.new(auction: [:holds_slot])) == "auction=holds_slot"
    end

    test "try_new_peers is a boolean scalar" do
      assert Query.to_query_string(Query.new(try_new_peers: false)) == "try_new_peers=false"
    end

    test "serves_mempool and auction are closed enums, case matters" do
      assert :ok = Query.validate(Query.new(serves_mempool: [true, false, nil]))
      assert :ok = Query.validate(Query.new(auction: ~w(holds_slot bidding bid_before) ++ [nil]))

      assert {:error, %{parameter: "serves_mempool"}} =
               Query.validate(Query.new(serves_mempool: ["yes"]))

      assert {:error, %{parameter: "auction"}} = Query.validate(Query.new(auction: ["holding"]))

      assert {:error, %{parameter: "auction"}} =
               Query.validate(Query.new(auction: ["HOLDS_SLOT"]))
    end

    test "state adds responsive and open" do
      assert :ok = Query.validate(Query.new(state: ["responsive", "open"]))
      assert {:error, %{parameter: "state"}} = Query.validate(Query.new(state: ["FULL"]))
    end

    test "mempool_servers is serves_mempool=true and composes with cc" do
      assert Query.to_query_string(Query.mempool_servers()) == "serves_mempool=true"

      assert Query.to_query_string(Query.mempool_servers() |> Query.cc(["DE"])) ==
               "cc=DE&serves_mempool=true"
    end
  end
end
