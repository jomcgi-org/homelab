defmodule Embervm.InstanceTable do
  @moduledoc """
  ETS row and live/banked count helpers shared by the lifecycle stores. Each
  store owns its table and serializes writes through its own GenServer; these
  functions only run inside those callbacks.
  """

  @doc "Looks up one row by id."
  @spec fetch(:ets.table(), term()) :: {:ok, map()} | {:error, {:not_found, term()}}
  def fetch(table, id) do
    case :ets.lookup(table, id) do
      [{^id, row}] -> {:ok, row}
      [] -> {:error, {:not_found, id}}
    end
  end

  @doc "Rewrites an existing row with `fun`. `:error` for an unknown id."
  @spec update(:ets.table(), term(), (map() -> map())) :: {:ok, map()} | :error
  def update(table, id, fun) do
    case fetch(table, id) do
      {:ok, row} ->
        updated = fun.(row)
        :ets.insert(table, {id, updated})
        {:ok, updated}

      {:error, _} ->
        :error
    end
  end

  @doc "Every row, in no particular order."
  @spec all(:ets.table()) :: [map()]
  def all(table), do: :ets.foldl(fn {_id, row}, acc -> [row | acc] end, [], table)

  @doc "The rows of one workload, newest `created_at` first."
  @spec list(:ets.table(), term()) :: [map()]
  def list(table, workload) do
    :ets.foldl(
      fn {_id, row}, acc ->
        if row.workload == workload, do: [row | acc], else: acc
      end,
      [],
      table
    )
    |> Enum.sort_by(& &1.created_at, :desc)
  end

  @doc """
  Moves one unit of `workload`'s count from `prior_bucket` to `next_bucket`. A nil
  bucket is not counted, and a count never drops below zero.
  """
  @spec bump_counts(map(), term(), :live | :banked | nil, :live | :banked | nil) :: map()
  def bump_counts(counts, workload, prior_bucket, next_bucket) do
    workload_counts =
      counts
      |> Map.get(workload, %{live: 0, banked: 0})
      |> dec_bucket(prior_bucket)
      |> inc_bucket(next_bucket)

    Map.put(counts, workload, workload_counts)
  end

  defp inc_bucket(counts, nil), do: counts
  defp inc_bucket(counts, bucket), do: Map.update!(counts, bucket, &(&1 + 1))

  defp dec_bucket(counts, nil), do: counts
  defp dec_bucket(counts, bucket), do: Map.update!(counts, bucket, &max(&1 - 1, 0))
end
