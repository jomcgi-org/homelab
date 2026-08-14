defmodule Embervm.Dispatcher do
  @moduledoc """
  The heart of R0: turns a `queued` task into an `Assign` against a primed VM
  and drives it to a terminal state. Until this process runs, `submit` is
  async-only (tasks sit `queued`); with it, submit actually drains.

  ## what it owns (single hot-path writer)

  This one GenServer owns, in-process, everything the dispatch decision touches,
  so the decision is a sequence of O(1) reads/writes with no cross-process
  round-trip and no lock:

    * per-workload FAIR QUEUES: round-robin across principals, FIFO within a
      principal (`fair_queue.ex`-style structure inlined below);
    * the PRIMED-VM INVENTORY (`{node, workload} -> vm_ids`) the pool deposits
      into and dispatch pops from, which IS the destroy-on-assign accounting:
      a vm_id is single-use, removed the instant it is committed to an Assign,
      so there is no async lag against the daemon's `free_primed_slots`;
    * in-flight counters per workload and per principal, for the enforcement
      gates;
    * a public queue-depth counter table the submit path reads to 429.

  The capacity facts it gates against (`Embervm.NodeCapacity`) and the workload
  config (`Embervm.WorkloadCatalog`) are read O(1) from their `read_concurrency`
  ETS tables, never through a GenServer.

  ## the two dispatch paths

    * WARM (the budget path, p95 <= 25ms): a primed VM is in inventory; pop it,
      `assign`, done.
    * MISS (restore-bound, p95 <= 500ms): the inventory is empty but a node has
      a ready base and budget; a worker Primes-then-Assigns inline. It is
      counted separately in `stats/0`.

  ALL Prime/Assign I/O runs in `spawn_monitor` workers, never in this GenServer,
  so a 500ms miss-path restore never head-of-line-blocks warm dispatch, the next
  decision, or a capacity read. The GenServer only does the cheap decision and
  the (fast, local) FSM writes through `Embervm.TaskStore`; the heavy RPC is the
  worker's.

  ## enforcement (fail-closed)

    * per-workload `cap` on in-flight tasks;
    * per-principal SHARE cap: a values-configured fraction of `cap`, or
      `cap / active_principals` (min 1) when unset, so under contention no
      principal starves the others (an over-share principal is skipped in the
      rotation, not served);
    * per-principal QUEUE-DEPTH cap (default 10k), enforced as a synchronous 429
      pre-check at submit via `admit?/3` (a coarse abuse guard; the FSM has no
      queued->failed edge, so a queued task is never terminally dropped);
    * if capacity facts are MISSING or STALE (> 15s since the registry stamped
      them) or no node has a ready base, dispatch is denied with a DISTINCT kind
      (`:stale_capacity` / `:no_capacity` / `:cap` / `:principal_share`), tallied
      as metrics for the Task 16 latency/fairness gates. (The op-log `:denied`
      append is reserved for Task 12 quota, to avoid per-tick log spam.)

  Staleness is measured against the registry's own monotonic stamp
  (`facts.updated_at` is `System.monotonic_time(:millisecond)`), so this
  process's `clock` is monotonic too and the subtraction is apples-to-apples.

  ## weights are a parameter, not a redesign

  Round-robin is the weight-1 case of deficit round-robin: each principal carries
  an implicit weight of 1 today. A future weighted policy changes only the credit
  a principal accrues per rotation, not the queue structure (per-principal FIFO +
  a rotation deque), which is why the fairness spec calls weights a follow-on.

  ## how tasks arrive and leave

    * PUSH: `Embervm.TaskStore`'s `on_queued` hook casts `enqueue/1` on every
      transition INTO `queued` (submit-created, retry, redrive).
    * SWEEP: a boot + periodic reconcile over `TaskStore.list_backlog/1` re-drives
      what the push path cannot. A dropped `enqueue` cast or a restart that
      empties the in-memory queues is re-enqueued; `reassign_in_flight/0` on a
      downed node produces `failed_retryable` out of band and is retried. And an
      in-flight (`assigned`/`running`) task with NO live worker tracking it is an
      ORPHAN, a dispatcher restart dropped its workers, a partial assign/start
      commit spawned none, or a completion's terminal op-log append failed, so it
      is reclaimed by failing it as transport (at-least-once, through Retry). A
      task a live worker still owns is skipped, so a running task is never
      double-dispatched. This is what makes "no task lost" hold across restarts
      and partial commits, not just across dropped push signals.

  ## deferred, on purpose

  Live untruncated response handoff to a `?wait=true` caller is NOT built here:
  the sync waiter still wakes on the terminal transition and the router serves
  the stored (possibly truncated) result copy, exactly as before. Metering
  (`AssignResponse.usage`) is Task 12; OTel spans are Task 13; guest-image
  provisioning (which is what lets a real Assign succeed) is Task 14. In R0
  `EMBERVM_NODED_IMAGES` is empty, so live verification is limited to the
  dispatcher wiring up and correctly denying/queueing against the empty-capacity
  node, plus the fake-daemon drain in ExUnit; no real task runs end to end yet.
  """

  use GenServer
  require Logger
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{Brick, NodeCapacity, WorkloadCatalog}
  alias Embervm.PrimedOp
  alias Embervm.Scheduler
  alias Embervm.Scheduler.Request

  alias Embervm.Node.V1.{
    AssignRequest,
    AssignResponse,
    GuestRequest,
    GuestResponse,
    PrimeRequest,
    PrimeResponse,
    Trace
  }

  @stale_after_ms 15_000
  @queue_depth_cap 10_000
  @sweep_interval_ms 5_000
  @assign_watchdog_margin_ms 15_000
  @assign_watchdog_ms nil
  @depth_table :embervm_queue_depth
  # The per-principal queue-depth cap is stored as a distinguished row in the
  # depth table (an atom key, never colliding with the {workload, principal}
  # tuple keys) so admit?/3 reads BOTH the current depth and the cap from the one
  # table the router already has a handle to, with no app-env or GenServer round
  # trip. The dispatcher writes it on init from its configured cap.
  @cap_key :__queue_depth_cap__

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Enqueue a freshly-queued task into its workload's fair queue. A cast so
  `Embervm.TaskStore`'s transition never blocks on the dispatcher, and
  `whereis`-guarded so a store with no dispatcher wired (unit tests) is a no-op.
  This is the default `on_queued` hook target.
  """
  @spec enqueue(GenServer.server(), map()) :: :ok
  def enqueue(server \\ __MODULE__, %{} = task) do
    case GenServer.whereis(server) do
      nil -> :ok
      _ -> GenServer.cast(server, {:enqueue, task})
    end
  end

  @doc """
  Deposit a primed vm_id into the inventory for `{node_id, workload}` (the pool
  Primed it). A cast from `Embervm.PoolManager`; may unblock a queued task, so it
  triggers a drain.
  """
  @spec deposit(GenServer.server(), String.t(), String.t(), String.t()) :: :ok
  def deposit(server \\ __MODULE__, node_id, workload, vm_id) do
    GenServer.cast(server, {:deposit, node_id, workload, vm_id})
  end

  @doc """
  Whether `principal` may submit another task to `workload` without breaching its
  per-principal queue-depth cap. A read-only advisory check the router runs
  BEFORE the durable submit to 429; the actual reservation happens on `enqueue`,
  so a burst can momentarily overshoot the cap by the concurrent-submit window
  (an accepted coarseness for an abuse guard, not precise accounting). Reads the
  public depth-counter table directly (no GenServer round-trip), and fails closed
  (denies) when the table is not up yet.
  """
  @spec admit?(atom(), String.t(), String.t()) :: boolean()
  def admit?(depth_table \\ @depth_table, workload, principal) do
    if :ets.whereis(depth_table) == :undefined do
      false
    else
      current =
        case :ets.lookup(depth_table, {workload, principal}) do
          [{_key, n}] -> n
          [] -> 0
        end

      current < cap_from_table(depth_table)
    end
  end

  defp cap_from_table(depth_table) do
    case :ets.lookup(depth_table, @cap_key) do
      [{_key, cap}] -> cap
      [] -> @queue_depth_cap
    end
  end

  @doc """
  A snapshot of dispatch metrics + queue/inventory/in-flight state, for tests and
  operational visibility (WHY is nothing draining: no capacity, all stale, a
  principal capped, the workload at cap).
  """
  @spec stats(GenServer.server()) :: map()
  def stats(server \\ __MODULE__) do
    GenServer.call(server, :stats)
  end

  @doc """
  Atomically claims one primed vm_id from `{node_id, workload}` inventory for a
  session create (R2), removing it from the pool exactly as a warm dispatch does:
  the vm_id is single-use, so removing it the instant it is committed to a session
  is the same destroy-on-assign accounting a task claim gets. Returns
  `{:ok, vm_id}` on a hit, or `:miss` when no primed VM is parked for that
  `{node, workload}` (the caller then Primes one itself). Serialized through this
  GenServer so a session claim and a task dispatch can never pop the same VM.
  """
  @spec claim(GenServer.server(), String.t(), String.t()) :: {:ok, String.t()} | :miss
  def claim(server \\ __MODULE__, node_id, workload) do
    GenServer.call(server, {:claim, node_id, workload})
  end

  @doc """
  Runs one backlog reconcile synchronously (the same code the periodic sweep
  runs) and returns after it plus the drain it triggers complete. Tests drive
  recovery/reassign paths through this deterministically with `start_sweep:
  false`; in production the timer fires it every #{@sweep_interval_ms}ms.
  """
  @spec sweep(GenServer.server()) :: :ok
  def sweep(server \\ __MODULE__) do
    GenServer.call(server, :sweep)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    task_store = Keyword.get(opts, :task_store, Embervm.TaskStore)

    state = %{
      task_store: task_store,
      op_log: Keyword.get(opts, :op_log, nil),
      op_log_mod: Keyword.get(opts, :op_log_mod, nil),
      # `%Op{}` enforces :tenant, and the dispatcher otherwise has no concept of
      # one: priming is workload-scoped (PrimeRequest carries the workload, no
      # principal), so there is no per-task tenant to inherit here. Same default
      # as Embervm.Metering, which faces the same problem for its audit ops.
      tenant: Keyword.get(opts, :tenant, "homelab"),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      depth_table: Keyword.get(opts, :depth_table, @depth_table),
      # Monotonic, to compare against the registry's monotonic facts.updated_at.
      clock: Keyword.get(opts, :clock, &default_mono/0),
      # Wall clock, for result expires_at (a stored-result TTL is wall time).
      wall_clock: Keyword.get(opts, :wall_clock, &default_wall/0),
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      assign_fun: Keyword.get(opts, :assign_fun, &default_assign/2),
      prime_fun: Keyword.get(opts, :prime_fun, &default_prime/2),
      get_request_fun:
        Keyword.get(opts, :get_request_fun, fn tid -> Embervm.TaskStore.get_request(task_store, tid) end),
      stale_after_ms: Keyword.get(opts, :stale_after_ms, @stale_after_ms),
      queue_depth_cap: Keyword.get(opts, :queue_depth_cap, @queue_depth_cap),
      assign_watchdog_margin_ms:
        Keyword.get(opts, :assign_watchdog_margin_ms, @assign_watchdog_margin_ms),
      # Test-only seam: when set, IS the watchdog budget; when nil (production), budget is transport_timeout(entry.timeout_ms) + margin. Tests use this to avoid waiting out the 5s transport headroom floor.
      assign_watchdog_ms: Keyword.get(opts, :assign_watchdog_ms, @assign_watchdog_ms),
      share_fraction: Keyword.get(opts, :share_fraction, nil),
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, @sweep_interval_ms),
      # Quota enforcement (Task 12): the Metering quota-cache table and the
      # values-configured per-principal daily vCPU-second budgets. Read O(1) and
      # fail-closed (per principal WITH a budget) when the cache is unreadable, so
      # an over-budget principal is SKIPPED in the fair rotation (parked queued,
      # no queued->failed FSM edge needed), unparking at day rollover. Empty
      # budgets (the default) means no principal is ever skipped here.
      quota_table: Keyword.get(opts, :quota_table, Embervm.Metering.table()),
      quota_config: Keyword.get(opts, :quota_config, Embervm.Metering.quota_config()),
      # Dynamic state.
      queues: %{},
      queued_ids: MapSet.new(),
      inventory: %{},
      inflight_wl: %{},
      inflight_pr: %{},
      workers: %{},
      retry_timers: %{},
      denials: %{cap: 0, principal_share: 0, stale_capacity: 0, no_capacity: 0, quota: 0},
      warm_hits: 0,
      misses: 0,
      adoption_vm_ids: MapSet.new()
    }

    # Named + public so the router's admit?/3 reads it lock-free. Owned here so it
    # dies with this subtree; write_concurrency because it is the depth counter.
    # The cap row lets admit?/3 read the cap from the same table.
    create_depth_table(state.depth_table)
    :ets.insert(state.depth_table, {@cap_key, state.queue_depth_cap})

    if Keyword.get(opts, :start_sweep, true) do
      {:ok, state, {:continue, :boot_sweep}}
    else
      {:ok, state}
    end
  end

  @impl true
  def handle_continue(:boot_sweep, state) do
    state = run_sweep(state)
    # The CP incarnation id, NOT a fresh unique integer. This record marks the
    # boundary between incarnations, so it is only useful if it matches the
    # run_id stamped on every other record in the trace; an arbitrary number
    # correlates with nothing and makes the boundary unfindable. SpecTrace.run_id/0
    # is stable across writer restarts for the same reason.
    Embervm.SpecTrace.emit(:adoption, :restart_cp, %{
      "incarnation_id" => Embervm.SpecTrace.run_id()
    })
    schedule_sweep(state)
    {:noreply, state}
  end

  @impl true
  def handle_cast({:enqueue, %{task_id: tid, workload: wl, principal: pr}}, state) do
    {:noreply, state |> do_enqueue(tid, wl, pr) |> drain_workload(wl)}
  end

  def handle_cast({:deposit, node_id, wl, vm_id}, state) do
    {:noreply, state |> put_vm_if_unknown(node_id, wl, vm_id) |> drain_workload(wl)}
  end

  @impl true
  def handle_call(:stats, _from, state), do: {:reply, snapshot(state), state}

  def handle_call(:sweep, _from, state) do
    {:reply, :ok, run_sweep(state)}
  end

  def handle_call({:claim, node_id, wl}, _from, state) do
    case reserve_vm(state, node_id, wl, :warm) do
      {new_state, vm_id} when is_binary(vm_id) -> {:reply, {:ok, vm_id}, new_state}
      {new_state, nil} -> {:reply, :miss, new_state}
    end
  end

  @impl true
  def handle_info({:assign_done, pid, outcome}, state) do
    {:noreply, finish_worker(state, pid, outcome, :flush)}
  end

  # A server-enforced gRPC deadline cannot fire when an orphaned channel is
  # unreachable, so one wedged stream could consume an in-flight slot forever
  # and silently cap the workload. This wall-clock watchdog is the last-resort
  # recovery path for that case; the normal transport deadline gets first shot.
  def handle_info({:worker_timeout, pid, ref}, state) do
    case Map.get(state.workers, pid) do
      %{ref: ^ref} = meta ->
        elapsed_ms = System.monotonic_time(:millisecond) - meta.watchdog_started_at

        Logger.warning("assign worker watchdog fired (elapsed=#{elapsed_ms}ms, budget=#{meta.watchdog_ms}ms)",
          workload: meta.workload,
          task_id: meta.task_id,
          node_id: meta.node_id
        )

        Process.exit(pid, :kill)
        {:noreply, state}

      _ ->
        # The worker completed, or this is a stale timer message for a reused pid.
        {:noreply, state}
    end
  end

  # A miss worker reports the vm_id it just primed AND the instance it primed on.
  # Stamp BOTH into the worker's meta so:
  #   * known_vm_ids counts the vm_id immediately, closing the window where
  #     adopt_inventory would see the node reporting a just-primed miss VM as primed
  #     and re-adopt it (the VM is single-use and about to be assigned by this worker);
  #   * meta.node_id tracks the brick the VM ACTUALLY primed on (== the committed head
  #     with no retry, or the winning candidate after a cross-brick reject/retry), so
  #     any reconcile/adopt logic keys against the brick that really holds the VM, not
  #     the committed head. THIS is the node-reassignment update the ADR 014 decision 3
  #     retry requires (see prime_with_retry): the single source of truth for "which
  #     brick did this miss land on" after a retry. Ignored if the worker already
  #     finished (pid gone).
  def handle_info({:vm_primed, pid, vm_id, primed_node_id}, state) do
    case Map.get(state.workers, pid) do
      nil -> {:noreply, state}
      meta -> {:noreply, put_in(state.workers[pid], %{meta | vm_id: vm_id, node_id: primed_node_id})}
    end
  end

  # A worker died without reporting (it always sends {:assign_done, ...} in the
  # normal path, so a bare DOWN is an abnormal exit): treat as a transport
  # failure so the task retries at-least-once, exactly as a downed node would.
  def handle_info({:DOWN, _ref, :process, pid, reason}, state) do
    case Map.get(state.workers, pid) do
      nil -> {:noreply, state}
      meta ->
        if meta.mode == :miss do
          Embervm.SpecTrace.emit(:adoption, :abandon_claim, %{
            "task_id" => meta.task_id,
            "vm_id" => meta.vm_id
          })
        end

        {:noreply, finish_worker(state, pid, {:failed, :transport, {:worker_down, reason}, nil}, :no_flush)}
    end
  end

  # A backoff retry timer fired: move the task back to queued. TaskStore.retry
  # fires the on_queued hook, which casts enqueue/1 back to us, so the requeue
  # and drain happen on that ensuing message; nothing else to do here.
  def handle_info({:retry_task, task_id}, state) do
    state = %{state | retry_timers: Map.delete(state.retry_timers, task_id)}
    _ = safe(fn -> Embervm.TaskStore.retry(state.task_store, task_id) end)
    {:noreply, state}
  end

  def handle_info(:sweep, state) do
    state = run_sweep(state)
    schedule_sweep(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, state) do
    for {pid, _meta} <- state.workers, do: Process.exit(pid, :shutdown)
    :ok
  end

  # -- enqueue ---------------------------------------------------------------

  # Idempotent enqueue: dedupe on task_id so a sweep racing a live push cannot
  # double-count depth or double-queue a task. Only a first insertion charges the
  # per-principal depth counter (released on dispatch commit or terminal drop).
  defp do_enqueue(state, task_id, wl, pr) do
    if MapSet.member?(state.queued_ids, task_id) do
      state
    else
      :ets.update_counter(state.depth_table, {wl, pr}, {2, 1}, {{wl, pr}, 0})

      fq = state.queues |> Map.get(wl, new_fq()) |> fq_enqueue(pr, task_id)

      %{
        state
        | queues: Map.put(state.queues, wl, fq),
          queued_ids: MapSet.put(state.queued_ids, task_id)
      }
    end
  end

  # -- drain -----------------------------------------------------------------

  defp drain_all(state) do
    Enum.reduce(Map.keys(state.queues), state, fn wl, acc -> drain_workload(acc, wl) end)
  end

  # Dispatch as many tasks for `wl` as capacity, cap, and fairness allow, each in
  # its own worker. Every iteration re-reads capacity and re-checks the caps, so
  # the loop stops the moment the workload hits its cap, the pool empties, or the
  # only remaining principals are over their share.
  defp drain_workload(state, wl) do
    case WorkloadCatalog.fetch(state.catalog_table, wl) do
      :error ->
        # No catalog entry: cap/floor unknown, so we cannot dispatch (fail-closed).
        bump_denial(state, :no_capacity)

      {:ok, entry} ->
        drain_loop(state, wl, entry)
    end
  end

  defp drain_loop(state, wl, entry) do
    cond do
      fq_empty?(state.queues, wl) ->
        state

      inflight_count(state.inflight_wl, wl) >= entry.cap ->
        bump_denial(state, :cap)

      true ->
        case pick_node(state, wl, entry) do
          {:error, kind} ->
            bump_denial(state, kind)

          {:ok, node_id, mode, snapshot_ref, candidates} ->
            dispatch_one(state, wl, entry, node_id, mode, snapshot_ref, candidates)
        end
    end
  end

  # Try to commit exactly one task, then recurse to keep draining. A share-capped
  # or empty rotation stops the loop; a task that will not `assign` (a lost race)
  # is dropped and the loop continues.
  defp dispatch_one(state, wl, entry, node_id, mode, snapshot_ref, candidates) do
    share = share_cap(state, wl, entry)
    blocked = over_budget_principals(state, wl)

    case fq_take(Map.get(state.queues, wl), wl, share, state.inflight_pr, blocked) do
      {:none, fq} ->
        # Nothing servable right now: queue empty, or every remaining principal is
        # at its share or over its daily quota. Attribute to :quota when a quota
        # block contributed (a metric, not an op-log append: a queued task parked
        # for budget is re-evaluated every tick, so appending here would flood; the
        # audit append happens once at submit, see Embervm.Router), else to share.
        state = %{state | queues: Map.put(state.queues, wl, fq)}

        cond do
          fq_empty?(state.queues, wl) -> state
          MapSet.size(blocked) > 0 -> bump_denial(state, :quota)
          true -> bump_denial(state, :principal_share)
        end

      {:ok, task_id, pr, fq} ->
        state = %{state | queues: Map.put(state.queues, wl, fq)}
        commit(state, wl, entry, node_id, mode, snapshot_ref, candidates, task_id, pr)
    end
  end

  # Commit one task to a worker: reserve the VM (warm), drive queued->assigned->
  # running through the FSM, bump counters, spawn the assign worker, then recurse.
  #
  # First, TTL check (ADR embervm/002): a task popped past its `expires_at` is
  # expired to failed_permanent and NEVER dispatched (no VM reserved, no primed VM
  # consumed), then the loop continues draining. This runs BEFORE reserve_vm so an
  # expired task cannot burn a primed VM. Wall clock, to match the wall-time
  # `expires_at` the submit stamped.
  defp commit(state, wl, entry, node_id, mode, snapshot_ref, candidates, task_id, pr) do
    if expired?(state, task_id) do
      _ = safe(fn -> Embervm.TaskStore.expire(state.task_store, task_id) end)
      release_depth(state, wl, pr)
      state = Map.update!(state, :queued_ids, &MapSet.delete(&1, task_id))
      drain_loop(state, wl, entry)
    else
      commit_dispatch(state, wl, entry, node_id, mode, snapshot_ref, candidates, task_id, pr)
    end
  end

  # Whether the task's `expires_at` (wall-time ms, or nil for no TTL) has passed.
  # Reads the ETS hot set (the dispatch hot path never touches the durable store);
  # a task missing from ETS (a race) is treated as not-expired so the assign path's
  # own not_found handling decides its fate.
  defp expired?(state, task_id) do
    case safe_call(fn -> Embervm.TaskStore.get(state.task_store, task_id) end) do
      {:ok, {:ok, %{expires_at: exp}}} when is_integer(exp) -> state.wall_clock.() > exp
      _ -> false
    end
  end

  defp commit_dispatch(state, wl, entry, node_id, mode, snapshot_ref, candidates, task_id, pr) do
    {state, vm_id} = reserve_vm(state, node_id, wl, mode)

    # Pass the reserved vm_id into assign/start so that, under
    # EMBERVM_ASYNC_LIFECYCLE_WRITES (ADR embervm/014 decision 2), TaskStore
    # registers the deferred :assigned/:started appends against this VM with
    # Embervm.AsyncWriter; the reconciler's adopt-and-backfill discriminator then
    # sees the write in flight. nil for a miss (its vm_id is minted in the worker);
    # inert under the gate off. The ETS FSM advance and the queued-race guard here
    # are unchanged (Option A): only the durable append moves off the hot path.
    with {:ok, {:ok, _}} <- safe_call(fn -> Embervm.TaskStore.assign(state.task_store, task_id, vm_id, node_id) end),
         {:ok, {:ok, _}} <- safe_call(fn -> Embervm.TaskStore.start(state.task_store, task_id, vm_id, node_id) end) do
      # Task left the queue: release its depth reservation and dedupe slot.
      release_depth(state, wl, pr)

      state =
        state
        |> Map.update!(:queued_ids, &MapSet.delete(&1, task_id))
        |> inc_inflight(wl, pr)
        |> tally(mode)
        |> spawn_assign_worker(task_id, pr, wl, node_id, mode, vm_id, snapshot_ref, candidates, entry)

      drain_loop(state, wl, entry)
    else
      _ ->
        # assign/start refused (task not in queued state: a race, or it vanished).
        # Return the reserved VM to inventory and drop the task from our view;
        # keep draining the rest.
        state = return_vm(state, node_id, wl, mode, vm_id)
        release_depth(state, wl, pr)
        state = Map.update!(state, :queued_ids, &MapSet.delete(&1, task_id))
        drain_loop(state, wl, entry)
    end
  end

  # -- capacity selection (fail-closed) --------------------------------------

  # Choose a node + path for `wl`, or a denial kind. Prefers a WARM node (one with
  # a primed VM in inventory) over a MISS (prime-then-assign). Fail-closed:
  #
  #   * a node is a candidate only if its facts are FRESH (registry stamped them
  #     within stale_after_ms), not draining, and it has a READY base for wl;
  #   * no candidate but some node carries a base for wl that is merely STALE
  #     -> :stale_capacity (distinct: the registry has not aged it out yet, or is
  #     wedged, and we must not trust an old fact);
  #   * otherwise -> :no_capacity (no base for wl anywhere, or every candidate is
  #     at its node live-VM cap so a miss cannot prime).
  defp pick_node(state, wl, entry) do
    now = state.clock.()
    facts = NodeCapacity.all(state.capacity_table)
    need_mib = Map.get(entry, :mem_mib) || 0

    # Nodes that carry a base for this workload at all (fresh or not), used to
    # distinguish "stale" from "no base anywhere".
    with_base = Enum.filter(facts, fn f -> Map.has_key?(f.workloads || %{}, wl) end)

    candidates =
      Enum.filter(with_base, fn f ->
        not stale?(f, now, state.stale_after_ms) and not f_draining?(f) and base_ready?(f, wl)
      end)

    cond do
      candidates == [] and with_base != [] and Enum.any?(with_base, &stale?(&1, now, state.stale_after_ms)) ->
        {:error, :stale_capacity}

      candidates == [] ->
        {:error, :no_capacity}

      true ->
        # Placement keys on the INSTANCE id ("node/pod_uid"), so two instances on
        # one node (co-located bricks, or a surge roll's old + fresh pod) are
        # DISTINCT candidate pools (R0 PR-2, brick-capacity). We deliberately do
        # NOT collapse a node to one instance: each brick is its own capacity unit,
        # so all of a node's healthy instances stay candidates. Warmth stays a
        # two-tier decision here, with Score ordering selecting the warm brick
        # and Score.order selecting the miss frontier. A draining instance
        # never reaches here (its capacity row is dropped fail-closed); the removed
        # prefer-newest also covered the seconds-wide surge window before the old
        # pod flips to draining, so we accept a rare, self-healing chance of picking
        # a pod about to drain rather than keep node-keyed grouping (wrong under
        # bricks).
        #
        # SIZE-AWARE MISS TIER (Step 5): the WARM tier is left unchanged (a brick
        # that already holds a primed VM for wl is by construction big enough to run
        # it, and we must not refuse a warm hit). The MISS tier, which places a BRAND-
        # NEW VM, is additionally gated on `Embervm.Scheduler.mem_eligible?/2` so a
        # miss never primes onto a brick too small to boot the workload: a wildcard/
        # zero-budget DS is always eligible (inert on the DS-only fleet), a classed
        # brick needs `mem_headroom_mib >= mem_mib`. No eligible brick -> the existing
        # :no_capacity path (never a too-small placement).
        warm_candidates =
          Enum.filter(candidates, fn f -> inventory_ready?(state, instance_id_of(f), wl) end)

        case Scheduler.Score.order(warm_candidates, wl) |> List.first() do
          nil ->
            # No warm VM anywhere, so a miss must place a NEW VM: `place/1` applies
            # the free-slot and size gates. We pass the raw facts (not normalized
            # bricks) so the chosen one still carries its `workloads` for
            # snapshot_ref_of; `Brick.free_slots/1` reads either shape.
            #
            # The result is BOTH the frontier and the commit: the head IS the
            # committed brick. They were computed apart until c7439e144, the frontier
            # keeping a flat instance_id sort from before scoring existed, and since
            # the worker Primes the FRONTIER head (retry gate off = exactly one
            # attempt) that stale order was what actually placed. Keep them one value.
            ordered = Scheduler.place(%Request{bricks: candidates, key: wl, need_mib: need_mib})

            case ordered do
              [] ->
                # A true CAPACITY wall (Axis C demand signal): ready, base-holding
                # candidates exist but every one is out of slots or too small for
                # the workload's mem_mib. The no-base / stale branches above are
                # deliberately NOT noted: they wait on provisioning or fact
                # freshness, which a scale-up cannot serve. Async cast; a missing
                # controller (tests) makes it a silent no-op.
                Embervm.BrickController.note_denial(need_mib)
                {:error, :no_capacity}

              [f | _] ->
                # Each entry is a MAP carrying the :instance_id to dial (the
                # Retry.run contract reads it for logging/on_reject) plus that
                # instance's own :snapshot_ref for the Prime. A bare tuple crashes
                # Retry's Logger.debug with a BadMapError on the first rejection,
                # so the map shape is load-bearing, not cosmetic.
                miss_frontier =
                  Enum.map(ordered, fn brick ->
                    %{
                      instance_id: instance_id_of(brick),
                      snapshot_ref: snapshot_ref_of(brick, wl)
                    }
                  end)

                {:ok, instance_id_of(f), :miss, snapshot_ref_of(f, wl), miss_frontier}
            end

          warm ->
            # Warm has exactly one reserved VM on one instance: a single-element
            # frontier (no cross-brick retry; the VM is already primed HERE). Map
            # shape matches the Retry.run contract, same as the miss frontier, though
            # the warm path never enters prime_with_retry (acquire_vm short-circuits).
            {:ok, instance_id_of(warm), :warm, snapshot_ref_of(warm, wl),
             [%{instance_id: instance_id_of(warm), snapshot_ref: snapshot_ref_of(warm, wl)}]}
        end
    end
  end

  # The instance handle the dispatcher keys inventory / NodeChannel / BaseBuilder
  # on: the "node/pod_uid" string the registry stamps into facts. Falls back to
  # configured_id for a fact map that predates instance keying (defensive; the
  # registry always sets instance_id now).
  defp instance_id_of(f), do: Map.get(f, :instance_id) || f.configured_id

  defp stale?(f, now, stale_after), do: now - (f.updated_at || 0) > stale_after
  defp f_draining?(f), do: Map.get(f, :draining, false) == true

  defp base_ready?(f, wl) do
    case Map.get(f.workloads || %{}, wl) do
      %{base_state: base_state, snapshot_ref: ref} ->
        base_state_ready?(base_state) and is_binary(ref) and ref != ""

      _ ->
        false
    end
  end

  # The daemon reports base_state as the proto enum. Accept the atom form
  # (protobuf-elixir default) and the integer form (3) defensively.
  defp base_state_ready?(:BASE_BUILD_STATE_READY), do: true
  defp base_state_ready?(3), do: true
  defp base_state_ready?(_), do: false

  defp snapshot_ref_of(f, wl), do: get_in(f.workloads, [wl, :snapshot_ref])

  defp inventory_ready?(state, node_id, wl) do
    case Map.get(state.inventory, {node_id, wl}) do
      nil -> false
      q -> not :queue.is_empty(q)
    end
  end

  # -- VM reservation --------------------------------------------------------

  defp reserve_vm(state, node_id, wl, :warm) do
    q = Map.get(state.inventory, {node_id, wl}, :queue.new())

    case :queue.out(q) do
      {{:value, vm_id}, rest} ->
        # NOTE: the vm is NOT dropped from adoption_vm_ids here, deliberately.
        # spawn_assign_worker snapshots the set into the worker ctx AFTER this
        # runs, and the dispatch emission reads that snapshot to record
        # provenance. Removing here means the snapshot never contains the vm, so
        # an adopted vm records `warm` and #4768 stays unfixed. The removal
        # happens in spawn_assign_worker, once the snapshot has been taken.
        {%{state | inventory: Map.put(state.inventory, {node_id, wl}, rest)}, vm_id}

      {:empty, _} ->
        # pick_node guaranteed non-empty warm inventory just above; if it somehow
        # emptied, degrade to a miss (nil vm_id, worker will prime).
        {state, nil}
    end
  end

  defp reserve_vm(state, _node_id, _wl, :miss), do: {state, nil}

  # Return an unused reserved VM to the FRONT of inventory (a commit that failed
  # to assign): it is still primed and pristine, so it stays dispatchable.
  defp return_vm(state, node_id, wl, :warm, vm_id) when is_binary(vm_id) do
    q = Map.get(state.inventory, {node_id, wl}, :queue.new())
    %{state | inventory: Map.put(state.inventory, {node_id, wl}, :queue.in_r(vm_id, q))}
  end

  defp return_vm(state, _node_id, _wl, _mode, _vm_id), do: state

  # -- assign worker ---------------------------------------------------------

  defp spawn_assign_worker(state, task_id, pr, wl, node_id, mode, vm_id, snapshot_ref, candidates, entry) do
    owner = self()
    channel_fun = state.channel_fun
    invalidate_fun = state.invalidate_fun
    assign_fun = state.assign_fun
    prime_fun = state.prime_fun
    get_request_fun = state.get_request_fun
    op_log = state.op_log
    op_log_mod = state.op_log_mod
    wall_clock = state.wall_clock
    wall = state.wall_clock.()

    ctx = %{
      task_id: task_id,
      op_log: op_log,
      op_log_mod: op_log_mod,
      wall_clock: wall_clock,
      tenant: state.tenant,
      workload: wl,
      principal: pr,
      node_id: node_id,
      mode: mode,
      vm_id: vm_id,
      adoption_vm_ids: state.adoption_vm_ids,
      # The dispatcher pid, so a MISS worker can report the vm_id it primes back
      # here the instant it has one (see ensure_vm): that closes the window where
      # a just-primed miss VM is reported primed by the node but invisible to
      # known_vm_ids (meta.vm_id still nil), which adopt_inventory would otherwise
      # briefly re-adopt.
      owner: owner,
      snapshot_ref: snapshot_ref,
      # The ORDERED miss candidate frontier [{instance_id, snapshot_ref}]: the worker
      # reject/retries Prime across these under EMBERVM_PLACEMENT_RETRY (ADR 014
      # decision 3). HEAD == node_id above (the committed brick, today's single
      # attempt). Warm mode carries a single-element list (its VM is already primed
      # on node_id, nothing to retry). Unused by the warm path.
      candidates: candidates,
      invoke_path: entry.invoke_path,
      timeout_ms: entry.timeout_ms,
      result_max_bytes: entry.result_max_bytes,
      result_expires_at: wall + entry.result_ttl_ms,
      # Capture the current OTel context HERE (in the dispatcher process) so the
      # assign worker, spawned below, can attach it and nest its spans under the
      # dispatch trace across the process boundary (no automatic propagation).
      otel_ctx: OpenTelemetry.Ctx.get_current()
    }

    {pid, ref} =
      spawn_monitor(fn ->
        outcome =
          run_assign(ctx, channel_fun, invalidate_fun, assign_fun, prime_fun, get_request_fun)

        send(owner, {:assign_done, self(), outcome})
      end)

    watchdog_ms = state.assign_watchdog_ms || (transport_timeout(entry.timeout_ms) + state.assign_watchdog_margin_ms)
    # This must fire AFTER the normal gRPC deadline so the server-enforced path
    # gets first shot. The client-side kill is genuinely last-resort only.
    watchdog_ref = Process.send_after(owner, {:worker_timeout, pid, ref}, watchdog_ms)

    # vm_id is carried in the meta (not just the worker's ctx) so known_vm_ids can
    # see a warm-reserved VM that is out of inventory but still reported primed by
    # the node until the Assign lands; without it adopt_inventory would re-adopt an
    # in-flight VM and double-dispatch it. nil for a miss (the worker primes its own).
    meta = %{
      ref: ref,
      task_id: task_id,
      workload: wl,
      principal: pr,
      node_id: node_id,
      mode: mode,
      vm_id: vm_id,
      watchdog_timer_ref: watchdog_ref,
      watchdog_started_at: System.monotonic_time(:millisecond),
      watchdog_ms: watchdog_ms
    }
    # The adopted-provenance drop happens HERE, not in reserve_vm, because ctx
    # above snapshots adoption_vm_ids for the worker's dispatch emission. Drop
    # any earlier and the snapshot loses the vm and an adopted dispatch records
    # `warm`, which is #4768 unfixed. Task-lane vms are single-use, so a
    # dispatched vm never returns to inventory and this keeps the set bounded to
    # currently-resident adopted vms.
    %{
      state
      | workers: Map.put(state.workers, pid, meta),
        adoption_vm_ids: MapSet.delete(state.adoption_vm_ids, vm_id)
    }
  end

  # The worker body (runs OFF this GenServer). Fetches the guest request from the
  # op-log, acquires the shared channel, primes on the miss path, Assigns, and
  # classifies the result into a terminal outcome. Returns a 2- or 3-tuple the
  # GenServer applies to the FSM.
  defp run_assign(ctx, channel_fun, invalidate_fun, assign_fun, prime_fun, get_request_fun) do
    req_env = fetch_request(get_request_fun, ctx.task_id)

    # Restore the CALLER's trace context (the traceparent the submit carried, e.g.
    # the demos page whose httpx is OTel-instrumented) so the dispatch/guest_exec
    # spans join the caller's trace and appear in its waterfall. Falls back to the
    # dispatcher's own context (cron, retries, or a submit with no trace). Then
    # open the top dispatch span; `pool_hit` marks warm vs miss.
    _ = restore_trace_ctx(req_env, ctx)

    Tracer.with_span "embervm.dispatch", %{
      attributes: %{
        "ember.task_id" => ctx.task_id,
        "ember.workload" => ctx.workload,
        "ember.principal" => ctx.principal,
        "ember.node_id" => ctx.node_id,
        "ember.pool_hit" => ctx.mode == :warm
      }
    } do
      do_run_assign(ctx, req_env, channel_fun, invalidate_fun, assign_fun, prime_fun)
    end
  end

  # Prefer the caller's W3C traceparent (propagated through the op-log's submitted
  # request env) so EmberVM's spans nest under the caller's trace; else the
  # dispatcher's captured context.
  defp restore_trace_ctx(req_env, ctx) do
    case parse_traceparent(Map.get(req_env, "traceparent")) do
      {trace_id, span_id, flags} ->
        # Set the CALLER's span as the remote parent so the dispatch/guest_exec
        # spans nest under the caller's trace (parse the W3C traceparent directly
        # rather than via the text-map propagator, which needs propagator config we
        # do not carry). Guarded: a trace hiccup must never crash the assign worker.
        try do
          remote = :otel_tracer.from_remote_span(trace_id, span_id, flags)
          OpenTelemetry.Tracer.set_current_span(remote)
        rescue
          _ -> :ok
        catch
          _, _ -> :ok
        end

      :error ->
        OpenTelemetry.Ctx.attach(Map.get(ctx, :otel_ctx, :undefined))
    end
  end

  # Parse a W3C `traceparent` (`<ver>-<32hex trace>-<16hex span>-<2hex flags>`)
  # into integer ids, or :error for anything malformed/absent.
  defp parse_traceparent(tp) when is_binary(tp) do
    case String.split(tp, "-") do
      [_ver, trace_hex, span_hex, flags_hex]
      when byte_size(trace_hex) == 32 and byte_size(span_hex) == 16 ->
        with {trace_id, ""} <- Integer.parse(trace_hex, 16),
             {span_id, ""} <- Integer.parse(span_hex, 16),
             {flags, ""} <- Integer.parse(flags_hex, 16) do
          {trace_id, span_id, flags}
        else
          _ -> :error
        end

      _ ->
        :error
    end
  end

  defp parse_traceparent(_), do: :error

  defp do_run_assign(ctx, req_env, channel_fun, invalidate_fun, assign_fun, prime_fun) do
    # Resolve the VM to Assign and the {node_id, channel} it actually lives on. Warm
    # reuses its reserved VM on ctx.node_id; a miss Primes one, reject/retrying across
    # the candidate frontier (ADR 014 decision 3) and returning the node it ACTUALLY
    # primed on (which may differ from the committed head under retry), so the Assign
    # below dials the winning brick's channel, not the committed one.
    case acquire_vm(ctx, channel_fun, invalidate_fun, prime_fun) do
      {:ok, vm_id, node_id, channel} ->
        case ctx.mode do
          # PROVENANCE BELONGS ON THE WARM BRANCH, and this is the whole point
          # of the emission (#4768). An ADOPTED vm is reconciled back INTO
          # inventory after a control-plane restart, so a task that takes it
          # takes it from inventory: that is a warm hit, never a miss. A miss
          # Primes a fresh vm, which by definition was not adopted.
          #
          # Getting this backwards is not cosmetic: it leaves every adopted vm
          # recorded as an ordinary warm hit, a checker still cannot tell it
          # from a primed one, and the ~50% post-restart false-violation rate
          # #4768 measured stays exactly where it was.
          :warm ->
            Embervm.SpecTrace.emit(:adoption, :dispatch_warm, %{
              "task_id" => ctx.task_id,
              "vm_id" => vm_id,
              "node_id" => node_id,
              "provenance" =>
                if(MapSet.member?(ctx.adoption_vm_ids, vm_id), do: :adopted, else: :warm)
            })

          :miss ->
            Embervm.SpecTrace.emit(:adoption, :dispatch_miss, %{
              "task_id" => ctx.task_id,
              "vm_id" => vm_id,
              "node_id" => node_id,
              "provenance" => :miss
            })
        end
        guest_req = build_guest_request(req_env, ctx.invoke_path)

        assign_req = %AssignRequest{
          trace: %Trace{workload: ctx.workload, task_id: ctx.task_id},
          vm_id: vm_id,
          request: guest_req,
          timeout_ms: ctx.timeout_ms
        }

        case guest_exec(assign_fun, channel, assign_req, ctx) do
          {:ok, %AssignResponse{response: %GuestResponse{} = resp, usage: usage}} ->
            classify_response(resp, ctx, usage)

          {:error, reason} ->
            _ = invalidate_fun.(node_id, channel)
            classify_error(reason)
        end

      {:error, :no_channel, reason} ->
        {:failed, :transport, {:no_channel, reason}, nil}

      {:error, :prime_failed, reason} ->
        classify_error(reason)
    end
  end

  # Warm: the VM is already reserved on ctx.node_id; just acquire that node's channel.
  defp acquire_vm(%{mode: :warm, vm_id: vm_id} = ctx, channel_fun, _invalidate_fun, _prime_fun)
       when is_binary(vm_id) do
    case channel_fun.(ctx.node_id) do
      {:ok, channel} -> {:ok, vm_id, ctx.node_id, channel}
      {:error, reason} -> {:error, :no_channel, reason}
    end
  end

  # Miss: Prime a VM, reject/retrying across the candidate frontier. Returns the
  # winning {vm_id, node_id, channel} so the Assign dials the brick that actually
  # primed, and reports that node back to the dispatcher (meta / adopt-inventory).
  defp acquire_vm(ctx, channel_fun, invalidate_fun, prime_fun) do
    prime_with_retry(ctx, channel_fun, invalidate_fun, prime_fun)
  end

  # Reject/retry Prime across ctx.candidates (ADR 014 decision 3). Each attempt
  # acquires THAT candidate's channel and Primes on it; a RESOURCE_EXHAUSTED (the
  # node-side pressure rejection) retries the next brick, a no-channel is likewise
  # advanced past (that brick is unreachable), any other Prime error is terminal.
  # Gate OFF => Retry.run makes exactly one attempt on the head candidate, i.e.
  # today's single Prime on the committed brick. On success it reports the ACTUAL
  # primed vm_id + node back to the owner so meta.node_id and the known_vm_ids view
  # track the brick the VM really lives on (not the committed head), which keeps
  # adopt_inventory and the Assign channel correct after a cross-brick retry.
  defp prime_with_retry(ctx, channel_fun, invalidate_fun, prime_fun) do
    attempt_fun = fn %{instance_id: dial_id, snapshot_ref: snapshot_ref} ->
      case channel_fun.(dial_id) do
        {:ok, channel} ->
          case prime_on(ctx, dial_id, channel, snapshot_ref, prime_fun) do
            {:ok, vm_id} ->
              {:ok, {vm_id, dial_id, channel}}

            {:reject, reason} ->
              # Node-pressure RESOURCE_EXHAUSTED: this brick is full; drop its
              # channel (best-effort) and let Retry try the next candidate.
              _ = invalidate_fun.(dial_id, channel)
              {:reject, reason}

            {:error, reason} ->
              _ = invalidate_fun.(dial_id, channel)
              {:error, {:prime_failed, reason}}
          end

        {:error, reason} ->
          # No channel to this brick: treat as a retryable rejection so the frontier
          # advances to a reachable candidate rather than failing the whole dispatch
          # on one unreachable brick (gate-off degrades to the single committed brick,
          # whose unreachability is the same terminal no-channel as before).
          {:reject, {:no_channel, reason}}
      end
    end

    case Embervm.Scheduler.Retry.run(ctx.candidates, attempt_fun) do
      {:ok, {vm_id, node_id, channel}} ->
        # Instrumentation covers task-lane priming only. Session, serving, and
        # stateful lane priming is not yet instrumented. Append once, after the
        # retry policy has selected the successful prime attempt.
        _ = safe_call(fn -> append_primed(ctx, vm_id, node_id) end)
        {:ok, vm_id, node_id, channel}

      {:error, {:prime_failed, reason}} ->
        {:error, :prime_failed, reason}

      # Every candidate rejected (pressure or unreachable), or the gate-off single
      # attempt rejected: surface as a prime failure so the task retries at-least-once
      # exactly like a downed node, never a wedge. Carry the last reason shape.
      {:error, :no_capacity} ->
        {:error, :prime_failed, {:rpc, :resource_exhausted}}
    end
  end

  # One Prime attempt on an already-acquired channel for the candidate `dial_id`.
  # Classifies a RESOURCE_EXHAUSTED (gRPC status 8) as a retryable {:reject, _}; any
  # other error is terminal. On success it reports the vm_id AND `dial_id` back to the
  # owner immediately (the adopt-inventory race window the original ensure_vm
  # documented) and returns the vm_id.
  defp prime_on(ctx, dial_id, channel, snapshot_ref, prime_fun) do
    req = %PrimeRequest{trace: %Trace{workload: ctx.workload}, snapshot_ref: snapshot_ref || ""}

    result =
      Tracer.with_span "embervm.prime", %{
        attributes: %{"ember.workload" => ctx.workload, "ember.task_id" => ctx.task_id}
      } do
        prime_fun.(channel, req)
      end

    case result do
      {:ok, %PrimeResponse{vm_id: vm_id}} ->
        # Report the vm_id AND the brick (dial_id) it primed on so the dispatcher
        # stamps BOTH into this worker's meta immediately: until then adopt_inventory
        # cannot see this in-flight miss VM (the node reports it primed) and could
        # re-adopt it, and a cross-brick retry means dial_id differs from the committed
        # head, so meta.node_id must follow the VM to the brick it actually landed on.
        send(ctx.owner, {:vm_primed, self(), vm_id, dial_id})
        {:ok, vm_id}

      {:error, %GRPC.RPCError{status: 8}} = rejected ->
        {:reject, rejected}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp append_primed(ctx, vm_id, node_id) do
    if ctx.op_log == nil do
      :ok
    else
      op = PrimedOp.build(ctx.tenant, ctx.workload, vm_id, node_id, :task)

      ctx.op_log_mod.append(ctx.op_log, op)
    end
  end

  defp fetch_request(get_request_fun, task_id) do
    case safe_call(fn -> get_request_fun.(task_id) end) do
      {:ok, {:ok, env}} when is_map(env) -> env
      _ -> %{}
    end
  end

  # The guest Assign RPC (the actual guest execution) in its own span, so guest
  # latency is never the uninstrumented phase (the fc-invoke 5-QPS lesson).
  defp guest_exec(assign_fun, channel, assign_req, ctx) do
    Tracer.with_span "embervm.guest_exec", %{attributes: %{"ember.task_id" => ctx.task_id}} do
      assign_fun.(channel, assign_req)
    end
  end

  defp build_guest_request(req_env, invoke_path) do
    path = Map.get(req_env, "path") || invoke_path
    headers = Map.get(req_env, "headers") || %{}

    headers =
      case Map.get(req_env, "content_type") do
        nil -> headers
        ct -> Map.put(headers, "content-type", ct)
      end

    body =
      case Map.get(req_env, "body_b64") do
        b64 when is_binary(b64) -> decode_body(b64)
        _ -> ""
      end

    %GuestRequest{method: "POST", path: path, headers: headers, body: body}
  end

  defp decode_body(b64) do
    case Base.decode64(b64) do
      {:ok, bin} -> bin
      :error -> ""
    end
  end

  # A well-formed guest HTTP response: 2xx/3xx succeeds (result stored, truncated
  # to result_max_bytes); a 5xx is a retryable :guest5xx; a 4xx is a permanent
  # :guest4xx (the request itself is wrong, retrying reproduces it). Every guest
  # response, success OR error, carries usage (the VM did measured work before
  # answering), so the outcome carries the normalized stats to bill; a
  # transport/timeout failure (classify_error) carries nil (nothing measured).
  defp classify_response(%GuestResponse{status_code: code} = resp, ctx, usage) do
    stats = Embervm.Usage.from_proto(usage)

    cond do
      code >= 500 and code <= 599 ->
        {:failed, :guest5xx, {:guest_status, code}, stats}

      code >= 400 and code <= 499 ->
        {:failed, :guest4xx, {:guest_status, code}, stats}

      true ->
        body = resp.body || ""
        {stored, truncated?} = truncate(body, ctx.result_max_bytes)

        {:succeeded,
         %{
           status_code: code,
           body: stored,
           size_bytes: byte_size(body),
           truncated: truncated?,
           expires_at: ctx.result_expires_at,
           headers: resp.headers || %{}
         }, stats}
    end
  end

  # A transport/RPC-level failure. DEADLINE_EXCEEDED (the guest did not answer in
  # time) is :timeout; everything else (UNAVAILABLE, FAILED_PRECONDITION for a
  # vm that vanished, RESOURCE_EXHAUSTED, a raw Mint error) is :transport, so it
  # retries onto a freshly primed VM. No usage is reported for these (the guest
  # never answered), so they bill nothing.
  defp classify_error(%GRPC.RPCError{status: 4}), do: {:failed, :timeout, :deadline_exceeded, nil}
  defp classify_error(%GRPC.RPCError{} = e), do: {:failed, :transport, {:rpc, e.status}, nil}
  defp classify_error(reason), do: {:failed, :transport, reason, nil}

  defp truncate(body, max) when byte_size(body) <= max, do: {body, false}
  defp truncate(body, max) when max <= 0, do: {"", byte_size(body) > 0}
  defp truncate(body, max), do: {binary_part(body, 0, max), true}

  # -- worker completion -----------------------------------------------------

  # Apply a finished worker's outcome to the FSM, release its in-flight counters,
  # and drain (its capacity is now free). `flush` demonitors normal completions
  # so their trailing :DOWN is discarded; an abnormal exit arrives AS the :DOWN,
  # so there is nothing to flush there.
  defp finish_worker(state, pid, outcome, flush) do
    case Map.pop(state.workers, pid) do
      {nil, _} ->
        state

      {meta, workers} ->
        if flush == :flush, do: Process.demonitor(meta.ref, [:flush])
        # Defensive cancellation: a late timer message is harmless because its
        # monitor ref is compared against the live worker metadata above.
        Process.cancel_timer(meta.watchdog_timer_ref)

        state = %{state | workers: workers}
        state = apply_outcome(state, meta, outcome)

        state
        |> dec_inflight(meta.workload, meta.principal)
        |> drain_all()
    end
  end

  defp apply_outcome(state, meta, {:succeeded, result, stats}) do
    result = safe_call(fn -> Embervm.TaskStore.succeed(state.task_store, meta.task_id, result, stats) end)

    case result do
      {:ok, {:ok, _task}} -> emit_succeed(meta, state)
      {:ok, {:ok, _task, _}} -> emit_succeed(meta, state)
      _ -> :ok
    end

    state
  end

  defp apply_outcome(state, meta, {:failed, reason, _detail, stats}) do
    apply_failure(state, meta, reason, stats)
  end

  # Usage-less fallbacks (a bare `{:failed, reason}` from a path that never saw a
  # guest response): bill nothing.
  defp apply_outcome(state, meta, {:succeeded, result}) do
    result = safe_call(fn -> Embervm.TaskStore.succeed(state.task_store, meta.task_id, result) end)

    case result do
      {:ok, {:ok, _task}} -> emit_succeed(meta, state)
      {:ok, {:ok, _task, _}} -> emit_succeed(meta, state)
      _ -> :ok
    end

    state
  end

  defp emit_succeed(meta, state) do
    # Check the gate BEFORE the lookup, not just inside emit/3. The TaskStore
    # call is a GenServer.call from the single Dispatcher process into the single
    # TaskStore process, so running it unconditionally doubles store round trips
    # per completion on a hot path, in production, purely to populate a debug
    # record that is then discarded because the trace is off. safe_call/1 catches
    # an exit but does not shorten the default 5s timeout, so a slow TaskStore
    # would block the dispatcher an extra 5s per completed task with the facility
    # disabled.
    if Embervm.SpecTrace.enabled_now?() do
      do_emit_succeed(meta, state)
    end

    :ok
  end

  defp do_emit_succeed(meta, state) do
    session_id =
      case safe_call(fn -> Embervm.TaskStore.get(state.task_store, meta.task_id) end) do
        {:ok, {:ok, task}} -> Map.get(task, :session_id) || Map.get(task, "session_id")
        _ -> nil
      end

    Embervm.SpecTrace.emit(:adoption, :succeed, %{
      "task_id" => meta.task_id,
      "vm_id" => meta.vm_id,
      "session_id" => session_id
    })
  end

  defp apply_outcome(state, meta, {:failed, reason, _detail}) do
    apply_failure(state, meta, reason, nil)
  end

  defp apply_outcome(state, meta, {:failed, reason}) do
    apply_failure(state, meta, reason, nil)
  end

  # Drive the failure through TaskStore's Retry classification, passing any
  # measured usage so a guest 4xx/5xx is still billed. A retryable failure comes
  # back with a backoff we arm a timer for (this dispatcher owns retry
  # scheduling); a permanent one is already dead-lettered by TaskStore.
  defp apply_failure(state, meta, reason, stats) do
    case safe_call(fn -> Embervm.TaskStore.fail(state.task_store, meta.task_id, reason, stats) end) do
      {:ok, {:ok, _task, backoff}} -> arm_retry(state, meta.task_id, backoff)
      _ -> state
    end
  end

  defp arm_retry(state, task_id, backoff) do
    ref = Process.send_after(self(), {:retry_task, task_id}, max(backoff, 0))
    %{state | retry_timers: Map.put(state.retry_timers, task_id, ref)}
  end

  # -- backlog sweep ---------------------------------------------------------

  # Reconcile against TaskStore's durable backlog:
  #   * queued -> enqueue any the in-memory queues do not hold (recovery / a
  #     dropped push cast);
  #   * failed_retryable with no pending retry timer -> retry now (a node-down
  #     reassign produces these out of band with no backoff owner);
  #   * assigned/running with NO live worker -> an ORPHAN (a dispatcher restart
  #     dropped its workers, a partial assign/start commit spawned none, or a
  #     completion's terminal op-log append failed): fail it as transport so it
  #     re-queues through Retry (at-least-once). A task a live worker still owns
  #     is skipped, so a genuinely-running task is never double-dispatched.
  # Then drain.
  defp run_sweep(state) do
    case safe_call(fn -> Embervm.TaskStore.list_backlog(state.task_store) end) do
      {:ok, {:ok, backlog}} ->
        tracked = state.workers |> Map.values() |> MapSet.new(& &1.task_id)

        state
        |> reduce_backlog(backlog, tracked)
        |> adopt_inventory()
        |> emit_spec_trace_checkpoint()
        |> drain_all()

      _ ->
        state
    end
  end

  defp emit_spec_trace_checkpoint(state) do
    node_workload_vm_ids =
      for {{node_id, workload}, queue} <- state.inventory, into: %{} do
        {"#{node_id}:#{workload}", :queue.to_list(queue)}
      end

    reserved_vm_ids =
      state.workers
      |> Map.values()
      |> Enum.map(&Map.get(&1, :vm_id))
      |> Enum.filter(&is_binary/1)

    node_reported =
      for facts <- NodeCapacity.all(state.capacity_table), into: %{} do
        primed_vm_ids =
          Map.get(facts, :workloads, %{})
          |> Map.values()
          |> Enum.flat_map(&(Map.get(&1, :primed_vm_ids, []) || []))

        {instance_id_of(facts), %{
          "live_vms" => Map.get(facts, :live_vms, 0),
          "primed_count" => length(primed_vm_ids)
        }}
      end

    Embervm.SpecTrace.emit(:adoption, :checkpoint, %{
      "node_workload_vm_ids" => node_workload_vm_ids,
      "reserved_vm_ids" => reserved_vm_ids,
      "node_health" => safe_node_health(),
      "node_reported" => node_reported
    })

    state
  rescue
    _ -> state
  end

  defp safe_node_health do
    Embervm.NodeRegistry.node_health()
  catch
    :exit, _ -> %{}
  end

  # Reconcile the dispatch inventory with each node's reported primed pool: adopt
  # every node-reported primed vm_id we do not already know into its {node,wl}
  # inventory. This is what lets a RESTARTED control plane recover the node's warm
  # pool (which outlives the control plane) instead of orphaning it: without it
  # the fresh control plane has an empty inventory, cannot assign to the running
  # primed VMs (it never learned their vm_ids), and cannot prime past them because
  # they still count against the node's max_live_vms - dispatch deadlocks on
  # :no_capacity forever (see node.proto WorkloadCapacity.primed_vm_ids).
  #
  # ADDITIVE only (never drops on a status read): a vm_id reserved for an in-flight
  # assign is still reported primed by the node until the Assign lands, so dropping
  # on status would race the reserve and could double-dispatch. A vm_id the node
  # destroyed out of band (base turnover) self-corrects on its next use (one
  # failed-then-retried assign), not here. Runs every sweep, so recovery lands
  # within one sweep interval of the registry populating capacity after a restart.
  defp adopt_inventory(state) do
    facts = NodeCapacity.all(state.capacity_table)

    {state, adopted} = Enum.reduce(facts, {state, %{}}, fn f, {acc, adopted} ->
      # Key the adopted inventory by the INSTANCE id ("node/pod_uid"), the same key
      # pick_node dispatches against, so an adopted warm pool is reachable (R0 PR-2).
      node_id = instance_id_of(f)

      Enum.reduce(f.workloads || %{}, {acc, adopted}, fn {wl, wc}, {acc2, adopted2} ->
        Enum.reduce(Map.get(wc, :primed_vm_ids, []) || [], {acc2, adopted2}, fn vm_id, {acc3, adopted3} ->
          if MapSet.member?(known_vm_ids(acc3), vm_id) do
            {acc3, adopted3}
          else
            acc4 = put_vm_if_unknown(acc3, node_id, wl, vm_id)
            {
              %{acc4 | adoption_vm_ids: MapSet.put(acc4.adoption_vm_ids, vm_id)},
              Map.update(adopted3, node_id, [vm_id], &[vm_id | &1])
            }
          end
        end)
      end)
    end)

    for {node_id, vm_ids} <- adopted do
      # `vm_ids`, NOT `adopted`. The checker reads `vars["vm_ids"]` in five
      # places (dispatch_provenance, adopt_idempotent, prime_before_checkpoint)
      # and the fixtures use that name, so emitting `adopted` meant the adopted
      # set was empty on every production trace while every test passed. Same
      # writer-versus-reader shape as the checkpoint inventory bug.
      #
      # Two consequences, in opposite directions. `adopt_idempotent` reported
      # PASS over zero vm_ids with a non-zero coverage count, which reads as
      # examined. And `prime_before_checkpoint` reported FAIL after a
      # control-plane restart: run_id is fresh per incarnation, so an adopted
      # vm's `prime` record sits in the previous run's group, and the adopted
      # set that was supposed to cover it was empty. That is the exact event
      # adoption.tla exists to model, failing for a typo.
      Embervm.SpecTrace.emit(:adoption, :adopt_inventory, %{
        "node_id" => node_id,
        "vm_ids" => Enum.sort(vm_ids)
      })
    end

    state
  end

  # Enqueue a primed vm_id into the {node,wl} inventory unless we already hold it
  # (parked in any inventory queue, or reserved by an in-flight assign worker).
  # Idempotent so the deposit cast and adopt_inventory - which both surface the
  # same freshly primed VM, and can race in the window between Prime returning and
  # the deposit cast being processed - never double-enqueue one vm_id, which would
  # let two tasks assign to the same single-use VM.
  defp put_vm_if_unknown(state, node_id, wl, vm_id) when is_binary(vm_id) and vm_id != "" do
    if MapSet.member?(known_vm_ids(state), vm_id) do
      state
    else
      q = Map.get(state.inventory, {node_id, wl}, :queue.new())
      %{state | inventory: Map.put(state.inventory, {node_id, wl}, :queue.in(vm_id, q))}
    end
  end

  defp put_vm_if_unknown(state, _node_id, _wl, _vm_id), do: state

  # Every vm_id the dispatcher currently holds: parked in any inventory queue, or
  # reserved by an in-flight assign worker. The dedup basis for inventory adds.
  defp known_vm_ids(state) do
    inv =
      for {_k, q} <- state.inventory, id <- :queue.to_list(q), into: MapSet.new(), do: id

    for {_pid, meta} <- state.workers, is_binary(Map.get(meta, :vm_id)), into: inv, do: meta.vm_id
  end

  defp reduce_backlog(state, backlog, tracked) do
    Enum.reduce(backlog, state, fn item, acc -> sweep_one(item, acc, tracked) end)
  end

  defp sweep_one(%{state: :queued, task_id: tid, workload: wl, principal: pr}, state, _tracked) do
    do_enqueue(state, tid, wl, pr)
  end

  defp sweep_one(%{state: :failed_retryable, task_id: tid}, state, _tracked) do
    if Map.has_key?(state.retry_timers, tid) do
      state
    else
      _ = safe(fn -> Embervm.TaskStore.retry(state.task_store, tid) end)
      state
    end
  end

  defp sweep_one(%{state: inflight, task_id: tid}, state, tracked)
       when inflight in [:assigned, :running] do
    if MapSet.member?(tracked, tid) do
      state
    else
      # Orphan reclaim: fail as transport, then (if retryable) arm the backoff
      # timer exactly as the live-failure path does, so it re-queues on its own.
      case safe_call(fn -> Embervm.TaskStore.fail(state.task_store, tid, :transport) end) do
        {:ok, {:ok, _task, backoff}} -> arm_retry(state, tid, backoff)
        _ -> state
      end
    end
  end

  defp sweep_one(_other, state, _tracked), do: state

  defp schedule_sweep(state) do
    Process.send_after(self(), :sweep, state.sweep_interval_ms)
    state
  end

  # -- counters + denials ----------------------------------------------------

  defp inflight_count(map, wl), do: Map.get(map, wl, 0)

  defp inc_inflight(state, wl, pr) do
    %{
      state
      | inflight_wl: Map.update(state.inflight_wl, wl, 1, &(&1 + 1)),
        inflight_pr: Map.update(state.inflight_pr, {wl, pr}, 1, &(&1 + 1))
    }
  end

  defp dec_inflight(state, wl, pr) do
    %{
      state
      | inflight_wl: dec_key(state.inflight_wl, wl),
        inflight_pr: dec_key(state.inflight_pr, {wl, pr})
    }
  end

  defp dec_key(map, key) do
    case Map.get(map, key, 0) do
      n when n <= 1 -> Map.delete(map, key)
      n -> Map.put(map, key, n - 1)
    end
  end

  defp bump_denial(state, kind) do
    %{state | denials: Map.update!(state.denials, kind, &(&1 + 1))}
  end

  defp tally(state, :warm), do: %{state | warm_hits: state.warm_hits + 1}
  defp tally(state, :miss), do: %{state | misses: state.misses + 1}

  defp release_depth(state, wl, pr) do
    :ets.update_counter(state.depth_table, {wl, pr}, {2, -1, 0, 0}, {{wl, pr}, 0})
    state
  end

  # -- share cap -------------------------------------------------------------

  # A principal's max in-flight share of the workload cap. A configured fraction
  # (values-driven) if set, else cap split evenly across the currently-active
  # principals (those with a queued or in-flight task), min 1 so a single
  # principal is never starved to zero.
  defp share_cap(state, wl, entry) do
    case state.share_fraction do
      f when is_number(f) and f > 0 ->
        max(1, trunc(entry.cap * f))

      _ ->
        active = active_principals(state, wl)
        max(1, div(entry.cap, max(active, 1)))
    end
  end

  # The set of a workload's currently-queued principals that are over their daily
  # quota as of now (wall clock, to match the usage projection's day bucket). Only
  # queued principals can be skipped in the rotation, so only they are checked.
  # within_quota?/4 is fail-closed per principal WITH a budget; a principal with
  # no configured budget is never in this set (quota is opt-in), so an unquota'd
  # cluster computes an empty set and pays no per-tick cost.
  defp over_budget_principals(state, wl) do
    now = state.wall_clock.()

    case Map.get(state.queues, wl) do
      nil ->
        MapSet.new()

      fq ->
        fq
        |> fq_principals()
        |> Enum.filter(fn pr ->
          not Embervm.Metering.within_quota?(pr, now, state.quota_config, state.quota_table)
        end)
        |> MapSet.new()
    end
  end

  defp active_principals(state, wl) do
    queued = state.queues |> Map.get(wl, new_fq()) |> fq_principals()

    inflight =
      state.inflight_pr
      |> Map.keys()
      |> Enum.filter(fn {w, _p} -> w == wl end)
      |> Enum.map(fn {_w, p} -> p end)

    (queued ++ inflight) |> MapSet.new() |> MapSet.size()
  end

  # -- fair queue (per workload) ---------------------------------------------
  #
  # rotation: a :queue of principals in round-robin order (each appears at most
  #   once, only while its FIFO is non-empty).
  # members:  the MapSet of principals currently in rotation (dedupe guard).
  # fifos:    principal -> :queue of task_id (FIFO within a principal).

  defp new_fq, do: %{rotation: :queue.new(), members: MapSet.new(), fifos: %{}}

  defp fq_enqueue(fq, pr, task_id) do
    fifo = Map.get(fq.fifos, pr, :queue.new())
    fifos = Map.put(fq.fifos, pr, :queue.in(task_id, fifo))

    if MapSet.member?(fq.members, pr) do
      %{fq | fifos: fifos}
    else
      %{fq | fifos: fifos, rotation: :queue.in(pr, fq.rotation), members: MapSet.put(fq.members, pr)}
    end
  end

  defp fq_principals(fq), do: MapSet.to_list(fq.members)

  defp fq_empty_q?(fq), do: MapSet.size(fq.members) == 0

  defp fq_empty?(queues, wl) do
    case Map.get(queues, wl) do
      nil -> true
      fq -> fq_empty_q?(fq)
    end
  end

  # Pop the next task in round-robin-across-principals / FIFO-within order,
  # skipping principals already at their in-flight share OR in the `blocked` set
  # (over their daily quota). Walks at most one full rotation; returns {:none, fq}
  # when nothing is servable (empty, or all remaining principals are share-capped
  # or quota-blocked), rotating skipped principals to the back so order is
  # preserved (a quota-blocked principal is parked, not dropped: it becomes
  # servable again when its budget resets at day rollover).
  defp fq_take(nil, _wl, _share, _inflight_pr, _blocked), do: {:none, new_fq()}

  defp fq_take(fq, wl, share, inflight_pr, blocked) do
    fq_take(fq, wl, share, inflight_pr, blocked, MapSet.size(fq.members))
  end

  defp fq_take(fq, _wl, _share, _inflight_pr, _blocked, 0), do: {:none, fq}

  defp fq_take(fq, wl, share, inflight_pr, blocked, remaining) do
    case :queue.out(fq.rotation) do
      {:empty, _} ->
        {:none, fq}

      {{:value, pr}, rot} ->
        pr_inflight = Map.get(inflight_pr, {wl, pr}, 0)
        fifo = Map.get(fq.fifos, pr, :queue.new())

        cond do
          pr_inflight >= share or MapSet.member?(blocked, pr) ->
            # Over its share or over its quota: rotate to the back, try the next.
            fq_take(%{fq | rotation: :queue.in(pr, rot)}, wl, share, inflight_pr, blocked, remaining - 1)

          :queue.is_empty(fifo) ->
            # Shouldn't happen (members implies non-empty), but drop it defensively.
            fq_take(
              %{fq | rotation: rot, members: MapSet.delete(fq.members, pr), fifos: Map.delete(fq.fifos, pr)},
              wl,
              share,
              inflight_pr,
              blocked,
              remaining - 1
            )

          true ->
            {{:value, task_id}, fifo2} = :queue.out(fifo)

            if :queue.is_empty(fifo2) do
              {:ok, task_id, pr,
               %{fq | rotation: rot, members: MapSet.delete(fq.members, pr), fifos: Map.delete(fq.fifos, pr)}}
            else
              # Still has tasks: rotate this principal to the back (round-robin).
              {:ok, task_id, pr,
               %{fq | rotation: :queue.in(pr, rot), fifos: Map.put(fq.fifos, pr, fifo2)}}
            end
        end
    end
  end

  # -- misc ------------------------------------------------------------------

  defp snapshot(state) do
    %{
      denials: state.denials,
      warm_hits: state.warm_hits,
      misses: state.misses,
      inflight_wl: state.inflight_wl,
      inflight_pr: state.inflight_pr,
      queued: MapSet.size(state.queued_ids),
      queue_depth:
        for(wl <- Map.keys(state.queues), into: %{}, do: {wl, fq_depth(Map.get(state.queues, wl))}),
      inventory: for({k, q} <- state.inventory, into: %{}, do: {k, :queue.len(q)}),
      workers: map_size(state.workers),
      # The instance each in-flight worker's VM is currently attributed to, keyed by
      # vm_id (nil until a miss worker reports its Prime). Observability for tests +
      # ops: after an ADR 014 decision 3 cross-brick retry, a miss worker's node_id is
      # the brick that ACTUALLY primed (the {:vm_primed, vm_id, dial_id} update), not
      # the committed head, so this is how the node-reassignment invariant is asserted.
      worker_nodes: for({_pid, m} <- state.workers, into: %{}, do: {Map.get(m, :vm_id), m.node_id})
    }
  end

  defp fq_depth(fq) do
    Enum.reduce(fq.fifos, 0, fn {_p, q}, acc -> acc + :queue.len(q) end)
  end

  defp create_depth_table(table) do
    if :ets.whereis(table) == :undefined do
      :ets.new(table, [:set, :public, :named_table, write_concurrency: true])
    end
  end

  # Wrap a TaskStore/op-log call so a rare exception (a store mid-restart) never
  # crashes the dispatcher; returns {:ok, result} or :error.
  defp safe_call(fun) do
    try do
      {:ok, fun.()}
    rescue
      _ -> :error
    catch
      _, _ -> :error
    end
  end

  defp safe(fun) do
    try do
      fun.()
    rescue
      _ -> :error
    catch
      _, _ -> :error
    end
  end

  defp default_mono, do: System.monotonic_time(:millisecond)
  defp default_wall, do: System.system_time(:millisecond)

  defp default_assign(channel, %AssignRequest{timeout_ms: timeout_ms} = req) do
    Embervm.Node.V1.NodeService.Stub.assign(channel, req, timeout: transport_timeout(timeout_ms))
  end

  # Prime is a fast VM allocation on a brick with guaranteed free slots;
  # deliberately keep the short elixir-grpc default (10s) since a Prime should
  # complete in <1s and this is not an application deadline like Assign/SessionAssign.
  defp default_prime(channel, %PrimeRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.prime(channel, req)
  end

  # Transport timeout must exceed the application deadline (timeout_ms) the guest is
  # told it has, plus headroom so the guest-side timeout fires first and noded can
  # respond with a structured deadline-exceeded error before the transport deadline
  # hits. Without this, the caller gets {:server_closed_request, :cancel} (grpc-elixir
  # 1.0.2 cancel-frame bug #4144) instead of noded's real status.
  # Headroom is 5s: conservative, as a well-behaved guest should not approach its own
  # timeout, and noded's error response is O(1ms). The misnamed default gRPC timeout
  # is 10s; our guest default is 90s, so an explicit transport timeout is needed.
  # MUST stay in sync with session.ex transport_timeout/1 (they are duplicated to avoid
  # a module dependency; any change to one must be reflected in the other immediately).
  @default_timeout_ms 90_000
  @headroom_ms 5_000
  @doc false
  def transport_timeout(nil), do: @default_timeout_ms + @headroom_ms
  def transport_timeout(0), do: @default_timeout_ms + @headroom_ms
  def transport_timeout(timeout_ms) when is_integer(timeout_ms) and timeout_ms > 0 do
    timeout_ms + @headroom_ms
  end
end
