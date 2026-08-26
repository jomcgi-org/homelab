defmodule Embervm.SpecTrace.ReachabilityTest do
  @moduledoc """
  Every invariant must be able to produce all three verdicts, or say why not.

  ## Why this exists

  Six false PASSes were found in the checker in a single evening (#4770), two of
  them live in production behind `GET /v1/conformance`. Every one was invisible
  to the test suite, and they all failed in the SAME direction, which is not
  luck: a reader that cannot parse its input yields an empty collection, every
  invariant asks "is there a violation in this collection", and every such
  question answers no on empty. Parse failure and clean bill of health were
  literally the same value.

  So this asserts the property those tests could not: that each invariant can
  DISTINGUISH. An invariant that only ever returns `:pass` is decoration
  regardless of how correct its logic reads.

  ## Fidelity: this is TIER 2, and tier 3 is NOT satisfied

      tier 1  hand-built maps -> checker
      tier 2  emit -> real Writer -> real Store -> checker    <- WE ARE HERE
      tier 3  real emitters (dispatcher, session_manager) -> real transport

  Tier 2 catches serialisation bugs, because records traverse `jsonable/1`, the
  Writer's batching and the store's round trip. That is how `true` reaching the
  store as the string `"true"` would be caught.

  **It does NOT catch a writer-reader key mismatch.** At tier 2 the scenario
  author writes whatever key they believe in, which is the same belief the
  checker encodes, so both agree and production disagrees with both. That is
  exactly how `adopt_inventory` shipped emitting `"adopted"` while the checker
  read `"vm_ids"` in five places, passing every test.

  Tier 3 needs forced interleavings (a crash mid-destroy, a straggler status
  frame after a forget) and belongs to the hermetic lane, #4761. Until that
  exists, do not read a green run here as "the invariants work against
  production". Read it as "the invariants can distinguish, given records of the
  shape we believe in". Overclaiming coverage is the defect class this file
  exists to catch, and this file is not exempt from it.

  ## Exemptions

  An invariant whose FAIL no real emitter can produce declares `fail_exempt`
  with a reason, rather than fabricating a record to make the test green. A
  fabricated FAIL proves the checker can read a value nothing writes, which is
  worse than no assertion: it reports coverage that does not exist.
  """

  use ExUnit.Case, async: false

  alias Embervm.SpecTrace
  alias Embervm.SpecTrace.Checker
  alias Embervm.SpecTrace.Store.SQLite

  # `fail:` means a real emitter can produce this. `fail_exempt:` means it
  # cannot, with the reason. Do not convert an exemption into a fabricated
  # scenario to make the matrix look full.
  @registry %{
    no_double_assign: %{
      pass: [{"adoption", "dispatch_warm", %{"vm_id" => "vm-pass"}}],
      fail: [
        {"adoption", "dispatch_warm", %{"vm_id" => "vm-fail"}},
        {"adoption", "dispatch_miss", %{"vm_id" => "vm-fail"}}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    dispatch_provenance: %{
      pass: [
        {"adoption", "prime", %{"vm_id" => "vm-pass"}},
        {"adoption", "dispatch_warm", %{"vm_id" => "vm-pass"}}
      ],
      fail: [{"adoption", "dispatch_warm", %{"vm_id" => "vm-fail"}}],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    adopt_idempotent: %{
      pass: [
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-one"]}},
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-two"]}}
      ],
      fail: [
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-repeat"]}},
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-repeat"]}}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    health_monotonic: %{
      pass: [
        {"adoption", "age_to_unknown", %{"node_id" => "node-pass"}},
        {"adoption", "age_to_down", %{"node_id" => "node-pass"}}
      ],
      fail: [{"adoption", "age_to_down", %{"node_id" => "node-fail"}}],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    prime_before_checkpoint: %{
      # The map shape is the PRODUCTION shape, `%{"node:workload" => [vm_id]}`.
      # The fixtures used a list of triples for months while the dispatcher
      # emitted this, so the reader parsed nothing and the invariant passed on
      # every production trace (#4811).
      pass: [
        {"adoption", "prime", %{"vm_id" => "vm-pass"}},
        {"adoption", "checkpoint", %{"node_workload_vm_ids" => %{"node:workload" => ["vm-pass"]}}}
      ],
      fail: [
        {"adoption", "checkpoint", %{"node_workload_vm_ids" => %{"node:workload" => ["vm-fail"]}}}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    eventually_dispatched: %{
      # THREE checkpoints, because K is 2 and a window is K+1. Two would render
      # vacuous ("no bounded window could be formed"), and the fix for that is a
      # longer scenario, never a smaller K: shrinking the bound to fit the test
      # is how a liveness gate ratchets itself into never firing.
      pass: [
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-pass", "workload" => "workload"}],
          "node_workload_vm_ids" => %{"node:workload" => ["vm-pass"]}
        }},
        {"adoption", "dispatch_warm", %{"task_id" => "task-pass", "vm_id" => "vm-pass"}},
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-pass", "workload" => "workload"}],
          "node_workload_vm_ids" => %{"node:workload" => ["vm-pass"]}
        }},
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-pass", "workload" => "workload"}],
          "node_workload_vm_ids" => %{"node:workload" => ["vm-pass"]}
        }}
      ],
      fail: [
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-fail", "workload" => "workload"}],
          "node_workload_vm_ids" => %{"node:workload" => ["vm-fail"]}
        }},
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-fail", "workload" => "workload"}],
          "node_workload_vm_ids" => %{"node:workload" => ["vm-fail"]}
        }},
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-fail", "workload" => "workload"}],
          "node_workload_vm_ids" => %{"node:workload" => ["vm-fail"]}
        }}
      ],
      vacuous: [
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-vacuous", "workload" => "workload"}],
          "node_workload_vm_ids" => %{}
        }},
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-vacuous", "workload" => "workload"}],
          "node_workload_vm_ids" => %{}
        }},
        {"adoption", "checkpoint", %{
          "queued_tasks" => [%{"task_id" => "task-vacuous", "workload" => "workload"}],
          "node_workload_vm_ids" => %{}
        }}
      ]
    },
    inventory_reconciled: %{
      pass: [
        {"adoption", "checkpoint", %{
          "node_workload_vm_ids" => %{"node:workload" => ["vm-pass"]},
          "node_reported" => %{"node" => %{"live_vms" => 1, "connected" => true}}
        }}
      ],
      fail: [
        {"adoption", "checkpoint", %{
          "node_workload_vm_ids" => %{"node:workload" => []},
          "node_reported" => %{"node" => %{"live_vms" => 1, "connected" => true}}
        }}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    destroy_intent_precedes_record: %{
      pass: [
        {"adoption", "begin_destroy", %{"session_id" => "session-pass"}},
        {"adoption", "confirm_destroy", %{"session_id" => "session-pass", "had_vm" => true}}
      ],
      # Genuinely realizable, and the asymmetry with its sibling is the point:
      # this invariant correlates TWO INDEPENDENT records, so any future code
      # path that destroys a live VM without first appending the durable intent
      # produces exactly this trace. #4813 was one such path, found this way.
      fail: [{"adoption", "confirm_destroy", %{"session_id" => "session-fail", "had_vm" => true}}],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    no_destroy_before_confirm: %{
      pass: [
        {"adoption", "confirm_destroy", %{"gate" => true, "node_confirmed" => true, "had_vm" => true}}
      ],
      fail_exempt:
        "No emitter can produce a confirm_destroy with gate true, had_vm true and " <>
          "node_confirmed false. The redrive site derives node_confirmed from " <>
          "confirmed_by, whose only two callers pass \"teardown\" and \"absence\", " <>
          "both of which map to true. The node-confirmed site sits inside " <>
          "`if confirmed do`, so it is provably true wherever it is reached. The " <>
          "legacy site emits nil when there is no VM, and those records are " <>
          "excluded by the had_vm filter before evaluation. Every gate-on path " <>
          "that writes the terminal op has already confirmed teardown, which is " <>
          "the property holding BY CONSTRUCTION rather than by observation. " <>
          "Unlike destroy_intent_precedes_record it reads a single field on a " <>
          "single record, so there is no correlation a future bypass path could " <>
          "break. A fabricated record would prove only that the checker can read " <>
          "a value nothing writes. See #4809.",
      vacuous: [
        {"adoption", "confirm_destroy", %{"gate" => false, "node_confirmed" => false, "had_vm" => true}}
      ]
    }
  }

  setup do
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    # The gate is a process-global `:persistent_term` outside ExUnit's rollback,
    # so a module that does not restore it decides, by ExUnit seed alone, whether
    # later modules run with the trace on. That already cost a CI failure once.
    on_exit(fn ->
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      SpecTrace.configure()
      :persistent_term.erase({SpecTrace, :run_id})
      :persistent_term.erase({SpecTrace, :dropped})
    end)

    store = start_supervised!({SQLite, name: nil, path: ":memory:"})
    writer = start_supervised!({SpecTrace.Writer, store_mod: SQLite, store: store})
    {:ok, store: store, writer: writer}
  end

  test "the registry covers every checker invariant, exactly" do
    assert map_size(@registry) > 0, "the registry is empty, so this file asserts nothing"

    assert MapSet.new(Map.keys(@registry)) == MapSet.new(Checker.invariants()),
           "the registry and Checker.invariants() disagree. An invariant missing here is " <>
             "unverified for reachability, which is how an invariant that can only ever " <>
             "return :pass survives review."
  end

  test "every invariant declares pass, vacuous, and either fail or an exemption" do
    for {invariant, entry} <- @registry do
      assert Map.has_key?(entry, :pass), "#{invariant} declares no pass scenario"
      assert Map.has_key?(entry, :vacuous), "#{invariant} declares no vacuous scenario"

      assert Map.has_key?(entry, :fail) or Map.has_key?(entry, :fail_exempt),
             "#{invariant} declares neither a fail scenario nor a fail_exempt reason. " <>
               "An invariant whose FAIL is unreachable must say so and why, because " <>
               "silently omitting it is indistinguishable from forgetting it."

      refute Map.has_key?(entry, :fail) and Map.has_key?(entry, :fail_exempt),
             "#{invariant} declares both a fail scenario and an exemption. Pick one: " <>
               "either a real emitter can produce it, or it cannot."

      if reason = entry[:fail_exempt] do
        assert is_binary(reason) and String.length(reason) > 80,
               "#{invariant}'s exemption needs a reason explaining why no emitter can " <>
                 "produce the failing record. An exemption without one is an excuse."
      end
    end
  end

  for {invariant, entry} <- @registry,
      {expected_verdict, scenarios} <- Map.take(entry, [:pass, :fail, :vacuous]) do
    test "#{invariant} reaches #{expected_verdict}", %{store: store, writer: writer} do
      scenarios = unquote(Macro.escape(scenarios))

      # Through the REAL emit path and the REAL Writer, never handed to the
      # checker as a map. That is what puts jsonable/1 and the store round trip
      # inside the assertion.
      Enum.each(scenarios, fn {spec, action, vars} ->
        assert :ok = SpecTrace.emit(spec, action, vars)
      end)

      assert :ok = SpecTrace.drain(writer)

      verdict =
        SQLite
        |> Checker.run(store)
        |> Enum.find(&(&1.invariant == unquote(invariant)))

      assert verdict, "#{unquote(invariant)} returned no verdict at all"

      assert verdict.verdict == unquote(expected_verdict),
             "expected #{unquote(expected_verdict)}, got #{inspect(verdict.verdict)} " <>
               "with detail #{inspect(verdict.detail)}"
    end
  end
end
