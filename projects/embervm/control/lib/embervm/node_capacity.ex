defmodule Embervm.NodeCapacity do
  @moduledoc """
  Pure ETS accessor for the node capacity facts the control plane learns from
  each daemon's `WatchNode` stream: NO process of its own. `Embervm.NodeRegistry`
  owns the table's lifecycle (creates it on init, writes a node's facts on every
  `NodeStatus`, deletes them the instant the node stops being dispatchable), and
  this module is just the read/write surface both the registry and readers
  (Task 11's dispatcher) call against a table name, never a PID.

  This mirrors `Embervm.WorkloadCatalog` exactly, and for the same reason: the
  dispatch hot path (Task 11) must read `free_primed_slots` per workload per node
  in O(1) without serializing through a `GenServer.call` to the registry, so it
  hits this `read_concurrency: true` table directly.

  ## fail-closed by construction

  The registry only ever `put/3`s a node whose health is `:healthy` AND whose
  daemon is not `draining`; every degraded state (`:starting`, `:unknown`,
  `:down`, or a draining daemon) `drop/2`s the row. So the table's contents ARE
  the set of dispatchable nodes: a reader that finds no row for a node (or an
  empty table) is looking at the correct, safe answer with no extra
  interpretation. "No capacity facts means no dispatch" is the empty read, not a
  branch a reader has to remember to write.
  """

  @table :embervm_node_capacity

  @doc "The ETS table name every function defaults to when none is given."
  @spec table() :: atom()
  def table, do: @table

  @doc """
  Creates the capacity table. `:set` (one row per node id), `:public` (Task 11's
  dispatcher reads it directly, outside the owning process), `:named_table`
  (looked up by atom name, not PID), `read_concurrency: true` (writes happen only
  on a status change or a health transition; reads happen on every dispatch
  decision).
  """
  @spec create(atom()) :: atom()
  def create(table \\ @table) do
    :ets.new(table, [:set, :public, :named_table, read_concurrency: true])
  end

  @doc """
  Writes one dispatchable node's capacity facts, keyed by the CONFIGURED node id
  (the registry's stable per-node key, used identically by `drop/2` and
  `fetch/2`); the daemon-reported id is carried inside the facts map as
  `:node_id`. Only called by the registry for a node it has already decided is
  dispatchable, so a row's mere presence is the dispatchable signal.
  """
  @spec put(atom(), String.t(), map()) :: true
  def put(table \\ @table, node_id, facts) do
    :ets.insert(table, {node_id, facts})
  end

  @doc "Removes a node's capacity facts (it is no longer dispatchable)."
  @spec drop(atom(), String.t()) :: true
  def drop(table \\ @table, node_id) do
    :ets.delete(table, node_id)
  end

  @doc """
  All dispatchable nodes' capacity facts, in no particular order. Empty when no
  node is currently dispatchable (the fail-closed default). Returns `[]` rather
  than raising when the table does not exist yet (the registry has not booted, or
  is between a crash and its restart), so a reader is never blocked on the
  registry's lifecycle.
  """
  @spec all(atom()) :: [map()]
  def all(table \\ @table) do
    if :ets.whereis(table) == :undefined do
      []
    else
      :ets.select(table, [{{:_, :"$1"}, [], [:"$1"]}])
    end
  end

  @doc """
  One node's capacity facts by node id, or `:error` if that node is not currently
  dispatchable (or the table does not exist yet).
  """
  @spec fetch(atom(), String.t()) :: {:ok, map()} | :error
  def fetch(table \\ @table, node_id) do
    if :ets.whereis(table) == :undefined do
      :error
    else
      case :ets.lookup(table, node_id) do
        [{^node_id, facts}] -> {:ok, facts}
        [] -> :error
      end
    end
  end
end
