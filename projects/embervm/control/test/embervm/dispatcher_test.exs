defmodule Embervm.DispatcherTest do
  @moduledoc """
  Task 11 acceptance for `Embervm.Dispatcher`: warm + miss dispatch drive a task
  to terminal, fail-closed enforcement denies with distinct kinds, the caps bound
  in-flight and enforce per-principal fairness, and 1k tasks drain with zero lost.

  A FAKE DAEMON is injected via `channel_fun`/`assign_fun`/`prime_fun` (the same
  seam idiom the NodeRegistry/BaseBuilder tests use), a real (unnamed) op-log +
  TaskStore give the real FSM, and unique ETS table names + an unnamed store keep
  each test isolated from the application's own supervised dispatcher.
  """
  # async: false: put_env/delete_env on EMBERVM_PLACEMENT_RETRY here would leak the
  # gate into other async modules' gate-off assertions and flake CI.
  use ExUnit.Case, async: false

  alias Embervm.{Dispatcher, NodeCapacity, TaskStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Scheduler.Score
  alias Embervm.Node.V1.{AssignRequest, AssignResponse, GuestResponse, PrimeResponse, Trace, UsageStats}

  # -- harness ---------------------------------------------------------------

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"cap_#{suffix}"
    cat_table = :"cat_#{suffix}"
    depth_table = :"depth_#{suffix}"
    disp_name = :"disp_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_dispatcher_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = TaskStore.start_link(name: nil, op_log: op_log, on_queued: fn t -> Dispatcher.enqueue(disp_name, t) end)

    disp_opts =
      [
        name: disp_name,
        task_store: store,
        capacity_table: cap_table,
        catalog_table: cat_table,
        depth_table: depth_table,
        clock: fn -> 1_000_000 end,
        channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
        assign_fun: Keyword.get(opts, :assign_fun, fn _ch, _req -> {:ok, success_resp()} end),
        prime_fun: Keyword.get(opts, :prime_fun, fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-#{System.unique_integer([:positive])}"}} end),
        start_sweep: false
      ] ++ Keyword.take(opts, [:queue_depth_cap, :share_fraction, :stale_after_ms, :quota_config, :quota_table, :wall_clock])

    {:ok, disp} = Dispatcher.start_link(disp_opts)

    %{disp: disp, name: disp_name, store: store, cap_table: cap_table, cat_table: cat_table, depth_table: depth_table}
  end

  defp success_resp(code \\ 200, body \\ "ok") do
    %AssignResponse{
      response: %GuestResponse{status_code: code, headers: %{}, body: body},
      usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}
    }
  end

  defp put_facts(ctx, wl, opts \\ []) do
    node = Keyword.get(opts, :node, "node-4")
    # By default a fact keys by its bare node name (one instance per node, the
    # DS-only fleet). A test placing TWO co-located bricks on one node passes a
    # distinct :key (the {node, pod_uid} instance tuple) and :instance_id so the
    # two facts do not collide in ETS and the dispatcher keys inventory per brick.
    key = Keyword.get(opts, :key, node)
    instance_id = Keyword.get(opts, :instance_id, node)

    NodeCapacity.put(ctx.cap_table, key, %{
      node_id: node,
      configured_id: node,
      instance_id: instance_id,
      workloads: %{
        wl => %{
          free_primed_slots: Keyword.get(opts, :free, 0),
          snapshot_ref: Keyword.get(opts, :snapshot_ref, "snap-#{wl}"),
          base_state: Keyword.get(opts, :base_state, :BASE_BUILD_STATE_READY),
          primed_vm_ids: Keyword.get(opts, :primed_ids, [])
        }
      },
      # Default: no size_class / zero mem_budget => a WILDCARD brick (always
      # mem-eligible), which keeps every pre-Step-5 test inert. A size-aware test
      # overrides size_class + mem_budget + mem_headroom to model a classed brick.
      size_class: Keyword.get(opts, :size_class, ""),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom, 4096),
      mem_budget_mib: Keyword.get(opts, :mem_budget, 0),
      cpu_headroom_millicores: 4000,
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.get(opts, :max, 8),
      draining: Keyword.get(opts, :draining, false),
      updated_at: Keyword.get(opts, :updated_at, 1_000_000)
    })
  end

  defp put_catalog(ctx, wl, opts \\ []) do
    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      cap: Keyword.get(opts, :cap, 10),
      floor: Keyword.get(opts, :floor, 0),
      mem_mib: Keyword.get(opts, :mem_mib, 0),
      invoke_path: "/",
      timeout_ms: 5_000,
      result_ttl_ms: 60_000,
      result_max_bytes: Keyword.get(opts, :result_max_bytes, 1_048_576),
      retry: Keyword.get(opts, :retry, %{max_attempts: 3, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:transport, :timeout, :guest5xx]}),
      triggers: []
    })
  end

  defp submit(ctx, wl, principal) do
    {:ok, :created, tid} =
      TaskStore.submit(ctx.store, %{
        tenant: "t",
        principal: principal,
        workload: wl,
        request: %{path: "/", headers: %{}, body_b64: Base.encode64("")}
      })

    tid
  end

  defp state_of(ctx, tid) do
    case TaskStore.get(ctx.store, tid) do
      {:ok, %{state: s}} -> s
      _ -> :missing
    end
  end

  defp eventually(fun, timeout \\ 5_000, interval \\ 10) do
    deadline = System.monotonic_time(:millisecond) + timeout
    do_eventually(fun, deadline, interval)
  end

  defp do_eventually(fun, deadline, interval) do
    if fun.() do
      true
    else
      if System.monotonic_time(:millisecond) >= deadline do
        false
      else
        Process.sleep(interval)
        do_eventually(fun, deadline, interval)
      end
    end
  end

  # -- warm + miss -----------------------------------------------------------

  test "warm dispatch drives a queued task to succeeded and stores the result" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 1)
    Dispatcher.deposit(ctx.name, "node-4", "wl-a", "vm-1")

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    assert {:ok, %{status_code: 200, body: "ok"}} = TaskStore.get_result(ctx.store, tid)
    assert Dispatcher.stats(ctx.name).warm_hits >= 1
  end

  test "a warm vm on a co-located, non-newest brick is found (no prefer-newest collapse)" do
    # Two bricks share node-4 (brick-capacity: a node legitimately holds several
    # instances). Brick A is the NEWEST instance (higher updated_at) but holds no
    # warm VM; brick B is older but has a primed VM deposited. The removed
    # prefer_newest_per_node collapse kept only the newest instance PER NODE, which
    # would have dropped B and forced a miss on A. With the collapse gone both
    # bricks are candidates, so dispatch warms on B (warm_hits >= 1). If the
    # collapse were still present this would be a miss and warm_hits would be 0.
    ctx = start_stack(stale_after_ms: 60_000)
    put_catalog(ctx, "wl-a", cap: 10)

    put_facts(ctx, "wl-a",
      key: {"node-4", "a"},
      instance_id: "node-4/a",
      updated_at: 1_000_000,
      live: 1,
      max: 8
    )

    put_facts(ctx, "wl-a", key: {"node-4", "b"}, instance_id: "node-4/b", updated_at: 990_000)
    Dispatcher.deposit(ctx.name, "node-4/b", "wl-a", "vm-b")

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    assert Dispatcher.stats(ctx.name).warm_hits >= 1
  end

  test "a guest response's headers survive into the stored succeeded result" do
    guest_headers = %{"content-type" => "image/png", "x-custom" => "yes"}

    resp = %AssignResponse{
      response: %GuestResponse{status_code: 200, headers: guest_headers, body: "PNGDATA"},
      usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}
    }

    # Inject an assign_fun that returns the header-carrying response (start_stack's
    # default returns the headerless success_resp/0).
    ctx = start_stack(assign_fun: fn _ch, _req -> {:ok, resp} end)
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 1)
    Dispatcher.deposit(ctx.name, "node-4", "wl-a", "vm-1")

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)

    assert {:ok, %{status_code: 200, body: "PNGDATA", headers: headers}} =
             TaskStore.get_result(ctx.store, tid)

    assert headers["content-type"] == "image/png"
    assert headers["x-custom"] == "yes"
  end

  test "adopts a node-reported primed vm into an empty inventory (control-plane restart recovery)" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)

    # The restart deadlock: the node is AT its live cap with primed VMs from a
    # prior control-plane incarnation (live == max, so no miss budget), and our
    # inventory is empty (we never learned their vm_ids). Without adoption this is
    # a permanent :no_capacity park. Adoption reads the node-reported primed vm_id
    # and seeds inventory, so the queued task warm-dispatches to the running VM.
    put_facts(ctx, "wl-a", free: 1, primed_ids: ["vm-orphan-1"], live: 8, max: 8)

    tid = submit(ctx, "wl-a", "p1")
    # sweep runs adopt_inventory before draining (the periodic + boot path).
    Dispatcher.sweep(ctx.name)

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    assert Dispatcher.stats(ctx.name).warm_hits >= 1
  end

  test "adoption does not double-enqueue a vm already in inventory (dedup)" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    # vm-1 is BOTH reported primed by the node AND already deposited (the Prime-
    # then-deposit window where both the cast and adoption surface the same VM).
    # No tasks, so nothing consumes it: inventory must hold exactly one copy.
    put_facts(ctx, "wl-a", free: 1, primed_ids: ["vm-1"], live: 8, max: 8)
    Dispatcher.deposit(ctx.name, "node-4", "wl-a", "vm-1")

    # Adoption runs on every sweep; vm-1 is already known, so re-adopting must not
    # grow the inventory (a double-add would let two tasks assign one single-use VM).
    Dispatcher.sweep(ctx.name)
    Dispatcher.sweep(ctx.name)

    assert Dispatcher.stats(ctx.name).inventory[{"node-4", "wl-a"}] == 1
  end

  test "a miss worker's just-primed vm is not re-adopted while its assign is in flight" do
    gate = new_gate()
    parent = self()

    ctx =
      start_stack(
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-miss-1"}} end,
        assign_fun: fn _ch, _req ->
          send(parent, :at_assign)
          wait_open(gate)
          {:ok, success_resp()}
        end
      )

    put_catalog(ctx, "wl-a", cap: 10)
    # The node reports vm-miss-1 primed (the window between the worker priming it
    # and the node dropping it on assign); inventory is empty so the task misses
    # and primes it.
    put_facts(ctx, "wl-a", free: 1, primed_ids: ["vm-miss-1"], live: 0, max: 8)

    _tid = submit(ctx, "wl-a", "p1")
    assert_receive :at_assign, 2000

    # The worker is blocked at assign; ensure_vm sent {:vm_primed} before it, so
    # this sweep (enqueued after that message) sees meta.vm_id set and adoption
    # must SKIP vm-miss-1 rather than re-adopt the in-flight miss VM into inventory.
    Dispatcher.sweep(ctx.name)
    refute Map.has_key?(Dispatcher.stats(ctx.name).inventory, {"node-4", "wl-a"})

    open_gate(gate)
  end

  test "miss path primes then assigns, counted as a miss" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 0, live: 0, max: 8)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    stats = Dispatcher.stats(ctx.name)
    assert stats.misses >= 1
    assert stats.warm_hits == 0
  end

  test "a miss commits and retries in Score.order order" do
    parent = self()
    suffix =
      Enum.find(0..1_024, fn n ->
        :erlang.phash2("wl-test-ordering-#{n}", 2) == 0
      end)

    wl = "wl-test-ordering-#{suffix}"
    flat_rotation = :erlang.phash2(wl, 2)
    assert flat_rotation == 0

    ctx =
      start_stack(
        stale_after_ms: 60_000,
        channel_fun: fn instance ->
          send(parent, {:miss_dialed, instance})
          {:ok, instance}
        end
      )

    put_catalog(ctx, wl, cap: 10, mem_mib: 1_000)

    put_facts(ctx, wl,
      key: {"node-4", "apple"},
      instance_id: "apple",
      size_class: "8gi",
      mem_budget: 8_192,
      mem_headroom: 8_192,
      free: 0,
      live: 0,
      max: 8
    )

    put_facts(ctx, wl,
      key: {"node-4", "zoo"},
      instance_id: "zoo",
      size_class: "8gi",
      mem_budget: 8_192,
      mem_headroom: 8_192,
      free: 0,
      live: 1,
      max: 8
    )

    candidates = [
      %{instance_id: "apple", size_class: "8gi", mem_budget_mib: 8_192, mem_headroom_mib: 8_192, live_vms: 0, max_live_vms: 8},
      %{instance_id: "zoo", size_class: "8gi", mem_budget_mib: 8_192, mem_headroom_mib: 8_192, live_vms: 1, max_live_vms: 8}
    ]

    score_ids = Score.order(candidates, wl) |> Enum.map(& &1.instance_id)
    flat_ids =
      candidates
      |> Enum.sort_by(& &1.instance_id)
      |> then(fn sorted ->
        {head, tail} = Enum.split(sorted, flat_rotation)
        Enum.map(tail ++ head, & &1.instance_id)
      end)

    assert score_ids == ["zoo", "apple"]
    assert flat_ids == ["apple", "zoo"]
    assert score_ids != flat_ids
    [score_head | _] = score_ids

    tid = submit(ctx, wl, "p1")
    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    assert_receive {:miss_dialed, ^score_head}, 2_000
  end

  # -- size-aware miss placement (Step 5) ------------------------------------

  test "a miss with a too-small co-located brick + a big brick primes on the big one" do
    parent = self()

    # The channel_fun records which INSTANCE the miss worker dialed, so we can assert
    # the miss never landed on the too-small 2Gi brick (pick_node passes the chosen
    # instance_id as ctx.node_id, which the worker dials via channel_fun).
    ctx =
      start_stack(
        stale_after_ms: 60_000,
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-big"}} end,
        channel_fun: fn node ->
          send(parent, {:dialed, node})
          {:ok, :ch}
        end
      )

    # A 4Gi-need workload; a 2Gi brick (100 MiB headroom) cannot boot it, an 8Gi brick can.
    put_catalog(ctx, "wl-a", cap: 10, mem_mib: 4_000)

    put_facts(ctx, "wl-a",
      key: {"node-4", "small"},
      instance_id: "node-4/small",
      size_class: "2gi",
      mem_budget: 2_048,
      mem_headroom: 100,
      free: 0,
      live: 0,
      max: 8
    )

    put_facts(ctx, "wl-a",
      key: {"node-4", "big"},
      instance_id: "node-4/big",
      size_class: "8gi",
      mem_budget: 8_192,
      mem_headroom: 8_000,
      free: 0,
      live: 0,
      max: 8
    )

    tid = submit(ctx, "wl-a", "p1")
    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    assert_receive {:dialed, "node-4/big"}, 2_000
    refute_received {:dialed, "node-4/small"}
  end

  test "a miss where the ONLY brick is too small denies :no_capacity (never a bad placement)" do
    ctx = start_stack(stale_after_ms: 60_000)
    put_catalog(ctx, "wl-a", cap: 10, mem_mib: 4_000)

    put_facts(ctx, "wl-a",
      key: {"node-4", "small"},
      instance_id: "node-4/small",
      size_class: "2gi",
      mem_budget: 2_048,
      mem_headroom: 100,
      free: 0,
      live: 0,
      max: 8
    )

    tid = submit(ctx, "wl-a", "p1")
    # Never dispatched: stays queued, denial is :no_capacity (not a too-small prime).
    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.no_capacity >= 1 end)
    assert state_of(ctx, tid) == :queued
  end

  # -- reject/retry placement (ADR 014 decision 3) ---------------------------

  # Seed two co-located big bricks on one node so the miss frontier has a real
  # next candidate to retry onto.
  defp two_colocated_bricks(ctx, wl) do
    put_catalog(ctx, wl, cap: 10, mem_mib: 4_000)

    for {pod, iid} <- [{"a", "node-4/a"}, {"b", "node-4/b"}] do
      put_facts(ctx, wl,
        key: {"node-4", pod},
        instance_id: iid,
        size_class: "8gi",
        mem_budget: 8_192,
        mem_headroom: 8_000,
        free: 0,
        live: 0,
        max: 8
      )
    end
  end

  test "gate ON: first brick rejects Prime under pressure -> miss retries the second brick" do
    System.put_env("EMBERVM_PLACEMENT_RETRY", "1")
    on_exit(fn -> System.delete_env("EMBERVM_PLACEMENT_RETRY") end)

    parent = self()
    # Reject the FIRST brick Primed (whichever the frontier head is, which depends on
    # the workload hash), succeed the second: the retry then always fires regardless
    # of which co-located brick the deterministic pick puts first.
    {:ok, primes} = Agent.start_link(fn -> 0 end)

    ctx =
      start_stack(
        stale_after_ms: 60_000,
        # Channel echoes the dialed instance so assign_fun can assert the winner.
        channel_fun: fn instance ->
          send(parent, {:dialed, instance})
          {:ok, instance}
        end,
        prime_fun: fn channel, _req ->
          n = Agent.get_and_update(primes, fn n -> {n, n + 1} end)

          if n == 0 do
            {:error, %GRPC.RPCError{status: 8, message: "noded: pressure:mem"}}
          else
            {:ok, %PrimeResponse{vm_id: "vm-on-#{channel}"}}
          end
        end,
        # Assign must dial the WINNING brick's channel (the second one primed),
        # proving the retry re-pointed the Assign, not just the Prime.
        assign_fun: fn channel, _req ->
          send(parent, {:assigned_on, channel})
          {:ok, success_resp()}
        end
      )

    two_colocated_bricks(ctx, "wl-a")

    tid = submit(ctx, "wl-a", "p1")
    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)

    # BOTH bricks were dialed for Prime (the head rejected, the retry hit the other).
    assert_receive {:dialed, first}, 2_000
    assert_receive {:dialed, second}, 2_000
    assert Enum.sort([first, second]) == ["node-4/a", "node-4/b"]
    # The Assign landed on the SECOND brick (the one that actually primed), proving
    # the retry re-pointed the Assign channel to the winning brick, not the head
    # (mandatory node-reassignment assertion #1: Assign dials B).
    assert_receive {:assigned_on, assigned_on}, 2_000
    assert assigned_on == second
    assert assigned_on != first
  end

  test "gate ON: after a cross-brick retry, meta.node_id follows the VM to B and A never double-adopts" do
    # This is THE node-reassignment accounting test (ADR 014 decision 3): on a retry
    # onto brick B, the worker's meta.node_id must become B (so reconcile keys against
    # the brick that actually holds the VM), and the originally-attempted brick A must
    # NOT retain the VM in adopt inventory (no double-adopt). We hold the worker at
    # Assign with a gate so the post-Prime state is inspectable mid-flight.
    System.put_env("EMBERVM_PLACEMENT_RETRY", "1")
    on_exit(fn -> System.delete_env("EMBERVM_PLACEMENT_RETRY") end)

    parent = self()
    gate = new_gate()
    # Reject the FIRST brick dialed and succeed the SECOND, regardless of which the
    # frontier hash puts first (hash-agnostic). The winner's identity is encoded into
    # the vm_id (vm-on-<channel>) AND the dialed order is reported, so the assertions
    # name B by what actually primed, never by guessing the hash.
    {:ok, primes} = Agent.start_link(fn -> 0 end)

    ctx =
      start_stack(
        stale_after_ms: 60_000,
        channel_fun: fn instance ->
          send(parent, {:dialed, instance})
          {:ok, instance}
        end,
        prime_fun: fn channel, _req ->
          n = Agent.get_and_update(primes, fn n -> {n, n + 1} end)

          if n == 0 do
            {:error, %GRPC.RPCError{status: 8, message: "noded: pressure:mem"}}
          else
            {:ok, %PrimeResponse{vm_id: "vm-on-#{channel}"}}
          end
        end,
        assign_fun: fn _ch, _req ->
          send(parent, :at_assign)
          wait_open(gate)
          {:ok, success_resp()}
        end
      )

    two_colocated_bricks(ctx, "wl-a")

    _tid = submit(ctx, "wl-a", "p1")

    # A (first dial) rejected, B (second dial) primed. Capture both so B is named by
    # what actually happened, not by the hash.
    assert_receive {:dialed, _brick_a}, 2_000
    assert_receive {:dialed, brick_b}, 2_000
    # Worker is now blocked at Assign, after {:vm_primed, "vm-on-<B>", B} was sent.
    assert_receive :at_assign, 2_000

    vm_on_b = "vm-on-#{brick_b}"

    stats = Dispatcher.stats(ctx.name)
    # #2: meta.node_id followed the VM to B (the brick that actually primed), not the
    # committed head. worker_nodes is keyed by vm_id -> node_id.
    assert Map.get(stats.worker_nodes, vm_on_b) == brick_b

    # #3: no double-adopt. B reports vm-on-<B> primed (the in-flight window); a sweep
    # must SKIP it (known via meta.vm_id) and adopt it under NO instance's inventory.
    put_facts(ctx, "wl-a",
      key: {"node-4", "b"},
      instance_id: brick_b,
      size_class: "8gi",
      mem_budget: 8_192,
      mem_headroom: 8_000,
      free: 1,
      primed_ids: [vm_on_b],
      live: 0,
      max: 8
    )

    Dispatcher.sweep(ctx.name)
    stats2 = Dispatcher.stats(ctx.name)
    refute Map.has_key?(stats2.inventory, {"node-4/a", "wl-a"})
    refute Map.has_key?(stats2.inventory, {"node-4/b", "wl-a"})

    open_gate(gate)
  end

  test "gate OFF: a Prime rejection never advances placement to the sibling brick" do
    # Gate unset (default off): Retry.run makes exactly ONE placement attempt, so a
    # RESOURCE_EXHAUSTED on the committed head must NEVER advance to the sibling
    # co-located brick. The task itself may re-dispatch (a transport failure is
    # retryable at the TASK level, orthogonal to placement retry), and each
    # re-dispatch re-dials the SAME deterministic head; that is expected. The
    # gate-off property is specifically "placement does not advance to the other
    # candidate", so we assert the SIBLING is never dialed rather than "only one
    # dial ever" (which task re-dispatch would spuriously trip).
    System.delete_env("EMBERVM_PLACEMENT_RETRY")

    parent = self()

    ctx =
      start_stack(
        stale_after_ms: 60_000,
        channel_fun: fn instance ->
          send(parent, {:dialed, instance})
          {:ok, instance}
        end,
        # Every brick rejects, so if placement ever advanced it WOULD dial the
        # sibling. Gate-off must not.
        prime_fun: fn _channel, _req ->
          {:error, %GRPC.RPCError{status: 8, message: "noded: pressure:mem"}}
        end,
        assign_fun: fn _ch, _req -> {:ok, success_resp()} end
      )

    two_colocated_bricks(ctx, "wl-a")

    _tid = submit(ctx, "wl-a", "p1")

    # The head brick is dialed (deterministic hash); capture it, derive the sibling,
    # and assert the sibling is NEVER dialed (placement did not advance under gate off).
    assert_receive {:dialed, head}, 2_000
    sibling = if head == "node-4/a", do: "node-4/b", else: "node-4/a"
    refute_receive {:dialed, ^sibling}, 500
  end

  test "gate OFF: the single Prime succeeds on the committed brick and meta.node_id is that brick" do
    # Mandatory node-reassignment assertion #4 (the node_id half): gate off, the ONE
    # attempt lands on the committed head, meta.node_id == that brick, byte-for-byte
    # today's single-attempt behaviour. Held at Assign so meta is inspectable.
    System.delete_env("EMBERVM_PLACEMENT_RETRY")

    parent = self()
    gate = new_gate()

    ctx =
      start_stack(
        stale_after_ms: 60_000,
        channel_fun: fn instance ->
          send(parent, {:dialed, instance})
          {:ok, instance}
        end,
        prime_fun: fn channel, _req -> {:ok, %PrimeResponse{vm_id: "vm-on-#{channel}"}} end,
        assign_fun: fn _ch, _req ->
          send(parent, :at_assign)
          wait_open(gate)
          {:ok, success_resp()}
        end
      )

    two_colocated_bricks(ctx, "wl-a")

    _tid = submit(ctx, "wl-a", "p1")

    # Exactly one dial (the committed head); no retry even though a second brick exists.
    assert_receive {:dialed, committed}, 2_000
    refute_receive {:dialed, _second}, 300
    assert_receive :at_assign, 2_000

    stats = Dispatcher.stats(ctx.name)
    # meta.node_id is the committed brick (== dial_id, no reassignment when nothing
    # rejected), exactly as before this change.
    assert Map.get(stats.worker_nodes, "vm-on-#{committed}") == committed

    open_gate(gate)
  end

  # -- fail-closed -----------------------------------------------------------

  test "no capacity facts: task stays queued, denial recorded as :no_capacity" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    # No facts at all.

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.no_capacity > 0 end)
    Process.sleep(50)
    assert state_of(ctx, tid) == :queued
  end

  test "stale capacity (> 15s since stamp): denial recorded as :stale_capacity" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    # Base ready, but stamped 20s before the dispatcher's clock (1_000_000).
    put_facts(ctx, "wl-a", free: 1, updated_at: 1_000_000 - 20_000)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.stale_capacity > 0 end)
    assert state_of(ctx, tid) == :queued
  end

  test "draining node is not dispatchable (fail-closed)" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 1, draining: true)

    tid = submit(ctx, "wl-a", "p1")
    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.no_capacity > 0 end)
    assert state_of(ctx, tid) == :queued
  end

  # -- caps ------------------------------------------------------------------

  test "per-workload cap bounds in-flight tasks" do
    gate = new_gate()
    ctx = start_stack(assign_fun: gated_assign(gate))
    put_catalog(ctx, "wl-a", cap: 2)
    put_facts(ctx, "wl-a", live: 0, max: 8)

    tids = for _ <- 1..5, do: submit(ctx, "wl-a", "p1")

    # Exactly cap (2) go in-flight; the rest wait queued.
    assert eventually(fn -> Map.get(Dispatcher.stats(ctx.name).inflight_wl, "wl-a") == 2 end)
    assert Dispatcher.stats(ctx.name).queued == 3
    assert Dispatcher.stats(ctx.name).denials.cap > 0

    open_gate(gate)
    assert eventually(fn -> Enum.all?(tids, &(state_of(ctx, &1) == :succeeded)) end)
  end

  test "per-principal share cap splits a workload's cap across principals" do
    gate = new_gate()
    ctx = start_stack(assign_fun: gated_assign(gate))
    # cap 4, two active principals -> share = 2 each.
    put_catalog(ctx, "wl-a", cap: 4)

    # A has 5 tasks, B has 1. Queue BOTH before granting capacity so both are
    # active at dispatch time (otherwise A, submitted first, greedily takes the
    # whole cap while it is the only active principal, share = cap/1). With share
    # 2, A is held to 2 in-flight even though 4 slots exist; B takes its 1. So the
    # in-flight split is A:2, B:1 (not A:4).
    for _ <- 1..5, do: submit(ctx, "wl-a", "A")
    submit(ctx, "wl-a", "B")
    assert eventually(fn -> Dispatcher.stats(ctx.name).queued == 6 end)

    put_facts(ctx, "wl-a", live: 0, max: 16)
    Dispatcher.sweep(ctx.name)

    assert eventually(fn ->
             ifp = Dispatcher.stats(ctx.name).inflight_pr
             Map.get(ifp, {"wl-a", "A"}) == 2 and Map.get(ifp, {"wl-a", "B"}) == 1
           end)

    open_gate(gate)
    assert eventually(fn -> Dispatcher.stats(ctx.name).queued == 0 end)
  end

  # -- fairness (round-robin across principals, FIFO within) -----------------

  test "dispatch order is round-robin across principals, FIFO within a principal" do
    {:ok, order} = Agent.start_link(fn -> [] end)

    record_assign = fn _ch, %AssignRequest{trace: %Trace{task_id: tid}} ->
      Agent.update(order, &(&1 ++ [tid]))
      {:ok, success_resp()}
    end

    # cap 1 serializes dispatch so the recorded call order IS the dispatch order.
    ctx = start_stack(assign_fun: record_assign)
    put_catalog(ctx, "wl-a", cap: 1)

    # Submit with NO capacity first so all six queue before any dispatch, giving a
    # clean steady-state (both principals always have work) rather than a startup
    # artifact where one principal is served while alone.
    a = for _ <- 1..3, do: submit(ctx, "wl-a", "A")
    b = for _ <- 1..3, do: submit(ctx, "wl-a", "B")
    principal = Map.new(Enum.map(a, &{&1, "A"}) ++ Enum.map(b, &{&1, "B"}))

    # Wait until all six enqueue casts (in submit order) are processed, so the
    # fair-queue order is the submit order and not a random-task-id backlog sort.
    assert eventually(fn -> Dispatcher.stats(ctx.name).queued == 6 end)

    # Now grant capacity and kick a drain (the tasks are already queued, so the
    # sweep only triggers the drain; do_enqueue dedupes them).
    put_facts(ctx, "wl-a", live: 0, max: 8)
    Dispatcher.sweep(ctx.name)

    assert eventually(fn -> length(Agent.get(order, & &1)) == 6 end)

    principals = order |> Agent.get(& &1) |> Enum.map(&Map.get(principal, &1))
    assert principals == ["A", "B", "A", "B", "A", "B"]

    # And FIFO within a principal: A's three tasks ran in submit order.
    a_order = order |> Agent.get(& &1) |> Enum.filter(&(Map.get(principal, &1) == "A"))
    assert a_order == a
  end

  # -- drain integrity -------------------------------------------------------

  test "1000 tasks drain with zero lost, all terminal" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 200)
    put_facts(ctx, "wl-a", live: 0, max: 1_000_000)

    tids = for _ <- 1..1000, do: submit(ctx, "wl-a", "p1")

    assert eventually(
             fn ->
               s = Dispatcher.stats(ctx.name)
               s.queued == 0 and s.workers == 0 and map_size(s.inflight_wl) == 0
             end,
             30_000
           )

    succeeded = Enum.count(tids, &(state_of(ctx, &1) == :succeeded))
    assert succeeded == 1000
    assert {:ok, []} = TaskStore.list_backlog(ctx.store)
  end

  # -- orphan reclaim (restart / partial commit) -----------------------------

  test "sweep reclaims an orphaned in-flight task with no live worker" do
    suffix = System.unique_integer([:positive])
    cap_table = :"ocap_#{suffix}"
    cat_table = :"ocat_#{suffix}"
    depth_table = :"odepth_#{suffix}"
    disp_name = :"odisp_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)
    path = Path.join(System.tmp_dir!(), "embervm_orphan_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = TaskStore.start_link(name: nil, op_log: op_log, on_queued: fn t -> Dispatcher.enqueue(disp_name, t) end)

    # A task durably `assigned` from a "previous incarnation": no dispatcher owned
    # it (the one below starts fresh, so it has no worker tracking it). This models
    # a dispatcher restart or a partial assign/start commit.
    {:ok, :created, tid} =
      TaskStore.submit(store, %{tenant: "t", principal: "p1", workload: "wl-a", request: %{path: "/", headers: %{}, body_b64: Base.encode64("")}})

    {:ok, %{state: :assigned}} = TaskStore.assign(store, tid)

    WorkloadCatalog.upsert(cat_table, "wl-a", %{
      name: "wl-a",
      namespace: "embervm",
      cap: 10,
      floor: 0,
      invoke_path: "/",
      timeout_ms: 5_000,
      result_ttl_ms: 60_000,
      result_max_bytes: 1_048_576,
      retry: %{max_attempts: 3, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:transport]},
      triggers: []
    })

    NodeCapacity.put(cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{"wl-a" => %{free_primed_slots: 0, snapshot_ref: "snap", base_state: :BASE_BUILD_STATE_READY}},
      mem_headroom_mib: 4096,
      cpu_headroom_millicores: 4000,
      live_vms: 0,
      max_live_vms: 8,
      draining: false,
      updated_at: 1_000_000
    })

    {:ok, _disp} =
      Dispatcher.start_link(
        name: disp_name,
        task_store: store,
        capacity_table: cap_table,
        catalog_table: cat_table,
        depth_table: depth_table,
        clock: fn -> 1_000_000 end,
        channel_fun: fn _ -> {:ok, :ch} end,
        assign_fun: fn _ch, _req -> {:ok, success_resp()} end,
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-1"}} end,
        start_sweep: false
      )

    Dispatcher.sweep(disp_name)

    # Reclaimed: failed as transport -> retried -> re-dispatched -> succeeded.
    assert eventually(fn -> match?({:ok, %{state: :succeeded}}, TaskStore.get(store, tid)) end)
  end

  # -- failure classification ------------------------------------------------

  test "guest 5xx retries through Retry until attempts exhaust, then dead-letters" do
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    fail_5xx = fn _ch, _req ->
      Agent.update(attempts, &(&1 + 1))
      {:ok, success_resp(503, "boom")}
    end

    ctx = start_stack(assign_fun: fail_5xx)
    put_catalog(ctx, "wl-a", cap: 10, retry: %{max_attempts: 3, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:guest5xx]})
    put_facts(ctx, "wl-a", live: 0, max: 8)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :dead_lettered end)
    # Three attempts (1..max_attempts), each classified retryable until the last.
    assert Agent.get(attempts, & &1) == 3
  end

  test "guest 4xx is permanent (never retried)" do
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    fail_4xx = fn _ch, _req ->
      Agent.update(attempts, &(&1 + 1))
      {:ok, success_resp(400, "bad")}
    end

    ctx = start_stack(assign_fun: fail_4xx)
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", live: 0, max: 8)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :dead_lettered end)
    assert Agent.get(attempts, & &1) == 1
  end

  # -- admit? 429 gate -------------------------------------------------------

  test "admit? denies once the per-principal queue-depth cap is reached" do
    ctx = start_stack(queue_depth_cap: 2)
    put_catalog(ctx, "wl-a", cap: 10)
    # No capacity, so submitted tasks stay queued and keep charging depth.

    assert Dispatcher.admit?(ctx.depth_table, "wl-a", "p1")
    submit(ctx, "wl-a", "p1")
    submit(ctx, "wl-a", "p1")

    assert eventually(fn -> not Dispatcher.admit?(ctx.depth_table, "wl-a", "p1") end)
    # A different principal is unaffected.
    assert Dispatcher.admit?(ctx.depth_table, "wl-a", "p2")
  end

  # -- gate helpers ----------------------------------------------------------

  defp new_gate do
    {:ok, agent} = Agent.start_link(fn -> false end)
    agent
  end

  # -- quota enforcement at dispatch (Task 12) -------------------------------

  test "an over-budget principal is skipped in the rotation while others dispatch" do
    day5 = 5 * 86_400_000
    quota_table = :"quota_#{System.unique_integer([:positive])}"
    :ets.new(quota_table, [:set, :public, :named_table, read_concurrency: true, write_concurrency: true])
    # p1 is already over its 1.0 vCPU-second budget (2.0 charged); p2 has none.
    :ets.insert(quota_table, {{"p1", div(day5, 86_400_000)}, 2000})

    ctx =
      start_stack(
        quota_table: quota_table,
        quota_config: %{budgets: %{"p1" => 1.0}, default: nil},
        wall_clock: fn -> day5 end
      )

    put_catalog(ctx, "wl-q", cap: 10)
    put_facts(ctx, "wl-q", free: 0, max: 8)

    # Queue both principals, THEN drive a drain. p2 dispatches (miss-path prime),
    # p1 is parked for quota (not dropped: no queued->failed edge).
    over = submit(ctx, "wl-q", "p1")
    under = submit(ctx, "wl-q", "p2")

    assert eventually(fn -> state_of(ctx, under) == :succeeded end)
    # p1 stays queued (parked), never failed or dispatched.
    assert state_of(ctx, over) == :queued
    assert Dispatcher.stats(ctx.name).denials.quota > 0
  end

  # -- queued-task expiry at dispatch (ADR embervm/002) ----------------------

  test "a queued task popped past its expires_at is expired, not dispatched (no VM consumed)" do
    {:ok, assigns} = Agent.start_link(fn -> 0 end)

    count_assign = fn _ch, _req ->
      Agent.update(assigns, &(&1 + 1))
      {:ok, success_resp()}
    end

    # wall_clock is well past the task's expires_at, so at pop time the task is
    # expired to failed_permanent and never assigned; the primed VM is untouched.
    ctx = start_stack(assign_fun: count_assign, wall_clock: fn -> 2_000_000 end)
    put_catalog(ctx, "wl-x", cap: 10)
    put_facts(ctx, "wl-x", free: 1)
    Dispatcher.deposit(ctx.name, "node-4", "wl-x", "vm-1")

    {:ok, :created, tid} =
      TaskStore.submit(ctx.store, %{
        tenant: "t",
        principal: "p1",
        workload: "wl-x",
        expires_at: 1_000,
        request: %{path: "/", headers: %{}, body_b64: Base.encode64("")}
      })

    assert eventually(fn -> state_of(ctx, tid) == :failed_permanent end)
    # Never dispatched: no assign call, so the primed VM was not consumed.
    Process.sleep(50)
    assert Agent.get(assigns, & &1) == 0
    assert Map.get(Dispatcher.stats(ctx.name).inventory, {"node-4", "wl-x"}) == 1
  end

  defp open_gate(agent), do: Agent.update(agent, fn _ -> true end)

  # An assign_fun that blocks (holding the task in-flight) until the gate opens,
  # so a test can observe steady-state in-flight counts before completion.
  defp gated_assign(gate) do
    fn _ch, _req ->
      wait_open(gate)
      {:ok, success_resp()}
    end
  end

  defp wait_open(gate) do
    if Agent.get(gate, & &1) do
      :ok
    else
      Process.sleep(5)
      wait_open(gate)
    end
  end
end
