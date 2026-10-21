defmodule HlPeers.Diff do
  @moduledoc """
  A minimal unified diff, used by `HlPeers.GossipConfig.save/3` under
  `dry_run: true` so an operator can read exactly what would change in the file
  their node boots from, before anything is written.

  Three lines of context, `---`/`+++` headers naming the path, `\\n` line
  endings. A missing target renders as a diff against an empty file.
  """

  @context 3

  @doc """
  Renders a unified diff of `old` against `new`.

      iex> HlPeers.Diff.unified("a\\nb\\n", "a\\nc\\n", "f.json")
      "--- f.json\\n+++ f.json\\n@@ -1,2 +1,2 @@\\n a\\n-b\\n+c\\n"

      iex> HlPeers.Diff.unified("same\\n", "same\\n", "f.json")
      ""
  """
  @spec unified(String.t(), String.t(), String.t()) :: String.t()
  def unified(old, new, path) when is_binary(old) and is_binary(new) do
    if old == new do
      ""
    else
      ops = ops(lines(old), lines(new))

      case hunks(ops) do
        [] -> ""
        hunks -> IO.iodata_to_binary(["--- ", path, "\n+++ ", path, "\n", render(hunks)])
      end
    end
  end

  defp lines(""), do: []

  defp lines(text) do
    text |> String.split("\n") |> trim_trailing_empty()
  end

  # "a\nb\n" is two lines, not three.
  defp trim_trailing_empty(parts) do
    case List.last(parts) do
      "" -> Enum.drop(parts, -1)
      _ -> parts
    end
  end

  defp ops(old_lines, new_lines) do
    old_lines
    |> List.myers_difference(new_lines)
    |> Enum.flat_map(fn {tag, chunk} -> Enum.map(chunk, &{tag, &1}) end)
  end

  # Walks the op list, keeping line numbers, and cuts it into hunks that hold
  # every change plus @context lines either side.
  defp hunks(ops) do
    numbered =
      ops
      |> Enum.reduce({[], 1, 1}, fn
        {:eq, line}, {acc, a, b} -> {[{:eq, line, a, b} | acc], a + 1, b + 1}
        {:del, line}, {acc, a, b} -> {[{:del, line, a, b} | acc], a + 1, b}
        {:ins, line}, {acc, a, b} -> {[{:ins, line, a, b} | acc], a, b + 1}
      end)
      |> elem(0)
      |> Enum.reverse()

    keep = keep_indexes(numbered)

    numbered
    |> Enum.with_index()
    |> Enum.filter(fn {_op, i} -> MapSet.member?(keep, i) end)
    |> group_runs()
  end

  defp keep_indexes(numbered) do
    changed =
      numbered
      |> Enum.with_index()
      |> Enum.filter(fn {{tag, _, _, _}, _} -> tag != :eq end)
      |> Enum.map(&elem(&1, 1))

    last = length(numbered) - 1

    Enum.reduce(changed, MapSet.new(), fn i, acc ->
      lo = max(i - @context, 0)
      hi = min(i + @context, last)
      Enum.reduce(lo..hi, acc, &MapSet.put(&2, &1))
    end)
  end

  defp group_runs(indexed) do
    indexed
    |> Enum.chunk_while(
      nil,
      fn {op, i}, acc ->
        case acc do
          nil -> {:cont, {[op], i}}
          {ops, prev} when i == prev + 1 -> {:cont, {[op | ops], i}}
          {ops, _} -> {:cont, Enum.reverse(ops), {[op], i}}
        end
      end,
      fn
        nil -> {:cont, nil}
        {ops, _} -> {:cont, Enum.reverse(ops), nil}
      end
    )
    |> Enum.reject(&is_nil/1)
  end

  defp render(hunks), do: Enum.map(hunks, &render_hunk/1)

  defp render_hunk(ops) do
    {a_start, a_len} = span(ops, [:eq, :del], fn {_, _, a, _} -> a end)
    {b_start, b_len} = span(ops, [:eq, :ins], fn {_, _, _, b} -> b end)

    header = "@@ -#{a_start},#{a_len} +#{b_start},#{b_len} @@\n"
    [header | Enum.map(ops, &render_op/1)]
  end

  defp span(ops, tags, get) do
    nums = for {tag, _, _, _} = op <- ops, tag in tags, do: get.(op)

    case nums do
      [] -> {0, 0}
      _ -> {Enum.min(nums), length(nums)}
    end
  end

  defp render_op({:eq, line, _, _}), do: [" ", line, "\n"]
  defp render_op({:del, line, _, _}), do: ["-", line, "\n"]
  defp render_op({:ins, line, _, _}), do: ["+", line, "\n"]
end
