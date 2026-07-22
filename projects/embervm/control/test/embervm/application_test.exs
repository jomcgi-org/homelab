defmodule Embervm.ApplicationTest do
  @moduledoc """
  Boot-ordering regression for the noded node-list seed. Under dial-home
  registration (R0 PR-2) `Embervm.Application.configured_nodes/0` seeds
  BaseBuilder / NodeRegistry / NodeChannel at children-CONSTRUCTION time, which
  runs BEFORE any supervised child (including Finch) is started. It therefore MUST
  NOT touch Finch / the K8s API: doing so raised `(ArgumentError) unknown
  registry: Embervm.Finch` and crash-looped the whole control plane in prod. These
  tests pin the contract: without a pinned override the seed is EMPTY and
  Finch-free (dial-home registration populates the fleet post-Finch); the
  explicit-address override still seeds statically.

  Finch is NOT started in ExUnit, so if configured_nodes/0 called it these tests
  would RAISE rather than return, which is exactly the regression they guard.
  """
  use ExUnit.Case, async: false

  alias Embervm.Application, as: App

  setup do
    # Snapshot and restore the env vars these tests toggle, so they never leak.
    saved =
      for k <- ~w(EMBERVM_NODE_ADDRESS EMBERVM_NODE_ID EMBERVM_OPLOG_DSN) do
        {k, System.get_env(k)}
      end

    on_exit(fn ->
      for {k, v} <- saved do
        if v, do: System.put_env(k, v), else: System.delete_env(k)
      end
    end)

    for {k, _} <- saved, do: System.delete_env(k)
    :ok
  end

  test "configured_nodes/0 seeds EMPTY (and does not touch Finch) with no pinned override" do
    # No pinned override: the fleet arrives via dial-home registration post-Finch.
    System.delete_env("EMBERVM_NODE_ADDRESS")

    # Must NOT raise (a Finch call here would raise "unknown registry:
    # Embervm.Finch" because Finch is not started in ExUnit and, in prod, not yet
    # started at construction time). Must return the empty seed.
    assert App.configured_nodes() == []
  end

  test "configured_nodes/0 seeds the static pinned node when EMBERVM_NODE_ADDRESS is set" do
    System.put_env("EMBERVM_NODE_ADDRESS", "node-4.test:9090")
    System.put_env("EMBERVM_NODE_ID", "node-4")
    # An address override wins over discovery and needs no Finch.
    assert App.configured_nodes() == [%{id: "node-4", address: "node-4.test:9090"}]
  end

  test "configured_nodes/0 seeds EMPTY when neither override nor service is configured" do
    # No discovery, no override: an idle control plane, empty node set, no Finch.
    assert App.configured_nodes() == []
  end

  # op_log_mod/0 selection (PR-4, #18/#27): EMBERVM_OPLOG_DSN unset or empty
  # keeps every cluster on Embervm.OpLog.SQLite (today's shipped default); a
  # set, non-empty DSN selects Embervm.OpLog.Postgres. This PR does not wire
  # the DSN into any deploy values, so the selection stays SQLite in prod
  # until a later cutover PR sets it.
  test "op_log_mod/0 selects Embervm.OpLog.SQLite when EMBERVM_OPLOG_DSN is unset" do
    System.delete_env("EMBERVM_OPLOG_DSN")
    assert App.op_log_mod() == Embervm.OpLog.SQLite
  end

  test "op_log_mod/0 selects Embervm.OpLog.SQLite when EMBERVM_OPLOG_DSN is empty" do
    System.put_env("EMBERVM_OPLOG_DSN", "")
    assert App.op_log_mod() == Embervm.OpLog.SQLite
  end

  test "op_log_mod/0 selects Embervm.OpLog.Postgres when EMBERVM_OPLOG_DSN is set" do
    System.put_env("EMBERVM_OPLOG_DSN", "postgres://embervm:pw@embervm-pg:5432/embervm")
    assert App.op_log_mod() == Embervm.OpLog.Postgres
  end
end
