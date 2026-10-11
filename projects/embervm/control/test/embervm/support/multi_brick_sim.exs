defmodule Embervm.MultiBrickSim.Noded do
  @moduledoc """
  noded's memory admission predicate, ported from
  `projects/embervm/noded/server/pressure.go` so the simulated bricks refuse
  exactly when a real daemon would.

    * `facts/2` is `admissionFactsLocked`: one reading of every input, taken
      under the same lock as the reservation (here, the fleet process).
    * `admittable_mib/1` is `admissionFacts.admittableMib`:

          observed = headroom - inflight - floor
          reserved = budget - claimed - inflight - overhead * (live + inflightVMs + 1)

      The reserved model admits on `min(reserved, observed)`; the observed model
      on `observed` alone. Headroom is always known here (a fake cgroup), and a
      known but exhausted cgroup reads at least 1 MiB, as the budget reader does.
    * `admit/2` is `Server.admit`'s check order: the live-VM backstop
      (`slotsFull`), then the memory predicate (`memAdmits`). Refusal messages
      copy `Server.refusal` byte for byte.

  `ledger: false` models a daemon without the in-flight ticket ledger (the
  pre-#6905 check-then-act gate): its in-flight terms read zero.
  """

  @doc "The admission inputs for a brick and the needs of the boots it has admitted."
  def facts(brick, pending_needs) do
    vms = Map.values(brick.vms)
    host = vms |> Enum.map(&(&1.resident + brick.overhead)) |> Enum.sum()

    {inflight_mib, inflight_vms} =
      if brick.ledger, do: {Enum.sum(pending_needs), length(pending_needs)}, else: {0, 0}

    %{
      model: brick.model,
      budget: brick.budget,
      headroom: max(brick.budget - host, 1),
      claimed: vms |> Enum.map(& &1.declared) |> Enum.sum(),
      live: length(vms),
      inflight_mib: inflight_mib,
      inflight_vms: inflight_vms,
      overhead: brick.overhead,
      floor: brick.floor,
      slot_ceiling: brick.slot_ceiling
    }
  end

  @doc "The largest guest memMib this brick admits for ONE more VM (signed)."
  def admittable_mib(f) do
    observed = f.headroom - f.inflight_mib - f.floor

    if f.model == :reserved and f.budget > 0 do
      reserved = f.budget - f.claimed - f.inflight_mib - f.overhead * (f.live + f.inflight_vms + 1)
      min(observed, reserved)
    else
      observed
    end
  end

  def slots_full?(f), do: f.slot_ceiling > 0 and f.live + f.inflight_vms >= f.slot_ceiling

  @doc "`:ok`, or `{:refuse, token, %GRPC.RPCError{status: 8}}` with noded's message."
  def admit(f, need) do
    cond do
      slots_full?(f) ->
        {:refuse, "slots",
         %GRPC.RPCError{
           status: 8,
           message: "noded: node live-VM cap #{f.slot_ceiling} reached (live #{f.live}, in flight #{f.inflight_vms})"
         }}

      need > admittable_mib(f) ->
        {:refuse, "pressure:mem",
         %GRPC.RPCError{
           status: 8,
           message:
             "noded: pressure:mem (need #{need} MiB, admittable #{admittable_mib(f)} MiB, model #{model_name(f)})"
         }}

      true ->
        :ok
    end
  end

  defp model_name(%{model: :reserved}), do: "reserved"
  defp model_name(_), do: "observed"
end

defmodule Embervm.MultiBrickSim.Fleet do
  @moduledoc """
  A fleet of fake bricks behind the control plane's injectable RPC seams.

  The CP-facing functions (`prime/3`, `assign/3`, `bank/3`, `destroy/3`) run in
  the CP's own worker processes. An admitted boot holds an in-flight ticket and
  BLOCKS its caller until the harness lands it (`land/3`), and a running task
  blocks in Assign until the harness completes it, so "several creates in flight
  at once" is a state the harness steps through rather than a race. Every RPC
  sends the harness a `{:sim_rpc, event}` naming the calling process.

  Capacity facts reach `Embervm.NodeCapacity` ONLY on an explicit `report/2`
  (or registration), so the control plane places on whatever it last saw.
  """
  use GenServer

  alias Embervm.MultiBrickSim.Noded
  alias Embervm.NodeCapacity
  alias Embervm.Node.V1.{AssignResponse, BankResponse, GuestResponse, PrimeResponse, UsageStats}

  def start_link(opts), do: GenServer.start_link(__MODULE__, opts)

  # -- CP-facing seams --------------------------------------------------------

  def prime(fleet, {:sim, lane, dial}, req) do
    case GenServer.call(fleet, {:boot, lane, dial, req.snapshot_ref, self()}, :infinity) do
      {:admitted, id} -> await(id)
      {:error, _} = error -> error
    end
  end

  def assign(fleet, {:sim, lane, dial}, req) do
    case GenServer.call(fleet, {:assign, lane, dial, req.vm_id, self()}, :infinity) do
      {:running, id} -> await(id)
      {:error, _} = error -> error
    end
  end

  def bank(fleet, {:sim, lane, dial}, req),
    do: GenServer.call(fleet, {:bank, lane, dial, req.vm_id, self()}, :infinity)

  def destroy(fleet, {:sim, lane, dial}, vm_id),
    do: GenServer.call(fleet, {:destroy, lane, dial, vm_id, self()}, :infinity)

  defp await(id) do
    receive do
      {:sim_reply, ^id, reply} -> reply
    end
  end

  # -- harness controls -------------------------------------------------------

  def register(fleet, spec), do: GenServer.call(fleet, {:register, spec})
  def report(fleet, dial), do: GenServer.call(fleet, {:report, dial})
  def land(fleet, ticket_id, resident_mib), do: GenServer.call(fleet, {:land, ticket_id, resident_mib})
  def complete(fleet, vm_id), do: GenServer.call(fleet, {:complete, vm_id})
  def drain(fleet, dial), do: GenServer.call(fleet, {:drain, dial})
  def vanish(fleet, dial), do: GenServer.call(fleet, {:vanish, dial})
  def set_resident(fleet, updates), do: GenServer.call(fleet, {:set_resident, updates})
  def snapshot(fleet), do: GenServer.call(fleet, :snapshot)
  def kill_waiters(fleet), do: GenServer.call(fleet, :kill_waiters)

  @doc "The ground-truth admittable MiB of one brick, as `admit/2` would read it now."
  def admittable(state, dial) do
    brick = Map.fetch!(state.bricks, dial)
    brick |> Noded.facts(pending_needs(state, dial)) |> Noded.admittable_mib()
  end

  def pending_needs(state, dial) do
    for {_id, %{dial: ^dial, need: need}} <- state.tickets, do: need
  end

  # -- server -----------------------------------------------------------------

  @impl true
  def init(opts) do
    {:ok,
     %{
       harness: Keyword.fetch!(opts, :harness),
       cap_table: Keyword.fetch!(opts, :cap_table),
       workloads: Keyword.fetch!(opts, :workloads),
       updated_at: Keyword.get(opts, :updated_at, 1_000_000),
       bricks: %{},
       tickets: %{},
       assigns: %{},
       seq: 0
     }}
  end

  @impl true
  def handle_call({:boot, lane, dial, snapshot_ref, pid}, _from, st) do
    brick = Map.get(st.bricks, dial)
    workload = workload_of(snapshot_ref)
    need = Map.get(st.workloads, workload)
    base = %{verb: :prime, lane: lane, dial: dial, pid: pid, workload: workload, need: need}

    cond do
      brick == nil or brick.status == :gone ->
        notify(st, Map.merge(base, %{outcome: :gone, class: brick && brick.class, status: :gone}))
        {:reply, {:error, unavailable()}, st}

      need == nil ->
        notify(st, Map.merge(base, %{outcome: :bad_request, class: brick.class, status: brick.status}))
        {:reply, {:error, %GRPC.RPCError{status: 3, message: "noded: unknown snapshot #{snapshot_ref}"}}, st}

      true ->
        facts = Noded.facts(brick, pending_needs(st, dial))
        base = Map.merge(base, %{class: brick.class, status: brick.status, admittable: Noded.admittable_mib(facts)})

        case Noded.admit(facts, need) do
          :ok ->
            id = st.seq + 1
            ticket = %{id: id, dial: dial, need: need, workload: workload, lane: lane, pid: pid}
            notify(st, Map.merge(base, %{outcome: :admitted, ticket: id}))
            {:reply, {:admitted, id}, %{st | seq: id, tickets: Map.put(st.tickets, id, ticket)}}

          {:refuse, token, error} ->
            brick = %{brick | refusals: Map.update(brick.refusals, token, 1, &(&1 + 1))}
            notify(st, Map.merge(base, %{outcome: :refused, reason: token}))
            {:reply, {:error, error}, put_brick(st, brick)}
        end
    end
  end

  def handle_call({:assign, lane, dial, vm_id, pid}, _from, st) do
    brick = Map.get(st.bricks, dial)
    base = %{verb: :assign, lane: lane, dial: dial, pid: pid, vm_id: vm_id}

    cond do
      brick == nil or brick.status == :gone ->
        notify(st, Map.put(base, :outcome, :gone))
        {:reply, {:error, unavailable()}, st}

      not Map.has_key?(brick.vms, vm_id) ->
        notify(st, Map.put(base, :outcome, :not_found))
        {:reply, {:error, %GRPC.RPCError{status: 5, message: "noded: vm #{vm_id} not found"}}, st}

      true ->
        id = st.seq + 1
        notify(st, Map.put(base, :outcome, :running))
        assigns = Map.put(st.assigns, vm_id, %{id: id, dial: dial, pid: pid, vm_id: vm_id, lane: lane})
        {:reply, {:running, id}, %{st | seq: id, assigns: assigns}}
    end
  end

  def handle_call({verb, lane, dial, vm_id, pid}, _from, st) when verb in [:bank, :destroy] do
    brick = Map.get(st.bricks, dial)
    base = %{verb: verb, lane: lane, dial: dial, pid: pid, vm_id: vm_id}

    cond do
      brick == nil or brick.status == :gone ->
        notify(st, Map.put(base, :outcome, :gone))
        {:reply, {:error, unavailable()}, st}

      verb == :bank and not Map.has_key?(brick.vms, vm_id) ->
        notify(st, Map.put(base, :outcome, :not_found))
        {:reply, {:error, %GRPC.RPCError{status: 5, message: "noded: vm #{vm_id} not found"}}, st}

      true ->
        {vm, vms} = Map.pop(brick.vms, vm_id)
        notify(st, Map.put(base, :outcome, :ok))
        st = put_brick(st, %{brick | vms: vms})

        reply =
          case verb do
            :bank -> {:ok, %BankResponse{snapshot_ref: "bank-" <> vm_id, size_bytes: vm.declared * 1_048_576}}
            :destroy -> {:ok, %{teardown_confirmed: true}}
          end

        {:reply, reply, st}
    end
  end

  def handle_call({:register, spec}, _from, st) do
    brick =
      Map.merge(
        %{
          status: :up,
          vms: %{},
          refusals: %{},
          model: :reserved,
          ledger: true,
          reports_ledger: true
        },
        spec
      )

    st = put_brick(st, brick)
    publish(st, brick)
    {:reply, brick.dial, st}
  end

  def handle_call({:report, dial}, _from, st) do
    case Map.get(st.bricks, dial) do
      %{status: :up} = brick -> publish(st, brick)
      _ -> :ok
    end

    {:reply, :ok, st}
  end

  def handle_call({:land, id, resident}, _from, st) do
    {ticket, tickets} = Map.pop!(st.tickets, id)
    brick = Map.fetch!(st.bricks, ticket.dial)
    seq = st.seq + 1
    vm_id = "vm-#{seq}"

    vm = %{
      vm_id: vm_id,
      declared: ticket.need,
      resident: min(ticket.need, resident),
      workload: ticket.workload,
      lane: ticket.lane
    }

    st = put_brick(%{st | seq: seq, tickets: tickets}, %{brick | vms: Map.put(brick.vms, vm_id, vm)})
    send(ticket.pid, {:sim_reply, id, {:ok, %PrimeResponse{vm_id: vm_id}}})
    {:reply, {:ok, Map.put(ticket, :vm_id, vm_id)}, st}
  end

  def handle_call({:complete, vm_id}, _from, st) do
    {assign, assigns} = Map.pop!(st.assigns, vm_id)
    brick = Map.fetch!(st.bricks, assign.dial)
    # A task VM is single-use: noded destroys it on Assign.
    st = put_brick(%{st | assigns: assigns}, %{brick | vms: Map.delete(brick.vms, vm_id)})

    response = %AssignResponse{
      response: %GuestResponse{status_code: 200, headers: %{}, body: "ok"},
      usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}
    }

    send(assign.pid, {:sim_reply, assign.id, {:ok, response}})
    {:reply, :ok, st}
  end

  def handle_call({:drain, dial}, _from, st) do
    brick = Map.fetch!(st.bricks, dial)
    NodeCapacity.drop(st.cap_table, {brick.node_id, brick.pod_uid})
    {:reply, :ok, put_brick(st, %{brick | status: :draining})}
  end

  def handle_call({:vanish, dial}, _from, st) do
    brick = Map.fetch!(st.bricks, dial)
    NodeCapacity.drop(st.cap_table, {brick.node_id, brick.pod_uid})
    {lost_tickets, tickets} = Enum.split_with(st.tickets, fn {_id, t} -> t.dial == dial end)
    {lost_assigns, assigns} = Enum.split_with(st.assigns, fn {_vm, a} -> a.dial == dial end)

    for {id, t} <- lost_tickets, do: send(t.pid, {:sim_reply, id, {:error, unavailable()}})
    for {_vm, a} <- lost_assigns, do: send(a.pid, {:sim_reply, a.id, {:error, unavailable()}})

    lost = %{
      tickets: Enum.map(lost_tickets, &elem(&1, 1)),
      assigns: Enum.map(lost_assigns, &elem(&1, 1)),
      vms: Map.keys(brick.vms)
    }

    st = %{st | tickets: Map.new(tickets), assigns: Map.new(assigns)}
    {:reply, lost, put_brick(st, %{brick | status: :gone, vms: %{}})}
  end

  def handle_call({:set_resident, updates}, _from, st) do
    st =
      Enum.reduce(updates, st, fn {dial, vm_id, resident}, acc ->
        brick = Map.fetch!(acc.bricks, dial)
        put_brick(acc, %{brick | vms: Map.update!(brick.vms, vm_id, &%{&1 | resident: resident})})
      end)

    {:reply, :ok, st}
  end

  def handle_call(:snapshot, _from, st), do: {:reply, st, st}

  def handle_call(:kill_waiters, _from, st) do
    for {_id, t} <- st.tickets, do: Process.exit(t.pid, :kill)
    for {_vm, a} <- st.assigns, do: Process.exit(a.pid, :kill)
    {:reply, :ok, %{st | tickets: %{}, assigns: %{}}}
  end

  defp put_brick(st, brick), do: %{st | bricks: Map.put(st.bricks, brick.dial, brick)}

  defp notify(st, event), do: send(st.harness, {:sim_rpc, event})

  defp workload_of("snap-" <> workload), do: workload
  defp workload_of(_), do: nil

  defp unavailable, do: %GRPC.RPCError{status: 14, message: "noded: connection lost (brick gone)"}

  # The NodeStatus projection the registry would write (NodeRegistry
  # facts_from_status), for the fields placement reads.
  defp publish(st, brick) do
    facts = Noded.facts(brick, pending_needs(st, brick.dial))
    ledger? = brick.reports_ledger

    NodeCapacity.put(st.cap_table, {brick.node_id, brick.pod_uid}, %{
      node_id: brick.node_id,
      reported_node_id: brick.node_id,
      configured_id: brick.node_id,
      pod_uid: brick.pod_uid,
      instance_id: brick.dial,
      boot_id: "boot-" <> brick.pod_uid,
      size_class: brick.class,
      cpu_vendor: "amd",
      workloads:
        Map.new(st.workloads, fn {wl, _mib} ->
          {wl,
           %{
             free_primed_slots: 0,
             snapshot_ref: "snap-" <> wl,
             serving_image_ref: "",
             base_state: :BASE_BUILD_STATE_READY,
             exported: true,
             primed_vm_ids: []
           }}
        end),
      mem_headroom_mib: facts.headroom,
      mem_reject_floor_mib: brick.floor,
      mem_reserved_mib: facts.claimed,
      admits_on_reservation: brick.model == :reserved,
      vm_overhead_mib: brick.overhead,
      admission_reported: ledger?,
      mem_admittable_mib: if(ledger?, do: Noded.admittable_mib(facts), else: 0),
      mem_inflight_mib: if(ledger?, do: facts.inflight_mib, else: 0),
      inflight_vms: if(ledger?, do: facts.inflight_vms, else: 0),
      admission_refusals: brick.refusals,
      jailer_armed: false,
      cpu_headroom_millicores: 4_000,
      live_vms: facts.live,
      max_live_vms: brick.slot_ceiling,
      mem_budget_mib: brick.budget,
      cpu_budget_millicores: 4_000,
      draining: false,
      store_reachable: false,
      updated_at: st.updated_at,
      session_vms: [],
      session_snapshots: [],
      session_volumes: [],
      session_volumes_complete: true,
      snapshot_disk_free_bytes: 100 * 1_073_741_824,
      snapshot_disk_used_bytes: 0,
      serving_vms: [],
      serving_snapshots: [],
      serving_subnet_cidr: "",
      stateful_vms: [],
      group_member_vms: []
    })
  end
end

defmodule Embervm.MultiBrickSim do
  @moduledoc """
  A deterministic multi-brick scheduling simulation of the EmberVM control plane.

  The REAL `Embervm.SessionManager`, `Embervm.Dispatcher` (with a real
  `TaskStore` and SQLite op-log), `Embervm.Scheduler`, `Embervm.Scheduler.Inflight`
  (a private table), `Embervm.Brick`, `Embervm.Scheduler.Retry` and
  `Embervm.BrickController` run against `Embervm.MultiBrickSim.Fleet`, a fleet of
  fake bricks that admit through noded's predicate (`Embervm.MultiBrickSim.Noded`).

  ## determinism

  The harness is the only clock. Each step performs one operation and then
  SETTLES: it waits, on messages rather than sleeps, until every control-plane
  process the step woke is either blocked inside a fake RPC (an admitted boot
  waiting to land, a task running in Assign) or has exited, every outstanding
  create is blocked or answered, and the managers have drained their mailboxes.
  Only then does the next operation run, so one seed always replays the same
  trace. Virtual time moves only on an `{:advance, ms}` step: it shifts every
  Inflight row's expiry (settle holds lapse), grows guest resident memory and
  advances the BrickController clock.

  ## invariants (checked after every step)

  Named after the properties of `projects/embervm/specs/admission.tla`:

    * `NoOvercommit`: per brick, declared memory of live VMs plus in-flight
      tickets, each with the per-VM overhead, fits the budget.
    * `ResidentWithinBudget`: resident guest memory plus overhead fits the budget.
    * `NoStrandedRequest` (create lane): a create ends placed or as a retryable
      `{:denied, :no_capacity}`, never another error, unless the brick its boot
      was in flight on vanished; every refusal is rerouted to a brick the create
      has not tried or ends the create as `:no_capacity`; each `:no_capacity`
      records exactly one demand note.
    * `NoLeakedClaim`: unsettled Inflight rows per brick equal the boots in flight
      there; held rows sit only on bricks that booted something; at the end no
      row and no ticket remains (`ClaimsDrain`, `TicketsEventuallyReleased`).
    * `ClassFit`: every boot RPC targets, and every VM lives on, a brick whose
      class can hold it; no boot RPC reaches a draining or vanished brick.
    * `SessionBookkeeping`: a placed session's row and remembered dial name the
      brick its VM actually landed on.
    * `DemandAttribution`: every demand note lands on the smallest fitting class
      with replica headroom, else the smallest fitting class, and every
      BrickController scale-up is justified by its attributed window.
  """

  import ExUnit.Assertions

  alias Embervm.{BrickController, Dispatcher, NodeCapacity, SessionManager, SessionStore, TaskStore, WorkloadCatalog}
  alias Embervm.MultiBrickSim.Fleet
  alias Embervm.OpLog.SQLite
  alias Embervm.Scheduler.Inflight
  alias Embervm.Node.V1.{GuestResponse, SessionAssignResponse, SessionInterruptResponse, UsageStats}

  @overhead 64
  @floor 512
  @prefix "embervm-sim-brick-"
  @up_threshold 3
  @up_window_ms 60_000
  @up_cooldown_ms 60_000
  @hold_ms Inflight.default_hold_ms()
  # An unsettled row keeps at least this much of its TTL in any run here, so a
  # row expiring later than now + this is a claim, not a settle hold.
  @unsettled_floor_ms 30_000
  @max_elapsed_ms 120_000

  @class_budgets %{"2gi" => 1_792, "4gi" => 3_840, "8gi" => 7_936}

  @session_workloads %{"claude-runtime" => 3_072, "sess-128" => 128, "sess-512" => 512, "sess-1g" => 1_024}
  @task_sizes [128, 512, 1_024]

  defstruct [
    :seed,
    :rng,
    :fleet,
    :ctx,
    :config,
    step: 0,
    op: nil,
    log: [],
    creates: %{},
    sessions: %{},
    tasks: %{},
    next_n: 0,
    seen: %{},
    expect: [],
    events: [],
    notes: [],
    results: [],
    lost: %{tickets: [], assigns: []},
    landed: %{},
    violations: [],
    acted: %{},
    registrations: [],
    elapsed_ms: 0,
    brick_seq: 0,
    stats: %{}
  ]

  # -- lifecycle --------------------------------------------------------------

  @doc """
  Builds one simulated control plane. Options:

    * `:seed` (required) - reported in every failure.
    * `:bricks` - `[{class, overrides}]` registered and reported at start.
    * `:class_max` - `%{class => max replicas}` for the BrickController.
    * `:retry` - `EMBERVM_PLACEMENT_RETRY` for this run (default true).
    * `:debit` - false runs the CP with no Inflight table (no pending debit).
    * `:brick_defaults` - fields every brick takes (`model`, `ledger`, ...).
    * `:report_weight` - how often a random step is a single-brick report.
    * `:on_violation` - `:raise` (default) or `:collect`.
  """
  def new(opts) do
    seed = Keyword.fetch!(opts, :seed)
    suffix = System.unique_integer([:positive])
    harness = self()
    prior_retry = System.get_env("EMBERVM_PLACEMENT_RETRY")

    if Keyword.get(opts, :retry, true),
      do: System.put_env("EMBERVM_PLACEMENT_RETRY", "1"),
      else: System.delete_env("EMBERVM_PLACEMENT_RETRY")

    cap_table = NodeCapacity.create(:"msim_cap_#{suffix}")
    cat_table = WorkloadCatalog.create(:"msim_cat_#{suffix}")
    debit? = Keyword.get(opts, :debit, true)

    # A missing table makes every Inflight read 0 and every claim a no-op: the
    # control plane with its pending debit switched off.
    inflight_table =
      if debit?, do: Inflight.create(:"msim_inflight_#{suffix}"), else: :"msim_no_inflight_#{suffix}"

    # Fixed names: Score.order breaks ties on a hash of the workload name, so a
    # per-run name would make the same seed place differently.
    task_workloads = Map.new(@task_sizes, &{"msim-task-#{&1}", &1})
    workloads = Map.merge(@session_workloads, task_workloads)

    for {wl, mib} <- @session_workloads, do: WorkloadCatalog.upsert(cat_table, wl, session_entry(wl, mib))

    # TaskStore reads retry config from the APPLICATION catalog, and its
    # full-jitter backoff is the only wall-clock timer a capacity failure arms.
    # A ~23 day backoff keeps a refused task parked until the harness retries
    # it, so no timer can fire inside a run.
    if :ets.whereis(WorkloadCatalog.table()) == :undefined, do: WorkloadCatalog.create()

    for {wl, mib} <- task_workloads do
      WorkloadCatalog.upsert(cat_table, wl, task_entry(wl, mib))
      WorkloadCatalog.upsert(wl, task_entry(wl, mib))
    end

    {:ok, fleet} = Fleet.start_link(harness: harness, cap_table: cap_table, workloads: workloads)
    {:ok, callers} = Task.Supervisor.start_link()

    path = Path.join(System.tmp_dir!(), "embervm_msim_#{suffix}.db")
    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    {:ok, store} =
      SessionStore.start_link(
        name: nil,
        op_log: op_log,
        op_log_mod: SQLite,
        async_writer: writer,
        async_lifecycle_writes: false
      )

    registry = :"msim_reg_#{suffix}"
    {:ok, registry_pid} = Registry.start_link(keys: :unique, name: registry)
    {:ok, sup} = DynamicSupervisor.start_link(strategy: :one_for_one)

    disp_name = :"msim_disp_#{suffix}"

    {:ok, task_store} =
      TaskStore.start_link(name: nil, op_log: op_log, on_queued: fn t -> Dispatcher.enqueue(disp_name, t) end)

    # Seed the store's jitter so the (never firing) backoff draws replay too.
    :sys.replace_state(task_store, fn s ->
      :rand.seed(:exsss, {seed, 17, 29})
      s
    end)

    {:ok, disp} =
      Dispatcher.start_link(
        name: disp_name,
        task_store: task_store,
        capacity_table: cap_table,
        catalog_table: cat_table,
        depth_table: :"msim_depth_#{suffix}",
        inflight_table: inflight_table,
        clock: fn -> 1_000_000 end,
        channel_fun: fn dial -> {:ok, {:sim, :task, dial}} end,
        invalidate_fun: fn _dial, _channel -> :ok end,
        note_denial_fun: fn wl, need -> send(harness, {:sim_note, :task, :denial, wl, need}) end,
        assign_fun: fn ch, req -> Fleet.assign(fleet, ch, req) end,
        prime_fun: fn ch, req -> Fleet.prime(fleet, ch, req) end,
        start_sweep: false
      )

    session_channel = fn dial -> {:ok, {:sim, :session, dial}} end

    session_opts = [
      channel_fun: session_channel,
      assign_fun: fn _ch, req ->
        {:ok,
         %SessionAssignResponse{
           response: %GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
           usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1},
           suspect: false
         }}
      end,
      interrupt_fun: fn _ch, _req ->
        {:ok, %SessionInterruptResponse{terminal_reason: "user_interrupt", killed: false, timeout: false}}
      end,
      destroy_fun: fn ch, vm_id -> Fleet.destroy(fleet, ch, vm_id) end,
      destroy_exact_fun: fn _ch, _req -> {:error, :unsupported} end,
      invalidate_fun: fn _node, _ch -> :ok end,
      brick_status_fun: fn _node ->
        %{health: :healthy, draining: false, registered: true, tombstoned: false, pod_uid: "sim"}
      end,
      drain_bank_retry_ms: 10,
      drain_flush_ms: 60_000,
      clock: fn -> 5_000_000 end
    ]

    {:ok, mgr} =
      SessionManager.start_link(
        name: nil,
        session_store: store,
        node_inventory_fun: fn -> {:error, :not_configured} end,
        dispatcher: disp_name,
        supervisor: sup,
        registry: registry,
        capacity_table: cap_table,
        catalog_table: cat_table,
        clock: fn -> 5_000_000 end,
        monotonic_clock: fn -> -800_000 end,
        channel_fun: session_channel,
        invalidate_channel_fun: fn _node, _channel -> :ok end,
        claim_fun: fn _dispatcher, _node, _workload -> :miss end,
        prime_fun: fn ch, req -> Fleet.prime(fleet, ch, req) end,
        bank_fun: fn ch, req -> Fleet.bank(fleet, ch, req) end,
        relight_fun: fn _ch, _req -> {:error, :not_simulated} end,
        evict_fun: fn _ch, _req -> {:ok, %{}} end,
        evict_artifact_fun: fn _ch, _req -> {:ok, %{}} end,
        restore_artifact_fun: fn _ch, _req -> {:error, %GRPC.RPCError{status: 5}} end,
        archive_volume_fun: fn _ch, _req -> {:ok, %{skipped: false}} end,
        export_artifact_fun: fn _ch, _req -> {:ok, %{content_fingerprint: "sim"}} end,
        retire_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        delete_session_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        expected_instances_fun: fn -> %{} end,
        session_opts: session_opts,
        async_writer: writer,
        async_lifecycle_writes: false,
        op_log: op_log,
        op_log_mod: SQLite,
        tenant: "homelab",
        inflight_table: inflight_table,
        note_denial_fun: fn wl, need -> send(harness, {:sim_note, :session, :denial, wl, need}) end
      )

    # A capacity miss at placement time casts to the application's
    # BrickController from inside the manager (Scheduler.place_and_claim/2,
    # not the note_denial_fun seam), so the manager's sends are traced to see it.
    :erlang.trace(mgr, true, [:send])

    class_max = Keyword.get(opts, :class_max, %{"2gi" => 3, "4gi" => 3, "8gi" => 2})
    initial = Keyword.get(opts, :bricks, [{"4gi", %{}}, {"8gi", %{}}])
    counts = Enum.frequencies_by(initial, &elem(&1, 0))
    {:ok, deployments} = Agent.start_link(fn -> Map.new(class_max, fn {c, _} -> {c, Map.get(counts, c, 0)} end) end)
    clock = :atomics.new(1, signed: true)
    :atomics.put(clock, 1, 1_000_000)

    classes =
      for {name, max} <- Enum.sort_by(class_max, fn {c, _} -> Map.fetch!(@class_budgets, c) end) do
        %{
          name: name,
          desired: Map.get(counts, name, 0),
          min: 0,
          max: max,
          usable_mib: Map.fetch!(@class_budgets, name),
          mem_reject_floor_mib: @floor,
          vm_overhead_mib: @overhead
        }
      end

    {:ok, bc} =
      BrickController.start_link(
        name: nil,
        mode: :up,
        classes: classes,
        namespace: "embervm",
        deployment_prefix: @prefix,
        interval_ms: 3_600_000,
        reconcile_on_start: false,
        scale_fun: fn _ns, @prefix <> class, replicas ->
          Agent.update(deployments, &Map.put(&1, class, replicas))
          :ok
        end,
        scale_get_fun: fn _ns, @prefix <> class -> {:ok, Agent.get(deployments, &Map.get(&1, class, 0))} end,
        registered_fun: fn ->
          fleet
          |> Fleet.snapshot()
          |> Map.fetch!(:bricks)
          |> Map.values()
          |> Enum.filter(&(&1.status in [:up, :draining]))
          |> Enum.frequencies_by(& &1.class)
        end,
        facts_fun: fn -> NodeCapacity.all(cap_table) end,
        catalog_fun: fn -> [] end,
        condition_fun: fn _workload, _condition -> :ok end,
        clock: fn -> :atomics.get(clock, 1) end,
        up_threshold: @up_threshold,
        up_window_ms: @up_window_ms,
        up_cooldown_ms: @up_cooldown_ms
      )

    sim = %__MODULE__{
      seed: seed,
      rng: :rand.seed_s(:exsss, {seed, 101, 7}),
      fleet: fleet,
      config: %{
        on_violation: Keyword.get(opts, :on_violation, :raise),
        debit: debit?,
        retry: Keyword.get(opts, :retry, true),
        class_max: class_max,
        brick_defaults: Keyword.get(opts, :brick_defaults, %{}),
        report_weight: Keyword.get(opts, :report_weight, 12),
        workloads: workloads,
        task_workloads: task_workloads,
        prior_retry: prior_retry
      },
      ctx: %{
        callers: callers,
        mgr: mgr,
        disp: disp,
        disp_name: disp_name,
        task_store: task_store,
        store: store,
        writer: writer,
        op_log: op_log,
        path: path,
        registry_pid: registry_pid,
        sup: sup,
        bc: bc,
        deployments: deployments,
        clock: clock,
        cap_table: cap_table,
        cat_table: cat_table,
        inflight_table: inflight_table
      }
    }

    sim = Enum.reduce(initial, sim, fn {class, overrides}, acc -> register_brick(acc, class, overrides) end)
    :ok = BrickController.reconcile_now(bc)
    %{sim | acted: Agent.get(deployments, & &1)}
  end

  @doc "Tears the stack down, including CP workers still blocked in a fake RPC."
  def stop(%__MODULE__{} = sim) do
    # Managers first, so a worker still blocked in a fake RPC is never finished
    # as a crash (which would audit a denial into the application's Metering).
    for pid <-
          [sim.ctx.callers, sim.ctx.mgr, sim.ctx.disp, sim.ctx.task_store, sim.ctx.bc, sim.ctx.store, sim.ctx.writer] ++
            [sim.ctx.sup, sim.ctx.registry_pid, sim.ctx.op_log, sim.ctx.deployments] do
      Embervm.TestProcess.stop_safely(pid)
    end

    _ = safe(fn -> Fleet.kill_waiters(sim.fleet) end)
    Embervm.TestProcess.stop_safely(sim.fleet)

    for table <- [sim.ctx.cap_table, sim.ctx.cat_table, sim.ctx.inflight_table] do
      if :ets.whereis(table) != :undefined, do: :ets.delete(table)
    end

    for {wl, _mib} <- sim.config.task_workloads, do: WorkloadCatalog.drop(wl)
    File.rm_rf(sim.ctx.path)

    case sim.config.prior_retry do
      nil -> System.delete_env("EMBERVM_PLACEMENT_RETRY")
      value -> System.put_env("EMBERVM_PLACEMENT_RETRY", value)
    end

    flush_mailbox()
    :ok
  end

  @doc "Runs `fun` on a fresh simulation for `seed` and always tears it down."
  def with_sim(opts, fun) do
    sim = new(opts)

    try do
      result = fun.(sim)
      if System.get_env("EMBERVM_SIM_VERBOSE") && match?(%__MODULE__{}, result), do: IO.puts(summary(result))
      result
    after
      stop(sim)
    end
  end

  @doc "The run's step log and counters, for EMBERVM_SIM_VERBOSE=1."
  def summary(sim) do
    "seed #{sim.seed}: #{inspect(sim.stats)}\n  " <> (sim.log |> Enum.reverse() |> Enum.join("\n  "))
  end

  # -- steps --------------------------------------------------------------------

  @doc "Performs one operation, settles, then checks every invariant."
  def step(%__MODULE__{} = sim, op) do
    sim = %{sim | step: sim.step + 1, op: op, events: [], notes: [], results: [], lost: %{tickets: [], assigns: []}}
    {sim, detail} = perform(sim, op)
    sim = settle(sim)
    sim = forward_demand(sim)
    kind = if is_tuple(op), do: elem(op, 0), else: op
    sim = %{sim | log: [describe(sim, op, detail) | sim.log], stats: bump(sim.stats, {:op, kind}, 1)}
    check(sim)
  end

  def run(sim, ops), do: Enum.reduce(ops, sim, &step(&2, &1))

  @doc "One seeded random operation, chosen from what the current state allows."
  def random_step(sim) do
    {op, sim} = random_op(sim)
    step(sim, op)
  end

  @doc """
  Lands every boot, completes every task, lets settle holds lapse and checks
  that nothing is left behind (`ClaimsDrain`, `TicketsEventuallyReleased`).
  """
  def quiesce(sim, budget \\ 400) do
    fs = Fleet.snapshot(sim.fleet)

    cond do
      budget == 0 ->
        record(sim, [{:quiescence_not_reached, map_size(fs.tickets), map_size(fs.assigns)}])

      fs.tickets != %{} ->
        id = fs.tickets |> Map.keys() |> Enum.min()
        sim |> step({:land, id}) |> quiesce(budget - 1)

      fs.assigns != %{} ->
        vm = fs.assigns |> Map.keys() |> Enum.min_by(&vm_seq/1)
        sim |> step({:task_complete, vm}) |> quiesce(budget - 1)

      true ->
        sim = step(sim, {:advance, @hold_ms + 1_000})
        fs = Fleet.snapshot(sim.fleet)

        leftovers =
          [
            (sim.config.debit and Inflight.entries(table: sim.ctx.inflight_table) != []) &&
              {:claims_drain, Inflight.entries(table: sim.ctx.inflight_table)},
            fs.tickets != %{} && {:tickets_released, fs.tickets},
            sim.creates != %{} && {:creates_answered, Map.values(sim.creates)}
          ]
          |> Enum.filter(& &1)

        record(sim, leftovers)
    end
  end

  # -- operations -----------------------------------------------------------------

  defp perform(sim, {:create, wl}) do
    n = sim.next_n
    mgr = sim.ctx.mgr
    principal = "p#{n}"
    # Unlinked, so a run torn down mid-create never takes the test process along.
    task = Task.Supervisor.async_nolink(sim.ctx.callers, fn -> SessionManager.create(mgr, wl, principal) end)
    need = Map.fetch!(sim.config.workloads, wl)
    create = %{n: n, wl: wl, need: need, task: task, step: sim.step}
    {%{sim | next_n: n + 1, creates: Map.put(sim.creates, task.ref, create)}, "##{n} #{wl}"}
  end

  defp perform(sim, {:land, id}) do
    {resident, sim} = rand_range(sim, 16, 64)
    {:ok, landed} = Fleet.land(sim.fleet, id, resident)
    sim = %{sim | landed: Map.update(sim.landed, landed.dial, 1, &(&1 + 1))}
    {sim, "ticket #{id} -> #{landed.vm_id} on #{landed.dial} (#{landed.lane})"}
  end

  defp perform(sim, {:report, dial}) do
    :ok = Fleet.report(sim.fleet, dial)
    {sim, dial}
  end

  defp perform(sim, :report_all) do
    for dial <- live_dials(sim), do: Fleet.report(sim.fleet, dial)
    {sim, nil}
  end

  defp perform(sim, {:advance, ms}), do: perform(sim, {:advance, ms, {10, 60}})

  defp perform(sim, {:advance, ms, {lo, hi}}) do
    # Virtual time: every Inflight row (unsettled TTLs and settle holds alike)
    # is that much closer to expiry, guests fault in another lo..hi percent of
    # their declared RAM, and the BrickController's clock moves on.
    if sim.config.debit do
      table = sim.ctx.inflight_table
      for {key, mib, exp} <- :ets.tab2list(table), do: :ets.insert(table, {key, mib, exp - ms})
    end

    :atomics.add(sim.ctx.clock, 1, ms)
    fs = Fleet.snapshot(sim.fleet)

    {updates, sim} =
      fs.bricks
      |> Enum.sort_by(&elem(&1, 0))
      |> Enum.flat_map(fn {dial, b} -> b.vms |> Enum.sort_by(&vm_seq(elem(&1, 0))) |> Enum.map(&{dial, &1}) end)
      |> Enum.reduce({[], sim}, fn {dial, {vm_id, vm}}, {acc, s} ->
        {pct, s} = rand_range(s, lo, hi)
        {[{dial, vm_id, min(vm.declared, vm.resident + div(vm.declared * pct, 100))} | acc], s}
      end)

    :ok = Fleet.set_resident(sim.fleet, updates)
    {%{sim | elapsed_ms: sim.elapsed_ms + ms}, "#{ms} ms"}
  end

  defp perform(sim, {:bank, n}) do
    s = Map.fetch!(sim.sessions, n)

    case SessionManager.bank(sim.ctx.mgr, s.sid) do
      :ok ->
        sim = %{sim | expect: [{:bank, s.vm} | sim.expect]}
        {put_in(sim.sessions[n].state, :banked), "session ##{n} :ok"}

      other ->
        {sim, "session ##{n} #{inspect(other)}"}
    end
  end

  defp perform(sim, {:destroy, n}) do
    s = Map.fetch!(sim.sessions, n)

    case SessionManager.destroy(sim.ctx.mgr, s.sid) do
      {:ok, :destroying} ->
        sim = %{sim | expect: [{:destroy, s.vm} | sim.expect]}
        {put_in(sim.sessions[n].state, :destroyed), "session ##{n} :destroying"}

      other ->
        {sim, "session ##{n} #{inspect(other)}"}
    end
  end

  defp perform(sim, {:task_submit, wl}) do
    n = sim.next_n

    {:ok, :created, tid} =
      TaskStore.submit(sim.ctx.task_store, %{
        tenant: "homelab",
        principal: "tp#{n}",
        workload: wl,
        request: %{path: "/", headers: %{}, body_b64: Base.encode64("")}
      })

    task = %{n: n, tid: tid, wl: wl, need: Map.fetch!(sim.config.workloads, wl)}
    {%{sim | next_n: n + 1, tasks: Map.put(sim.tasks, n, task)}, "task ##{n} #{wl}"}
  end

  defp perform(sim, {:task_complete, vm}) do
    :ok = Fleet.complete(sim.fleet, vm)
    {sim, vm}
  end

  defp perform(sim, {:task_retry, n}) do
    t = Map.fetch!(sim.tasks, n)
    result = TaskStore.retry(sim.ctx.task_store, t.tid)
    {sim, "task ##{n} #{inspect(elem(result, 0))}"}
  end

  defp perform(sim, :tick) do
    bc = sim.ctx.bc
    before = Agent.get(sim.ctx.deployments, & &1)
    flagged = BrickController.flagged(bc)
    last_up = :sys.get_state(bc).last_up_at
    :ok = BrickController.reconcile_now(bc)
    after_counts = Agent.get(sim.ctx.deployments, & &1)
    window = :sys.get_state(bc).denials
    now = :atomics.get(sim.ctx.clock, 1)

    violations =
      for {class, b} <- Enum.sort(before),
          a = Map.fetch!(after_counts, class),
          pressure = length(Map.get(window, class, [])) >= @up_threshold,
          cooled = cooldown_ok?(last_up, class, now),
          expected =
            if(
              pressure and cooled and b < Map.fetch!(sim.config.class_max, class) and
                not MapSet.member?(flagged, class),
              do: b + 1,
              else: b
            ),
          a != expected do
        {:scale_decision, class, %{before: b, after: a, expected: expected, window: length(Map.get(window, class, []))}}
      end

    grown =
      for {class, b} <- Enum.sort(before), a = Map.fetch!(after_counts, class), a > b, _ <- 1..(a - b), do: class

    sim = %{
      sim
      | acted: after_counts,
        registrations: sim.registrations ++ grown,
        stats: bump(sim.stats, :scale_ups, length(grown))
    }

    sim = record(sim, violations)
    {sim, "scaled #{inspect(grown)}"}
  end

  defp perform(sim, {:register, class}) do
    sim = %{sim | registrations: List.delete(sim.registrations, class)}
    sim = register_brick(sim, class, %{})
    {sim, "#{class} as #{sim.brick_seq}"}
  end

  defp perform(sim, {:drain, dial}) do
    :ok = Fleet.drain(sim.fleet, dial)
    {sim, dial}
  end

  defp perform(sim, {:vanish, dial}) do
    brick = Map.fetch!(Fleet.snapshot(sim.fleet).bricks, dial)
    lost = Fleet.vanish(sim.fleet, dial)

    # The registry forgets a brick that stopped reporting, and with it the
    # brick's in-flight debits (NodeRegistry.drop_inflight/2). Kubernetes
    # replaces the pod, so a fresh brick of the class registers later.
    if sim.config.debit, do: Inflight.drop(dial, table: sim.ctx.inflight_table)

    sessions =
      Map.new(sim.sessions, fn {n, s} ->
        if s.dial == dial and s.state == :running, do: {n, %{s | state: :lost}}, else: {n, s}
      end)

    sim = %{sim | lost: lost, sessions: sessions, registrations: sim.registrations ++ [brick.class]}
    {sim, "#{dial}: #{length(lost.tickets)} boots, #{length(lost.assigns)} tasks, #{length(lost.vms)} vms lost"}
  end

  defp register_brick(sim, class, overrides) do
    seq = sim.brick_seq + 1
    budget = Map.fetch!(@class_budgets, class)
    node_id = "sim-node-#{seq}"
    pod_uid = "pod-#{class}-#{seq}"

    spec =
      %{
        dial: "#{node_id}/#{pod_uid}",
        node_id: node_id,
        pod_uid: pod_uid,
        class: class,
        budget: budget,
        overhead: @overhead,
        floor: @floor,
        slot_ceiling: div(budget, 384)
      }
      |> Map.merge(sim.config.brick_defaults)
      |> Map.merge(overrides)

    Fleet.register(sim.fleet, spec)
    %{sim | brick_seq: seq}
  end

  # -- settle -------------------------------------------------------------------

  # Wait, on messages, until nothing the step woke can still move without the
  # harness. A round syncs every manager, then drains what arrived. Quiescence
  # must hold on two consecutive rounds, the second delivering no message and
  # observing the same state: a worker's exit can arrive AFTER the sync, while
  # its manager is still mid-way through the done message it sent, and the
  # second round's sync waits that handler out. The bounded receive is a
  # liveness poll for a wake-up that carries no message here; the outcome never
  # depends on its length.
  defp settle(sim, deadline \\ nil, prior \\ nil) do
    deadline = deadline || System.monotonic_time(:millisecond) + 20_000
    sync(sim)
    {sim, delivered} = drain_mailbox(sim, 0)

    case quiescent(sim) do
      :ok ->
        seen = fingerprint(sim)
        if delivered == 0 and seen == prior, do: sim, else: settle(sim, deadline, seen)

      {:waiting, why} ->
        if System.monotonic_time(:millisecond) > deadline do
          flunk(failure(sim, "step never settled: #{inspect(why)}", []))
        end

        receive do
          msg -> settle(handle(sim, msg), deadline)
        after
          20 -> settle(%{sim | stats: bump(sim.stats, :settle_polls, 1)}, deadline)
        end
    end
  end

  defp fingerprint(sim) do
    fs = Fleet.snapshot(sim.fleet)
    rows = if sim.config.debit, do: Inflight.entries(table: sim.ctx.inflight_table), else: []

    {Map.keys(fs.tickets), Map.keys(fs.assigns), map_size(sim.creates), task_states(sim),
     Enum.map(rows, fn {dial, ref, _mib, _exp} -> {dial, ref} end) |> Enum.sort()}
  end

  defp sync(sim) do
    _ = :sys.get_state(sim.ctx.mgr)
    _ = :sys.get_state(sim.ctx.disp)
    _ = :sys.get_state(sim.ctx.task_store)
    ref = :erlang.trace_delivered(sim.ctx.mgr)

    receive do
      {:trace_delivered, _, ^ref} -> :ok
    end
  end

  # Trace messages for the manager's ordinary sends are noise, not progress.
  defp drain_mailbox(sim, delivered) do
    receive do
      {:trace, _, _, _, _} = msg -> sim |> handle(msg) |> drain_mailbox(delivered + traced_demand(msg))
      msg -> sim |> handle(msg) |> drain_mailbox(delivered + 1)
    after
      0 -> {sim, delivered}
    end
  end

  defp traced_demand({:trace, _, _, {:"$gen_cast", {tag, _, _}}, _}) when tag in [:denial, :empty_demand], do: 1
  defp traced_demand(_), do: 0

  defp handle(sim, {:sim_rpc, event}) do
    sim =
      if Map.has_key?(sim.seen, event.pid) do
        sim
      else
        Process.monitor(event.pid)
        %{sim | seen: Map.put(sim.seen, event.pid, :alive)}
      end

    expect =
      case event do
        %{verb: verb, vm_id: vm, outcome: :ok} when verb in [:bank, :destroy] -> List.delete(sim.expect, {verb, vm})
        _ -> sim.expect
      end

    %{sim | events: [event | sim.events], expect: expect}
  end

  defp handle(%{creates: creates} = sim, {ref, result}) when is_reference(ref) and is_map_key(creates, ref) do
    Process.demonitor(ref, [:flush])
    {create, creates} = Map.pop(creates, ref)
    %{sim | creates: creates, results: [Map.put(create, :result, result) | sim.results]}
  end

  defp handle(%{creates: creates} = sim, {:DOWN, ref, :process, _pid, reason}) when is_map_key(creates, ref) do
    {create, creates} = Map.pop(creates, ref)
    %{sim | creates: creates, results: [Map.put(create, :result, {:caller_crashed, reason}) | sim.results]}
  end

  defp handle(%{seen: seen} = sim, {:DOWN, _ref, :process, pid, _reason}) when is_map_key(seen, pid) do
    %{sim | seen: Map.put(seen, pid, :down)}
  end

  defp handle(sim, {:sim_note, lane, kind, wl, need}) do
    %{sim | notes: [%{lane: lane, kind: kind, wl: wl, need: need} | sim.notes]}
  end

  defp handle(sim, {:trace, _pid, kind, {:"$gen_cast", {tag, wl, need}}, _to})
       when kind in [:send, :send_to_non_existing_process] and tag in [:denial, :empty_demand] do
    kind = if tag == :denial, do: :denial, else: :empty_demand
    %{sim | notes: [%{lane: :session, kind: kind, wl: wl, need: need} | sim.notes]}
  end

  defp handle(sim, _other), do: sim

  defp quiescent(sim) do
    fs = Fleet.snapshot(sim.fleet)
    blocked = MapSet.new(Enum.map(Map.values(fs.tickets), & &1.pid) ++ Enum.map(Map.values(fs.assigns), & &1.pid))
    running = Enum.filter(sim.seen, fn {pid, st} -> st == :alive and not MapSet.member?(blocked, pid) end)
    session_boots = Enum.count(fs.tickets, fn {_id, t} -> t.lane == :session end)
    task_boots = Enum.count(fs.tickets, fn {_id, t} -> t.lane == :task end)
    tasks_in_flight = Enum.count(sim.tasks, fn {_n, t} -> task_state(sim, t) in [:assigned, :running] end)

    cond do
      running != [] -> {:waiting, {:workers_running, length(running)}}
      map_size(sim.creates) != session_boots -> {:waiting, {:creates, map_size(sim.creates), session_boots}}
      tasks_in_flight != task_boots + map_size(fs.assigns) -> {:waiting, {:tasks, tasks_in_flight, task_boots}}
      sim.expect != [] -> {:waiting, {:rpcs, sim.expect}}
      true -> :ok
    end
  end

  defp task_states(sim) do
    for {n, t} <- Enum.sort(sim.tasks), do: {n, t.tid, task_state(sim, t)}
  end

  defp task_state(sim, t) do
    case TaskStore.get(sim.ctx.task_store, t.tid) do
      {:ok, %{state: state}} -> state
      _ -> :missing
    end
  end

  defp flush_mailbox do
    receive do
      {:sim_rpc, _} -> flush_mailbox()
      {:sim_note, _, _, _, _} -> flush_mailbox()
      {:sim_reply, _, _} -> flush_mailbox()
      {:trace, _, _, _, _} -> flush_mailbox()
      {:trace_delivered, _, _} -> flush_mailbox()
      {:DOWN, _, :process, _, _} -> flush_mailbox()
      {ref, _} when is_reference(ref) -> flush_mailbox()
    after
      0 -> :ok
    end
  end

  # -- demand ---------------------------------------------------------------------

  # Every demand note this step reached a different server in production (the
  # create refusal path and the dispatcher through their note_denial_fun seams,
  # the placement-time miss through the application's BrickController). They are
  # replayed, in arrival order, into this run's BrickController, and each
  # denial's attribution is checked against the documented rule.
  defp forward_demand(sim) do
    bc = sim.ctx.bc

    Enum.reduce(Enum.reverse(sim.notes), sim, fn note, acc ->
      case note.kind do
        :empty_demand ->
          BrickController.note_empty_demand(bc, note.wl, note.need)
          acc

        :denial ->
          before = :sys.get_state(bc).denials
          flagged = BrickController.flagged(bc)
          BrickController.note_denial(bc, note.wl, note.need)
          after_denials = :sys.get_state(bc).denials

          got =
            Enum.find(Map.keys(after_denials), fn class ->
              length(Map.get(after_denials, class, [])) > length(Map.get(before, class, []))
            end)

          expected = expected_class(acc, note.need, flagged)

          if got == expected,
            do: acc,
            else: record(acc, [{:demand_attribution, note.wl, note.need, %{expected: expected, got: got}}])
      end
    end)
  end

  @doc "The documented attribution rule, from the run's declared classes and acted counts."
  def expected_class(sim, need, flagged \\ MapSet.new()) do
    fitting =
      sim.config.class_max
      |> Map.keys()
      |> Enum.filter(&(Map.fetch!(@class_budgets, &1) >= need + max(@floor, @overhead)))
      |> Enum.sort_by(&Map.fetch!(@class_budgets, &1))

    case fitting do
      [] ->
        nil

      [smallest | _] ->
        Enum.find(fitting, smallest, fn class ->
          Map.get(sim.acted, class, 0) < Map.fetch!(sim.config.class_max, class) and
            not MapSet.member?(flagged, class)
        end)
    end
  end

  defp cooldown_ok?(stamps, class, now) do
    case Map.get(stamps, class) do
      nil -> true
      at -> now - at >= @up_cooldown_ms
    end
  end

  # -- invariants -----------------------------------------------------------------

  defp check(sim) do
    fs = Fleet.snapshot(sim.fleet)
    sim = track_results(sim, fs)

    violations =
      no_overcommit(fs) ++
        resident_within_budget(fs) ++
        create_outcomes(sim) ++
        demand_once(sim) ++
        refusals_rerouted(sim) ++
        task_outcomes(sim) ++
        no_leaked_claim(sim, fs) ++
        class_fit(sim, fs) ++
        session_bookkeeping(sim, fs) ++
        tickets_held(fs)

    record(sim, violations)
  end

  defp no_overcommit(fs) do
    for {dial, b} <- Enum.sort(fs.bricks),
        b.status != :gone,
        needs = Fleet.pending_needs(fs, dial),
        vms = Map.values(b.vms),
        committed =
          Enum.sum(Enum.map(vms, & &1.declared)) + Enum.sum(needs) + b.overhead * (length(vms) + length(needs)),
        committed > b.budget do
      {:no_overcommit, dial, %{committed: committed, budget: b.budget, live: length(vms), tickets: length(needs)}}
    end
  end

  defp resident_within_budget(fs) do
    for {dial, b} <- Enum.sort(fs.bricks),
        b.status != :gone,
        resident = b.vms |> Map.values() |> Enum.map(&(&1.resident + b.overhead)) |> Enum.sum(),
        resident > b.budget do
      {:resident_within_budget, dial, %{resident: resident, budget: b.budget}}
    end
  end

  # A create ends placed or as the retryable capacity denial (the router's 429).
  # The one exception is a boot in flight on a brick that vanished: its Prime
  # fails with the transport's UNAVAILABLE, which is not a capacity answer.
  defp create_outcomes(sim) do
    interrupted = Enum.count(sim.lost.tickets, &(&1.lane == :session))

    {lost_ok, others} =
      sim.results
      |> Enum.reject(&(outcome(&1) in [:placed, :no_capacity]))
      |> Enum.split_with(&match?({:error, {:denied, {:prime_failed, {:error, %GRPC.RPCError{status: 14}}}}}, &1.result))

    excess = if length(lost_ok) > interrupted, do: lost_ok, else: []

    for r <- others ++ excess do
      {:no_stranded_request, "create ##{r.n} #{r.wl}", r.result}
    end
  end

  # Each :no_capacity create records exactly one demand note: the placement-time
  # miss or the exhausted frontier, never both and never neither.
  defp demand_once(sim) do
    denied = Enum.count(sim.results, &(outcome(&1) == :no_capacity))
    notes = Enum.count(sim.notes, &(&1.lane == :session))

    if denied == notes,
      do: [],
      else: [{:demand_once, %{no_capacity_creates: denied, session_demand_notes: notes, notes: sim.notes}}]
  end

  # A refusal moves the boot to a brick its walk has not tried, or ends the walk
  # as a capacity outcome; it never repeats a brick or ends any other way. A
  # session create's exhausted walk is its :no_capacity (demand_once/1 counts
  # it); a task's is a :capacity failure that notes demand exactly once, so the
  # exhausted task walks of a step must each find their own task-lane note
  # (the rest of the notes are drain passes that hit a placement wall).
  defp refusals_rerouted(sim) do
    walks =
      sim.events
      |> Enum.reverse()
      |> Enum.filter(&(&1.verb == :prime))
      |> Enum.group_by(& &1.pid)
      |> Map.values()

    repeated =
      for attempts <- walks, dials = Enum.map(attempts, & &1.dial), Enum.uniq(dials) != dials do
        {:refusal_rerouted, :brick_retried, dials}
      end

    exhausted = Enum.filter(walks, fn attempts -> List.last(attempts).outcome == :refused end)
    {sessions, tasks} = Enum.split_with(exhausted, &(hd(&1).lane == :session))

    unanswered_sessions =
      if sessions != [] and not Enum.any?(sim.results, &(outcome(&1) == :no_capacity)),
        do: [{:refusal_rerouted, :refusal_not_answered, Enum.map(sessions, &Enum.map(&1, fn e -> e.dial end))}],
        else: []

    task_notes = sim.notes |> Enum.filter(&(&1.lane == :task)) |> Enum.map(& &1.wl)
    task_walks = Enum.map(tasks, &hd(&1).workload)

    unanswered_tasks =
      case task_walks -- task_notes do
        [] ->
          []

        missing ->
          [{:refusal_rerouted, :task_walk_without_demand, %{walks: task_walks, notes: task_notes, missing: missing}}]
      end

    repeated ++ unanswered_sessions ++ unanswered_tasks
  end

  # A task refused everywhere waits as failed_retryable; only a task whose brick
  # vanished under it may fail for another reason.
  defp task_outcomes(sim) do
    terminal =
      Enum.filter(sim.tasks, fn {_n, t} -> task_state(sim, t) in [:failed_permanent, :dead_lettered] end)

    interrupted = Map.get(sim.stats, :tasks_interrupted, 0)

    if length(terminal) > interrupted,
      do: [{:task_outcome, Enum.map(terminal, fn {n, t} -> {n, t.wl, task_state(sim, t)} end)}],
      else: []
  end

  defp no_leaked_claim(%{config: %{debit: false}}, _fs), do: []

  defp no_leaked_claim(sim, fs) do
    now = System.monotonic_time(:millisecond)
    rows = Inflight.entries(table: sim.ctx.inflight_table)
    {unsettled, held} = Enum.split_with(rows, fn {_dial, _ref, _mib, exp} -> exp > now + @unsettled_floor_ms end)
    claims = Enum.frequencies_by(unsettled, &elem(&1, 0))
    boots = Enum.frequencies_by(Map.values(fs.tickets), & &1.dial)

    leaked =
      if claims == boots,
        do: [],
        else: [
          {:no_leaked_claim, :unsettled, %{claims: claims, boots: boots, rows: unsettled, tasks: task_states(sim)}}
        ]

    stray =
      for {dial, ref, _mib, _exp} <- held, Map.get(sim.landed, dial, 0) == 0 do
        {:no_leaked_claim, :held_without_boot, {dial, ref}}
      end

    leaked ++ stray
  end

  defp class_fit(sim, fs) do
    rpcs =
      for %{verb: :prime, need: need, dial: dial} = e <- Enum.reverse(sim.events), is_integer(need) do
        brick = Map.get(fs.bricks, dial)

        cond do
          brick == nil -> {:class_fit, :boot_rpc_to_unknown_brick, dial}
          e.status != :up -> {:class_fit, :boot_rpc_to_undispatchable_brick, {dial, e.status, e.outcome}}
          not fits?(brick, need) -> {:class_fit, :boot_rpc_to_unfit_class, {dial, brick.class, need}}
          true -> nil
        end
      end

    vms =
      for {dial, b} <- fs.bricks, {vm_id, vm} <- b.vms, not fits?(b, vm.declared) do
        {:class_fit, :vm_on_unfit_class, {dial, b.class, vm_id, vm.declared}}
      end

    Enum.filter(rpcs, & &1) ++ vms
  end

  defp fits?(brick, need), do: brick.budget >= need + max(brick.floor, brick.overhead)

  defp session_bookkeeping(sim, fs) do
    dials = :sys.get_state(sim.ctx.mgr).session_dials

    for r <- sim.results, outcome(r) == :placed, sid = elem(r.result, 1).session_id do
      {:ok, row} = SessionStore.get(sim.ctx.store, sid)
      landed_on = Enum.find_value(fs.bricks, fn {dial, b} -> Map.has_key?(b.vms, row.vm_id) && dial end)
      brick = landed_on && Map.fetch!(fs.bricks, landed_on)

      cond do
        landed_on == nil -> {:session_bookkeeping, :vm_not_on_any_brick, {sid, row.vm_id}}
        Map.get(dials, sid) != landed_on -> {:session_bookkeeping, :dial, {sid, Map.get(dials, sid), landed_on}}
        row.node_id != brick.node_id -> {:session_bookkeeping, :node, {sid, row.node_id, brick.node_id}}
        true -> nil
      end
    end
    |> Enum.filter(& &1)
  end

  # TicketsBalanced, from the outside: a ticket is held only by a live boot.
  defp tickets_held(fs) do
    for {id, t} <- fs.tickets, not Process.alive?(t.pid), do: {:tickets_balanced, :orphan_ticket, {id, t.dial}}
  end

  defp outcome(%{result: {:ok, _}}), do: :placed
  defp outcome(%{result: {:error, {:denied, :no_capacity}}}), do: :no_capacity
  defp outcome(_), do: :other

  # Session rows, demand and refusal statistics for the run's summary.
  defp track_results(sim, fs) do
    sim =
      Enum.reduce(sim.results, sim, fn r, acc ->
        case r.result do
          {:ok, %{session_id: sid}} ->
            {:ok, row} = SessionStore.get(acc.ctx.store, sid)
            dial = Enum.find_value(fs.bricks, fn {dial, b} -> Map.has_key?(b.vms, row.vm_id) && dial end)
            s = %{n: r.n, sid: sid, wl: r.wl, need: r.need, vm: row.vm_id, dial: dial, state: :running}
            %{acc | sessions: Map.put(acc.sessions, r.n, s)}

          _ ->
            acc
        end
      end)

    refused = Enum.count(sim.events, &(&1.verb == :prime and &1.outcome == :refused))

    rerouted =
      sim.events
      |> Enum.filter(&(&1.verb == :prime))
      |> Enum.group_by(& &1.pid)
      |> Enum.count(fn {_pid, evs} ->
        Enum.any?(evs, &(&1.outcome == :refused)) and Enum.any?(evs, &(&1.outcome == :admitted))
      end)

    tasks_lost = length(sim.lost.tickets |> Enum.filter(&(&1.lane == :task))) + length(sim.lost.assigns)

    # A capacity denial while some dispatchable brick could, by noded's own
    # predicate, have admitted the create: the conservative double counts the
    # specs README names. Counted, not asserted.
    spurious =
      Enum.count(sim.results, fn r ->
        outcome(r) == :no_capacity and
          Enum.any?(fs.bricks, fn {dial, b} -> b.status == :up and Fleet.admittable(fs, dial) >= r.need end)
      end)

    stats =
      sim.stats
      |> bump(:placed, Enum.count(sim.results, &(outcome(&1) == :placed)))
      |> bump(:no_capacity, Enum.count(sim.results, &(outcome(&1) == :no_capacity)))
      |> bump(:refusals, refused)
      |> bump(:rerouted, rerouted)
      |> bump(:spurious_no_capacity, spurious)
      |> bump(:tasks_interrupted, tasks_lost)
      |> bump(:steps, 1)

    %{sim | stats: stats}
  end

  defp bump(stats, key, n), do: Map.update(stats, key, n, &(&1 + n))

  defp record(sim, []), do: sim

  defp record(sim, violations) do
    tagged = Enum.map(violations, &{sim.step, &1})

    case sim.config.on_violation do
      :raise -> flunk(failure(sim, "invariant violated", violations))
      :collect -> %{sim | violations: sim.violations ++ tagged}
    end
  end

  defp failure(sim, headline, violations) do
    trace = sim.log |> Enum.take(40) |> Enum.reverse() |> Enum.join("\n  ")

    """
    multi-brick sim seed=#{sim.seed} step=#{sim.step} op=#{inspect(sim.op)}: #{headline}
    #{Enum.map_join(violations, "\n", &("  " <> inspect(&1, limit: 50, printable_limit: 400)))}
    reproduce with EMBERVM_SIM_SEEDS=#{sim.seed}
    last steps:
      #{trace}
    """
  end

  defp describe(sim, op, detail) do
    refusals = Enum.count(sim.events, &(&1.outcome == :refused))
    results = sim.results |> Enum.sort_by(& &1.n) |> Enum.map(fn r -> "##{r.n}=#{short(r.result)}" end)
    base = "#{sim.step}. #{inspect(op)}"
    base = if detail, do: base <> " " <> to_string(detail), else: base
    base = if refusals > 0, do: base <> " refusals=#{refusals}", else: base
    if results == [], do: base, else: base <> " " <> Enum.join(results, " ")
  end

  defp short({:ok, _}), do: "ok"
  defp short({:error, {:denied, :no_capacity}}), do: "no_capacity"
  defp short(other), do: inspect(other, limit: 5)

  # -- random operations ----------------------------------------------------------

  defp random_op(sim) do
    fs = Fleet.snapshot(sim.fleet)
    up = live_dials(sim)
    tickets = fs.tickets |> Map.keys() |> Enum.sort()
    assigns = fs.assigns |> Map.keys() |> Enum.sort_by(&vm_seq/1)

    running =
      sim.sessions
      |> Enum.filter(fn {_n, s} -> s.state == :running end)
      |> Enum.map(&elem(&1, 0))
      |> Enum.sort()

    retryable =
      sim.tasks
      |> Enum.filter(fn {_n, t} -> task_state(sim, t) == :failed_retryable end)
      |> Enum.map(&elem(&1, 0))
      |> Enum.sort()

    choices =
      [
        {30, :create},
        {if(tickets != [], do: 24, else: 0), :land},
        {sim.config.report_weight, :report},
        {3, :report_all},
        # Capped so an unsettled claim (TTL 200 s) never lapses under a boot in flight.
        {if(sim.elapsed_ms < @max_elapsed_ms, do: 8, else: 0), :advance},
        {if(running != [], do: 5, else: 0), :bank},
        {if(running != [], do: 5, else: 0), :destroy},
        {12, :task_submit},
        {if(assigns != [], do: 10, else: 0), :task_complete},
        {if(retryable != [], do: 3, else: 0), :task_retry},
        {4, :tick},
        {if(sim.registrations != [], do: 5, else: 0), :register},
        {if(length(up) >= 2, do: 1, else: 0), :drain},
        {if(length(up) >= 2, do: 1, else: 0), :vanish}
      ]
      |> Enum.filter(fn {w, _} -> w > 0 end)

    {kind, sim} = weighted(sim, choices)

    case kind do
      :create ->
        {wl, sim} = pick(sim, @session_workloads |> Map.keys() |> Enum.sort())
        {{:create, wl}, sim}

      :land ->
        {id, sim} = pick(sim, tickets)
        {{:land, id}, sim}

      :report ->
        if up == [] do
          {:report_all, sim}
        else
          {dial, sim} = pick(sim, up)
          {{:report, dial}, sim}
        end

      :advance ->
        {ms, sim} = rand_range(sim, 500, 6_000)
        {{:advance, ms}, sim}

      :bank ->
        {n, sim} = pick(sim, running)
        {{:bank, n}, sim}

      :destroy ->
        {n, sim} = pick(sim, running)
        {{:destroy, n}, sim}

      :task_submit ->
        {wl, sim} = pick(sim, sim.config.task_workloads |> Map.keys() |> Enum.sort())
        {{:task_submit, wl}, sim}

      :task_complete ->
        {vm, sim} = pick(sim, assigns)
        {{:task_complete, vm}, sim}

      :task_retry ->
        {n, sim} = pick(sim, retryable)
        {{:task_retry, n}, sim}

      :report_all ->
        {:report_all, sim}

      :tick ->
        {:tick, sim}

      :register ->
        {{:register, hd(sim.registrations)}, sim}

      :drain ->
        {dial, sim} = pick(sim, up)
        {{:drain, dial}, sim}

      :vanish ->
        {dial, sim} = pick(sim, up)
        {{:vanish, dial}, sim}
    end
  end

  defp live_dials(sim) do
    Fleet.snapshot(sim.fleet).bricks
    |> Enum.filter(fn {_dial, b} -> b.status == :up end)
    |> Enum.map(&elem(&1, 0))
    |> Enum.sort()
  end

  defp weighted(sim, choices) do
    total = choices |> Enum.map(&elem(&1, 0)) |> Enum.sum()
    {r, rng} = :rand.uniform_s(total, sim.rng)

    {:hit, kind} =
      Enum.reduce_while(choices, r, fn {w, kind}, left ->
        if left <= w, do: {:halt, {:hit, kind}}, else: {:cont, left - w}
      end)

    {kind, %{sim | rng: rng}}
  end

  defp pick(sim, list) do
    {i, rng} = :rand.uniform_s(length(list), sim.rng)
    {Enum.at(list, i - 1), %{sim | rng: rng}}
  end

  defp rand_range(sim, lo, hi) do
    {i, rng} = :rand.uniform_s(hi - lo + 1, sim.rng)
    {lo + i - 1, %{sim | rng: rng}}
  end

  defp vm_seq("vm-" <> n), do: String.to_integer(n)

  # -- catalog --------------------------------------------------------------------

  defp session_entry(wl, mib) do
    %{
      name: wl,
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:sim",
      invoke_path: "/",
      timeout_ms: 90_000,
      cap: 10_000,
      floor: 0,
      mem_mib: mib,
      persistence: nil,
      session: %{
        idle_bank_seconds: 86_400,
        max_lifetime_seconds: 86_400,
        banked_ttl_seconds: 86_400,
        max_sessions: 10_000,
        invoke_queue_cap: 4
      }
    }
  end

  defp task_entry(wl, mib) do
    %{
      name: wl,
      namespace: "embervm",
      class: "task",
      cap: 10_000,
      floor: 0,
      mem_mib: mib,
      invoke_path: "/",
      timeout_ms: 600_000,
      result_ttl_ms: 60_000,
      result_max_bytes: 1_048_576,
      retry: %{
        max_attempts: 3,
        backoff_ms: 2_000_000_000,
        backoff_cap_ms: 2_000_000_000,
        retry_on: [:transport, :timeout, :guest5xx]
      },
      triggers: []
    }
  end

  defp safe(fun) do
    fun.()
  catch
    _, _ -> :error
  end
end
