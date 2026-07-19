defmodule Embervm.ApplicationTest do
  @moduledoc """
  Boot-ordering regression for the noded node-list seed (artifact-decoupling
  PR-C, C4). `Embervm.Application.configured_nodes/0` seeds BaseBuilder /
  NodeRegistry / NodeChannel at children-CONSTRUCTION time, which runs BEFORE any
  supervised child (including Finch) is started. It therefore MUST NOT touch Finch
  / the K8s API: doing so raised `(ArgumentError) unknown registry: Embervm.Finch`
  and crash-looped the whole control plane in prod. These tests pin the contract:
  under discovery config, the seed is EMPTY and Finch-free (post-Finch discovery
  populates the fleet); the explicit-address override still seeds statically.

  Finch is NOT started in ExUnit, so if configured_nodes/0 called it these tests
  would RAISE rather than return, which is exactly the regression they guard.
  """
  use ExUnit.Case, async: false

  alias Embervm.Application, as: App

  setup do
    # Snapshot and restore the env vars these tests toggle, so they never leak.
    saved =
      for k <- ~w(EMBERVM_NODED_SERVICE EMBERVM_NODE_ADDRESS EMBERVM_NODE_ID EMBERVM_NODED_GRPC_PORT) do
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

  test "configured_nodes/0 seeds EMPTY (and does not touch Finch) under discovery config" do
    # Discovery is the active source: a headless Service name, no pinned override.
    System.put_env("EMBERVM_NODED_SERVICE", "embervm-embervm-noded")
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
end
