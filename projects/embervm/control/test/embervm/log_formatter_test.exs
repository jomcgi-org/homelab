defmodule Embervm.LogFormatterTest do
  use ExUnit.Case, async: true

  alias Embervm.CapacityObserver

  test "preserves every CapacityObserver record field in structured JSON" do
    reservation_table =
      String.to_atom("log_formatter_reservation_#{System.unique_integer([:positive])}")

    record =
      CapacityObserver.build_record(
        %{
          instance_id: "node-1/pod-a",
          node_id: "node-1",
          size_class: "2gi",
          mem_budget_mib: 1_536,
          mem_headroom_mib: 1_111,
          mem_reserved_mib: 0,
          admits_on_reservation: false,
          live_vms: 0,
          max_live_vms: 8
        },
        reservation_table
      )

    line =
      Embervm.LogFormatter.format(
        # :logger metadata is a MAP, not a keyword list. Passing a list makes
        # whitelisted_meta/1 raise, and the formatter's rescue then emits its
        # plain-text fallback, so the assertion failure points at the JSON
        # decoder rather than at the real cause.
        %{level: :info, msg: {:string, "embervm capacity brick"}, meta: record},
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    for key <- Map.keys(record) do
      assert Map.has_key?(decoded, Atom.to_string(key)),
             "record field #{inspect(key)} was dropped from structured JSON"
    end

    assert Map.has_key?(decoded, Atom.to_string(:guest_free?))
  end

  test "preserves every retention manifest field in structured JSON" do
    metadata = [
      node_id: "node-1",
      path: "/var/lib/embervm/scratch/bases/ref-1",
      size_bytes: 42,
      workload: "claude-runtime",
      vendor: "intel",
      age_seconds: 72_000,
      reason_unreferenced: "known workload superseded: not in current, CP snapshot, or active base_refs",
      base_generation: 17
    ]

    line =
      Embervm.LogFormatter.format(
        %{level: :info, msg: {:string, "embervm base retention candidate"}, meta: Map.new(metadata)},
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    for {key, value} <- metadata do
      assert Map.get(decoded, Atom.to_string(key)) == value
    end
  end
end
