defmodule Embervm.Cell do
  @moduledoc """
  Cell identity and workload ownership lookup.

  `cell-0` is the explicit compatibility default. Workload assignments are
  durable in the op-log backend and mirrored into an ETS table by
  `Embervm.WorkloadWatcher`. The active bit is informer state: a durable owner
  remains available to cleanup reconciliation after its Workload CR is deleted,
  while request routing refuses an inactive or unknown workload.
  """

  @default_id "cell-0"
  @assignment_table :embervm_workload_cells
  @id_re ~r/^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$/

  @type assignment :: %{cell_id: String.t(), active: boolean()}

  @spec default_id() :: String.t()
  def default_id, do: @default_id

  @spec assignment_table() :: atom()
  def assignment_table, do: @assignment_table

  @spec valid_id?(term()) :: boolean()
  def valid_id?(id), do: is_binary(id) and Regex.match?(@id_re, id)

  @spec current() :: String.t()
  def current do
    Application.get_env(:embervm, :cell_id) || env_cell_id()
  end

  @spec known_ids() :: [String.t()]
  def known_ids do
    Application.get_env(:embervm, :known_cell_ids) || env_known_ids(current())
  end

  @doc false
  @spec configuration!() :: {String.t(), [String.t()]}
  def configuration! do
    cell_id = env_cell_id()
    known_ids = env_known_ids(cell_id)

    unless valid_id?(cell_id) do
      raise "EMBERVM_CELL_ID must be a DNS label, got #{inspect(cell_id)}"
    end

    invalid = Enum.reject(known_ids, &valid_id?/1)

    if invalid != [] do
      raise "EMBERVM_KNOWN_CELL_IDS contains invalid cell ids: #{inspect(invalid)}"
    end

    unless cell_id in known_ids do
      raise "EMBERVM_CELL_ID #{inspect(cell_id)} is not present in EMBERVM_KNOWN_CELL_IDS"
    end

    {cell_id, known_ids}
  end

  @spec create(atom()) :: atom()
  def create(table \\ @assignment_table) do
    case :ets.whereis(table) do
      :undefined -> :ets.new(table, [:set, :public, :named_table, read_concurrency: true])
      _ -> table
    end
  end

  @spec seed(atom(), [map()]) :: :ok
  def seed(table \\ @assignment_table, rows) do
    Enum.each(rows, fn row ->
      workload = Map.get(row, :workload) || Map.get(row, "workload")
      cell_id = Map.get(row, :cell_id) || Map.get(row, "cell_id")

      if is_binary(workload) and workload != "" and valid_id?(cell_id) do
        :ets.insert(table, {workload, %{cell_id: cell_id, active: false}})
      end
    end)

    :ok
  end

  @spec put(atom(), String.t(), String.t(), boolean()) :: true
  def put(table \\ @assignment_table, workload, cell_id, active \\ true) do
    :ets.insert(table, {workload, %{cell_id: cell_id, active: active}})
  end

  @spec deactivate(atom(), String.t()) :: true
  def deactivate(table \\ @assignment_table, workload) do
    case fetch(table, workload) do
      {:ok, assignment} -> :ets.insert(table, {workload, %{assignment | active: false}})
      :error -> true
    end
  end

  @spec fetch(atom(), String.t()) :: {:ok, assignment()} | :error
  def fetch(table \\ @assignment_table, workload) do
    if :ets.whereis(table) == :undefined do
      :error
    else
      case :ets.lookup(table, workload) do
        [{^workload, assignment}] -> {:ok, assignment}
        [] -> :error
      end
    end
  end

  @doc "Return whether this control plane may route an active workload."
  @spec route(String.t(), atom(), String.t()) :: :owned | {:error, term()}
  def route(workload, table \\ @assignment_table, current_cell \\ current()) do
    if :ets.whereis(table) == :undefined do
      # A pre-cell single-control-plane test or local process has no watcher.
      # Keep the historical cell-0 behavior, while every non-default cell fails
      # closed until its durable assignment cache is available.
      if current_cell == @default_id, do: :owned, else: {:error, :assignment_unavailable}
    else
      case fetch(table, workload) do
        {:ok, %{cell_id: ^current_cell, active: true}} -> :owned
        {:ok, %{cell_id: assigned, active: true}} -> {:error, {:wrong_cell, assigned}}
        {:ok, %{active: false}} -> {:error, :unknown_workload}
        :error -> {:error, :unknown_assignment}
      end
    end
  end

  @doc "Return durable ownership for cleanup, including deleted Workload CRs."
  @spec owner(String.t(), atom()) :: {:ok, String.t()} | :error
  def owner(workload, table \\ @assignment_table) do
    case fetch(table, workload) do
      {:ok, %{cell_id: cell_id}} -> {:ok, cell_id}
      :error -> :error
    end
  end

  @spec active_names(atom()) :: [String.t()]
  def active_names(table \\ @assignment_table) do
    if :ets.whereis(table) == :undefined do
      []
    else
      table
      |> :ets.tab2list()
      |> Enum.flat_map(fn
        {workload, %{active: true}} -> [workload]
        _ -> []
      end)
    end
  end

  defp env_cell_id do
    case System.get_env("EMBERVM_CELL_ID") do
      nil -> @default_id
      value -> String.trim(value)
    end
  end

  defp env_known_ids(_cell_id) do
    case System.get_env("EMBERVM_KNOWN_CELL_IDS") do
      nil -> [@default_id]
      value -> value |> String.split(",", trim: true) |> Enum.map(&String.trim/1) |> Enum.uniq()
    end
  end
end
