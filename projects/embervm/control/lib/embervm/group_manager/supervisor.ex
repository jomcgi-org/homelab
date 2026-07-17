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
      :get_secret_fun,
      :secret_fun,
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
