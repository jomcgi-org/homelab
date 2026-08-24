defmodule Embervm.SessionCreateIdempotencyTest do
  @moduledoc """
  #4919 acceptance: idempotent session creation with a client-supplied key.

  Covers the four required behaviours plus the store-level contract:

    * a same-key retry returns the SAME live session (no duplicate row, no
      second token, no capacity consumed);
    * two concurrent same-key creates (the second parked behind the first's
      in-flight create) resolve to exactly ONE durable session;
    * reusing a key after its session is DESTROYED is a conflict, never a
      silent fresh session;
    * malformed and oversized keys are rejected before anything is placed;
    * different keys and an absent key behave exactly like today;
    * the binding is scoped per principal and survives a store rebuild from
      the durable op-log projection.

  The harness is the session_manager_test stack idiom: a real unnamed SQLite
  op-log + real SessionStore give the real FSM and durable projection, with
  fake daemon seams; unique ETS tables keep tests isolated.
  """

  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, SessionManager, SessionStore, WorkloadCatalog}
  alias Embervm.Node.V1.PrimeResponse
  alias Embervm.OpLog.SQLite

  setup do
    %{ctx: start_stack()}
  end

  # -- harness ---------------------------------------------------------------

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"icap_#{suffix}"
    cat_table = :"icat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_session_idem_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)
    {:ok, store} = SessionStore.start_link(name: nil, op_log: op_log, async_writer: writer)

    registry = :"ireg_#{suffix}"
    {:ok, _} = Registry.start_link(keys: :unique, name: registry)
    {:ok, sup} = DynamicSupervisor.start_link(strategy: :one_for_one)

    claim_fun =
      Keyword.get(opts, :claim_fun, fn _dispatcher, _node, _workload -> {:ok, "vm-#{suffix}"} end)

    {:ok, mgr} =
      SessionManager.start_link(
        name: nil,
        session_store: store,
        supervisor: sup,
        registry: registry,
        capacity_table: cap_table,
        catalog_table: cat_table,
        clock: fn -> 5_000_000 end,
        monotonic_clock: fn -> -800_000 end,
        channel_fun: fn _node -> {:ok, :fake_channel} end,
        claim_fun: claim_fun,
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-primed-#{suffix}"}} end,
        bank_fun: fn _ch, req -> {:ok, %{snapshot_ref: "snap-#{req.session_id}", size_bytes: 1_000}} end,
        relight_fun: fn _ch, _req -> {:ok, %{vm_id: "vm-relit"}} end,
        evict_fun: fn _ch, _req -> {:ok, %{}} end,
        evict_artifact_fun: fn _ch, _req -> {:ok, %{}} end,
        restore_artifact_fun: fn _ch, _req -> {:error, %GRPC.RPCError{status: 5}} end,
        archive_volume_fun: fn _ch, _req -> {:ok, %{skipped: false}} end,
        retire_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        delete_session_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        session_opts: [
          channel_fun: fn _node -> {:ok, :ch} end,
          assign_fun: fn _ch, req ->
            {:ok,
             %Embervm.Node.V1.SessionAssignResponse{
               response: %Embervm.Node.V1.GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
               usage: nil,
               suspect: false
             }}
          end,
          destroy_fun: fn _ch, _vm -> {:ok, %{teardown_confirmed: true}} end,
          invalidate_fun: fn _node, _ch -> :ok end
        ],
        async_writer: writer,
        op_log: op_log,
        op_log_mod: SQLite
      )

    ctx = %{
      mgr: mgr,
      store: store,
      op_log: op_log,
      cap_table: cap_table,
      cat_table: cat_table,
      registry: registry,
      sup: sup,
      path: path
    }

    put_session_workload(ctx, "wl-idem")
    ctx
  end

  defp put_session_workload(ctx, wl) do
    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      pod_uid: "pod-node-4",
      configured_id: "node-4",
      workloads: %{
        wl => %{
          free_primed_slots: 1,
          snapshot_ref: "snap-#{wl}",
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: []
        }
      },
      live_vms: 0,
      max_live_vms: 8,
      draining: false,
      store_reachable: true,
      updated_at: 5_000_000
    })

    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      class: "session",
      image_ref: "img@sha256:abc",
      invoke_path: "/",
      timeout_ms: 90_000,
      cap: 8,
      floor: 1,
      session: %{
        idle_bank_seconds: 300,
        max_lifetime_seconds: 3600,
        banked_ttl_seconds: 3600,
        max_sessions: 16,
        invoke_queue_cap: 4
      },
      mem_mib: nil,
      persistence: nil
    })
  end

  defp eventually(fun), do: eventually(fun, 200)

  defp eventually(_fun, 0), do: flunk("condition not met in time")

  defp eventually(fun, n) do
    if fun.(), do: :ok, else: (Process.sleep(10) && eventually(fun, n - 1))
  end

  defp created_ops(op_log) do
    {:ok, ops} = SQLite.read_from(op_log, 0)
    Enum.filter(ops, &(&1.kind == :session_created))
  end

  defp destroy_and_settle(ctx, session_id) do
    {:ok, _} = SessionManager.destroy(ctx.mgr, session_id)
    eventually(fn ->
      match?({:ok, %{state: :destroyed}}, SessionStore.get(ctx.store, session_id))
    end)
  end

  # -- same-key retry --------------------------------------------------------

  test "a same-key retry returns the SAME live session without minting a duplicate", %{ctx: ctx} do
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "run/42/step")

    assert %{session_id: session_id, token: token} = created
    refute Map.get(created, :replayed)

    {:ok, replay} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "run/42/step")

    # The SAME session, its current state, no fresh capability token.
    assert replay.session_id == session_id
    assert replay.replayed == true
    refute Map.has_key?(replay, :token)
    assert token != nil

    # Exactly one durable session and one create op; the retry consumed nothing.
    assert %{live: 1, banked: 0} = SessionStore.counts(ctx.store, "wl-idem")
    assert length(created_ops(ctx.op_log)) == 1
  end

  test "the store resolves a session by (principal, key), scoped to that principal", %{ctx: ctx} do
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "scope-key")

    assert {:ok, %{session_id: session_id}} =
             SessionStore.get_by_idempotency_key(ctx.store, "principal-a", "scope-key")

    assert session_id == created.session_id
    # A different principal's binding is invisible: keys are unique PER PRINCIPAL.
    assert :error = SessionStore.get_by_idempotency_key(ctx.store, "principal-b", "scope-key")
    assert :error = SessionStore.get_by_idempotency_key(ctx.store, "principal-a", "other-key")
  end

  # -- concurrency -----------------------------------------------------------

  test "two concurrent same-key creates yield exactly one session", %{ctx: ctx} do
    # Re-stack with a claim gate so create A is provably mid-worker when B lands.
    GenServer.stop(ctx.mgr)
    test_pid = self()

    {:ok, mgr2} =
      SessionManager.start_link(
        name: nil,
        session_store: ctx.store,
        supervisor: ctx.sup,
        registry: ctx.registry,
        capacity_table: ctx.cap_table,
        catalog_table: ctx.cat_table,
        clock: fn -> 5_000_000 end,
        monotonic_clock: fn -> -800_000 end,
        channel_fun: fn _node -> {:ok, :fake_channel} end,
        claim_fun: fn _d, _n, _w ->
          # Signal ENTRY (with this worker's pid so the test can release it)
          # then hold the create mid-worker until the test sends :go HERE.
          send(test_pid, {:claim_entered, self()})

          receive do
            :go -> {:ok, "vm-gated"}
          end
        end,
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-primed"}} end,
        bank_fun: fn _ch, req -> {:ok, %{snapshot_ref: "snap-#{req.session_id}", size_bytes: 1_000}} end,
        relight_fun: fn _ch, _req -> {:ok, %{vm_id: "vm-relit"}} end,
        evict_fun: fn _ch, _req -> {:ok, %{}} end,
        evict_artifact_fun: fn _ch, _req -> {:ok, %{}} end,
        restore_artifact_fun: fn _ch, _req -> {:error, %GRPC.RPCError{status: 5}} end,
        archive_volume_fun: fn _ch, _req -> {:ok, %{skipped: false}} end,
        retire_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        delete_session_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        session_opts: [
          channel_fun: fn _node -> {:ok, :ch} end,
          assign_fun: fn _ch, req ->
            {:ok,
             %Embervm.Node.V1.SessionAssignResponse{
               response: %Embervm.Node.V1.GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
               usage: nil,
               suspect: false
             }}
          end,
          destroy_fun: fn _ch, _vm -> {:ok, %{teardown_confirmed: true}} end,
          invalidate_fun: fn _node, _ch -> :ok end
        ],
        async_writer: Embervm.AsyncWriter,
        op_log: ctx.op_log,
        op_log_mod: SQLite
      )

    task_a =
      Task.async(fn -> SessionManager.create(mgr2, "wl-idem", "principal-a", nil, idempotency_key: "conc") end)

    # Deterministic overlap: A is inside its worker (in-flight registered) before
    # B arrives, and A cannot SETTLE until the test releases its worker, so B can
    # only park behind it (or, in the degenerate race where :go lands first,
    # replay the finished winner: the same session either way).
    assert_receive {:claim_entered, worker_a}, 2_000

    task_b =
      Task.async(fn -> SessionManager.create(mgr2, "wl-idem", "principal-a", nil, idempotency_key: "conc") end)

    Process.sleep(20)
    send(worker_a, :go)

    {:ok, res_a} = Task.await(task_a)
    {:ok, res_b} = Task.await(task_b)

    assert res_a.session_id == res_b.session_id

    # Exactly ONE response carries the capability token; the other is a replay view.
    tokens = [Map.get(res_a, :token), Map.get(res_b, :token)]
    assert Enum.count(tokens, &is_binary/1) == 1
    replays = [Map.get(res_a, :replayed), Map.get(res_b, :replayed)]
    assert Enum.count(replays, & &1) == 1

    assert %{live: 1} = SessionStore.counts(ctx.store, "wl-idem")
    assert length(created_ops(ctx.op_log)) == 1
  end

  test "concurrent same-key creates from DIFFERENT principals are independent", %{ctx: ctx} do
    {:ok, a} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "shared")
    {:ok, b} = SessionManager.create(ctx.mgr, "wl-idem", "principal-b", nil, idempotency_key: "shared")

    refute a.session_id == b.session_id
    assert %{live: 2} = SessionStore.counts(ctx.store, "wl-idem")
  end

  # -- reuse after destroy ---------------------------------------------------

  test "reusing a key after its session is destroyed CONFLICTS instead of silently creating", %{ctx: ctx} do
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "dead/run")
    destroy_and_settle(ctx, created.session_id)

    assert {:error, {:conflict, :session_idempotency_key_reused}} =
             SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "dead/run")

    # Still exactly one row (terminal), no second session ever minted.
    assert length(created_ops(ctx.op_log)) == 1
    assert %{live: 0, banked: 0} = SessionStore.counts(ctx.store, "wl-idem")
  end

  test "the binding survives a store rebuild from the durable projection", %{ctx: ctx} do
    {:ok, created} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "restart-k")

    GenServer.stop(ctx.mgr)
    GenServer.stop(ctx.store)

    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)
    {:ok, store} = SessionStore.start_link(name: nil, op_log: ctx.op_log, async_writer: writer)

    {:ok, mgr} =
      SessionManager.start_link(
        name: nil,
        session_store: store,
        supervisor: ctx.sup,
        registry: ctx.registry,
        capacity_table: ctx.cap_table,
        catalog_table: ctx.cat_table,
        clock: fn -> 5_000_000 end,
        monotonic_clock: fn -> -800_000 end,
        channel_fun: fn _node -> {:ok, :fake_channel} end,
        claim_fun: fn _d, _n, _w -> {:ok, "vm-x"} end,
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-primed"}} end,
        bank_fun: fn _ch, req -> {:ok, %{snapshot_ref: "snap-#{req.session_id}", size_bytes: 1_000}} end,
        relight_fun: fn _ch, _req -> {:ok, %{vm_id: "vm-relit"}} end,
        evict_fun: fn _ch, _req -> {:ok, %{}} end,
        evict_artifact_fun: fn _ch, _req -> {:ok, %{}} end,
        restore_artifact_fun: fn _ch, _req -> {:error, %GRPC.RPCError{status: 5}} end,
        archive_volume_fun: fn _ch, _req -> {:ok, %{skipped: false}} end,
        retire_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        delete_session_volume_fun: fn _ch, _req -> {:ok, %{}} end,
        session_opts: [
          channel_fun: fn _node -> {:ok, :ch} end,
          assign_fun: fn _ch, req ->
            {:ok,
             %Embervm.Node.V1.SessionAssignResponse{
               response: %Embervm.Node.V1.GuestResponse{status_code: 200, headers: %{}, body: req.request.body},
               usage: nil,
               suspect: false
             }}
          end,
          destroy_fun: fn _ch, _vm -> {:ok, %{teardown_confirmed: true}} end,
          invalidate_fun: fn _node, _ch -> :ok end
        ],
        async_writer: writer,
        op_log: ctx.op_log,
        op_log_mod: SQLite
      )

    # Rebuild recovered the index: the same key still resolves to the SAME session.
    {:ok, replay} = SessionManager.create(mgr, "wl-idem", "principal-a", nil, idempotency_key: "restart-k")
    assert replay.session_id == created.session_id
    assert replay.replayed == true
    assert length(created_ops(ctx.op_log)) == 1
  end

  # -- malformed / oversized keys ---------------------------------------------

  test "oversized keys are rejected before anything is placed", %{ctx: ctx} do
    oversized = String.duplicate("k", 201)

    assert {:error, {:denied, :invalid_idempotency_key}} =
             SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: oversized)

    assert %{live: 0} = SessionStore.counts(ctx.store, "wl-idem")
    assert created_ops(ctx.op_log) == []
  end

  test "malformed keys (non-printable, non-ASCII) are rejected", %{ctx: ctx} do
    for bad <- ["has\0nul", "cl\xc3\xa9", "\u0007bell"] do
      assert {:error, {:denied, :invalid_idempotency_key}} =
               SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: bad),
             "expected #{inspect(bad)} to be rejected"
    end

    assert created_ops(ctx.op_log) == []
  end

  # -- unchanged behaviour ----------------------------------------------------

  test "different keys and an absent key behave as today (fresh sessions)", %{ctx: ctx} do
    {:ok, a} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "k1")
    {:ok, b} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "k2")
    {:ok, c} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil)

    ids = [a.session_id, b.session_id, c.session_id]
    assert length(Enum.uniq(ids)) == 3

    for res <- [a, b, c] do
      assert is_binary(Map.fetch!(res, :token))
      refute Map.get(res, :replayed)
    end

    assert %{live: 3} = SessionStore.counts(ctx.store, "wl-idem")

    # An empty header normalizes to ABSENT (no key), matching the
    # restore_lineage tolerance idiom: it is a normal create, not an error.
    {:ok, d} = SessionManager.create(ctx.mgr, "wl-idem", "principal-a", nil, idempotency_key: "")
    refute Map.get(d, :replayed)
    assert %{live: 4} = SessionStore.counts(ctx.store, "wl-idem")
  end
end
