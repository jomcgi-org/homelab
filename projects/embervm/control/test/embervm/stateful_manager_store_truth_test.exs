defmodule Embervm.StatefulManagerStoreTruthTest do
  use ExUnit.Case, async: false
  import ExUnit.CaptureLog

  alias Embervm.{NodeCapacity, StatefulManager, StatefulStore, WorkloadCatalog}
  alias Embervm.KeyService.Envelope
  alias Embervm.Node.V1.{RestoreArtifactResponse, StartStatefulResponse}
  alias Embervm.OpLog.SQLite

  defmodule FakePublisher do
    use GenServer

    def start_link, do: GenServer.start_link(__MODULE__, nil)

    @impl true
    def init(state), do: {:ok, state}

    @impl true
    def handle_cast(:publish, state), do: {:noreply, state}
  end

  defmodule CapabilityS3 do
    def get(agent, _key), do: Agent.get(agent, & &1)
  end

  defmodule CapabilityKeys do
    def current_epoch(agent, _principal), do: Agent.get(agent, &{:ok, &1.epoch})

    def set_epoch(agent, _principal, epoch, _reason) do
      Agent.update(agent, &%{&1 | epoch: epoch})
      {:ok, epoch}
    end

    def unwrap(agent, _envelope), do: Agent.get(agent, & &1.unwrap)
  end

  test "confirmed-gone anchor adopts an older blessed store generation before restore" do
    parent = self()
    enable_restore_capability()
    {volume_store, store_calls} = complete_volume_store(7)

    restore_fun = fn _channel, request ->
      send(parent, {:restore_request, request})

      {:ok,
       %RestoreArtifactResponse{
         bytes_moved: 4_096,
         skipped: false,
         generation: 7
       }}
    end

    start_fun = fn _channel, request ->
      assert request.blessed_generation == 9

      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-restored",
         ip: "10.88.0.7",
         port: 5432,
         generation: 9,
         was_relight: false
       }}
    end

    ctx =
      start_stack(
        volume_store: volume_store,
        restore_artifact_fun: restore_fun,
        start_stateful_fun: start_fun
      )

    prepare_confirmed_missing_anchor(ctx, 8, 8, 7)

    assert {:ok, %{generation: 9}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert_receive {:restore_request, request}
    assert capability_generation(request.capability) == 7

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 1

    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)

    assert Enum.any?(ops, fn op ->
             op.kind == :volume_recovery_updated and
               op.payload["generation"] == 7 and
               op.payload["exported_generation"] == 7
           end)

    assert %{node_id: "node-live", generation: 9, exported_generation: 7} =
             StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "confirmed-gone anchor with equal row and store generations keeps restore request unchanged" do
    parent = self()
    enable_restore_capability()
    {volume_store, store_calls} = complete_volume_store(7)

    restore_fun = fn _channel, request ->
      send(parent, {:restore_request, request})
      {:ok, %RestoreArtifactResponse{bytes_moved: 4_096, skipped: false, generation: 7}}
    end

    ctx = start_stack(volume_store: volume_store, restore_artifact_fun: restore_fun)
    prepare_confirmed_missing_anchor(ctx, 7, 7, 7)

    assert {:ok, %{generation: 8}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert_receive {:restore_request, request}
    assert capability_generation(request.capability) == 7

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 0
  end

  test "newer unblessed store generation is refused without adoption or restore" do
    {volume_store, store_calls} = complete_volume_store(8, blessed_generation: 7)
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7, 7, 8)

    log =
      capture_log(
        [format: "$message $metadata\n", metadata: [:workload, :anchor, :row_generation, :store_generation]],
        fn ->
          for _ <- 1..2 do
            assert {:error, {:wake_failed, :volume_node_gone}} =
                     StatefulManager.wake(ctx.mgr, "wl-a", "p")
          end
        end
      )

    assert log =~ "embervm stateful: volume restore refused, store generation unblessed"
    assert log =~ "workload=wl-a"
    assert log =~ "anchor=node-dead"
    assert log =~ "row_generation=7"
    assert log =~ "store_generation=8"

    assert %{node_id: "node-dead", generation: 7, exported_generation: 8} =
             StatefulStore.get_volume(ctx.store, "wl-a")

    assert Agent.get(ctx.restore_calls, & &1) == []
    assert Agent.get(ctx.start_calls, & &1) == []

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 1
  end

  test "restore generation mismatch on confirmed-gone anchor refuses cold fallback" do
    parent = self()
    {volume_store, _store_calls} = complete_volume_store(7)

    restore_fun = fn _channel, request ->
      send(parent, {:restore_request, request})

      {:error,
       %GRPC.RPCError{
         status: 7,
         message: "noded: restore capability generation mismatch"
       }}
    end

    ctx = start_stack(volume_store: volume_store, restore_artifact_fun: restore_fun)
    prepare_confirmed_missing_anchor(ctx, 8, 8, 7)

    log =
      capture_log(
        [format: "$message $metadata\n", metadata: [:workload, :anchor, :row_generation, :store_generation]],
        fn ->
          assert {:error, {:wake_failed, :volume_restore_failed_anchor_gone}} =
                   StatefulManager.wake(ctx.mgr, "wl-a", "p")
        end
      )

    assert log =~
             "embervm stateful: volume restore failed on confirmed-gone anchor, cold fallback refused"

    assert length(
             :binary.matches(
               log,
               "embervm stateful: volume restore failed on confirmed-gone anchor, cold fallback refused"
             )
           ) == 1

    assert log =~ "workload=wl-a"
    assert log =~ "anchor=node-dead"
    assert log =~ "row_generation=8"
    assert log =~ "store_generation=7"
    assert_receive {:restore_request, _request}
    assert Agent.get(ctx.start_calls, & &1) == []

    assert %{node_id: "node-dead", generation: 7, exported_generation: 7} =
             StatefulStore.get_volume(ctx.store, "wl-a")

    {:ok, rebuilt} =
      StatefulStore.start_link(name: nil, op_log: ctx.op_log, clock: fn -> 200_000 end)

    assert %{node_id: "node-dead", generation: 7, exported_generation: 7} =
             StatefulStore.get_volume(rebuilt, "wl-a")
  end

  test "node gone and complete blessed store export heals projection and restores" do
    {volume_store, store_calls} = complete_volume_store(7)
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7)

    assert {:ok, %{generation: 8}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 1

    assert [%{artifact: %{kind: :ARTIFACT_KIND_VOLUME, workload: "wl-a"}}] =
             Agent.get(ctx.restore_calls, & &1)

    assert %{node_id: "node-live", generation: 8, exported_generation: 7} =
             StatefulStore.get_volume(ctx.store, "wl-a")

    {:ok, rebuilt} =
      StatefulStore.start_link(name: nil, op_log: ctx.op_log, clock: fn -> 200_000 end)

    assert %{node_id: "node-live", exported_generation: 7} =
             StatefulStore.get_volume(rebuilt, "wl-a")
  end

  test "banked instance on a confirmed gone anchor consults store truth when projected export is absent" do
    {volume_store, store_calls} = complete_volume_store(7)
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7)
    seed_banked_with_pair(ctx, "stf-dead-anchor", 7)

    assert %{exported_generation: 0} = StatefulStore.get_volume(ctx.store, "wl-a")
    assert {:ok, %{generation: 8}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 1

    {:ok, evicted} = StatefulStore.get(ctx.store, "stf-dead-anchor")
    assert evicted.state == :evicted
    assert evicted.terminal_reason == "anchor_gone"
  end

  test "encrypted generation metadata adopts through the durable blessing watermark" do
    {volume_store, store_calls} = complete_volume_store(7, encrypted: true)
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7)

    assert {:ok, %{generation: 8}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    refute Enum.any?(Agent.get(store_calls, & &1), fn
             {:get, key} -> String.ends_with?(key, "/gen") or String.ends_with?(key, "/genblessed")
             _call -> false
           end)

    assert %{node_id: "node-live", exported_generation: 7} =
             StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "encrypted generation metadata without a lease or watermark fails closed" do
    {volume_store, _store_calls} = complete_volume_store(7, encrypted: true)
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7, :none)

    # The reason travels as structured metadata, so the capture must format it.
    log =
      capture_log([format: "$message $metadata\n", metadata: [:reason]], fn ->
        assert {:error, {:wake_failed, :volume_node_gone}} =
                 StatefulManager.wake(ctx.mgr, "wl-a", "p")
      end)

    assert log =~ "generation_not_blessed"
    assert %{node_id: "node-dead", exported_generation: 0} =
             StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "node gone and incomplete or unblessed store export stays volume_node_gone" do
    cases = [
      {7, [missing: "gen"], 7},
      {7, [missing: "vol.img"], 7},
      {8, [blessed_generation: 7], 7}
    ]

    for {generation, store_opts, blessed_generation} <- cases do
      {volume_store, _store_calls} = complete_volume_store(generation, store_opts)
      ctx = start_stack(volume_store: volume_store)
      prepare_confirmed_missing_anchor(ctx, generation, blessed_generation)

      assert {:error, {:wake_failed, :volume_node_gone}} =
               StatefulManager.wake(ctx.mgr, "wl-a", "p")

      assert %{node_id: "node-dead", exported_generation: 0} =
               StatefulStore.get_volume(ctx.store, "wl-a")
    end
  end

  test "node gone and store unreachable stays volume_node_gone" do
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    volume_store = %{
      list: fn _prefix ->
        Agent.update(calls, &(&1 + 1))
        {:error, :econnrefused}
      end,
      get: fn _key -> flunk("GET must not follow a failed listing") end
    }

    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7)

    assert {:error, {:wake_failed, :volume_node_gone}} =
             StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert Agent.get(calls, & &1) == 1

    assert %{node_id: "node-dead", exported_generation: 0} =
             StatefulStore.get_volume(ctx.store, "wl-a")
  end

  test "a definitive negative store consult is memoized within the TTL" do
    {volume_store, store_calls} = complete_volume_store(7, missing: "gen")
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7)

    for _ <- 1..2 do
      assert {:error, {:wake_failed, :volume_node_gone}} =
               StatefulManager.wake(ctx.mgr, "wl-a", "p")
    end

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 1
  end

  test "late store consult from a finished wake cannot affect its successor" do
    {volume_store, store_calls} = complete_volume_store(7, list_blocker: self())
    ctx = start_stack(volume_store: volume_store)
    prepare_confirmed_missing_anchor(ctx, 7)

    first = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    assert_receive {:store_list_waiting, first_worker}

    send(ctx.mgr, {:wake_done, "wl-a", {:error, :forced_finish}})
    assert {:error, {:wake_failed, :forced_finish}} = Task.await(first)

    successor = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    assert_receive {:store_list_waiting, successor_worker}

    first_worker_ref = Process.monitor(first_worker)
    :ok = :sys.suspend(ctx.mgr)
    send(first_worker, :release_store_list)
    assert_receive {:DOWN, ^first_worker_ref, :process, ^first_worker, :normal}
    :ok = :sys.resume(ctx.mgr)
    _ = :sys.get_state(ctx.mgr)

    assert StatefulStore.blessing_watermark(ctx.store, "wl-a") == 7
    assert %{exported_generation: 0} = StatefulStore.get_volume(ctx.store, "wl-a")
    assert Agent.get(ctx.restore_calls, & &1) == []
    assert Agent.get(ctx.start_calls, & &1) == []

    send(successor_worker, :release_store_list)
    assert {:ok, %{generation: 8}} = Task.await(successor)

    assert StatefulStore.blessing_watermark(ctx.store, "wl-a") == 8
    assert length(Agent.get(ctx.restore_calls, & &1)) == 1
    assert length(Agent.get(ctx.start_calls, & &1)) == 1
    assert Enum.count(Agent.get(store_calls, & &1), fn {verb, _key} -> verb == :list end) == 2
  end

  test "store discovery warnings distinguish incomplete, unblessed, and blessing read failures" do
    cases = [
      {[missing: "gen"], 7, :incomplete_volume_export},
      {[blessed_generation: 6], :none, :generation_not_blessed},
      {[get_error: "genblessed"], :none, :blessing_read_failed}
    ]

    for {store_opts, watermark, expected_reason} <- cases do
      {volume_store, _store_calls} = complete_volume_store(7, store_opts)
      ctx = start_stack(volume_store: volume_store)
      prepare_confirmed_missing_anchor(ctx, 7, watermark)

      log =
        capture_log([format: "$message $metadata\n", metadata: [:reason]], fn ->
          assert {:error, {:wake_failed, :volume_node_gone}} =
                   StatefulManager.wake(ctx.mgr, "wl-a", "p")
        end)

      assert log =~ Atom.to_string(expected_reason)
    end
  end

  test "a healed projection skips the store consult on the next wake" do
    {volume_store, store_calls} = complete_volume_store(7)

    ctx =
      start_stack(
        volume_store: volume_store,
        restore_artifact_fun: fn _channel, _request -> {:error, :restore_failed} end,
        start_stateful_fun: fn _channel, _request -> {:error, :volume_absent} end
      )

    prepare_confirmed_missing_anchor(ctx, 7)

    # The restore is attempted and refused on the confirmed-gone anchor, and the
    # cold fallback is refused rather than looping on a missing volume (#5708).
    assert {:error, {:wake_failed, :volume_restore_failed_anchor_gone}} =
             StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert %{node_id: "node-dead", exported_generation: 7} =
             StatefulStore.get_volume(ctx.store, "wl-a")

    assert {:error, {:wake_failed, :volume_restore_failed_anchor_gone}} =
             StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert Agent.get(
             store_calls,
             &Enum.count(&1, fn {verb, _key} -> verb == :list end)
           ) == 1
  end

  defp start_stack(opts) do
    suffix = System.unique_integer([:positive])
    capacity_table = :"store_truth_capacity_#{suffix}"
    catalog_table = :"store_truth_catalog_#{suffix}"
    NodeCapacity.create(capacity_table)
    WorkloadCatalog.create(catalog_table)

    path = Path.join(System.tmp_dir!(), "embervm_stateful_store_truth_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)

    {:ok, store} =
      StatefulStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)

    {:ok, publisher} = FakePublisher.start_link()
    {:ok, now} = Agent.start_link(fn -> 0 end)
    {:ok, restore_calls} = Agent.start_link(fn -> [] end)
    {:ok, start_calls} = Agent.start_link(fn -> [] end)

    configured_start_stateful_fun =
      Keyword.get(opts, :start_stateful_fun, fn _channel, _request ->
        {:ok,
         %StartStatefulResponse{
           vm_id: "vm-restored",
           ip: "10.88.0.7",
           port: 5432,
           generation: 8,
           was_relight: false
         }}
      end)

    start_stateful_fun = fn channel, request ->
      Agent.update(start_calls, &[request | &1])
      configured_start_stateful_fun.(channel, request)
    end

    restore_artifact_fun =
      Keyword.get(opts, :restore_artifact_fun, fn _channel, request ->
        Agent.update(restore_calls, &[request | &1])

        {:ok,
         %RestoreArtifactResponse{
           bytes_moved: 4_096,
           skipped: false,
           generation: 7
         }}
      end)

    workload = %{
      class: "stateful",
      namespace: "embervm-workloads",
      stateful: %{
        port: 5432,
        listen_port: 9100,
        volume_size_gib: 1,
        volume_mount_path: "/data",
        idle_bank_seconds: 300,
        max_lifetime_seconds: 86_400,
        banked_ttl_seconds: 604_800,
        wake_timeout_seconds: 60,
        secret_ref: nil
      }
    }

    WorkloadCatalog.upsert(catalog_table, "wl-a", workload)

    NodeCapacity.put(capacity_table, "node-live", %{
      configured_id: "node-live",
      node_id: "node-live",
      pod_uid: "pod-live",
      cpu_vendor: "amd",
      serving_subnet_cidr: "10.88.0.0/24",
      max_live_vms: 4,
      live_vms: 0,
      workloads: %{
        "wl-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap-a"}
      },
      stateful_vms: [],
      stateful_bundles: [],
      volumes: [],
      store_reachable: true
    })

    {:ok, mgr} =
      StatefulManager.start_link(
        name: nil,
        store: store,
        publisher: publisher,
        capacity_table: capacity_table,
        catalog_table: catalog_table,
        clock: fn -> Agent.get(now, & &1) end,
        channel_fun: fn _node -> {:ok, :channel} end,
        invalidate_fun: fn _node, _channel -> :ok end,
        start_stateful_fun: start_stateful_fun,
        stop_stateful_fun: fn _channel, _request -> {:ok, %{}} end,
        delete_volume_fun: fn _channel, _request -> {:ok, %{}} end,
        restore_artifact_fun: restore_artifact_fun,
        volume_store: Keyword.fetch!(opts, :volume_store),
        get_secret_fun: fn _namespace, _name -> {:ok, %{}} end,
        op_log: op_log,
        reconcile_interval_ms: 0,
        id_fun: fn -> "stf-store-truth-#{suffix}" end
      )

    %{
      mgr: mgr,
      store: store,
      op_log: op_log,
      now: now,
      restore_calls: restore_calls,
      start_calls: start_calls
    }
  end

  defp prepare_confirmed_missing_anchor(ctx, generation, blessed_generation \\ nil) do
    prepare_confirmed_missing_anchor(ctx, generation, blessed_generation, 0)
  end

  defp prepare_confirmed_missing_anchor(
         ctx,
         generation,
         blessed_generation,
         exported_generation
       ) do
    {:ok, _volume} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-dead",
        generation: generation,
        exported_generation: exported_generation,
        size_bytes: 100,
        allocated_bytes: 10
      })

    if blessed_generation != :none do
      {:ok, _blessing} =
        StatefulStore.bless_generation(ctx.store, "wl-a", blessed_generation || generation)
    end

    :ok = StatefulManager.reconcile(ctx.mgr)
    Agent.update(ctx.now, fn _ -> 90_000 end)
  end

  defp enable_restore_capability do
    saved_env =
      for key <- [
            :artifact_encryption,
            :artifact_store_client,
            :artifact_key_service,
            :noded_bearer_token
          ],
          into: %{},
          do: {key, Application.get_env(:embervm, key)}

    envelope =
      Envelope.encode(%Envelope{
        principal: "system:stateful:wl-a",
        epoch: 0,
        nonce: :binary.copy(<<1>>, 12),
        tag: :binary.copy(<<2>>, 16),
        wrapped_key: :binary.copy(<<3>>, 32)
      })

    meta = :json.encode(%{envelope: Base.encode64(envelope)}) |> IO.iodata_to_binary()
    {:ok, s3} = Agent.start_link(fn -> {:ok, meta} end)
    {:ok, keys} = Agent.start_link(fn -> %{epoch: 0, unwrap: {:ok, :binary.copy(<<4>>, 32)}} end)

    Application.put_env(:embervm, :artifact_encryption, true)
    Application.put_env(:embervm, :artifact_store_client, {CapabilityS3, s3})
    Application.put_env(:embervm, :artifact_key_service, {CapabilityKeys, keys})
    Application.put_env(:embervm, :noded_bearer_token, "manager-shared-bearer")

    on_exit(fn ->
      Enum.each(saved_env, fn
        {key, nil} -> Application.delete_env(:embervm, key)
        {key, value} -> Application.put_env(:embervm, key, value)
      end)
    end)
  end

  defp capability_generation(capability) do
    <<1, _expiry::unsigned-64, key_size::unsigned-16, rest::binary>> = capability
    <<_key::binary-size(key_size), tuple_size::unsigned-16, rest::binary>> = rest
    <<tuple_json::binary-size(tuple_size), _mac::binary-32>> = rest
    tuple_json |> Jason.decode!() |> Map.fetch!("generation")
  end

  defp seed_banked_with_pair(ctx, instance_id, generation) do
    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-dead",
        vm_id: "vm-#{instance_id}",
        generation: generation
      })

    {:ok, _} = StatefulStore.publish(ctx.store, instance_id, "10.88.0.9", 5432, :started)
    {:ok, _} = StatefulStore.unpublish(ctx.store, instance_id, :bank)

    {:ok, _} =
      StatefulStore.transition(
        ctx.store,
        instance_id,
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "stateful/#{instance_id}", size_bytes: 10, snapshot_generation: generation},
        %{snapshot_ref: "stateful/#{instance_id}", snapshot_generation: generation, snapshot_size_bytes: 10, vm_id: nil}
      )
  end

  defp complete_volume_store(generation, opts \\ []) do
    prefix = "volume/wl-a"
    blessed_generation = Keyword.get(opts, :blessed_generation, generation)

    encoding =
      if Keyword.get(opts, :encrypted, false) do
        %{"encryption" => "aes-256-gcm-v1"}
      else
        %{}
      end

    files = %{
      "vol.img" => %{"size" => 10, "sha256" => String.duplicate("a", 64)},
      "gen" => Map.merge(%{"size" => 1, "sha256" => String.duplicate("b", 64)}, encoding),
      "genblessed" => Map.merge(%{"size" => 1, "sha256" => String.duplicate("c", 64)}, encoding)
    }

    objects = %{
      (prefix <> "/meta.json") => Jason.encode!(%{"files" => files, "generation" => generation}),
      (prefix <> "/vol.img") => "volume-data",
      (prefix <> "/gen") => Integer.to_string(generation),
      (prefix <> "/genblessed") => Integer.to_string(blessed_generation)
    }

    objects =
      case Keyword.get(opts, :missing) do
        nil -> objects
        object -> Map.delete(objects, prefix <> "/" <> object)
      end

    {:ok, calls} = Agent.start_link(fn -> [] end)

    volume_store = %{
      list: fn key ->
        assert key == prefix <> "/"
        Agent.update(calls, &[{:list, key} | &1])

        if blocker = Keyword.get(opts, :list_blocker) do
          send(blocker, {:store_list_waiting, self()})

          receive do
            :release_store_list -> :ok
          end
        end

        entries =
          Enum.map(objects, fn {object_key, body} ->
            %{key: object_key, size: byte_size(body), last_modified_ms: 0}
          end)

        {:ok, entries}
      end,
      get: fn key ->
        Agent.update(calls, &[{:get, key} | &1])

        get_error = Keyword.get(opts, :get_error)

        if is_binary(get_error) and String.ends_with?(key, "/" <> get_error) do
          {:error, :econnrefused}
        else
          case Map.fetch(objects, key) do
            {:ok, body} -> {:ok, body}
            :error -> {:error, :not_found}
          end
        end
      end
    }

    {volume_store, calls}
  end
end
