defmodule Embervm.RouterTest do
  @moduledoc """
  Request tests for the submit API, driving the LIVE Bandit instance the
  application boots (over the Embervm.Finch pool on loopback) with an injected
  fake authenticator, so no live Kubernetes API server is needed. `async: false`
  because these mutate global application env (the authenticator + sync knobs)
  and share the one running control-plane instance; unique workloads and
  idempotency keys keep tasks from colliding across tests.
  """
  use ExUnit.Case, async: false

  alias Embervm.TaskStore

  @allowed "system:serviceaccount:embervm:embervm"

  defmodule FakeAuth do
    @allowed "system:serviceaccount:embervm:embervm"
    def authenticate("good"), do: {:ok, @allowed}
    def authenticate("good2"), do: {:ok, "principal-2"}
    def authenticate("forbidden"), do: {:error, :forbidden}
    def authenticate(_), do: {:error, :unauthenticated}
  end

  setup do
    Application.put_env(:embervm, :authenticator, FakeAuth)

    on_exit(fn ->
      Application.delete_env(:embervm, :authenticator)
      Application.delete_env(:embervm, :sync_park_cap)
      Application.delete_env(:embervm, :sync_timeout_ms)
      Application.delete_env(:embervm, :quota)
      Application.delete_env(:embervm, :usage_admins)
    end)

    :ok
  end

  defp unique(prefix), do: "#{prefix}-#{System.unique_integer([:positive, :monotonic])}"

  defp req(method, path, headers \\ [], body \\ "") do
    {:ok, resp} =
      Finch.build(method, "http://127.0.0.1:8080#{path}", headers, body)
      |> Finch.request(Embervm.Finch)

    resp
  end

  defp auth(token), do: [{"authorization", "Bearer " <> token}]

  defp json(body), do: :json.decode(body)

  # -- auth ------------------------------------------------------------------

  test "a /v1 request without a token is 401" do
    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks")
    assert resp.status == 401
  end

  test "an unauthenticated token is 401" do
    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks", auth("nope"), "x")
    assert resp.status == 401
  end

  test "an authenticated but non-allow-listed token is 403" do
    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks", auth("forbidden"), "x")
    assert resp.status == 403
  end

  test "/healthz needs no auth" do
    resp = req(:get, "/healthz")
    assert resp.status == 200
    assert resp.body == "ok"
  end

  test "/v1/nodes needs auth and returns a node + dispatch snapshot" do
    assert req(:get, "/v1/nodes").status == 401

    resp = req(:get, "/v1/nodes", auth("good"))
    assert resp.status == 200
    body = json(resp.body)
    # Read-only operational introspection: both sections present and shaped, even
    # with no node wired in the test app (nodes empty, dispatch snapshot present).
    assert is_list(body["nodes"])
    assert is_map(body["dispatch"])
  end

  # -- submit ----------------------------------------------------------------

  test "async submit returns 202 and creates a queued task backed by the op-log" do
    wl = unique("wl")
    resp = req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "source-file-bytes")

    assert resp.status == 202
    body = json(resp.body)
    assert body["state"] == "queued"
    task_id = body["task_id"]
    assert is_binary(task_id)

    # The queued task is durable (write-through op-log projection surfaced via
    # the ETS hot set): GET reflects it.
    got = req(:get, "/v1/tasks/#{task_id}", auth("good"))
    assert got.status == 200
    view = json(got.body)
    assert view["state"] == "queued"
    assert view["workload"] == wl
    assert view["attempt"] == 1
  end

  test "a submit without X-Ember-Guest-Path stores NO path, so the workload invokePath applies" do
    wl = unique("wl")
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "x").body)["task_id"]

    {:ok, env} = TaskStore.get_request(task_id)
    # No "path" key: the dispatcher's `req_env["path"] || invoke_path` then falls
    # back to the workload's invokePath (e.g. /invoke), not a hard-coded "/".
    refute Map.has_key?(env, "path")
  end

  test "X-Ember-Guest-Path is recorded and overrides the workload invokePath" do
    wl = unique("wl")
    headers = auth("good") ++ [{"x-ember-guest-path", "/custom"}]
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]

    {:ok, env} = TaskStore.get_request(task_id)
    assert env["path"] == "/custom"
  end

  test "Idempotency-Key dedupes a resubmit to the same task" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    a = req(:post, "/v1/workloads/#{wl}/tasks", headers, "body")
    b = req(:post, "/v1/workloads/#{wl}/tasks", headers, "body")

    assert a.status == 202
    assert b.status == 202
    assert json(a.body)["task_id"] == json(b.body)["task_id"]
  end

  test "a body over 8 MiB is rejected 413" do
    # An over-cap POST is answered 413 BEFORE the server reads the whole body,
    # which leaves the HTTP/1.1 connection with an undrained tail. Reusing that
    # pooled connection for a later request would misframe and hang it, so this
    # abnormal request runs against its OWN throwaway Finch pool that no other
    # test shares; the app's shared Embervm.Finch pool is never poisoned.
    start_supervised!({Finch, name: __MODULE__.BigBodyFinch})
    wl = unique("wl")
    big = :binary.copy("a", 8_388_609)

    {:ok, resp} =
      Finch.build(:post, "http://127.0.0.1:8080/v1/workloads/#{wl}/tasks", auth("good"), big)
      |> Finch.request(__MODULE__.BigBodyFinch)

    assert resp.status == 413
  end

  # -- reads -----------------------------------------------------------------

  test "GET unknown task is 404" do
    resp = req(:get, "/v1/tasks/#{unique("nope")}", auth("good"))
    assert resp.status == 404
  end

  test "GET result is 404 before any result exists" do
    wl = unique("wl")
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "x").body)["task_id"]

    resp = req(:get, "/v1/tasks/#{task_id}/result", auth("good"))
    assert resp.status == 404
  end

  # -- sync ------------------------------------------------------------------

  test "sync submit returns the stored guest result when the task is already terminal" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    # Create the task, then drive it to success directly (no dispatcher in R0).
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(task_id, %{
        status_code: 201,
        body: "FINDINGS",
        size_bytes: 8,
        truncated: false
      })

    # A sync submit with the same idempotency key resolves to the existing,
    # now-terminal task; the already-terminal re-check returns its result without
    # a real park.
    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", headers, "x")
    assert resp.status == 201
    assert resp.body == "FINDINGS"
  end

  # -- guest response headers ------------------------------------------------

  defp header(resp, name) do
    Enum.find_value(resp.headers, fn {k, v} -> if String.downcase(k) == name, do: v end)
  end

  test "sync submit replays the guest content-type and strips framing headers" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(task_id, %{
        status_code: 200,
        body: "PNGDATA",
        size_bytes: 7,
        truncated: false,
        headers: %{
          "content-type" => "image/png",
          "x-custom" => "yes",
          # framing / hop-by-hop headers the server must own: stripped.
          "content-length" => "999",
          "transfer-encoding" => "chunked",
          "connection" => "keep-alive"
        }
      })

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", headers, "x")
    assert resp.status == 200
    assert resp.body == "PNGDATA"

    # The guest content-type wins over the octet-stream default.
    assert header(resp, "content-type") == "image/png"
    assert header(resp, "x-custom") == "yes"
    # x-ember-truncated is still set by the control plane.
    assert header(resp, "x-ember-truncated") == "false"
    # Framing headers the guest tried to set were stripped (the server owns them);
    # content-length reflects the real body, never the guest's bogus "999".
    assert header(resp, "transfer-encoding") == nil
    assert header(resp, "content-length") == "7"
  end

  test "sync submit falls back to octet-stream when the guest set no headers" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(task_id, %{
        status_code: 200,
        body: "BYTES",
        size_bytes: 5,
        truncated: false
      })

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", headers, "x")
    assert resp.status == 200
    assert header(resp, "content-type") == "application/octet-stream"
    assert header(resp, "x-ember-truncated") == "false"
  end

  test "sync submit is 429 when the principal's park cap is exhausted" do
    Application.put_env(:embervm, :sync_park_cap, 0)
    wl = unique("wl")

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", auth("good"), "x")
    assert resp.status == 429
    assert json(resp.body)["retryable"] == true
  end

  test "sync submit times out to 202 while work is still in flight" do
    Application.put_env(:embervm, :sync_timeout_ms, 60)
    wl = unique("wl")

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", auth("good"), "x")
    assert resp.status == 202
    assert json(resp.body)["state"] == "queued"
  end

  # -- DLQ + redrive ---------------------------------------------------------

  test "dead-letter listing and redrive round-trip" do
    wl = unique("wl")

    {:ok, :created, task_id} =
      TaskStore.submit(%{tenant: "homelab", principal: @allowed, workload: wl})

    {:ok, _} = TaskStore.assign(task_id)
    # guest4xx is always permanent, so this dead-letters in one step.
    {:ok, dl} = TaskStore.fail(task_id, :guest4xx)
    assert dl.state == :dead_lettered

    listed = req(:get, "/v1/workloads/#{wl}/dead-letters", auth("good"))
    assert listed.status == 200
    body = json(listed.body)
    assert body["total"] == 1
    assert [item] = body["items"]
    assert item["task_id"] == task_id
    assert item["state"] == "dead_lettered"

    redriven = req(:post, "/v1/tasks/#{task_id}/redrive", auth("good"))
    assert redriven.status == 200
    assert json(redriven.body)["state"] == "queued"

    got = req(:get, "/v1/tasks/#{task_id}", auth("good"))
    assert json(got.body)["state"] == "queued"
    # Attempt counter was reset to a fresh budget.
    assert json(got.body)["attempt"] == 1
  end

  test "redrive of a non-dead-lettered task is 409" do
    wl = unique("wl")
    {:ok, :created, task_id} = TaskStore.submit(%{tenant: "homelab", principal: @allowed, workload: wl})

    resp = req(:post, "/v1/tasks/#{task_id}/redrive", auth("good"))
    assert resp.status == 409
  end

  # -- metering + quotas (Task 12) -------------------------------------------

  # Drive a submitted task terminal with usage directly through the store (no
  # dispatcher in R0), so GET /v1/usage has a row to serve.
  defp bill(task_id, cpu_ms) do
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(
        TaskStore,
        task_id,
        %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil},
        %{cpu_ms: cpu_ms, peak_rss_mib: 1024, wall_ms: 1000}
      )
  end

  test "GET /v1/usage returns the caller's own billed usage" do
    wl = unique("wl")
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "x").body)["task_id"]
    bill(task_id, 3000)

    resp = req(:get, "/v1/usage", auth("good"))
    assert resp.status == 200

    mine = Enum.find(json(resp.body)["items"], &(&1["principal"] == @allowed))
    assert mine
    # 3000ms = 3.0 vCPU-s; accumulates across this file's tests, so >=.
    assert mine["vcpu_seconds"] >= 3.0
  end

  test "GET /v1/usage is self-scoped: a non-admin cannot read another principal" do
    resp = req(:get, "/v1/usage?principal=#{URI.encode_www_form(@allowed)}", auth("good2"))
    assert resp.status == 200
    # good2 (principal-2, not an admin) is forced to its own scope, never @allowed.
    refute Enum.any?(json(resp.body)["items"], &(&1["principal"] == @allowed))
  end

  test "a usage admin can read another principal via ?principal=" do
    Application.put_env(:embervm, :usage_admins, ["principal-2"])

    resp = req(:get, "/v1/usage?principal=#{URI.encode_www_form(@allowed)}", auth("good2"))
    assert resp.status == 200
    # Every returned row is the requested principal (the admin filter took effect).
    assert Enum.all?(json(resp.body)["items"], &(&1["principal"] == @allowed))
  end

  test "submit is 429 when the principal's daily vCPU-second quota is exhausted" do
    Application.put_env(:embervm, :quota, %{budgets: %{@allowed => 0.0}, default: nil})

    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks", auth("good"), "x")
    assert resp.status == 429
    body = json(resp.body)
    assert body["error"] =~ "quota"
    assert body["retryable"] == true
  end
end
