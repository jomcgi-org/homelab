defmodule Embervm.SpecTrace.CheckerTest do
  use ExUnit.Case, async: false

  alias Embervm.SpecTrace
  alias Embervm.SpecTrace.Checker
  alias Embervm.SpecTrace.Store.SQLite

  setup do
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    # The gate lives in `:persistent_term`, which is global to the BEAM and is
    # NOT rolled back between test modules. Without this restore, every module
    # ordered after this one starts with the trace enabled, and `async: false`
    # means that is decided by the ExUnit seed rather than by anything visible
    # in the file. It failed exactly that way: router_test's conformance cases
    # assert `enabled == false`, saw a leaked `true`, queried a store that is
    # not running in their context, and got a 500 whose body has no "verdicts"
    # key at all.
    on_exit(fn ->
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      SpecTrace.configure()
    end)

    store = start_supervised!({SQLite, name: nil, path: ":memory:"})
    {:ok, store: store}
  end

  describe "negative fixtures" do
    # health_monotonic was the ONE invariant shipped without a fixture, and it was
    # also the one that was wrong. Two real bugs sat on a path no test reached: it
    # grouped ALL records rather than health records, and its reducer discarded the
    # accumulator, so every age_to_down halted as a violation and a node that merely
    # went unknown reported one. The untested path and the broken path were the same
    # path, which is the argument for a fixture per invariant rather than per feature.
    test "health_monotonic fails on age_to_down with no preceding age_to_unknown", %{store: store} do
      run_id = "test-run-health"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "reconnect",
          "vars" => %{"node_id" => "node-7", "gen" => 1}
        },
        # Straight to down without passing through unknown: the health machine
        # ages healthy -> unknown -> down, so this ordering cannot occur.
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "age_to_down",
          "vars" => %{"node_id" => "node-7", "last_gen" => 1}
        }
      ]

      :ok = SQLite.write(store, records)
      verdicts = Checker.run(SQLite, store)
      health = Enum.find(verdicts, &(&1[:invariant] == :health_monotonic))

      assert health[:verdict] == :fail
      assert health[:coverage] == 2
      assert String.contains?(health[:detail], "node-7")
    end

    test "health_monotonic passes on a lawful unknown-then-down sequence", %{store: store} do
      run_id = "test-run-health-ok"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "age_to_unknown",
          "vars" => %{"node_id" => "node-8", "last_gen" => 3}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "age_to_down",
          "vars" => %{"node_id" => "node-8", "last_gen" => 3}
        }
      ]

      :ok = SQLite.write(store, records)
      verdicts = Checker.run(SQLite, store)
      health = Enum.find(verdicts, &(&1[:invariant] == :health_monotonic))

      assert health[:verdict] == :pass
      assert health[:coverage] == 2
    end

    test "no_double_assign fails when vm_id appears in two dispatches", %{store: store} do
      run_id = "test-run-1"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "dispatch_miss",
          "vars" => %{"vm_id" => "vm-1"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      no_double_assign = Enum.find(verdicts, &(&1[:invariant] == :no_double_assign))

      assert no_double_assign[:verdict] == :fail
      assert no_double_assign[:coverage] == 2
      assert String.contains?(no_double_assign[:detail], "vm-1")
    end

    test "dispatch_provenance fails when dispatch lacks prime and adopted flag", %{store: store} do
      run_id = "test-run-2"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-2", "provenance" => nil}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      provenance = Enum.find(verdicts, &(&1[:invariant] == :dispatch_provenance))

      assert provenance[:verdict] == :fail
      assert provenance[:coverage] == 1
      assert String.contains?(provenance[:detail], "vm-2")
    end

    test "dispatch_provenance passes when dispatch has prime before it", %{store: store} do
      run_id = "test-run-3"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-3", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-3"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      provenance = Enum.find(verdicts, &(&1[:invariant] == :dispatch_provenance))

      assert provenance[:verdict] == :pass
      assert provenance[:coverage] == 1
    end

    test "dispatch_provenance passes when dispatch carries adopted provenance", %{store: store} do
      run_id = "test-run-4"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-4", "provenance" => "adopted"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      provenance = Enum.find(verdicts, &(&1[:invariant] == :dispatch_provenance))

      assert provenance[:verdict] == :pass
      assert provenance[:coverage] == 1
    end

    test "adopt_idempotent fails when vm_id appears in two adopt_inventory records", %{store: store} do
      run_id = "test-run-5"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-5", "vm-6"]}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-5", "vm-7"]}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      adopt = Enum.find(verdicts, &(&1[:invariant] == :adopt_idempotent))

      assert adopt[:verdict] == :fail
      assert adopt[:coverage] == 2
      assert String.contains?(adopt[:detail], "vm-5")
    end

    test "adopt_idempotent passes when vm_ids don't repeat across adopts", %{store: store} do
      run_id = "test-run-6"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-5", "vm-6"]}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-7", "vm-8"]}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      adopt = Enum.find(verdicts, &(&1[:invariant] == :adopt_idempotent))

      assert adopt[:verdict] == :pass
      assert adopt[:coverage] == 2
    end

    test "prime_before_checkpoint fails when checkpoint has unreprimed vm_id", %{store: store} do
      run_id = "test-run-7"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-a", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "checkpoint",
          # The PRODUCTION shape: dispatcher.ex builds a map of
          # "node:workload" => [vm_id]. These fixtures used a list of triples,
          # which the reader parsed happily while production's map parsed to
          # nothing, so the invariant passed here and checked nothing live.
          "vars" => %{"node_workload_vm_ids" => %{"n1:w1" => ["vm-a", "vm-b"]}}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      checkpoint = Enum.find(verdicts, &(&1[:invariant] == :prime_before_checkpoint))

      assert checkpoint[:verdict] == :fail
      assert checkpoint[:coverage] == 1
    end

    test "prime_before_checkpoint passes when all checkpoint vms are primed", %{store: store} do
      run_id = "test-run-8"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-a", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 150,
          "ts" => 1500,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-b", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 3,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "checkpoint",
          # The PRODUCTION shape: dispatcher.ex builds a map of
          # "node:workload" => [vm_id]. These fixtures used a list of triples,
          # which the reader parsed happily while production's map parsed to
          # nothing, so the invariant passed here and checked nothing live.
          "vars" => %{"node_workload_vm_ids" => %{"n1:w1" => ["vm-a", "vm-b"]}}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      checkpoint = Enum.find(verdicts, &(&1[:invariant] == :prime_before_checkpoint))

      assert checkpoint[:verdict] == :pass
      assert checkpoint[:coverage] == 1
    end
  end

  describe "eventually_dispatched" do
    test "a dispatch before a later queued wedge does not satisfy the wedge", %{store: store} do
      task_id = "task-requeued-wedge"

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_queue_edge(1, task_id, "W"),
          eventually_progress(10, "dispatch_warm", task_id),
          eventually_queue_edge(49, task_id, "W"),
          eventually_checkpoint(50, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(60, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(70, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]})
        ])

      assert verdict.verdict == :fail
      assert verdict.coverage == 1
      assert verdict.detail =~ task_id
    end

    test "a boot cohort slides past its first empty-inventory checkpoint", %{store: store} do
      task_id = "task-boot-wedge"

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_queue_edge(1, task_id, "W"),
          eventually_checkpoint(10, [queued_task(task_id, "W")], %{}),
          eventually_checkpoint(20, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(30, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(40, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]})
        ])

      assert verdict.verdict == :fail
      assert verdict.coverage == 1
      assert verdict.detail =~ task_id
    end

    test "progress strictly before a queued window does not satisfy the window", %{store: store} do
      task_id = "task-old-progress"

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_queue_edge(1, task_id, "W"),
          eventually_progress(10, "dispatch_miss", task_id),
          eventually_checkpoint(20, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(30, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(40, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]})
        ])

      assert verdict.verdict == :fail
      assert verdict.detail =~ task_id
    end

    test "a dispatch inside a persistent queued window passes", %{store: store} do
      task_id = "task-progress"

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_checkpoint(10, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_progress(20, "dispatch_warm", task_id),
          eventually_checkpoint(30, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(40, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]})
        ])

      assert verdict.verdict == :pass
      assert verdict.coverage == 1
    end

    test "progress is indexed by task across shared overlapping windows", %{store: store} do
      task_a = "task-a-pass"
      task_b = "task-b-no-progress"
      task_c = "task-c-partial-progress"

      queued_tasks = [
        queued_task(task_a, "W"),
        queued_task(task_b, "W"),
        queued_task(task_c, "W")
      ]

      checkpoints =
        for mono <- [10, 20, 30, 40] do
          eventually_checkpoint(mono, queued_tasks, %{"node:W" => ["vm-w"]})
        end

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_progress(15, "succeed", task_c),
          eventually_progress(25, "dispatch_warm", task_a)
          | checkpoints
        ])

      assert verdict == %{
               invariant: :eventually_dispatched,
               verdict: :fail,
               coverage: 3,
               oracle: :trace_only,
               detail:
                 "violating task_ids: \"task-b-no-progress\", \"task-c-partial-progress\""
             }
    end

    test "inventory for an unrelated workload leaves the task unjudged", %{store: store} do
      task_id = "task-workload-w"

      checkpoints =
        for mono <- [10, 20, 30] do
          eventually_checkpoint(
            mono,
            [queued_task(task_id, "W")],
            %{"node:V" => ["vm-v"]}
          )
        end

      verdict = eventually_dispatched_verdict(store, checkpoints)

      assert verdict.verdict == :vacuous
      assert verdict.coverage == 0
      assert verdict.detail =~ "inventory"
      assert verdict.detail =~ "W"
    end

    test "a task omitted from a truncated checkpoint is unjudged", %{store: store} do
      task_id = "task-truncated"

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_checkpoint(10, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(20, [], %{"node:W" => ["vm-w"]}, truncated: true),
          eventually_checkpoint(30, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]})
        ])

      assert verdict.verdict == :vacuous
      assert verdict.coverage == 0
      assert verdict.detail =~ "truncation"
      assert verdict.detail =~ task_id
    end

    test "checkpoints without queued testimony are old-format vacuous", %{store: store} do
      checkpoints =
        for mono <- [10, 20, 30] do
          eventually_checkpoint(mono, :absent, %{"node:W" => ["vm-w"]})
        end

      verdict = eventually_dispatched_verdict(store, checkpoints)

      assert verdict.verdict == :vacuous
      assert verdict.coverage == 0
      assert verdict.detail =~ "old-format"
      refute verdict.detail =~ "3 consecutive"
    end

    test "a task queued for only K checkpoints has no bounded window", %{store: store} do
      task_id = "task-short"

      verdict =
        eventually_dispatched_verdict(store, [
          eventually_checkpoint(10, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(20, [queued_task(task_id, "W")], %{"node:W" => ["vm-w"]}),
          eventually_checkpoint(30, [], %{"node:W" => ["vm-w"]})
        ])

      assert verdict.verdict == :vacuous
      assert verdict.coverage == 0
      assert verdict.detail =~ "3 consecutive checkpoints"
    end
  end

  describe "vacuous verdicts" do
    test "all invariants return :vacuous on empty trace", %{store: store} do
      # Write no adoption records at all
      verdicts = Checker.run(SQLite, store)

      Enum.each(verdicts, fn verdict ->
        assert verdict[:verdict] == :vacuous
        assert verdict[:coverage] == 0
      end)
    end

    # A BUSY trace that happens to contain no destroys. This is the case the
    # empty-trace test above cannot reach, and a regression lived in exactly that
    # gap: destroy_intent_precedes_record guarded on `records == []` rather than
    # on the destroy records, so a run full of primes and checkpoints fell
    # through to the violation scan, found nothing to violate, and returned PASS.
    #
    # PASS there is a claim that the destroy ordering held on a run that never
    # destroyed anything. An empty-trace test cannot catch it because the buggy
    # guard is accidentally correct when the whole trace is empty. The busy case
    # is the discriminating one.
    # An inventory the reader cannot parse must be VACUOUS, never PASS. This is
    # the shape the old reader silently produced on every production trace: a
    # checkpoint declaring inventory, none of it readable, an empty vm_id set,
    # and Enum.any? over empty returning false, so PASS.
    test "prime_before_checkpoint is :vacuous when checkpoint inventory is unreadable", %{store: store} do
      run_id = "test-run-unreadable"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "checkpoint",
          "vars" => %{"node_workload_vm_ids" => ["totally-unexpected", 42]}
        }
      ]

      assert :ok = SQLite.write(store, records)
      verdicts = Checker.run(SQLite, store)
      checkpoint = Enum.find(verdicts, &(&1[:invariant] == :prime_before_checkpoint))

      assert checkpoint[:verdict] == :vacuous,
             "an unreadable inventory must not report #{inspect(checkpoint[:verdict])}"

      refute checkpoint[:verdict] == :pass
      assert checkpoint[:detail] =~ "unreadable"
    end

    test "destroy invariants are :vacuous on a busy trace with no destroys", %{store: store} do
      run_id = "test-run-no-destroys"

      records = [
        %{"run_id" => run_id, "seq" => 1, "mono" => 1, "ts" => 10, "spec" => "adoption", "action" => "prime", "vars" => %{"vm_id" => "vm-1"}},
        %{"run_id" => run_id, "seq" => 2, "mono" => 2, "ts" => 11, "spec" => "adoption", "action" => "checkpoint", "vars" => %{"inventory" => ["vm-1"]}}
      ]

      assert :ok = SQLite.write(store, records)
      verdicts = Checker.run(SQLite, store)

      for invariant <- [:destroy_intent_precedes_record, :no_destroy_before_confirm] do
        verdict = Enum.find(verdicts, &(&1[:invariant] == invariant))

        assert verdict[:verdict] == :vacuous,
               "#{invariant} must be :vacuous with no destroys observed, got #{inspect(verdict[:verdict])}"

        refute verdict[:verdict] == :pass
      end
    end

    test "no_double_assign is :vacuous when no dispatches", %{store: store} do
      run_id = "test-run-9"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-1", "node_id" => "n1"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      no_double = Enum.find(verdicts, &(&1[:invariant] == :no_double_assign))

      assert no_double[:verdict] == :vacuous
      assert no_double[:coverage] == 0
    end
  end

  describe "run_id boundary handling" do
    test "does not report violations across run_id boundaries", %{store: store} do
      # Two separate runs: same vm_id but in different runs should not be a violation
      records = [
        %{
          "run_id" => "run-1",
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-same"}
        },
        %{
          "run_id" => "run-2",
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-same"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      # Should have verdicts for both runs, not a cross-run violation
      assert length(verdicts) >= 10  # At least 5 invariants per run
    end
  end

  describe "inventory reconciliation" do
    test "inventory_reconciled fails on a suppress-primed wedge", %{store: store} do
      :ok = SQLite.write(store, [inventory_checkpoint("wedge", 100, %{"node-1:wl" => []}, %{"node-1" => %{"live_vms" => 1, "primed_count" => 0}})])

      verdict = inventory_verdict(Checker.run(SQLite, store))

      assert verdict[:verdict] == :fail
      assert verdict[:oracle] == :node_reconciled
      assert verdict[:coverage] == 1
      assert verdict[:detail] =~ "node-1"
      assert verdict[:detail] =~ "1"
      assert verdict[:detail] =~ "100"
    end

    test "inventory_reconciled does not fail for an idle node", %{store: store} do
      :ok = SQLite.write(store, [inventory_checkpoint("idle", 200, %{"node-1:wl" => []}, %{"node-1" => %{"live_vms" => 0, "primed_count" => 0}})])

      assert inventory_verdict(Checker.run(SQLite, store))[:verdict] == :pass
    end

    test "inventory_reconciled is vacuous when node_reported is absent", %{store: store} do
      record = inventory_checkpoint("absent", 300, %{"node-1:wl" => []}, nil)
      record = update_in(record, ["vars"], &Map.delete(&1, "node_reported"))
      :ok = SQLite.write(store, [record])

      verdict = inventory_verdict(Checker.run(SQLite, store))

      assert verdict[:verdict] == :vacuous
      assert verdict[:detail] =~ "absent"
    end

    test "inventory_reconciled is vacuous for an empty node_reported map", %{store: store} do
      :ok = SQLite.write(store, [inventory_checkpoint("empty", 400, %{"node-1:wl" => []}, %{})])

      verdict = inventory_verdict(Checker.run(SQLite, store))

      assert verdict[:verdict] == :vacuous
      assert verdict[:detail] =~ "no dispatchable node instance in the checkpoint testimony"
    end

    test "inventory_reconciled is vacuous when checkpoint inventory is unreadable", %{store: store} do
      record = inventory_checkpoint("missing-inventory", 500, %{}, %{"node-1" => %{"live_vms" => 1, "primed_count" => 0}})
      record = put_in(record, ["vars", "node_workload_vm_ids"], nil)
      :ok = SQLite.write(store, [record])

      verdict = inventory_verdict(Checker.run(SQLite, store))

      assert verdict[:verdict] == :vacuous
      assert verdict[:detail] =~ "no checkpoint carried a readable inventory"
    end
  end

  describe "destroy invariants" do
    test "destroy_intent_precedes_record passes when gate on and begin precedes confirm", %{store: store} do
      :ok = SQLite.write(store, destroy_records(true, true, true, 100, 200))
      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)

      assert verdict[:verdict] == :pass
      assert verdict[:coverage] > 0
    end

    test "destroy_intent_precedes_record passes when begin_destroy resumed and followed by confirm", %{store: store} do
      records = [
        %{
          "run_id" => "test-run-resumed", "seq" => 1, "mono" => 100, "ts" => 1000,
          "spec" => "adoption", "action" => "begin_destroy",
          "vars" => %{"session_id" => "session-resumed", "vm_id" => "vm-resumed", "node_id" => "node-resumed", "resumed" => true}
        },
        %{
          "run_id" => "test-run-resumed", "seq" => 2, "mono" => 200, "ts" => 2000,
          "spec" => "adoption", "action" => "confirm_destroy",
          "vars" => %{"session_id" => "session-resumed", "vm_id" => "vm-resumed", "node_id" => "node-resumed", "had_vm" => true, "node_confirmed" => true, "gate" => true}
        }
      ]

      :ok = SQLite.write(store, records)
      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)

      assert verdict[:verdict] == :pass
    end

    test "destroy_intent_precedes_record fails when confirm precedes begin", %{store: store} do
      :ok = SQLite.write(store, destroy_records(true, true, true, 200, 100))
      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)

      assert verdict[:verdict] == :fail
      assert String.contains?(verdict[:detail], "session-destroy")
    end

    test "destroy_intent_precedes_record passes when gate is off", %{store: store} do
      :ok = SQLite.write(store, destroy_records(false, false, false, 100, 200))
      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)

      assert verdict[:verdict] == :pass
      assert verdict[:coverage] > 0

      :ok = SQLite.write(store, [destroy_record("confirm_destroy", 300, false, false)])
      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)
      assert verdict[:verdict] == :pass
    end

    test "destroy_intent_precedes_record excludes banked destroys", %{store: store} do
      records = [
        destroy_record("begin_destroy", 100, true, false),
        put_in(destroy_record("confirm_destroy", 200, true, false), ["vars", "had_vm"], false)
      ]

      :ok = SQLite.write(store, records)
      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)

      assert verdict[:verdict] == :vacuous
      assert verdict[:coverage] == 0
      assert String.contains?(verdict[:detail], "1 snapshot-only")
    end

    test "destroy_intent_precedes_record is vacuous when had_vm is absent", %{store: store} do
      record = destroy_record("confirm_destroy", 100, true, true)
      record = update_in(record, ["vars"], &Map.delete(&1, "had_vm"))
      :ok = SQLite.write(store, [record])

      verdict = destroy_verdict(Checker.run(SQLite, store), :destroy_intent_precedes_record)

      assert verdict[:verdict] == :vacuous
      assert String.contains?(verdict[:detail], "lack had_vm")
    end

    test "no_destroy_before_confirm passes with node confirmation and gate on", %{store: store} do
      :ok = SQLite.write(store, [destroy_record("confirm_destroy", 100, true, true)])
      verdict = destroy_verdict(Checker.run(SQLite, store), :no_destroy_before_confirm)

      assert verdict[:verdict] == :pass
    end

    test "no_destroy_before_confirm fails without node confirmation and gate on", %{store: store} do
      :ok = SQLite.write(store, [destroy_record("confirm_destroy", 100, true, false)])
      verdict = destroy_verdict(Checker.run(SQLite, store), :no_destroy_before_confirm)

      assert verdict[:verdict] == :fail
      assert String.contains?(verdict[:detail], "vm-destroy")
    end

    test "no_destroy_before_confirm is vacuous when gate is off", %{store: store} do
      :ok = SQLite.write(store, [destroy_record("confirm_destroy", 100, false, false)])
      verdict = destroy_verdict(Checker.run(SQLite, store), :no_destroy_before_confirm)

      assert verdict[:verdict] == :vacuous
      assert verdict[:coverage] == 0
    end

    test "no_destroy_before_confirm excludes banked destroys under gate on", %{store: store} do
      record = put_in(destroy_record("confirm_destroy", 100, true, false), ["vars", "had_vm"], false)
      :ok = SQLite.write(store, [record])

      verdict = destroy_verdict(Checker.run(SQLite, store), :no_destroy_before_confirm)

      assert verdict[:verdict] == :vacuous
      assert verdict[:coverage] == 0
      assert String.contains?(verdict[:detail], "1 snapshot-only")
    end

    test "no_destroy_before_confirm is vacuous when had_vm is absent", %{store: store} do
      record = destroy_record("confirm_destroy", 100, true, true)
      record = update_in(record, ["vars"], &Map.delete(&1, "had_vm"))
      :ok = SQLite.write(store, [record])

      verdict = destroy_verdict(Checker.run(SQLite, store), :no_destroy_before_confirm)

      assert verdict[:verdict] == :vacuous
      assert String.contains?(verdict[:detail], "lack had_vm")
    end
  end

  describe "the moduledoc and invariants/0 agree" do
    # #4802 is about lists that must agree and are kept in sync by hand, where
    # disagreement reports green rather than erroring. Both router sites now read
    # `invariants/0`, so the code copies are gone, but the prose copy in the
    # moduledoc survived and immediately drifted: it documented 6 of the 8 and
    # numbered the last one 8, which is what a reader consults to learn what the
    # endpoint covers. An operator reading it during an incident would conclude
    # the destroy ordering is not checked at all.
    #
    # Deriving the expected heading from the atom is the point. A hand-written
    # list of expected headings here would be a FIFTH copy, drifting the same way.
    test "every invariant in invariants/0 has a moduledoc entry" do
      {:docs_v1, _, _, _, %{"en" => moduledoc}, _, _} = Code.fetch_docs(Checker)

      undocumented =
        Enum.reject(Checker.invariants(), fn invariant ->
          heading =
            invariant
            |> Atom.to_string()
            |> String.split("_")
            |> Enum.map_join(&String.capitalize/1)

          String.contains?(moduledoc, "**#{heading}**")
        end)

      assert undocumented == [],
             "invariants evaluated but not documented: #{inspect(undocumented)}"
    end

    # The other direction. A heading with no invariant behind it is the same
    # defect read backwards: the moduledoc promises a check the endpoint never
    # runs, and nothing fails because prose cannot fail.
    test "every moduledoc entry is an invariant invariants/0 evaluates" do
      {:docs_v1, _, _, _, %{"en" => moduledoc}, _, _} = Code.fetch_docs(Checker)

      documented =
        Regex.scan(~r/^\s*\d+\. \*\*(\w+)\*\*/m, moduledoc, capture: :all_but_first)
        |> List.flatten()

      evaluated =
        Enum.map(Checker.invariants(), fn invariant ->
          invariant
          |> Atom.to_string()
          |> String.split("_")
          |> Enum.map_join(&String.capitalize/1)
        end)

      # Positive control: a regex that matches nothing would make the assertion
      # below pass over an empty list, which is the empty-collection false PASS
      # this whole module exists to refuse.
      assert length(documented) == length(evaluated)
      assert documented -- evaluated == []
    end
  end

  defp destroy_verdict(verdicts, invariant) do
    Enum.find(verdicts, &(&1[:invariant] == invariant))
  end

  defp inventory_verdict(verdicts) do
    Enum.find(verdicts, &(&1[:invariant] == :inventory_reconciled))
  end

  defp eventually_dispatched_verdict(store, records) do
    :ok = SQLite.write(store, records)

    Checker.run(SQLite, store)
    |> Enum.find(&(&1.invariant == :eventually_dispatched))
  end

  defp eventually_checkpoint(mono, queued_tasks, inventory, opts \\ []) do
    vars = %{"node_workload_vm_ids" => inventory}

    vars =
      if queued_tasks == :absent do
        vars
      else
        Map.put(vars, "queued_tasks", queued_tasks)
      end

    vars =
      if Keyword.get(opts, :truncated, false) do
        Map.put(vars, "queued_tasks_truncated", true)
      else
        vars
      end

    eventually_record("checkpoint", mono, vars)
  end

  defp eventually_progress(mono, action, task_id) do
    eventually_record(action, mono, %{"task_id" => task_id})
  end

  defp eventually_queue_edge(mono, task_id, workload) do
    eventually_record("queue_task", mono, %{"task_id" => task_id, "workload" => workload})
  end

  defp queued_task(task_id, workload), do: %{"task_id" => task_id, "workload" => workload}

  defp eventually_record(action, mono, vars) do
    %{
      "run_id" => "test-run-eventually-dispatched",
      "seq" => mono,
      "mono" => mono,
      "ts" => mono * 10,
      "spec" => "adoption",
      "action" => action,
      "vars" => vars
    }
  end

  defp inventory_checkpoint(run_id, mono, inventory, node_reported) do
    vars = %{"node_workload_vm_ids" => inventory}
    vars = if is_nil(node_reported), do: vars, else: Map.put(vars, "node_reported", node_reported)

    %{
      "run_id" => "test-run-#{run_id}",
      "seq" => mono,
      "mono" => mono,
      "ts" => mono * 10,
      "spec" => "adoption",
      "action" => "checkpoint",
      "vars" => vars
    }
  end

  defp destroy_records(gate, node_confirmed, _begin_gate, begin_mono, confirm_mono) do
    [
      destroy_record("begin_destroy", begin_mono, gate, node_confirmed),
      destroy_record("confirm_destroy", confirm_mono, gate, node_confirmed)
    ]
  end

  defp destroy_record(action, mono, gate, node_confirmed) do
    %{
      "run_id" => "test-run-destroy",
      "seq" => mono,
      "mono" => mono,
      "ts" => mono * 10,
      "spec" => "adoption",
      "action" => action,
      "vars" => %{
        "session_id" => "session-destroy",
        "vm_id" => "vm-destroy",
        "node_id" => "node-destroy",
        "gate" => gate,
        "had_vm" => true,
        "node_confirmed" => node_confirmed
      }
    }
  end
end
