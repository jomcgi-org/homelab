defmodule Embervm.StatefulManagerTest do
  @moduledoc """
  Exercises Embervm.StatefulManager (the L4 wake brain) against a real
  StatefulStore + op-log, an injected StartStateful/StopStateful seam, and
  injected catalog/capacity tables. Covers the wake round-trip (park -> wake
  -> publish -> endpoint returned), single-flight (N concurrent wakes on the
  same workload => exactly ONE StartStateful, N replies), the per-workload
  wake-rate limit + parked-connection cap (each with an audited denial op),
  wake-failure retry-ability (a failed wake leaves the activator able to
  retry), plan_wake's relight-vs-cold decision (valid pair relights, broken
  pair evicts + cold-boots, no bundle => fresh/cold), and the restart adoption
  matrix (a live VM is adopted, a bundle-only instance heals to banked, a
  vanished instance fails, a disconnected node's instance is left untouched).
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, StatefulManager, StatefulStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.StartStatefulResponse

  defmodule FakePublisher do
    use GenServer
    def start_link, do: GenServer.start_link(__MODULE__, 0)
    def count(pid), do: GenServer.call(pid, :count)
    @impl true
    def init(n), do: {:ok, n}
    @impl true
    def handle_cast(:publish, n), do: {:noreply, n + 1}
    @impl true
    def handle_call(:count, _from, n), do: {:reply, n, n}
  end

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"mcap_#{suffix}"
    cat_table = :"mcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_statefulmgr_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = StatefulStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)
    {:ok, pub} = FakePublisher.start_link()

    {:ok, starts} = Agent.start_link(fn -> 0 end)

    start_stateful_fun =
      Keyword.get(opts, :start_stateful_fun, fn _ch, _req ->
        Agent.update(starts, &(&1 + 1))
        if sleep = opts[:start_sleep_ms], do: Process.sleep(sleep)

        {:ok,
         %StartStatefulResponse{
           vm_id: "vm-woken-#{System.unique_integer([:positive])}",
           ip: "10.88.0.5",
           port: 5432,
           generation: 1,
           was_relight: false
         }}
      end)

    {:ok, secret_reads} = Agent.start_link(fn -> [] end)

    get_secret_fun =
      Keyword.get(opts, :get_secret_fun, fn namespace, name ->
        Agent.update(secret_reads, &[{namespace, name} | &1])
        {:ok, %{"POSTGRES_PASSWORD" => "hunter2"}}
      end)

    mgr_opts =
      [
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        clock: Keyword.get(opts, :clock, fn -> 1_000 end),
        channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
        invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _node, _chan -> :ok end),
        start_stateful_fun: start_stateful_fun,
        stop_stateful_fun: Keyword.get(opts, :stop_stateful_fun, fn _ch, _req -> {:ok, %Embervm.Node.V1.StopStatefulResponse{}} end),
        delete_volume_fun: Keyword.get(opts, :delete_volume_fun, fn _ch, _req -> {:ok, %{}} end),
        get_secret_fun: get_secret_fun,
        op_log: op_log,
        reconcile_interval_ms: 0,
        id_fun: id_seq(suffix)
      ] ++
        Keyword.take(opts, [
          :wake_max,
          :wake_window_ms,
          :park_cap,
          :wake_bound_ms,
          :mono_clock,
          :wake_timeout_margin_ms,
          :restore_artifact_fun,
          :evict_artifact_fun,
          :node_confirmed_destroy,
          :destroying_alarm_ms,
          :orphan_grace_ms
        ])

    {:ok, mgr} = StatefulManager.start_link(mgr_opts)

    %{
      mgr: mgr,
      store: store,
      cap_table: cap_table,
      cat_table: cat_table,
      starts: starts,
      pub: pub,
      op_log: op_log,
      secret_reads: secret_reads
    }
  end

  defp id_seq(suffix) do
    {:ok, counter} = Agent.start_link(fn -> 0 end)
    fn -> "stf-#{suffix}-#{Agent.get_and_update(counter, fn n -> {n + 1, n + 1} end)}" end
  end

  # Flush the manager mailbox (a :sys call is processed after every already-queued
  # message). An auto-wake dispatched by reconcile reports {:wake_done} from a
  # spawned worker asynchronously, so a test asserting its outcome polls: flush,
  # check, retry.
  defp flush_mgr(ctx), do: :sys.get_state(ctx.mgr)

  # Number of callers currently parked (owner + parkers) for a workload. Reads the
  # manager's `waking` map directly so a test can poll for a wake to register as
  # in-flight instead of guessing with a fixed Process.sleep.
  defp waiter_count(ctx, workload),
    do: length(Map.get(:sys.get_state(ctx.mgr).waking, workload, []))

  defp poll_mgr(ctx, fun, tries \\ 50) do
    flush_mgr(ctx)

    cond do
      fun.() -> :ok
      tries <= 0 -> flunk("poll_mgr: condition never held")
      true ->
        # Yield a scheduler slot between tries. flush_mgr only drains the
        # manager's own mailbox; a spawned {:wake_done} worker is a separate
        # process that must actually be scheduled before the condition can hold.
        # Without this backoff the loop spins through all tries in a couple of ms
        # and starves the worker under the 8-way parallel CI runner.
        Process.sleep(10)
        poll_mgr(ctx, fun, tries - 1)
    end
  end

  defp stateful_workload(ctx, name, extra \\ %{}) do
    # `:resources` promotes to the TOP-LEVEL entry (where resource_spec/1 reads
    # mem_mib/vcpus); everything else merges into the stateful cfg.
    {resources, extra} = Map.pop(extra, :resources)

    stateful_cfg =
      Map.merge(
        %{
          port: 5432,
          listen_port: 9100,
          volume_size_gib: 1,
          volume_mount_path: "/data",
          idle_bank_seconds: 300,
          max_lifetime_seconds: 86_400,
          banked_ttl_seconds: 604_800,
          wake_timeout_seconds: 60,
          secret_ref: nil
        },
        extra
      )

    entry = %{class: "stateful", namespace: "embervm-workloads", stateful: stateful_cfg}
    entry = if resources, do: Map.put(entry, :resources, resources), else: entry

    WorkloadCatalog.upsert(ctx.cat_table, name, entry)
  end

  defp stateful_node(ctx, node_id, opts \\ []) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      # CPU-vendor fact (Bug B): stamped onto a vendor-bound restore ref (STATEFUL),
      # left off a VOLUME restore (vendor-portable).
      cpu_vendor: Keyword.get(opts, :cpu_vendor, "amd"),
      serving_subnet_cidr: "10.88.0.0/24",
      max_live_vms: 4,
      live_vms: 0,
      workloads: %{
        # snapshot_ref is the cold-boot source for a stateful workload: an
        # image-lane guest has no serving handler artifact, so boot_image_ref is
        # the base snapshot key the daemon resolves against its base registry.
        "wl-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap-a"}
      },
      stateful_vms: Keyword.get(opts, :stateful_vms, []),
      stateful_bundles: Keyword.get(opts, :stateful_bundles, []),
      volumes: Keyword.get(opts, :volumes, []),
      store_reachable: Keyword.get(opts, :store_reachable, false)
    })
  end

  # -- wake round-trip ---------------------------------------------------------

  test "a cold wake (first boot, no volume) FRESH-boots, publishes, and returns the endpoint" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    assert Agent.get(ctx.starts, & &1) == 1
    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :serving
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.5", port: 5432}
    assert FakePublisher.count(ctx.pub) >= 1

    {:ok, [row]} = SQLite.load_stateful_instances(ctx.op_log)
    assert row.state == "serving"
  end

  # -- generation blessing (R7, ADR embervm/011, standing decision 4) ---------

  test "plan_wake issues a monotonic blessed generation, durably op-logged BEFORE the boot request carries it" do
    {:ok, captured} = Agent.start_link(fn -> nil end)
    {:ok, op_log_ref} = Agent.start_link(fn -> nil end)

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, req ->
          # The op-log-before-dispatch fence, asserted from inside the dispatched
          # RPC itself: by the time this seam runs, the generation_blessed op for
          # req.blessed_generation must ALREADY be durable
          # (bless_wake_generation/3 appends and awaits the op-log BEFORE
          # start_wake_dispatch/5 builds this request), never the reverse.
          {:ok, ops} = SQLite.read_from(Agent.get(op_log_ref, & &1), 0)
          blessed_ops = Enum.filter(ops, &(&1.kind == :generation_blessed))
          Agent.update(captured, fn _ -> {req, blessed_ops} end)

          {:ok,
           %StartStatefulResponse{
             vm_id: "vm-woken",
             ip: "10.88.0.5",
             port: 5432,
             generation: req.blessed_generation,
             was_relight: false
           }}
        end
      )

    Agent.update(op_log_ref, fn _ -> ctx.op_log end)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    {req, blessed_ops} = Agent.get(captured, & &1)
    assert req.blessed_generation == 1
    assert Enum.any?(blessed_ops, &(&1.payload["generation"] == 1))
    # The blessing ledger lives separately from the volume row (see
    # StatefulStore's moduledoc); next_blessed_generation/2 - 1 reads the last
    # blessed value without a dedicated accessor.
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-a") - 1 == 1

    # A second wake (destroy + rewake) issues the NEXT blessed generation, strictly
    # monotonic, never repeating or resetting.
    StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
    {req2, _blessed_ops2} = Agent.get(captured, & &1)
    assert req2.blessed_generation == 2
  end

  test "an unblessed report (generation past the last blessed one, generation_blessed=false) quarantines the volume and parks the next wake" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-a") - 1 == 1
    refute StatefulStore.quarantined?(ctx.store, "wl-a")

    # A forward-unblessed generation reported by a node that is NOT the volume's
    # anchor (the wake above anchored it to node-4) is the only genuine split-brain
    # shape and quarantines: a second writer would have to hold the RWO attach that
    # ADR embervm/011 fencing forbids. (A forward report from the ANCHOR node is
    # instead adopted, see the adopt test below.)
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-9", generation: 2, generation_blessed: false})
    assert StatefulStore.quarantined?(ctx.store, "wl-a")

    # A quarantined volume's next wake must park (never place): destroy the live
    # instance and rewake, which must refuse.
    StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert {:error, {:wake_failed, :volume_quarantined}} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
  end

  test "a forward-unblessed report from the volume's OWN anchor node is adopted, not quarantined, and does not park the next wake" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-a") - 1 == 1

    # node-4 holds the fenced attach. A generation it reports past the watermark is
    # the single writer running ahead of a watermark that rewound (e.g. a CP roll),
    # not split-brain: adopt it (advance the watermark, no quarantine) rather than
    # deadlock the workload. This is ADR embervm/014's node-authoritative
    # reconciliation applied to the R7 blessing watermark.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 3, generation_blessed: false})
    refute StatefulStore.quarantined?(ctx.store, "wl-a")
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-a") == 4

    # The next wake proceeds (the deadlock is gone): destroy the live instance and
    # rewake, which must NOT refuse with :volume_quarantined.
    StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
  end

  test "a node report that agrees with the last blessed generation (or reports generation_blessed=true) never quarantines" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    # Same generation the CP blessed, reported unblessed on the wire: not PAST the
    # watermark, so no quarantine (an exact-match report is the normal case right
    # after a blessed attach, before the daemon's own blessed marker write lands
    # in a subsequent scrape).
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 1, generation_blessed: false})
    refute StatefulStore.quarantined?(ctx.store, "wl-a")

    # A report claiming the reported generation IS blessed clears any prior
    # quarantine. Quarantine here is induced from a NON-anchor node (node-9); a
    # forward jump from the node-4 anchor would be adopted, not quarantined.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-9", generation: 2, generation_blessed: false})
    assert StatefulStore.quarantined?(ctx.store, "wl-a")
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-9", generation: 2, generation_blessed: true})
    refute StatefulStore.quarantined?(ctx.store, "wl-a")
  end

  test "a never-blessed volume (pre-R7, blessed_generation nil) is grandfathered on its first report, not quarantined" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    # A pre-R7 volume: created via the durable volume_created op with no blessing
    # ledger entry at all (blessed_generation stays nil/absent).
    {:ok, _} = StatefulStore.create_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 5})
    refute StatefulStore.quarantined?(ctx.store, "wl-a")

    # A node reporting generation 5 unblessed must NOT quarantine: nil means "not
    # yet under CP governance", never "behind it" (the moduledoc's grandfather
    # rule) -- distinct from a volume the CP has already blessed at least once.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 5, generation_blessed: false})
    refute StatefulStore.quarantined?(ctx.store, "wl-a")
  end

  test "export is never planned for a quarantined volume's artifacts" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
    # Quarantine from a NON-anchor node (a node-4-anchor forward report is adopted).
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-9", generation: 2, generation_blessed: false})
    assert StatefulStore.quarantined?(ctx.store, "wl-a")

    refute Embervm.StatefulSweeper.export_allowed?(ctx.store, "wl-a")
  end

  # -- mmds_env (R4, D-R4.PR-7.1: MMDS-lite over boot-args) --------------------

  test "a FRESH/COLD wake with a secretRef reads the secret and populates mmds_env" do
    {:ok, captured} = Agent.start_link(fn -> nil end)

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, req ->
          Agent.update(captured, fn _ -> req end)

          {:ok,
           %StartStatefulResponse{
             vm_id: "vm-woken",
             ip: "10.88.0.5",
             port: 5432,
             generation: 1,
             was_relight: false
           }}
        end
      )

    stateful_workload(ctx, "wl-a", %{secret_ref: "wl-a-creds"})
    stateful_node(ctx, "node-4")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    req = Agent.get(captured, & &1)
    assert req.mmds_env == %{"POSTGRES_PASSWORD" => "hunter2"}
    assert Agent.get(ctx.secret_reads, & &1) == [{"embervm-workloads", "wl-a-creds"}]
  end

  test "a wake with no secretRef leaves mmds_env empty and never reads a secret" do
    {:ok, captured} = Agent.start_link(fn -> nil end)

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, req ->
          Agent.update(captured, fn _ -> req end)

          {:ok,
           %StartStatefulResponse{
             vm_id: "vm-woken",
             ip: "10.88.0.5",
             port: 5432,
             generation: 1,
             was_relight: false
           }}
        end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    req = Agent.get(captured, & &1)
    assert req.mmds_env == %{}
    assert Agent.get(ctx.secret_reads, & &1) == []
  end

  test "a secret-read failure fails OPEN: the wake proceeds with an empty mmds_env" do
    {:ok, captured} = Agent.start_link(fn -> nil end)

    ctx =
      start_stack(
        get_secret_fun: fn _namespace, _name -> {:error, {:apiserver_status, 404}} end,
        start_stateful_fun: fn _ch, req ->
          Agent.update(captured, fn _ -> req end)

          {:ok,
           %StartStatefulResponse{
             vm_id: "vm-woken",
             ip: "10.88.0.5",
             port: 5432,
             generation: 1,
             was_relight: false
           }}
        end
      )

    stateful_workload(ctx, "wl-a", %{secret_ref: "missing-secret"})
    stateful_node(ctx, "node-4")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    req = Agent.get(captured, & &1)
    assert req.mmds_env == %{}
  end

  test "a RELIGHT never reads the workload's secret (mmds_env stays out of the request)" do
    {:ok, captured} = Agent.start_link(fn -> nil end)

    relit_fun = fn _ch, req ->
      Agent.update(captured, fn _ -> req end)

      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-relit",
         ip: "10.88.0.5",
         port: 5432,
         generation: 3,
         was_relight: true
       }}
    end

    # get_secret_fun raises if called at all: a RELIGHT must never invoke it
    # (D-R4.PR-7.1), so any call here fails the test loudly rather than
    # silently passing with an unasserted empty read.
    ctx =
      start_stack(
        start_stateful_fun: relit_fun,
        get_secret_fun: fn _ns, _name -> raise "get_secret_fun must not be called on RELIGHT" end
      )

    stateful_workload(ctx, "wl-a", %{secret_ref: "wl-a-creds"})
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    req = Agent.get(captured, & &1)
    assert req.mode == :START_STATEFUL_MODE_RELIGHT
    assert req.mmds_env == %{}
  end

  test "an unknown (non-stateful) workload is a 404-shaped miss" do
    ctx = start_stack()
    stateful_node(ctx, "node-4")
    assert {:error, {:unknown_workload}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  test "a wake with no reporting node is :no_capacity (no volume, nowhere to place)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    # No node registered at all. plan_wake's placement error rides the generic
    # wake-failure wrap (finish_wake_failure's {:error, reason} clause), so the
    # caller sees {:error, {:wake_failed, :no_capacity}}, not the bare reason.
    assert {:error, {:wake_failed, :no_capacity}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  # -- single-flight ------------------------------------------------------------

  test "N concurrent wakes for one workload => exactly ONE StartStateful and N replies" do
    ctx = start_stack(start_sleep_ms: 60)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    n = 8

    tasks =
      for _ <- 1..n do
        Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
      end

    results = Task.await_many(tasks, 5_000)

    assert Enum.all?(results, &match?({:ok, %{ip: "10.88.0.5", port: 5432}}, &1))
    assert length(results) == n
    # Exactly one StartStateful (single-flight), which is also what KEEPS the
    # singleton invariant under a concurrent burst.
    assert Agent.get(ctx.starts, & &1) == 1
    assert [_one] = StatefulStore.list(ctx.store, "wl-a")
  end

  # -- wake-rate limit + parked cap ---------------------------------------------

  test "the per-workload wake-rate limit denies excess wakes without touching the node" do
    ctx = start_stack(wake_max: 1, start_stateful_fun: fn _ch, _req -> {:error, :boom} end)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert {:error, {:wake_rate, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  test "the parked-connection cap closes excess connections behind an in-flight wake" do
    # park_cap counts EVERY parked connection, INCLUDING the first caller (the wake
    # owner is itself a real connection waiting for the endpoint). So a cap of 3
    # admits the owner + 2 more; a fourth is denied.
    ctx = start_stack(start_sleep_ms: 200, park_cap: 3, wake_max: 1000)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    # First caller kicks the wake; two more park (filling the cap of 3 with the
    # owner); a fourth is denied.
    first = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    # Wait for the first call to register as the in-flight wake owner (waiter #1).
    poll_mgr(ctx, fn -> waiter_count(ctx, "wl-a") >= 1 end)

    parked = for _ <- 1..2, do: Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    # Wait for both parkers to register so the park (cap 3) is exactly full.
    poll_mgr(ctx, fn -> waiter_count(ctx, "wl-a") == 3 end)

    assert {:error, {:park_full, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert {:ok, _} = Task.await(first, 5_000)
    assert Enum.all?(Task.await_many(parked, 5_000), &match?({:ok, _}, &1))
    assert Agent.get(ctx.starts, & &1) == 1
  end

  # -- wake failure + retry-ability ---------------------------------------------

  test "a wake failure errors the caller and stays retryable (activator not consumed)" do
    {:ok, fail?} = Agent.start_link(fn -> true end)

    start_fun = fn _ch, _req ->
      if Agent.get(fail?, & &1) do
        {:error, :readiness_timeout}
      else
        {:ok, %StartStatefulResponse{vm_id: "vm-ok", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
      end
    end

    ctx = start_stack(start_stateful_fun: start_fun, wake_max: 100)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == nil

    Agent.update(fail?, fn _ -> false end)
    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  test "a transport-dead wake invalidates the channel so the next wake re-dials" do
    test_pid = self()
    dead = %GRPC.RPCError{status: 2, message: "error occurred while receiving data: the connection is closed"}

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, _req -> {:error, dead} end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end,
        wake_max: 100
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert_receive {:invalidated, "node-4", :ch}, 1_000
  end

  test "a wake whose channel process is dead (:noproc exit) invalidates so the next wake re-dials" do
    # A noded rollout kills the Mint ConnectionProcess behind the cached
    # channel; the gRPC stub's GenServer.call then EXITS :noproc instead of
    # returning a transport error. Observed live on demo-postgres 2026-07-18:
    # without invalidation every subsequent wake failed until a control-plane
    # restart.
    test_pid = self()

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, _req -> exit({:noproc, {GenServer, :call, [:dead_pid]}}) end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end,
        wake_max: 100
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert_receive {:invalidated, "node-4", :ch}, 1_000
  end

  # -- straggler -----------------------------------------------------------------

  test "a connection arriving while a live endpoint exists is resolved, not re-woken" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 1

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 1
  end

  # -- plan_wake: relight vs cold, pair validity --------------------------------

  # Seed a volume row + a banked instance whose snapshot_generation is `bundle_gen`,
  # against a volume at `volume_gen`. valid iff bundle_gen == volume_gen.
  defp seed_banked_with_pair(ctx, id, node_id, bundle_gen, volume_gen) do
    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: node_id,
        vm_id: "vm-#{id}",
        generation: volume_gen
      })

    {:ok, _} = StatefulStore.publish(ctx.store, id, "10.88.0.9", 5432, :started)
    # unpublish already moves serving -> banking (ETS-only); a second :bank mark
    # from :banking would be illegal. Go straight to the durable bank_ready.
    {:ok, _} = StatefulStore.unpublish(ctx.store, id, :bank)

    {:ok, _} =
      StatefulStore.transition(
        ctx.store,
        id,
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "stateful/#{id}", size_bytes: 10, snapshot_generation: bundle_gen},
        %{snapshot_ref: "stateful/#{id}", snapshot_generation: bundle_gen, snapshot_size_bytes: 10, vm_id: nil}
      )

    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: node_id, generation: volume_gen, size_bytes: 10 * 1024 * 1024 * 1024, allocated_bytes: 1})

    id
  end

  test "plan_wake relights a VALID pair (daemon actually relit: was_relight=true)" do
    # A relight where the daemon genuinely resumed the bundle: the SAME banked
    # instance transitions in place via stateful_relit, no new lifecycle.
    relit_fun = fn _ch, _req ->
      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-relit",
         ip: "10.88.0.5",
         port: 5432,
         generation: 4,
         was_relight: true
       }}
    end

    ctx = start_stack(start_stateful_fun: relit_fun)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # The SAME instance transitioned via relight (not a fresh instance): stf-banked
    # is now serving, and it is the only non-terminal instance.
    {:ok, relit} = StatefulStore.get(ctx.store, "stf-banked")
    assert relit.state == :serving
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert length(live) == 1
  end

  test "a RELIGHT the DAEMON falls back to cold (was_relight=false) evicts the old instance, cold-boots a new one, and does not wedge" do
    # Regression: the control plane plans a relight (pair looks valid from here),
    # marks the banked instance :relighting, but the daemon discovers the pair is
    # actually broken and cold-boots, returning was_relight=false + cold_boot_reason.
    # The old :relighting instance MUST reach a terminal state via a legal edge
    # before the new instance's boot, or the singleton gate wedges the workload.
    fallback_fun = fn _ch, _req ->
      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-cold",
         ip: "10.88.0.6",
         port: 5432,
         generation: 4,
         was_relight: false,
         cold_boot_reason: "generation_mismatch"
       }}
    end

    ctx = start_stack(start_stateful_fun: fallback_fun)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    # The wake SUCCEEDS (not wedged): the daemon-side fallback still yields a live VM.
    assert {:ok, %{ip: "10.88.0.6", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # The old banked instance did NOT strand in :relighting: it is terminal.
    {:ok, old} = StatefulStore.get(ctx.store, "stf-banked")
    assert old.state == :evicted

    # A NEW instance was cold-booted and is serving (exactly one live instance).
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert [new_i] = live
    assert new_i.instance_id != "stf-banked"
    assert new_i.state == :serving

    # gate 2: the op-log alone tells the story (a stateful_cold_booted with the
    # discarded-warmth reason, not a plain stateful_started).
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    cold = Enum.find(ops, &(&1.kind == :stateful_cold_booted))
    assert cold, "expected a stateful_cold_booted op after a daemon-side relight fallback"
    assert cold.payload["reason"] == "generation_mismatch"
  end

  test "plan_wake evicts a BROKEN pair (generation mismatch) and cold-boots instead" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-stale", "node-4", 2, 5)
    refute StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    {:ok, evicted} = StatefulStore.get(ctx.store, "stf-stale")
    assert evicted.state == :evicted
    assert evicted.terminal_reason == "pair_broken"

    # A NEW instance was cold-booted (not the old banked one resumed).
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert [new_instance] = live
    assert new_instance.instance_id != "stf-stale"
  end

  test "plan_wake cold/fresh-boots when there is no bundle at all" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 1
    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :serving
  end

  test "a wake is refused when the volume's anchor node is gone" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    # A volume row anchored to a node that is NOT currently reporting.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-vanished", generation: 1, size_bytes: 1, allocated_bytes: 1})

    assert {:error, {:wake_failed, :volume_node_gone}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 0
  end

  # -- restart adoption matrix --------------------------------------------------

  test "adoption adopts a live stateful VM the node reports (restart republishes same endpoint)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-live",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-live",
        generation: 1
      })

    stateful_node(ctx, "node-4",
      stateful_vms: [%{vm_id: "vm-live", workload: "wl-a", ip: "10.88.0.9", port: 5432, healthy: true, generation: 1, last_probe_unix_ms: 1}]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)

    {:ok, adopted} = StatefulStore.get(ctx.store, "stf-live")
    assert adopted.state == :serving
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.9", port: 5432}
    assert Agent.get(ctx.starts, & &1) == 0
  end

  test "adoption SKIPS a :destroying instance even though the node still reports its live VM" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-destroying",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-live",
        generation: 1
      })

    # Drive the instance into :destroying via the begin_destroy FSM edge: mid
    # node-confirmed teardown (ADR embervm/014 decision 5), the teardown RPC in flight.
    {:ok, _} = StatefulStore.mark(ctx.store, "stf-destroying", :begin_destroy)

    # The node still reports the VM live: the straggler report that turns on the TLC
    # NoDestroyBeforeConfirm violation if adoption keys off the node, not the CP state.
    stateful_node(ctx, "node-4",
      stateful_vms: [%{vm_id: "vm-live", workload: "wl-a", ip: "10.88.0.9", port: 5432, healthy: true, generation: 1, last_probe_unix_ms: 1}]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)

    # NOT re-adopted to :serving; stays destroying for the (gated) redrive to own.
    {:ok, inst} = StatefulStore.get(ctx.store, "stf-destroying")
    assert inst.state == :destroying
    assert Agent.get(ctx.starts, & &1) == 0
  end

  test "adoption heals a limbo instance the node reports only as a bundle to banked" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-limbo",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-gone",
        generation: 1
      })

    {:ok, _} = StatefulStore.publish(ctx.store, "stf-limbo", "10.88.0.9", 5432, :started)
    # unpublish already moves serving -> banking; a second :bank mark is illegal.
    {:ok, _} = StatefulStore.unpublish(ctx.store, "stf-limbo", :bank)

    {:ok, _} =
      StatefulStore.transition(
        ctx.store,
        "stf-limbo",
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "stateful/limbo", size_bytes: 10, snapshot_generation: 1},
        %{snapshot_ref: "stateful/limbo", snapshot_generation: 1, snapshot_size_bytes: 10, vm_id: nil}
      )

    # The node reports the bundle AND a matching volume (generation 1 == the bundle's
    # stamped generation), so the pair is VALID and reconcile heals the limbo instance
    # to banked rather than eager-evicting it as a broken pair. A banked instance
    # always has a volume in production (data outlives the instance).
    stateful_node(ctx, "node-4",
      stateful_bundles: [%{snapshot_ref: "stateful/limbo", workload: "wl-a", generation: 1, size_bytes: 10, created_at_unix_ms: 1}],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 1, size_bytes: 100, allocated_bytes: 10}]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, healed} = StatefulStore.get(ctx.store, "stf-limbo")
    assert healed.state == :banked
  end

  test "adoption fails an instance whose VM and bundle both vanished (node IS reporting)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-vanished",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-vanished",
        generation: 1
      })

    stateful_node(ctx, "node-4", stateful_vms: [], stateful_bundles: [])

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, failed} = StatefulStore.get(ctx.store, "stf-vanished")
    assert failed.state == :failed
  end

  test "adoption NEVER reaps when the instance's node is not reporting (a disconnect)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-disc",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-gone",
        vm_id: "vm-x",
        generation: 1
      })

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, still} = StatefulStore.get(ctx.store, "stf-disc")
    assert still.state == :starting
  end

  test "ADR embervm/018 Phase 2: adopts an origin-ACTIVATOR stateful VM by relighting the banked instance, no orphan-destroy" do
    parent = self()

    # node_confirmed_destroy on so the orphan-destroy pass is live: an ordinary
    # rowless node VM would be destroyed, but a node-woken (ACTIVATOR) VM is adopted.
    ctx =
      start_stack(
        node_confirmed_destroy: true,
        stop_stateful_fun: fn _ch, _req ->
          send(parent, :stop_stateful_called)
          {:ok, %Embervm.Node.V1.StopStatefulResponse{teardown_confirmed: true}}
        end
      )

    stateful_workload(ctx, "wl-a")

    # A banked CP instance at generation 5 (the workload was banked before the gap).
    seed_banked_with_pair(ctx, "stf-banked", "node-4", 5, 5)

    # The brick relit it during the CP gap: origin ACTIVATOR, a NEW vm_id the CP
    # never issued, and a forward generation 6 (the node's self-bump).
    stateful_node(ctx, "node-4",
      stateful_vms: [
        %{
          vm_id: "vm-brick",
          workload: "wl-a",
          ip: "10.88.0.9",
          port: 5432,
          healthy: true,
          generation: 6,
          origin: :INSTANCE_ORIGIN_ACTIVATOR
        }
      ]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)

    # No orphan-destroy fired for the node-woken VM (it was adopted, not destroyed).
    refute_received :stop_stateful_called

    # The banked instance was relit onto the node's vm_id (reused instance_id) and
    # published, and now serves the node-reported endpoint at the forward generation.
    {:ok, inst} = StatefulStore.get(ctx.store, "stf-banked")
    assert inst.state == :serving
    assert inst.vm_id == "vm-brick"
    assert inst.generation == 6
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.9", port: 5432}
  end

  test "adoption refreshes volume facts and eager-evicts a pair broken while the control plane was down" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    # A banked bundle at generation 3, seeded consistent with a volume the store
    # does not yet know moved to generation 5 (the "control plane was down while
    # the daemon bumped it" scenario).
    seed_banked_with_pair(ctx, "stf-pair", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    # The node now reports the volume at a NEWER generation (5): the ledger moved
    # while we were not looking.
    stateful_node(ctx, "node-4", volumes: [%{workload: "wl-a", generation: 5, size_bytes: 1, allocated_bytes: 1}])

    # Each reconcile runs one eager_evict_broken_pairs observation. The eager
    # eviction has hysteresis (StatefulStore @broken_evict_threshold), so a
    # GENUINELY broken pair (the volume durably moved on) evicts after a few
    # consecutive reconciles rather than on the first. Drive enough reconciles to
    # cross the grace window.
    for _ <- 1..3 do
      :ok = StatefulManager.reconcile(ctx.mgr)
    end

    refute StatefulStore.pair_valid?(ctx.store, "wl-a")
    {:ok, evicted} = StatefulStore.get(ctx.store, "stf-pair")
    assert evicted.state == :evicted
    assert evicted.terminal_reason == "pair_broken"
  end

  # -- interruptible bank: adoption of a stranded checkpoint (ADR embervm/008) --

  test "adoption ABORTS a stranded checkpoint the node reports as checkpoint_pending (safe default)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    # A control-plane restart left the store showing :checkpointed while the node
    # still reports the paused VM as checkpoint_pending. Rebuild that ETS shape.
    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-ck",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-ck",
        generation: 1
      })

    {:ok, _} = StatefulStore.publish(ctx.store, "stf-ck", "10.88.0.9", 5432, :started)
    {:ok, _} = StatefulStore.unpublish(ctx.store, "stf-ck", :bank)
    {:ok, _} = StatefulStore.mark_with(ctx.store, "stf-ck", :checkpoint_ready, %{checkpoint_token: "ckpt-vm-ck", vm_id: "vm-ck"})

    stateful_node(ctx, "node-4",
      stateful_vms: [
        %{vm_id: "vm-ck", workload: "wl-a", ip: "10.88.0.9", port: 5432, healthy: true, generation: 1, checkpoint_pending: true, last_probe_unix_ms: 1}
      ]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)

    # Safe default: aborted back to serving, endpoint restored, republished.
    {:ok, resolved} = StatefulStore.get(ctx.store, "stf-ck")
    assert resolved.state == :serving
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.9", port: 5432}
  end

  # -- interruptible bank: wake-during-checkpoint parking (ADR embervm/008) -----

  # Drive a workload's instance into the transient :checkpointed state (serving ->
  # banking -> checkpointed), the paused-awaiting-resolve window a wake must park
  # behind rather than cold-boot.
  defp checkpointed_instance(ctx, id, workload, vm_id) do
    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "p",
        workload: workload,
        node_id: "node-4",
        vm_id: vm_id,
        generation: 1
      })

    {:ok, _} = StatefulStore.publish(ctx.store, id, "10.88.0.9", 5432, :started)
    {:ok, _} = StatefulStore.unpublish(ctx.store, id, :bank)
    {:ok, _} = StatefulStore.mark_with(ctx.store, id, :checkpoint_ready, %{checkpoint_token: "ckpt-#{vm_id}", vm_id: vm_id})
    id
  end

  test "parked?/2 reflects whether a caller is parked for the workload" do
    ctx = start_stack(start_sleep_ms: 200)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    refute StatefulManager.parked?(ctx.mgr, "wl-a")

    first = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    poll_mgr(ctx, fn -> StatefulManager.parked?(ctx.mgr, "wl-a") end)

    assert StatefulManager.parked?(ctx.mgr, "wl-a")

    assert {:ok, _} = Task.await(first, 5_000)
    refute StatefulManager.parked?(ctx.mgr, "wl-a")
  end

  test "a wake arriving while the workload is :checkpointed PARKS (no cold boot), and is served hot on a :abort resolve" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    checkpointed_instance(ctx, "stf-ck", "wl-a", "vm-ck")

    # The wake must PARK, not boot: run it in a task so it blocks.
    waiter = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    poll_mgr(ctx, fn -> StatefulManager.parked?(ctx.mgr, "wl-a") end)

    # No StartStateful was issued (parked, not cold-booted).
    assert Agent.get(ctx.starts, & &1) == 0
    assert StatefulManager.parked?(ctx.mgr, "wl-a")

    # Simulate the sweeper's ABORT resolve: the paused VM resumes hot and is
    # republished, THEN the manager is told. Drive the instance back to serving +
    # publish exactly as an abort would, then cast the resolution.
    {:ok, _} = StatefulStore.mark(ctx.store, "stf-ck", :abort)

    StatefulStore.adopt_endpoint(ctx.store, "stf-ck", "node-4", "vm-ck", %{
      ip: "10.88.0.9",
      port: 5432,
      healthy: true
    })

    GenServer.cast(ctx.mgr, {:checkpoint_resolved, "wl-a", :abort})

    # The parked caller is served the hot endpoint, no boot ever happened.
    assert {:ok, %{ip: "10.88.0.9", port: 5432}} = Task.await(waiter, 5_000)
    assert Agent.get(ctx.starts, & &1) == 0
    refute StatefulManager.parked?(ctx.mgr, "wl-a")
  end

  test "a wake parked during :checkpointed is served by a relight wake on a :commit resolve" do
    # A relight fun: the commit path leaves a banked bundle, and the manager's
    # commit cast starts a normal wake that relights it.
    relit_fun = fn _ch, _req ->
      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-relit",
         ip: "10.88.0.5",
         port: 5432,
         generation: 4,
         was_relight: true
       }}
    end

    ctx = start_stack(start_stateful_fun: relit_fun)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    # A checkpointed instance the commit converts into a banked bundle. Set up the
    # committed banked bundle + a matching volume so the relight pair is valid.
    checkpointed_instance(ctx, "stf-ck", "wl-a", "vm-ck")

    waiter = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    poll_mgr(ctx, fn -> StatefulManager.parked?(ctx.mgr, "wl-a") end)
    assert Agent.get(ctx.starts, & &1) == 0

    # Simulate the sweeper's COMMIT: the temp becomes the bundle (checkpointed ->
    # banked) with a matching volume generation, then the manager is told.
    {:ok, _} =
      StatefulStore.transition(
        ctx.store,
        "stf-ck",
        :commit,
        :stateful_banked,
        %{snapshot_ref: "stateful/ck", size_bytes: 10, generation: 4},
        %{snapshot_ref: "stateful/ck", snapshot_size_bytes: 10, snapshot_generation: 4, vm_id: nil}
      )

    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 4, size_bytes: 10, allocated_bytes: 1})

    GenServer.cast(ctx.mgr, {:checkpoint_resolved, "wl-a", :commit})

    # The parked caller is served by a relight wake off the committed bundle: it
    # receives the relit endpoint (10.88.0.5, from relit_fun), which only the
    # commit-driven relight wake produces, so the reply itself proves a relight ran.
    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = Task.await(waiter, 5_000)
  end

  # -- destroy_instance / delete_volume management verbs ------------------------

  test "destroy_instance destroys the live instance and evicts the banked bundle" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert live == []
  end

  # -- node-confirmed destroy (ADR embervm/014 decision 5, gated) -------------

  test "gated: a confirming teardown records the stateful instance destroyed" do
    ctx =
      start_stack(
        node_confirmed_destroy: true,
        stop_stateful_fun: fn _ch, _req -> {:ok, %{teardown_confirmed: true}} end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")
    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :destroyed
  end

  test "gated: an unconfirmed teardown stays destroying and reconcile re-drives it" do
    {:ok, confirmations} = Agent.start_link(fn -> [false, true] end)

    ctx =
      start_stack(
        node_confirmed_destroy: true,
        stop_stateful_fun: fn _ch, _req ->
          Agent.get_and_update(confirmations, fn [confirmed | rest] ->
            {{:ok, %{teardown_confirmed: confirmed}}, rest}
          end)
        end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")
    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert %{destroyed: 0, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")

    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :destroying

    stateful_node(ctx, "node-4",
      stateful_vms: [%{vm_id: instance.vm_id, workload: "wl-a", ip: "10.88.0.5", port: 5432, healthy: true, generation: 1, last_probe_unix_ms: 1}]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, instance} = StatefulStore.get(ctx.store, instance.instance_id)
    assert instance.state == :destroyed
  end

  test "gated: a destroying instance is confirmed destroyed by owner-reported absence" do
    ctx =
      start_stack(
        node_confirmed_destroy: true,
        stop_stateful_fun: fn _ch, _req -> {:ok, %{teardown_confirmed: false}} end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")
    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert %{destroyed: 0, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")

    stateful_node(ctx, "node-4", stateful_vms: [])
    :ok = StatefulManager.reconcile(ctx.mgr)

    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :destroyed
  end

  test "gate-off destroy records destroyed before an unconfirmed teardown" do
    parent = self()

    ctx =
      start_stack(
        stop_stateful_fun: fn _ch, _req ->
          send(parent, :stop_stateful_called)
          {:ok, %{teardown_confirmed: false}}
        end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")
    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert_received :stop_stateful_called
    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :destroyed
  end

  test "destroy_instance leaves the volume row intact" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 1, size_bytes: 10, allocated_bytes: 1})

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert %{workload: "wl-a", node_id: "node-4", generation: 1} = StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "delete_volume is refused while a live instance exists, succeeds once it is gone" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert {:error, :instance_exists} = StatefulManager.delete_volume(ctx.mgr, "wl-a")

    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert {:ok, %{deleted: true}} = StatefulManager.delete_volume(ctx.mgr, "wl-a")
    assert StatefulStore.get_volume(ctx.store, "wl-a") == nil
  end

  test "delete_volume fans out to every node reporting the volume and the store anchor" do
    {:ok, deleted} = Agent.start_link(fn -> [] end)

    ctx =
      start_stack(
        channel_fun: fn node_id -> {:ok, node_id} end,
        delete_volume_fun: fn node_id, _req ->
          Agent.update(deleted, &[node_id | &1])
          {:ok, %{}}
        end
      )

    stateful_workload(ctx, "wl-a")
    Enum.each(["node-1", "node-2", "node-3"], fn node_id ->
      stateful_node(ctx, node_id, volumes: [%{workload: "wl-a"}])
    end)

    StatefulStore.upsert_volume(ctx.store, "wl-a", %{
      node_id: "node-1",
      generation: 1,
      size_bytes: 10,
      allocated_bytes: 1
    })

    assert {:ok, %{deleted: true}} = StatefulManager.delete_volume(ctx.mgr, "wl-a")
    assert Enum.sort(Agent.get(deleted, & &1)) == ["node-1", "node-2", "node-3"]
    assert StatefulStore.get_volume(ctx.store, "wl-a") == nil
  end

  test "delete_volume reports failed nodes and retains the store record" do
    {:ok, deleted} = Agent.start_link(fn -> [] end)

    ctx =
      start_stack(
        channel_fun: fn node_id -> {:ok, node_id} end,
        delete_volume_fun: fn node_id, req ->
          Agent.update(deleted, &[req.workload | &1])
          if node_id == "node-1", do: {:error, :failed}, else: {:ok, %{}}
        end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-1", volumes: [%{workload: "wl-a"}])
    stateful_node(ctx, "node-2", volumes: [%{workload: "wl-a"}])
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-1", generation: 1})

    assert {:error, {:delete_incomplete, ["node-1"]}} =
             StatefulManager.delete_volume(ctx.mgr, "wl-a")

    assert StatefulStore.get_volume(ctx.store, "wl-a")
    assert length(Agent.get(deleted, & &1)) == 2
  end

  test "refresh drops a volume record when its reporting anchor omits the volume" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-1")
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-1", generation: 1})

    assert :ok = StatefulManager.reconcile(ctx.mgr)
    assert StatefulStore.get_volume(ctx.store, "wl-a") == nil
  end

  test "refresh keeps a volume record when its reporting anchor includes the volume" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-1",
      volumes: [%{workload: "wl-a", generation: 1, size_bytes: 10, allocated_bytes: 1}]
    )
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-1", generation: 1})

    assert :ok = StatefulManager.reconcile(ctx.mgr)
    assert %{workload: "wl-a", node_id: "node-1"} = StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "refresh keeps a volume record when its anchor is absent from facts" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-1", generation: 1})

    assert :ok = StatefulManager.reconcile(ctx.mgr)
    assert %{workload: "wl-a", node_id: "node-1"} = StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "a vanished volume record makes the next wake plan a fresh boot" do
    {:ok, requests} = Agent.start_link(fn -> [] end)

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, req ->
          Agent.update(requests, &[req | &1])
          {:ok,
           %StartStatefulResponse{
             vm_id: "vm-fresh",
             ip: "10.88.0.5",
             port: 5432,
             generation: 1,
             was_relight: false
           }}
        end
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-1")
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-1", generation: 1})
    assert :ok = StatefulManager.reconcile(ctx.mgr)

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")
    assert [%{mode: :START_STATEFUL_MODE_FRESH}] = Agent.get(requests, & &1)
  end

  # -- wake-worker bound + adoption self-recovery (Task 10) --------------------

  test "a wedged wake worker fails at the bound, releasing single-flight, and adoption heals back to banked" do
    # A relight whose StartStateful never returns (the guest never opens its port,
    # the R5 `:infinity`-chain symptom): the fake sleeps 3s, but the wake WORKER is
    # bounded to 40ms, so {:wake_timeout} fails the wake before the worker reports.
    # The parked caller gets a wake_failed error (NOT a forever-hang). The instance is
    # stranded in :relighting; the next reconcile heals it back to :banked (the node
    # still reports the bundle + matching volume), so it is re-wakeable.
    hang_fun = fn _ch, _req ->
      Process.sleep(3_000)
      {:error, :never_ready}
    end

    ctx = start_stack(start_stateful_fun: hang_fun, wake_bound_ms: 40)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4",
      stateful_bundles: [%{snapshot_ref: "stateful/stf-banked", workload: "wl-a", generation: 3, size_bytes: 10, created_at_unix_ms: 1}],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10}]
    )

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:error, {:wake_failed, {:wake_timeout, "wl-a"}}} =
             StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # Single-flight released + the stranded relight heals back to banked (re-wakeable).
    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, healed} = StatefulStore.get(ctx.store, "stf-banked")
    assert healed.state == :banked
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")
  end

  test "adoption recovers a workload stuck waking past 2 * wakeTimeoutSeconds" do
    # Directly exercise the adoption self-recovery: an in-flight wake whose worker
    # never reports and whose {:wake_timeout} timer is (modeled as) lost. wake_bound_ms
    # is huge so the timer never fires within the test; the injected mono_clock jumps
    # past 2 * wakeTimeoutSeconds (60s here, so 120s) between the wake stamp and the
    # reconcile, so wake_stuck? trips and adoption recovers the workload instead of
    # skipping it. The parked caller is erred out of its :infinity wait.
    {:ok, mono} = Agent.start_link(fn -> 0 end)
    mono_clock = fn -> Agent.get(mono, & &1) end

    hang_fun = fn _ch, _req ->
      Process.sleep(3_000)
      {:error, :never_ready}
    end

    ctx =
      start_stack(
        start_stateful_fun: hang_fun,
        wake_bound_ms: 10 * 60_000,
        mono_clock: mono_clock
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4",
      stateful_bundles: [%{snapshot_ref: "stateful/stf-banked", workload: "wl-a", generation: 3, size_bytes: 10, created_at_unix_ms: 1}],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10}]
    )

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)

    caller = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    # Wait for the wake to register as in-flight (mark :relighting, stamp
    # wake_started at 0) before advancing the clock past the stuck threshold.
    poll_mgr(ctx, fn -> StatefulManager.parked?(ctx.mgr, "wl-a") end)

    # Advance mono past 2 * wakeTimeoutSeconds (2 * 60s = 120_000ms).
    Agent.update(mono, fn _ -> 200_000 end)

    :ok = StatefulManager.reconcile(ctx.mgr)

    assert {:error, {:wake_failed, :wake_stuck}} = Task.await(caller, 5_000)

    {:ok, healed} = StatefulStore.get(ctx.store, "stf-banked")
    assert healed.state == :banked
  end

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  # Records every RestoreArtifact call the manager issues, returning a successful
  # response. `kind`/`ref`/`workload` are pulled off the ArtifactRef; `vendor` is
  # pulled off the REQUEST (that is where noded reads it, req.GetVendor()), so a test
  # can assert exactly what was restored and with which vendor.
  defp recording_restore_fun do
    {:ok, calls} = Agent.start_link(fn -> [] end)

    fun = fn _ch, req ->
      art = req.artifact
      Agent.update(calls, &[%{kind: art.kind, ref: art.ref, workload: art.workload, vendor: req.vendor} | &1])
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{bytes_moved: 4096, skipped: false, generation: 3}}
    end

    {fun, calls}
  end

  test "a local-bundle miss with an exported pair RESTORES the bundle then relights" do
    {restore_fun, restore_calls} = recording_restore_fun()

    relit_fun = fn _ch, _req ->
      {:ok, %StartStatefulResponse{vm_id: "vm-relit", ip: "10.88.0.5", port: 5432, generation: 3, was_relight: true}}
    end

    ctx = start_stack(start_stateful_fun: relit_fun, restore_artifact_fun: restore_fun)
    stateful_workload(ctx, "wl-a")

    # The anchor node reports the workload's volume (so exported_generation is
    # known) but NO local stateful bundle: a true local bundle miss. The store copy
    # is current (exported_generation == the banked bundle generation) and the store
    # is reachable, so the wake restores then relights.
    stateful_node(ctx, "node-4",
      stateful_bundles: [],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10, exported_generation: 3}],
      store_reachable: true
    )

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # RestoreArtifact was called for the STATEFUL bundle, before the relight landed.
    # Bug B: STATEFUL is vendor-bound, so the REQUEST carries the anchor node's
    # cpu_vendor ("amd"), which noded reads (req.GetVendor()) to compose the
    # vendor-keyed store prefix.
    assert [%{kind: :ARTIFACT_KIND_STATEFUL, ref: "stateful/stf-banked", workload: "wl-a", vendor: "amd"}] =
             Agent.get(restore_calls, & &1)

    # The SAME banked instance relit in place (a warm relight, not a fresh boot).
    {:ok, relit} = StatefulStore.get(ctx.store, "stf-banked")
    assert relit.state == :serving

    # gate: the restore is auditable from the op-log alone.
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    restored = Enum.find(ops, &(&1.kind == :artifact_restored))
    assert restored, "expected an artifact_restored op"
    assert restored.payload["kind"] == "stateful"
    assert restored.payload["ref"] == "stateful/stf-banked"
  end

  test "a local-volume miss with an exported (vol, gen) pair RESTORES the volume then cold-boots at that generation" do
    {restore_fun, restore_calls} = recording_restore_fun()

    cold_fun = fn _ch, _req ->
      {:ok, %StartStatefulResponse{vm_id: "vm-cold", ip: "10.88.0.7", port: 5432, generation: 3, was_relight: false}}
    end

    ctx = start_stack(start_stateful_fun: cold_fun, restore_artifact_fun: restore_fun)
    stateful_workload(ctx, "wl-a")

    # No banked bundle, but the anchor node reports the volume with an exported pair
    # (exported_generation > 0) and a reachable store: the wake restores the volume
    # then cold-boots against it.
    stateful_node(ctx, "node-4",
      stateful_bundles: [],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10, exported_generation: 3}],
      store_reachable: true
    )

    # Seed the volume ETS fact (no banked instance) so plan_wake sees a volume row
    # with an exported generation.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10, exported_generation: 3})

    assert {:ok, %{ip: "10.88.0.7", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # Bug B: VOLUME is vendor-portable, so its restore request leaves vendor EMPTY even
    # though the anchor node reports "amd" (noded keys volumes without a vendor segment).
    assert [%{kind: :ARTIFACT_KIND_VOLUME, ref: "wl-a", workload: "wl-a", vendor: ""}] = Agent.get(restore_calls, & &1)

    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert [inst] = live
    assert inst.state == :serving
    assert inst.generation == 3
  end

  test "an UNREACHABLE store on a true local-bundle miss attempts no restore and degrades to the relight/cold path" do
    {restore_fun, restore_calls} = recording_restore_fun()

    # The daemon-side relight falls back to a cold boot (the bundle is not on disk),
    # returning was_relight=false: the CP-side warmth was correctly not consulted.
    fallback_fun = fn _ch, _req ->
      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-cold",
         ip: "10.88.0.8",
         port: 5432,
         generation: 3,
         was_relight: false,
         cold_boot_reason: "no_bundle"
       }}
    end

    ctx = start_stack(start_stateful_fun: fallback_fun, restore_artifact_fun: restore_fun)
    stateful_workload(ctx, "wl-a")

    # True local miss (no local bundle) but the store is UNREACHABLE: fail-open
    # warmth means the local-state wake still runs, it just never consults the store.
    stateful_node(ctx, "node-4",
      stateful_bundles: [],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10, exported_generation: 3}],
      store_reachable: false
    )

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)

    assert {:ok, %{ip: "10.88.0.8", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # No restore was attempted (store unreachable), and no wake was blocked.
    assert Agent.get(restore_calls, & &1) == []
  end

  # -- instance-aware dial (brick co-location foundation, Step 4) ----------------

  # Put ONE per-instance capacity fact (keyed by {node, pod_uid}) for a co-located
  # brick, carrying the fields WakeInstance.select reads.
  defp put_brick(ctx, node_id, pod_uid, opts) do
    # free_slots (Brick.to_brick) = max(max_live_vms - live_vms, 0), and a cold
    # pick requires free_slots > 0 (a full instance cannot take a new VM). Default to a
    # free slot (max_live_vms 4, live_vms 0) so a brick this fixture expects the cold
    # path to select is not filtered out on SLOTS - a too-small brick must be excluded
    # on MEM, not because the fixture forgot to give it a slot. Overridable to model a
    # genuinely full instance.
    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      node_id: node_id,
      configured_id: node_id,
      pod_uid: pod_uid,
      instance_id: "#{node_id}/#{pod_uid}",
      serving_subnet_cidr: "10.88.0.0/24",
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 8_192),
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: Keyword.get(opts, :max_live_vms, 4),
      workloads: Keyword.get(opts, :workloads, %{"wl-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap-a"}}),
      stateful_vms: Keyword.get(opts, :stateful_vms, []),
      stateful_bundles: Keyword.get(opts, :stateful_bundles, []),
      volumes: Keyword.get(opts, :volumes, []),
      store_reachable: Keyword.get(opts, :store_reachable, false)
    })
  end

  test "a relight dials the bundle-OWNING co-located instance's instance_id, not the node name" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    relit_fun = fn _ch, _req ->
      {:ok, %StartStatefulResponse{vm_id: "vm-relit", ip: "10.88.0.5", port: 5432, generation: 3, was_relight: true}}
    end

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    ctx = start_stack(start_stateful_fun: relit_fun, channel_fun: capture_channel)
    stateful_workload(ctx, "wl-a")

    # Two co-located bricks on node-4. pod-b banked the bundle on disk; pod-a is a
    # too-small 2Gi brick. A node-name dial could hit either; instance selection
    # must pin the relight to pod-b (the owner).
    put_brick(ctx, "node-4", "pod-a",
      size_class: "2gi",
      mem_headroom_mib: 100,
      mem_budget_mib: 2_048,
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10}]
    )

    put_brick(ctx, "node-4", "pod-b",
      stateful_bundles: [%{snapshot_ref: "stateful/stf-banked", workload: "wl-a", generation: 3, size_bytes: 10, created_at_unix_ms: 1}],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10}]
    )

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(dialed, & &1) == ["node-4/pod-b"]
  end

  test "a cold wake dials a mem-eligible co-located instance and skips a too-small classed brick" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    cold_fun = fn _ch, _req ->
      {:ok, %StartStatefulResponse{vm_id: "vm-cold", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
    end

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    # A large workload (4000 MiB) so the 2Gi brick is ineligible.
    ctx = start_stack(start_stateful_fun: cold_fun, channel_fun: capture_channel)
    stateful_workload(ctx, "wl-a", %{resources: %{vcpus: 2, mem_mib: 4_000}})

    put_brick(ctx, "node-4", "pod-small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
    put_brick(ctx, "node-4", "pod-big", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

    # Fresh first boot (no volume): a cold wake places on a mem-eligible instance.
    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(dialed, & &1) == ["node-4/pod-big"]
  end

  test "boot_image_ref resolves from the CHOSEN instance's fact, not a co-located sibling that lacks the workload" do
    # Instance-key unification (PR-B0a): the cold pick is instance-aware, but
    # boot_image_ref used to re-resolve the base snapshot_ref by BARE node name,
    # racing across the co-located facts. A sibling brick that never built this
    # workload's base advertises NO wl-a entry, so the node-name fetch could read
    # its ABSENT workload (nil -> boot_image_ref "" -> noded rejects). The ref must
    # be read from the SAME instance the cold RPC dials.
    {:ok, captured} = Agent.start_link(fn -> nil end)

    cold_fun = fn _ch, req ->
      Agent.update(captured, fn _ -> req end)
      {:ok, %StartStatefulResponse{vm_id: "vm-cold", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
    end

    # A large workload (4000 MiB) so the 2Gi sibling is mem-ineligible and the cold
    # pick lands on pod-big deterministically.
    ctx = start_stack(start_stateful_fun: cold_fun)
    stateful_workload(ctx, "wl-a", %{resources: %{vcpus: 2, mem_mib: 4_000}})

    # pod-small: mem-ineligible AND advertises NO wl-a base (empty workloads), the
    # exact ABSENT-fact a node-name fetch could race onto.
    put_brick(ctx, "node-4", "pod-small",
      size_class: "2gi",
      mem_headroom_mib: 100,
      mem_budget_mib: 2_048,
      workloads: %{}
    )

    # pod-big: the chosen instance, advertising the wl-a base READY with its ref.
    put_brick(ctx, "node-4", "pod-big",
      size_class: "8gi",
      mem_headroom_mib: 8_000,
      mem_budget_mib: 8_192,
      workloads: %{"wl-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap-a"}}
    )

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    req = Agent.get(captured, & &1)
    assert req.boot_image_ref == "snap-a"
  end

  # -- A2 auto-wake after base READY (instance-key unification PR-B0a) ----------

  test "reconcile auto-wakes an autoWake workload whose base is READY and has no instance" do
    {:ok, wakes} = Agent.start_link(fn -> 0 end)

    start_fun = fn _ch, _req ->
      Agent.update(wakes, &(&1 + 1))
      {:ok, %StartStatefulResponse{vm_id: "vm-auto", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
    end

    ctx = start_stack(start_stateful_fun: start_fun)
    stateful_workload(ctx, "wl-a", %{auto_wake: true})
    stateful_node(ctx, "node-4")

    # No instance exists yet; the base is READY (stateful_node advertises it). A
    # reconcile must auto-wake the workload (retires the post-roll manual wake).
    :ok = StatefulManager.reconcile(ctx.mgr)

    # The StartStateful RPC records `wakes` inside the spawned wake worker, BEFORE
    # it posts {:wake_done}; the publish only lands when finish_wake handles that
    # message. So poll on the PUBLISHED ENDPOINT (the terminal effect), not `wakes`,
    # to avoid asserting between the RPC return and the async publish.
    poll_mgr(ctx, fn ->
      match?(%{ip: "10.88.0.5", port: 5432}, StatefulStore.published_endpoint(ctx.store, "wl-a"))
    end)

    assert Agent.get(wakes, & &1) == 1
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.5", port: 5432}
  end

  test "reconcile does NOT auto-wake when autoWake is unset, or an instance already exists" do
    {:ok, wakes} = Agent.start_link(fn -> 0 end)

    start_fun = fn _ch, _req ->
      Agent.update(wakes, &(&1 + 1))
      {:ok, %StartStatefulResponse{vm_id: "vm-auto", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
    end

    ctx = start_stack(start_stateful_fun: start_fun)
    # autoWake unset (default false): a base-READY workload is NOT auto-woken.
    stateful_workload(ctx, "wl-a", %{})
    stateful_node(ctx, "node-4")

    :ok = StatefulManager.reconcile(ctx.mgr)
    flush_mgr(ctx)
    assert Agent.get(wakes, & &1) == 0
  end

  test "reconcile does NOT auto-wake an autoWake workload that already has a banked instance" do
    {:ok, wakes} = Agent.start_link(fn -> 0 end)

    start_fun = fn _ch, _req ->
      Agent.update(wakes, &(&1 + 1))
      {:ok, %StartStatefulResponse{vm_id: "vm-auto", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
    end

    ctx = start_stack(start_stateful_fun: start_fun)
    stateful_workload(ctx, "wl-a", %{auto_wake: true})
    stateful_node(ctx, "node-4",
      stateful_bundles: [%{snapshot_ref: "stateful/stf-banked", workload: "wl-a", generation: 3, size_bytes: 10, created_at_unix_ms: 1}]
    )
    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)

    :ok = StatefulManager.reconcile(ctx.mgr)
    flush_mgr(ctx)
    # A banked instance relights on the next connection; nothing to auto-wake.
    assert Agent.get(wakes, & &1) == 0
  end

  test "restore-on-miss dials the RESTORE and the BOOT on the SAME selected instance_id (not the node name)" do
    # Regression for the co-location restore bug: PR-2.5 makes banked bundles
    # per-instance ON DISK, so the RestoreArtifact RPC must land the bundle on the
    # SAME instance the subsequent relight dials. A node-name dial would resolve the
    # restore to an arbitrary co-located instance (channel alias), stranding the boot
    # on an instance whose disk never got the bundle.
    {:ok, restore_channels} = Agent.start_link(fn -> [] end)
    {:ok, boot_channels} = Agent.start_link(fn -> [] end)

    # A channel tagged with the exact key it was dialled on, so both the restore and
    # the boot record which instance_id they targeted.
    capture_channel = fn key -> {:ok, {:ch, key}} end

    restore_fun = fn {:ch, key}, _req ->
      Agent.update(restore_channels, &[key | &1])
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{bytes_moved: 4096, skipped: false, generation: 3}}
    end

    relit_fun = fn {:ch, key}, _req ->
      Agent.update(boot_channels, &[key | &1])
      {:ok, %StartStatefulResponse{vm_id: "vm-relit", ip: "10.88.0.5", port: 5432, generation: 3, was_relight: true}}
    end

    ctx = start_stack(start_stateful_fun: relit_fun, channel_fun: capture_channel, restore_artifact_fun: restore_fun)
    stateful_workload(ctx, "wl-a", %{resources: %{vcpus: 2, mem_mib: 4_000}})

    # Two co-located bricks; NEITHER reports the bundle locally (a true local miss),
    # the store is reachable, and pod-small is too small for the 4000 MiB workload.
    # The relight's restore + boot must BOTH land on pod-big.
    put_brick(ctx, "node-4", "pod-small",
      size_class: "2gi",
      mem_headroom_mib: 100,
      mem_budget_mib: 2_048,
      stateful_bundles: [],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10, exported_generation: 3}],
      store_reachable: true
    )

    put_brick(ctx, "node-4", "pod-big",
      size_class: "8gi",
      mem_headroom_mib: 8_000,
      mem_budget_mib: 8_192,
      stateful_bundles: [],
      volumes: [%{workload: "wl-a", node_id: "node-4", generation: 3, size_bytes: 100, allocated_bytes: 10, exported_generation: 3}],
      store_reachable: true
    )

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # The restore and the boot both dialled the SAME mem-eligible instance_id, never
    # the collapsing "node-4" node-name alias and never the too-small "node-4/pod-small".
    assert Agent.get(restore_channels, & &1) == ["node-4/pod-big"]
    assert Agent.get(boot_channels, & &1) == ["node-4/pod-big"]
  end

  test "a cold wake with no eligible instance fails cleanly rather than dialing a too-small brick" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    ctx = start_stack(channel_fun: capture_channel)
    stateful_workload(ctx, "wl-a", %{resources: %{vcpus: 2, mem_mib: 4_000}})

    # The node's only instance is a too-small 2Gi classed brick.
    put_brick(ctx, "node-4", "pod-small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)

    assert {:error, {:wake_failed, :no_eligible_instance}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    # No dial happened: the wake failed at selection, never sending a StartStateful.
    assert Agent.get(dialed, & &1) == []
    assert Agent.get(ctx.starts, & &1) == 0
  end
end
