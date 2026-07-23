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
    # A member whose start returns a transport-dead error (a wrapped "connection is
    # closed", the shape a noded rollout surfaces), for the channel-invalidation test.
    transport_dead_member = Keyword.get(opts, :transport_dead_member)
    # A member whose RELIGHT StartGroupMember fails (Task 7): forces the all-or-
    # nothing relight -> fresh fallback. relight_unverified_member instead returns a
    # was_relight=false response (the clock-resync casualty, decision 7).
    relight_fail_member = Keyword.get(opts, :relight_fail_member)
    relight_unverified_member = Keyword.get(opts, :relight_unverified_member)
    # A scripted daemon endpoint projection {ip, port} the fake attaches to the
    # ENTRY member's response (the F-fix lane: the CP must publish the DAEMON's
    # reported endpoint, not its own pod IP). nil models a pre-R6/DNAT-disabled
    # daemon that reports none.
    daemon_endpoint = Keyword.get(opts, :daemon_endpoint)

    create_group_network_fun = fn _ch, req ->
      record(rec, {:create_network, req.group_instance_id, req.cidr})
      {:ok, %CreateGroupNetworkResponse{bridge_name: "emg123", gateway_ip: "10.101.0.1"}}
    end

    delete_group_network_fun = fn _ch, req ->
      record(rec, {:delete_network, req.group_instance_id})
      {:ok, %Embervm.Node.V1.DeleteGroupNetworkResponse{}}
    end

    # Models noded's pinned-IP allocator: reserve a member's IP on start, REJECT a
    # double-reserve (:ip_in_use), release it on StopGroupMember DESTROY. So a fresh
    # re-pin over a member whose relight-resumed VM was NOT destroyed first FAILS on
    # its IP, exactly as the real EnsureMemberTap/alloc.reserve would. `vm_id -> ip`
    # tracks live members so a DESTROY (by vm_id) frees the right IP.
    {:ok, alloc} = Agent.start_link(fn -> %{reserved: MapSet.new(), by_vm: %{}} end)

    reserve = fn ip, vm_id ->
      Agent.get_and_update(alloc, fn s ->
        if MapSet.member?(s.reserved, ip) do
          {:error, s}
        else
          {:ok, %{reserved: MapSet.put(s.reserved, ip), by_vm: Map.put(s.by_vm, vm_id, ip)}}
        end
      end)
    end

    release = fn vm_id ->
      Agent.update(alloc, fn s ->
        case Map.get(s.by_vm, vm_id) do
          nil -> s
          ip -> %{reserved: MapSet.delete(s.reserved, ip), by_vm: Map.delete(s.by_vm, vm_id)}
        end
      end)
    end

    start_group_member_fun = fn _ch, req ->
      relight? = req.mode == :START_GROUP_MEMBER_MODE_RELIGHT
      record(rec, {:start_member, req.member_name, req.member_index, req.ip, req.env, req.mode})
      # Additive record of the entry-DNAT marker (invisible to the :start_member
      # assertions, which all filter/find by that tag) so a test can assert only the
      # entry member carries a non-zero entry_guest_port.
      record(rec, {:start_member_entry, req.member_name, req.entry_guest_port})
      # Additive record of the forwarded readiness budget (the G-fix lane: the
      # daemon gate must share the workload's wake policy).
      record(rec, {:start_member_budget, req.member_name, req.ready_budget_seconds})
      vm_id = "vm-#{req.member_name}"

      cond do
        req.member_name == transport_dead_member ->
          {:error, %GRPC.RPCError{status: 2, message: "the connection is closed"}}

        req.member_name == fail_member ->
          {:error, :boom}

        relight? and req.member_name == relight_fail_member ->
          {:error, :relight_boom}

        relight? and req.member_name == relight_unverified_member ->
          # A resume the daemon could not verify (clock-resync out of bounds):
          # was_relight=false on a RELIGHT-mode response. It still RESERVED the IP (the
          # VM did resume before verification failed), so a later fresh re-pin collides
          # unless it is destroyed first.
          case reserve.(req.ip, vm_id) do
            :ok -> {:ok, %StartGroupMemberResponse{vm_id: vm_id, ip: req.ip, was_relight: false}}
            :error -> {:error, :ip_in_use}
          end

        true ->
          case reserve.(req.ip, vm_id) do
            :ok ->
              resp = %StartGroupMemberResponse{vm_id: vm_id, ip: req.ip, was_relight: relight?}

              resp =
                case {daemon_endpoint, req.entry_guest_port} do
                  {{ep_ip, ep_port}, p} when is_integer(p) and p > 0 ->
                    %{resp | endpoint_ip: ep_ip, endpoint_port: ep_port}

                  _ ->
                    resp
                end

              {:ok, resp}

            :error ->
              {:error, :ip_in_use}
          end
      end
    end

    bank_fail_member = Keyword.get(opts, :bank_fail_member)

    stop_group_member_fun = fn _ch, req ->
      record(rec, {:stop_member, req.vm_id, req.mode, req.set_id, req.member_name})
      if req.mode == :STOP_GROUP_MEMBER_MODE_DESTROY, do: release.(req.vm_id)

      cond do
        req.mode == :STOP_GROUP_MEMBER_MODE_BANK and req.member_name == bank_fail_member ->
          {:error, :bank_boom}

        req.mode == :STOP_GROUP_MEMBER_MODE_BANK ->
          # BANK pauses+snapshots+destroys, returning the per-member bundle ref written
          # under group/<set_id>/<member>/.
          release.(req.vm_id)
          {:ok, %Embervm.Node.V1.StopGroupMemberResponse{snapshot_ref: "snap-#{req.set_id}-#{req.member_name}", size_bytes: 4_096}}

        true ->
          {:ok,
           %Embervm.Node.V1.StopGroupMemberResponse{
             teardown_confirmed: Keyword.get(opts, :destroy_confirmed, true)
           }}
      end
    end

    evict_snapshot_fun = fn _ch, req ->
      record(rec, {:evict_snapshot, req.snapshot_ref})
      {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
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
      invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _n, _c -> :ok end),
      create_group_network_fun: create_group_network_fun,
      delete_group_network_fun: delete_group_network_fun,
      start_group_member_fun: start_group_member_fun,
      stop_group_member_fun: stop_group_member_fun,
      evict_snapshot_fun: evict_snapshot_fun,
      get_secret_fun: get_secret_fun,
      secret_fun: Keyword.get(opts, :secret_fun, fn -> "minted-secret" end),
      node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
      clock: fn -> 1_000 end
    ]

    {:ok, mgr} = GroupManager.start_link(mgr_opts)

    %{mgr: mgr, store: store, pub: pub, rec: rec, op_log: op_log, secret_reads: secret_reads, alloc: alloc}
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

  test "only the entry member carries a non-zero entry_guest_port (installs the entry DNAT)" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    entry_ports =
      events(ctx.rec)
      |> Enum.filter(&match?({:start_member_entry, _, _}, &1))
      |> Map.new(fn {:start_member_entry, name, port} -> {name, port} end)

    # The entry member ("leader") gets the workload entry port; every other member 0.
    assert entry_ports["leader"] == 8080
    assert entry_ports["worker-0"] == 0
    assert entry_ports["worker-1"] == 0
  end

  test "create publishes the DAEMON-reported entry endpoint over the CP-local derivation" do
    # The daemon reports its own projection ({noded pod IP, vmPort}): that is where
    # the entry DNAT lives, so THAT is what must be published. The CP-local
    # {pod_ip: 10.0.0.9, 30010} derivation (asserted by the round-trip test above
    # when no daemon endpoint is reported) is only the fallback.
    ctx = start_group(daemon_endpoint: {"10.42.1.95", 36_443})

    assert {:ok, endpoint} = GroupManager.create_group(ctx.mgr)
    assert endpoint == %{ip: "10.42.1.95", port: 36_443}

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.entry_ip == "10.42.1.95"
    assert inst.entry_port_published == 36_443
  end

  test "the workload wake budget is forwarded as every member's ready budget" do
    entry = default_entry()
    entry = %{entry | group: Map.put(entry.group, :wake_timeout_seconds, 180)}
    ctx = start_group(entry: entry)
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    budgets =
      events(ctx.rec)
      |> Enum.filter(&match?({:start_member_budget, _, _}, &1))
      |> Map.new(fn {:start_member_budget, name, secs} -> {name, secs} end)

    assert budgets == %{"leader" => 180, "worker-0" => 180, "worker-1" => 180}
  end

  test "no wake budget in the catalog forwards 0 (daemon default)" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    budgets =
      events(ctx.rec)
      |> Enum.filter(&match?({:start_member_budget, _, _}, &1))
      |> Enum.map(fn {:start_member_budget, _, secs} -> secs end)

    assert budgets == [0, 0, 0]
  end

  test "ordered-start property: order N never starts before every order N-1 member" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    starts =
      events(ctx.rec)
      |> Enum.filter(&match?({:start_member, _, _, _, _, _}, &1))
      |> Enum.map(fn {:start_member, name, _idx, _ip, _env, _mode} -> name end)

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

  test "a transport-dead member start invalidates the shared channel so the next wake re-dials" do
    test_pid = self()

    ctx =
      start_group(
        transport_dead_member: "worker-0",
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end
      )

    assert {:error, _reason} = GroupManager.create_group(ctx.mgr)

    # The dead shared channel was dropped (node "node-4", channel :ch), so the next
    # safe_channel/2 re-dials instead of every member start wedging on a dead
    # ConnectionProcess until the control plane restarts (D-R2.7.2).
    assert_receive {:invalidated, "node-4", :ch}, 1_000
  end

  test "a server-status member start failure leaves the shared channel up (no needless invalidate)" do
    test_pid = self()

    ctx =
      start_group(
        fail_member: "worker-0",
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end
      )

    assert {:error, _reason} = GroupManager.create_group(ctx.mgr)

    # :boom is a plain application error that rode a HEALTHY channel; it must NOT tear
    # the shared channel down (D-R2.7.2), else a legit member rejection would needlessly
    # redial for every other consumer of the node.
    refute_receive {:invalidated, _, _}, 300
  end

  test "EMBER_GROUP_* env: member/role/ip/peer-map/secret keys, casing + .10+i addressing" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    # The env the WORKER-0 member was started with.
    {:start_member, "worker-0", _idx, _ip, env, _mode} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "worker-0", _, _, _, _}, &1))

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
    {:start_member, "leader", _, _, leader_env, _mode} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "leader", _, _, _, _}, &1))

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

    {:start_member, "leader", _, _, env, _mode} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "leader", _, _, _, _}, &1))

    # The secret came from the referenced K8s Secret's key, not a mint.
    assert env["EMBER_GROUP_SECRET"] == "from-k8s"
    # The secretRef was read at create.
    assert Agent.get(ctx.secret_reads, & &1) == [{"embervm-workloads", "grp-secret"}]
  end

  test "secret sourcing: without secretRef, a secret is MINTED at create" do
    ctx = start_group(secret_fun: fn -> "fixed-mint" end)
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    {:start_member, "leader", _, _, env, _mode} =
      events(ctx.rec) |> Enum.find(&match?({:start_member, "leader", _, _, _, _}, &1))

    assert env["EMBER_GROUP_SECRET"] == "fixed-mint"
    # No K8s read happened.
    assert Agent.get(ctx.secret_reads, & &1) == []
  end

  # -- wake (relight / fresh fallback), R5 Task 7 ----------------------------

  # Drive the created group to `banked` with a complete set (a snapshot_ref for every
  # expanded member), so a wake_group relights it. A real bank (StopGroupMember BANK)
  # snapshots then tears down every live VM, freeing its pinned tap+IP; the store-level
  # bank_ready here bypasses the RPC, so free the allocator model to match (otherwise a
  # relight would spuriously collide on the still-reserved create-time IPs).
  defp bank_complete(ctx) do
    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)

    members = [
      %{name: "leader", snapshot_ref: "snap-leader"},
      %{name: "worker-0", snapshot_ref: "snap-worker-0"},
      %{name: "worker-1", snapshot_ref: "snap-worker-1"}
    ]

    {:ok, _} = GroupStore.bank_ready(ctx.store, "g-1", "set-1", members)
    Agent.update(ctx.alloc, fn _ -> %{reserved: MapSet.new(), by_vm: %{}} end)
    :ok
  end

  defp relight_events(ctx) do
    events(ctx.rec)
    |> Enum.filter(&match?({:start_member, _, _, _, _, :START_GROUP_MEMBER_MODE_RELIGHT}, &1))
    |> Enum.map(fn {:start_member, name, _idx, _ip, _env, _mode} -> name end)
  end

  test "wake_group relights a complete banked set in role order and republishes the entry" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)
    :ok = bank_complete(ctx)

    assert {:ok, endpoint, :relit} = GroupManager.wake_group(ctx.mgr)
    assert endpoint == %{ip: "10.0.0.9", port: 30_010}

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :running
    assert inst.entry_port_published == 30_010

    # Every member was RELIGHT-resumed (not fresh), in role order (leader before both
    # workers), and the network was re-issued before the resumes.
    relit = relight_events(ctx)
    assert Enum.sort(relit) == ["leader", "worker-0", "worker-1"]
    assert Enum.find_index(relit, &(&1 == "leader")) < Enum.find_index(relit, &(&1 == "worker-0"))

    # The network was re-created on the wake (the bridge dies with the noded pod).
    creates = Enum.count(events(ctx.rec), &match?({:create_network, "g-1", _}, &1))
    assert creates >= 2
  end

  test "wake_group falls back to a FRESH boot (same call) when a member relight fails, evicting the set" do
    ctx = start_group(relight_fail_member: "worker-1")
    {:ok, _} = GroupManager.create_group(ctx.mgr)
    :ok = bank_complete(ctx)

    assert {:ok, endpoint, {:fresh, :relight_failed}} = GroupManager.wake_group(ctx.mgr)
    assert endpoint == %{ip: "10.0.0.9", port: 30_010}

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :running
    # The set was evicted (set_id cleared) and each member's snapshot_ref cleared.
    assert inst.set_id == nil
    assert Enum.all?(GroupStore.members(ctx.store, "g-1"), &is_nil(&1.snapshot_ref))

    # The banked snapshots were EvictSnapshot'd on the node.
    evicted = for {:evict_snapshot, ref} <- events(ctx.rec), do: ref
    assert Enum.sort(evicted) == ["snap-leader", "snap-worker-0", "snap-worker-1"]

    # CRITICAL (the collision fix): every member that DID resume (leader order 0,
    # worker-0 order 1) was StopGroupMember DESTROY'd BEFORE the fresh re-pin, so their
    # taps+IPs were freed. Without this the fresh boot would collide on .10/.11
    # (:ip_in_use, modeled by the allocator fake) and the whole wake would fail.
    destroyed =
      for {:stop_member, vm_id, :STOP_GROUP_MEMBER_MODE_DESTROY, _set, _name} <- events(ctx.rec), do: vm_id

    assert "vm-leader" in destroyed
    assert "vm-worker-0" in destroyed

    # Each DESTROY preceded the fresh re-pin of that member's IP (the allocator would
    # have rejected the fresh reserve otherwise, so the run reaching :ok proves order):
    # the successful {:ok, endpoint, {:fresh, ...}} above is the end-to-end proof.

    # After the fallback, every member was FRESH-started (mode FRESH), and the group
    # is whole again.
    fresh =
      events(ctx.rec)
      |> Enum.filter(&match?({:start_member, _, _, _, _, :START_GROUP_MEMBER_MODE_FRESH}, &1))
      |> Enum.map(fn {:start_member, name, _, _, _, _} -> name end)

    # Fresh starts happen both at create AND at the fallback: the fallback set is the
    # last three.
    assert Enum.take(fresh, -3) |> Enum.sort() == ["leader", "worker-0", "worker-1"]
  end

  test "gated relight cleanup defers fresh boot when a member teardown is unconfirmed" do
    ctx =
      start_group(
        relight_fail_member: "worker-1",
        node_confirmed_destroy: true,
        destroy_confirmed: false
      )

    {:ok, _} = GroupManager.create_group(ctx.mgr)
    :ok = bank_complete(ctx)

    assert {:error, {:member_teardown_unconfirmed, _}} = GroupManager.wake_group(ctx.mgr)
    assert {:ok, %{state: :banked}} = GroupStore.get(ctx.store, "g-1")

    destroyed =
      for {:stop_member, vm_id, :STOP_GROUP_MEMBER_MODE_DESTROY, _set, _name} <- events(ctx.rec),
          do: vm_id

    assert "vm-leader" in destroyed
    assert "vm-worker-0" in destroyed
  end

  test "wake_group treats an UNVERIFIED relight (clock-resync) as a failure and fresh-boots" do
    ctx = start_group(relight_unverified_member: "leader")
    {:ok, _} = GroupManager.create_group(ctx.mgr)
    :ok = bank_complete(ctx)

    assert {:ok, _endpoint, {:fresh, :clock_resync_failed}} = GroupManager.wake_group(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :running
    assert inst.set_id == nil
  end

  # -- bank_group (R5 Task 8) -------------------------------------------------

  test "bank_group banks the whole set under one set_id and records it atomically" do
    ctx = start_group()
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    # The sweeper moves running -> banking (unpublish + activator) BEFORE driving the
    # bank; bank_group operates from :banking.
    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)

    assert {:ok, %{set_id: set_id, banked: 3, pause_spread_ms: spread}} = GroupManager.bank_group(ctx.mgr)
    assert is_binary(set_id) and set_id != ""
    assert spread >= 0

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :banked
    assert inst.set_id == set_id

    # Every member banked under the SAME set_id (BANK mode), each stamped with its ref.
    banks =
      for {:stop_member, _vm, :STOP_GROUP_MEMBER_MODE_BANK, sid, name} <- events(ctx.rec), do: {sid, name}

    assert Enum.map(banks, &elem(&1, 0)) |> Enum.uniq() == [set_id], "one shared set_id"
    assert Enum.map(banks, &elem(&1, 1)) |> Enum.sort() == ["leader", "worker-0", "worker-1"]

    members = GroupStore.members(ctx.store, "g-1")
    assert Enum.all?(members, &(is_binary(&1.snapshot_ref) and &1.snapshot_ref != ""))
    assert Enum.all?(members, &(&1.vm_id == nil)), "the VMs are gone after bank"

    # ONE atomic group_banked op recorded the whole set.
    ops = load_ops(ctx, "group_banked")
    assert length(ops) == 1
  end

  test "bank_group ABORTS back to running when a member BANK fails (all-or-nothing)" do
    ctx = start_group(bank_fail_member: "worker-1")
    {:ok, _} = GroupManager.create_group(ctx.mgr)

    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)

    assert {:error, {:bank_partial, _}} = GroupManager.bank_group(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :running, "a failed member bank returns the group to running"
    assert inst.set_id == nil

    assert load_ops(ctx, "group_banked") == []
  end

  defp load_ops(ctx, kind) do
    atom = String.to_existing_atom(kind)
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    Enum.filter(ops, &(&1.kind == atom))
  end
end
