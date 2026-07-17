defmodule Embervm.GroupManagerTest do
  @moduledoc """
  Exercises Embervm.GroupManager (the per-instance composite create brain) against a
  real GroupStore + op-log, an injected CreateGroupNetwork/StartGroupMember seam
  (a recording fake), and a FakePublisher. Covers: the ordered create round-trip
  (network up -> role-ordered health-gated member starts -> entry published ->
  running), the ordered-start PROPERTY (order N never starts before all order N-1
  are up, asserted from the recorded call order), create-failure teardown (a member
  start failure tears the group to failed AND deletes the network), the EMBER_GROUP_*
  env compose (member/role/ip/peer-map/secret keys, casing + .10+i addressing), and
  secret sourcing (secretRef read stable vs minted).
  """
  use ExUnit.Case, async: true

  alias Embervm.{GroupManager, GroupStore}
  alias Embervm.OpLog.SQLite

  alias Embervm.Node.V1.{CreateGroupNetworkResponse, StartGroupMemberResponse}

  defmodule FakePublisher do
    use GenServer
    def start_link, do: GenServer.start_link(__MODULE__, 0)
    def count(pid), do: GenServer.call(pid, :count)
    @impl true
    def init(n), do: {:ok, n}
    @impl true
    def handle_cast(:publish, n), do: {:noreply, n + 1}
    @impl true
    def handle_call(:count, _from, n), do: {:reply, n, n}
  end

  # A recording seam: every CreateGroupNetwork / StartGroupMember / DeleteGroupNetwork
  # call is appended (with a monotonic sequence + the request) to an Agent so a test
  # can assert the ORDER and content. StartGroupMember can be made to fail for a named
  # member.
  defp recorder, do: Agent.start_link(fn -> [] end)
  defp record(agent, event), do: Agent.update(agent, &(&1 ++ [event]))
  defp events(agent), do: Agent.get(agent, & &1)

  defp start_group(opts \\ []) do
    suffix = System.unique_integer([:positive])
    path = Path.join(System.tmp_dir!(), "embervm_groupmgr_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = GroupStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)
    {:ok, pub} = FakePublisher.start_link()
    {:ok, rec} = recorder()

    fail_member = Keyword.get(opts, :fail_member)

    create_group_network_fun = fn _ch, req ->
      record(rec, {:create_network, req.group_instance_id, req.cidr})
      {:ok, %CreateGroupNetworkResponse{bridge_name: "emg123", gateway_ip: "10.101.0.1"}}
    end

    delete_group_network_fun = fn _ch, req ->
      record(rec, {:delete_network, req.group_instance_id})
      {:ok, %Embervm.Node.V1.DeleteGroupNetworkResponse{}}
    end

    start_group_member_fun = fn _ch, req ->
      record(rec, {:start_member, req.member_name, req.member_index, req.ip, req.env})

      if req.member_name == fail_member do
        {:error, :boom}
      else
        {:ok, %StartGroupMemberResponse{vm_id: "vm-#{req.member_name}", ip: req.ip, was_relight: false}}
      end
    end

    {:ok, secret_reads} = Agent.start_link(fn -> [] end)

    get_secret_fun =
      Keyword.get(opts, :get_secret_fun, fn ns, name ->
        Agent.update(secret_reads, &[{ns, name} | &1])
        {:ok, %{"token" => "from-k8s"}}
      end)

    entry = Keyword.get(opts, :entry, default_entry())

    mgr_opts = [
      instance_id: "g-1",
      workload: "grp-a",
      principal: "system:group:grp-a",
      entry: entry,
      node_id: "node-4",
      store: store,
      publisher: pub,
      supernet: "10.101.0.0/16",
      port_base: 30_000,
      pod_ip: "10.0.0.9",
      channel_fun: fn _node -> {:ok, :ch} end,
      create_group_network_fun: create_group_network_fun,
      delete_group_network_fun: delete_group_network_fun,
      start_group_member_fun: start_group_member_fun,
      get_secret_fun: get_secret_fun,
      secret_fun: Keyword.get(opts, :secret_fun, fn -> "minted-secret" end),
      clock: fn -> 1_000 end
    ]

    {:ok, mgr} = GroupManager.start_link(mgr_opts)

    %{mgr: mgr, store: store, pub: pub, rec: rec, op_log: op_log, secret_reads: secret_reads}
  end

  defp default_entry do
    %{
      name: "grp-a",
      namespace: "embervm-workloads",
      class: "composite",
      group: %{
        entry: %{member: "leader", port: 8080, listen_port: 5410},
        secret_ref: nil,
        members: [
          %{name: "leader", role: "leader", start_order: 0, replicas: 1, image_ref: "img-l", health_port: 9000, env: %{"FOO" => "bar"}},
          %{name: "worker", role: "worker", start_order: 1, replicas: 2, image_ref: "img-w", health_port: 9001, env: %{}}
        ]
      }
    }
  end

  test "the ordered create round-trip: network up -> members -> published -> running" do
    ctx = start_group()

    assert {:ok, endpoint} = GroupManager.create_group(ctx.mgr)
    # The published entry endpoint is {pod IP, vmPort}: vmPort = 30000 + hostOffset of
    # the entry member's IP (.10 in the group /24). For 10.101.0.10 in 10.101.0.0/16
    # the offset is (0 << 8) | 10 = 10, so vmPort = 30010.
    assert endpoint == %{ip: "10.0.0.9", port: 30_010}

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :running
    assert inst.entry_ip == "10.0.0.9"
    assert inst.entry_port_published == 30_010

    # Three expanded members recorded (leader, worker-0, worker-1).
    members = GroupStore.members(ctx.store, "g-1")
    assert Enum.map(members, & &1.member_name) |> Enum.sort() == ["leader", "worker-0", "worker-1"]
    assert Enum.all?(members, & &1.healthy)

    assert FakePublisher.count(ctx.pub) >= 1
  end

  test "ordered-start property: order N never starts before every order N-1 member" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    starts =
      events(ctx.rec)
      |> Enum.filter(&match?({:start_member, _, _, _, _}, &1))
      |> Enum.map(fn {:start_member, name, _idx, _ip, _env} -> name end)

    # leader (startOrder 0) must come before BOTH worker replicas (startOrder 1).
    leader_pos = Enum.find_index(starts, &(&1 == "leader"))
    w0_pos = Enum.find_index(starts, &(&1 == "worker-0"))
    w1_pos = Enum.find_index(starts, &(&1 == "worker-1"))

    assert leader_pos < w0_pos
    assert leader_pos < w1_pos

    # The network is created before any member starts.
    assert [{:create_network, "g-1", "10.101.0.0/24"} | _] = events(ctx.rec)
  end

  test "create-failure teardown: a member start failure tears the group to failed AND deletes the network" do
    ctx = start_group(fail_member: "worker-0")

    assert {:error, _reason} = GroupManager.create_group(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :failed

    # The group network was torn down (no half-group leaks).
    assert Enum.any?(events(ctx.rec), &match?({:delete_network, "g-1"}, &1))
  end

  test "EMBER_GROUP_* env: member/role/ip/peer-map/secret keys, casing + .10+i addressing" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    # The env the WORKER-0 member was started with.
    {:start_member, "worker-0", _idx, _ip, env} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "worker-0", _, _, _}, &1))

    # Member identity.
    assert env["EMBER_GROUP_MEMBER"] == "worker-0"
    assert env["EMBER_GROUP_ROLE"] == "worker"
    # worker-0 is the 2nd expanded member (index 1) -> .10 + 1 = .11.
    assert env["EMBER_GROUP_IP"] == "10.101.0.11"

    # The peer map: every member's IP, keyed UPPERCASE with - -> _.
    assert env["EMBER_PEER_LEADER"] == "10.101.0.10"
    assert env["EMBER_PEER_WORKER_0"] == "10.101.0.11"
    assert env["EMBER_PEER_WORKER_1"] == "10.101.0.12"

    # The minted group secret (no secretRef in the default entry).
    assert env["EMBER_GROUP_SECRET"] == "minted-secret"

    # The leader carries its declared env untouched too.
    {:start_member, "leader", _, _, leader_env} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "leader", _, _, _}, &1))

    assert leader_env["FOO"] == "bar"
    assert leader_env["EMBER_GROUP_IP"] == "10.101.0.10"
  end

  test "secret sourcing: with secretRef the K8s key is read (stable), recorded in the create op" do
    entry = %{
      name: "grp-a",
      namespace: "embervm-workloads",
      class: "composite",
      group: %{
        entry: %{member: "leader", port: 8080, listen_port: 5410},
        secret_ref: %{name: "grp-secret", key: "token"},
        members: [%{name: "leader", role: "leader", start_order: 0, replicas: 1, image_ref: "img-l", health_port: 9000, env: %{}}]
      }
    }

    ctx = start_group(entry: entry)
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    {:start_member, "leader", _, _, env} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "leader", _, _, _}, &1))

    # The secret came from the referenced K8s Secret's key, not a mint.
    assert env["EMBER_GROUP_SECRET"] == "from-k8s"
    # The secretRef was read at create.
    assert Agent.get(ctx.secret_reads, & &1) == [{"embervm-workloads", "grp-secret"}]
  end

  test "secret sourcing: without secretRef, a secret is MINTED at create" do
    ctx = start_group(secret_fun: fn -> "fixed-mint" end)
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    {:start_member, "leader", _, _, env} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "leader", _, _, _}, &1))

    assert env["EMBER_GROUP_SECRET"] == "fixed-mint"
    # No K8s read happened.
    assert Agent.get(ctx.secret_reads, & &1) == []
  end
end
