defmodule Embervm.ZipLaneEchoTest do
  @moduledoc """
  Task 8 acceptance: the zip lane end to end with the echo function, encoded as a
  CI integration test with NO real VMs. It stitches the real WorkloadWatcher,
  BaseBuilder, and Dispatcher together against ONE fake daemon and asserts the
  same round-trip the live smoke test (testdata/echo + workload-echo-fn.yaml)
  runs against a real Firecracker guest:

      a zip-source Workload CR
        -> WorkloadWatcher parses source.zip, catalogs it (frozen port 1027,
           EMBER_HANDLER init_env), and triggers the BaseBuilder
        -> BaseBuilder maps the zip source to a proto ZipSource (resolved
           runtime image ref + archive url + sha256) and issues BuildBase to the
           FAKE daemon, which returns a snapshot
        -> a task submit drains through the Dispatcher, which "runs" the guest on
           the fake daemon: the fake daemon marshals the request into the shim's
           event shape and applies the echo handler (echo the event back as a
           JSON body), exactly as app.handle + shim.py would guest-side
        -> assert the returned GuestResponse body JSON-decodes to the marshaled
           event (body == the payload we sent).

  The fake daemon is the SAME injection seam the BaseBuilder and Dispatcher unit
  tests use (`build_fun`/`connect_fun` for the builder; `assign_fun`/`prime_fun`/
  `channel_fun` for the dispatcher). No apiserver, no vsock, no microVM.

  The echo marshaling here mirrors projects/embervm/runtimes/python/shim.py
  (`event_from_request`, `response_from_return`) and
  runtimes/python/testdata/echo/app.py (`handle`): the CI-enforced half of the
  acceptance keeps the Elixir control plane and the python guest fixture on the
  same contract.
  """
  use ExUnit.Case, async: true

  alias Embervm.{BaseBuilder, Dispatcher, NodeCapacity, TaskStore, WorkloadCatalog, WorkloadWatcher}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.{AssignResponse, GuestResponse, PrimeResponse, UsageStats, ZipSource}

  @node %{id: "node-4", address: "node-4:9090"}
  @runtime_images %{"python312" => "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/python:pinned"}

  # The echo zip CR, byte-shaped like a decoded apiserver object (binary keys).
  # Mirrors projects/embervm/crd/samples/workload-echo-fn.yaml.
  @echo_sha "53ff98ccb09d4d12a629322caac8ee0aee9f77ca69fd08fbc1eee83b7a60230b"
  @echo_uri "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333/faas/echo-fn/#{@echo_sha}.zip"

  defp echo_cr do
    %{
      "metadata" => %{"name" => "echo-fn", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "task",
        "source" => %{
          "zip" => %{
            "runtime" => "python312",
            "codeUri" => @echo_uri,
            "sha256" => @echo_sha,
            "handler" => "app.handle",
            "invokePath" => "/invoke",
            "readyPath" => "/shim/ready"
          }
        },
        "resources" => %{"vcpus" => 1, "memMib" => 512},
        "concurrency" => %{"floor" => 1, "cap" => 4},
        "invocation" => %{"timeoutSeconds" => 30}
      }
    }
  end

  # -- the echo, in Elixir, mirroring shim.py + app.py -----------------------

  # shim.event_from_request: marshal an inbound request into the normative event.
  # Empty body -> nil (the "no body" sentinel); valid utf-8 -> string.
  defp event_from_request(method, path, query, headers, raw_body) do
    {body, is_b64} =
      cond do
        raw_body == "" -> {nil, false}
        String.valid?(raw_body) -> {raw_body, false}
        true -> {Base.encode64(raw_body), true}
      end

    %{
      "httpMethod" => method,
      "path" => path,
      "queryStringParameters" => query,
      "headers" => headers,
      "body" => body,
      "isBase64Encoded" => is_b64
    }
  end

  # app.handle: echo the event back as a JSON response body; then
  # shim.response_from_return marshals {statusCode, headers, body} to the wire.
  # We return the (status_code, headers, body) the guest would put on the wire.
  defp run_echo(event) do
    body = event |> :json.encode() |> :erlang.iolist_to_binary()
    {200, %{"Content-Type" => "application/json"}, body}
  end

  # -- harness ----------------------------------------------------------------

  defp assert_eventually(fun, timeout_ms \\ 5_000, interval_ms \\ 10) do
    deadline = System.monotonic_time(:millisecond) + timeout_ms
    do_eventually(fun, deadline, interval_ms)
  end

  defp do_eventually(fun, deadline, interval_ms) do
    cond do
      fun.() -> :ok
      System.monotonic_time(:millisecond) >= deadline -> flunk("condition was not met within the timeout")
      true ->
        Process.sleep(interval_ms)
        do_eventually(fun, deadline, interval_ms)
    end
  end

  test "a zip-source echo Workload builds a base and a submit returns the echoed event" do
    suffix = System.unique_integer([:positive])
    cat_table = :"ziplane_cat_#{suffix}"
    cap_table = :"ziplane_cap_#{suffix}"
    depth_table = :"ziplane_depth_#{suffix}"
    disp_name = :"ziplane_disp_#{suffix}"
    test_pid = self()

    # The WorkloadWatcher owns the catalog table: its init calls
    # WorkloadCatalog.create(cat_table), so the test must NOT pre-create it (a
    # named ETS table cannot be created twice: "table name already exists").
    NodeCapacity.create(cap_table)

    # Seed a capacity fact for the builder node so placement succeeds
    NodeCapacity.put(cap_table, {"node-4", "ds"}, %{
      node_id: "node-4",
      configured_id: "node-4",
      instance_id: "node-4",
      cpu_vendor: "amd",
      size_class: "8gi",
      mem_budget_mib: 8_192,
      mem_headroom_mib: 8_000,
      live_vms: 0,
      max_live_vms: 8,
      # The Dispatcher path reads fact.workloads with strict access, so the
      # seeded fact must carry the key even before any workload reports.
      workloads: %{},
      updated_at: 0
    })

    # -- BaseBuilder over a fake daemon: a ZipSource build -> a snapshot. ------
    build_fun = fn :fake_channel, req ->
      send(test_pid, {:build_req, req})
      {:ok, %Embervm.Node.V1.BuildBaseResponse{snapshot_ref: "echo-snap", image_digest: "sha256:echo", base_size_bytes: 1, arch: "amd64"}}
    end

    {:ok, builder} =
      BaseBuilder.start_link(
        name: nil,
        nodes: [@node],
        connect_fun: fn _addr -> {:ok, :fake_channel} end,
        disconnect_fun: fn :fake_channel -> :ok end,
        build_fun: build_fun,
        runtime_images: @runtime_images,
        # A no-op status writer: this test asserts on the build request + the
        # submit round-trip, not on the CR status patch (the watcher/builder unit
        # tests cover status).
        status_writer: fn _ns, _name, _status -> :ok end,
        capacity_table: cap_table
      )

    # -- WorkloadWatcher parses the CR, catalogs it, and drives the builder. ---
    lister = fn -> {:ok, [echo_cr()]} end

    {:ok, watcher} =
      WorkloadWatcher.start_link(
        name: nil,
        table: cat_table,
        lister: lister,
        status_writer: fn _ns, _name, _status -> :ok end,
        watch_startup: false,
        base_reconcile_fun: fn desc -> BaseBuilder.reconcile(builder, desc) end,
        base_forget_fun: fn name -> BaseBuilder.forget(builder, name) end
      )

    :ok = WorkloadWatcher.reconcile_now(watcher)

    # The CR is cataloged on the frozen zip contract (port 1027, /invoke), and
    # the build request carried the resolved ZipSource (runtime image + url + sha).
    assert {:ok, entry} = WorkloadCatalog.fetch(cat_table, "echo-fn")
    assert entry.image_ref == nil
    assert entry.port == 1027
    assert entry.invoke_path == "/invoke"
    assert entry.zip.sha256 == @echo_sha

    assert_receive {:build_req, req}, 2_000
    assert {:zip, %ZipSource{} = zip} = req.source
    assert zip.runtime_image_ref == @runtime_images["python312"]
    assert zip.archive_url == @echo_uri
    assert zip.archive_sha256 == @echo_sha
    assert req.init_env == %{"EMBER_HANDLER" => "app.handle"}

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["echo-fn"].snapshot_ref == "echo-snap" end)

    # -- Dispatcher over the SAME fake daemon: "run" the echo guest. -----------
    # assign_fun marshals the request the way the shim would and applies the echo
    # handler, returning the exact GuestResponse a restored echo microVM would.
    assign_fun = fn :ch, req ->
      # AssignRequest.request is a GuestRequest{method, path, headers, body}; body
      # is the raw (already-decoded) request bytes, method is "POST", and the
      # guest contract carries no query string, so queryStringParameters is nil
      # (matching the shim's event on a no-query POST).
      gr = req.request
      event = event_from_request(gr.method, gr.path, nil, Map.new(gr.headers), gr.body)
      {status, headers, body} = run_echo(event)

      {:ok,
       %AssignResponse{
         response: %GuestResponse{status_code: status, headers: headers, body: body},
         usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}
       }}
    end

    path = Path.join(System.tmp_dir!(), "embervm_ziplane_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = TaskStore.start_link(name: nil, op_log: op_log, on_queued: fn t -> Dispatcher.enqueue(disp_name, t) end)

    {:ok, _disp} =
      Dispatcher.start_link(
        name: disp_name,
        task_store: store,
        capacity_table: cap_table,
        catalog_table: cat_table,
        depth_table: depth_table,
        clock: fn -> 1_000_000 end,
        channel_fun: fn _node -> {:ok, :ch} end,
        assign_fun: assign_fun,
        prime_fun: fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "echo-vm-#{System.unique_integer([:positive])}"}} end,
        start_sweep: false
      )

    # The base is ready on the node (the snapshot the builder produced); a
    # miss-path prime + assign runs the guest.
    NodeCapacity.put(cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{
        "echo-fn" => %{
          free_primed_slots: 0,
          snapshot_ref: "echo-snap",
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: []
        }
      },
      mem_headroom_mib: 4096,
      cpu_headroom_millicores: 4000,
      live_vms: 0,
      max_live_vms: 8,
      draining: false,
      updated_at: 1_000_000
    })

    payload = "hello ember"

    {:ok, :created, tid} =
      TaskStore.submit(store, %{
        tenant: "homelab",
        principal: "system:serviceaccount:embervm:embervm",
        workload: "echo-fn",
        request: %{path: "/invoke", headers: %{}, body_b64: Base.encode64(payload)}
      })

    assert_eventually(fn -> match?({:ok, %{state: :succeeded}}, TaskStore.get(store, tid)) end)

    assert {:ok, %{status_code: 200, body: body}} = TaskStore.get_result(store, tid)

    # THE ACCEPTANCE: the guest response body is the marshaled event, and its
    # `body` field is exactly the payload we submitted. The echo round-tripped.
    decoded = :json.decode(body)
    assert decoded["httpMethod"] == "POST"
    assert decoded["path"] == "/invoke"
    assert decoded["body"] == payload
    assert decoded["isBase64Encoded"] == false
  end
end
