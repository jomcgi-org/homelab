defmodule Embervm.SpecTrace.Checker do
  @moduledoc """
  Evaluates adoption.tla invariants over spec-trace records.

  Returns a list of verdict maps:
    %{
      invariant: atom(),
      verdict: :pass | :fail | :vacuous,
      coverage: non_neg_integer(),
      oracle: :trace_only | :node_reconciled,
      detail: term()
    }

  `:vacuous` is a distinct outcome, not a pass. An empty or thin trace satisfies
  every invariant. If an invariant had nothing to check, say so. This is not
  theoretical: on the first live run of the primed-to-assigned correlation,
  zero assignments had occurred in the window and a naive checker would have
  reported "0 violations, PASS" over an empty set.

  `coverage` is how many instances were actually examined. A verdict without a
  denominator overclaims.

  `oracle` records what the verdict was checked against. Everything in this PR
  is `:trace_only` (the trace is control-plane testimony). Node reconciliation
  against `GET /v1/nodes` is later work, and the field exists so a report can
  never render "the system does what the spec says" and "the log agrees with
  itself" identically.

  Invariants (from adoption.tla):

  1. **NoDoubleAssign** — no vm_id appears in two dispatches without intervening
     consumption. Single-use vms make this strict: a vm_id in two `dispatch_*`
     records (without a consume between) is a violation.

  2. **DispatchProvenance** — every `dispatch_warm` / `dispatch_miss` either has
     a preceding `prime` record for that vm_id in the same run, OR carries
     `provenance: "adopted"`.

  3. **AdoptIdempotent** — a vm_id never appears in two `adopt_inventory`
     records within one run.

  4. **HealthMonotonic** — for a node, `age_to_down` is never observed without
     a preceding `age_to_unknown` since the last `reconnect`.

  5. **PrimeBeforeCheckpoint** — every vm_id in a `checkpoint` inventory set
     has a preceding `prime` or `adopt_inventory` in the run.
  """

  alias Embervm.SpecTrace.Store

  @spec invariants() :: list(atom())
  def invariants do
    [
      :no_double_assign,
      :dispatch_provenance,
      :adopt_idempotent,
      :health_monotonic,
      :prime_before_checkpoint,
      :destroy_intent_precedes_record,
      :no_destroy_before_confirm
    ]
  end

  @spec run(module(), GenServer.server(), keyword()) :: [map()]
  # `spec: "adoption"` is a DEFAULT rather than a hardcode: callers may narrow
  # the window (a time range, a run_id) without accidentally widening the spec.
  # Dropping the filter entirely would let these invariants examine
  # bank_relight and quota records, which inflates every coverage count with
  # records the checks then ignore, and a coverage number that counts records
  # it never examined is exactly the overclaim the verdict triple exists to
  # prevent.
  def run(store_mod, store, opts \\ []) do
    opts = Keyword.put_new(opts, :spec, "adoption")

    case store_mod.read_window(store, opts) do
      {:ok, records} ->
        records_by_run = Enum.group_by(records, & &1["run_id"])
        run_ids = Map.keys(records_by_run)

        Enum.flat_map(run_ids, fn run_id ->
          run_records = records_by_run[run_id] |> Enum.sort_by(& &1["mono"])
          Enum.map(invariants(), &check_invariant(&1, run_records))
        end)

      {:error, reason} ->
        [error_verdict(:unknown, reason)]
    end
  end

  defp check_invariant(:no_double_assign, records), do: check_no_double_assign(records)
  defp check_invariant(:dispatch_provenance, records), do: check_dispatch_provenance(records)
  defp check_invariant(:adopt_idempotent, records), do: check_adopt_idempotent(records)
  defp check_invariant(:health_monotonic, records), do: check_health_monotonic(records)
  defp check_invariant(:prime_before_checkpoint, records), do: check_prime_before_checkpoint(records)

  defp check_invariant(:destroy_intent_precedes_record, records),
    do: check_destroy_intent_precedes_record(records)

  defp check_invariant(:no_destroy_before_confirm, records),
    do: check_no_destroy_before_confirm(records)

  defp check_destroy_intent_precedes_record(records) do
    destroy_records = Enum.filter(records, &(&1["action"] in ["begin_destroy", "confirm_destroy"]))
    all_confirms = Enum.filter(destroy_records, &(&1["action"] == "confirm_destroy"))
    missing_had_vm = Enum.filter(all_confirms, &is_nil(&1["vars"]["had_vm"]))
    confirms = Enum.filter(all_confirms, &(&1["vars"]["had_vm"] == true))
    excluded = length(all_confirms) - length(confirms)
    examined_detail = "#{length(all_confirms)} confirmations, #{excluded} snapshot-only, #{length(confirms)} examined"

    cond do
      missing_had_vm != [] ->
        %{invariant: :destroy_intent_precedes_record, verdict: :vacuous, coverage: 0, oracle: :trace_only, detail: "#{examined_detail}; some destroy confirmations lack had_vm"}

      # `confirms`, NOT `records`. Guarding on the run having any records at all
      # meant a trace full of primes, dispatches and checkpoints but containing
      # zero destroys fell through to the violation scan, found nothing to
      # violate, and reported PASS. That is the precise claim this invariant must
      # never make: it would assert the destroy ordering held on a run that never
      # destroyed anything.
      #
      # A begin_destroy with no matching confirm is also not checkable: it is an
      # in-flight destroy, not a violation. So the presence of confirmations is
      # what makes this invariant evaluable, which is why the sibling
      # check_no_destroy_before_confirm guards on the same thing.
      confirms == [] ->
        %{invariant: :destroy_intent_precedes_record, verdict: :vacuous, coverage: 0, oracle: :trace_only, detail: "#{examined_detail}; no live-VM destroy confirmations in trace"}

      true ->
        begins = Enum.filter(destroy_records, &(&1["action"] == "begin_destroy"))
        violations =
          confirms
          |> Enum.filter(fn confirm ->
            session_id = confirm["vars"]["session_id"]
            not Enum.any?(begins, fn begin ->
              begin["vars"]["session_id"] == session_id and begin["mono"] < confirm["mono"]
            end)
          end)

        if violations == [] do
          %{invariant: :destroy_intent_precedes_record, verdict: :pass, coverage: length(confirms), oracle: :trace_only, detail: "#{examined_detail}; destroy intent precedes every destroy record"}
        else
          session_id = hd(violations)["vars"]["session_id"]
          %{invariant: :destroy_intent_precedes_record, verdict: :fail, coverage: length(confirms), oracle: :trace_only, detail: "#{examined_detail}; session_id #{session_id} has destroy confirmation without a preceding intent"}
        end
    end
  end

  defp check_no_destroy_before_confirm(records) do
    all_confirms = Enum.filter(records, &(&1["action"] == "confirm_destroy"))
    missing_had_vm = Enum.filter(all_confirms, &is_nil(&1["vars"]["had_vm"]))
    confirms = Enum.filter(all_confirms, &(&1["vars"]["had_vm"] == true))
    excluded = length(all_confirms) - length(confirms)
    gate_on = Enum.count(confirms, &(&1["vars"]["gate"] == true))
    gate_off = Enum.count(confirms, &(&1["vars"]["gate"] == false))
    examined_detail = "#{length(all_confirms)} confirmations, #{excluded} snapshot-only, #{gate_on} examined, #{gate_off} gate-off"
    confirmed_by_detail = fn ->
      teardown = Enum.count(confirms, &(&1["vars"]["confirmed_by"] == "teardown"))
      absence = Enum.count(confirms, &(&1["vars"]["confirmed_by"] == "absence"))
      "confirmed_by teardown=#{teardown}, absence=#{absence}"
    end

    cond do
      missing_had_vm != [] ->
        %{invariant: :no_destroy_before_confirm, verdict: :vacuous, coverage: 0, oracle: :trace_only, detail: "#{examined_detail}; some destroy confirmations lack had_vm"}

      confirms == [] ->
        %{invariant: :no_destroy_before_confirm, verdict: :vacuous, coverage: 0, oracle: :trace_only, detail: "#{examined_detail}; no live-VM destroy confirmations in trace"}

      # A record whose `gate` is absent or nil is NOT evaluable. Without this arm
      # it satisfies neither the all-false test nor the violation filter, falls
      # through, matches nothing, and returns pass: a missing field reading as
      # "condition not met" rather than "cannot be checked". Every emission site
      # sets `gate` today, so this is unreachable now, and it is exactly the
      # shape of the two false PASSes already fixed on this branch.
      Enum.any?(confirms, &is_nil(&1["vars"]["gate"])) ->
        %{invariant: :no_destroy_before_confirm, verdict: :vacuous, coverage: gate_on, oracle: :trace_only, detail: "#{examined_detail}; some destroy confirmations carry no gate field, so the ordering could not be evaluated"}

      Enum.all?(confirms, &(&1["vars"]["gate"] == false)) ->
        %{invariant: :no_destroy_before_confirm, verdict: :vacuous, coverage: 0, oracle: :trace_only, detail: "#{examined_detail}; all destroy confirmations used the gate-off path"}

      true ->
        violations = Enum.filter(confirms, fn record -> record["vars"]["gate"] == true and record["vars"]["node_confirmed"] != true end)

        if violations == [] do
          %{invariant: :no_destroy_before_confirm, verdict: :pass, coverage: gate_on, oracle: :trace_only, detail: "#{examined_detail}; #{confirmed_by_detail.()} ; all gated destroy confirmations have node confirmation"}
        else
          record = hd(violations)
          vars = record["vars"]
          %{invariant: :no_destroy_before_confirm, verdict: :fail, coverage: gate_on, oracle: :trace_only, detail: "#{examined_detail}; vm_id #{vars["vm_id"]} has node_confirmed #{inspect(vars["node_confirmed"])} with gate #{inspect(vars["gate"])}"}
        end
    end
  end

  defp check_no_double_assign(records) do
    dispatches = Enum.filter(records, &(&1["action"] in ["dispatch_warm", "dispatch_miss"]))

    case dispatches do
      [] ->
        %{
          invariant: :no_double_assign,
          verdict: :vacuous,
          coverage: 0,
          oracle: :trace_only,
          detail: "no dispatches in trace"
        }

      _ ->
        # Group dispatch records by vm_id
        by_vm = Enum.group_by(dispatches, & &1["vars"]["vm_id"])

        violations = Enum.filter(by_vm, fn {_vm_id, dispatch_list} ->
          # Multiple dispatches for the same vm_id is a violation
          length(dispatch_list) > 1
        end)

        if Enum.empty?(violations) do
          %{
            invariant: :no_double_assign,
            verdict: :pass,
            coverage: length(dispatches),
            oracle: :trace_only,
            detail: "no vm_id appears in multiple dispatches"
          }
        else
          offending_vm = violations |> Enum.map(&elem(&1, 0)) |> List.first()

          %{
            invariant: :no_double_assign,
            verdict: :fail,
            coverage: length(dispatches),
            oracle: :trace_only,
            detail: "vm_id #{offending_vm} dispatched multiple times"
          }
        end
    end
  end

  defp check_dispatch_provenance(records) do
    primes = Enum.filter(records, &(&1["action"] == "prime"))
    prime_vm_ids = Enum.map(primes, & &1["vars"]["vm_id"]) |> MapSet.new()

    adopts = Enum.filter(records, &(&1["action"] == "adopt_inventory"))

    adopted_vm_ids =
      Enum.filter(adopts, fn record ->
        vm_ids = record["vars"]["vm_ids"] || []
        is_list(vm_ids) and length(vm_ids) > 0
      end)
      |> Enum.flat_map(& &1["vars"]["vm_ids"])
      |> MapSet.new()

    dispatches = Enum.filter(records, &(&1["action"] in ["dispatch_warm", "dispatch_miss"]))

    case dispatches do
      [] ->
        %{
          invariant: :dispatch_provenance,
          verdict: :vacuous,
          coverage: 0,
          oracle: :trace_only,
          detail: "no dispatches in trace"
        }

      _ ->
        issues =
          Enum.filter(dispatches, fn dispatch ->
            vm_id = dispatch["vars"]["vm_id"]
            provenance = dispatch["vars"]["provenance"]

            # A dispatch is proven if it has provenance: "adopted" OR has a preceding prime or adopt
            not (provenance == "adopted" or MapSet.member?(prime_vm_ids, vm_id) or
                   MapSet.member?(adopted_vm_ids, vm_id))
          end)

        cond do
          Enum.empty?(issues) ->
            %{
              invariant: :dispatch_provenance,
              verdict: :pass,
              coverage: length(dispatches),
              oracle: :trace_only,
              detail: "all dispatches have provenance"
            }

          true ->
            offending = List.first(issues)

            %{
              invariant: :dispatch_provenance,
              verdict: :fail,
              coverage: length(dispatches),
              oracle: :trace_only,
              detail: "vm_id #{offending["vars"]["vm_id"]} dispatched without provenance or prime"
            }
        end
    end
  end

  defp check_adopt_idempotent(records) do
    adopts = Enum.filter(records, &(&1["action"] == "adopt_inventory"))

    case adopts do
      [] ->
        %{
          invariant: :adopt_idempotent,
          verdict: :vacuous,
          coverage: 0,
          oracle: :trace_only,
          detail: "no adopt_inventory records in trace"
        }

      _ ->
        # Group adopt records by vm_id (flattening the vm_ids list)
        adopt_vm_ids = Enum.flat_map(adopts, & &1["vars"]["vm_ids"] || [])

        # Count occurrences of each vm_id across adopts
        by_vm = Enum.reduce(adopt_vm_ids, %{}, fn vm_id, acc ->
          Map.update(acc, vm_id, 1, &(&1 + 1))
        end)

        violations = Enum.filter(by_vm, fn {_vm_id, count} -> count > 1 end)

        if Enum.empty?(violations) do
          %{
            invariant: :adopt_idempotent,
            verdict: :pass,
            coverage: length(adopts),
            oracle: :trace_only,
            detail: "no vm_id appears in multiple adopt_inventory records"
          }
        else
          {offending_vm, _count} = List.first(violations)

          %{
            invariant: :adopt_idempotent,
            verdict: :fail,
            coverage: length(adopts),
            oracle: :trace_only,
            detail: "vm_id #{offending_vm} adopted multiple times"
          }
        end
    end
  end

  defp check_health_monotonic(records) do
    health_records =
      Enum.filter(records, &(&1["action"] in ["age_to_unknown", "age_to_down", "reconnect"]))

    case health_records do
      [] ->
        %{
          invariant: :health_monotonic,
          verdict: :vacuous,
          coverage: 0,
          oracle: :trace_only,
          detail: "no health state transition records in trace"
        }

      _ ->
        # Group by node_id and track state for each node
        # Group the HEALTH records, not every record in the run: grouping all of
        # them buckets dispatches and primes under their node_id too, so a node
        # with no health transitions at all would be examined for one.
        #
        # (Enum.filter_map/3 was removed in Elixir 1.9; this runs 1.18.)
        violations =
          health_records
          |> Enum.group_by(& &1["vars"]["node_id"])
          |> Enum.filter(fn {_node_id, node_records} -> has_health_violation?(node_records) end)
          |> Enum.map(fn {node_id, _records} -> node_id end)

        if Enum.empty?(violations) do
          %{
            invariant: :health_monotonic,
            verdict: :pass,
            coverage: Enum.count(health_records),
            oracle: :trace_only,
            detail: "all nodes maintain health monotonicity"
          }
        else
          offending_node = List.first(violations)

          %{
            invariant: :health_monotonic,
            verdict: :fail,
            coverage: Enum.count(health_records),
            oracle: :trace_only,
            detail: "node #{offending_node} has age_to_down without preceding age_to_unknown"
          }
        end
    end
  end

  # The health machine ages healthy -> unknown -> down, so an age_to_down with no
  # age_to_unknown since the last reconnect is the violation.
  #
  # Two bugs lived here and both made this report violations on LAWFUL traces,
  # which is the worst failure mode for a gate: a checker that cries wolf gets
  # overridden by reflex, and the override rate is ADR 034's kill-point metric.
  #
  #   1. The accumulator was discarded (`fn record, _seen_unknown ->`), so the
  #      age_to_down branch could never consult it and EVERY age_to_down halted
  #      as a violation.
  #   2. When the reduce never halted it returned the accumulator itself, so a
  #      node that had merely gone unknown returned `true`, i.e. "violation",
  #      having done nothing wrong.
  #
  # Halting with a distinct marker keeps "have I seen unknown" and "did I find a
  # violation" from sharing one boolean, which is what allowed both.
  defp has_health_violation?(node_records) do
    node_records
    |> Enum.reduce_while(false, fn record, seen_unknown ->
      case record["action"] do
        # A reconnect starts a fresh incarnation of the health machine.
        "reconnect" -> {:cont, false}
        "age_to_unknown" -> {:cont, true}
        "age_to_down" -> if seen_unknown, do: {:cont, seen_unknown}, else: {:halt, :violation}
        _ -> {:cont, seen_unknown}
      end
    end)
    |> case do
      :violation -> true
      _ -> false
    end
  end

  defp check_prime_before_checkpoint(records) do
    primes = Enum.filter(records, &(&1["action"] == "prime"))
    prime_vm_ids = Enum.map(primes, & &1["vars"]["vm_id"]) |> MapSet.new()

    adopts = Enum.filter(records, &(&1["action"] == "adopt_inventory"))

    adopted_vm_ids =
      Enum.filter(adopts, fn record ->
        vm_ids = record["vars"]["vm_ids"] || []
        is_list(vm_ids) and length(vm_ids) > 0
      end)
      |> Enum.flat_map(& &1["vars"]["vm_ids"])
      |> MapSet.new()

    checkpoints = Enum.filter(records, &(&1["action"] == "checkpoint"))

    case checkpoints do
      [] ->
        %{
          invariant: :prime_before_checkpoint,
          verdict: :vacuous,
          coverage: 0,
          oracle: :trace_only,
          detail: "no checkpoint records in trace"
        }

      _ ->
        parsed = Enum.map(checkpoints, &checkpoint_vm_ids/1)

        # An UNPARSEABLE checkpoint must never reach a verdict of pass.
        #
        # This shipped inert. The dispatcher emits node_workload_vm_ids as a MAP
        # of "node:workload" => [vm_id], but the reader mapped over it expecting
        # a list of [node, workload, vm_id] triples. Enum.map over a map yields
        # {key, value} TUPLES, which matched neither clause, so every entry fell
        # to the nil fallback, was filtered out, and left an empty set. Enum.any?
        # over an empty set is false, so the invariant reported PASS having
        # examined zero vm_ids, on every production trace.
        #
        # The fixtures used the triple shape and passed, positive and negative
        # alike. Only production disagreed, and silently. So a checkpoint that
        # declares inventory we cannot read is now VACUOUS with the reason
        # stated: we did not check it, which is the one thing the old code could
        # not say.
        unreadable = Enum.filter(parsed, fn {declared, vm_ids} -> declared > 0 and vm_ids == [] end)

        cond do
          unreadable != [] ->
            %{
              invariant: :prime_before_checkpoint,
              verdict: :vacuous,
              coverage: length(checkpoints),
              oracle: :trace_only,
              detail:
                "#{length(unreadable)} of #{length(checkpoints)} checkpoints declared inventory in an unreadable shape, so nothing was checked"
            }

          true ->
            issues =
              Enum.filter(parsed, fn {_declared, vm_ids} ->
                Enum.any?(vm_ids, fn vm_id ->
                  not (MapSet.member?(prime_vm_ids, vm_id) or MapSet.member?(adopted_vm_ids, vm_id))
                end)
              end)

            prime_before_checkpoint_verdict(issues, checkpoints)
        end
    end
  end

  # Returns {entries_declared, vm_ids}. The declared count is what makes an
  # unreadable shape distinguishable from a genuinely empty inventory: both yield
  # no vm_ids, and only one of them is a checker bug.
  defp checkpoint_vm_ids(checkpoint) do
    raw = checkpoint["vars"]["node_workload_vm_ids"] || []

    vm_ids =
      raw
      |> Enum.flat_map(fn entry ->
        case entry do
          # Production shape: %{"node:workload" => [vm_id, ...]}, so iterating the
          # map hands back {key, list} tuples.
          {_node_workload, vm_ids} when is_list(vm_ids) -> vm_ids
          # Tolerated shapes, kept so an older segment still reads.
          [_node, _workload, vm_id] -> [vm_id]
          %{"vm_id" => vm_id} -> [vm_id]
          _ -> []
        end
      end)
      |> Enum.filter(&is_binary/1)

    {Enum.count(raw), vm_ids}
  end

  defp prime_before_checkpoint_verdict(issues, checkpoints) do
    cond do
      Enum.empty?(issues) ->
        %{
          invariant: :prime_before_checkpoint,
          verdict: :pass,
          coverage: length(checkpoints),
          oracle: :trace_only,
          detail: "all checkpoint vm_ids have preceding prime or adopt"
        }

      true ->
        %{
          invariant: :prime_before_checkpoint,
          verdict: :fail,
          coverage: length(checkpoints),
          oracle: :trace_only,
          detail: "some checkpoint vm_ids lack preceding prime or adopt"
        }
    end
  end

  defp error_verdict(invariant, reason) do
    %{
      invariant: invariant,
      verdict: :fail,
      coverage: 0,
      oracle: :trace_only,
      detail: "store error: #{inspect(reason)}"
    }
  end
end
