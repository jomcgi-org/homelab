defmodule Embervm.SessionBankRelightTest do
  @moduledoc """
  Task 7 + Task 8 acceptance for the bank/relight lifecycle, the placement seam, and
  restart adoption. A FAKE DAEMON is injected via bank_fun / relight_fun / evict_fun
  / claim_fun / channel_fun plus the session_opts assign fun, exactly the seam idiom
  the PR-3 SessionManagerTest uses. A real (unnamed) op-log + SessionStore give the
  real FSM and durable projection; unique ETS + a per-test supervisor/registry keep
  tests isolated from the application's supervised subtree.

  What a reviewer must scrutinise, each has a test:

    * idle-bank quiescence: banks only when no invoke is in flight or queued;
    * the three-strikes bank failure path (session -> failed + VM destroyed);
    * relight-on-invoke: a parked invoke on a banked session is served after relight,
      and session_relit appends only after a live vm_id (crash consistency);
    * the wake-rate limit: excess relight-triggering invokes 429 without a node hit;
    * snapshot_lost: an unrestorable snapshot -> failed + parked callers 410;
    * the adoption matrix: restart with running/banking/banked/relighting each
      converging to the right state, reap-free on a transient disconnect;
    * LRU eviction: disk-pressure evicts the coldest banked sessions to the watermark;
    * expiry (live + banked) and banked-TTL GC.
  """
  # async: false, not true. These tests wait (wait_until) for real session
  # GenServer lifecycle transitions whose clock is an Agent round-trip, so they
  # are timing-sensitive. Under a large async suite the scheduler contention made
  # the wait_until budget flake (bumping it only postponed the failure as the
  # suite grew). Running serially removes the contention and makes the timing
  # deterministic regardless of suite size.
  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, SessionManager, SessionStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite

  alias Embervm.Node.V1.{
    BankResponse,
    GuestResponse,
    RelightResponse,
    SessionAssignResponse,
    UsageStats
  }

  # A clock the test controls: an Agent holding the current ms, so timer/TTL logic is
  # deterministic. `advance/2` moves it forward.
  defp start_clock(start \\ 1_000_000) do
    {:ok, pid} = Agent.start_link(fn -> start end)
    pid
  end

  defp now(clock_pid), do: Agent.get(clock_pid, & &1)
  defp advance(clock_pid, ms), do: Agent.update(clock_pid, &(&1 + ms))

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"bcap_#{suffix}"
    cat_table = :"bcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_bankrelight_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    # The clock must be shared by the store and the manager: the store stamps
    # updated_at/last_invoke_at and the manager sweep compares against them, so a
    # split clock (a real wall-clock in the store vs the test Agent in the sweep)
    # makes now-last hugely negative and the TTL GC never fires. Create it FIRST.
    clock_pid = Keyword.get(opts, :clock_pid, start_clock())
    clock = fn -> now(clock_pid) end

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = SessionStore.start_link(name: nil, op_log: op_log, clock: clock)

    registry = :"breg_#{suffix}"
    {:ok, _} = Registry.start_link(keys: :unique, name: registry)
    {:ok, sup} = DynamicSupervisor.start_link(strategy: :one_for_one)

    test_pid = self()

    assign_fun = Keyword.get(opts, :assign_fun, &default_assign/2)

    session_opts = [
      channel_fun: fn _node -> {:ok, :ch} end,
      assign_fun: assign_fun,
      destroy_fun: fn _ch, vm -> send(test_pid, {:destroyed, vm}) && {:ok, %{}} end,
      invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _node, _ch -> :ok end)
    ]

    mgr_opts =
      [
        name: nil,
        session_store: store,
        supervisor: sup,
        registry: registry,
        capacity_table: cap_table,
        catalog_table: cat_table,
        op_log: op_log,
        clock: clock,
        channel_fun: fn _node -> {:ok, :ch} end,
        claim_fun: fn _d, _n, _w -> {:ok, "vm-#{suffix}-#{System.unique_integer([:positive])}"} end,
        prime_fun: fn _ch, _req -> {:error, :no_prime} end,
        bank_fun: Keyword.get(opts, :bank_fun, &default_bank/2),
        relight_fun: Keyword.get(opts, :relight_fun, &default_relight/2),
        evict_fun: Keyword.get(opts, :evict_fun, fn _ch, req -> send(test_pid, {:evicted, req.snapshot_ref}) && {:ok, %{}} end),
        retire_volume_fun: Keyword.get(opts, :retire_volume_fun, fn _ch, req -> send(test_pid, {:retired, req.lineage_id}) && {:ok, %{}} end),
        session_opts: session_opts,
        # Timers off by default: tests drive reconcile/1 + sweep/1 explicitly.
        reconcile_interval_ms: 0,
        sweep_interval_ms: 0
      ] ++
        Keyword.take(opts, [
          :disk_low_watermark_bytes,
          :wake_max,
          :wake_window_ms,
          :bank_concurrency,
          :status_writer,
          :restore_artifact_fun,
          :evict_artifact_fun
        ])

    {:ok, mgr} = SessionManager.start_link(mgr_opts)

    %{
      mgr: mgr,
      store: store,
      op_log: op_log,
      cap_table: cap_table,
      cat_table: cat_table,
      registry: registry,
      sup: sup,
      clock_pid: clock_pid,
      suffix: suffix
    }
  end

  defp default_assign(_ch, req) do
    {:ok,
     %SessionAssignResponse{
       response: %GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
       usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1},
       suspect: false
     }}
  end

  defp default_bank(_ch, req) do
    {:ok, %BankResponse{snapshot_ref: "snap-#{req.session_id}", size_bytes: 2_000}}
  end

  defp default_relight(_ch, _req) do
    {:ok, %RelightResponse{vm_id: "vm-relit-#{System.unique_integer([:positive])}"}}
  end

  defp put_workload(ctx, wl, opts \\ []) do
    NodeCapacity.put(ctx.cap_table, "node-4", node_fact(wl, opts))

    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:abc",
      invoke_path: Keyword.get(opts, :invoke_path, "/"),
      timeout_ms: 90_000,
      cap: Keyword.get(opts, :cap, 8),
      floor: 1,
      session: %{
        idle_bank_seconds: Keyword.get(opts, :idle_bank_seconds, 300),
        max_lifetime_seconds: Keyword.get(opts, :max_lifetime_seconds, 3600),
        banked_ttl_seconds: Keyword.get(opts, :banked_ttl_seconds, 3600),
        max_sessions: Keyword.get(opts, :max_sessions, 16),
        invoke_queue_cap: Keyword.get(opts, :queue_cap, 4)
      },
      persistence: Keyword.get(opts, :persistence)
    })
  end

  defp node_fact(wl, opts) do
    %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{
        wl => %{
          free_primed_slots: 1,
          snapshot_ref: "base-#{wl}",
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: []
        }
      },
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.get(opts, :max, 8),
      session_vms: Keyword.get(opts, :session_vms, []),
      session_snapshots: Keyword.get(opts, :session_snapshots, []),
      snapshot_disk_free_bytes: Keyword.get(opts, :free, 10_000_000),
      snapshot_disk_used_bytes: 0,
      draining: false,
      store_reachable: Keyword.get(opts, :store_reachable, false),
      updated_at: Keyword.get(opts, :updated_at, 1_000_000_000)
    }
  end

  # Drive a session's idle-bank timer synchronously: find its live process and send
  # it :maybe_bank, then wait for the banked state to land (the manager's async bank
  # worker completes on the manager process).
  defp force_idle_bank(ctx, session_id) do
    [{pid, _}] = Registry.lookup(ctx.registry, session_id)
    send(pid, :maybe_bank)
    wait_state(ctx, session_id, :banked)
  end

  # Same idle timer, a persistence-enabled workload (memory: false, filesystem:
  # true) routes it through park instead of bank (do_bank's
  # persistence_enabled_workload? branch), so the session lands on :parked with
  # its VM torn down rather than :banked with a snapshot.
  defp force_idle_park(ctx, session_id) do
    [{pid, _}] = Registry.lookup(ctx.registry, session_id)
    send(pid, :maybe_bank)
    wait_state(ctx, session_id, :parked)
  end

  defp wait_state(ctx, session_id, expected, tries \\ 100) do
    case SessionStore.get(ctx.store, session_id) do
      {:ok, %{state: ^expected}} ->
        :ok

      _ when tries > 0 ->
        Process.sleep(5)
        wait_state(ctx, session_id, expected, tries - 1)

      _ ->
        {:ok, s} = SessionStore.get(ctx.store, session_id)
        flunk("session #{session_id} never reached #{expected}; stuck at #{s.state}")
    end
  end

  # 1000 tries x 5ms = a 5s ceiling (was 200 tries, 1s), well under ExUnit's
  # 60s per-test timeout: the full async suite's scheduler contention can
  # occasionally push this test's Agent-round-trip session restart past a 1s
  # budget, so the ceiling absorbs that CI load rather than flaking. The poll
  # still returns the instant the condition holds, so this only spends time
  # on a genuine failure.
  defp wait_until(fun, tries \\ 1000) do
    cond do
      fun.() ->
        :ok

      tries > 0 ->
        Process.sleep(5)
        wait_until(fun, tries - 1)

      true ->
        flunk("wait_until condition never became true")
    end
  end

  # -- idle-bank -------------------------------------------------------------

  test "idle-bank fires only when quiescent: banks an idle session, releasing its VM" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    :ok = force_idle_bank(ctx, created.session_id)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :banked
    assert session.generation == 1
    assert session.snapshot_ref == "snap-#{created.session_id}"
    # A banked session has no process.
    assert Registry.lookup(ctx.registry, created.session_id) == []
    # counts moved live -> banked.
    assert SessionStore.counts(ctx.store, "wl") == %{live: 0, banked: 1}
  end

  test "idle-bank re-arms (does not bank) while an invoke is in flight" do
    # A blocking assign keeps the session non-quiescent; :maybe_bank must NOT bank.
    test_pid = self()

    blocking_assign = fn _ch, req ->
      send(test_pid, :assign_started)

      receive do
        :go -> {:ok, %SessionAssignResponse{response: %GuestResponse{status_code: 200, headers: %{}, body: req.request.body}, usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}, suspect: false}}
      end
    end

    ctx = start_stack(assign_fun: blocking_assign)
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    caller = spawn(fn -> SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"}) end)
    assert_receive :assign_started, 1_000

    [{pid, _}] = Registry.lookup(ctx.registry, created.session_id)
    send(pid, :maybe_bank)
    Process.sleep(30)

    # Still running (not banked): the in-flight invoke blocked the bank.
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :running

    Process.exit(caller, :kill)
  end

  test "three consecutive bank failures fail the session and destroy its VM" do
    fail_bank = fn _ch, _req -> {:error, %GRPC.RPCError{status: 13, message: "boom"}} end
    ctx = start_stack(bank_fun: fail_bank)
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    # Each :maybe_bank admission STOPS the session process; a failed bank re-starts
    # it with a FRESH pid (strikes 1-2) or, at three, fails + destroys it. Wait for
    # the re-start (a new pid) before the next send so a strike is never dropped by
    # racing a not-yet-restarted process. Deterministic, not a fixed sleep (which
    # raced the async re-start once the store's clock became an Agent round-trip).
    for _ <- 1..3 do
      old =
        case Registry.lookup(ctx.registry, created.session_id) do
          [{pid, _}] ->
            send(pid, :maybe_bank)
            pid

          [] ->
            nil
        end

      wait_until(fn ->
        case Registry.lookup(ctx.registry, created.session_id) do
          [{pid, _}] -> pid != old
          [] -> match?({:ok, %{state: :failed}}, SessionStore.get(ctx.store, created.session_id))
        end
      end)
    end

    wait_state(ctx, created.session_id, :failed)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :failed
    assert_received {:destroyed, _vm}
  end

  # -- relight-on-invoke -----------------------------------------------------

  test "invoke on a banked session relights it and serves the parked caller" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    # After bank, the node must report the snapshot so placement can relight it.
    NodeCapacity.put(ctx.cap_table, "node-4", node_fact("wl", session_snapshots: [snap_fact(created.session_id)]))

    {:ok, resp} = SessionManager.invoke(ctx.mgr, created.session_id, %{method: "POST", path: "/", headers: %{}, body: "after-relight"})
    assert resp.status_code == 200
    assert resp.body == "after-relight"

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :running
    # A session_relit op landed (state back to running with a fresh VM).
    assert [{_pid, _}] = Registry.lookup(ctx.registry, created.session_id)
  end

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  test "invoke on a banked session whose bundle is gone locally but exported RESTORES then relights" do
    test_pid = self()

    restore_fun = fn _ch, req ->
      art = req.artifact
      send(test_pid, {:restored, art.kind, art.ref, art.workload})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{bytes_moved: 2_000, skipped: false}}
    end

    ctx = start_stack(restore_artifact_fun: restore_fun)
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    # The node reports NO session snapshots (a true local bundle miss) but a
    # reachable store: the invoke restores the SESSION bundle then relights against
    # the same anchor node (bypassing the snapshot-presence placement guard the fresh
    # restore has not yet reflected).
    NodeCapacity.put(ctx.cap_table, "node-4", node_fact("wl", session_snapshots: [], store_reachable: true))

    {:ok, resp} = SessionManager.invoke(ctx.mgr, created.session_id, %{method: "POST", path: "/", headers: %{}, body: "restored"})
    assert resp.status_code == 200
    assert resp.body == "restored"

    assert_received {:restored, :ARTIFACT_KIND_SESSION, ref, "wl"}
    assert ref == "snap-#{created.session_id}"

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :running

    {:ok, ops} = Embervm.OpLog.SQLite.read_from(ctx.op_log, 0)
    assert Enum.any?(ops, &(&1.kind == :artifact_restored))
  end

  test "the wake-rate limit 429s an excess relight-triggering invoke without a node hit" do
    test_pid = self()
    counting_relight = fn _ch, _req -> send(test_pid, :relight_called) && {:ok, %RelightResponse{vm_id: "vm-r"}} end

    # wake_max 1: the first banked-invoke relights, the second (before it completes)
    # 429s. We block the relight so both invokes race the same banked session.
    ctx = start_stack(relight_fun: counting_relight, wake_max: 1)
    put_workload(ctx, "wl")
    {:ok, a} = SessionManager.create(ctx.mgr, "wl", "p1")
    {:ok, b} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, a.session_id)
    :ok = force_idle_bank(ctx, b.session_id)

    NodeCapacity.put(ctx.cap_table, "node-4",
      node_fact("wl", session_snapshots: [snap_fact(a.session_id), snap_fact(b.session_id)])
    )

    # First banked-invoke: allowed (consumes the single wake token), relights a.
    {:ok, _} = SessionManager.invoke(ctx.mgr, a.session_id, %{body: "x"})
    # Second: same principal, over the wake_max of 1 within the window -> 429.
    assert {:error, :wake_rate_limited} = SessionManager.invoke(ctx.mgr, b.session_id, %{body: "y"})
  end

  test "an unrestorable snapshot fails the session and 410s the caller (snapshot_lost)" do
    lost_relight = fn _ch, _req -> {:error, %GRPC.RPCError{status: 9, message: "unrestorable"}} end
    ctx = start_stack(relight_fun: lost_relight)
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    NodeCapacity.put(ctx.cap_table, "node-4", node_fact("wl", session_snapshots: [snap_fact(created.session_id)]))

    assert {:error, {:gone, "snapshot_lost"}} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :failed
    assert session.terminal_reason == "snapshot_lost"
    # The lost snapshot was evicted.
    assert_received {:evicted, _ref}
  end

  test "a server-returned gRPC status on invoke does NOT invalidate the shared node channel" do
    test_pid = self()

    ctx =
      start_stack(
        # A FAILED_PRECONDITION from the daemon rode a HEALTHY channel to get here.
        assign_fun: fn _ch, _req -> {:error, %GRPC.RPCError{status: 9, message: "boom"}} end,
        invalidate_fun: fn node, ch -> send(test_pid, {:invalidated, node, ch}) && :ok end
      )

    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    assert {:error, {:rpc, 9}} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})
    # The shared channel must NOT be torn down on a server status (D-R2.7.2): doing
    # so would disconnect every other session multiplexed on that channel.
    refute_receive {:invalidated, _, _}, 200
  end

  test "a transport error on invoke DOES invalidate the shared node channel" do
    test_pid = self()

    ctx =
      start_stack(
        # A raw transport fault (not a %GRPC.RPCError{}) means the channel is bad.
        assign_fun: fn _ch, _req -> {:error, :closed} end,
        invalidate_fun: fn node, ch -> send(test_pid, {:invalidated, node, ch}) && :ok end
      )

    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    assert {:error, :closed} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})
    assert_receive {:invalidated, _, _}, 200
  end

  test "a bare invoke (no explicit path) forwards the workload's invokePath to the guest" do
    test_pid = self()

    capture_assign = fn _ch, req ->
      send(test_pid, {:guest_path, req.request.path})

      {:ok,
       %SessionAssignResponse{
         response: %GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
         usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1},
         suspect: false
       }}
    end

    ctx = start_stack(assign_fun: capture_assign)
    # The shim serves only /invoke; a bare invoke must forward THAT, not "/".
    put_workload(ctx, "wl", invoke_path: "/invoke")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    {:ok, _} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})
    assert_receive {:guest_path, "/invoke"}
  end

  test "an explicit request path overrides the workload invokePath" do
    test_pid = self()

    capture_assign = fn _ch, req ->
      send(test_pid, {:guest_path, req.request.path})

      {:ok,
       %SessionAssignResponse{
         response: %GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
         usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1},
         suspect: false
       }}
    end

    ctx = start_stack(assign_fun: capture_assign)
    put_workload(ctx, "wl", invoke_path: "/invoke")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    {:ok, _} = SessionManager.invoke(ctx.mgr, created.session_id, %{path: "/custom", body: "x"})
    assert_receive {:guest_path, "/custom"}
  end

  # -- adoption matrix -------------------------------------------------------

  test "adoption rebinds a running session VM to a fresh process after a restart" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    # Simulate a control-plane restart: kill the session process so ETS says running
    # but no process exists. The node still reports the live VM.
    [{pid, _}] = Registry.lookup(ctx.registry, created.session_id)
    {:ok, %{vm_id: vm_id}} = SessionStore.get(ctx.store, created.session_id)
    DynamicSupervisor.terminate_child(ctx.sup, pid)
    Process.sleep(10)
    assert Registry.lookup(ctx.registry, created.session_id) == []

    NodeCapacity.put(ctx.cap_table, "node-4",
      node_fact("wl", session_vms: [%{vm_id: vm_id, session_id: created.session_id, workload: "wl"}])
    )

    :ok = SessionManager.reconcile(ctx.mgr)

    # Rebound: a fresh process runs and the session is invokable.
    assert [{_pid, _}] = Registry.lookup(ctx.registry, created.session_id)
    {:ok, resp} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "z"})
    assert resp.status_code == 200
  end

  test "adoption heals a banked session (node reports only the snapshot, no process)" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    NodeCapacity.put(ctx.cap_table, "node-4", node_fact("wl", session_snapshots: [snap_fact(created.session_id)]))

    :ok = SessionManager.reconcile(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :banked
    assert Registry.lookup(ctx.registry, created.session_id) == []
  end

  test "adoption fails a session whose VM AND snapshot both vanished (node reporting)" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    [{pid, _}] = Registry.lookup(ctx.registry, created.session_id)
    DynamicSupervisor.terminate_child(ctx.sup, pid)
    Process.sleep(10)

    # The node is up but reports NO vm and NO snapshot for this session: authoritative
    # vanish -> failed.
    NodeCapacity.put(ctx.cap_table, "node-4", node_fact("wl", session_vms: [], session_snapshots: []))

    :ok = SessionManager.reconcile(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :failed
    assert session.terminal_reason == "failed"
  end

  test "adoption NEVER reaps on a transient disconnect (node absent from the facts)" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    [{pid, _}] = Registry.lookup(ctx.registry, created.session_id)
    DynamicSupervisor.terminate_child(ctx.sup, pid)
    Process.sleep(10)

    # The node is GONE from the capacity table (a disconnect, fail-closed drop): the
    # session must be left untouched (reaping would wipe the fleet, the #3517 lesson).
    NodeCapacity.drop(ctx.cap_table, "node-4")

    :ok = SessionManager.reconcile(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :running
  end

  test "adoption evicts an orphaned snapshot whose session row is terminal" do
    ctx = start_stack()
    put_workload(ctx, "wl")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)

    # The node still reports a snapshot for the destroyed session: it must be evicted.
    NodeCapacity.put(ctx.cap_table, "node-4", node_fact("wl", session_snapshots: [snap_fact(created.session_id)]))

    :ok = SessionManager.reconcile(ctx.mgr)
    assert_received {:evicted, _ref}
  end

  # -- expiry, GC, eviction --------------------------------------------------

  test "sweep expires a live session past its max lifetime (destroys the VM)" do
    ctx = start_stack()
    put_workload(ctx, "wl", max_lifetime_seconds: 1)
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    advance(ctx.clock_pid, 2_000)
    :ok = SessionManager.sweep(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :expired
    assert_received {:destroyed, _vm}
  end

  test "sweep expires a banked session past its max lifetime (evicts the snapshot)" do
    ctx = start_stack()
    put_workload(ctx, "wl", max_lifetime_seconds: 10)
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    advance(ctx.clock_pid, 20_000)
    :ok = SessionManager.sweep(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :expired
    assert_received {:evicted, _ref}
  end

  test "invoke-time expiry 410s a session past its deadline without waiting for a sweep" do
    ctx = start_stack()
    put_workload(ctx, "wl", max_lifetime_seconds: 1)
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

    advance(ctx.clock_pid, 2_000)
    assert {:error, {:gone, "expired"}} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :expired
  end

  test "banked-TTL GC evicts a banked session untouched past bankedTtlSeconds" do
    ctx = start_stack()
    put_workload(ctx, "wl", max_lifetime_seconds: 100_000, banked_ttl_seconds: 5)
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    advance(ctx.clock_pid, 10_000)
    :ok = SessionManager.sweep(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :evicted
    assert session.terminal_reason == "idle_ttl"
  end

  test "banked-TTL GC also reaps a parked session untouched past bankedTtlSeconds (#4305)" do
    ctx = start_stack()

    put_workload(ctx, "wl",
      max_lifetime_seconds: 100_000,
      banked_ttl_seconds: 5,
      persistence: %{memory: false, filesystem: %{enabled: true, size_bytes: 1_000_000}}
    )

    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_park(ctx, created.session_id)

    advance(ctx.clock_pid, 10_000)
    :ok = SessionManager.sweep(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :evicted
    assert session.terminal_reason == "idle_ttl"
    # evict_parked retires the workspace volume instead of EvictSnapshot (a parked
    # session has no snapshot to evict); retirement is a spawned RPC, so
    # assert_receive (waits) rather than assert_received (checks the mailbox now).
    assert_receive {:retired, lineage_id}, 1_000
    assert lineage_id == created.session_id
  end

  test "a parked session inside bankedTtlSeconds survives the sweep" do
    ctx = start_stack()

    put_workload(ctx, "wl",
      max_lifetime_seconds: 100_000,
      banked_ttl_seconds: 5,
      persistence: %{memory: false, filesystem: %{enabled: true, size_bytes: 1_000_000}}
    )

    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_park(ctx, created.session_id)

    advance(ctx.clock_pid, 3_000)
    :ok = SessionManager.sweep(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :parked
    refute_received {:retired, _}
  end

  test "a banked-TTL eviction ALSO issues EvictArtifact(remote) for the session bundle" do
    test_pid = self()

    evict_artifact_fun = fn _ch, req ->
      send(test_pid, {:remote_evicted, req.remote, req.artifact.kind, req.artifact.ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{bytes_freed: 2_000}}
    end

    ctx = start_stack(evict_artifact_fun: evict_artifact_fun)
    put_workload(ctx, "wl", max_lifetime_seconds: 100_000, banked_ttl_seconds: 5)
    {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")
    :ok = force_idle_bank(ctx, created.session_id)

    advance(ctx.clock_pid, 10_000)
    :ok = SessionManager.sweep(ctx.mgr)

    # The local EvictSnapshot fired AND the remote store copy was dropped on the same
    # trigger (a SESSION artifact, remote=true).
    assert_received {:evicted, _ref}
    assert_received {:remote_evicted, true, :ARTIFACT_KIND_SESSION, ref}
    assert ref == "snap-#{created.session_id}"
  end

  test "disk-pressure eviction removes the coldest banked sessions LRU to the watermark" do
    ctx = start_stack(disk_low_watermark_bytes: 5_000)
    put_workload(ctx, "wl", banked_ttl_seconds: 100_000, max_lifetime_seconds: 100_000)

    # Two banked sessions, each snapshot 2_000 bytes. a is invoked LATER than b, so b
    # is colder (evicted first).
    {:ok, a} = SessionManager.create(ctx.mgr, "wl", "p1")
    {:ok, b} = SessionManager.create(ctx.mgr, "wl", "p1")

    # Bank b first (older last_invoke via updated_at), then a. Both have nil
    # last_invoke_at, so sort_by falls back to 0 for both; give a a later invoke to
    # make it the warmer victim-last.
    :ok = force_idle_bank(ctx, b.session_id)
    advance(ctx.clock_pid, 1_000)
    {:ok, session_a} = SessionStore.get(ctx.store, a.session_id)
    # Record an invoke on a so its last_invoke_at is set (warmer than b's nil/0).
    assert session_a.state == :running
    _ = SessionStore.record_invoke(ctx.store, a.session_id, %{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1})
    :ok = force_idle_bank(ctx, a.session_id)

    # Node reports both snapshots and a free-bytes BELOW the watermark (pressure).
    NodeCapacity.put(ctx.cap_table, "node-4",
      node_fact("wl", free: 3_000, session_snapshots: [snap_fact(a.session_id), snap_fact(b.session_id)])
    )

    :ok = SessionManager.sweep(ctx.mgr)

    # b (coldest) is evicted; a (warmer, invoked) survives. One eviction lifts free
    # 3_000 + 2_000 = 5_000 >= watermark, so a is spared.
    {:ok, sb} = SessionStore.get(ctx.store, b.session_id)
    {:ok, sa} = SessionStore.get(ctx.store, a.session_id)
    assert sb.state == :evicted
    assert sb.terminal_reason == "disk_pressure"
    assert sa.state == :banked
  end

  defp snap_fact(session_id) do
    %{snapshot_ref: "snap-#{session_id}", session_id: session_id, workload: "wl", size_bytes: 2_000, created_at_unix_ms: 0}
  end

  # -- status.sessions counts (Task 9) ---------------------------------------

  describe "status.sessions counts" do
    test "the sweep writes {live,banked} + summary for a session workload" do
      test_pid = self()

      writer = fn ns, name, status_map ->
        send(test_pid, {:status, ns, name, status_map})
        :ok
      end

      ctx = start_stack(status_writer: writer)
      put_workload(ctx, "wl")
      {:ok, created} = SessionManager.create(ctx.mgr, "wl", "p1")

      :ok = SessionManager.sweep(ctx.mgr)

      assert_receive {:status, "embervm", "wl",
                      %{"sessions" => %{"live" => 1, "banked" => 0}, "sessionsSummary" => "1 live / 0 banked"}}

      # Bank it: the next sweep reflects live -> banked.
      :ok = force_idle_bank(ctx, created.session_id)
      :ok = SessionManager.sweep(ctx.mgr)

      assert_receive {:status, "embervm", "wl",
                      %{"sessions" => %{"live" => 0, "banked" => 1}, "sessionsSummary" => "0 live / 1 banked"}}
    end

    test "the write is DEBOUNCED: an unchanged count does not re-patch" do
      test_pid = self()

      writer = fn ns, name, status_map ->
        send(test_pid, {:status, ns, name, status_map})
        :ok
      end

      ctx = start_stack(status_writer: writer)
      put_workload(ctx, "wl")
      {:ok, _created} = SessionManager.create(ctx.mgr, "wl", "p1")

      :ok = SessionManager.sweep(ctx.mgr)
      assert_receive {:status, "embervm", "wl", %{"sessions" => %{"live" => 1, "banked" => 0}}}

      # A second sweep with the SAME counts must not write again (change-detected,
      # like the PoolManager's primedFloorSatisfied debounce).
      :ok = SessionManager.sweep(ctx.mgr)
      refute_receive {:status, "embervm", "wl", _}, 50
    end
  end
end
