defmodule Embervm.SessionStoreTest do
  @moduledoc """
  Exercises Embervm.SessionStore against a real (unnamed) Embervm.OpLog.SQLite on
  a fresh temp file per test, mirroring the TaskStore test idiom: each async test
  gets an independent op-log + store pair. Proves the write-through discipline
  (op-log before ETS), the token capability (right/wrong/terminal), the
  per-workload counts, and boot-rebuild equivalence from the durable projection.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.SQLite
  alias Embervm.SessionStore

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_sessionstore_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_pair(path, opts \\ []) do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    clock = Keyword.get(opts, :clock, sequential_clock())
    id_fun = Keyword.get(opts, :id_fun, sequential_id_fun())

    {:ok, store} =
      SessionStore.start_link(op_log: op_log, name: nil, clock: clock, id_fun: id_fun)

    {op_log, store}
  end

  defp sequential_id_fun do
    {:ok, counter} = Agent.start_link(fn -> 0 end)

    fn ->
      n = Agent.get_and_update(counter, fn n -> {n + 1, n + 1} end)
      "s-test-#{n}"
    end
  end

  defp sequential_clock do
    {:ok, counter} = Agent.start_link(fn -> 1_000 end)
    fn -> Agent.get_and_update(counter, fn n -> {n, n + 1} end) end
  end

  defp create(store, opts \\ []) do
    SessionStore.create(store, %{
      tenant: "homelab",
      principal: Keyword.get(opts, :principal, "p1"),
      workload: Keyword.get(opts, :workload, "wl-a"),
      node_id: Keyword.get(opts, :node_id, "node-4"),
      vm_id: Keyword.get(opts, :vm_id, "vm-1"),
      base_snapshot_ref: "base:img@sha256:abc",
      base_digest: "sha256:abc",
      expires_at: Keyword.get(opts, :expires_at, 999_999)
    })
  end

  # -- create ----------------------------------------------------------------

  test "create mints an id + token, returns the token once, stores only its hash", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, created} = create(store)

    assert created.session_id == "s-test-1"
    assert is_binary(created.token)
    assert created.state == :running
    assert created.base_digest == "sha256:abc"

    {:ok, session} = SessionStore.get(store, created.session_id)
    assert session.state == :running
    assert session.token_sha256 == Embervm.SessionId.token_sha256(created.token)
    # The plaintext token is NEVER stored.
    refute session.token_sha256 == created.token

    # The durable projection agrees (write-through: op before ETS).
    {:ok, [row]} = SQLite.load_sessions(op_log)
    assert row.session_id == "s-test-1"
    assert row.state == "running"
    assert row.token_sha256 == session.token_sha256
    # The plaintext token is nowhere in the durable row.
    refute row.token_sha256 == created.token
  end

  test "create records residency and the live count", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, created} = create(store, workload: "wl-x", node_id: "node-4", vm_id: "vm-9")

    assert SessionStore.residency(store, created.session_id) == {:ok, {"node-4", "vm-9"}}
    assert SessionStore.counts(store, "wl-x") == %{live: 1, banked: 0}
  end

  # -- lineage_id (#4306 slice 1) ---------------------------------------------

  test "create mints lineage_id equal to session_id (no adoption path exists yet)", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, created} = create(store)
    {:ok, session} = SessionStore.get(store, created.session_id)

    assert session.lineage_id == session.session_id
  end

  test "get_latest_by_lineage returns the session by its lineage_id, or :error", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, created} = create(store)

    # lineage_id == session_id this slice, so the session's own id IS its lineage.
    assert {:ok, session} = SessionStore.get_latest_by_lineage(store, created.session_id)
    assert session.session_id == created.session_id

    assert SessionStore.get_latest_by_lineage(store, "no-such-lineage") == :error
  end

  test "get_latest_by_lineage sees a terminal session too (the orphan-reconcile needs this)",
       %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, created} = create(store)

    {:ok, _} =
      SessionStore.transition(store, created.session_id, :fail, :session_failed, %{reason: :failed}, %{})

    assert {:ok, session} = SessionStore.get_latest_by_lineage(store, created.session_id)
    assert session.state == :failed
  end

  test "an old-style session_created op with no lineage_id in its payload projects with lineage_id defaulted to session_id",
       %{path: path} do
    {op_log, _store} = start_pair(path)

    # Simulates an op appended before #4306 slice 1 existed: the payload carries
    # none of the fields introduced since. Constructed directly against the
    # op-log (bypassing SessionStore.create, which always sends lineage_id now)
    # to exercise the backward-compat default at the projection layer itself.
    old_op = %Embervm.OpLog.Op{
      kind: :session_created,
      tenant: "homelab",
      principal: "p1",
      workload: "wl-old",
      session_id: "s-legacy-1",
      ts: 5_000,
      payload: %{token_sha256: "deadbeef", state: "running"}
    }

    {:ok, _seq} = SQLite.append(op_log, old_op)

    {:ok, rows} = SQLite.load_sessions(op_log)
    row = Enum.find(rows, &(&1.session_id == "s-legacy-1"))
    assert row.lineage_id == "s-legacy-1"

    # And the hot-set rebuild agrees.
    {:ok, store2} = SessionStore.start_link(op_log: op_log, name: nil)
    {:ok, session} = SessionStore.get(store2, "s-legacy-1")
    assert session.lineage_id == "s-legacy-1"
  end

  # -- token capability ------------------------------------------------------

  test "verify_token accepts the session's own token and rejects another session's", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, a} = create(store, vm_id: "vm-a")
    {:ok, b} = create(store, vm_id: "vm-b")

    assert {:ok, _} = SessionStore.verify_token(store, a.session_id, a.token)
    # b's token grants nothing on a (a token authenticates exactly one session id).
    assert SessionStore.verify_token(store, a.session_id, b.token) == {:error, :unauthorized}
    # An unknown session is :not_found.
    assert SessionStore.verify_token(store, "s-nope", a.token) == {:error, :not_found}
  end

  test "verify_token rejects every token on a terminal session", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, a} = create(store)

    {:ok, _} =
      SessionStore.transition(store, a.session_id, :destroy, :session_destroyed, %{reason: :destroyed}, %{})

    assert SessionStore.verify_token(store, a.session_id, a.token) == {:error, :terminal}
  end

  # -- transitions (write-through) -------------------------------------------

  test "an illegal transition is rejected and leaves ETS + the op-log untouched", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, a} = create(store)

    # running -> relight is illegal (only banked can relight).
    assert {:error, {:illegal_transition, :running, :relight}} =
             SessionStore.transition(store, a.session_id, :relight, :session_relit, %{}, %{})

    # State unchanged.
    {:ok, session} = SessionStore.get(store, a.session_id)
    assert session.state == :running

    # Only the session_created op is in the journal (no session_relit appended).
    {:ok, ops} = SQLite.read_from(op_log, 0)
    kinds = Enum.map(ops, & &1.kind)
    assert kinds == [:session_created]
  end

  test "mark park is a legal transition from running", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, session} = create(store)

    # The :park mark is transient, moving to :parking
    assert {:ok, parking} = SessionStore.mark(store, session.session_id, :park)
    assert parking.state == :parking

    # The :park_complete transition is durable, moving to :parked
    assert {:ok, parked} =
             SessionStore.transition(store, session.session_id, :park_complete, :session_parked, %{}, %{})

    assert parked.state == :parked
  end

  test "parked sessions count in the disk bucket once park completes", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, session} = create(store, workload: "wl-parked")

    # running -> mark :park -> :parking (transient): still holds the VM, still live.
    assert {:ok, _} = SessionStore.mark(store, session.session_id, :park)
    assert SessionStore.counts(store, "wl-parked") == %{live: 1, banked: 0}

    # :parking -> transition :park_complete -> :parked: the VM is torn down
    # (session_manager.ex park_session nulls node_id/vm_id), leaving only the
    # workspace volume, so a parked session buckets with banked, not live.
    assert {:ok, _} =
             SessionStore.transition(store, session.session_id, :park_complete, :session_parked, %{}, %{})

    assert SessionStore.counts(store, "wl-parked") == %{live: 0, banked: 1}

    # :parked -> mark :relight -> :relighting: the wake attempt claims a VM
    # again, so it crosses back out of the disk bucket into live.
    assert {:ok, _} = SessionStore.mark(store, session.session_id, :relight)
    assert SessionStore.counts(store, "wl-parked") == %{live: 1, banked: 0}

    # :relighting -> mark :parked_abort -> :parked: a failed relight recovers
    # to parked (no VM claimed), so it crosses back into the disk bucket.
    assert {:ok, _} = SessionStore.mark(store, session.session_id, :parked_abort)
    assert SessionStore.counts(store, "wl-parked") == %{live: 0, banked: 1}
  end

  test "park transition persists the volume node id", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, session} = create(store, node_id: "node-4", vm_id: "vm-1")

    # First mark the transient :park state
    assert {:ok, _} = SessionStore.mark(store, session.session_id, :park)

    # Then transition to :parked with the volume_node_id persisted
    assert {:ok, parked} =
             SessionStore.transition(
               store,
               session.session_id,
               :park_complete,
               :session_parked,
               %{reason: "idled", volume_node_id: "node-4"},
               %{volume_node_id: "node-4", node_id: nil, vm_id: nil}
             )

    assert parked.state == :parked
    assert parked.volume_node_id == "node-4"
    assert parked.node_id == nil
    assert parked.vm_id == nil
    {:ok, [row]} = SQLite.load_sessions(op_log)
    assert row.volume_node_id == "node-4"
  end

  test "parked_abort is a legal transient mark", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, session} = create(store)

    # running -> mark :park -> :parking
    assert {:ok, _} = SessionStore.mark(store, session.session_id, :park)

    # :parking -> transition :park_complete -> :parked
    assert {:ok, _} =
             SessionStore.transition(store, session.session_id, :park_complete, :session_parked, %{}, %{})

    # :parked -> transition :relight -> :relighting
    assert {:ok, _} =
             SessionStore.transition(store, session.session_id, :relight, :session_relit, %{}, %{})

    # :relighting -> mark :parked_abort -> :parked (recovery from failed relight)
    assert {:ok, recovered} = SessionStore.mark(store, session.session_id, :parked_abort)
    assert recovered.state == :parked
  end

  test "a terminal transition records the reason and drops residency", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, a} = create(store, workload: "wl-t")
    assert SessionStore.counts(store, "wl-t") == %{live: 1, banked: 0}

    {:ok, updated} =
      SessionStore.transition(store, a.session_id, :fail, :session_failed, %{reason: :failed}, %{})

    assert updated.state == :failed
    assert updated.terminal_reason == "failed"
    assert SessionStore.residency(store, a.session_id) == :error
    assert SessionStore.counts(store, "wl-t") == %{live: 0, banked: 0}
  end

  test "record_invoke bumps last_invoke_at and usage without moving state", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, a} = create(store)
    usage = %{cpu_ms: 100, peak_rss_mib: 64, wall_ms: 200}

    {:ok, updated} = SessionStore.record_invoke(store, a.session_id, usage)
    assert updated.state == :running
    assert is_integer(updated.last_invoke_at)

    # The usage projection accumulated (D12.1): vcpu_seconds = 100/1000.
    {:ok, page} = SQLite.list_usage(op_log, principal: "p1", limit: :infinity)
    [row] = page.items
    assert_in_delta row.vcpu_seconds, 0.1, 0.0001

    # A session_invoked op is in the journal, after session_created.
    {:ok, ops} = SQLite.read_from(op_log, 0)
    assert Enum.map(ops, & &1.kind) == [:session_created, :session_invoked]
  end

  test "record_invoke on a non-running session is rejected", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, a} = create(store)
    {:ok, _} = SessionStore.transition(store, a.session_id, :destroy, :session_destroyed, %{reason: :destroyed}, %{})

    assert SessionStore.record_invoke(store, a.session_id, %{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}) ==
             {:error, :not_running}
  end

  # -- boot rebuild ----------------------------------------------------------

  test "a fresh store rebuilds ETS + counts exactly from the durable projection", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, a} = create(store, workload: "wl-a", vm_id: "vm-a")
    {:ok, b} = create(store, workload: "wl-a", vm_id: "vm-b")
    {:ok, c} = create(store, workload: "wl-b", vm_id: "vm-c")
    {:ok, d} = create(store, workload: "wl-a", vm_id: "vm-d")

    # Take one to a terminal state so the rebuild must reproduce a terminal row too.
    {:ok, _} = SessionStore.transition(store, c.session_id, :fail, :session_failed, %{reason: :failed}, %{})

    # Take another all the way to :parked so the rebuild must reproduce it into
    # the disk (banked) bucket, not live.
    {:ok, _} = SessionStore.mark(store, d.session_id, :park)

    {:ok, _} =
      SessionStore.transition(store, d.session_id, :park_complete, :session_parked, %{}, %{})

    # A new store against the SAME op-log rebuilds from the projection alone.
    {:ok, store2} = SessionStore.start_link(op_log: op_log, name: nil)

    {:ok, sa} = SessionStore.get(store2, a.session_id)
    {:ok, sb} = SessionStore.get(store2, b.session_id)
    {:ok, sc} = SessionStore.get(store2, c.session_id)
    {:ok, sd} = SessionStore.get(store2, d.session_id)

    assert sa.state == :running
    assert sb.state == :running
    assert sc.state == :failed
    assert sc.terminal_reason == "failed"
    assert sd.state == :parked

    # lineage_id (#4306 slice 1) survives the rebuild too, for every row
    # regardless of terminal state: it is a plain durable column like any other.
    assert sa.lineage_id == sa.session_id
    assert sb.lineage_id == sb.session_id
    assert sc.lineage_id == sc.session_id
    assert sd.lineage_id == sd.session_id

    # Counts reproduce: two live plus one banked (parked) on wl-a, zero live on
    # wl-b (its one session failed).
    assert SessionStore.counts(store2, "wl-a") == %{live: 2, banked: 1}
    assert SessionStore.counts(store2, "wl-b") == %{live: 0, banked: 0}

    # Token hashes survive the rebuild (so a token minted pre-restart still verifies).
    assert {:ok, _} = SessionStore.verify_token(store2, a.session_id, a.token)

    # Residency is NOT rebuilt from the durable node_id (adoption's job, PR-4): a
    # fresh boot has empty residency until the node reports.
    assert SessionStore.residency(store2, a.session_id) == :error
  end

  # -- node-confirmed destroy: destroying intent (ADR embervm/014 decision 5) ---

  test "begin_destroy records the destroying intent, stays live, and rebuilds", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, a} = create(store, workload: "wl-ncd")
    assert SessionStore.counts(store, "wl-ncd") == %{live: 1, banked: 0}

    {:ok, updated} =
      SessionStore.transition(store, a.session_id, :begin_destroy, :session_destroying, %{reason: :destroyed}, %{})

    # destroying is non-terminal and still counts as live (the VM is being torn down).
    assert updated.state == :destroying
    assert SessionStore.counts(store, "wl-ncd") == %{live: 1, banked: 0}

    # The intent op is journaled (before any destroyed op).
    {:ok, ops} = SQLite.read_from(op_log, 0)
    assert Enum.map(ops, & &1.kind) == [:session_created, :session_destroying]

    # A fresh store rebuilds the destroying row from the projection alone.
    {:ok, store2} = SessionStore.start_link(op_log: op_log, name: nil)
    {:ok, sa} = SessionStore.get(store2, a.session_id)
    assert sa.state == :destroying

    # destroying -> destroy -> destroyed is the confirming edge.
    {:ok, done} =
      SessionStore.transition(store, a.session_id, :destroy, :session_destroyed, %{reason: :destroyed}, %{})

    assert done.state == :destroyed
    assert SessionStore.counts(store, "wl-ncd") == %{live: 0, banked: 0}
  end

  # -- async lifecycle writes (ADR embervm/014 decision 2) -------------------

  defp durable_session_created_count(op_log, session_id) do
    {:ok, ops} = SQLite.read_from(op_log, 0)
    ops |> Enum.filter(&(&1.session_id == session_id and &1.kind == :session_created)) |> length()
  end

  test "gate ON: create mints ETS row + token synchronously but defers the durable append", %{
    path: path
  } do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    {:ok, store} =
      SessionStore.start_link(
        op_log: op_log,
        name: nil,
        clock: sequential_clock(),
        id_fun: sequential_id_fun(),
        async_writer: writer,
        async_lifecycle_writes: true
      )

    {:ok, created} = create(store, vm_id: "vm-async")

    # The caller got its token and the ETS row is live immediately (routable)...
    assert is_binary(created.token)
    assert {:ok, %{state: :running}} = SessionStore.get(store, created.session_id)

    # ...but the durable session_created append is deferred: drain, THEN it appears.
    :ok = Embervm.AsyncWriter.drain(writer)
    assert durable_session_created_count(op_log, created.session_id) == 1
  end

  test "gate OFF: create is write-through (durable append synchronous, writer untouched)", %{path: path} do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    {:ok, store} =
      SessionStore.start_link(
        op_log: op_log,
        name: nil,
        clock: sequential_clock(),
        id_fun: sequential_id_fun(),
        async_writer: writer,
        async_lifecycle_writes: false
      )

    {:ok, created} = create(store, vm_id: "vm-sync")

    # No drain: the append is already durable, and the writer was never used.
    assert durable_session_created_count(op_log, created.session_id) == 1
    refute Embervm.AsyncWriter.pending?(writer, "vm-sync")
  end

  test "backfill_created re-drives a lost session_created append from the surviving ETS row", %{
    path: path
  } do
    # This covers the IDEMPOTENCY edge: a re-drive of an ALREADY-durable create must
    # not double the projected row. The complementary CREATES-WHEN-ABSENT edge (the
    # durable row genuinely missing, which is the case the reconciler actually repairs)
    # is the next test, which uses a drop-first op-log double to model the lost write
    # deterministically.
    {op_log, store} = start_pair(path)
    {:ok, created} = create(store, vm_id: "vm-bf")

    assert durable_session_created_count(op_log, created.session_id) == 1

    # A re-drive of an already-durable create is idempotent: no regression, and the
    # projection's INSERT OR IGNORE keeps the row single (a second durable op row is
    # expected in the log as an audit entry, but the projected session is unchanged).
    :ok = SessionStore.backfill_created(store, created.session_id)
    {:ok, [row]} = SQLite.load_sessions(op_log)
    assert row.session_id == created.session_id
    assert row.state == "running"

    # Unknown session: a clean error tuple, never a crash.
    assert {:error, {:not_found, "s-nonexistent"}} =
             SessionStore.backfill_created(store, "s-nonexistent")
  end

  # An op-log double that delegates to a real SQLite backend but SWALLOWS the FIRST
  # :session_created append (returns {:ok, 0} as if durable, without persisting),
  # modelling an async lifecycle write that was lost before it reached disk while the
  # caller still saw success and minted its ETS row. The "already dropped one?" state
  # lives in a test-owned public ETS table, so the drop is deterministic with ZERO
  # timing dependence (killing the AsyncWriter mid-flight to lose the write would be
  # flaky, which is exactly the failure mode this suite is trying to shed).
  defmodule DropFirstCreateOpLog do
    def append(op_log, %{kind: :session_created} = op) do
      case :ets.update_counter(:sessionstore_drop_first, :created, {2, 1}, {:created, 0}) do
        1 -> {:ok, 0}
        _ -> SQLite.append(op_log, op)
      end
    end

    def append(op_log, op), do: SQLite.append(op_log, op)

    def load_sessions(op_log), do: SQLite.load_sessions(op_log)
  end

  test "backfill_created CREATES the durable session_created when it is genuinely absent (lost async write)",
       %{path: path} do
    # Owned by this test process, so it is auto-deleted when the test ends (no on_exit:
    # the table is already gone by the time on_exit's separate process would run).
    :ets.new(:sessionstore_drop_first, [:set, :public, :named_table])

    {:ok, op_log} = SQLite.start_link(path: path, name: nil)

    {:ok, store} =
      SessionStore.start_link(
        op_log: op_log,
        op_log_mod: DropFirstCreateOpLog,
        name: nil,
        clock: sequential_clock(),
        id_fun: sequential_id_fun()
      )

    # create's durable :session_created is swallowed (the lost write), but the store
    # still minted the ETS row because append reported {:ok, _}.
    {:ok, created} = create(store, vm_id: "vm-lost")

    # The ETS row is live and routable...
    assert {:ok, %{state: :running}} = SessionStore.get(store, created.session_id)
    # ...while the durable projection holds NOTHING: the write never reached disk, so a
    # fresh store rebuilding from the projection alone would lose the session entirely.
    assert {:ok, []} = SQLite.load_sessions(op_log)
    assert durable_session_created_count(op_log, created.session_id) == 0

    # The reconciler's adopt-and-backfill repair re-drives the create from the ETS row.
    :ok = SessionStore.backfill_created(store, created.session_id)

    # Now the durable row EXISTS (0 -> 1) and matches the surviving ETS row: a rebuild
    # would reproduce the session rather than orphaning its live VM.
    assert durable_session_created_count(op_log, created.session_id) == 1
    {:ok, [row]} = SQLite.load_sessions(op_log)
    assert row.session_id == created.session_id
    assert row.state == "running"

    {:ok, store2} = SessionStore.start_link(op_log: op_log, name: nil)
    assert {:ok, %{state: :running}} = SessionStore.get(store2, created.session_id)
  end
end
