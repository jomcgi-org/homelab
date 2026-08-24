defmodule Embervm.OpLog.ProjectionConformance do
  @moduledoc """
  Shared projector-conformance scenarios for the op-log backends.

  The SQLite and Postgres adapters are meant to mirror each other clause for
  clause (several `project/3` clauses in both files say so in prose), and the
  2026-08-10 incident (#4626, tracked by #4627) was exactly such a mirror
  breaking silently: Postgres had no `:session_destroying` clause while SQLite
  did, nothing executed the Postgres clause set in CI, and the missing clause
  crashed every live-session destroy once the kind became reachable. The
  database-free parity test (`projection_parity_test.exs`) pins the projected
  KIND sets since #5096; it cannot pin what each clause DOES to its row.

  This module holds the scenario functions that do: every scenario is a plain
  function of `{backend, server}` that appends a scripted op sequence and
  asserts on the durable projection through the `Embervm.OpLog` behaviour
  callbacks only (append/load_*/list_usage). Two thin ExUnit case modules in
  the same file generate one test per scenario per backend FROM THE SAME LIST,
  so a backend cannot skip an assertion the other backend runs: the SQLite
  cases always execute (SQLite needs no service), and the Postgres cases
  execute wherever `EMBERVM_OPLOG_TEST_DSN` names a real Postgres (locally,
  or in CI once CI provides one; see `Embervm.OpLog.PostgresLiveTest` for the
  same gating).

  A scenario failure names the backend that regressed, which is the drift
  signal: if the two projectors ever disagree on any scripted sequence, at
  least one generated test goes red without any new test code.
  """

  alias Embervm.OpLog.Op

  import ExUnit.Assertions

  @day_ms 86_400_000

  def scenarios do
    [
      task_lifecycle_projects_exact_state: &__MODULE__.task_lifecycle/2,
      late_async_appends_never_regress_or_cross_attempts: &__MODULE__.task_monotonic_guards/2,
      redrive_resets_the_attempt_budget: &__MODULE__.task_redrive/2,
      audit_only_kinds_write_ops_without_projection_rows: &__MODULE__.audit_only_kinds/2,
      session_lifecycle_from_create_to_relit: &__MODULE__.session_lifecycle/2,
      session_destroy_intent_and_terminals: &__MODULE__.session_destroying_and_terminals/2,
      terminal_session_cannot_be_revived_by_a_late_relit: &__MODULE__.session_relit_guard/2,
      serving_lifecycle_including_endpoint_clearing: &__MODULE__.serving_lifecycle/2,
      stateful_lifecycle_keeps_volume_pair_validity: &__MODULE__.stateful_lifecycle/2,
      stateful_unpublished_is_audit_only: &__MODULE__.stateful_unpublished/2,
      group_lifecycle_including_atomic_member_bank: &__MODULE__.group_lifecycle/2,
      usage_accrual_matches_per_kind_semantics: &__MODULE__.usage_accrual/2,
      key_epochs_blessing_leases_and_checkpoints: &__MODULE__.custodian_records/2,
      duplicate_idempotency_key_rolls_back_the_whole_append: &__MODULE__.idempotency/2,
      unknown_kind_is_rejected_before_any_write: &__MODULE__.unknown_kind/2
    ]
  end

  # -- shared helpers -------------------------------------------------------

  defp append(server, backend, kind, ts, fields \\ []) do
    fields = Keyword.merge([tenant: "t1", payload: %{}], fields)
    op = struct!(Op, Map.merge(Map.new(fields), %{kind: kind, ts: ts}))
    backend.append(server, op)
  end

  defp one(backend, server, loader) do
    case apply(backend, loader, [server]) do
      {:ok, [row]} -> row
      {:ok, rows} -> flunk("#{loader} expected exactly one row, got #{length(rows)}")
    end
  end

  defp assert_eq(left, right, msg \\ "assert equal") do
    ExUnit.Assertions.assert(left == right, message: "#{msg}: #{inspect(left)} != #{inspect(right)}")
  end

  # -- task projection ------------------------------------------------------

  def task_lifecycle(backend, server) do
    assert_eq(
      append(server, backend, :submitted, 100,
        principal: "p1",
        workload: "wl-task",
        task_id: "task-1",
        payload: %{idempotency_key: nil, expires_at: nil}
      ) |> elem(0),
      :ok,
      "submitted accepted"
    )

    assert_eq(one(backend, server, :load_tasks).state, "queued", "submitted -> queued")

    {:ok, _} = append(server, backend, :assigned, 101, task_id: "task-1")
    assert_eq(one(backend, server, :load_tasks).state, "assigned", "assigned")

    {:ok, _} = append(server, backend, :started, 102, task_id: "task-1")
    assert_eq(one(backend, server, :load_tasks).state, "running", "started")

    {:ok, _} =
      append(server, backend, :succeeded, 103,
        task_id: "task-1",
        payload: %{
          status_code: 201,
          body: <<0, 1, 255>>,
          size_bytes: 3,
          truncated: false,
          expires_at: nil,
          headers: %{"content-type" => "application/octet-stream"}
        }
      )

    task = one(backend, server, :load_tasks)
    assert_eq(task.state, "succeeded", "succeeded")
    assert_eq(task.attempt, 0, "no phantom retry")

    assert {:ok, result} = backend.load_result(server, "task-1")

    assert_eq(
      result,
      %{
        task_id: "task-1",
        status_code: 201,
        body: <<0, 1, 255>>,
        size_bytes: 3,
        truncated: false,
        created_at: 103,
        expires_at: nil,
        headers: %{"content-type" => "application/octet-stream"}
      },
      "result row round-trips a non-UTF-8 body"
    )
  end

  def task_monotonic_guards(backend, server) do
    {:ok, _} =
      append(server, backend, :submitted, 100,
        principal: "p1",
        workload: "wl-task",
        task_id: "task-1"
      )

    # epoch-tagged assigned (ADR embervm/014 decision 2): applies while the row's
    # attempt is still below the tag.
    {:ok, _} = append(server, backend, :assigned, 101, task_id: "task-1", payload: %{epoch: 1})
    assert_eq(one(backend, server, :load_tasks).state, "assigned", "epoch-1 assigned applies")

    {:ok, _} = append(server, backend, :retried, 105, task_id: "task-1")
    task = one(backend, server, :load_tasks)
    assert_eq(task.state, "queued", "retried requeues")
    assert_eq(task.attempt, 1, "retried bumps attempt")

    # The stale attempt-1 assigned lands after the retry: rank guard passes (the
    # row is queued again) but the attempt guard must drop it.
    {:ok, _} = append(server, backend, :assigned, 106, task_id: "task-1", payload: %{epoch: 1})
    task = one(backend, server, :load_tasks)
    assert_eq(task.state, "queued", "stale attempt-1 assigned dropped")
    assert_eq(task.attempt, 1, "attempt unchanged by stale append")

    {:ok, _} = append(server, backend, :assigned, 107, task_id: "task-1", payload: %{epoch: 2})
    assert_eq(one(backend, server, :load_tasks).state, "assigned", "fresh epoch applies")

    # A late :started after a terminal transition must not regress the row.
    {:ok, _} =
      append(server, backend, :succeeded, 120,
        task_id: "task-1",
        payload: %{status_code: 200, body: "", size_bytes: 0, truncated: false}
      )

    {:ok, _} = append(server, backend, :started, 130, task_id: "task-1")
    task = one(backend, server, :load_tasks)
    assert_eq(task.state, "succeeded", "late started cannot regress a succeeded row")

    # dead_lettered moves a live row to its terminal state.
    {:ok, _} = append(server, backend, :dead_lettered, 140, task_id: "task-1")
    assert_eq(one(backend, server, :load_tasks).state, "dead_lettered", "dead_lettered")
  end

  def task_redrive(backend, server) do
    {:ok, _} =
      append(server, backend, :submitted, 100,
        principal: "p1",
        workload: "wl-task",
        task_id: "task-1"
      )

    {:ok, _} = append(server, backend, :retried, 101, task_id: "task-1")
    {:ok, _} = append(server, backend, :retried, 102, task_id: "task-1")
    assert_eq(one(backend, server, :load_tasks).attempt, 2, "two retries -> attempt 2")

    # Redrive is the manual-intervention audit: full fresh retry budget.
    {:ok, _} = append(server, backend, :redrive, 110, task_id: "task-1")
    task = one(backend, server, :load_tasks)
    assert_eq(task.state, "queued", "redrive requeues")
    assert_eq(task.attempt, 0, "redrive resets attempt to 0")
  end

  # -- audit-only kinds ------------------------------------------------------

  def audit_only_kinds(backend, server) do
    audit_kinds = [
      :denied,
      :base_built,
      :primed,
      :vm_destroyed,
      :quota_enforced,
      :drain,
      :node_drain_started,
      :node_drain_finished,
      :artifact_exported,
      :artifact_restored,
      :artifact_evicted_remote
    ]

    seqs =
      for {kind, i} <- Enum.with_index(audit_kinds, 1) do
        {:ok, seq} = append(server, backend, kind, i * 10, workload: "wl-audit")
        seq
      end

    assert {:ok, ops} = backend.read_from(server, 0)
    assert_eq(Enum.map(ops, & &1.kind), audit_kinds, "every audit-only op journaled in order")

    # None of them may fabricate a tasks row.
    assert_eq(backend.load_tasks(server), {:ok, []}, "audit-only kinds project no task rows")
    assert_eq(length(seqs), length(audit_kinds), "one seq per append")
  end

  # -- sessions ---------------------------------------------------------------

  def session_lifecycle(backend, server) do
    {:ok, _} =
      append(server, backend, :session_created, 100,
        principal: "p1",
        workload: "wl-session",
        session_id: "s-1",
        payload: %{
          state: "running",
          node_id: "node-4",
          volume_node_id: nil,
          base_snapshot_ref: "base:sha256:abc",
          base_digest: "sha256:abc",
          token_sha256: "tok",
          expires_at: 100 + 3_600_000
        }
      )

    s = one(backend, server, :load_sessions)
    assert_eq(s.state, "running", "created -> running")
    assert_eq(s.generation, 0, "birth generation 0")
    assert_eq(s.lineage_id, "s-1", "lineage_id defaults to session_id")

    {:ok, _} =
      append(server, backend, :session_invoked, 150,
        principal: "p1",
        workload: "wl-session",
        session_id: "s-1"
      )

    assert_eq(one(backend, server, :load_sessions).last_invoke_at, 150, "invoked stamps last_invoke_at")

    {:ok, _} =
      append(server, backend, :session_banked, 200,
        session_id: "s-1",
        payload: %{snapshot_ref: "sessions/s-1@3", size_bytes: 42, generation: 3}
      )

    s = one(backend, server, :load_sessions)
    assert_eq(s.state, "banked", "banked")
    assert_eq(s.snapshot_ref, "sessions/s-1@3", "snapshot ref stamped")
    assert_eq(s.snapshot_size_bytes, 42, "snapshot size stamped")
    assert_eq(s.generation, 3, "generation bumped")

    {:ok, _} =
      append(server, backend, :session_parking, 250,
        session_id: "s-1",
        payload: %{volume_node_id: "node-9"}
      )

    s = one(backend, server, :load_sessions)
    assert_eq(s.state, "parking", "parking")
    assert_eq(s.volume_node_id, "node-9", "volume node recorded")

    {:ok, _} =
      append(server, backend, :session_parked, 300,
        session_id: "s-1",
        payload: %{volume_node_id: "node-9"}
      )

    s = one(backend, server, :load_sessions)
    assert_eq(s.state, "parked", "parked")
    assert_eq(s.node_id, nil, "parked clears node_id (the VM is gone)")

    {:ok, _} = append(server, backend, :session_relit, 350, session_id: "s-1")
    s = one(backend, server, :load_sessions)
    assert_eq(s.state, "running", "relit back to running")

    {:ok, _} = append(server, backend, :session_rejoined, 400, session_id: "s-1")
    assert_eq(one(backend, server, :load_sessions).state, "running", "rejoined rides the relit path")
  end

  def session_destroying_and_terminals(backend, server) do
    for {suffix, kind} <- [
          {"a", :session_expired},
          {"b", :session_evicted},
          {"c", :session_destroyed},
          {"d", :session_failed}
        ] do
      id = "s-" <> suffix

      {:ok, _} =
        append(server, backend, :session_created, 100,
          principal: "p1",
          workload: "wl-session",
          session_id: id,
          payload: %{node_id: "node-4"}
        )

      # Destroy INTENT first (ADR embervm/014 decision 5): non-terminal, no reason.
      {:ok, _} = append(server, backend, :session_destroying, 150, session_id: id)
      s = session_row(backend, server, id)
      assert_eq(s.state, "destroying", "#{id} destroy intent")
      assert_eq(s.terminal_reason, nil, "#{id} intent carries no terminal_reason yet")

      # Then the confirmed terminal edge with a machine-readable reason.
      {:ok, _} =
        append(server, backend, kind, 200,
          session_id: id,
          payload: %{reason: "test-driven"}
        )

      s = session_row(backend, server, id)
      assert_eq(s.state, kind |> Atom.to_string() |> String.replace("session_", ""),
                "#{id} terminal state")

      assert_eq(s.terminal_reason, "test-driven", "#{id} terminal reason recorded")
    end

    # A terminal default: the reason falls back to the state name when omitted.
    {:ok, _} =
      append(server, backend, :session_created, 300,
        principal: "p1",
        workload: "wl-session",
        session_id: "s-e",
        payload: %{}
      )

    {:ok, _} = append(server, backend, :session_expired, 400, session_id: "s-e")
    assert_eq(session_row(backend, server, "s-e").terminal_reason, "expired", "default reason")
  end

  defp session_row(backend, server, session_id) do
    {:ok, rows} = backend.load_sessions(server)
    Enum.find(rows, &(&1.session_id == session_id))
  end

  def session_relit_guard(backend, server) do
    {:ok, _} =
      append(server, backend, :session_created, 100,
        principal: "p1",
        workload: "wl-session",
        session_id: "s-1",
        payload: %{}
      )

    {:ok, _} =
      append(server, backend, :session_destroyed, 200,
        session_id: "s-1",
        payload: %{reason: "gone"}
      )

    # A deferred async relit landing AFTER the terminal edge must not resurrect.
    {:ok, _} = append(server, backend, :session_relit, 300, session_id: "s-1")
    s = one(backend, server, :load_sessions)
    assert_eq(s.state, "destroyed", "destroyed session stays destroyed")
    assert_eq(s.updated_at, 200, "the guard matches no row, so updated_at is untouched")
  end

  # -- serving instances --------------------------------------------------------

  def serving_lifecycle(backend, server) do
    {:ok, _} =
      append(server, backend, :serving_started, 100,
        principal: "p1",
        workload: "wl-serving",
        serving_instance_id: "sv-1",
        payload: %{
          state: "starting",
          node_id: "node-4",
          vm_id: "vm-1",
          ip: "10.0.0.2",
          port: 8080,
          base_snapshot_ref: "base:sha256:abc",
          base_digest: "sha256:abc"
        }
      )

    sv = one(backend, server, :load_serving_instances)
    assert_eq(sv.state, "starting", "started -> starting")
    assert_eq(sv.ip, "10.0.0.2", "start endpoint reported")
    assert_eq(sv.created_at, 100, "created_at stamped")

    {:ok, _} =
      append(server, backend, :serving_published, 150,
        serving_instance_id: "sv-1",
        payload: %{ip: "10.0.0.3", port: 8081}
      )

    sv = one(backend, server, :load_serving_instances)
    assert_eq(sv.state, "published", "published")
    assert_eq(sv.ip, "10.0.0.3", "published endpoint replaces start endpoint")

    {:ok, _} = append(server, backend, :serving_unpublished, 200, serving_instance_id: "sv-1")
    assert_eq(one(backend, server, :load_serving_instances).state, "draining", "unpublished -> draining")

    {:ok, _} =
      append(server, backend, :serving_banked, 250,
        serving_instance_id: "sv-1",
        payload: %{snapshot_ref: "serving/sv-1@2", size_bytes: 7, generation: 2}
      )

    sv = one(backend, server, :load_serving_instances)
    assert_eq(sv.state, "banked", "banked")
    assert_eq(sv.snapshot_ref, "serving/sv-1@2", "bundle stamped")
    assert_eq(sv.generation, 2, "generation bumped")
    assert_eq(sv.ip, nil, "bank clears ip (a relight gets a fresh allocation)")
    assert_eq(sv.port, nil, "bank clears port")

    {:ok, _} =
      append(server, backend, :serving_relit, 300,
        serving_instance_id: "sv-1",
        payload: %{node_id: "node-5", vm_id: "vm-2"}
      )

    sv = one(backend, server, :load_serving_instances)
    assert_eq(sv.state, "starting", "relit -> starting until republished")
    assert_eq(sv.node_id, "node-5", "relight reports fresh node")
    assert_eq(sv.vm_id, "vm-2", "relight reports fresh vm")

    # destroy intent marker, then the terminal edges with reasons.
    {:ok, _} = append(server, backend, :serving_destroying, 350, serving_instance_id: "sv-1")
    sv = one(backend, server, :load_serving_instances)
    assert_eq(sv.state, "destroying", "destroy intent")
    assert_eq(sv.terminal_reason, nil, "intent carries no reason")

    {:ok, _} = append(server, backend, :serving_evicted, 400, serving_instance_id: "sv-1")
    sv = one(backend, server, :load_serving_instances)
    assert_eq(sv.state, "evicted", "evicted terminal")
    assert_eq(sv.terminal_reason, "evicted", "reason defaults to the state name")

    # A second lifecycle for failed, to cover the remaining terminal kind.
    {:ok, _} =
      append(server, backend, :serving_started, 500,
        principal: "p1",
        workload: "wl-serving",
        serving_instance_id: "sv-2",
        payload: %{}
      )

    {:ok, _} =
      append(server, backend, :serving_failed, 600,
        serving_instance_id: "sv-2",
        payload: %{reason: "crash"}
      )

    {:ok, serving_rows} = backend.load_serving_instances(server)
    sv2 = Enum.find(serving_rows, &(&1.instance_id == "sv-2"))

    assert_eq(sv2.state, "failed", "failed terminal")
    assert_eq(sv2.terminal_reason, "crash", "failed reason")
  end

  # -- stateful + volumes ---------------------------------------------------------

  def stateful_lifecycle(backend, server) do
    {:ok, _} =
      append(server, backend, :volume_created, 90,
        workload: "wl-stateful",
        payload: %{node_id: "node-4", generation: 1, size_bytes: 100, allocated_bytes: 40}
      )

    vol = one(backend, server, :load_volumes)
    assert_eq(vol.generation, 1, "volume born at generation 1")

    {:ok, _} =
      append(server, backend, :generation_blessed, 95,
        workload: "wl-stateful",
        payload: %{generation: 2}
      )

    assert_eq(one(backend, server, :load_volume_blessing).blessed_generation, 2, "blessed before attach")

    {:ok, _} =
      append(server, backend, :stateful_started, 100,
        tenant: "t1",
        principal: "p1",
        workload: "wl-stateful",
        stateful_instance_id: "st-1",
        payload: %{node_id: "node-4", vm_id: "vm-st", generation: 2}
      )

    st = one(backend, server, :load_stateful_instances)
    assert_eq(st.state, "starting", "started -> starting")
    assert_eq(st.generation, 2, "instance boots at the blessed generation")
    assert_eq(one(backend, server, :load_volumes).generation, 2, "attach bumps the volume ledger")

    {:ok, _} =
      append(server, backend, :stateful_published, 150,
        stateful_instance_id: "st-1",
        payload: %{ip: "10.1.0.2", port: 7000}
      )

    st = one(backend, server, :load_stateful_instances)
    assert_eq(st.state, "serving", "published -> serving")
    assert_eq(st.ip, "10.1.0.2", "L4 endpoint recorded")

    {:ok, _} =
      append(server, backend, :stateful_banked, 200,
        stateful_instance_id: "st-1",
        payload: %{snapshot_ref: "stateful/st-1@2", generation: 2, size_bytes: 55}
      )

    st = one(backend, server, :load_stateful_instances)
    assert_eq(st.state, "banked", "banked")
    assert_eq(st.snapshot_ref, "stateful/st-1@2", "bundle ref stamped")
    assert_eq(st.snapshot_generation, 2, "pair key: stamped generation")
    assert_eq(st.snapshot_size_bytes, 55, "bundle size stamped")
    assert_eq(st.ip, nil, "bank clears the endpoint")
    assert_eq(st.port, nil, "bank clears the port")

    {:ok, _} =
      append(server, backend, :stateful_relit, 250,
        workload: "wl-stateful",
        stateful_instance_id: "st-1",
        payload: %{node_id: "node-6", vm_id: "vm-st2", generation: 3}
      )

    st = one(backend, server, :load_stateful_instances)
    assert_eq(st.state, "starting", "relit -> starting")
    assert_eq(st.generation, 3, "relight carries the post-bump generation")
    assert_eq(st.snapshot_ref, nil, "relight spends the bundle (ref cleared)")
    assert_eq(st.snapshot_generation, nil, "relight spends the bundle (pair key cleared)")
    assert_eq(one(backend, server, :load_volumes).generation, 3, "volume ledger follows the relight")

    # Cold boot creates a NEW instance lifecycle over the SAME volume.
    {:ok, _} =
      append(server, backend, :stateful_cold_booted, 300,
        tenant: "t1",
        principal: "p1",
        workload: "wl-stateful",
        stateful_instance_id: "st-2",
        payload: %{node_id: "node-6", vm_id: "vm-st3", generation: 4, reason: "explicit"}
      )

    {:ok, rows} = backend.load_stateful_instances(server)
    prior = Enum.find(rows, &(&1.instance_id == "st-1"))
    assert_eq(prior.state, "starting", "cold boot leaves the prior lifecycle row intact")

    cold = Enum.find(rows, &(&1.instance_id == "st-2"))
    assert_eq(cold.state, "starting", "cold boot inserts a fresh starting row")
    assert_eq(one(backend, server, :load_volumes).generation, 4, "cold boot bumps the ledger too")

    {:ok, _} = append(server, backend, :stateful_destroying, 350, stateful_instance_id: "st-2")
    assert_eq(one_row_for(backend, server, "st-2").state, "destroying", "destroy intent")

    {:ok, _} =
      append(server, backend, :stateful_destroyed, 400,
        stateful_instance_id: "st-2",
        payload: %{reason: "operator"}
      )

    st = one_row_for(backend, server, "st-2")
    assert_eq(st.state, "destroyed", "destroyed terminal")
    assert_eq(st.terminal_reason, "operator", "terminal reason")
  end

  defp one_row_for(backend, server, instance_id) do
    {:ok, rows} = backend.load_stateful_instances(server)
    Enum.find(rows, &(&1.instance_id == instance_id))
  end

  def stateful_unpublished(backend, server) do
    {:ok, _} =
      append(server, backend, :stateful_started, 100,
        tenant: "t1",
        principal: "p1",
        workload: "wl-stateful",
        stateful_instance_id: "st-1",
        payload: %{node_id: "node-4", vm_id: "vm-st", generation: 1}
      )

    # unpublished is AUDIT-ONLY for the state machine: it stamps updated_at but
    # must leave the state alone so boot-rebuild keeps its "there is a live VM,
    # republish it" verdict.
    {:ok, _} = append(server, backend, :stateful_unpublished, 150, stateful_instance_id: "st-1")
    st = one(backend, server, :load_stateful_instances)
    assert_eq(st.state, "starting", "unpublished leaves the FSM state untouched")
    assert_eq(st.updated_at, 150, "unpublished still stamps updated_at")
  end

  # -- composite groups -----------------------------------------------------------

  def group_lifecycle(backend, server) do
    {:ok, _} =
      append(server, backend, :group_created, 100,
        tenant: "t1",
        principal: "p1",
        workload: "wl-group",
        group_instance_id: "g-1",
        payload: %{node_id: "node-4", entry_member: "api", entry_port: 80}
      )

    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "starting", "created -> starting")
    assert_eq(g.entry_member, "api", "entry identity from the CR")
    assert_eq(g.subnet_cidr, nil, "no subnet yet")

    {:ok, _} =
      append(server, backend, :group_net_created, 110,
        group_instance_id: "g-1",
        payload: %{subnet_cidr: "10.20.0.0/29"}
      )

    assert_eq(one(backend, server, :load_group_instances).subnet_cidr, "10.20.0.0/29", "subnet recorded")

    {:ok, _} =
      append(server, backend, :group_member_started, 120,
        group_instance_id: "g-1",
        payload: %{member_name: "api", member_index: 0, vm_id: "vm-api", ip: "10.20.0.2"}
      )

    {:ok, [m]} = backend.load_group_members(server)
    assert_eq(m.member_name, "api", "member row inserted")
    assert_eq(m.vm_id, "vm-api", "member vm reported")
    assert_eq(m.healthy, false, "member starts unhealthy")
    assert_eq(m.snapshot_ref, nil, "no bundle slice yet")

    {:ok, _} = append(server, backend, :group_running, 130, group_instance_id: "g-1")
    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "running", "whole-group readiness edge")
    assert_eq(g.last_active_at, 130, "running advances last_active_at")

    {:ok, [m]} = backend.load_group_members(server)
    assert_eq(m.healthy, true, "running flips every member healthy")

    {:ok, _} =
      append(server, backend, :group_published, 140,
        group_instance_id: "g-1",
        payload: %{listen_port: 8080}
      )

    assert_eq(one(backend, server, :load_group_instances).listen_port, 8080, "entry listener recorded")

    # COALESCE keeps an already-recorded listener when the payload omits one.
    {:ok, _} = append(server, backend, :group_published, 145, group_instance_id: "g-1")
    assert_eq(one(backend, server, :load_group_instances).listen_port, 8080, "COALESCE preserves listen_port")

    {:ok, _} =
      append(server, backend, :group_degraded, 150,
        group_instance_id: "g-1",
        payload: %{member_name: "api"}
      )

    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "degraded", "degraded is LIVE, not terminal")

    {:ok, [m]} = backend.load_group_members(server)
    assert_eq(m.healthy, false, "degraded flips only the named member off")

    {:ok, _} = append(server, backend, :group_unpublished, 160, group_instance_id: "g-1")
    assert_eq(one(backend, server, :load_group_instances).state, "banking", "unpublished -> banking")

    {:ok, _} =
      append(server, backend, :group_banked, 170,
        group_instance_id: "g-1",
        payload: %{set_id: "set-7", members: [%{name: "api", snapshot_ref: "grp/api@1"}]}
      )

    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "banked", "whole-set bank")
    assert_eq(g.set_id, "set-7", "bundle-set handle stamped")

    {:ok, [m]} = backend.load_group_members(server)
    assert_eq(m.snapshot_ref, "grp/api@1", "member bundle slice stamped atomically")
    assert_eq(m.vm_id, nil, "member vms are gone after bank")
    assert_eq(m.ip, nil, "member ips cleared after bank")
    assert_eq(m.state, "banked", "member state follows the set")

    {:ok, _} = append(server, backend, :group_relit, 180, group_instance_id: "g-1")
    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "starting", "warm wake -> starting until group_running")
    assert_eq(g.last_active_at, 180, "relit advances last_active_at")

    {:ok, _} = append(server, backend, :group_set_evicted, 190, group_instance_id: "g-1")
    g = one(backend, server, :load_group_instances)
    assert_eq(g.set_id, nil, "set eviction discards the warmth handle")

    {:ok, _} = append(server, backend, :group_fresh_booted, 200, group_instance_id: "g-1")
    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "starting", "fresh boot restarts the lifecycle")

    # Member re-report after the fresh boot: upsert clears the spent snapshot.
    {:ok, _} =
      append(server, backend, :group_member_started, 210,
        group_instance_id: "g-1",
        payload: %{member_name: "api", member_index: 0, vm_id: "vm-api2", ip: "10.20.0.3"}
      )

    {:ok, [m]} = backend.load_group_members(server)
    assert_eq(m.vm_id, "vm-api2", "member facts refresh on reboot")
    assert_eq(m.snapshot_ref, nil, "fresh member boot spends the old bundle slice")
    assert_eq(m.healthy, false, "health resets on member reboot")

    {:ok, _} = append(server, backend, :group_destroying, 220, group_instance_id: "g-1")
    assert_eq(one(backend, server, :load_group_instances).state, "destroying", "destroy intent")

    {:ok, _} =
      append(server, backend, :group_destroyed, 230,
        group_instance_id: "g-1",
        payload: %{reason: "expired"}
      )

    g = one(backend, server, :load_group_instances)
    assert_eq(g.state, "destroyed", "destroyed terminal")
    assert_eq(g.terminal_reason, "expired", "expired rides group_destroyed (no dedicated kind)")

    # net_deleted clears the subnet fact so no rebuild shows a live subnet.
    {:ok, _} =
      append(server, backend, :group_created, 300,
        tenant: "t1",
        principal: "p1",
        workload: "wl-group",
        group_instance_id: "g-2",
        payload: %{entry_member: "api", entry_port: 80}
      )

    {:ok, _} =
      append(server, backend, :group_net_created, 310,
        group_instance_id: "g-2",
        payload: %{subnet_cidr: "10.30.0.0/29"}
      )

    {:ok, _} = append(server, backend, :group_net_deleted, 320, group_instance_id: "g-2")
    g2 = one_row_g(backend, server, "g-2")
    assert_eq(g2.subnet_cidr, nil, "net_deleted clears subnet_cidr")

    {:ok, _} =
      append(server, backend, :group_failed, 400,
        group_instance_id: "g-2",
        payload: %{reason: "provision_failed"}
      )

    g2 = one_row_g(backend, server, "g-2")
    assert_eq(g2.state, "failed", "failed terminal")
    assert_eq(g2.terminal_reason, "provision_failed", "failed reason")
  end

  defp one_row_g(backend, server, instance_id) do
    {:ok, rows} = backend.load_group_instances(server)
    Enum.find(rows, &(&1.instance_id == instance_id))
  end

  # -- usage / metering --------------------------------------------------------------

  def usage_accrual(backend, server) do
    day = 5 * @day_ms

    {:ok, _} =
      append(server, backend, :submitted, day,
        principal: "p1",
        workload: "wl-usage",
        task_id: "u-1"
      )

    {:ok, _} =
      append(server, backend, :succeeded, day + 1,
        principal: "p1",
        workload: "wl-usage",
        task_id: "u-1",
        payload: %{
          status_code: 200,
          body: "",
          size_bytes: 0,
          truncated: false,
          usage: %{vcpu_seconds: 1.5, gb_seconds: 2.0}
        }
      )

    {:ok, _} =
      append(server, backend, :submitted, day + 2,
        principal: "p1",
        workload: "wl-usage",
        task_id: "u-2"
      )

    {:ok, _} =
      append(server, backend, :failed, day + 3,
        principal: "p1",
        workload: "wl-usage",
        task_id: "u-2",
        payload: %{
          state: "failed_retryable",
          usage: %{vcpu_seconds: 0.25, gb_seconds: 0.5}
        }
      )

    {:ok, page} = backend.list_usage(server, since_day: 0)
    assert_eq(page.total, 1, "one (principal, day) bucket")
    {:ok, %{items: [item]}} = backend.list_usage(server, since_day: 0)

    assert_eq(item.principal, "p1", "bucket principal")
    assert_eq(item.day, 5, "epoch-day bucket")
    assert_eq(item.vcpu_seconds, 1.75, "vcpu accumulates across succeeded AND failed")
    assert_eq(item.gb_seconds, 2.5, "gb accumulates across succeeded AND failed")
    assert_eq(item.task_count, 2, "every charged op bumps task_count")

    # session_invoked charges the SAME (principal, day) bucket (D12.1).
    {:ok, _} =
      append(server, backend, :session_created, day + 4,
        principal: "p1",
        workload: "wl-session",
        session_id: "su-1",
        payload: %{}
      )

    # session_invoked charges the SAME (principal, day) bucket (D12.1). Like
    # every project_usage path it only accrues when the op CARRIES usage, so
    # script one the way SessionManager appends it.
    {:ok, _} =
      append(server, backend, :session_invoked, day + 5,
        principal: "p1",
        workload: "wl-session",
        session_id: "su-1",
        payload: %{usage: %{vcpu_seconds: 0.25, gb_seconds: 0.75}}
      )

    {:ok, %{items: [item]}} = backend.list_usage(server, since_day: 0)
    assert_eq(item.task_count, 3, "session_invoked joins the task bucket")
    assert_eq(item.vcpu_seconds, 2.0, "session_invoked vcpu accumulates into the same bucket")
    assert_eq(item.gb_seconds, 3.25, "session_invoked gb accumulates into the same bucket")

    # serving_stats charges request_count ONLY (never conflated with task_count).
    {:ok, _} =
      append(server, backend, :serving_stats, day + 6,
        principal: "p2",
        workload: "wl-serving",
        payload: %{rq_delta: 12, window_ms: 1000}
      )

    {:ok, _} =
      append(server, backend, :serving_stats, day + 7,
        principal: "p2",
        workload: "wl-serving",
        payload: %{rq_delta: 3, window_ms: 1000}
      )

    {:ok, %{items: items}} = backend.list_usage(server, since_day: 0, principal: "p2")
    assert_eq(items |> hd() |> Map.get(:request_count), 15, "rq_delta accumulates into request_count")
    assert_eq(items |> hd() |> Map.get(:task_count), 0, "serving requests never touch task_count")

    # stateful_stats: cx_delta into request_count, same discipline.
    {:ok, _} =
      append(server, backend, :stateful_stats, day + 8,
        principal: "p3",
        workload: "wl-stateful",
        payload: %{cx_delta: 4, window_ms: 1000}
      )

    {:ok, %{items: items}} = backend.list_usage(server, since_day: 0, principal: "p3")
    assert_eq(items |> hd() |> Map.get(:request_count), 4, "cx_delta accumulates like rq_delta")

    # group_stats bills PER MEMBER: the projection multiplies by member_count.
    {:ok, _} =
      append(server, backend, :group_stats, day + 9,
        principal: "p4",
        workload: "wl-group",
        payload: %{
          member_count: 3,
          usage: %{vcpu_seconds: 2.0, gb_seconds: 1.0},
          window_ms: 1000
        }
      )

    {:ok, %{items: items}} = backend.list_usage(server, since_day: 0, principal: "p4")
    item = items |> hd()
    assert_eq(item.vcpu_seconds, 6.0, "3 members x 2.0 vcpu seconds")
    assert_eq(item.gb_seconds, 3.0, "3 members x 1.0 gb seconds")

    # A principal-less stats op is a no-op, not a crash.
    {:ok, _} =
      append(server, backend, :serving_stats, day + 10,
        workload: "wl-serving",
        payload: %{rq_delta: 99}
      )

    {:ok, %{total: total}} = backend.list_usage(server, since_day: 0)
    assert_eq(total, 4, "nil-principal stats op accrued nothing")
  end

  # -- key custodian, leases, checkpoints ---------------------------------------------

  def custodian_records(backend, server) do
    {:ok, _} =
      append(server, backend, :key_epoch_set, 100,
        workload: nil,
        payload: %{principal: "p1", epoch: 3}
      )

    {:ok, [ke]} = backend.load_key_epochs(server)
    assert_eq(ke.current_epoch, 3, "epoch recorded")
    assert_eq(ke.min_epoch, 0, "new row floors at 0")

    {:ok, _} =
      append(server, backend, :key_min_epoch_raised, 110,
        payload: %{principal: "p1", min_epoch: 2}
      )

    {:ok, [ke]} = backend.load_key_epochs(server)
    assert_eq(ke.min_epoch, 2, "floor raised")

    {:ok, _} =
      append(server, backend, :key_epoch_set, 120,
        payload: %{principal: "p1", epoch: 5}
      )

    {:ok, [ke]} = backend.load_key_epochs(server)
    assert_eq(ke.current_epoch, 5, "later epoch updates in place")
    assert_eq(ke.min_epoch, 2, "floor survives an epoch change")

    {:ok, _} =
      append(server, backend, :blessing_lease_granted, 130,
        workload: "wl-stateful",
        payload: %{node_id: "node-4", next_generation: 7, lease_end: 999}
      )

    {:ok, [lease]} = backend.load_blessing_leases(server)
    assert_eq(lease.next_generation, 7, "lease records next_generation")

    {:ok, _} =
      append(server, backend, :blessing_lease_granted, 140,
        workload: "wl-stateful",
        payload: %{node_id: "node-4", next_generation: 9, lease_end: 1099}
      )

    {:ok, [lease]} = backend.load_blessing_leases(server)
    assert_eq(lease.next_generation, 9, "same (workload, node) lease upserts, never duplicates")

    {:ok, _} =
      append(server, backend, :checkpoint_dispatched, 150,
        workload: "wl-stateful",
        payload: %{vm_id: "vm-1", generation: 4}
      )

    {:ok, [cp]} = backend.load_checkpoint_dispatches(server)
    assert_eq(cp.vm_id, "vm-1", "in-flight checkpoint recorded")
    assert_eq(cp.generation, 4, "checkpoint pair generation recorded")

    {:ok, _} =
      append(server, backend, :checkpoint_dispatched, 160,
        workload: "wl-stateful",
        payload: %{vm_id: "vm-1", generation: 5}
      )

    {:ok, [%{generation: gen}]} = backend.load_checkpoint_dispatches(server)
    assert_eq(gen, 5, "a later checkpoint replaces the stale record")

    {:ok, _} =
      append(server, backend, :checkpoint_resolved, 170, workload: "wl-stateful")

    assert_eq(backend.load_checkpoint_dispatches(server), {:ok, []}, "resolve drops the record")
  end

  # -- transactionality ---------------------------------------------------------------

  def idempotency(backend, server) do
    {:ok, _} =
      append(server, backend, :submitted, 100,
        principal: "p1",
        workload: "wl-idem",
        task_id: "first",
        payload: %{idempotency_key: "key-1"}
      )

    # Same (workload, idempotency_key) must reject the SECOND append AND roll
    # back its ops-journal row inside the same transaction.
    assert_eq(
      append(server, backend, :submitted, 200,
        principal: "p1",
        workload: "wl-idem",
        task_id: "second",
        payload: %{idempotency_key: "key-1"}
      ),
      {:error, {:duplicate_idempotency_key, "first"}},
      "duplicate rejected naming the winner"
    )

    assert {:ok, ops} = backend.read_from(server, 0)
    assert_eq(Enum.map(ops, & &1.task_id), ["first"], "the rejected append left NO journal row")

    {:ok, tasks} = backend.load_tasks(server)
    assert_eq(Enum.map(tasks, & &1.task_id), ["first"], "and no tasks-row shadow")

    # A different workload never collides (the unique index is scoped).
    {:ok, _} =
      append(server, backend, :submitted, 300,
        principal: "p1",
        workload: "wl-other",
        task_id: "third",
        payload: %{idempotency_key: "key-1"}
      )

    assert_eq(length(elem(backend.load_tasks(server), 1)), 2, "cross-workload key is fine")
  end

  def unknown_kind(backend, server) do
    op = struct!(Op, %{kind: :definitely_not_a_kind, tenant: "t1", ts: 1})

    assert_eq(
      backend.append(server, op),
      {:error, {:unknown_kind, :definitely_not_a_kind}},
      "typo'd kind fails loudly"
    )

    assert_eq(backend.read_from(server, 0), {:ok, []}, "nothing journaled")
  end
end

defmodule Embervm.OpLog.ProjectionConformanceSQLiteTest do
  @moduledoc """
  Runs EVERY `Embervm.OpLog.ProjectionConformance` scenario against the SQLite
  backend over a private temp-file database per test. Always executes (no
  service needed); this side doubles as the executable specification the
  Postgres module below replays verbatim.
  """

  use ExUnit.Case, async: true

  alias Embervm.OpLog.SQLite
  alias Embervm.OpLog.ProjectionConformance

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_conformance_sqlite_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    {:ok, server} = SQLite.start_link(path: path, name: nil)
    on_exit(fn -> Embervm.TestProcess.stop_safely(server) end)
    on_exit(fn -> File.rm_rf!(path) end)
    %{server: server}
  end

  for {name, scenario} <- ProjectionConformance.scenarios() do
    test "#{name}", %{server: server} do
      unquote(scenario).(SQLite, server)
    end
  end
end

defmodule Embervm.OpLog.ProjectionConformancePostgresTest do
  @moduledoc """
  Runs the IDENTICAL `Embervm.OpLog.ProjectionConformance` scenario list against
  the Postgres backend, one temporary schema per test (the isolation idiom of
  `Embervm.OpLog.PostgresLiveTest`). Skips unless `EMBERVM_OPLOG_TEST_DSN`
  names a Postgres: CI has no control-plane Postgres service yet (that wiring
  is the open half of #4627), so run these locally against any disposable
  Postgres, e.g.

      EMBERVM_OPLOG_TEST_DSN=postgres://postgres@127.0.0.1:5432/embervm_test mix test test/embervm/op_log/projection_conformance_test.exs
  """

  use ExUnit.Case, async: true

  alias Embervm.OpLog.Postgres
  alias Embervm.OpLog.ProjectionConformance

  @moduletag :live_postgres
  @dsn System.get_env("EMBERVM_OPLOG_TEST_DSN")

  if is_nil(@dsn) or @dsn == "" do
    @moduletag skip: "EMBERVM_OPLOG_TEST_DSN is unset"
  end

  setup do
    schema = "conformance_#{System.unique_integer([:positive, :monotonic])}"
    opts = dsn_opts(@dsn)
    {:ok, setup_conn} = Postgrex.start_link(Keyword.put(opts, :name, nil))
    {:ok, _} = Postgrex.query(setup_conn, ~s(CREATE SCHEMA "#{schema}"), [])
    :ok = GenServer.stop(setup_conn)

    adapter_opts =
      opts
      |> Keyword.put(:name, nil)
      |> Keyword.put(:parameters, [search_path: schema])

    {:ok, server} = Postgres.start_link(dsn: adapter_opts, name: nil)

    on_exit(fn ->
      Embervm.TestProcess.stop_safely(server)
      {:ok, cleanup_conn} = Postgrex.start_link(Keyword.put(opts, :name, nil))
      {:ok, _} = Postgrex.query(cleanup_conn, ~s(DROP SCHEMA "#{schema}" CASCADE), [])
      Embervm.TestProcess.stop_safely(cleanup_conn)
    end)

    %{server: server}
  end

  for {name, scenario} <- ProjectionConformance.scenarios() do
    test "#{name}", %{server: server} do
      unquote(scenario).(Postgres, server)
    end
  end

  defp dsn_opts(dsn) do
    uri = URI.parse(dsn)
    [user, pass] = String.split(uri.userinfo || ":", ":", parts: 2)

    [
      hostname: uri.host,
      port: uri.port || 5432,
      username: empty_to_nil(user),
      password: empty_to_nil(pass),
      database: empty_to_nil(String.trim_leading(uri.path || "", "/"))
    ]
  end

  defp empty_to_nil(""), do: nil
  defp empty_to_nil(value), do: value
end
