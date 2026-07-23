defmodule Embervm.GroupManager do
  @moduledoc """
  The composite-group control-plane brain: ONE supervised process per LIVE group
  INSTANCE, owning that group's ordered create/start/bank/relight state machine.
  This is the composite generalization of `Embervm.StatefulManager`, but where the
  stateful manager is a SINGLE process single-flighting per-workload wakes, a group
  is a SET of member VMs whose ordered lifecycle is heavy enough (role-ordered,
  health-gated member starts across a private subnet) to own its own process. A
  `DynamicSupervisor` (`Embervm.GroupManager.Supervisor`) + `Registry`
  (`Embervm.GroupRegistry`, keyed by workload) spawn one GroupManager child per live
  group; `Embervm.GroupManager.Supervisor.create_group/2` is the entrypoint Task 7's
  activator calls on a wake-on-connect miss.

  ## why per-instance, not one process

  `StatefulManager` is one process because a stateful workload is a single VM with a
  trivially-ordered lifecycle; single-flighting a per-workload wake there is cheap.
  A composite group's create sequences N member starts across R role orders, each
  health-gated, and drives per-member bank/relight loops. Doing that on one shared
  process would head-of-line-block every other group's create behind one slow
  member boot. So the group lifecycle lives on a per-instance process (the
  `Embervm.Session` per-session precedent), and the supervisor/registry own
  discovery + single-instance-per-workload.

  ## the ordered create sequence (atomic: any member-start failure => failed)

  `create_group/1` on the per-instance process:

    1. records the group instance (`GroupStore.create/2`, the singleton gate);
    2. assigns the group's private /24 (lowest-free, `GroupStore.held_subnets/1`)
       and CreateGroupNetwork's it on the anchor node (re-issued idempotently before
       any relight, since the bridge dies with the noded pod);
    3. composes the deterministic member address plan (`.10 + i` over the flattened,
       declaration-ordered, replica-expanded member list, MATCHING the watcher's
       `expanded_member_names/1` AND noded's `groupMemberIP`);
    4. starts members in ROLE ORDER: all members of `startOrder` 0 in PARALLEL,
       health-gated (StartGroupMember returns only after noded health-gates
       {ip, health_port}), THEN order 1, and so on;
    5. publishes the entry endpoint (the entry member's DNAT `{pod IP, vmPort}`) and
       moves the group to `running`.

  A member start FAILURE during create tears the WHOLE group down to `failed` and
  deletes the group network (create is ATOMIC; degradation only applies to
  already-running groups, decision 11). There is no half-created group.

  ## EMBER_GROUP_* env (decision 13, FRESH-only)

  Each expanded member's FRESH boot env carries, on top of its declared `env`:

    * `EMBER_GROUP_MEMBER` = the expanded member name (`agent-0`);
    * `EMBER_GROUP_ROLE`   = the member's role;
    * `EMBER_GROUP_IP`     = the member's pinned group-subnet IP;
    * `EMBER_PEER_<NAME>`  = each peer's IP, `<NAME>` = the expanded peer name
      UPPERCASED with `-` -> `_` (so `agent-0` -> `EMBER_PEER_AGENT_0`);
    * `EMBER_GROUP_SECRET` = the group secret (from `secretRef`, or minted at
      create).

  The env is FRESH-only: a RELIGHT resumes each member's memory snapshot, so the
  kernel never re-reads boot-args and the birth env is kept. The peer map is built
  from the SAME deterministic address plan used to pin the taps, so a member's view
  of its peers is exactly the addresses noded configured.

  ## the entry endpoint (CP re-derives the DNAT port, Task 4 lockstep)

  noded exposes a group's entry member as `podIP:vmPort` where
  `vmPort = ServingPortBase + hostOffset(entryIP, compositeSupernet)` (the D-R3.11.4
  lane, `noded/serving/net.go` `PortForIP` + `hostOffset`, installed by
  `noded/serving/group.go` `EnsureEntryDNAT`). The daemon does NOT report that port
  back (StartGroupMemberResponse echoes only `{vm_id, ip, was_relight}`), so the
  control plane RE-DERIVES it here from the SAME shared `EMBERVM_COMPOSITE_PORT_BASE`
  + `EMBERVM_COMPOSITE_SUPERNET` values that feed noded's `ServingPortBase` +
  `CompositeSupernet`. This MUST stay in lockstep with noded's port derivation
  (`noded/serving/net.go:PortForIP`); a change to either side breaks the entry
  endpoint. The derived `{pod IP, vmPort}` is recorded on the GroupStore instance
  (`entry_ip`/`entry_port_published`), so if Task 7 later has noded REPORT the entry
  endpoint via NodeStatus, it just overwrites the same fact with no migration.
  """

  use GenServer
  require Logger
  import Bitwise

  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{GroupState, GroupStore, NodeCapacity, WorkloadCatalog}

  alias Embervm.Node.V1.{
    CreateGroupNetworkRequest,
    CreateGroupNetworkResponse,
    DeleteGroupNetworkRequest,
    EvictSnapshotRequest,
    StartGroupMemberRequest,
    StartGroupMemberResponse,
    StopGroupMemberRequest,
    StopGroupMemberResponse,
    ResourceSpec,
    Trace
  }

  # The host offset of member index 0 within a group /24 (MATCHES noded's
  # groupMemberBaseOffset in serving/group.go: member i is .10 + i; .1 is the
  # gateway, .2..9 are reserved headroom). Kept in lockstep with the daemon.
  @group_member_base_offset 10

  # -- Client API (per-instance process) -------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Drives the full ordered create sequence synchronously and returns
  `{:ok, %{ip, port}}` (the entry endpoint the caller/activator resolves the parked
  connection to) or `{:error, reason}` (the group is torn down to `failed` on any
  member-start failure). Blocks until the group is running or failed.
  """
  @spec create_group(GenServer.server()) :: {:ok, map()} | {:error, term()}
  def create_group(server) do
    GenServer.call(server, :create_group, :infinity)
  end

  @doc """
  Wakes a BANKED group instance in one single-flighted call (R5, Task 7): the
  relight-or-fresh sequence off `banked`. Re-issues CreateGroupNetwork (the bridge
  dies with the noded pod), then RESUMES every member in role order (RELIGHT
  StartGroupMember from its banked `snapshot_ref`). If ANY member relight fails
  (including the clock-resync FAILED_PRECONDITION noded returns as a member start
  failure, decision 7), it ABORTS the relight, evicts the whole set (durable
  `group_set_evicted`), and falls back to a full FRESH boot INSIDE THE SAME CALL
  (role-ordered fresh member starts on the same subnet), so the parked connection
  is held across the fallback (the caller blocks on this one `:infinity` call the
  whole time). Publishes the entry endpoint and moves the group to `running`.

  Returns `{:ok, %{ip, port}, outcome}` where `outcome` is `:relit` (a clean whole-
  set relight) or `{:fresh, reason}` (the relight aborted and a fresh boot recovered,
  `reason` one of `:partial_set | :set_unreadable | :relight_failed | ...`), or
  `{:error, reason}` when even the fresh fallback failed (the group is torn to
  `failed`). Blocks until the group is running or failed. The instance MUST already
  exist in the store (the wake brain created/located it); this never creates the
  singleton row (that is `create_group/1`).
  """
  @spec wake_group(GenServer.server()) :: {:ok, map(), atom() | tuple()} | {:error, term()}
  def wake_group(server) do
    GenServer.call(server, :wake_group, :infinity)
  end

  @doc """
  Banks the whole group as ONE set (R5, Task 8), the bank half of the economics
  loop the `Embervm.GroupSweeper` drives once a group is confirmed idle. The
  instance MUST already be in the transient `banking` state: the SWEEPER moves it
  `running -> banking` (which drops the entry endpoint from the fan-out) and installs
  the activator BEFORE calling, so a racing connection parks on the activator, not a
  VM about to be paused (standing decision 7, mirroring StatefulSweeper's
  unpublish-then-bank). The sequence, decision 3 (all-or-nothing) + decision 10 (an
  honest pause-spread number):

    1. mint ONE `set_id` shared across the members, and `StopGroupMember(BANK)` every
       LIVE member under `group/<set_id>/<member>/` (each BANK pauses, snapshots, AND
       destroys that member's VM per the proto), collecting `{member, snapshot_ref}`;
    2. if EVERY member banked, record the whole set ATOMICALLY
       (`GroupStore.bank_ready/4` -> one `group_banked` append, `banking -> banked`,
       stamping `set_id` + each member's `snapshot_ref`, and dropping the entry
       endpoint), then re-publish so the activator install is current;
    3. if ANY member's BANK failed, ABORT (`banking -> running` via `bank_abort`,
       ETS-only): the partial snapshots are orphans the daemon's own bundle GC
       reclaims, and the group returns to `running`. The sweeper re-publishes so the
       still-live group re-enters the fan-out (a failed bank must not leave a warm
       group dark).

  A crash mid-bank strands the instance in `banking`; adoption heals it (it finds no
  live members and no complete set and resolves the instance fresh-bootable).

  Returns `{:ok, %{set_id, banked: n, pause_spread_ms: ms}}` on a clean whole-set
  bank (the closure gate reads `pause_spread_ms`, decision 10), or
  `{:error, reason}` on an abort (the group is back `running`). Blocks until the
  bank completes or aborts.
  """
  @spec bank_group(GenServer.server()) :: {:ok, map()} | {:error, term()}
  def bank_group(server) do
    GenServer.call(server, :bank_group, :infinity)
  end

  @doc "The group instance's current entry endpoint (or nil), for tests + straggler resolution."
  @spec entry_endpoint(GenServer.server()) :: map() | nil
  def entry_endpoint(server) do
    GenServer.call(server, :entry_endpoint)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      instance_id: Keyword.fetch!(opts, :instance_id),
      workload: Keyword.fetch!(opts, :workload),
      principal: Keyword.fetch!(opts, :principal),
      entry: Keyword.fetch!(opts, :entry),
      node_id: Keyword.fetch!(opts, :node_id),
      # The channel dial KEY (brick co-location foundation, Step 4): the specific
      # instance on `node_id` this group's member RPCs dial, as an instance_id
      # ("node/pod_uid") the dual-keyed Embervm.NodeChannel resolves without the
      # node-name-alias collapse. Distinct from `node_id`, which stays the K8s NODE
      # NAME recorded durably (GroupStore rows, adoption's node lookups). Defaults to
      # `node_id` so a caller that does not select an instance (or a single-instance
      # fleet) dials exactly as before.
      dial_id: Keyword.get(opts, :dial_id) || Keyword.fetch!(opts, :node_id),
      store: Keyword.get(opts, :store, GroupStore),
      publisher: Keyword.get(opts, :publisher, Embervm.EndpointPublisher),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      supernet: Keyword.fetch!(opts, :supernet),
      port_base: Keyword.fetch!(opts, :port_base),
      pod_ip: Keyword.get(opts, :pod_ip),
      # Daemon group-verb seams (injected for tests; production dials the real
      # NodeService stub over the shared NodeChannel).
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      # Drop the shared node channel when a group verb reveals its transport is dead
      # (a noded rollout), so the next safe_channel/2 re-dials instead of every member
      # start failing on a dead ConnectionProcess until the control plane restarts
      # (D-R2.7.2). Injected for tests.
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      create_group_network_fun:
        Keyword.get(opts, :create_group_network_fun, &default_create_group_network/2),
      delete_group_network_fun:
        Keyword.get(opts, :delete_group_network_fun, &default_delete_group_network/2),
      start_group_member_fun:
        Keyword.get(opts, :start_group_member_fun, &default_start_group_member/2),
      stop_group_member_fun:
        Keyword.get(opts, :stop_group_member_fun, &default_stop_group_member/2),
      evict_snapshot_fun:
        Keyword.get(opts, :evict_snapshot_fun, &default_evict_snapshot/2),
      get_secret_fun: Keyword.get(opts, :get_secret_fun, &Embervm.K8s.get_secret/2),
      secret_fun: Keyword.get(opts, :secret_fun, &mint_secret/0),
      # ADR embervm/014 decision 5: node-confirmed destroy config plumbing.
      node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
      destroying_alarm_ms: Keyword.get(opts, :destroying_alarm_ms, 300_000),
      orphan_grace_ms: Keyword.get(opts, :orphan_grace_ms, 60_000),
      # The bank set_id minter (injected in tests to pin the opaque set directory);
      # production mints instance_id + bank-start clock (see mint_set_id/2).
      set_id_fun: Keyword.get(opts, :set_id_fun),
      clock: Keyword.get(opts, :clock, &default_clock/0)
    }

    {:ok, state}
  end

  @impl true
  def handle_call(:create_group, _from, state) do
    {reply, state} = do_create_group(state)
    {:reply, reply, state}
  end

  def handle_call(:wake_group, _from, state) do
    {reply, state} = do_wake_group(state)
    {:reply, reply, state}
  end

  def handle_call(:bank_group, _from, state) do
    {reply, state} = do_bank_group(state)
    {:reply, reply, state}
  end

  def handle_call(:entry_endpoint, _from, state) do
    endpoint =
      case GroupStore.get(state.store, state.instance_id) do
        {:ok, %{state: :running, entry_ip: ip, entry_port_published: port}}
        when is_binary(ip) and is_integer(port) ->
          %{ip: ip, port: port}

        _ ->
          nil
      end

    {:reply, endpoint, state}
  end

  # -- create sequence -------------------------------------------------------

  defp do_create_group(state) do
    entry_cfg = state.entry
    group = Map.fetch!(entry_cfg, :group)

    # 1. The deterministic member address plan (.10 + i over the flattened,
    #    declaration-ordered, replica-expanded member list). Built BEFORE the network
    #    so the CIDR + plan are consistent, and used for both the tap pin ip and the
    #    peer-map env.
    subnet_cidr = allocate_subnet(state)

    plan = member_plan(group, subnet_cidr)

    # 2. Record the group instance (the singleton gate) with its subnet + entry
    #    identity + the sourced/minted secret.
    secret = resolve_secret(state, entry_cfg, group)

    create_attrs = %{
      instance_id: state.instance_id,
      tenant: state.tenant,
      principal: state.principal,
      workload: state.workload,
      node_id: state.node_id,
      subnet_cidr: subnet_cidr,
      entry_member: group.entry.member,
      entry_port: group.entry.port,
      listen_port: group.entry.listen_port,
      secret: secret
    }

    # The singleton gate FIRST, on its own: a refusal (another live instance already
    # holds this workload's group + its subnet) must NOT trigger the teardown path,
    # which would tear down the OTHER instance's network. Only a failure AFTER the
    # instance is recorded is torn down to failed.
    case GroupStore.create(state.store, create_attrs) do
      {:ok, _instance} ->
        # The `create` root span (Task 9): a ROOT span around the whole ordered
        # create sequence (network + role-ordered member starts + publish). A raw TCP
        # accept carries no caller trace to nest under, so this is a root, the same
        # shape as the stateful wake/forced_roll roots. Per-member CHILD spans nest
        # under it via the captured OTel ctx attached inside each spawned Task (see
        # start_order_parallel), so member starts appear as children in the waterfall.
        Tracer.with_span "embervm.group.create",
                         %{attributes: %{"ember.workload" => state.workload, "ember.instance_id" => state.instance_id, "ember.members" => length(plan)}} do
          do_create_sequence(state, subnet_cidr, plan, secret, group)
        end

      {:error, reason} ->
        {{:error, reason}, state}
    end
  end

  defp do_create_sequence(state, subnet_cidr, plan, secret, group) do
    with {:ok, _net} <- create_network(state, subnet_cidr),
         :ok <- GroupStore.net_created(state.store, state.instance_id, subnet_cidr) |> ok_or(),
         # Role-ordered, health-gated member starts. Any failure short-circuits to the
         # else and is torn down to failed (create is ATOMIC, decision 11).
         :ok <- start_members_ordered(state, plan, secret, subnet_cidr) do
      # Publish the entry endpoint (the entry member's DNAT projection) and move the
      # group to running.
      finish_create(state, plan, group)
    else
      {:error, reason} ->
        teardown_failed(state, reason)
        {{:error, reason}, state}
    end
  rescue
    e ->
      teardown_failed(state, {:create_crashed, e})
      {{:error, {:create_crashed, e}}, state}
  end

  # Role-ordered member starts: group members by their startOrder ordinal, ascending;
  # start all members of one order in PARALLEL, health-gate every one (the
  # StartGroupMember RPC returns only after noded health-gates {ip, health_port}),
  # and only proceed to the next order once every member of the current order
  # succeeded. Order N never starts before all order N-1 members are healthy.
  defp start_members_ordered(state, plan, secret, subnet_cidr) do
    # Capture the current OTel ctx HERE (on the manager process, inside the `create`
    # root span) so each spawned member Task can attach it and nest its per-member
    # child span under the root across the process boundary (Task.async does NOT
    # propagate OTel context automatically; a child span opened without this attach is
    # a silent orphan). Mirrors Dispatcher's otel_ctx capture across spawn_monitor.
    otel_ctx = OpenTelemetry.Ctx.get_current()

    orders =
      plan
      |> Enum.group_by(& &1.start_order)
      |> Enum.sort_by(fn {order, _} -> order end)

    Enum.reduce_while(orders, :ok, fn {_order, members}, :ok ->
      case start_order_parallel(state, members, plan, secret, subnet_cidr, otel_ctx) do
        :ok -> {:cont, :ok}
        {:error, _reason} = error -> {:halt, error}
      end
    end)
  end

  # Start every member of one startOrder concurrently and await all. A single
  # failure fails the whole order (and, via the caller, the whole create). Each task
  # body is wrapped so a raised/exited member start becomes an {:error, _} result
  # rather than a task exit that would take the GroupManager process down mid-create
  # (the create is atomic: a failure must reach teardown_failed, not crash the owner).
  defp start_order_parallel(state, members, plan, secret, subnet_cidr, otel_ctx) do
    members
    |> Enum.map(fn member ->
      Task.async(fn ->
        # Attach the captured ctx so this member's child span nests under `create`.
        _ = OpenTelemetry.Ctx.attach(otel_ctx)

        try do
          start_one_member(state, member, plan, secret, subnet_cidr)
        rescue
          e -> {:error, {:member_start_crashed, member.expanded_name, e}}
        catch
          kind, reason -> {:error, {:member_start_crashed, member.expanded_name, {kind, reason}}}
        end
      end)
    end)
    |> Enum.map(&Task.await(&1, :infinity))
    |> Enum.find(:ok, &match?({:error, _}, &1))
  end

  # A per-member CHILD span (Task 9): `ember.member` (the expanded member name),
  # `ember.was_relight` (false for a FRESH start), and `ember.clock_delta_ms` (the
  # clock-resync delta). A FRESH member start does no clock resync, and the daemon's
  # StartGroupMemberResponse echoes no clock delta (only {vm_id, ip, was_relight}), so
  # clock_delta_ms is the -1 "not reported" sentinel here (a concrete integer, never
  # nil, per the OTel-needs-a-typed-value rule). The clock-resync signal lives on the
  # RELIGHT path (resume_one_member), where `ember.was_relight=false` on a relight IS
  # the clock-resync-failed derivation the Task 11 gate reads.
  defp start_one_member(state, member, plan, secret, subnet_cidr) do
    Tracer.with_span "embervm.group.member_start",
                     %{attributes: member_span_attrs(member.expanded_name, false, -1)} do
      do_start_one_member(state, member, plan, secret, subnet_cidr)
    end
  end

  defp do_start_one_member(state, member, plan, secret, subnet_cidr) do
    req = %StartGroupMemberRequest{
      trace: %Trace{workload: state.workload},
      mode: :START_GROUP_MEMBER_MODE_FRESH,
      group_instance_id: state.instance_id,
      member_name: member.expanded_name,
      member_index: member.index,
      ip: member.ip,
      source: member.image_ref || "",
      snapshot_ref: "",
      health_port: member.health_port || 0,
      resources: %ResourceSpec{vcpus: member.vcpus || 1, mem_mib: member.mem_mib || 512},
      env: member_env(member, plan, secret, state.workload, subnet_cidr),
      # Mark the ENTRY member so noded installs the entry DNAT that makes the
      # {pod IP, vmPort} endpoint we publish actually reach tap:entry_port. Only the
      # member named by the workload's entry carries a non-zero port; every other
      # member gets 0 (no DNAT). Without this the published entry is a dead port
      # (the entry-EOF): noded never wired the DNAT the endpoint assumes.
      entry_guest_port: entry_guest_port_for(state, member),
      # Forward the workload's wake budget as the daemon's per-member readiness
      # budget, so both ends enforce ONE policy. Without it noded reaps a member at
      # its own (shorter) default while this create is still inside the wake bound:
      # the k3s agents died exactly this way, silently, at noded's 60s.
      ready_budget_seconds: wake_budget_seconds(state)
    }

    # Node-anchored: a group member boots onto its group's per-instance bridge/tap
    # on ONE node (the whole set shares one group network), so there is no alternative
    # brick and the reject/retry frontier is a SINGLE element by construction. Routing
    # through Embervm.Placement.Retry keeps the reject/retry POLICY one piece of code
    # across every boot path; with one candidate it is exactly one attempt. Do NOT
    # expand this list: a different brick does not host this group's network.
    attempt_fun = fn _only ->
      case safe_start_group_member(state, req) do
        {:ok, %StartGroupMemberResponse{vm_id: vm_id, ip: ip} = resp} when is_binary(vm_id) and vm_id != "" ->
          case GroupStore.member_started(state.store, state.instance_id, %{
                 member_name: member.expanded_name,
                 member_index: member.index,
                 vm_id: vm_id,
                 ip: ip,
                 # The daemon's own entry-endpoint projection ({noded pod IP, vmPort}),
                 # non-empty only for the entry member. Recorded so finish_create
                 # publishes the address the DNAT actually lives at (the daemon pod),
                 # not this control plane's own pod IP.
                 endpoint_ip: resp.endpoint_ip || "",
                 endpoint_port: resp.endpoint_port || 0
               }) do
            {:ok, _} -> {:ok, :ok}
            {:error, reason} -> {:error, {:member_record_failed, member.expanded_name, reason}}
          end

        {:error, %GRPC.RPCError{status: 8}} = rejected ->
          {:reject, rejected}

        other ->
          {:error, {:member_start_failed, member.expanded_name, other}}
      end
    end

    case Embervm.Placement.Retry.run([%{instance_id: state.instance_id}], attempt_fun) do
      {:ok, outcome} -> outcome
      {:error, :no_capacity} -> {:error, {:member_start_failed, member.expanded_name, {:error, :no_capacity}}}
      {:error, reason} -> {:error, reason}
    end
  end

  # The workload's wakeTimeoutSeconds from the catalog entry this manager was
  # spawned with, forwarded to noded as the per-member readiness budget. 0 (keep
  # the daemon default) when the entry carries none.
  defp wake_budget_seconds(state) do
    case state.entry do
      %{group: %{wake_timeout_seconds: secs}} when is_integer(secs) and secs > 0 -> secs
      _ -> 0
    end
  end

  # entry_guest_port_for returns the workload entry's guest port when THIS member is
  # the entry member (matched by expanded_name, exactly as finish_create resolves the
  # entry from the plan), else 0. A non-zero value tells noded to install the entry
  # DNAT for this member so the {pod IP, vmPort} endpoint we publish reaches its tap;
  # 0 means a non-entry member (no DNAT). Kept in lockstep with finish_create's entry
  # resolution so the member noded DNATs is the same member whose IP we derive vmPort
  # from.
  defp entry_guest_port_for(state, member) do
    entry = with %{group: group} <- state.entry, do: Map.get(group, :entry)
    name = member.expanded_name

    if is_map(entry) and name == Map.get(entry, :member) do
      Map.get(entry, :port) || 0
    else
      0
    end
  end

  # Publish the entry endpoint + move to running. The published endpoint is the
  # DAEMON's own projection {noded pod IP, vmPort}, reported by noded in the entry
  # member's StartGroupMemberResponse and recorded on its member row: the entry
  # DNAT lives in the noded pod's netns, so that pod's IP is the only address the
  # endpoint is reachable at. The CP-local derivation ({state.pod_ip, re-derived
  # vmPort}, the pre-R6 lane) remains ONLY as a fallback for a daemon that reports
  # no endpoint (older noded, or DNAT disabled); publishing the CP's own pod IP was
  # the F-bug that made every published group entry unroutable.
  defp finish_create(state, plan, group) do
    entry_member = Enum.find(plan, &(&1.expanded_name == group.entry.member))

    case entry_member do
      nil ->
        teardown_failed(state, {:entry_member_not_in_plan, group.entry.member})
        {{:error, {:entry_member_not_in_plan, group.entry.member}}, state}

      %{ip: entry_ip} ->
        case entry_publish_endpoint(state, group, entry_ip) do
          {:ok, ep_ip, ep_port} ->
            case GroupStore.publish(state.store, state.instance_id, ep_ip, ep_port) do
              {:ok, _} ->
                Embervm.EndpointPublisher.publish(state.publisher)
                Logger.info("embervm group running", workload: state.workload, instance_id: state.instance_id)
                {{:ok, %{ip: ep_ip, port: ep_port}}, state}

              {:error, reason} ->
                teardown_failed(state, {:publish_failed, reason})
                {{:error, {:publish_failed, reason}}, state}
            end

          {:error, reason} ->
            teardown_failed(state, {:entry_port_derivation, reason})
            {{:error, {:entry_port_derivation, reason}}, state}
        end
    end
  end

  # The endpoint to publish for the entry member: the daemon-reported projection
  # recorded on the entry member's row when present, else the CP-local derivation
  # (fallback for a daemon that reported none).
  defp entry_publish_endpoint(state, group, entry_ip) do
    daemon_reported =
      GroupStore.members(state.store, state.instance_id)
      |> Enum.find_value(fn m ->
        if m.member_name == group.entry.member and
             is_binary(Map.get(m, :endpoint_ip)) and Map.get(m, :endpoint_ip) != "" and
             is_integer(Map.get(m, :endpoint_port)) and Map.get(m, :endpoint_port) > 0 do
          {Map.get(m, :endpoint_ip), Map.get(m, :endpoint_port)}
        end
      end)

    case daemon_reported do
      {ip, port} ->
        {:ok, ip, port}

      nil ->
        case entry_vm_port(state, entry_ip) do
          {:ok, vm_port} -> {:ok, state.pod_ip || entry_ip, vm_port}
          {:error, reason} -> {:error, reason}
        end
    end
  end

  # Tear a partially-created group down to failed: fail the instance (durable), then
  # best-effort DeleteGroupNetwork (the bridge must not leak). Create is atomic, so
  # this leaves no half-group behind.
  defp teardown_failed(state, reason) do
    _ =
      GroupStore.transition(
        state.store,
        state.instance_id,
        :fail,
        :group_failed,
        %{reason: failure_reason_string(reason)},
        %{}
      )

    _ = delete_network(state)
    Embervm.EndpointPublisher.publish(state.publisher)
    Logger.warning("embervm group create failed, torn down", workload: state.workload, reason: inspect(reason))
    :ok
  end

  # -- wake sequence (relight-or-fresh off banked, R5 Task 7) ----------------

  # The single-flighted wake of a BANKED instance: re-issue the group network,
  # RELIGHT every member in role order, publish. Any member relight failure aborts
  # the relight, evicts the set, and falls back to a FRESH boot in the SAME call
  # (the parked caller is held across the fallback). The instance already exists in
  # the store (the wake brain located/created the banked row); this drives its
  # banked -> relighting -> creating -> running (or banked -> fresh_booting ->
  # creating -> running) sequence.
  defp do_wake_group(state) do
    with {:ok, instance} <- fetch_instance(state),
         :banked <- instance.state do
      entry_cfg = state.entry
      group = Map.fetch!(entry_cfg, :group)
      subnet_cidr = instance.subnet_cidr
      secret = instance_secret(state, entry_cfg, group, instance)

      # The relight plan is anchored to the STORED member rows (their pinned ips +
      # banked snapshot_refs), enriched with the catalog member config (role,
      # start_order, health_port, resources, image_ref) so the role ordering + the
      # fresh-fallback env compose exactly match a create.
      plan = wake_plan(state, group, subnet_cidr, instance)

      attempt_relight(state, instance, subnet_cidr, secret, group, plan)
    else
      {:error, _reason} = error ->
        {error, state}

      other_state when is_atom(other_state) ->
        # Not banked (a race: adopted live, destroyed, or already relighting). The
        # wake brain re-reads the store and resolves the straggler; return the live
        # endpoint if running, else an error the caller retries.
        {wake_race_reply(state), state}
    end
  end

  defp attempt_relight(state, instance, subnet_cidr, secret, group, plan) do
    # The `relight` root span (Task 9): a ROOT span around the whole-set resume
    # sequence (re-network + role-ordered RELIGHT member starts + publish). Per-member
    # `member_relight` child spans nest under it via the captured ctx. A fresh fallback
    # opens its OWN `fresh_boot` root span (fallback_fresh), so a relight that fell
    # back reads as two sibling roots in the trace, honestly showing the discarded
    # relight then the fresh recovery.
    Tracer.with_span "embervm.group.relight",
                     %{attributes: %{"ember.workload" => state.workload, "ember.instance_id" => instance.instance_id, "ember.members" => length(plan)}} do
      do_attempt_relight(state, instance, subnet_cidr, secret, group, plan)
    end
  end

  defp do_attempt_relight(state, instance, subnet_cidr, secret, group, plan) do
    # banked -> relighting (ETS-only mark; a crash mid-relight heals from adoption).
    case GroupStore.mark(state.store, instance.instance_id, :relight) do
      {:ok, _} ->
        with {:ok, _net} <- create_network(state, subnet_cidr),
             :ok <- resume_members_ordered(state, plan) do
          # relighting -> creating (durable group_relit), then publish -> running.
          case GroupStore.transition(state.store, instance.instance_id, :relight_ready, :group_relit, %{}, %{}) do
            {:ok, _} ->
              case finish_wake_publish(state, plan, group) do
                {{:ok, endpoint}, state} -> {{:ok, endpoint, :relit}, state}
                {{:error, reason}, state} -> {{:error, reason}, state}
              end

            {:error, reason} ->
              # The durable relit edge failed: abort back to banked and fall back to
              # fresh (the set is intact, but we cannot record the relight).
              _ = GroupStore.mark(state.store, instance.instance_id, :relight_abort)
              fallback_fresh(state, instance, subnet_cidr, secret, group, plan, {:relit_record_failed, reason})
          end
        else
          {:error, reason} ->
            # A member relight failed (start error, or the clock-resync
            # FAILED_PRECONDITION, decision 7): abort back to banked, then evict the
            # whole set and fresh-boot in the same call. relight_abort is ETS-only.
            _ = GroupStore.mark(state.store, instance.instance_id, :relight_abort)
            fallback_fresh(state, instance, subnet_cidr, secret, group, plan, {:relight_failed, reason})
        end

      {:error, reason} ->
        {{:error, {:relight_mark, reason}}, state}
    end
  end

  # The all-or-nothing fallback: DESTROY every already-resumed live member (a partial
  # relight left member A's VM running, holding its pinned tap+IP in noded's group
  # allocator; a fresh boot of the WHOLE plan would then collide on that IP via
  # EnsureMemberTap's alloc.reserve, the exact case this fallback exists to handle),
  # evict the banked set (durable group_set_evicted, per-ref EvictSnapshot best-
  # effort), transition banked -> fresh_booting -> creating, then run the SAME role-
  # ordered fresh member starts a create does, re-issuing the network (idempotent
  # daemon-side; the taps/IPs it hangs off are now freed by the DESTROY drain). The
  # parked caller is still blocked on the one wake_group call, so this holds the
  # connection across the fallback. group_fresh_booted{reason} records the discarded
  # warmth.
  defp fallback_fresh(state, instance, subnet_cidr, secret, group, plan, reason) do
    # The `fresh_boot` root span (Task 9): a ROOT span around the relight-fallback
    # fresh sequence (destroy stragglers + evict set + role-ordered FRESH member
    # starts + publish). `ember.reason` carries the discarded-warmth reason
    # (clock_resync_failed | partial_set | relight_failed | ...), the honest signal
    # the fresh_boot{...} alerts key on. Per-member `member_start` child spans nest
    # under it. A sibling of the `relight` root, so the trace shows both the discarded
    # relight and the fresh recovery.
    Tracer.with_span "embervm.group.fresh_boot",
                     %{attributes: %{"ember.workload" => state.workload, "ember.instance_id" => instance.instance_id, "ember.reason" => fresh_reason_string(reason)}} do
      do_fallback_fresh(state, instance, subnet_cidr, secret, group, plan, reason)
    end
  end

  defp do_fallback_fresh(state, instance, subnet_cidr, secret, group, plan, reason) do
    fresh_reason = fresh_reason_string(reason)

    # Loud and COMPLETE on purpose: this is warmth being thrown away, and the
    # flattened fresh_reason drops the inner member error (which member, which
    # RPC error / clock delta). The R6 Gate-5 debugging started from a log that
    # said only "relight_failed" with the actual cause visible nowhere.
    Logger.warning("embervm group relight discarded, falling back to fresh boot",
      workload: state.workload,
      instance_id: instance.instance_id,
      reason: inspect(reason, limit: 50, printable_limit: 2048)
    )
    # Free tap+IP for any member that DID resume before the relight aborted, so the
    # fresh re-pin does not collide on an occupied tap. Drains from the instance's
    # stored live member rows (member_started recorded each resumed member).
    case destroy_live_members(state) do
      false ->
        Logger.warning("embervm group relight cleanup unconfirmed, fresh boot deferred",
          workload: state.workload,
          instance_id: instance.instance_id
        )

        {{:error, {:member_teardown_unconfirmed, reason}}, state}

      _ ->
        continue_fallback_fresh(
          state,
          instance,
          subnet_cidr,
          secret,
          group,
          plan,
          reason,
          fresh_reason
        )
    end
  rescue
    e ->
      teardown_failed(state, {:fresh_fallback_crashed, e})
      {{:error, {:fresh_fallback_crashed, e}}, state}
  end

  defp continue_fallback_fresh(
         state,
         instance,
         subnet_cidr,
         secret,
         group,
         plan,
         reason,
         fresh_reason
       ) do
    # Evict the WHOLE banked set (all-or-nothing, decision 3): the per-member bundle
    # refs come from the `plan` captured at wake start (wake_plan read the banked rows
    # BEFORE any relight cleared them), NOT the current live rows, which the resume /
    # destroy path has since mutated. Passing the plan is what makes the eviction
    # complete: every member's bundle is EvictSnapshot'd, not just the one that never
    # relit and so still carries its ref in the live rows.
    _ = evict_set(state, instance, plan)

    with {:ok, _} <- GroupStore.mark(state.store, instance.instance_id, :fresh_boot),
         {:ok, _} <-
           GroupStore.transition(
             state.store,
             instance.instance_id,
             :fresh_ready,
             :group_fresh_booted,
             %{reason: fresh_reason},
             %{}
           ),
         {:ok, _net} <- create_network(state, subnet_cidr),
         plan = member_plan(group, subnet_cidr),
         :ok <- start_members_ordered(state, plan, secret, subnet_cidr) do
      case finish_wake_publish(state, plan, group) do
        {{:ok, endpoint}, state} ->
          Logger.info("embervm group fresh-booted (relight fallback)",
            workload: state.workload,
            instance_id: instance.instance_id,
            reason: fresh_reason
          )

          {{:ok, endpoint, {:fresh, fresh_reason_atom(reason)}}, state}

        {{:error, publish_reason}, state} ->
          {{:error, publish_reason}, state}
      end
    else
      {:error, fresh_error} ->
        teardown_failed(state, {:fresh_fallback_failed, fresh_error})
        {{:error, {:fresh_fallback_failed, fresh_error}}, state}
    end
  end

  # RELIGHT every member in role order (all of one startOrder in parallel, gated,
  # then the next), exactly the create ordering but RELIGHT mode from each member's
  # banked snapshot_ref. Any member start failure short-circuits (aborting the
  # whole relight). A member with NO banked snapshot_ref is a partial set the wake
  # brain should already have fresh-booted, but guard it here as a relight failure
  # so a stale set never resumes half-warm.
  defp resume_members_ordered(state, plan) do
    # Capture the OTel ctx on the manager process (inside the `relight` root span) so
    # each spawned resume Task attaches it and its per-member child span nests under
    # the root, exactly as start_members_ordered does for a fresh create.
    otel_ctx = OpenTelemetry.Ctx.get_current()

    orders =
      plan
      |> Enum.group_by(& &1.start_order)
      |> Enum.sort_by(fn {order, _} -> order end)

    Enum.reduce_while(orders, :ok, fn {_order, members}, :ok ->
      case resume_order_parallel(state, members, otel_ctx) do
        :ok -> {:cont, :ok}
        {:error, _reason} = error -> {:halt, error}
      end
    end)
  end

  defp resume_order_parallel(state, members, otel_ctx) do
    members
    |> Enum.map(fn member ->
      Task.async(fn ->
        _ = OpenTelemetry.Ctx.attach(otel_ctx)

        try do
          resume_one_member(state, member)
        rescue
          e -> {:error, {:member_relight_crashed, member.expanded_name, e}}
        catch
          kind, reason -> {:error, {:member_relight_crashed, member.expanded_name, {kind, reason}}}
        end
      end)
    end)
    |> Enum.map(&Task.await(&1, :infinity))
    |> Enum.find(:ok, &match?({:error, _}, &1))
  end

  # A per-member RELIGHT child span (Task 9): `ember.member`, `ember.was_relight`
  # (the daemon's verified-relight verdict, the clock-resync signal), and
  # `ember.clock_delta_ms` (-1 "not reported": the daemon echoes only the boolean
  # verdict, not the measured delta). `ember.was_relight=false` on this span IS the
  # clock-resync-failed derivation the Task 11 gate reads (a relight the daemon could
  # not verify within its one-second clock bound). The span attribute is set from the
  # RPC reply inside do_resume_one_member so it reflects the actual verdict.
  defp resume_one_member(state, member) do
    Tracer.with_span "embervm.group.member_relight",
                     %{attributes: member_span_attrs(member.expanded_name, true, -1)} do
      do_resume_one_member(state, member)
    end
  end

  defp do_resume_one_member(state, member) do
    case member.snapshot_ref do
      ref when is_binary(ref) and ref != "" ->
        req = %StartGroupMemberRequest{
          trace: %Trace{workload: state.workload},
          mode: :START_GROUP_MEMBER_MODE_RELIGHT,
          group_instance_id: state.instance_id,
          member_name: member.expanded_name,
          member_index: member.index,
          ip: member.ip,
          source: "",
          snapshot_ref: ref,
          health_port: member.health_port || 0,
          resources: %ResourceSpec{vcpus: member.vcpus || 1, mem_mib: member.mem_mib || 512},
          env: %{},
          # Same one-policy budget forwarding as the FRESH path: the daemon's
          # readiness gate must not undercut the wake bound this relight runs under.
          ready_budget_seconds: wake_budget_seconds(state)
        }

        # Node-anchored: a group member RELIGHT resumes its banked bundle on the SAME
        # group network on the SAME node (relight never moves nodes, ADR embervm/014),
        # so the reject/retry frontier is a SINGLE element by construction. Routing
        # through Embervm.Placement.Retry keeps the reject/retry POLICY one piece of
        # code; with one candidate it is exactly one attempt. Do NOT expand this list.
        attempt_fun = fn _only ->
          case safe_start_group_member(state, req) do
            {:ok, %StartGroupMemberResponse{vm_id: vm_id, was_relight: true} = resp}
            when is_binary(vm_id) and vm_id != "" ->
              {:ok, record_member_live(state, member, resp)}

            # A RELIGHT that the daemon could not verify (clock-resync out of bounds,
            # decision 7, surfaces as was_relight=false): the VM DID resume and is holding
            # its pinned tap+IP, so RECORD it live first (so the fresh-fallback drain
            # destroys it and frees the tap) THEN treat it as a relight failure so the
            # whole set falls back to fresh. We never accept a half-resumed member as
            # part of a live relit set, but we must not leak its tap either. Stamp the
            # span's was_relight=false so the trace records the clock-resync-failed member.
            {:ok, %StartGroupMemberResponse{vm_id: vm_id, was_relight: false} = resp}
            when is_binary(vm_id) and vm_id != "" ->
              Tracer.set_attributes(%{"ember.was_relight" => false})
              _ = record_member_live(state, member, resp)
              {:error, {:member_relight_unverified, member.expanded_name}}

            {:ok, %StartGroupMemberResponse{was_relight: false}} ->
              Tracer.set_attributes(%{"ember.was_relight" => false})
              {:error, {:member_relight_unverified, member.expanded_name}}

            {:error, %GRPC.RPCError{status: 8}} = rejected ->
              {:reject, rejected}

            other ->
              {:error, {:member_relight_failed, member.expanded_name, other}}
          end
        end

        case Embervm.Placement.Retry.run([%{instance_id: state.instance_id}], attempt_fun) do
          {:ok, outcome} -> outcome
          {:error, :no_capacity} -> {:error, {:member_relight_failed, member.expanded_name, {:error, :no_capacity}}}
          {:error, reason} -> {:error, reason}
        end

      _ ->
        {:error, {:member_snapshot_missing, member.expanded_name}}
    end
  end

  defp record_member_live(state, member, %StartGroupMemberResponse{vm_id: vm_id, ip: ip} = resp) do
    case GroupStore.member_started(state.store, state.instance_id, %{
           member_name: member.expanded_name,
           member_index: member.index,
           vm_id: vm_id,
           ip: ip,
           # Daemon-reported entry-endpoint projection (entry member only), the
           # address finish_wake_publish/finish_create must publish.
           endpoint_ip: resp.endpoint_ip || "",
           endpoint_port: resp.endpoint_port || 0
         }) do
      {:ok, _} -> :ok
      {:error, reason} -> {:error, {:member_record_failed, member.expanded_name, reason}}
    end
  end

  # Publish the entry endpoint + move to running (creating -> running), the SAME
  # finish step create uses, but never tears down on a publish failure inside a wake
  # (the caller decides). Shared by the relight and fresh-fallback tails.
  defp finish_wake_publish(state, plan, group) do
    entry_member = Enum.find(plan, &(&1.expanded_name == group.entry.member))

    case entry_member do
      nil ->
        teardown_failed(state, {:entry_member_not_in_plan, group.entry.member})
        {{:error, {:entry_member_not_in_plan, group.entry.member}}, state}

      %{ip: entry_ip} ->
        case entry_publish_endpoint(state, group, entry_ip) do
          {:ok, ep_ip, ep_port} ->
            case GroupStore.publish(state.store, state.instance_id, ep_ip, ep_port) do
              {:ok, _} ->
                Embervm.EndpointPublisher.publish(state.publisher)
                Logger.info("embervm group woken", workload: state.workload, instance_id: state.instance_id)
                {{:ok, %{ip: ep_ip, port: ep_port}}, state}

              {:error, reason} ->
                {{:error, {:publish_failed, reason}}, state}
            end

          {:error, reason} ->
            {{:error, {:entry_port_derivation, reason}}, state}
        end
    end
  end

  # Evict the WHOLE banked set (all-or-nothing, decision 3): durably (group_set_evicted
  # clears set_id + every member's snapshot_ref in the store, no FSM edge; the
  # fresh_booting move is the mark/2 in fallback_fresh) so a rebuild agrees the warmth
  # is gone, AND best-effort EvictSnapshot every member's bundle on the node so no
  # orphan snapshot leaks. The per-member refs come from `plan` (captured at wake
  # start, before relight cleared the live rows' snapshot_refs), so the eviction is
  # complete regardless of which members relit or were destroyed. A member with no
  # captured ref (never banked) is simply skipped.
  defp evict_set(state, instance, plan) do
    _ = GroupStore.evict_set(state.store, instance.instance_id, "relight_fallback")

    for %{snapshot_ref: ref} <- plan, is_binary(ref) and ref != "" do
      _ = evict_snapshot(state, ref)
    end

    :ok
  end

  # -- bank sequence (whole-set BANK off running, R5 Task 8) ------------------

  # The whole-set bank: StopGroupMember(BANK) every live member under ONE shared
  # set_id, and either record the set atomically (all banked, banking -> banked) or
  # abort back to running (any member failed, banking -> running). The instance is
  # already `banking` (the sweeper moved it there and installed the activator before
  # calling), so a racing connection already parks on the activator; this sequence
  # never re-publishes a live endpoint (a clean bank drops it via group_banked; an
  # abort returns to running and the sweeper re-publishes).
  defp do_bank_group(state) do
    with {:ok, instance} <- fetch_instance(state),
         :banking <- instance.state do
      members = live_members(state)

      if members == [] do
        # A banking group with no live member rows: nothing to bank. Abort back to
        # running so the sweeper can re-publish (an empty group is a degraded/adoption
        # edge the reconcile heals, not the bank's concern).
        _ = GroupStore.mark(state.store, instance.instance_id, :bank_abort)
        {{:error, :no_live_members}, state}
      else
        attempt_bank(state, instance, members)
      end
    else
      {:error, _reason} = error ->
        {error, state}

      other_state when is_atom(other_state) ->
        # Not banking (a race: already banked, destroyed, or a sweeper that did not
        # pre-mark). The sweeper re-reads the store next tick; return an error it ignores.
        {{:error, {:not_banking, other_state}}, state}
    end
  end

  defp attempt_bank(state, instance, members) do
    # The `bank` root span (Task 9): a ROOT span around the whole-set bank. Per-member
    # `member_bank` child spans nest under it (via the captured ctx). `ember.pause_spread_ms`
    # (the decision-10 honesty number: the wall-clock span between the first and last
    # member's BANK completing) is set once the parallel bank returns, so the closure
    # gate can read the pause spread from the trace alone. `ember.members` is the set size.
    Tracer.with_span "embervm.group.bank",
                     %{attributes: %{"ember.workload" => state.workload, "ember.instance_id" => instance.instance_id, "ember.members" => length(members)}} do
      set_id = mint_set_id(state, instance)
      {results, pause_spread_ms} = bank_members_parallel(state, members, set_id)
      Tracer.set_attributes(%{"ember.pause_spread_ms" => pause_spread_ms})

      failed = Enum.filter(results, &match?({:error, _}, &1))

      if failed == [] do
        finish_bank(state, instance, set_id, results, pause_spread_ms)
      else
        abort_bank(state, instance, failed)
      end
    end
  end

  # StopGroupMember(BANK) every live member in PARALLEL under the shared set_id (bank
  # order is irrelevant: each BANK pauses+snapshots+destroys its own VM independently,
  # and crash-consistency is per-VM, never cross-member, per the GroupState moduledoc).
  # Returns the per-member results and the pause-spread (decision 10): the wall-clock
  # span between the first and last member's BANK completing, the honesty number the
  # closure gate reads. Each task body is wrapped so a raised/exited BANK becomes an
  # {:error, _} result rather than taking the GroupManager down mid-bank.
  defp bank_members_parallel(state, members, set_id) do
    # Capture the OTel ctx on the manager process (inside the `bank` root span) so each
    # spawned bank Task attaches it and its per-member `member_bank` child span nests
    # under the root, exactly as the start/resume loops do.
    otel_ctx = OpenTelemetry.Ctx.get_current()
    started = state.clock.()

    results =
      members
      |> Enum.map(fn member ->
        Task.async(fn ->
          _ = OpenTelemetry.Ctx.attach(otel_ctx)

          try do
            bank_one_member(state, member, set_id)
          rescue
            e -> {:error, {:member_bank_crashed, member.member_name, e}}
          catch
            kind, reason -> {:error, {:member_bank_crashed, member.member_name, {kind, reason}}}
          end
        end)
      end)
      |> Enum.map(&Task.await(&1, :infinity))

    pause_spread_ms = max(state.clock.() - started, 0)
    {results, pause_spread_ms}
  end

  # A per-member BANK child span (Task 9): `ember.member`. was_relight/clock_delta are
  # not meaningful for a bank (a bank pauses+snapshots+destroys, no resume), so this
  # carries only the member identity, bounding the StopGroupMember(BANK) RPC so a slow
  # member snapshot is visible in the pause-spread waterfall.
  defp bank_one_member(state, member, set_id) do
    Tracer.with_span "embervm.group.member_bank", %{attributes: %{"ember.member" => member.member_name}} do
      do_bank_one_member(state, member, set_id)
    end
  end

  defp do_bank_one_member(state, member, set_id) do
    req = %StopGroupMemberRequest{
      trace: %Trace{workload: state.workload},
      vm_id: member.vm_id,
      mode: :STOP_GROUP_MEMBER_MODE_BANK,
      set_id: set_id,
      member_name: member.member_name
    }

    case over_channel(state, state.dial_id, &state.stop_group_member_fun.(&1, req)) do
      {:ok, %StopGroupMemberResponse{snapshot_ref: ref}} when is_binary(ref) and ref != "" ->
        {:ok, %{name: member.member_name, snapshot_ref: ref}}

      other ->
        {:error, {:member_bank_failed, member.member_name, other}}
    end
  rescue
    e -> {:error, {:member_bank_raised, member.member_name, e}}
  catch
    kind, reason -> {:error, {:member_bank_raised, member.member_name, {kind, reason}}}
  end

  # Every member banked: record the whole set ATOMICALLY (one group_banked append
  # stamping set_id + each member's snapshot_ref, dropping the entry endpoint) and
  # re-publish so the activator install is current (bank_ready dropped the endpoint,
  # so this publish swaps in the activator for the now-empty group cluster).
  defp finish_bank(state, instance, set_id, results, pause_spread_ms) do
    members = for {:ok, m} <- results, do: m

    case GroupStore.bank_ready(state.store, instance.instance_id, set_id, members) do
      {:ok, _} ->
        Embervm.EndpointPublisher.publish(state.publisher)

        Logger.info("embervm group banked",
          workload: state.workload,
          instance_id: instance.instance_id,
          set_id: set_id,
          members: length(members),
          pause_spread_ms: pause_spread_ms
        )

        {{:ok, %{set_id: set_id, banked: length(members), pause_spread_ms: pause_spread_ms}}, state}

      {:error, reason} ->
        # The durable group_banked append failed AFTER every member's VM was already
        # snapshotted+destroyed: the set exists on disk but the log never recorded it.
        # We cannot return to a live group (the VMs are gone). Fail the instance so the
        # next connection fresh-boots (the orphan snapshots are the daemon's GC and the
        # adoption casualty path's concern), the honest terminal for an unrecordable
        # bank.
        _ =
          GroupStore.transition(
            state.store,
            instance.instance_id,
            :fail,
            :group_failed,
            %{reason: "bank_record_failed"},
            %{}
          )

        Embervm.EndpointPublisher.publish(state.publisher)
        {{:error, {:bank_record_failed, reason}}, state}
    end
  end

  # A member BANK failed: abort back to running (banking -> running, ETS-only). The
  # members that DID bank left orphan snapshots (the daemon's bundle GC reclaims them)
  # and their VMs are gone; the members that did NOT are still live. The group returns
  # to running with a partial member set, which the health/adoption path reconciles;
  # the sweeper re-publishes so the still-live group re-enters the fan-out. A failed
  # bank must never leave a warm group dark.
  defp abort_bank(state, instance, failed) do
    _ = GroupStore.mark(state.store, instance.instance_id, :bank_abort)

    Logger.warning("embervm group: bank aborted (member failure), returning to running",
      workload: state.workload,
      instance_id: instance.instance_id,
      failures: length(failed)
    )

    {{:error, {:bank_partial, failed}}, state}
  end

  # The live member rows (a live vm_id): the members a bank pauses+snapshots. A banked
  # instance's member rows carry nil vm_id (the bank cleared them), so this is empty
  # for a non-running instance, the guard do_bank_group already enforces.
  defp live_members(state) do
    state.store
    |> GroupStore.members(state.instance_id)
    |> Enum.filter(fn m -> is_binary(m.vm_id) and m.vm_id != "" end)
  end

  # A shared set_id across the members of ONE bank (the opaque set directory
  # group/<set_id>/<member>/ each member's snapshot is written under). The instance_id
  # plus the bank's start clock keeps it unique across successive banks of the same
  # instance (a re-bank after a wake gets a fresh set dir, never colliding with the
  # evicted prior set's dir). The id_fun seam lets a test pin it.
  defp mint_set_id(state, instance) do
    case Map.get(state, :set_id_fun) do
      fun when is_function(fun, 0) -> fun.()
      _ -> "set-#{instance.instance_id}-#{state.clock.()}"
    end
  end

  # -- member address plan (deterministic, lockstep with the watcher + noded) -

  # The flattened, declaration-ordered, replica-expanded member list, each stamped
  # with its global index i (0-based across the WHOLE list) and pinned IP `.10 + i`
  # on the group /24. Mirrors WorkloadWatcher.expanded_member_names/1 (replicas > 1
  # -> `<name>-<idx>` 0-based; replicas 1 -> bare name) AND noded's groupMemberIP
  # (.10 + index). Each expanded member inherits its declared member's role, source,
  # resources, health_port, env, and startOrder.
  defp member_plan(group, subnet_cidr) do
    base = subnet_base_tuple(subnet_cidr)

    group.members
    |> Enum.flat_map(&expand_member/1)
    |> Enum.with_index()
    |> Enum.map(fn {member, i} ->
      Map.merge(member, %{index: i, ip: member_ip(base, @group_member_base_offset + i)})
    end)
  end

  # Expand one declared member into its replica set: replicas > 1 -> `<name>-<idx>`
  # (0-based); replicas 1 (or absent) -> the bare name. MATCHES the watcher's
  # expanded_member_names/1 replica-expansion (minus the bare-name-also-listed quirk
  # there, which is only for entry-target validation: the ADDRESS plan is the pure
  # expanded set, one address per live member VM).
  defp expand_member(member) do
    replicas = member_replicas(member)
    role = Map.get(member, :role)
    start_order = Map.get(member, :start_order) || 0

    common = %{
      role: role,
      start_order: start_order,
      image_ref: Map.get(member, :image_ref),
      vcpus: Map.get(member, :vcpus),
      mem_mib: Map.get(member, :mem_mib),
      health_port: Map.get(member, :health_port),
      env: Map.get(member, :env) || %{}
    }

    if replicas > 1 do
      Enum.map(0..(replicas - 1), fn idx ->
        Map.put(common, :expanded_name, "#{member.name}-#{idx}")
      end)
    else
      [Map.put(common, :expanded_name, member.name)]
    end
  end

  # Clamp replicas to >= 1, guarding the Elixir truthiness trap (0 is truthy), EXACTLY
  # as WorkloadWatcher.member_replicas/1 does, so the CP and the watcher agree on the
  # expanded count.
  defp member_replicas(member) do
    case Map.get(member, :replicas) do
      n when is_integer(n) and n > 0 -> n
      _ -> 1
    end
  end

  # -- EMBER_GROUP_* env compose (decision 13) -------------------------------

  # The FRESH boot env for one expanded member: its declared env, plus the
  # platform-injected EMBER_GROUP_* keys (member identity, role, ip, the peer map,
  # and the group secret). Keys are string-keyed to match the proto map<string,string>.
  defp member_env(member, plan, secret, _workload, _subnet_cidr) do
    peer_env =
      plan
      |> Enum.reduce(%{}, fn peer, acc ->
        Map.put(acc, "EMBER_PEER_#{peer_env_key(peer.expanded_name)}", peer.ip)
      end)

    base =
      member.env
      |> Enum.into(%{}, fn {k, v} -> {to_string(k), to_string(v)} end)

    base
    |> Map.merge(peer_env)
    |> Map.merge(%{
      "EMBER_GROUP_MEMBER" => member.expanded_name,
      "EMBER_GROUP_ROLE" => to_string(member.role || ""),
      "EMBER_GROUP_IP" => member.ip,
      "EMBER_GROUP_SECRET" => secret || ""
    })
  end

  # Uppercase the expanded member name and replace `-` with `_` for the EMBER_PEER_*
  # env key (agent-0 -> AGENT_0). A DNS-label member name has only [a-z0-9-], so this
  # yields a valid [A-Z0-9_] env-var suffix.
  defp peer_env_key(name) do
    name |> String.upcase() |> String.replace("-", "_")
  end

  # -- secret sourcing -------------------------------------------------------

  # With spec.group.secretRef, read the referenced Secret key at create (stable
  # across instances by construction). Without it, mint 32 bytes base64url (the
  # secret_fun seam, injected in tests) recorded in the group_created op. Fail-open on
  # a secret-read failure: proceed with an empty secret (the group's own readiness is
  # the loud failure surface), matching the stateful mmds_env posture.
  defp resolve_secret(state, entry_cfg, group) do
    case Map.get(group, :secret_ref) do
      %{name: name, key: key} when is_binary(name) and name != "" ->
        namespace = Map.get(entry_cfg, :namespace)

        case state.get_secret_fun.(namespace, name) do
          {:ok, data} ->
            Map.get(data, key) || ""

          {:error, reason} ->
            Logger.warning("embervm group: secretRef read failed, proceeding with empty secret (fail-open)",
              workload: state.workload,
              secret_ref: name,
              reason: inspect(reason)
            )

            ""
        end

      _ ->
        state.secret_fun.()
    end
  end

  defp mint_secret do
    32 |> :crypto.strong_rand_bytes() |> Base.url_encode64(padding: false)
  end

  # -- subnet allocation (lowest-free /24 from the composite supernet) --------

  # Assign the LOWEST /24 within the supernet not currently held by a LIVE-or-BANKED
  # group instance (GroupStore.held_subnets/1). Lowest-free is a pure function of live
  # state (nothing extra to persist; the assigned CIDR is recorded in group_created so
  # a rebuild reconstructs the held set) and reuses freed /24s so the /16 cannot be
  # exhausted. The supernet is the SAME value noded validates group cidrs against, so
  # every assigned /24 is a valid /24 wholly within it. Two concurrent creates for
  # DIFFERENT workloads could momentarily read the same held set and pick the same /24
  # (this is not serialized across GroupManager instances); that race is
  # DAEMON-BACKSTOPPED: noded's CreateGroupNetwork refuses an overlapping CIDR
  # (FAILED_PRECONDITION), so the loser's create fails and retries, which re-reads the
  # now-updated held set and picks the next free /24. The CP allocation is an
  # optimization over that backstop, not the sole guard.
  defp allocate_subnet(state) do
    held = GroupStore.held_subnets(state.store)
    {a, b, _c, _d} = parse_ip(supernet_base(state.supernet))

    # A /16 supernet gives third octets 0..255; each /24 is a.b.<c>.0/24. Pick the
    # lowest c whose /24 is not held. (A supernet larger than /16 would carry more
    # candidates; v1's compositeSupernet is a /16, so the third octet enumerates the
    # /24s.)
    Enum.find_value(0..255, fn c ->
      cidr = "#{a}.#{b}.#{c}.0/24"
      if MapSet.member?(held, cidr), do: nil, else: cidr
    end) || raise "embervm group: composite supernet #{state.supernet} exhausted (all /24s held)"
  end

  # -- entry DNAT port (CP re-derives; lockstep with noded PortForIP) ---------

  # vmPort = port_base + hostOffset(entryIP, supernet), the EXACT derivation noded's
  # serving/net.go PortForIP does (offset within the SUPERNET, not the group /24), so
  # the CP's published {pod IP, vmPort} matches the daemon's installed DNAT rule.
  # MUST stay in lockstep with noded/serving/net.go:PortForIP + hostOffset.
  defp entry_vm_port(state, entry_ip) do
    with {:ok, offset} <- host_offset(state.supernet, entry_ip) do
      port = state.port_base + offset

      if port >= 1 and port <= 65_535 do
        {:ok, port}
      else
        {:error, {:port_out_of_range, port}}
      end
    end
  end

  # -- IP math ---------------------------------------------------------------

  defp subnet_base(cidr), do: cidr |> String.split("/") |> hd()

  defp supernet_base(cidr), do: cidr |> String.split("/") |> hd()

  defp subnet_base_tuple(cidr), do: cidr |> subnet_base() |> parse_ip()

  # The pinned member IP: network base + host offset (`.10 + i`). base is the /24
  # network address tuple.
  defp member_ip({a, b, c, _d}, offset) do
    "#{a}.#{b}.#{c}.#{offset}"
  end

  defp member_ip(cidr, offset) when is_binary(cidr) do
    member_ip(subnet_base_tuple(cidr), offset)
  end

  # hostOffset within the supernet: the ip's host part as a big-endian int, masked by
  # the supernet prefix. Mirrors noded's hostOffset (serving/net.go). Errors when ip
  # is not inside the supernet. v1 supernets are /16, so the offset is
  # (third_octet << 8) | fourth_octet.
  defp host_offset(supernet_cidr, ip) do
    [net, prefix_str] = String.split(supernet_cidr, "/")
    prefix = String.to_integer(prefix_str)
    {na, nb, nc, nd} = parse_ip(net)
    {ia, ib, ic, id} = parse_ip(ip)

    net_int = ip_to_int({na, nb, nc, nd})
    ip_int = ip_to_int({ia, ib, ic, id})
    mask = mask_for(prefix)

    if band(net_int, mask) == band(ip_int, mask) do
      {:ok, ip_int |> band(bnot(mask)) |> band(0xFFFFFFFF)}
    else
      {:error, {:ip_outside_supernet, ip, supernet_cidr}}
    end
  end

  defp ip_to_int({a, b, c, d}), do: bsl(a, 24) + bsl(b, 16) + bsl(c, 8) + d

  defp mask_for(prefix) do
    ones = bsl(0xFFFFFFFF, 32 - prefix)
    band(ones, 0xFFFFFFFF)
  end

  defp parse_ip(ip) do
    [a, b, c, d] = ip |> String.split(".") |> Enum.map(&String.to_integer/1)
    {a, b, c, d}
  end

  # -- daemon seams ----------------------------------------------------------

  defp create_network(state, cidr) do
    req = %CreateGroupNetworkRequest{
      trace: %Trace{workload: state.workload},
      group_instance_id: state.instance_id,
      cidr: cidr
    }

    case over_channel(state, state.dial_id, &state.create_group_network_fun.(&1, req)) do
      {:ok, %CreateGroupNetworkResponse{} = resp} -> {:ok, resp}
      other -> {:error, {:create_network_failed, other}}
    end
  rescue
    e -> {:error, {:create_network_raised, e}}
  end

  defp delete_network(state) do
    req = %DeleteGroupNetworkRequest{trace: %Trace{workload: state.workload}, group_instance_id: state.instance_id}

    with {:ok, channel} <- safe_channel(state, state.dial_id) do
      try do
        state.delete_group_network_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp safe_start_group_member(state, req) do
    over_channel(state, state.dial_id, &state.start_group_member_fun.(&1, req))
  rescue
    e -> {:error, {:start_group_member_raised, e}}
  catch
    kind, reason -> {:error, {:start_group_member_raised, {kind, reason}}}
  end

  defp safe_channel(state, node_id) do
    state.channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # Acquire the node's shared channel and run a group verb over it, invalidating that
  # channel (so the next safe_channel/2 re-dials) when the call reveals the transport
  # is dead: a transport_dead? error return, or an :exit from the Mint ConnectionProcess
  # dying (its crash on an in-flight stream RST during a noded rollout surfaces here as
  # an exit). A server-returned gRPC status rode a HEALTHY channel and never invalidates
  # it (Embervm.NodeChannel.transport_dead?/1, D-R2.7.2). Returns the verb's raw result,
  # {:error, {:exit, reason}} on an exit, or the safe_channel error if no channel.
  defp over_channel(state, node_id, call_fun) do
    with {:ok, channel} <- safe_channel(state, node_id) do
      try do
        result = call_fun.(channel)
        if Embervm.NodeChannel.transport_dead?(result), do: state.invalidate_fun.(node_id, channel)
        result
      catch
        :exit, reason ->
          state.invalidate_fun.(node_id, channel)
          {:error, {:exit, reason}}
      end
    end
  end

  defp default_create_group_network(channel, req) do
    Embervm.Node.V1.NodeService.Stub.create_group_network(channel, req)
  end

  defp default_delete_group_network(channel, req) do
    Embervm.Node.V1.NodeService.Stub.delete_group_network(channel, req)
  end

  # StartGroupMember blocks server-side for the member's whole readiness gate (up
  # to ready_budget_seconds, e.g. 180s for scratch-k8s), so the call deadline must
  # cover the budget plus RPC margin. Without an explicit timeout the grpc-elixir
  # default 10s deadline fires mid-gate: the daemon cancels the stream (the member
  # is reaped "context canceled" while healthy and mid-join) and the RST_STREAM
  # cancel trips the Mint adapter's connection-crash bug, killing every sibling
  # member stream on the same channel. This deadline was the true k3s-agent killer
  # behind the R6 Gate-1 drill; the daemon-side budget fix alone cannot help while
  # the client hangs up at 10s.
  defp default_start_group_member(channel, req) do
    budget_ms =
      case req.ready_budget_seconds do
        secs when is_integer(secs) and secs > 0 -> secs * 1_000
        _ -> 60_000
      end

    Embervm.Node.V1.NodeService.Stub.start_group_member(channel, req, timeout: budget_ms + 30_000)
  end

  defp default_stop_group_member(channel, req) do
    Embervm.Node.V1.NodeService.Stub.stop_group_member(channel, req)
  end

  defp default_evict_snapshot(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_snapshot(channel, req)
  end

  # DESTROY every currently-live member VM of the instance (a partial relight that
  # resumed some members before aborting), freeing each member's pinned tap+IP in
  # noded's group allocator (StopGroupMember DESTROY -> RemoveMemberTap +
  # ReleaseMember) so the fresh re-pin does not collide. Drains from the instance's
  # stored member rows: a member with a live vm_id is one that came up. Best-effort
  # per member (a failed DESTROY leaves its tap held, which the fresh boot's own
  # collision surfaces loudly rather than being silently skipped here).
  defp destroy_live_members(state) do
    if state.node_confirmed_destroy do
      destroy_live_members_node_confirmed(state)
    else
      destroy_live_members_legacy(state)
    end
  end

  defp destroy_live_members_legacy(state) do
    state.store
    |> GroupStore.members(state.instance_id)
    |> Enum.filter(fn m -> is_binary(m.vm_id) and m.vm_id != "" end)
    |> Enum.each(fn m -> _ = destroy_one_member(state, m) end)

    :ok
  end

  # This is the relight-to-fresh cleanup path, not a terminal group destroy, so it
  # does not write group lifecycle ops. Under the gate it still collects the real
  # daemon confirmation for every reachable member; an unconfirmed member defers the
  # fresh boot so it never re-pins over a tap the daemon may still hold.
  defp destroy_live_members_node_confirmed(state) do
    confirmations =
      state.store
      |> GroupStore.members(state.instance_id)
      |> Enum.map(fn member ->
        if is_binary(state.node_id) and is_binary(member.vm_id) do
          destroy_one_member_confirmed(state, member)
        else
          true
        end
      end)

    Enum.all?(confirmations)
  end

  defp destroy_one_member(state, member) do
    req = %StopGroupMemberRequest{
      trace: %Trace{workload: state.workload},
      vm_id: member.vm_id,
      mode: :STOP_GROUP_MEMBER_MODE_DESTROY,
      set_id: "",
      member_name: member.member_name
    }

    with {:ok, channel} <- safe_channel(state, state.dial_id) do
      try do
        state.stop_group_member_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp destroy_one_member_confirmed(state, member) do
    req = %StopGroupMemberRequest{
      trace: %Trace{workload: state.workload},
      vm_id: member.vm_id,
      mode: :STOP_GROUP_MEMBER_MODE_DESTROY,
      set_id: "",
      member_name: member.member_name
    }

    with {:ok, channel} <- safe_channel(state, state.dial_id) do
      try do
        match?({:ok, %{teardown_confirmed: true}}, state.stop_group_member_fun.(channel, req))
      rescue
        _ -> false
      catch
        _, _ -> false
      end
    else
      _ -> false
    end
  end

  # Best-effort EvictSnapshot of one member's banked bundle (group bundles reuse the
  # R2 EvictSnapshot unchanged). Never raises into the wake: a failed evict leaves a
  # stale snapshot on disk (the daemon's own bundle GC reclaims it later), which must
  # not fail the fresh-fallback boot the caller is parked on.
  defp evict_snapshot(state, snapshot_ref) do
    req = %EvictSnapshotRequest{trace: %Trace{workload: state.workload}, snapshot_ref: snapshot_ref}

    with {:ok, channel} <- safe_channel(state, state.dial_id) do
      try do
        state.evict_snapshot_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  # -- wake helpers ----------------------------------------------------------

  defp fetch_instance(state) do
    case GroupStore.get(state.store, state.instance_id) do
      {:ok, instance} -> {:ok, instance}
      :error -> {:error, {:instance_not_found, state.instance_id}}
    end
  end

  # The wake plan: the member address plan (deterministic, catalog-derived) merged
  # with each member's STORED live facts (its banked snapshot_ref, needed for the
  # RELIGHT source). The pinned ip comes from the deterministic plan (lockstep with
  # noded), NOT the stored ip, so a relight re-pins the same address a fresh boot
  # would (the bank cleared the live ip anyway).
  defp wake_plan(state, group, subnet_cidr, instance) do
    stored = GroupStore.members(state.store, instance.instance_id)
    by_name = Map.new(stored, fn m -> {m.member_name, m} end)

    member_plan(group, subnet_cidr)
    |> Enum.map(fn member ->
      snapshot_ref =
        case Map.get(by_name, member.expanded_name) do
          %{snapshot_ref: ref} -> ref
          _ -> nil
        end

      Map.put(member, :snapshot_ref, snapshot_ref)
    end)
  end

  # The group secret on a wake: a relight/fresh-boot re-derives the SAME secret the
  # birth boot recorded (from secretRef, or the minted value on the create op). The
  # store does not surface the secret on the instance view, so re-resolve it exactly
  # as create did (a secretRef read is stable; a minted secret is only re-minted on a
  # fresh boot, which is acceptable: a fresh boot is a NEW first boot of the members,
  # and the members re-read their birth env fresh anyway).
  defp instance_secret(state, entry_cfg, group, _instance) do
    resolve_secret(state, entry_cfg, group)
  end

  # A wake that found the instance no longer banked (a race with adoption/destroy):
  # return the live endpoint if it is running, else an error the wake brain surfaces
  # to the parked caller (which reconnects).
  defp wake_race_reply(state) do
    case GroupStore.get(state.store, state.instance_id) do
      {:ok, %{state: :running, entry_ip: ip, entry_port_published: port}}
      when is_binary(ip) and is_integer(port) ->
        {:ok, %{ip: ip, port: port}, :straggler}

      _ ->
        {:error, :not_banked}
    end
  end

  defp fresh_reason_atom({:relight_failed, {:member_relight_unverified, _}}), do: :clock_resync_failed
  defp fresh_reason_atom({:relight_failed, {:member_snapshot_missing, _}}), do: :partial_set
  defp fresh_reason_atom({:relight_failed, _}), do: :relight_failed
  defp fresh_reason_atom({:relit_record_failed, _}), do: :relit_record_failed
  defp fresh_reason_atom(reason) when is_atom(reason), do: reason
  defp fresh_reason_atom(_), do: :relight_failed

  defp fresh_reason_string(reason), do: reason |> fresh_reason_atom() |> Atom.to_string()

  # -- misc ------------------------------------------------------------------

  defp ok_or({:ok, _}), do: :ok
  defp ok_or({:error, _} = error), do: error

  defp failure_reason_string(reason) when is_atom(reason), do: Atom.to_string(reason)
  defp failure_reason_string({tag, _} = _reason) when is_atom(tag), do: Atom.to_string(tag)
  defp failure_reason_string(reason), do: inspect(reason)

  defp default_clock, do: System.system_time(:millisecond)

  # -- span attributes (Task 9) ----------------------------------------------

  # The per-member span attribute map: `ember.member` (expanded member name),
  # `ember.was_relight` (the resume verdict; false for a fresh start), and
  # `ember.clock_delta_ms` (the clock-resync delta in ms, or the -1 "not reported"
  # sentinel when the daemon echoes only the boolean verdict). All three are concrete
  # typed values (never nil), per the OTel Elixir SDK's per-key typing requirement.
  defp member_span_attrs(member_name, was_relight, clock_delta_ms) do
    %{
      "ember.member" => member_name,
      "ember.was_relight" => was_relight,
      "ember.clock_delta_ms" => clock_delta_ms
    }
  end

  # -- convenience for catalog resolution (used by the supervisor) -----------

  @doc false
  @spec catalog_group(atom() | :ets.tid(), String.t()) :: {:ok, map()} | :error
  def catalog_group(catalog_table, workload) do
    case WorkloadCatalog.fetch(catalog_table, workload) do
      {:ok, %{class: "composite", group: group} = entry} when is_map(group) ->
        {:ok, Map.put(entry, :name, workload)}

      _ ->
        :error
    end
  end

  @doc false
  def group_state_module, do: GroupState
end
