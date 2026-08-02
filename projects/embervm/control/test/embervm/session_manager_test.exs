defmodule Embervm.SessionManagerTest do
  @moduledoc """
  Task 6 acceptance for Embervm.SessionManager + Embervm.Session: create from the
  primed pool (happy path + each denial), an invoke round-trip against a fake
  SessionAssign, the per-session queue cap, token-gated invoke (a management token
  is not a session token), and destroy from every non-terminal state.

  A FAKE DAEMON is injected via claim_fun / channel_fun / prime_fun and the
  session_opts' session_assign/destroy funs, exactly the seam idiom the dispatcher
  test uses. A real (unnamed) op-log + SessionStore give the real FSM and durable
  projection; unique ETS tables + a per-test DynamicSupervisor/Registry keep tests
  isolated from the application's supervised session subtree.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, SessionManager, SessionStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.{BankResponse, GuestResponse, PrimeResponse, RelightResponse, SessionAssignResponse, UsageStats}

  # A stub op-log whose append/2 blocks until the test sends :go, so a test can hold
  # one async write "in flight" (pending) while it runs a reconcile pass, modelling
  # the writer-stalled-between-RPC-and-append window the adopt-and-backfill repair
  # guards. Only append/2 is exercised (AsyncWriter calls nothing else).
  defmodule BlockingOpLog do
    def append(_server, op) do
      case op.payload do
        %{block_to: waiter} when is_pid(waiter) ->
          send(waiter, {:appending, op.session_id, self()})

          receive do
            :go -> {:ok, 1}
          end

        _ ->
          {:ok, 1}
      end
    end
  end

  # -- harness ---------------------------------------------------------------

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"scap_#{suffix}"
    cat_table = :"scat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_sessionmgr_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)

    # A shared AsyncWriter (ADR embervm/014 decision 2), threaded into BOTH the store
    # and the manager so an async-gated stack defers appends through the same writer
    # the manager's adopt-and-backfill discriminator queries. Off by default.
    async = Keyword.get(opts, :async_lifecycle_writes, false)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    # Optional store clock: the manager clock is fixed at 5_000_000, so a test that
    # exercises the stuck-in-destroying alarm passes store_clock: fn -> 0 end to make
    # a row's updated_at precede now and the elapsed compare positive.
    store_clock_opts =
      case Keyword.get(opts, :store_clock) do
        nil -> []
        c -> [clock: c]
      end

    {:ok, store} =
      SessionStore.start_link(
        [name: nil, op_log: op_log, async_writer: writer, async_lifecycle_writes: async] ++
          store_clock_opts
      )

    registry = :"sreg_#{suffix}"
    {:ok, _registry_pid} = Registry.start_link(keys: :unique, name: registry)
    {:ok, sup} = DynamicSupervisor.start_link(strategy: :one_for_one)

    # A test-controlled inbox lets a test observe the fake SessionAssign calls and
    # decide the response per call. Default: echo the body at 200 with usage.
    assign_fun = Keyword.get(opts, :assign_fun, &default_assign/2)

    session_opts = [
      channel_fun: Keyword.get(opts, :session_channel_fun, fn _node -> {:ok, :ch} end),
      assign_fun: assign_fun,
      destroy_fun: Keyword.get(opts, :destroy_fun, fn _ch, _vm -> {:ok, %{teardown_confirmed: true}} end),
      invalidate_fun: fn _node, _ch -> :ok end
    ]

    mgr_opts =
      [
        name: nil,
        session_store: store,
        supervisor: sup,
        registry: registry,
        capacity_table: cap_table,
        catalog_table: cat_table,
        clock: fn -> 5_000_000 end,
        channel_fun: Keyword.get(opts, :channel_fun, fake_channel_fun()),
        claim_fun: Keyword.get(opts, :claim_fun, fn _d, _n, _w -> {:ok, "vm-primed-#{suffix}"} end),
        prime_fun: Keyword.get(opts, :prime_fun, fn _ch, _req -> {:error, :no_prime} end),
        bank_fun: Keyword.get(opts, :bank_fun, fn _ch, req -> {:ok, %BankResponse{snapshot_ref: "snap-#{req.session_id}", size_bytes: 1_000}} end),
        relight_fun: Keyword.get(opts, :relight_fun, fn _ch, _req -> {:ok, %RelightResponse{vm_id: "vm-relit"}} end),
        delete_session_volume_fun: Keyword.get(opts, :delete_session_volume_fun, fn _ch, _req -> {:ok, %{}} end),
        session_opts: session_opts,
        async_writer: writer,
        async_lifecycle_writes: async
      ] ++
        Keyword.take(opts, [
          :quota_config,
          :quota_table,
          :node_confirmed_destroy,
          :destroying_alarm_ms,
          :orphan_grace_ms
        ])

    {:ok, mgr} = SessionManager.start_link(mgr_opts)

    %{
      mgr: mgr,
      store: store,
      op_log: op_log,
      writer: writer,
      cap_table: cap_table,
      cat_table: cat_table,
      registry: registry,
      sup: sup
    }
  end

  defp default_assign(_ch, req) do
    {:ok,
     %SessionAssignResponse{
       response: %GuestResponse{
         status_code: 200,
         headers: %{"x-echo" => "1", "content-type" => "text/plain"},
         body: req.request.body
       },
       usage: %UsageStats{cpu_ms: 10, peak_rss_mib: 32, wall_ms: 20},
       suspect: false
     }}
  end

  defp put_session_workload(ctx, wl, opts \\ []) do
    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{
        wl => %{
          free_primed_slots: 1,
          snapshot_ref: "snap-#{wl}",
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: []
        }
      },
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.get(opts, :max, 8),
      draining: false,
      updated_at: 5_000_000
    })

    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      class: Keyword.get(opts, :class, "session"),
      image_ref: "img@sha256:abc",
      invoke_path: "/",
      timeout_ms: Keyword.get(opts, :timeout_ms, 90_000),
      cap: Keyword.get(opts, :cap, 8),
      floor: 1,
      session:
        Keyword.get(opts, :session, %{
          idle_bank_seconds: 300,
          max_lifetime_seconds: Keyword.get(opts, :max_lifetime_seconds, 3600),
          banked_ttl_seconds: 3600,
          max_sessions: Keyword.get(opts, :max_sessions, 16),
          invoke_queue_cap: Keyword.get(opts, :queue_cap, 4)
        }),
      persistence: Keyword.get(opts, :persistence)
    })
  end

  defp fake_channel_fun, do: fn _node -> {:ok, :fake_channel} end

  defp fake_claim_fun(vm_id), do: fn _dispatcher, _node, _workload -> {:ok, vm_id} end

  defp fake_prime_fun(vm_id), do: fn _channel, _req -> {:ok, %PrimeResponse{vm_id: vm_id}} end

  defp persistence_workload_opts(opts \\ []) do
    Keyword.merge(
      [persistence: %{memory: false, filesystem: %{enabled: true, size_bytes: 1_000_000}}],
      opts
    )
  end

  defp create_persistence_session(ctx, opts \\ []) do
    workload = Keyword.get(opts, :workload, "wl-persist")
    put_session_workload(ctx, workload, persistence_workload_opts(opts))

    {:ok, created} =
      SessionManager.create(
        ctx.mgr,
        workload,
        Keyword.get(opts, :principal, "p1")
      )

    created
  end

  defp park_session(ctx, created) do
    assert :ok = SessionManager.bank(ctx.mgr, created.session_id)
    {:ok, parked} = SessionStore.get(ctx.store, created.session_id)
    assert parked.state == :parked
    parked
  end

  defp wait_for_state(ctx, session_id, expected, attempts \\ 100)
  defp wait_for_state(_ctx, _session_id, _expected, 0), do: flunk("session did not reach expected state")

  defp wait_for_state(ctx, session_id, expected, attempts) do
    case SessionStore.get(ctx.store, session_id) do
      {:ok, %{state: ^expected} = session} -> session
      _ ->
        Process.sleep(10)
        wait_for_state(ctx, session_id, expected, attempts - 1)
    end
  end

  # A per-INSTANCE capacity fact on a node (Step 5 size-aware create). Two co-located
  # bricks share configured_id "node-4" so the placement row records the node, then
  # dial_instance selects the mem-eligible one among them.
  defp put_brick(ctx, wl, pod_uid, opts) do
    node_id = Keyword.get(opts, :node_id, "node-4")

    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      node_id: node_id,
      configured_id: node_id,
      instance_id: "#{node_id}/#{pod_uid}",
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom, 8_000),
      mem_budget_mib: Keyword.get(opts, :mem_budget, 8_192),
      workloads: %{
        wl => %{
          free_primed_slots: 1,
          snapshot_ref: Keyword.get(opts, :snapshot_ref, "snap-#{wl}"),
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: []
        }
      },
      live_vms: 0,
      max_live_vms: 8,
      draining: false,
      updated_at: 5_000_000
    })
  end

  # -- create ----------------------------------------------------------------

  test "size-aware create: a too-small co-located brick + a big brick claims on the big one" do
    parent = self()

    ctx =
      start_stack(
        claim_fun: fn _d, dial_id, _w ->
          send(parent, {:claimed, dial_id})
          {:ok, "vm-big"}
        end
      )

    # A 4Gi-need session workload; the 2Gi brick cannot boot it, the 8Gi can.
    WorkloadCatalog.upsert(ctx.cat_table, "wl-a", %{
      name: "wl-a",
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:abc",
      invoke_path: "/",
      timeout_ms: 90_000,
      cap: 8,
      floor: 1,
      mem_mib: 4_000,
      session: %{
        idle_bank_seconds: 300,
        max_lifetime_seconds: 3600,
        banked_ttl_seconds: 3600,
        max_sessions: 16,
        invoke_queue_cap: 4
      }
    })

    put_brick(ctx, "wl-a", "small", size_class: "2gi", mem_headroom: 100, mem_budget: 2_048)
    put_brick(ctx, "wl-a", "big", size_class: "8gi", mem_headroom: 8_000, mem_budget: 8_192)

    {:ok, created} = SessionManager.create(ctx.mgr, "wl-a", "p1")
    assert String.starts_with?(created.session_id, "s-")
    # The claim (and hence the VM) dialed the big instance, never the too-small one.
    assert_receive {:claimed, "node-4/big"}
    refute_received {:claimed, "node-4/small"}

    # The session ROW records the NODE name (node-4) so relight/drain read it
    # node-scoped, not the dial instance_id.
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.node_id == "node-4"
  end

  test "fleet placement skips a too-small rendezvous winner" do
    parent = self()
    suffix =
      Enum.find(0..100_000, fn n ->
        wl = "wl-b4-session-#{n}"
        :erlang.phash2({wl, "node-a"}, 4_294_967_296) >
          :erlang.phash2({wl, "node-b"}, 4_294_967_296)
      end)

    wl = "wl-b4-session-#{suffix}"
    node_a_hash = :erlang.phash2({wl, "node-a"}, 4_294_967_296)
    node_b_hash = :erlang.phash2({wl, "node-b"}, 4_294_967_296)

    ctx =
      start_stack(
        claim_fun: fn _d, dial_id, _w ->
          send(parent, {:claimed, dial_id})
          {:ok, "vm-#{dial_id}"}
        end
      )

    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:abc",
      invoke_path: "/",
      timeout_ms: 90_000,
      cap: 8,
      floor: 1,
      mem_mib: 4_000,
      session: %{idle_bank_seconds: 300, max_lifetime_seconds: 3600, banked_ttl_seconds: 3600, max_sessions: 16, invoke_queue_cap: 4}
    })

    put_brick(ctx, wl, "a", node_id: "node-a", size_class: "2gi", mem_headroom: 100, mem_budget: 2_048, snapshot_ref: "snap-a")
    put_brick(ctx, wl, "b", node_id: "node-b", size_class: "8gi", mem_headroom: 8_000, mem_budget: 8_192, snapshot_ref: "snap-b")

    # This assertion encodes that the old rendezvous stage would have chosen the node that cannot fit this guest.
    assert node_a_hash > node_b_hash
    {:ok, _created} = SessionManager.create(ctx.mgr, wl, "p1")
    assert_receive {:claimed, "node-b/b"}
    refute_received {:claimed, "node-a/a"}
  end

  test "the per-invoke dial targets the OWNER instance_id, not the node-name alias (task #4)" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    ctx =
      start_stack(
        # The claim lands the VM on the big brick.
        claim_fun: fn _d, _dial_id, _w -> {:ok, "vm-owner"} end,
        session_channel_fun: capture_channel
      )

    WorkloadCatalog.upsert(ctx.cat_table, "wl-a", %{
      name: "wl-a",
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:abc",
      invoke_path: "/",
      timeout_ms: 90_000,
      cap: 8,
      floor: 1,
      mem_mib: 4_000,
      session: %{
        idle_bank_seconds: 300,
        max_lifetime_seconds: 3600,
        banked_ttl_seconds: 3600,
        max_sessions: 16,
        invoke_queue_cap: 4
      }
    })

    # Two co-located bricks: the 2Gi cannot boot the 4Gi-need session, so dial_instance
    # picks node-4/big. The node-name alias would collapse to whichever brick
    # registered last; the per-invoke dial must target node-4/big regardless.
    put_brick(ctx, "wl-a", "small", size_class: "2gi", mem_headroom: 100, mem_budget: 2_048)
    put_brick(ctx, "wl-a", "big", size_class: "8gi", mem_headroom: 8_000, mem_budget: 8_192)

    {:ok, created} = SessionManager.create(ctx.mgr, "wl-a", "p1")

    {:ok, _resp} =
      SessionManager.invoke(ctx.mgr, created.session_id, %{method: "POST", path: "/", headers: %{}, body: "hi"})

    # The invoke's SessionAssign dialled the owning instance, never the node alias.
    keys = Agent.get(dialed, & &1)
    assert "node-4/big" in keys
    refute "node-4" in keys
    refute "node-4/small" in keys
  end

  test "create happy path: claims a primed VM, mints a token, starts a live session" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-a")

    {:ok, created} = SessionManager.create(ctx.mgr, "wl-a", "p1")

    assert String.starts_with?(created.session_id, "s-")
    assert is_binary(created.token)
    assert created.expires_at == 5_000_000 + 3600 * 1000

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :running
    # A live session has a process registered under its id.
    assert [{_pid, _}] = Registry.lookup(ctx.registry, created.session_id)
  end

  test "create denies unknown workload (404-shaped reason)" do
    ctx = start_stack()
    assert {:error, {:denied, :unknown_workload}} = SessionManager.create(ctx.mgr, "nope", "p1")
  end

  test "create denies a task-class workload" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-task", class: "task")
    assert {:error, {:denied, :not_session_class}} = SessionManager.create(ctx.mgr, "wl-task", "p1")
  end

  test "create denies at the session cap (live + banked >= maxSessions)" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-cap", max_sessions: 1)

    {:ok, _} = SessionManager.create(ctx.mgr, "wl-cap", "p1")
    assert {:error, {:denied, :session_cap}} = SessionManager.create(ctx.mgr, "wl-cap", "p1")
  end

  test "create denies when no node has ready capacity" do
    ctx = start_stack()
    # Catalog present but no capacity facts: pick_node returns :no_capacity.
    WorkloadCatalog.upsert(ctx.cat_table, "wl-nocap", %{
      name: "wl-nocap",
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:abc",
      invoke_path: "/",
      timeout_ms: 90_000,
      cap: 8,
      floor: 1,
      session: %{idle_bank_seconds: 300, max_lifetime_seconds: 3600, banked_ttl_seconds: 3600, max_sessions: 16, invoke_queue_cap: 4}
    })

    assert {:error, {:denied, :no_capacity}} = SessionManager.create(ctx.mgr, "wl-nocap", "p1")
  end

  test "create primes on a claim miss and fails to :no_capacity when prime fails" do
    ctx = start_stack(claim_fun: fn _d, _n, _w -> :miss end, prime_fun: fn _ch, _req -> {:error, :boom} end)
    put_session_workload(ctx, "wl-miss")

    assert {:error, {:denied, :no_capacity}} = SessionManager.create(ctx.mgr, "wl-miss", "p1")
  end

  test "create quota fail-closed denies a principal over budget" do
    quota_table = :"squota_#{System.unique_integer([:positive])}"
    :ets.new(quota_table, [:set, :public, :named_table])
    # Budget of 0 for p1 = hard stop; used(0) < 0 is false, so denied.
    quota = %{budgets: %{"p1" => 0.0}, default: nil}

    ctx = start_stack(quota_config: quota, quota_table: quota_table)
    put_session_workload(ctx, "wl-q")

    assert {:error, {:denied, :quota}} = SessionManager.create(ctx.mgr, "wl-q", "p1")
    # A principal with no budget is allowed (opt-in quota).
    assert {:ok, _} = SessionManager.create(ctx.mgr, "wl-q", "p2")
  end

  # -- invoke ----------------------------------------------------------------

  test "invoke round-trips against the fake daemon, returning the guest response verbatim" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-inv")

    {:ok, created} = SessionManager.create(ctx.mgr, "wl-inv", "p1")

    req = %{method: "POST", path: "/", headers: %{}, body: "hello"}
    {:ok, resp} = SessionManager.invoke(ctx.mgr, created.session_id, req)

    assert resp.status_code == 200
    assert resp.body == "hello"
    assert resp.headers["x-echo"] == "1"

    # The invoke was recorded (last_invoke_at set, usage charged).
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert is_integer(session.last_invoke_at)
  end

  test "invoke on an unknown session is :not_found" do
    ctx = start_stack()
    assert {:error, :not_found} = SessionManager.invoke(ctx.mgr, "s-unknown", %{body: "x"})
  end

  test "invoke on a terminal session returns {:gone, reason}" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-gone")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-gone", "p1")
    {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)

    assert {:error, {:gone, "destroyed"}} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})
  end

  test "persistence_only_session_parks_on_idle" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-persist"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)

    parked = park_session(ctx, created)

    assert parked.volume_node_id == "node-4"
    assert parked.node_id == nil
    assert parked.vm_id == nil
    assert {:ok, _} = SessionStore.verify_token(ctx.store, created.session_id, created.token)
  end

  test "persistence session destroyed before parking deletes its volume" do
    parent = self()
    ctx = start_stack(
      prime_fun: fake_prime_fun("vm-persist-destroy"),
      channel_fun: fake_channel_fun(),
      delete_session_volume_fun: fn _ch, req ->
        send(parent, {:volume_deleted, req.lineage_id})
        {:ok, %{}}
      end
    )
    created = create_persistence_session(ctx)
    session_id = created.session_id

    {:ok, created_row} = SessionStore.get(ctx.store, created.session_id)
    assert created_row.volume_node_id == "node-4"
    assert {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)
    assert_receive {:volume_deleted, ^session_id}, 1_000
  end

  test "node-confirmed destroy deletes a persistence volume after confirmation" do
    parent = self()
    ctx = start_stack(
      node_confirmed_destroy: true,
      prime_fun: fake_prime_fun("vm-persist-confirmed-destroy"),
      channel_fun: fake_channel_fun(),
      delete_session_volume_fun: fn _ch, req ->
        send(parent, {:volume_deleted, req.lineage_id})
        {:ok, %{}}
      end
    )
    created = create_persistence_session(ctx)
    session_id = created.session_id

    assert {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)
    assert_receive {:volume_deleted, ^session_id}, 1_000
  end

  test "invoke on a parking session is transiently not ready" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-parking"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)
    {:ok, _} = SessionStore.transition(ctx.store, created.session_id, :park, :session_parking,
      %{reason: "idled", volume_node_id: "node-4"}, %{volume_node_id: "node-4"})

    assert {:error, {:not_ready, :parking}} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "retry"})
  end

  test "successful rejoin disarms the park-once failure handler" do
    {:ok, attempts} = Agent.start_link(fn -> 0 end)
    ctx = start_stack(
      prime_fun: fake_prime_fun("vm-rejoin-once"),
      channel_fun: fake_channel_fun(),
      relight_fun: fn _channel, _req -> {:ok, %RelightResponse{vm_id: "vm-rejoin-once"}} end,
      assign_fun: fn _ch, req ->
        case Agent.get_and_update(attempts, fn n -> {n, n + 1} end) do
          0 -> default_assign(:ch, req)
          _ -> {:error, :delivery_failed}
        end
      end
    )
    created = create_persistence_session(ctx)
    park_session(ctx, created)

    assert {:ok, _} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "rejoin"})
    assert {:error, :delivery_failed} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "fail"})

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :failed
    assert :session_failed in op_kinds_for(ctx, created.session_id)
  end

  test "crash-window park completes to parked on restart adoption" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-crash-window"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)
    {:ok, _} = SessionStore.transition(ctx.store, created.session_id, :park, :session_parking,
      %{reason: "idled", volume_node_id: "node-4"}, %{volume_node_id: "node-4"})

    assert :ok = SessionManager.reconcile(ctx.mgr)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :parked
    assert :session_parking in op_kinds_for(ctx, created.session_id)
    assert :session_parked in op_kinds_for(ctx, created.session_id)
  end

  test "memory false with filesystem disabled destroys on idle, not parks" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-no-fs", persistence: %{memory: false, filesystem: %{enabled: false}})
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-no-fs", "p1")

    assert :ok = SessionManager.bank(ctx.mgr, created.session_id)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed
    assert :session_destroyed in op_kinds_for(ctx, created.session_id)
  end

  test "invoke_on_parked_reprimes_on_volume_node" do
    ctx =
      start_stack(
        prime_fun: fake_prime_fun("vm-rejoined"),
        channel_fun: fake_channel_fun(),
        relight_fun: fn _channel, _req -> {:ok, %RelightResponse{vm_id: "vm-rejoined"}} end
      )

    created = create_persistence_session(ctx)
    parked = park_session(ctx, created)
    req = %{method: "POST", path: "/", headers: %{}, body: "hello"}

    assert {:ok, response} = SessionManager.invoke(ctx.mgr, created.session_id, req)
    assert response.status_code == 200
    assert response.body == "hello"

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :running
    assert session.volume_node_id == parked.volume_node_id
    assert session.node_id == parked.volume_node_id
    assert session.vm_id == "vm-rejoined"
  end

  test "rejoin delivery failure destroys the primed VM and leaves session parked" do
    parent = self()
    ctx = start_stack(
      prime_fun: fake_prime_fun("vm-rejoin-failed"),
      channel_fun: fake_channel_fun(),
      assign_fun: fn _ch, _req -> {:error, :delivery_failed} end,
      destroy_fun: fn _ch, vm -> send(parent, {:destroyed, vm}); {:ok, %{teardown_confirmed: true}} end
    )
    created = create_persistence_session(ctx)
    park_session(ctx, created)

    assert {:error, _} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "retry"})
    assert_receive {:destroyed, "vm-rejoin-failed"}, 1_000
    # Wait for the rejoin_assign_failed handler to complete the two-phase park
    parked = wait_for_state(ctx, created.session_id, :parked)
    assert parked.state == :parked
    assert {:ok, _} = SessionStore.verify_token(ctx.store, created.session_id, created.token)
  end

  test "parked session with filesystem disabled cannot rejoin an empty workspace" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-fs-disabled"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)
    park_session(ctx, created)
    {:ok, entry} = WorkloadCatalog.fetch(ctx.cat_table, "wl-persist")
    WorkloadCatalog.upsert(ctx.cat_table, "wl-persist", %{entry | persistence: %{memory: false, filesystem: %{enabled: false}}})

    assert {:error, {:gone, "persistence_disabled"}} =
             SessionManager.invoke(ctx.mgr, created.session_id, %{body: "must not rejoin"})
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed
  end

  test "volume delete is best effort when destroying parked session" do
    parent = self()
    ctx = start_stack(
      prime_fun: fake_prime_fun("vm-volume-delete"),
      channel_fun: fake_channel_fun(),
      delete_session_volume_fun: fn _ch, req -> send(parent, {:volume_deleted, req.lineage_id}); {:error, :node_gone} end
    )
    created = create_persistence_session(ctx)
    session_id = created.session_id
    park_session(ctx, created)

    assert {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)
    assert_receive {:volume_deleted, ^session_id}, 1_000
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed
  end

  test "rejoin_prime_failure_leaves_parked" do
    # Use a counter to make prime_fun work for create but fail for rejoin
    {:ok, counter} = Agent.start_link(fn -> 0 end)
    prime_fun = fn _channel, _req ->
      n = Agent.get_and_update(counter, fn c -> {c, c + 1} end)
      if n == 0 do
        # First call (create) succeeds
        {:ok, %PrimeResponse{vm_id: "vm-rejoin-prime-fail"}}
      else
        # Subsequent calls (rejoin) fail
        {:error, :no_capacity}
      end
    end
    
    ctx = start_stack(prime_fun: prime_fun, channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)
    park_session(ctx, created)

    assert {:error, {:relight_failed, :no_capacity}} =
             SessionManager.invoke(ctx.mgr, created.session_id, %{body: "retry"})

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :parked
    assert {:ok, _} = SessionStore.verify_token(ctx.store, created.session_id, created.token)
  end

  test "volume_node_gone_prevents_rejoin" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-rejoined"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)
    park_session(ctx, created)
    NodeCapacity.drop(ctx.cap_table, "node-4")

    assert {:error, {:relight_failed, :no_capacity}} =
             SessionManager.invoke(ctx.mgr, created.session_id, %{body: "retry"})

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :parked
    assert NodeCapacity.fetch(ctx.cap_table, "node-4") == :error
  end

  test "parked_session_expires_on_ttl" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-expire"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx, max_lifetime_seconds: -1)
    park_session(ctx, created)

    assert :ok = SessionManager.sweep(ctx.mgr)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :expired
    assert :session_expired in op_kinds_for(ctx, created.session_id)
  end

  test "parked_session_can_be_destroyed" do
    ctx = start_stack(prime_fun: fake_prime_fun("vm-destroy"), channel_fun: fake_channel_fun())
    created = create_persistence_session(ctx)
    park_session(ctx, created)

    assert {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed
    assert :session_destroyed in op_kinds_for(ctx, created.session_id)
  end

  test "non_persistence_sessions_unaffected" do
    ctx =
      start_stack(
        claim_fun: fake_claim_fun("vm-regular"),
        channel_fun: fake_channel_fun(),
        bank_fun: fn _channel, req ->
          {:ok, %BankResponse{snapshot_ref: "snap-#{req.session_id}", size_bytes: 1_000}}
        end,
        relight_fun: fn _channel, _req -> {:ok, %RelightResponse{vm_id: "vm-regular-relit"}} end
      )

    put_session_workload(ctx, "wl-regular")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-regular", "p1")
    assert :ok = SessionManager.bank(ctx.mgr, created.session_id)

    banked = wait_for_state(ctx, created.session_id, :banked)
    assert banked.volume_node_id == nil

    {:ok, fact} = NodeCapacity.fetch(ctx.cap_table, "node-4")
    NodeCapacity.put(ctx.cap_table, "node-4", Map.put(fact, :session_snapshots, [
      %{session_id: created.session_id, snapshot_ref: banked.snapshot_ref, workload: "wl-regular"}
    ]))

    result = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "relight"})
    # On failure, surface the session row: terminal_reason/detail name which fail
    # path fired, which a bare match error hides (this test has flaked in CI).
    {:ok, row_now} = SessionStore.get(ctx.store, created.session_id)
    assert match?({:ok, _response}, result),
           "invoke=#{inspect(result)} session=#{inspect(row_now)}"

    running = wait_for_state(ctx, created.session_id, :running)
    assert running.state == :running
    assert running.volume_node_id == nil
  end

  test "the queue cap rejects pile-ups past invokeQueueCap with :queue_full" do
    # A blocking assign_fun (never replies until told) lets us fill the queue: one
    # invoke runs, `queue_cap` more wait, and the next is rejected.
    test_pid = self()

    blocking_assign = fn _ch, req ->
      send(test_pid, {:assign_started, self()})

      receive do
        :go ->
          {:ok,
           %SessionAssignResponse{
             response: %GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
             usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1},
             suspect: false
           }}
      end
    end

    ctx = start_stack(assign_fun: blocking_assign)
    put_session_workload(ctx, "wl-full", queue_cap: 2)

    {:ok, created} = SessionManager.create(ctx.mgr, "wl-full", "p1")

    # Fire the in-flight invoke + 2 queued (fills the cap of 2 waiters).
    callers =
      for _ <- 1..3 do
        spawn(fn ->
          SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})
        end)
      end

    # Wait until the first invoke is actually in flight (worker started).
    assert_receive {:assign_started, _worker}, 1_000

    # Give the queued callers a moment to enqueue behind the in-flight one.
    Process.sleep(50)

    # The 4th invoke exceeds the cap (1 in-flight + 2 waiting): queue_full.
    assert {:error, :queue_full} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "y"})

    Enum.each(callers, fn pid -> Process.exit(pid, :kill) end)
  end

  test "a suspect/transport failure on invoke fails the session and 502s the caller" do
    fail_assign = fn _ch, _req ->
      {:ok, %SessionAssignResponse{response: nil, usage: nil, suspect: true}}
    end

    ctx = start_stack(assign_fun: fail_assign)
    put_session_workload(ctx, "wl-fail")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-fail", "p1")

    assert {:error, :suspect} = SessionManager.invoke(ctx.mgr, created.session_id, %{body: "x"})

    # The session is now failed (a guest in unknown mid-request state is not reused).
    Process.sleep(50)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :failed
    assert session.terminal_reason == "failed"
  end

  # -- destroy ---------------------------------------------------------------

  test "destroy from running tears down the process and records session_destroyed" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-d")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-d", "p1")

    assert {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed

    # The process is gone.
    Process.sleep(20)
    assert Registry.lookup(ctx.registry, created.session_id) == []
  end

  test "destroy of an already-terminal session is a no-op success" do
    ctx = start_stack()
    put_session_workload(ctx, "wl-d2")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-d2", "p1")
    {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)

    assert {:ok, :already_terminal} = SessionManager.destroy(ctx.mgr, created.session_id)
  end

  test "destroy of an unknown session is :not_found" do
    ctx = start_stack()
    assert {:error, :not_found} = SessionManager.destroy(ctx.mgr, "s-nope")
  end

  # -- node-confirmed destroy (ADR embervm/014 decision 5, gated) -------------

  test "gated: destroy with a confirming node records destroying then destroyed" do
    # A destroy_fun that confirms teardown drives the full destroying -> destroyed
    # sequence; both ops land in order and the session ends destroyed.
    ctx =
      start_stack(
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, _vm -> {:ok, %{teardown_confirmed: true}} end
      )

    put_session_workload(ctx, "wl-ncd")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-ncd", "p1")

    assert {:ok, _} = SessionManager.destroy(ctx.mgr, created.session_id)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed

    kinds = op_kinds_for(ctx, created.session_id)
    assert :session_destroying in kinds
    assert :session_destroyed in kinds
    # destroying is appended BEFORE destroyed.
    assert index_of(kinds, :session_destroying) < index_of(kinds, :session_destroyed)
  end

  test "gated: destroy with an unconfirming node stays destroying, no destroyed op" do
    # teardown_confirmed=false (an old daemon, or a partial reap) must NOT record
    # destroyed; the session stays destroying for the reconcile loop.
    ctx =
      start_stack(
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, _vm -> {:ok, %{teardown_confirmed: false}} end
      )

    put_session_workload(ctx, "wl-ncd2")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-ncd2", "p1")

    assert {:ok, :destroying} = SessionManager.destroy(ctx.mgr, created.session_id)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroying

    kinds = op_kinds_for(ctx, created.session_id)
    assert :session_destroying in kinds
    refute :session_destroyed in kinds
  end

  test "gated: reconcile re-drives a destroying session to destroyed once confirmed" do
    # First destroy leaves it destroying (unconfirmed); a later reconcile with a
    # confirming node completes it. The node keeps reporting the VM, so the reconcile
    # retries the teardown RPC.
    parent = self()

    ctx =
      start_stack(
        node_confirmed_destroy: true,
        session_channel_fun: fn _node -> {:ok, :ch} end,
        destroy_fun: fn _ch, _vm ->
          send(parent, :destroy_called)
          {:ok, %{teardown_confirmed: true}} end
      )

    put_session_workload(ctx, "wl-ncd3")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-ncd3", "p1")

    # Force the session into destroying with the node still reporting its VM, then
    # reconcile: the re-drive re-issues destroy, confirms, and records destroyed.
    {:ok, _} =
      SessionStore.transition(ctx.store, created.session_id, :begin_destroy, :session_destroying, %{reason: :destroyed}, %{})

    report_session_vm(ctx, created.session_id, "wl-ncd3")

    :ok = SessionManager.reconcile(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroyed
  end

  test "gated: a session stuck in destroying alarms ONCE across reconciles, not every tick" do
    # Unconfirming teardown so the session stays destroying across both reconciles.
    # store_clock: fn -> 0 end sets updated_at before the manager's fixed 5_000_000
    # clock, so elapsed exceeds the alarm threshold and the alarm is eligible each tick.
    ctx =
      start_stack(
        node_confirmed_destroy: true,
        destroying_alarm_ms: 100,
        store_clock: fn -> 0 end,
        session_channel_fun: fn _node -> {:ok, :ch} end,
        destroy_fun: fn _ch, _vm -> {:ok, %{teardown_confirmed: false}} end
      )

    put_session_workload(ctx, "wl-alarm")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-alarm", "p1")

    {:ok, _} =
      SessionStore.transition(ctx.store, created.session_id, :begin_destroy, :session_destroying, %{reason: :destroyed}, %{})

    report_session_vm(ctx, created.session_id, "wl-alarm")

    log =
      ExUnit.CaptureLog.capture_log(fn ->
        :ok = SessionManager.reconcile(ctx.mgr)
        :ok = SessionManager.reconcile(ctx.mgr)
      end)

    # Teardown never confirmed, so it is still destroying and BOTH reconciles were
    # alarm-eligible, but the dedup logs the stuck error exactly once.
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :destroying

    hits = (log |> String.split("session stuck in destroying") |> length()) - 1
    assert hits == 1, "expected exactly one stuck-in-destroying alarm, got #{hits}"

    assert MapSet.member?(:sys.get_state(ctx.mgr).destroying_alarmed, created.session_id)
  end

  # Both vanished tests pin the store clock BELOW the node facts' 5_000_000 so the
  # session row predates the fact: the vanish branch requires the node's report to
  # postdate the row (node_fact_authoritative?), and the store's default wall clock
  # would leave the fact permanently stale and the branch unreachable.
  test "test_banked_session_with_vanished_vm_and_snapshot_evicts_not_loops" do
    ctx = start_stack(evict_fun: fn _ch, _req -> {:ok, %{}} end, store_clock: fn -> 4_000_000 end)
    put_session_workload(ctx, "wl-vanished-banked")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-vanished-banked", "p1")
    bank_session(ctx, created.session_id)
    report_empty_node(ctx)

    :ok = SessionManager.reconcile(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :evicted
  end

  test "co-located node facts use the freshest timestamp for vanished snapshot reaping" do
    ctx = start_stack(evict_fun: fn _ch, _req -> {:ok, %{}} end, store_clock: fn -> 4_500_000 end)
    put_session_workload(ctx, "wl-fresh-fact")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-fresh-fact", "p1")
    bank_session(ctx, created.session_id)

    for {pod_uid, updated_at} <- [{"old", 4_000_000}, {"fresh", 5_000_000}] do
      NodeCapacity.put(ctx.cap_table, {"node-4", pod_uid}, %{
        node_id: "node-4",
        configured_id: "node-4",
        instance_id: "node-4/#{pod_uid}",
        workloads: %{},
        session_vms: [],
        session_snapshots: [],
        live_vms: 0,
        max_live_vms: 8,
        draining: false,
        updated_at: updated_at
      })
    end

    :ok = SessionManager.reconcile(ctx.mgr)
    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :evicted
  end

  test "test_running_session_with_vanished_vm_and_snapshot_fails" do
    ctx = start_stack(store_clock: fn -> 4_000_000 end)
    put_session_workload(ctx, "wl-vanished-running")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-vanished-running", "p1")
    report_empty_node(ctx)

    :ok = SessionManager.reconcile(ctx.mgr)

    {:ok, session} = SessionStore.get(ctx.store, created.session_id)
    assert session.state == :failed
  end

  test "test_repeated_adoption_sweep_does_not_double_log_unapplicable_transition" do
    ctx = start_stack(evict_fun: fn _ch, _req -> {:ok, %{}} end)
    put_session_workload(ctx, "wl-no-double-warning")
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-no-double-warning", "p1")
    bank_session(ctx, created.session_id)
    report_empty_node(ctx)

    log =
      ExUnit.CaptureLog.capture_log(fn ->
        :ok = SessionManager.reconcile(ctx.mgr)
        :ok = SessionManager.reconcile(ctx.mgr)
      end)

    hits = (log |> String.split("session fail transition failed") |> length()) - 1
    assert hits == 0
  end


  test "gated: reconcile destroys an orphan reported session VM with no CP row" do
    parent = self()

    ctx =
      start_stack(
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, vm ->
          send(parent, {:orphan_destroyed, vm})
          {:ok, %{teardown_confirmed: true}} end
      )

    # A node reports a session VM the control plane has no row for: an orphan to
    # destroy, not adopt.
    report_session_vm(ctx, "s-orphan", "wl-orphan", "vm-orphan")

    :ok = SessionManager.reconcile(ctx.mgr)

    assert_received {:orphan_destroyed, "vm-orphan"}
    # No CP row was created for it (never adopted).
    assert :error = SessionStore.get(ctx.store, "s-orphan")
  end

  test "gated async: reconcile ADOPTS a rowless reported VM with a pending async write, not destroy" do
    parent = self()

    # Async gate ON (so the discriminator is live) + node-confirmed-destroy ON (so the
    # Direction-2 pass runs at all). destroy_fun records any destroy so we can assert
    # it is NEVER called for the adopted VM.
    ctx =
      start_stack(
        async_lifecycle_writes: true,
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, vm ->
          send(parent, {:destroyed, vm})
          {:ok, %{teardown_confirmed: true}}
        end
      )

    # Hold one async write in flight for vm-race: enqueue a blocking append through
    # the shared writer so AsyncWriter.pending?(writer, "vm-race") is true across the
    # reconcile. This models the writer stalled between RPC success and the
    # session_created append landing.
    op = %Embervm.OpLog.Op{
      kind: :session_created,
      tenant: "t",
      session_id: "s-race",
      ts: 0,
      payload: %{block_to: parent}
    }

    Embervm.AsyncWriter.enqueue(ctx.writer, %{
      op: op,
      op_log_mod: BlockingOpLog,
      op_log: :ignored,
      vm_id: "vm-race"
    })

    # Wait until the writer has entered the (blocked) append: the vm is now pending.
    assert_receive {:appending, "s-race", appender}
    assert Embervm.AsyncWriter.pending?(ctx.writer, "vm-race")

    # The node reports the live VM but the CP has no row (its create append is the one
    # stalled above). The reconcile must ADOPT (skip destroy), not orphan-destroy it.
    report_session_vm(ctx, "s-race", "wl-race", "vm-race")
    :ok = SessionManager.reconcile(ctx.mgr)

    refute_received {:destroyed, "vm-race"}

    # Release the stalled append; the write completes normally afterward.
    send(appender, :go)
  end

  test "gated async off the discriminator: a truly rowless VM with NO pending write is still destroyed" do
    parent = self()

    # Async gate ON but NO pending write for vm-orphan2: the discriminator returns
    # false, so PR 1's orphan-destroy still fires (adopt-and-backfill only spares a
    # VM with a write actually in flight).
    ctx =
      start_stack(
        async_lifecycle_writes: true,
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, vm ->
          send(parent, {:destroyed, vm})
          {:ok, %{teardown_confirmed: true}}
        end
      )

    report_session_vm(ctx, "s-orphan2", "wl-orphan2", "vm-orphan2")
    :ok = SessionManager.reconcile(ctx.mgr)

    assert_received {:destroyed, "vm-orphan2"}
    assert :error = SessionStore.get(ctx.store, "s-orphan2")
  end

  test "gated async: a rowless report whose vm a LIVE row owns is adopted+backfilled, not destroyed" do
    parent = self()

    ctx =
      start_stack(
        async_lifecycle_writes: true,
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, vm ->
          send(parent, {:destroyed, vm})
          {:ok, %{teardown_confirmed: true}}
        end
      )

    # A live session row owns vm-owned (created here so its ETS row + durable create
    # exist). The node reports a DIFFERENT session_id but the SAME vm: the report's
    # session_id resolves to :error, yet the vm is owned by a live row -> the
    # discriminator picks the backfill arm and re-drives session_created (idempotent),
    # never destroying the legitimately-owned VM.
    {:ok, owned} =
      SessionStore.create(ctx.store, %{
        tenant: "homelab",
        principal: "p1",
        workload: "wl-owned",
        node_id: "node-4",
        vm_id: "vm-owned",
        base_snapshot_ref: "base@sha256:abc",
        base_digest: "sha256:abc",
        expires_at: 9_999_999
      })

    report_session_vm(ctx, "s-report-mismatch", "wl-owned", "vm-owned")
    :ok = SessionManager.reconcile(ctx.mgr)

    refute_received {:destroyed, "vm-owned"}
    # The owning row is intact and non-terminal (never destroyed).
    {:ok, still} = SessionStore.get(ctx.store, owned.session_id)
    refute still.state in [:destroyed, :failed, :expired, :evicted]
  end

  test "gated: reconcile never destroys a primed-pool VM" do
    # A primed VM is reported in primed_vm_ids, never in session_vms, so the orphan
    # destroy pass (which only walks session_vms) never touches it.
    parent = self()

    ctx =
      start_stack(
        node_confirmed_destroy: true,
        destroy_fun: fn _ch, vm ->
          send(parent, {:destroyed, vm})
          {:ok, %{teardown_confirmed: true}} end
      )

    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{
        "wl-primed" => %{
          free_primed_slots: 1,
          snapshot_ref: "snap-primed",
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: ["vm-primed-x"]
        }
      },
      session_vms: [],
      live_vms: 1,
      max_live_vms: 8,
      draining: false,
      updated_at: 5_000_000
    })

    :ok = SessionManager.reconcile(ctx.mgr)

    refute_received {:destroyed, "vm-primed-x"}
  end

  # -- gated-destroy test helpers --------------------------------------------

  defp op_kinds_for(ctx, session_id) do
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)

    ops
    |> Enum.filter(&(&1.session_id == session_id))
    |> Enum.map(& &1.kind)
  end

  defp index_of(list, elem), do: Enum.find_index(list, &(&1 == elem))

  defp report_session_vm(ctx, session_id, workload, vm_id \\ nil) do
    vm_id = vm_id || "vm-#{session_id}"

    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{},
      session_vms: [%{session_id: session_id, vm_id: vm_id, workload: workload}],
      live_vms: 1,
      max_live_vms: 8,
      draining: false,
      updated_at: 5_000_000
    })
  end

  # Direct unit tests for transport deadline arithmetic, ensuring the fix
  # (transport timeout EXCEEDS application deadline) cannot be reverted unnoticed.
  defp bank_session(ctx, session_id) do
    SessionStore.adopt_state(ctx.store, session_id, :banked)
  end

  defp report_empty_node(ctx) do
    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{},
      session_vms: [],
      session_snapshots: [],
      live_vms: 0,
      max_live_vms: 8,
      draining: false,
      updated_at: 5_000_000
    })
  end

  describe "transport_timeout/1" do
    test "non-default positive timeout_ms adds headroom" do
      # Use 45s (non-default) so a hardcoded constant cannot pass by luck.
      assert Embervm.Session.transport_timeout(45_000) == 50_000
    end

    test "default timeout_ms (90s) adds headroom" do
      assert Embervm.Session.transport_timeout(90_000) == 95_000
    end

    test "nil timeout_ms falls back to 95s" do
      assert Embervm.Session.transport_timeout(nil) == 95_000
    end

    test "zero timeout_ms falls back to 95s" do
      assert Embervm.Session.transport_timeout(0) == 95_000
    end

    test "transport timeout ALWAYS exceeds application deadline (invariant)" do
      # Property: for any positive timeout_ms, result > timeout_ms.
      # This is the bug that was fixed: the old code defaulted to 10s < 90s.
      for timeout_ms <- [1, 10, 100, 1_000, 50_000, 90_000] do
        result = Embervm.Session.transport_timeout(timeout_ms)
        assert result > timeout_ms, "transport timeout #{result} must exceed deadline #{timeout_ms}"
      end
    end
  end
end
