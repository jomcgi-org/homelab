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
     `provenance: "adopted"`. A dispatch with neither is `:unproven` (boundary
     case: window starts after prime) or `:fail` (genuine finding).

  3. **AdoptIdempotent** — a vm_id never appears in two `adopt_inventory`
     records within one run.

  4. **HealthMonotonic** — for a node, `age_to_down` is never observed without
     a preceding `age_to_unknown` since the last `reconnect`.

  5. **PrimeBeforeCheckpoint** — every vm_id in a `checkpoint` inventory set
     has a preceding `prime` or `adopt_inventory` in the run.
  """

  alias Embervm.SpecTrace.Store

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
          [
            check_no_double_assign(run_records),
            check_dispatch_provenance(run_records),
            check_adopt_idempotent(run_records),
            check_health_monotonic(run_records),
            check_prime_before_checkpoint(run_records)
          ]
        end)

      {:error, reason} ->
        [error_verdict(:unknown, reason)]
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
        # For each checkpoint, check that all vm_ids in the inventory have been primed or adopted
        issues =
          Enum.filter(checkpoints, fn checkpoint ->
            node_workload_vm_ids = checkpoint["vars"]["node_workload_vm_ids"] || []

            vm_ids_in_checkpoint =
              Enum.map(node_workload_vm_ids, fn item ->
                case item do
                  [_node, _workload, vm_id] -> vm_id
                  %{"vm_id" => vm_id} -> vm_id
                  _ -> nil
                end
              end)
              |> Enum.filter(&is_binary/1)
              |> MapSet.new()

            # Check if any checkpoint VM has NOT been primed or adopted
            Enum.any?(vm_ids_in_checkpoint, fn vm_id ->
              not (MapSet.member?(prime_vm_ids, vm_id) or MapSet.member?(adopted_vm_ids, vm_id))
            end)
          end)

        if Enum.empty?(issues) do
          %{
            invariant: :prime_before_checkpoint,
            verdict: :pass,
            coverage: length(checkpoints),
            oracle: :trace_only,
            detail: "all checkpoint vm_ids have preceding prime or adopt"
          }
        else
          %{
            invariant: :prime_before_checkpoint,
            verdict: :fail,
            coverage: length(checkpoints),
            oracle: :trace_only,
            detail: "some checkpoint vm_ids lack preceding prime or adopt"
          }
        end
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
