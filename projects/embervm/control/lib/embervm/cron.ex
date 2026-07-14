defmodule Embervm.Cron do
  @moduledoc """
  A minimal, dependency-free 5-field cron parser + next-fire calculator.

  Deliberately NOT a hex dependency: adding one to the control plane's hermetic
  Bazel hex closure is a heavy, multi-file change for what a small pure function
  covers. The five fields are `minute hour day-of-month month day-of-week`, each
  supporting `*`, `*/step`, `a-b` ranges, `a,b,c` lists, and bare values; the
  day-of-week field is 0-6 with 0 = Sunday.

  `next/2` returns the first minute STRICTLY AFTER the given time that matches
  (fires happen on the minute boundary), which is what makes misfires during
  downtime skipped rather than replayed: the adapter always computes the next
  fire from the current time, so a fire whose minute passed while the control
  plane was down is simply never in the future window.

  Day-of-month vs day-of-week follows the Vixie-cron rule: when BOTH are
  restricted (neither is `*`), a day matches if EITHER matches; otherwise both
  must match (the unrestricted `*` field is always satisfied).
  """

  @type field :: MapSet.t(non_neg_integer())
  @type t :: %{minute: field, hour: field, dom: field, month: field, dow: field, dom_star: boolean(), dow_star: boolean()}

  @minute_range 0..59
  @hour_range 0..23
  @dom_range 1..31
  @month_range 1..12
  @dow_range 0..6

  # Safety bound: a valid cron matches within a year; give up past that so a
  # malformed spec that never matches cannot loop forever.
  @max_lookahead_minutes 366 * 24 * 60

  @doc """
  Parse a 5-field cron string into a compiled matcher, or `{:error, reason}`.
  """
  @spec parse(String.t()) :: {:ok, t()} | {:error, term()}
  def parse(expr) when is_binary(expr) do
    case expr |> String.trim() |> String.split(~r/\s+/, trim: true) do
      [minute, hour, dom, month, dow] ->
        with {:ok, m} <- field(minute, @minute_range),
             {:ok, h} <- field(hour, @hour_range),
             {:ok, d} <- field(dom, @dom_range),
             {:ok, mo} <- field(month, @month_range),
             {:ok, w} <- field(dow, @dow_range) do
          {:ok,
           %{
             minute: m,
             hour: h,
             dom: d,
             month: mo,
             dow: w,
             dom_star: String.trim(dom) == "*",
             dow_star: String.trim(dow) == "*"
           }}
        end

      parts ->
        {:error, {:expected_5_fields, length(parts)}}
    end
  end

  def parse(_), do: {:error, :not_a_string}

  @doc """
  The first `%DateTime{}` (in `from`'s time zone, UTC in practice) strictly after
  `from` that matches `cron`. `{:error, :no_match}` for a spec that matches
  nothing within a year (a malformed field combination).
  """
  @spec next(t(), DateTime.t()) :: {:ok, DateTime.t()} | {:error, :no_match}
  def next(%{} = cron, %DateTime{} = from) do
    # Start at the next whole minute after `from` (truncate to minute, +60s).
    start =
      from
      |> Map.put(:second, 0)
      |> Map.put(:microsecond, {0, 0})
      |> DateTime.add(60, :second)

    search(cron, start, 0)
  end

  defp search(_cron, _dt, n) when n > @max_lookahead_minutes, do: {:error, :no_match}

  defp search(cron, dt, n) do
    if matches?(cron, dt) do
      {:ok, dt}
    else
      search(cron, DateTime.add(dt, 60, :second), n + 1)
    end
  end

  @doc "Whether `dt` (at minute resolution) matches `cron`."
  @spec matches?(t(), DateTime.t()) :: boolean()
  def matches?(cron, %DateTime{} = dt) do
    MapSet.member?(cron.minute, dt.minute) and
      MapSet.member?(cron.hour, dt.hour) and
      MapSet.member?(cron.month, dt.month) and
      day_matches?(cron, dt)
  end

  # Vixie rule: both restricted -> OR; otherwise the restricted one(s) must match.
  defp day_matches?(cron, dt) do
    dom_ok = MapSet.member?(cron.dom, dt.day)
    dow_ok = MapSet.member?(cron.dow, day_of_week(dt))

    cond do
      not cron.dom_star and not cron.dow_star -> dom_ok or dow_ok
      true -> dom_ok and dow_ok
    end
  end

  # Date.day_of_week/1 is 1 (Monday)..7 (Sunday); cron wants 0 (Sunday)..6.
  defp day_of_week(dt) do
    case Date.day_of_week(DateTime.to_date(dt)) do
      7 -> 0
      n -> n
    end
  end

  # -- field parsing ---------------------------------------------------------

  defp field(spec, range) do
    spec
    |> String.split(",", trim: true)
    |> Enum.reduce_while({:ok, MapSet.new()}, fn part, {:ok, acc} ->
      case term(part, range) do
        {:ok, set} -> {:cont, {:ok, MapSet.union(acc, set)}}
        {:error, _} = err -> {:halt, err}
      end
    end)
    |> case do
      {:ok, set} -> if MapSet.size(set) == 0, do: {:error, {:empty_field, spec}}, else: {:ok, set}
      err -> err
    end
  end

  # One comma-separated term: "*", "*/n", "a-b", "a-b/n", "a".
  defp term("*", range), do: {:ok, MapSet.new(range)}

  defp term(part, range) do
    case String.split(part, "/", parts: 2) do
      [base, step_str] ->
        with {:ok, step} <- positive_int(step_str),
             {:ok, base_range} <- base_range(base, range) do
          {:ok, base_range |> Enum.take_every(step) |> MapSet.new()}
        end

      [base] ->
        with {:ok, base_range} <- base_range(base, range) do
          {:ok, MapSet.new(base_range)}
        end
    end
  end

  # The base of a term (before any "/step"): "*", "a-b", or "a".
  defp base_range("*", range), do: {:ok, Enum.to_list(range)}

  defp base_range(spec, range) do
    case String.split(spec, "-", parts: 2) do
      [a, b] ->
        with {:ok, lo} <- int_in(a, range),
             {:ok, hi} <- int_in(b, range) do
          if lo <= hi, do: {:ok, Enum.to_list(lo..hi)}, else: {:error, {:reversed_range, spec}}
        end

      [a] ->
        with {:ok, v} <- int_in(a, range), do: {:ok, [v]}
    end
  end

  defp int_in(str, range) do
    case Integer.parse(str) do
      {n, ""} -> if n in range, do: {:ok, n}, else: {:error, {:out_of_range, n}}
      _ -> {:error, {:not_an_int, str}}
    end
  end

  defp positive_int(str) do
    case Integer.parse(str) do
      {n, ""} when n > 0 -> {:ok, n}
      _ -> {:error, {:bad_step, str}}
    end
  end
end
