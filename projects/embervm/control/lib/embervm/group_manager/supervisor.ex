defmodule Embervm.GroupManager.Supervisor do
  @moduledoc """
  Owns the per-group-instance `Embervm.GroupManager` processes: a `DynamicSupervisor`
  (`Embervm.GroupManager.DynamicSupervisor`) holding one child per LIVE group
  instance, keyed in a `Registry` (`Embervm.GroupRegistry`, unique keys) by WORKLOAD
  (a composite workload is a group-level singleton, so one live process per
  workload). This is the composite counterpart of the SessionManager's
  SessionRegistry + SessionSupervisor per-instance model; the GroupManager owns the
  ordered create/start/bank/relight state machine of ONE group.

  `create_group/2` is the entrypoint Task 7's wake-on-connect activator calls: it
  resolves the composite catalog entry, picks the anchor node, mints the group
  instance id, spawns a GroupManager child (registered under the workload), and
  synchronously drives its ordered create sequence, returning the entry endpoint (or
  an error, with the child having torn the group down to failed). A second concurrent
  create for the same workload finds the registry key taken and refuses
  `{:error, :already_live}` (the singleton at the process level; the GroupStore's own
  singleton gate is the durable backstop).
  """

  use Supervisor

  alias Embervm.{GroupManager, NodeCapacity, WorkloadCatalog}

  @registry Embervm.GroupRegistry
  @dyn_sup Embervm.GroupManager.DynamicSupervisor

  @spec start_link(keyword()) :: Supervisor.on_start()
  def start_link(opts) do
    Supervisor.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @impl true
  def init(opts) do
    # The default seams are stored in an app-env-backed config the create/2 entrypoint
    # reads, so the supervisor itself only owns the registry + dynamic supervisor. The
    # per-group config (store, publisher, capacity/catalog tables, supernet, port_base,
    # pod_ip, node funs) is passed through create/2's opts (tests inject fakes).
    Application.put_env(:embervm, :group_manager_defaults, Keyword.get(opts, :defaults, []))

    children = [
      {Registry, keys: :unique, name: @registry},
      {DynamicSupervisor, strategy: :one_for_one, name: @dyn_sup}
    ]

    Supervisor.init(children, strategy: :one_for_one)
  end

  @doc """
  Creates + runs a group for `workload` synchronously. `opts` overrides the app-env
  defaults (tests inject store/publisher/tables + node funs). Returns
  `{:ok, %{ip, port}}` (the entry endpoint) or `{:error, reason}`. A second live
  create for the same workload is `{:error, :already_live}`.
  """
  @spec create_group(String.t(), keyword()) :: {:ok, map()} | {:error, term()}
  def create_group(workload, opts \\ []) do
    defaults = Application.get_env(:embervm, :group_manager_defaults, [])
    opts = Keyword.merge(defaults, opts)

    catalog_table = Keyword.get(opts, :catalog_table, WorkloadCatalog.table())
    capacity_table = Keyword.get(opts, :capacity_table, NodeCapacity.table())

    with {:ok, entry} <- GroupManager.catalog_group(catalog_table, workload),
         {:ok, node_id} <- anchor_node(capacity_table),
         {:ok, pid} <- start_child(workload, entry, node_id, opts) do
      result = GroupManager.create_group(pid)
      # On a failed create the group is torn down to failed; retire the process so a
      # later retry spawns a fresh one (a live create keeps the process for Task 7/8's
      # bank/relight drive).
      case result do
        {:ok, _endpoint} ->
          result

        {:error, _reason} ->
          _ = DynamicSupervisor.terminate_child(@dyn_sup, pid)
          result
      end
    end
  end

  @doc """
  Wakes the EXISTING banked instance `instance_id` of `workload` synchronously (R5,
  Task 7): spawns (or reuses) the GroupManager child bound to that instance and
  drives its `wake_group/1` (relight, or relight -> fresh fallback in the SAME call).
  Returns `{:ok, %{ip, port}, outcome}` (`outcome` :relit or `{:fresh, reason}`) or
  `{:error, reason}`. A second concurrent wake for the same workload finds the
  registry key taken and reuses the SAME child (single-instance-per-workload at the
  process level; the wake brain single-flights above this, so only one wake worker
  ever calls in per burst). On a wake failure the child is retired so a retry spawns
  fresh; a successful wake keeps the process (a later bank/relight has an owner).
  """
  @spec wake_group(String.t(), String.t(), keyword()) :: {:ok, map(), atom() | tuple()} | {:error, term()}
  def wake_group(workload, instance_id, opts \\ []) do
    defaults = Application.get_env(:embervm, :group_manager_defaults, [])
    opts = Keyword.merge(defaults, opts)

    catalog_table = Keyword.get(opts, :catalog_table, WorkloadCatalog.table())
    capacity_table = Keyword.get(opts, :capacity_table, NodeCapacity.table())

    with {:ok, entry} <- GroupManager.catalog_group(catalog_table, workload),
         {:ok, node_id} <- anchor_node(capacity_table),
         {:ok, pid} <- start_or_get_child(workload, instance_id, entry, node_id, opts) do
      result = GroupManager.wake_group(pid)

      case result do
        {:ok, _endpoint, _outcome} ->
          result

        {:error, _reason} ->
          _ = DynamicSupervisor.terminate_child(@dyn_sup, pid)
          result
      end
    end
  end

  @doc """
  Banks the live group `instance_id` of `workload` as ONE set (R5, Task 8): locate
  (or spawn) the GroupManager child bound to the instance and drive its
  `bank_group/1`. The sweeper calls this once a group is confirmed idle (it already
  unpublished the entry endpoint). Returns `{:ok, %{set_id, banked, pause_spread_ms}}`
  on a clean whole-set bank or `{:error, reason}` on an abort (the group is back
  `running` and the sweeper re-publishes). A group with no live owner process (e.g.
  a CP restart that has not re-adopted it yet) spawns one bound to the instance so
  the bank has a driver. A missing catalog entry / anchor node is `{:error, reason}`.
  """
  @spec bank_group(String.t(), String.t(), keyword()) :: {:ok, map()} | {:error, term()}
  def bank_group(workload, instance_id, opts \\ []) do
    defaults = Application.get_env(:embervm, :group_manager_defaults, [])
    opts = Keyword.merge(defaults, opts)

    catalog_table = Keyword.get(opts, :catalog_table, WorkloadCatalog.table())
    capacity_table = Keyword.get(opts, :capacity_table, NodeCapacity.table())

    with {:ok, entry} <- GroupManager.catalog_group(catalog_table, workload),
         {:ok, node_id} <- anchor_node(capacity_table),
         {:ok, pid} <- start_or_get_child(workload, instance_id, entry, node_id, opts) do
      case GroupManager.bank_group(pid) do
        {:ok, _} = ok ->
          ok

        {:error, _reason} = error ->
          # Terminate the owner on a failed bank, mirroring create_group: a
          # registered child left behind refuses every later wake as
          # :already_live at the DynamicSupervisor layer (the invisible-wedge
          # class), while a fresh wake respawns an owner cleanly.
          _ = DynamicSupervisor.terminate_child(@dyn_sup, pid)
          error
      end
    end
  end

  @doc """
  Adoption (R5, Task 7): (re)spawn the GroupManager child for a live-adopted instance
  so a later bank/relight has an owner, WITHOUT driving any lifecycle. Idempotent: a
  child already registered under the workload is left as-is. Returns `:ok`. Never
  touches a VM (the reconcile already forced the store state from node truth).
  """
  @spec adopt_group(String.t(), String.t(), keyword()) :: :ok
  def adopt_group(workload, instance_id, opts \\ []) do
    defaults = Application.get_env(:embervm, :group_manager_defaults, [])
    opts = Keyword.merge(defaults, opts)

    catalog_table = Keyword.get(opts, :catalog_table, WorkloadCatalog.table())
    capacity_table = Keyword.get(opts, :capacity_table, NodeCapacity.table())

    with {:ok, entry} <- GroupManager.catalog_group(catalog_table, workload),
         {:ok, node_id} <- anchor_node(capacity_table) do
      _ = start_or_get_child(workload, instance_id, entry, node_id, opts)
      :ok
    else
      _ -> :ok
    end
  end

  @doc "The live GroupManager pid for a workload, or nil."
  @spec whereis(String.t()) :: pid() | nil
  def whereis(workload) do
    case Registry.lookup(@registry, workload) do
      [{pid, _}] -> pid
      [] -> nil
    end
  end

  # -- internals -------------------------------------------------------------

  defp start_child(workload, entry, node_id, opts) do
    instance_id = mint_instance_id(opts)
    do_start_child(workload, instance_id, entry, node_id, opts)
  end

  # Start a child bound to a SPECIFIC (already-existing) instance_id, or return the
  # child already registered under the workload (the wake/adopt path: the banked
  # instance already exists, and a registered owner is reused). A registered pid for
  # a DIFFERENT instance_id is still returned (one live process per workload; the
  # registry key is the workload, and the class is a group-level singleton).
  defp start_or_get_child(workload, instance_id, entry, node_id, opts) do
    case do_start_child(workload, instance_id, entry, node_id, opts) do
      {:ok, pid} ->
        {:ok, pid}

      {:error, :already_live} ->
        case whereis(workload) do
          nil -> {:error, :already_live}
          pid -> {:ok, pid}
        end

      other ->
        other
    end
  end

  defp do_start_child(workload, instance_id, entry, node_id, opts) do
    child_opts =
      [
        instance_id: instance_id,
        workload: workload,
        principal: group_principal(workload),
        entry: entry,
        node_id: node_id,
        name: {:via, Registry, {@registry, workload}}
      ] ++ Keyword.take(opts, group_manager_opt_keys())

    spec = %{
      id: {:group, workload},
      start: {GroupManager, :start_link, [child_opts]},
      restart: :temporary,
      type: :worker
    }

    case DynamicSupervisor.start_child(@dyn_sup, spec) do
      {:ok, pid} -> {:ok, pid}
      {:error, {:already_started, _pid}} -> {:error, :already_live}
      {:error, reason} -> {:error, reason}
    end
  end

  # The per-group config keys the GroupManager child accepts (everything the
  # create/2 caller may inject; production reads the app-env defaults).
  defp group_manager_opt_keys do
    [
      :store,
      :publisher,
      :capacity_table,
      :tenant,
      :supernet,
      :port_base,
      :pod_ip,
      :channel_fun,
      :create_group_network_fun,
      :delete_group_network_fun,
      :start_group_member_fun,
      :stop_group_member_fun,
      :evict_snapshot_fun,
      :get_secret_fun,
      :secret_fun,
      :set_id_fun,
      :clock
    ]
  end

  # A composite group's synthesized owner principal (mirrors the stateful
  # system:stateful:<workload> attribution), the op-log owner for its lifecycle ops.
  defp group_principal(workload), do: "system:group:#{workload}"

  # Pick the anchor node for a fresh group: the first node reporting a
  # stateful/serving-capable subnet with live-VM budget (rendezvous-hashed with one
  # node is trivially that node; a real placement is Task 7's concern). No node is
  # {:error, :no_capacity}.
  defp anchor_node(capacity_table) do
    NodeCapacity.all(capacity_table)
    |> Enum.filter(&group_capable?/1)
    |> case do
      [] -> {:error, :no_capacity}
      [fact | _] -> {:ok, fact.configured_id}
    end
  end

  defp group_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  defp mint_instance_id(opts) do
    case Keyword.get(opts, :id_fun) do
      fun when is_function(fun, 0) -> fun.()
      _ -> "grp-" <> String.trim_leading(Embervm.SessionId.new(System.system_time(:millisecond)), "s-")
    end
  end
end
