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

  ## keyed by INSTANCE, not node (R0 PR-2)

  The row key is the `{node_id, pod_uid}` INSTANCE tuple, not the node name.
  Dial-home registration (ADR embervm/005) makes two noded instances on one node
  simultaneously representable (a surge roll, ADR embervm/012: the draining old
  instance and the fresh one), so capacity, liveness, and dispatch are per
  instance. Every facts map still carries `:node_id` (the K8s node name, shared
  across a node's instances) and `:pod_uid`, plus a derived `:instance_id`
  string (`"node/pod_uid"`) the dispatcher/NodeChannel/BaseBuilder key their own
  string-keyed maps on. Node-scoped facts (vendor/template, serving subnet,
  snapshots, volumes) survive an instance turnover because they are re-reported
  by whichever instance owns the node's substrate.
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
  Writes one dispatchable INSTANCE's capacity facts, keyed by the
  `{node_id, pod_uid}` tuple (the registry's stable per-instance key, used
  identically by `drop/2` and `fetch/2`); the node name and pod UID are also
  carried inside the facts map (`:node_id`, `:pod_uid`, `:instance_id`). Only
  called by the registry for an instance it has already decided is dispatchable,
  so a row's mere presence is the dispatchable signal.
  """
  @spec put(atom(), {String.t(), String.t()}, map()) :: true
  def put(table \\ @table, instance_key, facts) do
    :ets.insert(table, {instance_key, facts})
  end

  @doc "Removes an instance's capacity facts (it is no longer dispatchable)."
  @spec drop(atom(), {String.t(), String.t()}) :: true
  def drop(table \\ @table, instance_key) do
    :ets.delete(table, instance_key)
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
  Capacity facts for a key that is EITHER an instance tuple `{node_id, pod_uid}`
  (exact instance lookup) OR a bare `node_id` string (NODE-scoped lookup, returns
  an instance ON that node, preferring the most recently updated). `:error` when
  nothing dispatchable matches (or the table does not exist yet).

  The bare-string form is what the node-scoped consumers (session/serving/stateful/
  group placement + adoption) call: snapshots and volumes are NODE resources, not
  pod resources (ADR embervm/005 R0 PR-2 step 4), so those readers ask "is THIS
  node dispatchable" regardless of which instance currently owns it. The tuple form
  is the dispatcher/registry's exact per-instance read.
  """
  @spec fetch(atom(), {String.t(), String.t()} | String.t()) :: {:ok, map()} | :error
  def fetch(table \\ @table, key)

  def fetch(table, {_node, _pod_uid} = instance_key) do
    if :ets.whereis(table) == :undefined do
      :error
    else
      case :ets.lookup(table, instance_key) do
        [{^instance_key, facts}] -> {:ok, facts}
        [] -> :error
      end
    end
  end

  def fetch(table, node_id) when is_binary(node_id) do
    if :ets.whereis(table) == :undefined do
      :error
    else
      table
      |> :ets.select([{{:_, :"$1"}, [], [:"$1"]}])
      |> Enum.filter(fn facts -> Map.get(facts, :node_id) == node_id end)
      |> case do
        [] -> :error
        matches -> {:ok, Enum.max_by(matches, fn f -> Map.get(f, :updated_at, 0) end)}
      end
    end
  end

  @doc """
  The CPUID vendor ("amd"/"intel") the anchor `key` currently reports, for stamping
  `RestoreArtifactRequest.artifact.vendor` on a restore-on-miss (R7, ADR
  embervm/011). `key` is whatever the caller anchors on (a bare node-name string or
  an instance tuple), resolved through `fetch/2`; the vendor is a NODE-scoped fact
  shared across a node's instances. Returns `""` when the node is not dispatchable
  (no facts) or its daemon reports no vendor (pre-R7): noded's resolveRestorePrefix
  maps an empty vendor to the node-4 legacy alias, so an empty vendor still restores
  the legacy un-vendored prefix rather than failing closed. Never raises.
  """
  @spec vendor_for(atom(), {String.t(), String.t()} | String.t()) :: String.t()
  def vendor_for(table \\ @table, key) do
    case fetch(table, key) do
      {:ok, facts} -> Map.get(facts, :cpu_vendor, "") || ""
      :error -> ""
    end
  end
end
