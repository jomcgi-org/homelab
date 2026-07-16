defmodule Embervm.EndpointPublisherTest do
  @moduledoc """
  Exercises Embervm.EndpointPublisher, the sole writer to the xDS sidecar. Covers
  the PURE-function projection (facts in ServingStore + the catalog, snapshot map
  out) including the empty-cluster activator swap in BOTH directions, the
  fixed-width monotonic version format (D-R3.5.1), debounce coalescing, the
  serving-node derivation (data-driven, no hardcoded node-4), the boot re-push,
  and that a PUT failure never blocks and is retried.

  A fake put_fun records every (node_id, desired) PUT into an Agent so a test can
  assert exactly what was pushed, and can be made to fail to prove the retry
  posture. The store + catalog + capacity tables are driven directly (the
  publisher reads only these facts).
  """
  use ExUnit.Case, async: true

  alias Embervm.{EndpointPublisher, NodeCapacity, ServingStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite

  # -- harness ---------------------------------------------------------------

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"pcap_#{suffix}"
    cat_table = :"pcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_publisher_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = ServingStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)

    {:ok, puts} = Agent.start_link(fn -> [] end)

    put_fun =
      Keyword.get(opts, :put_fun, fn node_id, desired ->
        Agent.update(puts, fn acc -> acc ++ [{node_id, desired}] end)
        :ok
      end)

    # A :seed callback runs against the store + tables BEFORE the publisher starts,
    # so an `active: true` boot flush sees the seeded facts deterministically (the
    # boot handle_continue races table population done after start_link otherwise).
    pre_ctx = %{store: store, cap_table: cap_table, cat_table: cat_table}

    case Keyword.get(opts, :seed) do
      nil -> :ok
      seed when is_function(seed, 1) -> seed.(pre_ctx)
    end

    pub_opts =
      [
        name: nil,
        store: store,
        catalog_table: cat_table,
        capacity_table: cap_table,
        put_fun: put_fun,
        # Tests drive flush/1 deterministically: no boot publish, no timers.
        active: Keyword.get(opts, :active, false),
        epoch: Keyword.get(opts, :epoch, 100),
        debounce_ms: Keyword.get(opts, :debounce_ms, 20),
        repush_ms: Keyword.get(opts, :repush_ms, 0),
        activator_endpoint: Keyword.get(opts, :activator_endpoint, %{ip: "10.1.1.1", port: 7000})
      ]

    {:ok, pub} = EndpointPublisher.start_link(pub_opts)

    %{
      pub: pub,
      store: store,
      cap_table: cap_table,
      cat_table: cat_table,
      puts: puts
    }
  end

  defp serving_workload(ctx, name, host) do
    WorkloadCatalog.upsert(ctx.cat_table, name, %{
      class: "serving",
      serving: %{host: host, port: 8080, health_path: "/healthz"}
    })
  end

  defp serving_node(ctx, node_id) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.99.0.0/24",
      serving_vms: [],
      serving_snapshots: []
    })
  end

  defp start_published(ctx, instance_id, workload, ip, port) do
    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "p1",
        workload: workload,
        node_id: "node-4",
        vm_id: "vm-" <> instance_id,
        ip: ip,
        port: port
      })

    {:ok, _} = ServingStore.publish(ctx.store, instance_id, ip, port, :started)
  end

  defp last_puts(ctx), do: Agent.get(ctx.puts, & &1)

  # -- pure projection: activator swap both directions ------------------------

  test "a serving workload with no live instance renders the activator endpoint (cold)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    :ok = EndpointPublisher.flush(ctx.pub)

    assert [{"node-4", desired}] = last_puts(ctx)
    assert [cluster] = desired.clusters
    assert cluster.name == "serve|wl-a"
    # Empty of real endpoints: the activator is the fallback.
    assert cluster.endpoints == [%{ip: "10.1.1.1", port: 7000}]

    assert [route] = desired.routes
    assert route.host == "wl-a.example"
    assert route.path_prefix == "/"
    assert route.cluster == "serve|wl-a"
    assert route.request_headers == %{"x-ember-workload" => "wl-a"}
  end

  test "publishing an instance swaps the activator OUT for the real endpoint" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    start_published(ctx, "srv-1", "wl-a", "10.99.0.5", 8080)
    :ok = EndpointPublisher.flush(ctx.pub)

    assert [{"node-4", desired}] = last_puts(ctx)
    assert [cluster] = desired.clusters
    assert cluster.endpoints == [%{ip: "10.99.0.5", port: 8080}]
    refute Enum.any?(cluster.endpoints, &(&1.ip == "10.1.1.1"))
  end

  test "unpublishing the last instance swaps the activator back IN" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    start_published(ctx, "srv-1", "wl-a", "10.99.0.5", 8080)
    {:ok, _} = ServingStore.unpublish(ctx.store, "srv-1", :drain)
    :ok = EndpointPublisher.flush(ctx.pub)

    assert [{"node-4", desired}] = last_puts(ctx)
    assert [cluster] = desired.clusters
    assert cluster.endpoints == [%{ip: "10.1.1.1", port: 7000}]
  end

  test "an unhealthy instance is ejected from the rendered cluster (activator swaps in)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    start_published(ctx, "srv-1", "wl-a", "10.99.0.5", 8080)
    {:ok, _} = ServingStore.set_health(ctx.store, "srv-1", false)
    :ok = EndpointPublisher.flush(ctx.pub)

    assert [{"node-4", desired}] = last_puts(ctx)
    assert [cluster] = desired.clusters
    assert cluster.endpoints == [%{ip: "10.1.1.1", port: 7000}]
  end

  test "multiple healthy instances render as one cluster with all endpoints, id-ordered" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    start_published(ctx, "srv-b", "wl-a", "10.0.0.2", 8080)
    start_published(ctx, "srv-a", "wl-a", "10.0.0.1", 8080)
    :ok = EndpointPublisher.flush(ctx.pub)

    assert [{"node-4", desired}] = last_puts(ctx)
    assert [cluster] = desired.clusters
    assert cluster.endpoints == [%{ip: "10.0.0.1", port: 8080}, %{ip: "10.0.0.2", port: 8080}]
  end

  # -- version format (D-R3.5.1) ---------------------------------------------

  test "format_version is fixed-width so lexical order equals numeric order" do
    v9 = EndpointPublisher.format_version(100, 9)
    v10 = EndpointPublisher.format_version(100, 10)

    assert String.length(v9) == 40
    assert String.length(v10) == 40
    # The trap a bare integer falls into: "10" < "9" lexically. Fixed-width fixes it.
    assert v9 < v10
    assert v10 > v9

    # A higher epoch always sorts above any counter at a lower epoch (restart wins).
    assert EndpointPublisher.format_version(101, 1) > EndpointPublisher.format_version(100, 999_999)
  end

  test "each flush advances the per-node version strictly" do
    ctx = start_stack(epoch: 500)
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    :ok = EndpointPublisher.flush(ctx.pub)
    :ok = EndpointPublisher.flush(ctx.pub)
    :ok = EndpointPublisher.flush(ctx.pub)

    versions = last_puts(ctx) |> Enum.map(fn {_n, d} -> d.version end)
    assert length(versions) == 3
    assert versions == Enum.sort(versions)
    assert Enum.uniq(versions) == versions
    # Fixed 40-char width throughout.
    assert Enum.all?(versions, &(String.length(&1) == 40))
  end

  # -- serving-node derivation (data-driven) ---------------------------------

  test "pushes to every serving-capable node, none when no node reports serving" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", "wl-a.example")

    # No serving node yet: a clean no-op (no PUT, no crash).
    :ok = EndpointPublisher.flush(ctx.pub)
    assert last_puts(ctx) == []

    # A non-serving node (no serving_subnet_cidr) is NOT a target.
    NodeCapacity.put(ctx.cap_table, "node-1", %{configured_id: "node-1", node_id: "node-1"})
    :ok = EndpointPublisher.flush(ctx.pub)
    assert last_puts(ctx) == []

    # Now a serving node appears: it becomes the target, keyed by configured_id.
    serving_node(ctx, "node-4")
    :ok = EndpointPublisher.flush(ctx.pub)
    assert [{"node-4", _desired}] = last_puts(ctx)
  end

  # -- debounce coalescing ----------------------------------------------------

  test "a burst of publish/0 casts coalesces into ONE PUT within the window" do
    ctx = start_stack(active: true, debounce_ms: 40)
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    # active: true ran a boot flush already; snapshot the count and clear.
    Process.sleep(10)
    Agent.update(ctx.puts, fn _ -> [] end)

    for _ <- 1..10, do: EndpointPublisher.publish(ctx.pub)
    # Within the 40ms window nothing has flushed yet.
    Process.sleep(15)
    assert last_puts(ctx) == []

    # After the window, exactly ONE coalesced PUT lands.
    Process.sleep(50)
    assert [{"node-4", _}] = last_puts(ctx)
  end

  # -- boot publish -----------------------------------------------------------

  test "boot does one synchronous publish before readiness (active: true)" do

    # Seed the facts BEFORE the publisher starts, so the boot handle_continue flush
    # sees a serving node + workload and pushes without any explicit flush/1.
    seed = fn pre ->
      WorkloadCatalog.upsert(pre.cat_table, "wl-a", %{
        class: "serving",
        serving: %{host: "wl-a.example", port: 8080, health_path: "/healthz"}
      })

      NodeCapacity.put(pre.cap_table, "node-4", %{
        configured_id: "node-4",
        node_id: "node-4",
        serving_subnet_cidr: "10.99.0.0/24",
        serving_vms: [],
        serving_snapshots: []
      })
    end

    ctx = start_stack(active: true, debounce_ms: 20, seed: seed)

    # A synchronous flush/1 serializes behind the boot handle_continue, so once it
    # returns the boot push has already happened. Assert a node-4 push exists.
    :ok = EndpointPublisher.flush(ctx.pub)
    assert Enum.any?(last_puts(ctx), fn {node, _} -> node == "node-4" end)
    # And the boot push carried the activator fallback (no live instance seeded).
    assert Enum.any?(last_puts(ctx), fn {_n, d} ->
             match?([%{name: "serve|wl-a", endpoints: [%{ip: "10.1.1.1", port: 7000}]}], d.clusters)
           end)
  end

  # -- PUT failure never blocks, retries --------------------------------------

  test "a PUT failure does not crash and re-uses the version on the next flush" do
    {:ok, fail?} = Agent.start_link(fn -> true end)
    {:ok, seen} = Agent.start_link(fn -> [] end)

    put_fun = fn node_id, desired ->
      Agent.update(seen, fn acc -> acc ++ [{node_id, desired.version}] end)

      if Agent.get(fail?, & &1) do
        {:error, :sidecar_down}
      else
        :ok
      end
    end

    ctx = start_stack(put_fun: put_fun, epoch: 700)
    serving_workload(ctx, "wl-a", "wl-a.example")
    serving_node(ctx, "node-4")

    # First flush fails (sidecar down): the publisher does not crash.
    :ok = EndpointPublisher.flush(ctx.pub)
    assert Process.alive?(ctx.pub)

    # Recover the sidecar; the retry re-uses the SAME version (the failed one was
    # never accepted), still strictly greater than the sidecar's last-accepted.
    Agent.update(fail?, fn _ -> false end)
    :ok = EndpointPublisher.flush(ctx.pub)

    versions = Agent.get(seen, & &1) |> Enum.map(fn {_n, v} -> v end)
    assert [v1, v2] = versions
    assert v1 == v2
  end
end
